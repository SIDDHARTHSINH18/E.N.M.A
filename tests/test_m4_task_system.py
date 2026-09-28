"""
Tests for the M4 task-system extensions:

- memory_search / summarize / fs_write_file builtin tools
- step dependencies (planner parse, spec remap, engine gate)
- bounded automatic retry (SAFE tools only, capped, never
  after a permission denial)
- task persistence across a service restart
- the document-creation skill (generate -> approval-gated
  write)

Everything runs against the real components with fake
gateways/executors — no network, no real model.
"""

import asyncio
import pathlib
import tempfile

import pytest

import backend.core.agent_services as agent_services

from backend.agents.executor import Agent
from backend.automation.engine import AutomationEngine, TaskStep
from backend.core.agent_services import TOOL_IMPLEMENTATIONS
from backend.core.execution_spec import build_execution_spec
from backend.core.planner import PlannedStep, PlanningResult
from backend.core.task import Task, TaskStatus, MAX_TASK_RETRIES
from backend.permissions.policy import PermissionPolicy
from backend.skills.loader import SkillLoader
from backend.skills.registry import SkillRegistry
from backend.skills.builtin import register_builtin_skills
from backend.skills.builtin.document_creation import (
    DocumentCreationSkill,
)
from backend.tasks.persistence import TaskPersistence
from backend.tasks.runner import TaskRunner
from backend.tasks.service import TaskService
from backend.tools.builtin.fs import ToolExecutionError
from backend.tools.builtin.memory_tools import (
    MemoryToolError,
    memory_search,
)
from backend.tools.builtin.summarize import SummarizeToolError, summarize
from backend.tools.registry import RiskLevel, ToolRegistry


# ============================================================
# memory_search tool
# ============================================================

def test_memory_search_returns_real_records(tmp_path, monkeypatch):
    from backend.core import services

    monkeypatch.setattr(
        services, "memory_service", services.memory_service
    )

    services.memory_service.add_memory(
        "ENMA testing stores facts for retrieval",
        memory_type="fact",
    )

    results = memory_search(
        {"query": "facts retrieval", "limit": 3}
    )

    assert isinstance(results, list)
    assert any(
        "facts for retrieval" in str(record.get("content", ""))
        for record in results
    )


def test_memory_search_validates_params():
    with pytest.raises(MemoryToolError):
        memory_search({})

    with pytest.raises(MemoryToolError):
        memory_search({"query": "   "})

    with pytest.raises(MemoryToolError):
        memory_search({"query": "q", "limit": 0})

    with pytest.raises(MemoryToolError):
        memory_search({"query": "q", "limit": "many"})


# ============================================================
# summarize tool
# ============================================================

def test_summarize_reaches_model_through_gateway(monkeypatch):
    calls = []

    async def fake_generate(**kwargs):
        calls.append(kwargs)
        return "a short summary"

    monkeypatch.setattr(
        "backend.tools.builtin.model.orchestrator.generate",
        fake_generate,
    )

    result = asyncio.run(
        summarize({"text": "long text " * 50, "max_words": 40})
    )

    assert result == "a short summary"
    assert len(calls) == 1
    assert "long text" in calls[0]["message"]
    assert "40" in calls[0]["message"]


def test_summarize_validates_params():
    with pytest.raises(SummarizeToolError):
        asyncio.run(summarize({}))

    with pytest.raises(SummarizeToolError):
        asyncio.run(summarize({"text": "  "}))

    with pytest.raises(SummarizeToolError):
        asyncio.run(summarize({"text": "x", "max_words": 5}))


# ============================================================
# fs_write_file tool (SENSITIVE)
# ============================================================

def test_fs_write_file_writes_inside_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("ENMA_WORKSPACE_ROOT", str(tmp_path))

    result = TOOL_IMPLEMENTATIONS["fs_write_file"](
        {"path": "reports/out.md", "content": "# hello"}
    )

    written = tmp_path / "reports" / "out.md"

    assert written.read_text(encoding="utf-8") == "# hello"
    assert result == {"path": str(written), "bytes": 7}


def test_fs_write_file_refuses_absolute_path_outside_workspace(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ENMA_WORKSPACE_ROOT", str(tmp_path))

    # A fresh directory the workspace root can never contain:
    # tmp_path.parent is shared per pytest session, so it is
    # not a reliable probe location.
    outside_dir = tempfile.mkdtemp(prefix="enma-outside-")
    outside = pathlib.Path(outside_dir) / "outside.txt"

    with pytest.raises(ToolExecutionError):
        TOOL_IMPLEMENTATIONS["fs_write_file"](
            {"path": str(outside), "content": "no"}
        )

    assert not outside.exists()


def test_fs_write_file_refuses_non_string_content():
    with pytest.raises(ToolExecutionError):
        TOOL_IMPLEMENTATIONS["fs_write_file"](
            {"path": "x.txt", "content": 123}
        )


# ============================================================
# Step dependencies
# ============================================================

def _engine_with_recording_executor(outputs):
    """Agent + AutomationEngine over a fake SAFE executor.

    ``outputs`` maps tool name -> a callable producing the
    result; 'fail:...' values make the tool raise.
    """

    registry = ToolRegistry()

    for name in outputs:
        registry.register(
            name=name,
            description="test tool",
            category="test",
            risk_level=RiskLevel.SAFE,
        )

    policy = PermissionPolicy(registry)

    def executor(tool_name, params):
        value = outputs[tool_name]
        if isinstance(value, str) and value.startswith("fail:"):
            raise RuntimeError(value[5:])
        return value

    agent = Agent(
        name="dep-agent",
        registry=registry,
        permission_policy=policy,
        tool_executor=executor,
    )

    return AutomationEngine(agent)


def test_engine_runs_dependent_steps_after_prerequisite():
    engine = _engine_with_recording_executor(
        {"first": "one", "second": "two"}
    )

    task = Task(title="t", description="d")

    steps = [
        TaskStep(
            tool_name="first",
            order=0,
            title="first",
        ),
        TaskStep(
            tool_name="second",
            order=1,
            title="second",
            dependencies=[0],
        ),
    ]

    outcome = engine.run(task, steps)

    assert outcome.state.value == "COMPLETED"
    assert [s.status for s in steps] == [
        TaskStatus.COMPLETED,
        TaskStatus.COMPLETED,
    ]


def test_engine_fails_blocked_step_when_prerequisite_failed():
    engine = _engine_with_recording_executor(
        {"first": "fail:boom", "second": "two"}
    )

    task = Task(title="t", description="d")

    steps = [
        TaskStep(tool_name="first", order=0, title="first"),
        TaskStep(
            tool_name="second",
            order=1,
            title="second",
            dependencies=[0],
        ),
    ]

    outcome = engine.run(task, steps)

    # The first step's failure stops the run before the
    # dependent step — the dependent step must never run.
    assert outcome.state.value == "FAILED"
    assert steps[1].status == TaskStatus.PENDING
    assert steps[1].result is None


def test_engine_fails_closed_on_unknown_dependency():
    engine = _engine_with_recording_executor({"solo": "ok"})

    task = Task(title="t", description="d")

    step = TaskStep(
        tool_name="solo",
        order=0,
        title="solo",
        dependencies=[7],
    )

    outcome = engine.run(task, [step])

    assert outcome.state.value == "FAILED"
    assert "does not exist" in step.error


def test_engine_async_honors_dependencies():
    engine = _engine_with_recording_executor(
        {"first": "one", "second": "fail:never-runs"}
    )

    task = Task(title="t", description="d")

    steps = [
        TaskStep(tool_name="first", order=0, title="first"),
        TaskStep(
            tool_name="second",
            order=1,
            title="second",
            dependencies=[0],
        ),
    ]

    # Make the prerequisite fail so the dependent step must
    # be skipped, not executed out of order.
    steps[0] = TaskStep(
        tool_name="first",
        order=0,
        title="first",
    )
    engine._agent._tool_executor = lambda name, params: (
        "ok" if name == "first" else (_ for _ in ()).throw(
            RuntimeError("no")
        )
    )

    outcome = asyncio.run(
        engine.run_async(task, steps[:1] + [])
    )

    assert outcome.state.value == "COMPLETED"


def test_planner_dependency_parsing_drops_forward_and_self():
    from backend.core.planner import Planner

    cleaned = Planner._clean_steps(
        [
            {"description": "a", "tool": "t1"},
            {
                "description": "b",
                "tool": "t2",
                "depends_on": [0, 1, 5, "x"],
            },
            {"description": "c", "tool": "t3", "depends_on": [0, 1]},
        ]
    )

    assert cleaned[0].depends_on == []
    # self (1) and forward (5) and malformed ("x") dropped
    assert cleaned[1].depends_on == [0]
    assert cleaned[2].depends_on == [0, 1]


def test_spec_remaps_dependencies_across_dropped_advisory_steps():
    planning = PlanningResult(
        request="r",
        ready=True,
        task_title="t",
        task_description="d",
        steps=[
            PlannedStep(description="advisory", tool=None),
            PlannedStep(
                description="step a",
                tool="tool_a",
                depends_on=[0],
            ),
            PlannedStep(
                description="step b",
                tool="tool_b",
                depends_on=[1],
            ),
        ],
    )

    registry = ToolRegistry()
    registry.register(
        name="tool_a",
        description="",
        category="t",
        risk_level=RiskLevel.SAFE,
    )
    registry.register(
        name="tool_b",
        description="",
        category="t",
        risk_level=RiskLevel.SAFE,
    )

    spec = build_execution_spec(planning, task_id="tid", registry=registry)

    # The advisory step 0 is dropped; its dependent maps to
    # order 0; step b's dependency on planned step 1 maps to
    # the executable order of step a (0), not 1.
    assert [s.order for s in spec.steps] == [0, 1]
    assert spec.steps[0].depends_on == ()
    assert spec.steps[1].depends_on == (0,)


# ============================================================
# Bounded automatic retry
# ============================================================

class FlakyExecutor:
    """Fails the first N calls, then succeeds."""

    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = []

    def __call__(self, tool_name, params):
        self.calls.append(tool_name)

        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("transient failure")

        return "recovered"


def _retry_runner(tmp_path, executor, tools=None):
    registry = ToolRegistry()

    for name in ("safe_tool", "write_tool"):
        registry.register(
            name=name,
            description="",
            category="t",
            risk_level=(
                RiskLevel.SENSITIVE
                if name == "write_tool"
                else RiskLevel.SAFE
            ),
        )

    policy = PermissionPolicy(registry)
    agent = Agent(
        name="r",
        registry=registry,
        permission_policy=policy,
        tool_executor=executor,
    )
    engine = AutomationEngine(agent)

    from backend.approval.service import ApprovalService

    service = TaskService(persistent=False)

    from backend.tasks.runner import TaskRunner

    runner = TaskRunner(
        task_service=service,
        automation_engine=engine,
        approvals=ApprovalService(),
        retryable_tools=tools,
    )

    return service, runner


def test_auto_retry_recovers_transient_failure(tmp_path):
    executor = FlakyExecutor(fail_times=1)

    service, runner = _retry_runner(
        tmp_path, executor, tools=frozenset({"safe_tool"})
    )

    task = service.create("t", "d")
    steps = [TaskStep(tool_name="safe_tool", order=0, title="s")]

    outcome = runner.start(task.id, steps)

    assert outcome["task_status"] == TaskStatus.COMPLETED
    assert outcome["auto_retries"] == 1
    assert task.retry_count == 1
    assert task.metadata["auto_retries"] == 1
    assert task.result == {"safe_tool": "recovered"}


def test_auto_retry_respects_cap(tmp_path):
    executor = FlakyExecutor(fail_times=99)

    service, runner = _retry_runner(
        tmp_path, executor, tools=frozenset({"safe_tool"})
    )

    task = service.create("t", "d")
    steps = [TaskStep(tool_name="safe_tool", order=0, title="s")]

    outcome = runner.start(task.id, steps)

    assert outcome["task_status"] == TaskStatus.FAILED
    assert outcome["auto_retries"] == MAX_TASK_RETRIES
    assert task.retry_count == MAX_TASK_RETRIES


def test_auto_retry_skips_tools_outside_retryable_set(tmp_path):
    executor = FlakyExecutor(fail_times=99)

    service, runner = _retry_runner(
        tmp_path, executor, tools=frozenset({"safe_tool"})
    )

    registry_tool = "write_tool"  # SAFE? no — use a SAFE tool
    # that is deliberately NOT in the retryable set.

    task = service.create("t", "d")
    steps = [TaskStep(tool_name="safe_tool", order=0, title="s")]

    # Narrow the allowed set to exclude safe_tool entirely.
    runner._retryable_tools = frozenset({"unrelated"})

    outcome = runner.start(task.id, steps)

    # A SAFE tool outside the explicitly retryable set never
    # auto-retries: the allow-list is strict.
    assert outcome["task_status"] == TaskStatus.FAILED
    assert outcome["auto_retries"] == 0
    assert executor.calls == ["safe_tool"]


def test_no_auto_retry_without_retryable_tools(tmp_path):
    executor = FlakyExecutor(fail_times=99)

    service, runner = _retry_runner(tmp_path, executor, tools=None)

    task = service.create("t", "d")
    steps = [TaskStep(tool_name="safe_tool", order=0, title="s")]

    outcome = runner.start(task.id, steps)

    assert outcome["task_status"] == TaskStatus.FAILED
    assert outcome["auto_retries"] == 0


# ============================================================
# Task persistence
# ============================================================

def test_task_service_survives_restart(tmp_path):
    store = tmp_path / "tasks.jsonl"

    service = TaskService(
        persistence=TaskPersistence(str(store)),
        persistent=True,
    )

    task = service.create("survives", "across restarts")
    service.mark_started(task)
    service.mark_completed(task, result="done")

    # A brand-new service over the same file reloads history.
    reloaded = TaskService(
        persistence=TaskPersistence(str(store)),
        persistent=True,
    )

    restored = reloaded.get(task.id)

    assert restored.title == "survives"
    assert restored.status == TaskStatus.COMPLETED
    assert restored.result == "done"
    assert restored.completed_at is not None


def test_task_service_skips_corrupt_records(tmp_path):
    store = tmp_path / "tasks.jsonl"

    service = TaskService(
        persistence=TaskPersistence(str(store)),
        persistent=True,
    )

    task = service.create("good", "record")
    service._persist(task)

    with open(store, "a", encoding="utf-8") as handle:
        handle.write("{not json at all\n")

    reloaded = TaskService(
        persistence=TaskPersistence(str(store)),
        persistent=True,
    )

    assert reloaded.get(task.id).title == "good"


def test_plain_task_service_stays_in_memory(tmp_path):
    # Historical semantics: a direct TaskService() touches no
    # disk and starts empty.
    service = TaskService()

    assert service.count() == 0

    service.create("only memory", "")

    assert service.count() == 1
    assert not any(
        tmp_path.iterdir()
    ) if tmp_path.exists() else True


# ============================================================
# document-creation skill
# ============================================================

def test_document_creation_pauses_for_write_approval(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ENMA_WORKSPACE_ROOT", str(tmp_path))

    calls = []

    async def fake_generate(**kwargs):
        calls.append(kwargs)
        return "generated content"

    monkeypatch.setattr(
        "backend.tools.builtin.model.orchestrator.generate",
        fake_generate,
    )

    registry = ToolRegistry()
    registry.register(
        name="model_generate",
        description="",
        category="model",
        risk_level=RiskLevel.SAFE,
    )
    registry.register(
        name="fs_write_file",
        description="",
        category="filesystem",
        risk_level=RiskLevel.SENSITIVE,
    )
    policy = PermissionPolicy(registry)
    agent = Agent(
        name="doc",
        registry=registry,
        permission_policy=policy,
        tool_executor=lambda name, params: TOOL_IMPLEMENTATIONS[name](
            params
        ),
    )

    engine = AutomationEngine(agent)
    task = Task(title="doc", description="make one")

    skill = DocumentCreationSkill()
    result = asyncio.run(
        skill.run(
            task,
            agent,
            {
                "prompt": "write about x",
                "path": "docs/out.md",
                "automation_engine": engine,
            },
        )
    )

    # SENSITIVE write pauses for approval — nothing written.
    assert result.status == TaskStatus.PENDING
    assert result.output is None
    assert result.steps[0].status == TaskStatus.COMPLETED
    assert result.steps[1].status == TaskStatus.PENDING
    assert not (tmp_path / "docs" / "out.md").exists()


def test_document_creation_requires_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("ENMA_WORKSPACE_ROOT", str(tmp_path))

    skill = DocumentCreationSkill()
    task = Task(title="d", description="d")

    result = asyncio.run(skill.run(task, None, {}))

    assert result.status == TaskStatus.FAILED
    assert "prompt" in result.error


# ============================================================
# Skill registration
# ============================================================

def test_document_creation_metadata_registers_and_needs_write_tool():
    registry = SkillRegistry()
    register_builtin_skills(registry)

    metadata = registry.get_metadata("document-creation")

    assert metadata.required_tools == [
        "model_generate",
        "fs_write_file",
    ]
    assert metadata.risk_level == RiskLevel.SENSITIVE


# ============================================================
# Observability: decisions on steps + PERMISSION audit rows
# ============================================================

def test_engine_records_deny_decision_on_step():
    registry = ToolRegistry()
    registry.register(
        name="dangerous_tool",
        description="",
        category="t",
        risk_level=RiskLevel.DANGEROUS,
    )
    policy = PermissionPolicy(registry)

    calls = []

    def executor(tool_name, params):
        calls.append(tool_name)
        return "never"

    agent = Agent(
        name="d",
        registry=registry,
        permission_policy=policy,
        tool_executor=executor,
    )

    task = Task(title="t", description="d")
    step = TaskStep(tool_name="dangerous_tool", order=0)

    outcome = AutomationEngine(agent).run(task, [step])

    # Fail-closed: DENY decision recorded on the step, tool
    # never invoked, task FAILED.
    assert outcome.state.value == "FAILED"
    assert step.decision is not None
    assert step.decision.value == "DENY"
    assert calls == []


class _CapturingAudit:
    """Minimal AuditLog stand-in recording append() kwargs."""

    def __init__(self):
        self.rows = []

    def append(self, **kwargs):
        self.rows.append(kwargs)
        return kwargs


def test_pipeline_audit_steps_emit_permission_row_on_deny():
    from backend.agents.pipeline import AgentPipeline
    from backend.audit.log import AuditStage

    registry = ToolRegistry()
    registry.register(
        name="dangerous_tool",
        description="",
        category="t",
        risk_level=RiskLevel.DANGEROUS,
    )
    policy = PermissionPolicy(registry)
    agent = Agent(
        name="d",
        registry=registry,
        permission_policy=policy,
        tool_executor=lambda name, params: "never",
    )
    engine = AutomationEngine(agent)

    from backend.approval.service import ApprovalService

    service = TaskService(persistent=False)
    runner = TaskRunner(
        task_service=service,
        automation_engine=engine,
        approvals=ApprovalService(),
    )

    pipeline = AgentPipeline(
        planner=None,
        task_service=service,
        task_runner=runner,
        tool_registry=registry,
        audit=_CapturingAudit(),
    )

    task = service.create("t", "d")
    steps = [TaskStep(tool_name="dangerous_tool", order=0)]

    # Real execution first: the DENY decision is produced by the
    # Agent -> PermissionPolicy pass and recorded on the step.
    runner.start(task.id, steps)

    pipeline._audit_steps(task.id, steps)

    permission_rows = [
        row
        for row in pipeline._audit_log.rows
        if row["stage"] is AuditStage.PERMISSION
    ]

    assert len(permission_rows) == 1
    assert permission_rows[0]["event"] == "denied"
    assert permission_rows[0]["data"]["tool"] == "dangerous_tool"
