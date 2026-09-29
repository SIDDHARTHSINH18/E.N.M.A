"""
GHOST — foundational automation engine (M3-E).

Runs a Task through an ordered list of steps, where every
step executes a tool through the existing M3-D agent:

  Task -> AutomationEngine -> ordered TaskSteps
       -> Agent -> PermissionPolicy -> ToolRegistry
       -> execution result -> next step / pause / failure

Rules:
- Steps run strictly sequentially in `order` — deterministic
  and synchronous.
- Permission checking is NOT duplicated here: each step
  goes through Agent.execute, which consults the shared
  PermissionPolicy before any tool runs.
- A successful step advances to the next one.
- A step requiring approval PAUSES the workflow (not a
  failure); the step stays PENDING for a later approval
  step to resume.
- A denied or failed step stops the workflow and records
  the failure on the step and the task.
- All steps succeeding marks the Task COMPLETED.

This is not a second orchestrator: model-level
orchestration remains in core.orchestrator; the engine is
a task-level runner that reuses the Agent executor.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from backend.agents.executor import Agent, ExecutionResult
from backend.core.task import Task, TaskStatus
from backend.permissions.policy import PermissionDecision

# Cap for a value pulled from another step's output through a
# {"from_step": N, "field": ...} reference: bounded data flow,
# never an unbounded object hand-off.
MAX_REF_CHARS = 8000


class TaskStep:
    """
    One ordered unit of work inside a Task.

    Status reuses TaskStatus — one status vocabulary for
    tasks and steps, no second state model.
    """

    def __init__(
        self,
        tool_name: str,
        params: Optional[dict] = None,
        order: int = 0,
        title: str = "",
        dependencies: Optional[list] = None,
    ):
        self.id: str = str(uuid4())
        self.order: int = order
        self.title: str = title or tool_name
        self.tool_name: str = tool_name
        # Copy dict params (the common case). Other shapes
        # (e.g. a top-level list, or a list of M5.6 reference
        # objects) are stored as given — coercing them with
        # dict() would silently mangle a 2-key dict into a
        # bogus {"key": "value"} mapping.
        self.params: dict = (
            dict(params) if isinstance(params, dict) else (params if params else {})
        )
        # Orders of steps that must be COMPLETED before this
        # one may run. The engine enforces this strictly:
        # an unsatisfied dependency fails the step (and the
        # workflow) instead of executing it out of order.
        self.dependencies: list = list(dependencies or [])
        self.status: TaskStatus = TaskStatus.PENDING
        # Permission decision this step actually received from
        # the shared PermissionPolicy (None before execution).
        # Recorded for audit and for safe-retry gating; the
        # decision itself is always made by the policy, never
        # here.
        self.decision = None
        self.result: Any = None
        self.error: Optional[str] = None

    def __repr__(self) -> str:
        return (
            f"TaskStep(id={self.id!r}, order={self.order}, "
            f"tool={self.tool_name!r}, status={self.status.value})"
        )


class WorkflowState(Enum):
    """Outcome of one automation run over a full step list."""

    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    # The plan contained no executable steps. Nothing ran, so
    # the workflow did not succeed: the task must not be
    # terminalized as COMPLETED by an empty loop.
    NO_STEPS = "NO_STEPS"
    # Cooperative cancellation: a CANCELLED task is detected at
    # step boundaries. A step already executing cannot be
    # interrupted mid-flight — documented limitation.
    CANCELLED = "CANCELLED"


@dataclass
class AutomationResult:
    task_id: str
    state: WorkflowState
    steps: list = field(default_factory=list)
    reason: str = ""
    # Set only when a paused workflow is terminalized by an
    # explicit human denial in TaskRunner. This is an audit
    # artifact, not a new workflow state.
    approval_denied: bool = False


class AutomationEngine:
    """
    Executes a Task's steps sequentially through the
    shared Agent executor.
    """

    def __init__(self, agent: Agent):
        self._agent = agent

    def run(
        self,
        task: Task,
        steps: list,
    ) -> AutomationResult:
        """
        Run all steps in order. The task's status/result/
        error fields reflect the final workflow outcome.
        """

        ordered = sorted(steps, key=lambda step: step.order)

        if not ordered:
            # An empty plan executed nothing. Leave the task in
            # its pre-run (PENDING) state — a zero-iteration
            # loop is not a successful run.
            return AutomationResult(
                task_id=task.id,
                state=WorkflowState.NO_STEPS,
                steps=[],
                reason="plan contained no executable steps",
            )

        task.status = TaskStatus.RUNNING

        for step in ordered:

            # Cooperative cancellation: a cancelled task never
            # runs another step. Cancelled is terminal. The
            # cancel_requested flag survives the per-step status
            # restoration the loop performs.
            if (
                task.status is TaskStatus.CANCELLED
                or task.cancel_requested
            ):
                # The task record must agree with the reported
                # workflow state: a cancelled task is terminal,
                # never left RUNNING (which would make it
                # unretryable and lie about its lifecycle).
                task.status = TaskStatus.CANCELLED
                task.error = "task was cancelled"
                return AutomationResult(
                    task_id=task.id,
                    state=WorkflowState.CANCELLED,
                    steps=ordered,
                    reason="task was cancelled",
                )

            # Agent.execute() correctly marks an individual
            # successful tool call COMPLETED. A workflow may
            # still have later steps, though, so restore the
            # workflow-level task status before each next step.
            # In particular, a following SENSITIVE step must
            # pause a resumable RUNNING task, not a COMPLETED one.
            task.status = TaskStatus.RUNNING

            blocked = self._dependency_gate(step, ordered)

            if blocked is not None:
                # Fail-closed: never execute a step whose
                # prerequisites are not complete.
                step.status = TaskStatus.FAILED
                step.error = blocked
                task.status = TaskStatus.FAILED
                task.error = (
                    f"Step {step.order} ({step.title}) blocked: "
                    f"{blocked}."
                )
                return AutomationResult(
                    task_id=task.id,
                    state=WorkflowState.FAILED,
                    steps=ordered,
                    reason=task.error,
                )

            # M5.6: resolve validated output references in the
            # params (after the dependency gate — a reference
            # without a declared dependency fails here).
            reference_error = self._resolve_param_references(
                step,
                ordered,
            )

            if reference_error is not None:
                task.status = TaskStatus.FAILED
                task.error = reference_error
                return AutomationResult(
                    task_id=task.id,
                    state=WorkflowState.FAILED,
                    steps=ordered,
                    reason=reference_error,
                )

            result = self._agent.execute(
                task,
                step.tool_name,
                step.params,
            )

            self._sync_step(step, result)

            if result.decision == PermissionDecision.REQUIRE_APPROVAL:
                # Waiting on the user — not a failure. The
                # step stays PENDING so it can be resumed.
                return AutomationResult(
                    task_id=task.id,
                    state=WorkflowState.PAUSED,
                    steps=ordered,
                    reason=result.reason,
                )

            if result.status == TaskStatus.FAILED:
                task.status = TaskStatus.FAILED
                task.error = result.error or result.reason
                return AutomationResult(
                    task_id=task.id,
                    state=WorkflowState.FAILED,
                    steps=ordered,
                    reason=task.error,
                )

        # Every step completed.
        task.status = TaskStatus.COMPLETED
        task.result = self._result_mapping(ordered)
        task.error = None
        return AutomationResult(
            task_id=task.id,
            state=WorkflowState.COMPLETED,
            steps=ordered,
        )

    async def run_async(
        self,
        task: Task,
        steps: list,
    ) -> AutomationResult:
        """
        Async twin of run() (M4 step 3), for workflows whose
        steps need the model gateway.

        Sequential order, permission-gated execution, pause on
        REQUIRE_APPROVAL, fail-closed on denial/failure, and the
        final COMPLETED bookkeeping are all identical to run().
        The synchronous run() is left untouched because it is
        proven code; this method exists so a FastAPI request can
        await one workflow without asyncio.run(), a nested loop,
        or a thread bridge.

        Completed steps are never re-executed by this method:
        callers resume with the remaining step list, exactly as
        TaskRunner already does.
        """

        ordered = sorted(steps, key=lambda step: step.order)

        if not ordered:
            # Same semantics as run(): an empty plan executed
            # nothing and must not read as success.
            return AutomationResult(
                task_id=task.id,
                state=WorkflowState.NO_STEPS,
                steps=[],
                reason="plan contained no executable steps",
            )

        task.status = TaskStatus.RUNNING

        for step in ordered:

            # Cooperative cancellation (same as run()).
            if (
                task.status is TaskStatus.CANCELLED
                or task.cancel_requested
            ):
                # The task record must agree with the reported
                # workflow state: a cancelled task is terminal,
                # never left RUNNING (which would make it
                # unretryable and lie about its lifecycle).
                task.status = TaskStatus.CANCELLED
                task.error = "task was cancelled"
                return AutomationResult(
                    task_id=task.id,
                    state=WorkflowState.CANCELLED,
                    steps=ordered,
                    reason="task was cancelled",
                )

            # Same workflow-level status restoration the sync
            # path performs: a later SENSITIVE step must pause a
            # RUNNING task, not a COMPLETED one.
            task.status = TaskStatus.RUNNING

            blocked = self._dependency_gate(step, ordered)

            if blocked is not None:
                # Fail-closed: never execute a step whose
                # prerequisites are not complete.
                step.status = TaskStatus.FAILED
                step.error = blocked
                task.status = TaskStatus.FAILED
                task.error = (
                    f"Step {step.order} ({step.title}) blocked: "
                    f"{blocked}."
                )
                return AutomationResult(
                    task_id=task.id,
                    state=WorkflowState.FAILED,
                    steps=ordered,
                    reason=task.error,
                )

            # M5.6: resolve validated output references in the
            # params (after the dependency gate — a reference
            # without a declared dependency fails here).
            reference_error = self._resolve_param_references(
                step,
                ordered,
            )

            if reference_error is not None:
                task.status = TaskStatus.FAILED
                task.error = reference_error
                return AutomationResult(
                    task_id=task.id,
                    state=WorkflowState.FAILED,
                    steps=ordered,
                    reason=reference_error,
                )

            result = await self._agent.execute_async(
                task,
                step.tool_name,
                step.params,
            )

            self._sync_step(step, result)

            if result.decision == PermissionDecision.REQUIRE_APPROVAL:
                return AutomationResult(
                    task_id=task.id,
                    state=WorkflowState.PAUSED,
                    steps=ordered,
                    reason=result.reason,
                )

            if result.status == TaskStatus.FAILED:
                task.status = TaskStatus.FAILED
                task.error = result.error or result.reason
                return AutomationResult(
                    task_id=task.id,
                    state=WorkflowState.FAILED,
                    steps=ordered,
                    reason=task.error,
                )

        task.status = TaskStatus.COMPLETED
        task.result = self._result_mapping(ordered)
        task.error = None
        return AutomationResult(
            task_id=task.id,
            state=WorkflowState.COMPLETED,
            steps=ordered,
        )

    @staticmethod
    def _result_mapping(ordered: list) -> dict:
        """
        Final task result keyed by tool name. Two steps using
        the SAME tool must not overwrite each other: the first
        occurrence keeps the plain name (backwards compatible),
        later occurrences get "<tool>#<n>" keys.
        """

        counts: dict = {}
        mapping: dict = {}

        for step in ordered:
            n = counts.get(step.tool_name, 0)
            counts[step.tool_name] = n + 1
            key = (
                step.tool_name
                if n == 0
                else f"{step.tool_name}#{n}"
            )
            mapping[key] = step.result

        return mapping

    @staticmethod
    def _sync_step(
        step: TaskStep,
        result: ExecutionResult,
    ) -> None:
        """Copy the agent outcome onto the step record."""

        step.decision = result.decision

        if result.decision == PermissionDecision.REQUIRE_APPROVAL:
            step.status = TaskStatus.PENDING
            return

        step.status = result.status

        if result.status == TaskStatus.COMPLETED:
            step.result = result.output
        elif result.status == TaskStatus.FAILED:
            step.error = result.error or result.reason

    @staticmethod
    def _dependency_gate(
        step: TaskStep,
        ordered: list,
    ) -> Optional[str]:
        """
        Fail-closed dependency check, shared by run() and
        run_async(). Returns a failure reason when the step
        must NOT run, or None when it may.

        A dependency is satisfied if it names a known step that
        is COMPLETED, or — on a resumed pass, where the runner
        hands the engine only the remaining steps — if it names
        an order BEFORE the first step of this pass (the resume
        invariant: every step before the pending one already
        completed, otherwise the run would have stopped there).

        An unknown dependency at or after the first step of the
        pass means the plan itself is broken: fail closed.
        """

        if not step.dependencies:
            return None

        by_order = {s.order: s for s in ordered}

        if ordered:
            first_order = min(s.order for s in ordered)
        else:
            first_order = 0

        for dep in step.dependencies:
            prerequisite = by_order.get(dep)

            if prerequisite is None:
                if dep < first_order:
                    # Completed in an earlier pass before the
                    # resume hand-off.
                    continue

                return (
                    f"prerequisite step {dep} does not exist "
                    "in the plan"
                )

            if prerequisite.status is not TaskStatus.COMPLETED:
                return (
                    f"prerequisite step {dep} "
                    f"({prerequisite.title}) is "
                    f"{prerequisite.status.value}, not COMPLETED"
                )

        return None

    # --------------------------------------------------------
    # Step output references (M5.6)
    # --------------------------------------------------------

    @staticmethod
    def _resolve_param_references(
        step: TaskStep,
        ordered: list,
    ) -> Optional[str]:
        """
        Resolve {"from_step": N, "field": "a.b"} reference
        objects inside step.params into the referenced step's
        output, BEFORE execution.

        Strict validation, fail-closed:
        - a reference may only cite a step listed in THIS
          step's dependencies (no dependency, no data —
          dependencies are the authorization for data flow)
        - the referenced step must be COMPLETED
        - the dot-path 'field' must exist in its result
        - stringified values are size-capped
        """

        by_order = {s.order: s for s in ordered}

        def resolve_value(value):
            if isinstance(value, dict):
                if set(value.keys()) == {"from_step", "field"}:
                    dep = value["from_step"]
                    field_path = value["field"]

                    if not isinstance(
                        dep, int
                    ) or isinstance(dep, bool):
                        raise ValueError(
                            "from_step must be an integer step "
                            "order."
                        )

                    if dep not in step.dependencies:
                        raise ValueError(
                            f"step {step.order} references "
                            f"output of step {dep} without "
                            "declaring that dependency."
                        )

                    source = by_order.get(dep)

                    if source is None:
                        raise ValueError(
                            f"referenced step {dep} does not "
                            "exist in the plan."
                        )

                    if source.status is not TaskStatus.COMPLETED:
                        raise ValueError(
                            f"referenced step {dep} is "
                            f"{source.status.value}, not "
                            "COMPLETED."
                        )

                    current = source.result

                    for part in str(field_path).split("."):
                        part = part.strip()

                        if isinstance(current, dict) and (
                            part in current
                        ):
                            current = current[part]
                        elif (
                            isinstance(current, list)
                            and part.isdigit()
                            and int(part) < len(current)
                        ):
                            current = current[int(part)]
                        else:
                            raise ValueError(
                                f"field '{field_path}' not found "
                                f"in output of step {dep}."
                            )

                    if current is None:
                        raise ValueError(
                            f"field '{field_path}' in output of "
                            f"step {dep} is empty."
                        )

                    if isinstance(current, (dict, list)):
                        import json

                        text = json.dumps(current, default=str)
                    else:
                        text = str(current)

                    if len(text) > MAX_REF_CHARS:
                        text = (
                            text[:MAX_REF_CHARS]
                            + "...[truncated]"
                        )

                    return text

                return {
                    key: resolve_value(item)
                    for key, item in value.items()
                }

            if isinstance(value, list):
                return [resolve_value(item) for item in value]

            return value

        try:
            step.params = resolve_value(step.params)
        except ValueError as error:
            step.status = TaskStatus.FAILED
            step.error = str(error)
            return (
                f"Step {step.order} ({step.title}) has an "
                f"invalid output reference: {error}"
            )

        return None
