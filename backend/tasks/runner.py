"""
GHOST — task runner (M3-H step 5).

Drives one stored Task through its planned steps via the
existing AutomationEngine (Agent -> PermissionPolicy ->
ToolRegistry), and resumes paused workflows after an
explicit approval.

Rules enforced here:
- start() only from a PENDING task; resume() only when
  the task exists, has a recorded workflow with a
  pending step, and a GRANTED (unused) approval exists
  for the exact paused task+tool. Denied or undecided
  approvals refuse resume without touching the engine.
- Resume never bypasses permissions: the engine re-enters
  Agent -> PermissionPolicy, which consumes the one-shot
  grant at execution time. DENIED/DANGEROUS still fail.
- Already COMPLETED/FAILED/CANCELLED tasks cannot be
  resumed; completed steps are never re-executed.
- When a run pauses again (a further SENSITIVE step), a
  new approval record is created for that step.
"""

from datetime import datetime
from typing import Dict, List, Optional

from backend.approval.service import (
    ApprovalDeniedError,
    ApprovalRequiredError,
    ApprovalStatus,
    ApprovalService,
)
from backend.automation.engine import (
    AutomationEngine,
    AutomationResult,
    WorkflowState,
)
from backend.core.task import TaskStatus, MAX_TASK_RETRIES
from backend.permissions.policy import PermissionDecision
from backend.reflection.engine import ReflectionEngine


class TaskRunner:
    def __init__(
        self,
        task_service,
        automation_engine: AutomationEngine,
        approvals: ApprovalService,
        reflection_engine: Optional[ReflectionEngine] = None,
        retryable_tools: Optional[frozenset] = None,
        max_auto_retries: int = MAX_TASK_RETRIES,
    ):
        self._tasks = task_service
        self._engine = automation_engine
        self._approvals = approvals
        self._reflection = reflection_engine
        # task_id -> planned steps, in plan order.
        self._workflows: Dict[str, list] = {}
        # Bounded automatic recovery (M4): a FAILED run whose
        # steps are ALL explicitly listed here (i.e. SAFE tools
        # only, no approval surface) is re-executed up to
        # max_auto_retries times. None disables auto-retry
        # entirely — fail-safe default for any runner that is
        # not explicitly wired for it.
        self._retryable_tools = retryable_tools
        self._max_auto_retries = max_auto_retries

    def start(self, task_id: str, steps: list) -> dict:
        """
        Execute a task's steps. Pauses (with an approval
        record) on the first SENSITIVE step.
        """

        task, steps = self._prepare_start(task_id, steps)

        self._workflows[task_id] = steps

        return self._run(task, steps)

    async def start_async(self, task_id: str, steps: list) -> dict:
        """
        Async twin of start() (M4 step 3) for workflows that
        include model-gateway steps.

        Start validation is shared with the synchronous path by
        construction — both call _prepare_start — so an async
        start can never be more permissive than a sync one.
        """

        task, steps = self._prepare_start(task_id, steps)

        self._workflows[task_id] = steps

        return await self._run_async(task, steps)

    def _prepare_start(self, task_id: str, steps: list):
        """Shared start gate. Raises exactly as before."""

        task = self._tasks.get(task_id)

        if task.status != TaskStatus.PENDING:
            raise ValueError(
                f"Task '{task_id}' is "
                f"{task.status.value} and cannot be started."
            )

        steps = list(steps or [])

        if not steps:
            raise ValueError(
                f"Task '{task_id}' has no steps to execute."
            )

        return task, steps

    def resume(self, task_id: str) -> dict:
        """
        Continue a paused workflow after explicit approval.
        Refuses safely when anything is not exactly right.
        """

        task, remaining = self._prepare_resume(task_id)

        return self._run(task, remaining)

    async def resume_async(self, task_id: str) -> dict:
        """
        Async twin of resume(). The approval gate is the shared
        _prepare_resume(), so a resume can never bypass the
        grant requirement just because it runs asynchronously.
        """

        task, remaining = self._prepare_resume(task_id)

        return await self._run_async(task, remaining)

    def _prepare_resume(self, task_id: str):
        """
        Shared resume gate: task state, pending step, and the
        explicit GRANTED approval requirement. Raises exactly
        the same errors in the same order as before.
        """

        task = self._tasks.get(task_id)

        steps = self._workflows.get(task_id)

        if steps is None:
            raise ValueError(
                f"Task '{task_id}' has no recorded "
                "workflow to resume."
            )

        # A human-denied approval terminalizes the task. Keep
        # the established denial-specific error on later resume
        # attempts instead of treating it as an ordinary failure.
        if (
            task.status == TaskStatus.FAILED
            and self._denied_approval_for_task(task_id) is not None
        ):
            raise ApprovalDeniedError(
                f"Approval for task '{task_id}' was denied."
            )

        if task.status not in (
            TaskStatus.PENDING,
            TaskStatus.RUNNING,
        ):
            raise ValueError(
                f"Task '{task_id}' is {task.status.value} "
                "and cannot be resumed."
            )

        paused = next(
            (
                step
                for step in steps
                if step.status == TaskStatus.PENDING
            ),
            None,
        )

        if paused is None:
            raise ValueError(
                f"Task '{task_id}' has no pending step "
                "to resume."
            )

        if not self._approvals.grants_for(
            task_id, paused.tool_name
        ):
            denied = self._denied_approval_for_step(
                task_id,
                paused,
            )
            if denied is not None:
                self.finalize_denied_approval(
                    denied.approval_id,
                )
                raise ApprovalDeniedError(
                    f"Approval for task '{task_id}' tool "
                    f"'{paused.tool_name}' was denied."
                )
            raise ApprovalRequiredError(
                f"Task '{task_id}' is awaiting an approval "
                f"decision for '{paused.tool_name}'."
            )

        # Completed steps are never re-executed.
        remaining = [
            step
            for step in steps
            if step.status != TaskStatus.COMPLETED
        ]

        return task, remaining

    def finalize_denied_approval(self, approval_id: str) -> dict:
        """Terminalize the exact paused workflow step after a human denial.

        ApprovalService remains the record keeper and is the only component
        that decides whether the record is DENIED. This runner method only
        records the resulting workflow state and reflects that real decision;
        it executes no tool and never changes permission policy.
        """

        record = self._approvals.get(approval_id)

        if record.status != ApprovalStatus.DENIED:
            raise ValueError(
                f"Approval '{approval_id}' is not denied."
            )

        task = self._tasks.get(record.task_id)
        steps = self._workflows.get(record.task_id)

        if steps is None:
            raise ValueError(
                f"Task '{record.task_id}' has no recorded "
                "workflow for this approval."
            )

        paused = next(
            (
                step
                for step in steps
                if step.id == record.step_id
                and step.status == TaskStatus.PENDING
            ),
            None,
        )

        if paused is None:
            raise ValueError(
                f"Approval '{approval_id}' does not match a "
                "pending workflow step."
            )

        reason = (
            f"Approval for task '{task.id}' tool "
            f"'{record.tool_name}' was denied by the user."
        )
        paused.status = TaskStatus.FAILED
        paused.error = reason
        task.status = TaskStatus.FAILED
        task.error = reason
        task.updated_at = datetime.now()

        result = AutomationResult(
            task_id=task.id,
            state=WorkflowState.FAILED,
            steps=steps,
            reason=reason,
            approval_denied=True,
        )
        reflection = self._reflect(task, result)

        return {
            "task_id": task.id,
            "state": result.state,
            "task_status": task.status,
            "approval_id": approval_id,
            "reflection": (
                reflection.to_dict()
                if reflection is not None
                else None
            ),
        }

    # --------------------------------------------------------

    def planned_steps(self, task_id: str) -> list:
        """
        One task's recorded workflow steps, in plan order.

        The returned list is a copy; the TaskStep objects are the
        same ones the engine updates, so a caller can read what
        ran (for reporting or auditing) without depending on
        runner internals. No step is executed or altered here.
        """

        steps = self._workflows.get(task_id) or []

        return sorted(steps, key=lambda step: step.order)

    def _run(self, task, steps: list) -> dict:
        """
        One engine pass. Always through the existing
        Agent -> PermissionPolicy -> ToolRegistry path.
        """

        return self._execute_with_recovery(
            task,
            steps,
            lambda: self._engine.run(task, steps),
        )

    async def _run_async(self, task, steps: list) -> dict:
        """
        One async engine pass through the same shared
        Agent -> PermissionPolicy -> ToolRegistry path.
        """

        return await self._execute_with_recovery_async(
            task,
            steps,
            lambda: self._engine.run_async(task, steps),
        )

    # --------------------------------------------------------
    # Bounded automatic recovery (M4)
    # --------------------------------------------------------

    def _can_auto_retry(
        self,
        task,
        result: AutomationResult,
    ) -> bool:
        """
        Only genuinely safe failures retry automatically:
        - the workflow FAILED (not paused, cancelled, or empty)
        - no step was DENIED by the permission policy (a policy
          decision will not change by re-running it; denials
          finalize through the explicit denial path instead)
        - every step's tool is in the explicitly allowed
          retryable set (SAFE tools only — nothing that writes,
          approves, or can be dangerous)
        - the shared retry cap (MAX_TASK_RETRIES, tracked on
          the task) is not exhausted
        """

        if result.state is not WorkflowState.FAILED:
            return False

        if result.approval_denied:
            return False

        if task.retry_count >= self._max_auto_retries:
            return False

        if not self._retryable_tools:
            return False

        for step in result.steps:
            if getattr(step, "decision", None) is (
                PermissionDecision.DENY
            ):
                return False

            if step.tool_name not in self._retryable_tools:
                return False

        return True

    def _execute_with_recovery(self, task, steps, run_once):
        result = run_once()
        attempts = 0

        while self._can_auto_retry(task, result):
            attempts += 1
            # TaskService.retry validates the FAILED->PENDING
            # transition and enforces the shared retry cap.
            self._tasks.retry(task.id)
            result = run_once()

        return self._outcome(task, result, auto_retries=attempts)

    async def _execute_with_recovery_async(self, task, steps, run_once):
        result = await run_once()
        attempts = 0

        while self._can_auto_retry(task, result):
            attempts += 1
            self._tasks.retry(task.id)
            result = await run_once()

        return self._outcome(task, result, auto_retries=attempts)

    def _outcome(
        self,
        task,
        result: AutomationResult,
        auto_retries: int = 0,
    ) -> dict:
        """
        Post-execution bookkeeping, shared by both passes:
        reflection, approval-record creation on PAUSE, and the
        runner's result envelope. Identical for sync and async,
        so neither path can drift.
        """

        if auto_retries:
            # Record the recovery honestly, on the task and in
            # the envelope: a retried run must never read as a
            # first-attempt success.
            task.metadata["auto_retries"] = auto_retries

        # Reflection is strictly post-execution and read-only. It
        # receives the AutomationResult produced by the real Agent /
        # PermissionPolicy / ToolRegistry execution path; it neither
        # executes tools nor alters workflow or approval decisions.
        reflection = self._reflect(task, result)

        approval_id: Optional[str] = None

        if result.state.value == "PAUSED":
            paused = next(
                (
                    step
                    for step in result.steps
                    if step.status == TaskStatus.PENDING
                ),
                None,
            )
            if paused is not None:
                record = self._approvals.create(
                    task_id=task.id,
                    tool_name=paused.tool_name,
                    step_id=paused.id,
                    reason=result.reason,
                )
                approval_id = record.approval_id

        return {
            "task_id": task.id,
            "state": result.state,
            "task_status": task.status,
            "approval_id": approval_id,
            "auto_retries": auto_retries,
            "reflection": (
                reflection.to_dict()
                if reflection is not None
                else None
            ),
        }

    def _reflect(self, task, result: AutomationResult):
        """Reflect one real workflow artifact when reflection is wired."""

        if self._reflection is None:
            return None

        reflection = self._reflection.reflect_on_task(
            task,
            automation_result=result,
        )
        task.reflection = reflection
        return reflection

    def _denied_approval_for_task(self, task_id: str):
        return next(
            (
                record
                for record in self._approvals.list_for_task(task_id)
                if record.status == ApprovalStatus.DENIED
            ),
            None,
        )

    def _denied_approval_for_step(self, task_id: str, step):
        return next(
            (
                record
                for record in self._approvals.list_for_task(task_id)
                if (
                    record.status == ApprovalStatus.DENIED
                    and record.tool_name == step.tool_name
                    and record.step_id == step.id
                )
            ),
            None,
        )
