"""
GHOST — agent pipeline (M4 step 7).

The composition root for the ENMA agent chain:

  request
    -> Planner                 (backend/core/planner.py)
    -> ExecutionSpec           (backend/core/execution_spec.py)
    -> SkillStage              (backend/skills/stage.py)
    -> TaskRunner -> AutomationEngine -> Agent
    -> PermissionPolicy -> ToolRegistry -> tool / model gateway
    -> ReflectionEngine
    -> MemoryBridge            (backend/agents/memory_bridge.py)
    -> AuditLog                (backend/audit/log.py)

Every stage is an existing component, injected here; this
module implements none of them. Its own responsibilities are
only: run the stages in order, translate a spec into TaskSteps,
and record what happened.

Rules:

- AWAIT, NEVER BLOCK. handle_request(), resume() and
  finalize_denied_approval() are coroutines to be awaited
  by the request handler that already owns the event loop. There
  is no asyncio.run(), no nested loop and no thread bridge here,
  so a task that calls the model gateway cannot block the
  server's loop.
- PERMISSION STAYS AUTHORITATIVE. Nothing in this module decides
  whether a tool may run: the spec's risk summary is a recorded
  statement, and the PermissionPolicy inside the Agent is the
  only gate. A step the policy denies fails here exactly as it
  does without the pipeline.
- AUDIT IS A RECORD, NOT A GATE. Audit failures cannot change
  execution (AuditLog.append is fail-safe by design).
- CLARIFICATION CREATES NO TASK. An unready plan is reported and
  audited; the task store and the executor are untouched.
- RESPONSES BELONG TO THE API. The pipeline returns typed
  results and passes the runner's envelope through unchanged, so
  the HTTP layer keeps ownership of its response shape.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend.agents.memory_bridge import MemoryBridge, MemoryWrite
from backend.automation.engine import TaskStep
from backend.audit.log import AuditLog, AuditStage
from backend.core.execution_spec import (
    ExecutionSpec,
    build_execution_spec,
)
from backend.core.planner import Planner, PlanningResult
from backend.core.task import Task, TaskStatus
from backend.skills.skill import SkillResult
from backend.skills.stage import SkillSelection, SkillStage
from backend.tools.registry import ToolRegistry


# Long strings an audit row must carry without any risk of
# holding a whole document or a whole transcript.
MAX_AUDIT_TEXT_CHARS = 500

# TaskStep titles come from planner descriptions, which are
# model output: bounded before they reach a task record.
MAX_STEP_TITLE_CHARS = 300


@dataclass
class PipelineOutcome:
    """Everything one request produced, for the API to shape."""

    planning: Optional[PlanningResult] = None
    task: Optional[Task] = None
    spec: Optional[ExecutionSpec] = None
    selection: Optional[SkillSelection] = None
    execution: Optional[dict] = None
    skill_result: Optional[SkillResult] = None
    memory: Optional[MemoryWrite] = None

    @property
    def task_id(self) -> Optional[str]:
        return self.task.id if self.task is not None else None

    @property
    def clarification_required(self) -> bool:
        return self.planning is not None and not self.planning.ready


class AgentPipeline:
    """Ordered execution of the stages named in this docstring."""

    def __init__(
        self,
        planner: Planner,
        task_service,
        task_runner,
        tool_registry: ToolRegistry,
        skill_stage: Optional[SkillStage] = None,
        reflection_engine=None,
        memory_bridge: Optional[MemoryBridge] = None,
        audit: Optional[AuditLog] = None,
    ):
        self._planner = planner
        self._tasks = task_service
        self._runner = task_runner
        self._registry = tool_registry
        self._skills = skill_stage
        self._reflection = reflection_engine
        self._memory = memory_bridge
        self._audit_log = audit

    # ========================================================
    # REQUEST -> PLAN -> SPEC -> EXECUTION
    # ========================================================

    async def handle_request(
        self,
        request: str,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        memory_context: Optional[str] = None,
        document_context: Optional[str] = None,
        document_id: Optional[str] = None,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> PipelineOutcome:
        """
        Plan one user request and, when the plan is ready,
        create and run its task.
        """

        request_text = str(request or "").strip()

        outcome = PipelineOutcome()

        task_id: Optional[str] = None

        try:
            self._audit(
                None,
                AuditStage.REQUEST,
                event="received",
                data={
                    "request": _clip(request_text),
                    "memory_context": bool(memory_context),
                    "document_context": bool(document_context),
                    "document_id": document_id,
                    "history_turns": len(conversation_history or []),
                },
            )

            planning = await self._planner.plan(
                request_text,
                memory_context=memory_context,
                document_context=document_context,
                conversation_history=conversation_history,
            )

            outcome.planning = planning

            if not planning.ready:
                # Nothing is created and nothing runs: the
                # planner's questions are the whole answer.
                self._audit(
                    None,
                    AuditStage.PLANNED,
                    event=_planning_source(planning),
                    status="clarification_required",
                    data=planning.to_dict(),
                )
                return outcome

            task = self._tasks.create(
                title or planning.task_title or request_text,
                description or planning.task_description or request_text,
                owner=owner,
            )

            outcome.task = task
            task_id = task.id

            self._audit(
                task.id,
                AuditStage.PLANNED,
                event=_planning_source(planning),
                status="ready",
                data=planning.to_dict(),
            )

            outcome.selection = self._select_skill(task.id, request_text)

            outcome.spec = build_execution_spec(
                planning,
                task_id=task.id,
                registry=self._registry,
                skill_name=(
                    outcome.selection.skill
                    if outcome.selection
                    else None
                ),
                skill_risk_level=(
                    outcome.selection.risk_level
                    if outcome.selection
                    else None
                ),
                provider=provider,
                model=model,
                memory_context=memory_context,
                document_context=document_context,
                document_id=document_id,
            )

            self._audit(
                task.id,
                AuditStage.SPEC,
                event=f"{outcome.spec.step_count} planned step(s)",
                status=outcome.spec.risk_level,
                data=outcome.spec.to_dict(),
            )

            await self._execute(outcome)

            outcome.memory = self._finalize(task)

            return outcome

        except Exception as error:
            self._audit(
                task_id,
                AuditStage.ERROR,
                event="request_failed",
                data={"detail": _error_text(error)},
            )
            raise

    # ========================================================
    # APPROVED RESUME
    # ========================================================

    async def resume(self, task_id: str) -> dict:
        """
        Continue an approved workflow through the same runner,
        then record and memorize the new outcome.

        The runner's refusal types (KeyError, ValueError,
        ApprovalDeniedError, ApprovalRequiredError) propagate
        unchanged so the HTTP layer keeps mapping them; the
        refusal is still audited.
        """

        try:
            execution = await self._runner.resume_async(task_id)
        except Exception as error:
            self._audit(
                task_id,
                AuditStage.ERROR,
                event="resume_refused",
                data={"detail": _error_text(error)},
            )
            raise

        self._audit_steps(task_id, self._runner.planned_steps(task_id))

        self._audit_pause(task_id, execution)

        self._finalize(self._tasks.get(task_id))

        return execution

    async def finalize_denied_approval(self, approval_id: str) -> dict:
        """
        Record a human denial: runner state, then audit and
        memory for the terminalized task.

        The denial itself is synchronous bookkeeping (it executes
        no tool and never changes a permission decision); the
        coroutine signature keeps the request handler await-only.
        """

        result = self._runner.finalize_denied_approval(approval_id)

        task_id = result["task_id"]

        self._audit(
            task_id,
            AuditStage.APPROVAL,
            event="denied",
            status=str(result["task_status"].value),
            data={"approval_id": approval_id},
        )

        self._audit_steps(task_id, self._runner.planned_steps(task_id))

        self._finalize(self._tasks.get(task_id))

        return result

    # ========================================================
    # RETRY RE-EXECUTION (M3)
    # ========================================================

    async def retry_execution(self, task_id: str) -> Optional[dict]:
        """
        Re-execute a retried task's recorded steps through the
        same path as the first attempt: TaskRunner ->
        AutomationEngine -> Agent -> PermissionPolicy ->
        ToolRegistry.

        The planner is never called and no new plan is
        generated — the task's existing stored steps are
        reused exactly as planned. A task with no recorded
        executable steps (advisory plan) returns None and
        keeps the plain re-queued PENDING semantics.

        The runner's start gate still requires a PENDING task
        and refuses through ValueError, so a cancellation that
        races the retry refuses safely; state-machine
        validation and the retry limit live in TaskService and
        are untouched.
        """

        steps = self._runner.planned_steps(task_id)

        if not steps:
            return None

        try:
            execution = await self._runner.start_async(task_id, steps)
        except Exception as error:
            self._audit(
                task_id,
                AuditStage.ERROR,
                event="retry_execution_refused",
                data={"detail": _error_text(error)},
            )
            raise

        self._audit_steps(task_id, self._runner.planned_steps(task_id))

        self._audit_pause(task_id, execution)

        self._finalize(self._tasks.get(task_id))

        return execution

    # ========================================================
    # APPROVAL DECISION (audit record only)
    # ========================================================

    def record_approval_decision(self, record) -> None:
        """
        Audit one approval decision.

        ApprovalService already stored the decision and
        TaskRunner terminalizes the workflow on a denial; this
        only puts the event on the audit trail.
        """

        self._audit(
            record.task_id,
            AuditStage.APPROVAL,
            event=record.status.value.lower(),
            status=record.status.value,
            data={
                "approval_id": record.approval_id,
                "tool": record.tool_name,
                "decided_at": record.decided_at,
            },
        )

    # ========================================================
    # STAGES
    # ========================================================

    def _select_skill(
        self,
        task_id: str,
        request_text: str,
    ) -> Optional[SkillSelection]:
        """Route once, and record the outcome either way."""

        if self._skills is None:
            return None

        selection = self._skills.select(request_text)

        self._audit(
            task_id,
            AuditStage.SKILL,
            event="selected" if selection.matched else "no_match",
            data=selection.to_dict(),
        )

        return selection

    async def _execute(self, outcome: PipelineOutcome) -> None:
        """
        Run the spec: concrete tool steps first, else the
        selected skill, else nothing at all.
        """

        spec = outcome.spec
        task = outcome.task

        if spec.has_executable_steps:
            steps = task_steps_from_spec(spec)

            try:
                outcome.execution = await self._runner.start_async(
                    task.id,
                    steps,
                )
            except ValueError as error:
                # The runner's start gate refused the workflow
                # (e.g. the task is no longer PENDING). Reported,
                # never retried and never run around the gate.
                self._audit(
                    task.id,
                    AuditStage.ERROR,
                    event="start_refused",
                    data={"detail": _error_text(error)},
                )
                return

            self._audit_steps(task.id, steps)
            self._audit_pause(task.id, outcome.execution)
            return

        if outcome.selection is not None and outcome.selection.matched:
            outcome.skill_result = self._run_skill(
                task,
                outcome.selection,
            )
            self._execution_from_skill(outcome)
            return

        # Advisory plan: a task exists, nothing is executable.
        # Report the honest state through the same envelope shape
        # the runner uses, so the API (and TaskCard) shows a
        # not-executed task instead of a missing execution.
        task_status = task.status.value

        self._audit(
            task.id,
            AuditStage.RESULT,
            event="nothing_executable",
            status=task_status,
            data={
                "advisory_steps": (
                len(outcome.planning.steps)
                if outcome.planning is not None
                else 0
            ),
                "note": (
                    "the plan proposed no registered tool and no "
                    "runnable skill matched"
                ),
            },
        )

        outcome.execution = {
            "task_id": task.id,
            "state": "NO_STEPS",
            "task_status": task_status,
            "approval_id": None,
            "reflection": None,
            "steps": [],
            "reason": (
                "no executable steps were planned; the task "
                "remains pending"
            ),
        }

    def _execution_from_skill(
        self,
        outcome: PipelineOutcome,
    ) -> None:
        """
        Populate the runner-shaped execution envelope for the
        skill path, so a skill run is never reported as
        execution:null.

        Honesty rule: when planning fell back because the model
        gateway failed (FALLBACK_*), a skill whose "completion"
        is only an advisory plan must not read as a completed
        user request. The task stays PENDING and the envelope
        says NO_STEPS with the reason.
        """

        result = outcome.skill_result

        if result is None:
            return

        fallback = (
            outcome.planning is not None
            and getattr(
                outcome.planning.source,
                "value",
                str(outcome.planning.source),
            )
            in ("FALLBACK_ERROR", "FALLBACK_MALFORMED")
            and result.status == TaskStatus.COMPLETED
        )

        if fallback:
            task = outcome.task

            if task.status == TaskStatus.COMPLETED:
                task.status = TaskStatus.PENDING

        state = (
            "COMPLETED"
            if (
                result.status == TaskStatus.COMPLETED
                and not fallback
            )
            else "NO_STEPS"
            if result.status == TaskStatus.COMPLETED
            else result.status.value
        )

        outcome.execution = {
            "task_id": outcome.task.id,
            "state": state,
            "task_status": outcome.task.status.value,
            "approval_id": None,
            "reflection": None,
            "steps": [],
            "skill": result.skill_name,
            "skill_status": result.status.value,
            "reason": (
                "planner fell back to deterministic defaults; "
                f"skill '{result.skill_name}' produced an "
                "advisory plan only, so the task remains pending"
                if fallback
                else f"skill '{result.skill_name}' executed"
            ),
        }

    def _run_skill(
        self,
        task: Task,
        selection: SkillSelection,
    ) -> SkillResult:
        """
        Run the matched skill through SkillRunner, which executes
        its own steps via the same Agent -> PermissionPolicy path.

        A skill raising is converted into a failed result: an
        exception here would otherwise surface as a server error
        after the task had already been created.
        """

        name = selection.skill or "unknown-skill"

        try:
            result = self._skills.execute(selection, task)
        except Exception as error:
            result = SkillResult(
                skill_name=name,
                status=TaskStatus.FAILED,
                error=_error_text(error),
            )

        self._apply_skill_result(task, result)

        if self._reflection is not None:
            reflection = self._reflection.reflect_on_task(
                task,
                skill_result=result,
            )
            task.reflection = reflection

        self._audit(
            task.id,
            AuditStage.SKILL,
            event="executed",
            status=result.status.value,
            data={
                "skill": result.skill_name,
                "output": result.output,
                "error": result.error,
                "steps": result.steps,
            },
        )

        return result

    @staticmethod
    def _apply_skill_result(task: Task, result: SkillResult) -> None:
        """
        Reflect a skill outcome on its task when the skill itself
        did not already do it through the engine.
        """

        if result.status == TaskStatus.COMPLETED:
            if task.status != TaskStatus.COMPLETED:
                task.status = TaskStatus.COMPLETED
                task.result = result.output
            task.error = None
            return

        if result.status == TaskStatus.FAILED:
            task.status = TaskStatus.FAILED
            task.error = result.error or f"{result.skill_name} failed"

        # PENDING means "waiting for an approval decision": the
        # task stays exactly as the engine left it.

    def _finalize(self, task: Task) -> Optional[MemoryWrite]:
        """Record the outcome, reflect it, then memorize it."""

        self._audit(
            task.id,
            AuditStage.RESULT,
            event="finished",
            status=task.status.value,
            data={
                "result": task.result,
                "error": task.error,
            },
        )

        reflection = task.reflection

        if reflection is not None:
            self._audit(
                task.id,
                AuditStage.REFLECTION,
                event=reflection.outcome.value,
                status=task.status.value,
                data=reflection.to_dict(),
            )

        if self._memory is None:
            return None

        return self._memory.record_outcome(task, reflection)

    # ========================================================
    # AUDIT HELPERS
    # ========================================================

    def _audit_steps(self, task_id: str, steps: List[TaskStep]) -> None:
        """One TOOL row per step, in plan order."""

        for step in steps:
            self._audit(
                task_id,
                AuditStage.TOOL,
                event=step.tool_name,
                status=step.status.value,
                data={
                    "step_id": step.id,
                    "order": step.order,
                    "title": _clip(step.title, MAX_STEP_TITLE_CHARS),
                    "params": step.params,
                    "result": step.result,
                    "error": step.error,
                },
            )

    def _audit_pause(
        self,
        task_id: str,
        execution: Optional[dict],
    ) -> None:
        """
        Record that a run is now waiting on a human decision.

        ApprovalService already holds the record; this only puts
        the event on the trail, identically for a first run and a
        resume that paused again.
        """

        if not execution or not execution.get("approval_id"):
            return

        self._audit(
            task_id,
            AuditStage.APPROVAL,
            event="requested",
            status=execution["state"].value,
            data={"approval_id": execution["approval_id"]},
        )

    def _audit(
        self,
        task_id: Optional[str],
        stage: AuditStage,
        event: Optional[str] = None,
        status: Optional[str] = None,
        data: Optional[dict] = None,
    ) -> Optional[dict]:
        if self._audit_log is None:
            return None

        return self._audit_log.append(
            task_id=task_id,
            stage=stage,
            event=event,
            status=status,
            data=data,
        )


# ============================================================
# SPEC -> TASK STEPS
# ============================================================

def task_steps_from_spec(spec: ExecutionSpec) -> List[TaskStep]:
    """
    Materialize one spec's executable steps as TaskSteps.

    This is the only place a plan becomes concrete work, so the
    HTTP layer no longer translates plans by hand. Order and
    params are taken from the spec verbatim; permission is still
    decided per step by the Agent, not here.
    """

    return [
        TaskStep(
            tool_name=step.tool,
            params=dict(step.params),
            order=step.order,
            title=_clip(step.description, MAX_STEP_TITLE_CHARS),
        )
        for step in spec.steps
    ]


# ============================================================
# SMALL PURE HELPERS
# ============================================================

def _planning_source(planning: PlanningResult) -> str:
    return getattr(planning.source, "value", str(planning.source))


def _error_text(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _clip(text: Any, limit: int = MAX_AUDIT_TEXT_CHARS) -> str:
    value = str(text if text is not None else "")

    if len(value) > limit:
        return value[:limit] + "...[truncated]"

    return value
