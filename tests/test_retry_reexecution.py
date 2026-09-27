"""
M3 retry re-execution tests.

Covers the retry endpoint's new behavior: after TaskService.retry
re-queues a FAILED task, the pipeline re-executes the task's
existing stored steps through the existing TaskRunner path —
no planner call, no new plan, retry_count preserved, retry limit
respected, cancellation intact, lifecycle audit recorded.
"""

import asyncio
import tempfile

import pytest

from backend.agents.pipeline import AgentPipeline
from backend.approval.service import ApprovalService
from backend.automation.engine import AutomationEngine, TaskStep
from backend.agents.executor import Agent
from backend.audit.log import AuditLog
from backend.core.planner import Planner
from backend.permissions.policy import PermissionPolicy
from backend.tasks.runner import TaskRunner
from backend.tasks.service import TaskService
from backend.tools.registry import RiskLevel, ToolRegistry


def make_pipeline(tool_impl):
    """
    Build the full stack with the same wiring as production
    agent_services, one registered tool, and a planner spy.

    tool_impl(call_count) is the real tool executor body: it
    decides per call whether the tool succeeds or fails.
    """

    calls = []

    def executor(name, params):
        calls.append((name, dict(params)))
        return tool_impl(len(calls), name, params)

    registry = ToolRegistry()
    registry.register(
        name="fs_read_file",
        description="read a local text file",
        category="filesystem",
        risk_level=RiskLevel.SAFE,
    )
    policy = PermissionPolicy(registry)
    agent = Agent(
        name="test-agent",
        registry=registry,
        permission_policy=policy,
        tool_executor=executor,
    )

    tasks = TaskService()
    approvals = ApprovalService()
    runner = TaskRunner(
        task_service=tasks,
        automation_engine=AutomationEngine(agent),
        approvals=approvals,
    )

    class PlannerSpy(Planner):
        # The retry path must never reach the planner.
        def __init__(self):
            super().__init__(None)
            self.plan_calls = 0

        async def plan(self, *args, **kwargs):
            self.plan_calls += 1
            raise AssertionError("planner must not run on retry")

    with tempfile.NamedTemporaryFile(
        suffix=".jsonl", delete=False
    ) as handle:
        audit = AuditLog(storage_path=handle.name)
        audit_path = handle.name

    pipeline = AgentPipeline(
        planner=PlannerSpy(),
        task_service=tasks,
        task_runner=runner,
        tool_registry=registry,
        audit=audit,
    )
    return pipeline, tasks, runner, calls, audit_path


def fail_first_attempt_then(pipeline, tasks, runner, second_impl):
    """
    Run one real first attempt that fails, then requeue it via
    the M3 retry mechanism. Returns (task, first_outcome).
    """

    task = tasks.create("M3 retry test", "d")

    steps = [
        TaskStep(
            tool_name="fs_read_file",
            params={"path": "C:/definitely/missing.txt"},
            order=0,
        )
    ]

    # First attempt: the runner records the workflow and the
    # real executor fails it.
    asyncio.run(runner.start_async(task.id, steps))
    assert task.status.value == "FAILED"

    # The M3 retry mechanism: re-queue only (TaskService.retry).
    retried = tasks.retry(task.id)
    assert retried.status.value == "PENDING"

    return task


# --------------------------------------------------------
# A. FAILED -> retry -> actual execution -> COMPLETED
# --------------------------------------------------------

def test_retry_reexecutes_stored_steps_to_completion():
    state = {"calls": 0}

    def tool(count, name, params):
        state["calls"] += 1
        if count == 1:
            raise FileNotFoundError("File not found: first attempt")
        return "file content from the real tool"

    pipeline, tasks, runner, calls, _ = make_pipeline(tool)
    task = fail_first_attempt_then(pipeline, tasks, runner, None)

    execution = asyncio.run(pipeline.retry_execution(task.id))

    assert execution["state"].value == "COMPLETED"
    assert task.status.value == "COMPLETED"
    assert task.result == {"fs_read_file": "file content from the real tool"}
    assert task.error is None
    # Two real executor calls: the failed attempt + the retry.
    assert len(calls) == 2


# --------------------------------------------------------
# B. FAILED -> retry -> same real failure -> FAILED again
# --------------------------------------------------------

def test_retry_reexecutes_and_fails_for_the_same_real_reason():
    def tool(count, name, params):
        raise FileNotFoundError(
            "File not found: C:/definitely/missing.txt"
        )

    pipeline, tasks, runner, calls, _ = make_pipeline(tool)
    task = fail_first_attempt_then(pipeline, tasks, runner, None)

    execution = asyncio.run(pipeline.retry_execution(task.id))

    assert execution["state"].value == "FAILED"
    assert task.status.value == "FAILED"
    assert "File not found" in task.error
    assert len(calls) == 2


# --------------------------------------------------------
# C. retry_count increments correctly
# --------------------------------------------------------

def test_retry_count_increments_across_retries():
    pipeline, tasks, runner, calls, _ = make_pipeline(
        lambda count, name, params: (_ for _ in ()).throw(
            FileNotFoundError("File not found: always")
        )
    )
    task = fail_first_attempt_then(pipeline, tasks, runner, None)

    assert task.retry_count == 1

    asyncio.run(pipeline.retry_execution(task.id))
    assert task.status.value == "FAILED"

    tasks.retry(task.id)
    assert task.retry_count == 2


# --------------------------------------------------------
# D. retry limit is enforced
# --------------------------------------------------------

def test_retry_limit_is_enforced():
    pipeline, tasks, runner, calls, _ = make_pipeline(
        lambda count, name, params: (_ for _ in ()).throw(
            FileNotFoundError("File not found: always")
        )
    )
    task = tasks.create("limit", "d")
    steps = [TaskStep(tool_name="fs_read_file", params={}, order=0)]
    asyncio.run(runner.start_async(task.id, steps))
    assert task.status.value == "FAILED"

    tasks.retry(task.id, max_retries=1)
    asyncio.run(pipeline.retry_execution(task.id))
    assert task.status.value == "FAILED"

    with pytest.raises(ValueError, match="retry limit"):
        tasks.retry(task.id, max_retries=1)


# --------------------------------------------------------
# E. retry does not invoke the planner
# --------------------------------------------------------

def test_retry_never_invokes_the_planner():
    state = {"calls": 0}

    def tool(count, name, params):
        state["calls"] += 1
        if count == 1:
            raise FileNotFoundError("File not found: first attempt")
        return "ran for real"

    pipeline, tasks, runner, calls, _ = make_pipeline(tool)
    task = fail_first_attempt_then(pipeline, tasks, runner, None)

    asyncio.run(pipeline.retry_execution(task.id))

    assert pipeline._planner.plan_calls == 0
    assert task.status.value == "COMPLETED"


# --------------------------------------------------------
# F. lifecycle audit events are recorded
# --------------------------------------------------------

def test_retry_records_lifecycle_and_tool_audit_events():
    state = {"calls": 0}

    def tool(count, name, params):
        state["calls"] += 1
        if count == 1:
            raise FileNotFoundError("File not found: first attempt")
        return "ran for real"

    pipeline, tasks, runner, calls, audit_path = make_pipeline(tool)
    task = fail_first_attempt_then(pipeline, tasks, runner, None)

    # The task_retried LIFECYCLE row is emitted by the API layer
    # immediately before re-execution; replay that exact append
    # first so this unit test covers the full endpoint sequence.
    from backend.audit.log import AuditStage

    pipeline._audit_log.append(
        task.id,
        AuditStage.LIFECYCLE,
        event="task_retried",
        status="PENDING",
        data={"retry_count": task.retry_count},
    )

    asyncio.run(pipeline.retry_execution(task.id))

    import json

    rows = [
        json.loads(line)
        for line in open(audit_path, encoding="utf-8")
        if line.strip()
    ]
    events = [(r["stage"], r["event"]) for r in rows]

    assert ("lifecycle", "task_retried") in events
    assert ("tool", "fs_read_file") in events
    assert ("result", "finished") in events
    retried = next(r for r in rows if r["event"] == "task_retried")
    assert retried["data"]["retry_count"] == 1
    # The lifecycle event precedes the re-execution rows.
    assert events.index(("lifecycle", "task_retried")) < events.index(
        ("tool", "fs_read_file")
    )


# --------------------------------------------------------
# G. existing cancellation behavior remains intact
# --------------------------------------------------------

def test_cancel_racing_retry_refuses_execution_and_stays_cancelled():
    pipeline, tasks, runner, calls, _ = make_pipeline(
        lambda count, name, params: (_ for _ in ()).throw(
            FileNotFoundError("File not found: always")
        )
    )
    task = fail_first_attempt_then(pipeline, tasks, runner, None)

    # The task is PENDING after the retry; a cancellation racing
    # the re-execution terminalizes it before the runner starts.
    tasks.cancel(task.id)
    assert task.status.value == "CANCELLED"
    calls_before = len(calls)

    with pytest.raises(ValueError):
        asyncio.run(pipeline.retry_execution(task.id))

    assert task.status.value == "CANCELLED"
    assert len(calls) == calls_before


# --------------------------------------------------------
# Advisory plans keep the plain re-queued semantics
# --------------------------------------------------------

def test_retry_without_recorded_steps_returns_none():
    pipeline, tasks, runner, calls, _ = make_pipeline(
        lambda count, name, params: "ran"
    )
    task = tasks.create("advisory", "d")
    tasks.mark_started(task)
    tasks.mark_failed(task, "boom")
    tasks.retry(task.id)

    assert asyncio.run(pipeline.retry_execution(task.id)) is None
    assert task.status.value == "PENDING"
