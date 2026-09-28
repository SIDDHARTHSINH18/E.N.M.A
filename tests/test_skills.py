import pytest

from backend.agents.executor import Agent
from backend.automation.engine import AutomationEngine
from backend.core.task import Task, TaskStatus
from backend.permissions.policy import PermissionPolicy
from backend.skills.builtin import (
    BUILTIN_SKILLS,
    register_builtin_skills,
)
from backend.skills.builtin.permission_explain import PermissionExplainSkill
from backend.skills.builtin.skill_catalog import SkillCatalogSkill
from backend.skills.loader import SkillLoader
from backend.skills.metadata import SkillMetadata
from backend.skills.registry import SkillRegistry
from backend.skills.router import SkillRouter
from backend.skills.runner import SkillRunner
from backend.skills.skill import SkillResult
from backend.tools.registry import RiskLevel, ToolRegistry


# ============================================================
# Deterministic mock tool execution (testing only).
# ============================================================

class MockToolExecutor:
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


def make_stack(risk_overrides=None):
    """
    Full stack: ToolRegistry + PermissionPolicy + Agent +
    SkillRegistry (builtins) + SkillLoader + SkillRouter +
    SkillRunner. risk_overrides optionally registers a
    tool at a different risk level.
    """
    risk_overrides = risk_overrides or {}
    executor = MockToolExecutor()

    registry = ToolRegistry()
    registry.register(
        "memory_search",
        "Search memory",
        "memory",
        risk_overrides.get("memory_search", RiskLevel.SAFE),
    )
    registry.register(
        "summarize",
        "Summarize text",
        "docs",
        risk_overrides.get("summarize", RiskLevel.SAFE),
    )

    policy = PermissionPolicy(registry)

    agent = Agent(
        name="skill-agent",
        registry=registry,
        permission_policy=policy,
        tool_executor=executor,
    )

    skills = SkillRegistry()
    register_builtin_skills(skills)

    loader = SkillLoader(skills)
    router = SkillRouter(skills)
    runner = SkillRunner(
        agent=agent,
        skill_registry=skills,
        loader=loader,
        permission_policy=policy,
    )
    return executor, skills, loader, router, runner, policy, agent


# ============================================================
# SkillMetadata
# ============================================================

def test_metadata_creation():
    metadata = SkillMetadata(
        name="demo",
        description="A demo skill",
        category="testing",
        required_tools=["some_tool"],
        risk_level=RiskLevel.SENSITIVE,
        entrypoint="backend.skills.builtin.demo:DemoSkill",
    )
    assert metadata.name == "demo"
    assert metadata.category == "testing"
    assert metadata.version == "1.0"
    assert metadata.required_tools == ["some_tool"]
    assert metadata.risk_level == RiskLevel.SENSITIVE
    assert metadata.entrypoint.endswith(":DemoSkill")


# ============================================================
# SkillRegistry
# ============================================================

def test_registry_registration_and_retrieval():
    registry = SkillRegistry()
    metadata = SkillMetadata(name="demo", description="d", category="c")
    registry.register_metadata(metadata)

    assert registry.get_metadata("demo") is metadata
    assert registry.count() == 1
    assert registry.list_metadata() == [metadata]


def test_registry_duplicate_registration_fails():
    registry = SkillRegistry()
    registry.register_metadata(SkillMetadata(name="demo", description="d", category="c"))

    with pytest.raises(ValueError, match="already registered"):
        registry.register_metadata(
            SkillMetadata(name="demo", description="other", category="c")
        )


def test_registry_unknown_skill_fails_clearly():
    registry = SkillRegistry()
    with pytest.raises(KeyError, match="not found"):
        registry.get_metadata("nonexistent")


def test_registry_rejects_empty_name():
    registry = SkillRegistry()
    with pytest.raises(ValueError):
        registry.register_metadata(SkillMetadata(name="", description="d", category="c"))


# ============================================================
# SkillLoader — lazy loading and caching
# ============================================================

def test_loader_loads_lazily_and_caches():
    _, skills, loader, _, _, _, _ = make_stack()

    assert not loader.is_loaded("memory-recall")

    first = loader.load("memory-recall")
    assert loader.is_loaded("memory-recall")

    second = loader.load("memory-recall")
    assert first is second  # cached instance, not a new import

    # Loading one skill did not load the others.
    assert not loader.is_loaded("note-summarizer")


def test_loader_unknown_skill_fails():
    _, skills, loader, _, _, _, _ = make_stack()
    with pytest.raises(KeyError, match="not found"):
        loader.load("nonexistent")


def test_loader_loads_all_builtins():
    _, skills, loader, _, _, _, _ = make_stack()
    for metadata in skills.list_metadata():
        skill = loader.load(metadata.name)
        assert skill.metadata.name == metadata.name


# ============================================================
# SkillRouter — deterministic v1 routing
# ============================================================

def test_router_ranks_by_name_and_description():
    _, skills, _, router, _, _, _ = make_stack()

    results = router.select("summarize my note")

    assert results, "expected at least one match"
    assert results[0].name == "note-summarizer"


def test_router_respects_limit():
    _, skills, _, router, _, _, _ = make_stack()
    assert len(router.select("skill", limit=2)) <= 2


def test_router_no_match_returns_empty():
    _, skills, _, router, _, _, _ = make_stack()
    assert router.select("zx qqqv totally-unrelated-words") == []


def test_router_empty_requirement_returns_empty():
    _, skills, _, router, _, _, _ = make_stack()
    assert router.select("") == []
    assert router.select("   ") == []


def test_router_is_deterministic():
    _, skills, _, router, _, _, _ = make_stack()
    first = router.select("recall memory")
    second = router.select("recall memory")
    assert [m.name for m in first] == [m.name for m in second]


# ============================================================
# SkillResult
# ============================================================

def test_skill_result_defaults():
    result = SkillResult(skill_name="demo", status=TaskStatus.COMPLETED)
    assert result.output is None
    assert result.error is None
    assert result.steps is None
    assert result.status == TaskStatus.COMPLETED


def test_skill_result_reuses_task_status():
    result = SkillResult(skill_name="demo", status=TaskStatus.FAILED, error="x")
    assert result.status == TaskStatus.FAILED
    assert result.error == "x"


# ============================================================
# Builtin skills
# ============================================================

def test_builtin_discovery_registers_seven_skills():
    registry = SkillRegistry()
    register_builtin_skills(registry)

    names = {m.name for m in registry.list_metadata()}
    assert names == {
        "memory-recall",
        "note-summarizer",
        "document-creation",
        "research",
        "task-breakdown",
        "skill-catalog",
        "permission-explain",
    }
    assert registry.count() == len(BUILTIN_SKILLS) == 7


def test_builtin_entrypoints_are_importable():
    _, skills, loader, _, _, _, _ = make_stack()
    for metadata in skills.list_metadata():
        module_path, sep, class_name = metadata.entrypoint.partition(":")
        assert sep and module_path.startswith("backend.skills.builtin.")
        # Loading proves the module imports and the class exists.
        assert loader.load(metadata.name) is not None


def test_task_breakdown_is_tool_free_and_deterministic():
    _, skills, loader, _, _, _, _ = make_stack()
    skill = loader.load("task-breakdown")

    task = Task(title="Plan", description="d")
    result = skill.run(
        task,
        agent=None,
        params={"goal": "write spec; review spec; ship spec"},
    )

    assert result.status == TaskStatus.COMPLETED
    assert [item["title"] for item in result.output] == [
        "write spec",
        "review spec",
        "ship spec",
    ]
    assert [item["order"] for item in result.output] == [0, 1, 2]


def test_task_breakdown_requires_goal():
    _, skills, loader, _, _, _, _ = make_stack()
    result = loader.load("task-breakdown").run(Task(title="t", description="d"), None, {})
    assert result.status == TaskStatus.FAILED
    assert "goal" in result.error


def test_skill_catalog_lists_registry_without_loading():
    _, skills, _, _, _, _, _ = make_stack()

    result = SkillCatalogSkill().run(
        Task(title="t", description="d"),
        None,
        {"skill_registry": skills},
    )

    assert result.status == TaskStatus.COMPLETED
    names = [item["name"] for item in result.output]
    assert "memory-recall" in names and "permission-explain" in names


def test_skill_catalog_requires_registry():
    result = SkillCatalogSkill().run(Task(title="t", description="d"), None, {})
    assert result.status == TaskStatus.FAILED
    assert "skill_registry" in result.error


def test_permission_explain_reports_decision():
    _, skills, loader, _, _, policy, _ = make_stack()
    skill = loader.load("permission-explain")

    result = skill.run(
        Task(title="t", description="d"),
        None,
        {"permission_policy": policy, "tool_name": "summarize"},
    )

    assert result.status == TaskStatus.COMPLETED
    assert result.output["tool"] == "summarize"
    assert result.output["decision"] == "ALLOW"
    assert "SAFE" in result.output["reason"]


def test_permission_explain_unknown_tool_denied():
    _, skills, loader, _, _, policy, _ = make_stack()
    result = PermissionExplainSkill().run(
        Task(title="t", description="d"),
        None,
        {"permission_policy": policy, "tool_name": "nonexistent"},
    )
    assert result.output["decision"] == "DENY"
    assert "Unknown tool" in result.output["reason"]


# ============================================================
# SkillRunner integration — existing pipeline not bypassed
# ============================================================

def test_runner_executes_safe_skill_end_to_end():
    executor, skills, loader, _, runner, _, _ = make_stack()
    executor.set("memory_search", ["fact one", "fact two"])

    task = Task(title="Recall", description="d")
    result = runner.execute("memory-recall", task, {"query": "project facts"})

    assert result.status == TaskStatus.COMPLETED
    assert result.output == ["fact one", "fact two"]
    # The step went through the Agent (permission path), and
    # the task reflects the outcome.
    assert executor.calls == [("memory_search", {"query": "project facts"})]
    assert task.status == TaskStatus.COMPLETED


def test_runner_summarizer_skill():
    # note-summarizer is a coroutine skill: the summarize tool
    # is async (model gateway), so it must run through
    # execute_async / run_async.
    import asyncio

    executor, skills, loader, _, runner, _, _ = make_stack()
    executor.set("summarize", "short summary")

    result = asyncio.run(
        runner.execute_async(
            "note-summarizer",
            Task(title="s", description="d"),
            {"text": "long text"},
        )
    )

    assert result.status == TaskStatus.COMPLETED
    assert result.output == "short summary"


def test_runner_sensitive_tool_pauses_not_executes():
    executor, skills, loader, _, runner, _, _ = make_stack(
        risk_overrides={"summarize": RiskLevel.SENSITIVE}
    )

    import asyncio

    result = asyncio.run(
        runner.execute_async(
            "note-summarizer",
            Task(title="s", description="d"),
            {"text": "long text"},
        )
    )

    # Paused for approval: nothing executed, task not failed.
    assert result.status == TaskStatus.PENDING
    assert result.error is None
    assert executor.calls == []


def test_runner_unknown_tool_fails_without_execution():
    # A stack whose ToolRegistry deliberately does NOT
    # register "memory_search", so the skill's step hits
    # the unknown-tool deny path.
    executor = MockToolExecutor()
    empty_registry = ToolRegistry()
    policy = PermissionPolicy(empty_registry)
    agent = Agent(
        name="skill-agent",
        registry=empty_registry,
        permission_policy=policy,
        tool_executor=executor,
    )
    skills = SkillRegistry()
    register_builtin_skills(skills)
    runner = SkillRunner(
        agent=agent,
        skill_registry=skills,
        loader=SkillLoader(skills),
        permission_policy=policy,
    )

    task = Task(title="t", description="d")
    result = runner.execute("memory-recall", task, {"query": "q"})

    assert result.status == TaskStatus.FAILED
    assert "Unknown tool" in result.error
    assert executor.calls == []
    assert task.status == TaskStatus.FAILED


def test_runner_unknown_skill_fails_before_anything_runs():
    executor, skills, loader, _, runner, _, _ = make_stack()
    with pytest.raises(KeyError, match="not found"):
        runner.execute("nonexistent", Task(title="t", description="d"), {})
    assert executor.calls == []


def test_runner_uses_provided_automation_engine():
    """The runner executes skills through the shared
    AutomationEngine wired to the same Agent, so permission
    checks stay in the existing path."""
    executor, skills, loader, _, runner, policy, agent = make_stack()
    engine = AutomationEngine(agent)
    runner2 = SkillRunner(
        agent=agent,
        skill_registry=skills,
        loader=loader,
        permission_policy=policy,
        automation_engine=engine,
    )
    executor.set("memory_search", "ok")

    result = runner2.execute(
        "memory-recall",
        Task(title="t", description="d"),
        {"query": "q"},
    )

    assert result.status == TaskStatus.COMPLETED
    assert executor.calls == [("memory_search", {"query": "q"})]
