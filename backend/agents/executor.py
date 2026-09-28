"""
GHOST — foundational agent execution (M3-D).

Executes a Task's tool call through the existing GHOST
security stack, in this exact order:

  Task -> Agent -> PermissionPolicy -> ToolRegistry -> tool -> result

Rules enforced here:
- Permission is checked BEFORE any tool execution.
- ALLOW            -> tool runs, Task COMPLETED (or FAILED).
- REQUIRE_APPROVAL -> nothing executes; a clear
  approval-required result is returned and the Task
  stays PENDING until a later M3 step handles approval.
- DENY (DANGEROUS / unknown tool) -> nothing executes;
  Task FAILED with the reason in error.

Tool execution itself is injected as a callable
(tool_name, params) -> Any, so real integrations can be
added in later milestones while tests use a deterministic
mock. This is not a second orchestrator: model-level
orchestration stays in core.orchestrator (this module can
be attached to its existing `agents` registry).
"""

from dataclasses import dataclass
import inspect
from datetime import datetime
from typing import Any, Callable, Optional

from backend.core.task import Task, TaskStatus
from backend.permissions.policy import (
    PermissionDecision,
    PermissionPolicy,
)
from backend.tools.registry import ToolRegistry


@dataclass
class ExecutionResult:
    """
    What happened when the agent handled one tool call.
    Exactly one of output / error carries information,
    depending on status.
    """

    task_id: str
    status: TaskStatus
    decision: Optional[PermissionDecision]
    reason: str
    output: Any = None
    error: Optional[str] = None


class Agent:
    """
    Foundational task executor.

    Executes one tool call on behalf of a Task, gated by
    the shared PermissionPolicy and ToolRegistry.
    """

    def __init__(
        self,
        name: str,
        registry: ToolRegistry,
        permission_policy: PermissionPolicy,
        tool_executor: Callable[[str, dict], Any],
    ):
        self.name = name
        self._registry = registry
        self._permissions = permission_policy
        self._execute_tool = tool_executor

    def execute(
        self,
        task: Task,
        tool_name: str,
        params: Optional[dict] = None,
    ) -> ExecutionResult:
        """
        Run one tool call for the task.

        The task's status/result/error fields are kept in
        sync with the outcome; updated_at is refreshed on
        every change.
        """

        # 1. Permission decision FIRST — no tool runs before this.
        # task_id lets the policy honor a one-shot, task-scoped
        # approval grant (M3-H step 5); without a grant the
        # decision is unchanged.
        permission = self._permissions.evaluate(
            tool_name,
            task_id=task.id,
        )

        if permission.decision == PermissionDecision.REQUIRE_APPROVAL:
            # Not executed, not failed: waiting for the user.
            return ExecutionResult(
                task_id=task.id,
                status=task.status,
                decision=permission.decision,
                reason=permission.reason,
            )

        if permission.decision == PermissionDecision.DENY:
            task.status = TaskStatus.FAILED
            task.error = permission.reason
            task.updated_at = _now()
            return ExecutionResult(
                task_id=task.id,
                status=TaskStatus.FAILED,
                decision=permission.decision,
                reason=permission.reason,
                error=task.error,
            )

        # 2. Allowed: mark running and execute.
        task.status = TaskStatus.RUNNING
        task.updated_at = _now()

        try:
            output = self._execute_tool(
                tool_name,
                params if params is not None else {},
            )

            # An awaitable can only come from an async tool
            # (e.g. the model gateway). Storing an unawaited
            # coroutine as a result would corrupt the task
            # silently, so fail loudly instead. No synchronous
            # tool can reach this branch.
            if inspect.isawaitable(output):
                if hasattr(output, "close"):
                    output.close()

                raise TypeError(
                    f"Tool '{tool_name}' is asynchronous and "
                    "must be executed with execute_async()."
                )

        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.error = f"{type(exc).__name__}: {exc}"
            task.updated_at = _now()
            return ExecutionResult(
                task_id=task.id,
                status=TaskStatus.FAILED,
                decision=permission.decision,
                reason=permission.reason,
                error=task.error,
            )

        # 3. Success.
        task.status = TaskStatus.COMPLETED
        task.result = output
        task.error = None
        task.updated_at = _now()
        return ExecutionResult(
            task_id=task.id,
            status=TaskStatus.COMPLETED,
            decision=permission.decision,
            reason=permission.reason,
            output=output,
        )

    async def execute_async(
        self,
        task: Task,
        tool_name: str,
        params: Optional[dict] = None,
    ) -> ExecutionResult:
        """
        Async twin of execute() for tools that must await the
        model gateway (M4 step 3).

        Semantics are identical and deliberately re-checked
        here rather than shared with the sync path, because the
        synchronous execute() is proven code that this milestone
        must not alter:

        - PermissionPolicy is still consulted BEFORE any tool
          runs, with the same task-scoped one-shot grant.
        - ALLOW -> runs; REQUIRE_APPROVAL -> nothing runs;
          DENY (including unknown tools) -> nothing runs.
        - Task status/result/error bookkeeping is unchanged.

        The only addition: when the injected tool callable
        returns an awaitable, it is awaited inside the caller's
        existing event loop. No asyncio.run(), no nested loop,
        no thread bridge.
        """

        # 1. Permission decision FIRST — no tool runs before this.
        permission = self._permissions.evaluate(
            tool_name,
            task_id=task.id,
        )

        if permission.decision == PermissionDecision.REQUIRE_APPROVAL:
            return ExecutionResult(
                task_id=task.id,
                status=task.status,
                decision=permission.decision,
                reason=permission.reason,
            )

        if permission.decision == PermissionDecision.DENY:
            task.status = TaskStatus.FAILED
            task.error = permission.reason
            task.updated_at = _now()
            return ExecutionResult(
                task_id=task.id,
                status=TaskStatus.FAILED,
                decision=permission.decision,
                reason=permission.reason,
                error=task.error,
            )

        # 2. Allowed: mark running and execute.
        task.status = TaskStatus.RUNNING
        task.updated_at = _now()

        try:
            output = self._execute_tool(
                tool_name,
                params if params is not None else {},
            )

            # Awaitable tool implementations (e.g. the model
            # gateway) are awaited in place; plain synchronous
            # tools behave exactly as they do under execute().
            if inspect.isawaitable(output):
                output = await output

        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.error = f"{type(exc).__name__}: {exc}"
            task.updated_at = _now()
            return ExecutionResult(
                task_id=task.id,
                status=TaskStatus.FAILED,
                decision=permission.decision,
                reason=permission.reason,
                error=task.error,
            )

        # 3. Success.
        task.status = TaskStatus.COMPLETED
        task.result = output
        task.error = None
        task.updated_at = _now()
        return ExecutionResult(
            task_id=task.id,
            status=TaskStatus.COMPLETED,
            decision=permission.decision,
            reason=permission.reason,
            output=output,
        )


def _now():
    return datetime.now()
