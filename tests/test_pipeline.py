"""
End-to-end tests for M4 step 7 — the agent pipeline.

The chain under test:

  request -> Planner -> ExecutionSpec -> SkillStage
        -> TaskRunner -> AutomationEngine -> Agent
        -> PermissionPolicy -> ToolRegistry
        -> Reflection -> MemoryBridge -> AuditLog

Every component here is the real one; only the model gateway
(a FakePlanner / a fake orchestrator) and the tool bodies
(a recording spy) are substituted, so these tests prove the
wiring itself:

- stage order and the audit rows each stage leaves behind
- the permission policy stays the only execution gate: a
  denied tool never reaches the executor and the denial is
  recorded on the task, the step and the audit trail
- a clarification round creates no task and runs nothing
- memory is written only from a recommended reflection
- the request path awaits: the model-gateway step runs inside
  the caller's loop, with no asyncio.run() and no thread bridge
"""

import asyncio
import ast
import inspect
import json
import threading
from types import SimpleNamespace

import pytest

import backend.agents.pipeline as pipeline_module
from backend.agents.memory_bridge import MemoryBridge
from backend.agents.pipeline import AgentPipeline, task_steps_from_spec
from backend.approval.service import ApprovalService, ApprovalStatus
from backend.agents.executor import Agent
from backend.automation.engine import AutomationEngine
from backend.audit.log import AuditLog, AuditStage
from backend.core.memory import MemoryService
from backend.core.planner import (
    PlannedStep,
    PlanningResult,
    PlanningSource,
)
from backend.core.task import TaskStatus
from backend.permissions.policy import PermissionPolicy
from backend.reflection.engine import ReflectionEngine
from backend.skills.skill import SkillResult
from backend.skills.stage import SkillSelection
from backend.tasks import TaskService
from backend.tasks.runner import TaskRunner
from backend.tools.registry import RiskLevel, ToolRegistry


# ============================================================
# Stack builder
# ============================================================

class SpyExecutor:
    """Records dispatches; proves a denied tool is never run."""

    def __init__(self):
        self.calls = []

    def __call__(self, tool_name, params):
        self.calls.append((tool_name, dict(params)))
        return f"{tool_name} output"


class FakePlanner:
    """Returns one canned PlanningResult; records its calls."""

    def __init__(self, planning):
        self._planning = planning
        self.calls = []

    async def plan(self, request, **kwargs):
        self.calls.append((request, kwargs))
        return self._planning


class FakeSkillStage:
    """Implements the SkillStage contract the pipeline uses."""

    def __init__(self, selection, result=None, raises=None):
        self._selection = selection
        self._result = result
        self._raises = raises
        self.selected = []
        self.executed = []

    def select(self, requirement):
        self.selected.append(requirement)
        return self._selection

    def execute(self, selection, task, params=None):
        self.executed.append((selection.skill, task.id))
        if self._raises is not None:
            raise self._raises
        return self._result

    async def execute_async(self, selection, task, params=None):
        return self.execute(selection, task, params)


class BrokenFileStore:
    """An audit store that cannot write: every append raises."""

    def append(self, **kwargs):
        raise OSError("audit disk is full")


def planning_with(*tools, ready=True, questions=(), source=None):
    return PlanningResult(
        request="do the work",
        ready=ready,
        task_title="Test task",
        task_description="Test description.",
        steps=[
            PlannedStep(
                description=f"Use {tool}",
                tool=tool,
                params={"path": "x"},
            )
            for tool in tools
        ],
        assumptions=["one assumption"],
        questions=list(questions),
        source=source or PlanningSource.DETERMINISTIC,
    )


_DEFAULT = object()


def build_stack(tmp_path, planning, *, skill_stage=_DEFAULT):
    if skill_stage is _DEFAULT:
        # Routing is wired by default so the stage order is the
        # production one; a no-match selection keeps it inert.
        skill_stage = FakeSkillStage(
            SkillSelection(reason="no skill matched the requirement")
        )

    registry = ToolRegistry()
    registry.register("read_note", "Read a note", "memory", RiskLevel.SAFE)
    registry.register("send_email", "Send an email", "comms", RiskLevel.SENSITIVE)
    registry.register("delete_all", "Delete everything", "system", RiskLevel.DANGEROUS)

    approvals = ApprovalService()
    policy = PermissionPolicy(registry, approval_grants=approvals)
    executor = SpyExecutor()
    agent = Agent(
        name="pipeline-test-agent",
        registry=registry,
        permission_policy=policy,
        tool_executor=executor,
    )
    store = TaskService()
    runner = TaskRunner(
        task_service=store,
        automation_engine=AutomationEngine(agent),
        approvals=approvals,
        reflection_engine=ReflectionEngine(),
    )
    memory = MemoryService(storage_path=str(tmp_path / "memory.json"))
    audit = AuditLog(storage_path=str(tmp_path / "audit.jsonl"))
    bridge = MemoryBridge(memory=memory, audit=audit)
    planner = FakePlanner(planning)

    pipeline = AgentPipeline(
        planner=planner,
        task_service=store,
        task_runner=runner,
        tool_registry=registry,
        skill_stage=skill_stage,
        reflection_engine=ReflectionEngine(),
        memory_bridge=bridge,
        audit=audit,
    )

    return SimpleNamespace(
        pipeline=pipeline,
        planner=planner,
        store=store,
        runner=runner,
        approvals=approvals,
        policy=policy,
        executor=executor,
        audit=audit,
        memory=memory,
        registry=registry,
    )


def run(coro):
    return asyncio.run(coro)


def stages(audit, task_id=None):
    return [
        row["stage"]
        for row in audit.read(task_id=task_id)
    ]


# ============================================================
# Happy path: plan -> spec -> tool -> result -> audit
# ============================================================

def test_safe_plan_runs_and_records_every_stage(tmp_path):
    stack = build_stack(tmp_path, planning_with("read_note"))

    outcome = run(stack.pipeline.handle_request("read the note"))

    assert outcome.clarification_required is False
    task = outcome.task
    assert task.status == TaskStatus.COMPLETED
    assert task.result == {"read_note": "read_note output"}

    # The executor saw exactly the planned call.
    assert stack.executor.calls == [("read_note", {"path": "x"})]

    # One PlanningResult became one ExecutionSpec became one step.
    assert outcome.spec.step_count == 1
    assert outcome.spec.steps[0].tool == "read_note"
    assert outcome.spec.risk_level == RiskLevel.SAFE.value
    assert outcome.spec.approval_required is False

    # The runner's envelope passed through unchanged.
    # auto_retries reports bounded automatic recovery attempts
    # (0 here — the run succeeded on its first pass).
    assert set(outcome.execution) == {
        "task_id",
        "state",
        "task_status",
        "approval_id",
        "auto_retries",
        "reflection",
    }

    # Every stage left its row, in order, correlated by task id.
    assert stages(stack.audit) == [
        AuditStage.REQUEST.value,
        AuditStage.PLANNED.value,
        AuditStage.SKILL.value,
        AuditStage.SPEC.value,
        AuditStage.TOOL.value,
        AuditStage.RESULT.value,
        AuditStage.REFLECTION.value,
        AuditStage.MEMORY.value,
    ]

    rows = {row["stage"]: row for row in stack.audit.read()}
    assert rows[AuditStage.REQUEST.value]["task_id"] is None
    tool_row = stack.audit.read(task_id=task.id, stage=AuditStage.TOOL)[0]
    assert tool_row["event"] == "read_note"
    assert tool_row["status"] == TaskStatus.COMPLETED.value
    assert tool_row["data"]["result"] == "read_note output"

    # A plain success writes nothing to memory.
    assert outcome.memory.written is False
    assert stack.memory.get_all() == []


def test_planner_receives_the_request_and_context(tmp_path):
    stack = build_stack(tmp_path, planning_with("read_note"))

    run(
        stack.pipeline.handle_request(
            "read the note",
            memory_context="known facts",
            conversation_history=[{"role": "user", "content": "hi"}],
        )
    )

    request, kwargs = stack.planner.calls[0]
    assert request == "read the note"
    assert kwargs["memory_context"] == "known facts"
    assert kwargs["conversation_history"] == [
        {"role": "user", "content": "hi"}
    ]
    # The task title/description come from the plan, as before.
    assert stack.store.list()[0].title == "Test task"


# ============================================================
# Clarification: no task, no execution
# ============================================================

def test_clarification_creates_no_task_and_runs_nothing(tmp_path):
    stack = build_stack(
        tmp_path,
        planning_with(ready=False, questions=["Which file?"]),
    )

    outcome = run(stack.pipeline.handle_request("do it"))

    assert outcome.clarification_required is True
    assert outcome.task is None
    assert outcome.task_id is None
    assert outcome.spec is None
    assert outcome.execution is None
    assert stack.store.list() == []
    assert stack.executor.calls == []

    rows = stack.audit.read(stage=AuditStage.PLANNED)
    assert len(rows) == 1
    assert rows[0]["task_id"] is None
    assert rows[0]["status"] == "clarification_required"
    assert rows[0]["data"]["questions"] == ["Which file?"]

    # Nothing downstream of planning was reached.
    assert stages(stack.audit) == [
        AuditStage.REQUEST.value,
        AuditStage.PLANNED.value,
    ]


# ============================================================
# Permissions stay authoritative
# ============================================================

def test_sensitive_step_pauses_and_never_executes(tmp_path):
    stack = build_stack(tmp_path, planning_with("send_email"))

    outcome = run(stack.pipeline.handle_request("send the email"))

    assert outcome.execution["state"].value == "PAUSED"
    assert outcome.task.status == TaskStatus.RUNNING
    assert stack.executor.calls == []

    pending = stack.approvals.list_pending()
    assert len(pending) == 1
    assert pending[0].tool_name == "send_email"

    row = stack.audit.read(task_id=outcome.task.id, stage=AuditStage.TOOL)[0]
    assert row["status"] == TaskStatus.PENDING.value

    # Waiting on a human is recorded as an approval event, and
    # the id the API hands back is the ApprovalService record.
    approval_rows = stack.audit.read(
        task_id=outcome.task.id,
        stage=AuditStage.APPROVAL,
    )
    assert [item["event"] for item in approval_rows] == ["requested"]
    assert approval_rows[0]["data"]["approval_id"] == (
        pending[0].approval_id
    )
    assert outcome.execution["approval_id"] == pending[0].approval_id


def test_approved_resume_completes_through_the_policy(tmp_path):
    stack = build_stack(tmp_path, planning_with("send_email"))
    task_id = run(stack.pipeline.handle_request("send it")).task.id

    approval_id = stack.approvals.list_pending()[0].approval_id
    stack.approvals.decide(approval_id, approved=True)

    resumed = run(stack.pipeline.resume(task_id))

    assert resumed["state"].value == "COMPLETED"
    assert stack.executor.calls == [("send_email", {"path": "x"})]
    assert stack.store.get(task_id).status == TaskStatus.COMPLETED

    # The grant was consumed by the policy, not by the pipeline.
    assert stack.approvals.get(approval_id).status == ApprovalStatus.CONSUMED

    tool_rows = stack.audit.read(task_id=task_id, stage=AuditStage.TOOL)
    assert tool_rows[-1]["status"] == TaskStatus.COMPLETED.value


def test_denied_tool_never_executes_and_is_recorded(tmp_path):
    """The core guarantee: policy denies, so nothing dispatches."""

    stack = build_stack(tmp_path, planning_with("delete_all"))

    outcome = run(stack.pipeline.handle_request("delete everything"))

    # Denied before dispatch: the tool body was never called.
    assert stack.executor.calls == []
    assert outcome.execution["state"].value == "FAILED"

    task = outcome.task
    assert task.status == TaskStatus.FAILED
    assert "DANGEROUS" in task.error

    step = stack.runner.planned_steps(task.id)[0]
    assert step.status == TaskStatus.FAILED
    assert step.error == task.error

    tool_row = stack.audit.read(task_id=task.id, stage=AuditStage.TOOL)[0]
    assert tool_row["status"] == TaskStatus.FAILED.value
    assert "DANGEROUS" in tool_row["data"]["error"]

    reflection_row = stack.audit.read(
        task_id=task.id,
        stage=AuditStage.REFLECTION,
    )[0]
    assert reflection_row["event"] == "DENIED"

    # The denial is the lesson memory keeps.
    assert outcome.memory.written is True
    memories = stack.memory.get_all()
    assert len(memories) == 1
    assert "DENIED" in memories[0]["content"]
    assert memories[0]["source"] == "reflection"

    memory_row = stack.audit.read(task_id=task.id, stage=AuditStage.MEMORY)[0]
    assert memory_row["event"] == "written"


def test_explicit_denial_terminalizes_and_audits(tmp_path):
    stack = build_stack(tmp_path, planning_with("send_email"))
    task_id = run(stack.pipeline.handle_request("send it")).task.id
    approval_id = stack.approvals.list_pending()[0].approval_id

    stack.approvals.decide(approval_id, approved=False)
    run(stack.pipeline.finalize_denied_approval(approval_id))

    assert stack.executor.calls == []
    assert stack.store.get(task_id).status == TaskStatus.FAILED

    row = stack.audit.read(task_id=task_id, stage=AuditStage.APPROVAL)[-1]
    assert row["event"] == "denied"
    assert row["status"] == TaskStatus.FAILED.value

    # The pause that produced it was recorded too.
    assert [
        row["event"]
        for row in stack.audit.read(
            task_id=task_id,
            stage=AuditStage.APPROVAL,
        )
    ] == ["requested", "denied"]

    # A denied workflow stays denied when someone asks again.
    from backend.approval.service import ApprovalDeniedError

    with pytest.raises(ApprovalDeniedError):
        run(stack.pipeline.resume(task_id))

    assert stack.executor.calls == []


def test_unregistered_tool_fails_closed_without_dispatch(tmp_path):
    planning = PlanningResult(
        request="rm the world",
        ready=True,
        task_title="Bad plan",
        task_description="Named a tool that does not exist.",
        steps=[PlannedStep(description="Wipe", tool="rm_rf")],
        source=PlanningSource.FALLBACK_MALFORMED,
    )
    stack = build_stack(tmp_path, planning)

    outcome = run(stack.pipeline.handle_request("rm the world"))

    assert stack.executor.calls == []
    assert outcome.task.status == TaskStatus.FAILED

    # The spec never pretends an unknown tool is safe.
    assert outcome.spec.risk_level == RiskLevel.DANGEROUS.value


# ============================================================
# Skill stage
# ============================================================

def test_matched_skill_runs_when_no_step_is_executable(tmp_path):
    skill_stage = FakeSkillStage(
        selection=SkillSelection(skill="task-breakdown", reason="match"),
        result=SkillResult(
            skill_name="task-breakdown",
            status=TaskStatus.COMPLETED,
            output=[{"order": 0, "title": "a"}],
        ),
    )
    stack = build_stack(
        tmp_path,
        planning_with(ready=True),
        skill_stage=skill_stage,
    )

    outcome = run(stack.pipeline.handle_request("break this down"))

    assert skill_stage.executed == [("task-breakdown", outcome.task.id)]
    assert outcome.skill_result.status == TaskStatus.COMPLETED
    # A skill run is reported through the runner envelope shape —
    # never execution:null.
    assert outcome.execution is not None
    assert outcome.execution["state"] == "COMPLETED"
    assert outcome.execution["skill"] == "task-breakdown"
    assert outcome.task.status == TaskStatus.COMPLETED
    assert outcome.spec.skill == "task-breakdown"

    executed = stack.audit.read(task_id=outcome.task.id, stage=AuditStage.SKILL)
    assert [row["event"] for row in executed] == ["selected", "executed"]
    assert executed[1]["status"] == TaskStatus.COMPLETED.value


def test_fallback_skill_completion_does_not_fabricate_success(tmp_path):
    """
    The live failure: model gateway error -> deterministic
    fallback plan -> a skill matches and "completes" on the
    advisory plan. The task must NOT read COMPLETED, and the
    execution envelope must say why.
    """

    skill_stage = FakeSkillStage(
        selection=SkillSelection(skill="task-breakdown", reason="match"),
        result=SkillResult(
            skill_name="task-breakdown",
            status=TaskStatus.COMPLETED,
            output=[{"order": 0, "title": "a"}],
        ),
    )
    stack = build_stack(
        tmp_path,
        planning_with(ready=True, source=PlanningSource.FALLBACK_ERROR),
        skill_stage=skill_stage,
    )

    outcome = run(stack.pipeline.handle_request("break this down"))

    assert outcome.skill_result.status == TaskStatus.COMPLETED

    # The skill ran, but the user's request was never executed:
    # no COMPLETED on a fallback-only plan.
    assert outcome.task.status == TaskStatus.PENDING

    assert outcome.execution is not None
    assert outcome.execution["state"] == "NO_STEPS"
    assert outcome.execution["task_status"] == TaskStatus.PENDING.value
    assert "advisory plan only" in outcome.execution["reason"]


def test_skill_failure_becomes_a_recorded_result_not_a_crash(tmp_path):
    skill_stage = FakeSkillStage(
        selection=SkillSelection(skill="task-breakdown", reason="match"),
        raises=RuntimeError("skill exploded"),
    )
    stack = build_stack(
        tmp_path,
        planning_with(ready=True),
        skill_stage=skill_stage,
    )

    outcome = run(stack.pipeline.handle_request("break this down"))

    assert outcome.skill_result.status == TaskStatus.FAILED
    assert "skill exploded" in outcome.skill_result.error
    assert outcome.task.status == TaskStatus.FAILED

    row = stack.audit.read(task_id=outcome.task.id, stage=AuditStage.SKILL)[-1]
    assert row["status"] == TaskStatus.FAILED.value
    assert "skill exploded" in row["data"]["error"]


def test_no_skill_and_no_tool_records_advisory_outcome(tmp_path):
    stack = build_stack(tmp_path, planning_with(ready=True))

    outcome = run(stack.pipeline.handle_request("just advise me"))

    # The advisory outcome is reported through the runner
    # envelope shape with an explicit non-success state: an
    # empty plan must never read as a completed execution.
    assert outcome.execution is not None
    assert outcome.execution["state"] == "NO_STEPS"
    assert outcome.execution["task_status"] == TaskStatus.PENDING.value
    assert outcome.execution["steps"] == []
    assert outcome.skill_result is None
    assert outcome.task.status == TaskStatus.PENDING

    rows = stack.audit.read(task_id=outcome.task.id, stage=AuditStage.RESULT)
    assert [row["event"] for row in rows] == [
        "nothing_executable",
        "finished",
    ]


# ============================================================
# Memory + audit are records, never gates
# ============================================================

def test_audit_failure_cannot_break_execution(tmp_path):
    """A store that cannot write must not fail the task it records."""

    stack = build_stack(tmp_path, planning_with("read_note"))

    broken = AgentPipeline(
        planner=stack.planner,
        task_service=stack.store,
        task_runner=stack.runner,
        tool_registry=stack.registry,
        memory_bridge=MemoryBridge(
            memory=stack.memory,
            # Raises on every append, beyond AuditLog's own
            # fail-safe contract.
            audit=BrokenFileStore(),
        ),
        # A directory as the target: every write fails.
        audit=AuditLog(storage_path=str(tmp_path)),
    )

    outcome = run(broken.handle_request("read the note"))

    assert outcome.task.status == TaskStatus.COMPLETED
    assert stack.executor.calls == [("read_note", {"path": "x"})]
    assert outcome.memory.written is False


def test_memory_bridge_receives_only_the_reflection(tmp_path):
    stack = build_stack(tmp_path, planning_with("delete_all"))

    outcome = run(stack.pipeline.handle_request("delete everything"))

    assert outcome.memory.written is True

    written = stack.memory.get_all()[0]

    # The stored text is the bridge's reflection-derived report.
    assert written["content"] == outcome.memory.content
    assert "DENIED" in written["content"]

    # Built from reflection fields only: no tool params are
    # copied into a memory that is replayed into later prompts.
    assert "path" not in json.dumps(written)

    assert written["metadata"]["task_id"] == outcome.task.id
    assert written["importance"] == pytest.approx(0.6)


def test_no_memory_bridge_is_still_a_valid_pipeline(tmp_path):
    stack = build_stack(tmp_path, planning_with("delete_all"))

    bare = AgentPipeline(
        planner=stack.planner,
        task_service=stack.store,
        task_runner=stack.runner,
        tool_registry=stack.registry,
    )

    outcome = run(bare.handle_request("delete everything"))

    assert outcome.task.status == TaskStatus.FAILED
    assert outcome.memory is None


# ============================================================
# Async contract: no nested loops, no thread bridge
# ============================================================

def test_pipeline_awaitable_surface_is_coroutines():
    assert inspect.iscoroutinefunction(AgentPipeline.handle_request)
    assert inspect.iscoroutinefunction(AgentPipeline.resume)
    assert inspect.iscoroutinefunction(AgentPipeline.finalize_denied_approval)


def test_pipeline_source_imports_no_loop_or_thread_bridges():
    """The pipeline cannot block: it never even imports the
    modules that would let it start a loop or a thread."""

    tree = ast.parse(inspect.getsource(pipeline_module))

    imported = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    for forbidden in (
        "asyncio",
        "threading",
        "concurrent.futures",
        "multiprocessing",
    ):
        assert forbidden not in imported, forbidden


def test_model_step_awaits_the_gateway_inside_the_callers_loop(
    tmp_path, monkeypatch
):
    """The live request path can run a model tool: it is awaited
    in the caller's loop, so nothing blocks and no loop is nested."""

    from backend.tools.builtin import model as model_tool

    class FakeGateway:
        def __init__(self):
            self.calls = []
            self.loop = None
            self.thread = None

        async def generate(self, **kwargs):
            self.calls.append(kwargs)
            self.loop = asyncio.get_running_loop()
            self.thread = threading.current_thread()
            return "model answered"

    gateway = FakeGateway()

    # The tool's own gateway reference: no real provider is
    # contacted, and no key is ever read here.
    monkeypatch.setattr(model_tool, "orchestrator", gateway)

    planning = PlanningResult(
        request="summarise this with the model",
        ready=True,
        task_title="Model task",
        task_description="Ask the model once.",
        steps=[
            PlannedStep(
                description="Ask the model",
                tool="model_generate",
                params={"prompt": "Summarise this with the model"},
            )
        ],
        source=PlanningSource.DETERMINISTIC,
    )

    registry = ToolRegistry()
    registry.register("model_generate", "Ask the model", "model", RiskLevel.SAFE)
    approvals = ApprovalService()
    agent = Agent(
        name="model-agent",
        registry=registry,
        permission_policy=PermissionPolicy(
            registry,
            approval_grants=approvals,
        ),
        tool_executor=(
            lambda name, params: model_tool.model_generate(params)
        ),
    )
    store = TaskService()
    runner = TaskRunner(
        task_service=store,
        automation_engine=AutomationEngine(agent),
        approvals=approvals,
        reflection_engine=ReflectionEngine(),
    )
    pipeline = AgentPipeline(
        planner=FakePlanner(planning),
        task_service=store,
        task_runner=runner,
        tool_registry=registry,
        audit=AuditLog(storage_path=str(tmp_path / "audit.jsonl")),
    )

    async def caller():
        loop = asyncio.get_running_loop()
        outcome = await pipeline.handle_request(
            "summarise this with the model"
        )
        return loop, outcome

    outer_loop, outcome = run(caller())

    # Same loop, same thread: the gateway call was awaited, not
    # bridged into a fresh event loop or a worker thread.
    assert gateway.loop is outer_loop
    assert gateway.thread is threading.current_thread()
    assert gateway.calls == [
        {
            "message": "Summarise this with the model",
            "provider_name": None,
            "model": None,
        }
    ]
    assert outcome.task.status == TaskStatus.COMPLETED
    assert "model answered" in str(outcome.task.result)


# ============================================================
# SPEC -> TaskSteps
# ============================================================

def test_task_steps_from_spec_preserves_plan_and_isolates_params():
    from backend.core.execution_spec import build_execution_spec

    registry = ToolRegistry()
    registry.register("read_note", "Read", "memory", RiskLevel.SAFE)

    planning = PlanningResult(
        request="multi step",
        ready=True,
        task_title="t",
        task_description="d",
        steps=[
            PlannedStep(description="advice only", tool=None),
            PlannedStep(description="First", tool="read_note", params={"a": 1}),
            PlannedStep(description="Second", tool="read_note", params={"b": 2}),
        ],
        source=PlanningSource.DETERMINISTIC,
    )
    spec = build_execution_spec(
        planning,
        task_id="task-1",
        registry=registry,
    )

    # Advisory steps are dropped and order is renumbered.
    assert [step.tool for step in spec.steps] == ["read_note", "read_note"]
    assert [step.order for step in spec.steps] == [0, 1]

    steps = task_steps_from_spec(spec)
    assert [step.order for step in steps] == [0, 1]
    assert [step.title for step in steps] == ["First", "Second"]
    assert steps[0].status == TaskStatus.PENDING

    # Mutating a step's params cannot reach back into the spec.
    steps[0].params["a"] = "changed"
    assert spec.steps[0].params == {"a": 1}


# ============================================================
# Failure inside planning is audited, then re-raised
# ============================================================

def test_planner_error_is_recorded_and_propagates(tmp_path):
    class ExplodingPlanner:
        async def plan(self, request, **kwargs):
            raise RuntimeError("gateway down")

    audit = AuditLog(storage_path=str(tmp_path / "audit.jsonl"))
    pipeline = AgentPipeline(
        planner=ExplodingPlanner(),
        task_service=TaskService(),
        task_runner=None,
        tool_registry=ToolRegistry(),
        audit=audit,
    )

    with pytest.raises(RuntimeError):
        run(pipeline.handle_request("anything"))

    rows = audit.read(stage=AuditStage.ERROR)
    assert rows[0]["event"] == "request_failed"
    assert "gateway down" in rows[0]["data"]["detail"]
    assert rows[0]["task_id"] is None


def test_resume_refusal_is_recorded_and_propagates(tmp_path):
    stack = build_stack(tmp_path, planning_with("read_note"))

    with pytest.raises(KeyError):
        run(stack.pipeline.resume("no-such-task"))

    row = stack.audit.read(stage=AuditStage.ERROR)[0]
    assert row["event"] == "resume_refused"
    assert row["task_id"] == "no-such-task"


# ============================================================
# API delegation: the pipeline is what the endpoints call
# ============================================================

def test_task_api_delegates_to_the_pipeline():
    import backend.api.tasks as tasks_api

    source = inspect.getsource(tasks_api)

    # The HTTP layer no longer translates plans by hand.
    assert "TaskStep(" not in source
    assert "task_runner.start(" not in source
    assert "task_runner.resume(" not in source
    assert "await build_pipeline().handle_request(" in source

    # And it composes the shared singletons, not its own copies.
    pipeline = tasks_api.build_pipeline()
    assert pipeline._planner is tasks_api.planner
    assert pipeline._tasks is tasks_api.task_service
    assert pipeline._runner is tasks_api.task_runner
    assert pipeline._registry is tasks_api.tool_registry
    assert pipeline._skills is tasks_api.skill_stage
    assert pipeline._memory is tasks_api.memory_bridge
    assert pipeline._audit_log is tasks_api.audit_log


def test_task_api_imports_no_loop_or_thread_bridges():
    """Request handling stays on the server's own event loop."""

    import backend.api.tasks as tasks_api

    tree = ast.parse(inspect.getsource(tasks_api))

    imported = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    for forbidden in ("asyncio", "threading", "concurrent.futures"):
        assert forbidden not in imported, forbidden


def test_pipeline_components_are_the_shared_singletons():
    """One store/registry per process: the API adds none."""

    import backend.api.tasks as tasks_api
    from backend.core import agent_services

    assert tasks_api.planner is agent_services.planner
    assert tasks_api.task_service is agent_services.task_service
    assert tasks_api.task_runner is agent_services.task_runner
    assert tasks_api.tool_registry is agent_services.tool_registry
    assert tasks_api.skill_stage is agent_services.skill_stage
    assert tasks_api.audit_log is agent_services.audit_log
    assert tasks_api.memory_bridge is agent_services.memory_bridge

    # api/graph.py reads its task store from the tasks module.
    from backend.api import graph

    assert graph.task_service is agent_services.task_service
