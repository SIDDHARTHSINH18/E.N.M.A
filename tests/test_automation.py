from backend.agents.executor import Agent
from backend.automation.engine import (
    AutomationEngine,
    TaskStep,
    WorkflowState,
)
from backend.core.task import Task, TaskStatus
from backend.permissions.policy import PermissionPolicy
from backend.tools.registry import RiskLevel, ToolRegistry


# ============================================================
# Deterministic mock tool execution (testing only — no real
# filesystem, shell, browser, email or other integrations).
# ============================================================

class MockToolExecutor:
    """Maps tool name -> return value or exception."""

    def __init__(self):
        self.behaviors = {}
        self.calls = []

    def set(self, tool_name, value):
        self.behaviors[tool_name] = value

    def __call__(self, tool_name, params):
        self.calls.append((tool_name, dict(params)))
        value = self.behaviors[tool_name]
        if isinstance(value, Exception):
            raise value
        return value


def make_engine(executor=None):
    registry = ToolRegistry()
    registry.register("read_note", "Read a note", "memory", RiskLevel.SAFE)
    registry.register("summarize", "Summarize text", "docs", RiskLevel.SAFE)
    registry.register("format_note", "Format a note", "memory", RiskLevel.SAFE)
    registry.register("send_email", "Send an email", "comms", RiskLevel.SENSITIVE)
    registry.register("delete_all", "Delete everything", "system", RiskLevel.DANGEROUS)

    agent = Agent(
        name="task-agent",
        registry=registry,
        permission_policy=PermissionPolicy(registry),
        tool_executor=executor if executor is not None else MockToolExecutor(),
    )
    return AutomationEngine(agent), agent


# ============================================================
# Step representation
# ============================================================

def test_step_has_identity_order_and_defaults():
    step = TaskStep(tool_name="read_note", params={"key": "a"}, order=1)
    assert isinstance(step.id, str) and step.id
    assert step.order == 1
    assert step.tool_name == "read_note"
    assert step.params == {"key": "a"}
    assert step.status == TaskStatus.PENDING
    assert step.result is None
    assert step.error is None


def test_steps_have_unique_ids():
    s1 = TaskStep(tool_name="read_note")
    s2 = TaskStep(tool_name="read_note")
    assert s1.id != s2.id


# ============================================================
# Workflow behavior
# ============================================================

def test_steps_execute_in_order():
    executor = MockToolExecutor()
    for tool in ("read_note", "summarize", "format_note"):
        executor.set(tool, f"{tool} output")
    engine, _ = make_engine(executor)

    steps = [
        TaskStep(tool_name="format_note", order=2),
        TaskStep(tool_name="read_note", order=0),
        TaskStep(tool_name="summarize", order=1),
    ]
    task = Task(title="Pipeline", description="d")
    result = engine.run(task, steps)

    assert [call[0] for call in executor.calls] == [
        "read_note",
        "summarize",
        "format_note",
    ]
    assert result.state == WorkflowState.COMPLETED


def test_successful_multi_step_workflow_completes_task():
    executor = MockToolExecutor()
    executor.set("read_note", "note contents")
    executor.set("summarize", "short summary")
    engine, _ = make_engine(executor)

    steps = [
        TaskStep(tool_name="read_note", order=0),
        TaskStep(tool_name="summarize", order=1),
    ]
    task = Task(title="Pipeline", description="d")
    result = engine.run(task, steps)

    assert result.state == WorkflowState.COMPLETED
    assert task.status == TaskStatus.COMPLETED
    assert task.error is None
    assert task.result == {
        "read_note": "note contents",
        "summarize": "short summary",
    }
    assert all(s.status == TaskStatus.COMPLETED for s in result.steps)
    assert result.steps[0].result == "note contents"
    assert result.steps[1].result == "short summary"


def test_step_failure_stops_subsequent_steps():
    executor = MockToolExecutor()
    executor.set("read_note", "note contents")
    executor.set("summarize", RuntimeError("model offline"))
    executor.set("format_note", "never reached")
    engine, _ = make_engine(executor)

    steps = [
        TaskStep(tool_name="read_note", order=0),
        TaskStep(tool_name="summarize", order=1),
        TaskStep(tool_name="format_note", order=2),
    ]
    task = Task(title="Pipeline", description="d")
    result = engine.run(task, steps)

    assert result.state == WorkflowState.FAILED
    assert task.status == TaskStatus.FAILED
    assert "model offline" in task.error
    # The failing step recorded its error; the last step never ran.
    assert [c[0] for c in executor.calls] == ["read_note", "summarize"]
    assert result.steps[1].status == TaskStatus.FAILED
    assert "model offline" in result.steps[1].error
    assert result.steps[2].status == TaskStatus.PENDING
    assert result.steps[2].result is None


def test_dangerous_step_blocked_and_workflow_fails():
    executor = MockToolExecutor()
    executor.set("read_note", "ok")
    engine, _ = make_engine(executor)

    steps = [
        TaskStep(tool_name="read_note", order=0),
        TaskStep(tool_name="delete_all", order=1),
        TaskStep(tool_name="summarize", order=2),
    ]
    executor.set("summarize", "never reached")
    task = Task(title="Pipeline", description="d")
    result = engine.run(task, steps)

    assert result.state == WorkflowState.FAILED
    assert task.status == TaskStatus.FAILED
    assert "DANGEROUS" in task.error
    # delete_all never executed; nothing after it either.
    assert [c[0] for c in executor.calls] == ["read_note"]
    assert result.steps[1].status == TaskStatus.FAILED
    assert result.steps[2].status == TaskStatus.PENDING


def test_sensitive_step_pauses_workflow():
    executor = MockToolExecutor()
    executor.set("send_email", "sent")
    engine, _ = make_engine(executor)

    steps = [
        TaskStep(tool_name="send_email", order=0),
        TaskStep(tool_name="summarize", order=1),
    ]
    executor.set("summarize", "never reached")
    task = Task(title="Pipeline", description="d")
    result = engine.run(task, steps)

    assert result.state == WorkflowState.PAUSED
    assert task.status == TaskStatus.RUNNING
    assert "SENSITIVE" in result.reason
    # Nothing executed, nothing failed: waiting for approval.
    assert executor.calls == []
    assert result.steps[0].status == TaskStatus.PENDING
    assert result.steps[0].error is None
    assert result.steps[1].status == TaskStatus.PENDING


def test_unknown_tool_stops_workflow():
    executor = MockToolExecutor()
    engine, _ = make_engine(executor)

    steps = [
        TaskStep(tool_name="nonexistent_tool", order=0),
        TaskStep(tool_name="read_note", order=1),
    ]
    executor.set("read_note", "never reached")
    task = Task(title="Pipeline", description="d")
    result = engine.run(task, steps)

    assert result.state == WorkflowState.FAILED
    assert task.status == TaskStatus.FAILED
    assert "Unknown tool" in task.error
    assert executor.calls == []
    assert result.steps[0].status == TaskStatus.FAILED
    assert result.steps[1].status == TaskStatus.PENDING


def test_step_results_and_errors_preserved_independently():
    executor = MockToolExecutor()
    executor.set("read_note", "note contents")
    executor.set("summarize", ValueError("bad input"))
    engine, _ = make_engine(executor)

    steps = [
        TaskStep(tool_name="read_note", order=0),
        TaskStep(tool_name="summarize", order=1),
    ]
    task = Task(title="Pipeline", description="d")
    result = engine.run(task, steps)

    succeeded = result.steps[0]
    failed = result.steps[1]
    assert succeeded.status == TaskStatus.COMPLETED
    assert succeeded.result == "note contents"
    assert succeeded.error is None
    assert failed.status == TaskStatus.FAILED
    assert failed.result is None
    assert "bad input" in failed.error


def test_final_task_status_reflects_workflow():
    executor = MockToolExecutor()
    executor.set("read_note", "ok")
    engine, _ = make_engine(executor)

    completed_task = Task(title="A", description="d")
    engine.run(completed_task, [TaskStep(tool_name="read_note", order=0)])
    assert completed_task.status == TaskStatus.COMPLETED

    failed_task = Task(title="B", description="d")
    engine.run(failed_task, [TaskStep(tool_name="nonexistent_tool", order=0)])
    assert failed_task.status == TaskStatus.FAILED

    paused_task = Task(title="C", description="d")
    engine.run(paused_task, [TaskStep(tool_name="send_email", order=0)])
    assert paused_task.status == TaskStatus.RUNNING


# ============================================================
# Same-tool result identity (P1 hardening)
# ============================================================

def test_same_tool_steps_keep_distinct_results():
    """Two steps using the SAME tool must both survive in the
    final task result: the first keeps the plain tool name,
    the second gets a '<tool>#1' key."""

    executor = MockToolExecutor()
    executor.set("read_note", "first read")
    engine, _ = make_engine(executor)

    steps = [
        TaskStep(tool_name="read_note", order=0),
        TaskStep(tool_name="read_note", order=1),
    ]
    task = Task(title="Same tool twice", description="d")
    result = engine.run(task, steps)

    assert result.state == WorkflowState.COMPLETED
    assert task.result == {
        "read_note": "first read",
        "read_note#1": "first read",
    }
    # The in-memory step records stay the source of truth for
    # per-step results either way.
    assert result.steps[0].result == "first read"
    assert result.steps[1].result == "first read"
    assert executor.calls == [
        ("read_note", {}),
        ("read_note", {}),
    ]


def test_single_tool_result_keeps_plain_name():
    """Backwards compatibility: one step per tool is keyed by
    the bare tool name exactly as before."""

    executor = MockToolExecutor()
    executor.set("read_note", "note contents")
    executor.set("summarize", "short summary")
    engine, _ = make_engine(executor)

    steps = [
        TaskStep(tool_name="read_note", order=0),
        TaskStep(tool_name="summarize", order=1),
    ]
    task = Task(title="Distinct tools", description="d")
    result = engine.run(task, steps)

    assert task.result == {
        "read_note": "note contents",
        "summarize": "short summary",
    }


# ============================================================
# P0.2 — execution-correctness stress (multi/same-tool, deps,
# failed-dependency propagation, partial results, cancellation)
# ============================================================

def test_mixed_repeated_tools_all_independently_addressable():
    """Step1 same_tool, Step2 same_tool, Step3 other, Step4
    same_tool: every result survives under its own key."""

    executor = MockToolExecutor()
    engine, _ = make_engine(executor)

    # Same tool, different results per call (stateful mock).
    responses = iter(["r0", "r1", "r2", "r3"])

    def cycling(name, params):
        return next(responses)

    engine, agent = make_engine(executor)
    agent._execute_tool = cycling

    steps = [
        TaskStep(tool_name="read_note", order=0),
        TaskStep(tool_name="read_note", order=1),
        TaskStep(tool_name="summarize", order=2),
        TaskStep(tool_name="read_note", order=3),
    ]
    task = Task(title="Mixed repeated tools", description="d")
    result = engine.run(task, steps)

    assert result.state == WorkflowState.COMPLETED
    assert task.result == {
        "read_note": "r0",
        "read_note#1": "r1",
        "summarize": "r2",
        "read_note#2": "r3",
    }


def test_dependency_reference_flows_between_steps():
    """A {"from_step": N, "field": ...} param reference resolves
    from the dependency's actual result."""

    executor = MockToolExecutor()
    executor.set("read_note", {"text": "hello world"})
    executor.set("summarize", "summary of: hello world")
    engine, _ = make_engine(executor)

    steps = [
        TaskStep(tool_name="read_note", order=0),
        TaskStep(
            tool_name="summarize",
            params={"text": {"from_step": 0, "field": "text"}},
            dependencies=[0],
            order=1,
        ),
    ]
    task = Task(title="Dependent steps", description="d")
    result = engine.run(task, steps)

    assert result.state == WorkflowState.COMPLETED
    # The summarize step received the upstream field value.
    assert executor.calls[1] == ("summarize", {"text": "hello world"})


def test_failed_dependency_blocks_dependent_step():
    """When a dependency fails, the dependent step must not run
    and the workflow must fail — no partial fake success."""

    executor = MockToolExecutor()
    executor.set("read_note", RuntimeError("disk exploded"))
    executor.set("summarize", "never reached")
    engine, _ = make_engine(executor)

    steps = [
        TaskStep(tool_name="read_note", order=0),
        TaskStep(
            tool_name="summarize",
            dependencies=[0],
            order=1,
        ),
    ]
    task = Task(title="Broken dependency", description="d")
    result = engine.run(task, steps)

    assert result.state == WorkflowState.FAILED
    assert result.steps[0].status == TaskStatus.FAILED
    # The workflow aborted before the dependent step: it was
    # never attempted, so it stays PENDING — never a fake FAILED
    # or COMPLETED.
    assert all(
        call[0] != "summarize" for call in executor.calls
    )
    assert result.steps[1].status == TaskStatus.PENDING
    assert task.status == TaskStatus.FAILED


def test_cancellation_stops_remaining_steps():
    """Cooperative cancellation: steps after the cancel request
    do not run; the workflow does not report COMPLETED."""

    executor = MockToolExecutor()
    engine, agent = make_engine(executor)

    task = Task(title="Cancelled workflow", description="d")

    def cancel_at_first_step(name, params):
        task.cancel_requested = True
        return "first done"

    agent._execute_tool = cancel_at_first_step

    steps = [
        TaskStep(tool_name="read_note", order=0),
        TaskStep(tool_name="summarize", order=1),
    ]
    result = engine.run(task, steps)

    assert result.state != WorkflowState.COMPLETED
    assert all(call[0] != "summarize" for call in executor.calls)
    assert task.status == TaskStatus.CANCELLED


def test_permission_denied_step_records_denial():
    """A DANGEROUS tool is denied by the policy before any tool
    execution: the step fails with the denial, later steps never
    execute, and the task fails truthfully."""

    executor = MockToolExecutor()
    executor.set("delete_all", "would have deleted")
    engine, agent = make_engine(executor)

    steps = [
        TaskStep(tool_name="delete_all", order=0),
        TaskStep(tool_name="read_note", order=1),
    ]
    task = Task(title="Denied step", description="d")
    result = engine.run(task, steps)

    assert result.state == WorkflowState.FAILED
    assert all(call[0] != "delete_all" for call in executor.calls)
    assert result.steps[0].decision.name == "DENY"
    assert result.steps[0].result is None
    assert task.status == TaskStatus.FAILED


def test_partial_execution_result_reflects_completed_steps_only():
    """A mid-workflow failure still surfaces the completed
    steps' results on the task — execution evidence, not
    wholesale erasure."""

    executor = MockToolExecutor()
    executor.set("read_note", "good content")
    executor.set("summarize", RuntimeError("model down"))
    engine, _ = make_engine(executor)

    steps = [
        TaskStep(tool_name="read_note", order=0),
        TaskStep(tool_name="summarize", order=1),
    ]
    task = Task(title="Partial", description="d")
    result = engine.run(task, steps)

    assert result.state == WorkflowState.FAILED
    assert result.steps[0].status == TaskStatus.COMPLETED
    assert result.steps[0].result == "good content"
    assert task.error  # truthful failure reason present
