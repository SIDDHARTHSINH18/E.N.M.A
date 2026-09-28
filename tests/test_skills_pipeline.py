"""
M4 step 5 — skills wired into the task pipeline.

The skill system already existed (registry / router / loader /
runner); nothing imported it. These tests pin the new seam:

- routing happens exactly once per request, and its ranked
  candidates survive into the selection for audit
- "no skill matched" stays a normal, non-raising outcome
- a matched skill that needs an unregistered tool is not run
- execution delegates to the existing SkillRunner (and through
  it to Agent -> PermissionPolicy), never to a tool directly
- routing never imports a skill implementation
- the production stage is built from the existing singletons,
  so no second skill system exists
- GET /api/skills exposes metadata only, behind the session
  middleware
"""

import pytest

import backend.core.agent_services as agent_services

from backend.core.task import Task, TaskStatus
from backend.skills.loader import SkillLoader
from backend.skills.metadata import SkillMetadata
from backend.skills.registry import SkillRegistry
from backend.skills.router import SkillRouter
from backend.skills.stage import SkillSelection, SkillStage
from backend.skills.skill import SkillResult
from backend.tools.registry import RiskLevel, ToolRegistry


# ============================================================
# LOCAL STACK (deterministic routing, spy runner)
# ============================================================

class CountingRouter(SkillRouter):
    def __init__(self, registry):
        super().__init__(registry)
        self.calls = []

    def select(self, requirement, limit=None):
        self.calls.append((requirement, limit))
        return super().select(requirement, limit)


class SpyRunner:
    """Stands in for SkillRunner to record delegation."""

    def __init__(self):
        self.calls = []

    def execute(self, skill_name, task, params):
        self.calls.append((skill_name, task, params))
        return SkillResult(
            skill_name=skill_name,
            status=TaskStatus.COMPLETED,
            output="delegated",
        )


def make_stage(tools=("fs_read_file",)):
    registry = SkillRegistry()

    registry.register_metadata(
        SkillMetadata(
            name="plain-reader",
            description="Read a note and list its lines",
            category="documents",
            required_tools=["fs_read_file"],
            risk_level=RiskLevel.SAFE,
            entrypoint="backend.skills.builtin.note_summarizer:X",
        )
    )
    registry.register_metadata(
        SkillMetadata(
            name="memory-helper",
            description="Recall stored memory about a topic",
            category="memory",
            required_tools=["memory_search"],
            risk_level=RiskLevel.SENSITIVE,
            entrypoint="backend.skills.builtin.memory_recall:X",
        )
    )
    registry.register_metadata(
        SkillMetadata(
            name="no-tools-needed",
            description="Explain the permission model",
            category="system",
            required_tools=[],
            risk_level=RiskLevel.SAFE,
            entrypoint="backend.skills.builtin.permission_explain:X",
        )
    )

    tool_registry = ToolRegistry()

    for name in tools:
        tool_registry.register(
            name=name,
            description="stub",
            category="filesystem",
            risk_level=RiskLevel.SAFE,
        )

    router = CountingRouter(registry)
    runner = SpyRunner()
    loader = SkillLoader(registry)

    stage = SkillStage(
        registry=registry,
        router=router,
        runner=runner,
        tool_registry=tool_registry,
    )

    return stage, router, runner, loader


def make_task(description="do the work"):
    return Task(title="skill task", description=description)


# ============================================================
# ROUTE ONCE
# ============================================================

def test_router_is_consulted_exactly_once():
    stage, router, _, _ = make_stage()

    stage.select("recall my notes about memory")

    assert len(router.calls) == 1
    assert router.calls[0][0] == "recall my notes about memory"


def test_ranked_candidates_are_carried_into_the_selection():
    stage, _, _, _ = make_stage()

    selection = stage.select("recall memory")

    assert selection.skill is None
    assert "memory-helper" in selection.candidates
    assert selection.unavailable == ("memory-helper",)


# ============================================================
# NO MATCH IS A SAFE OUTCOME
# ============================================================

def test_unrelated_requirement_matches_nothing():
    stage, _, runner, _ = make_stage()

    selection = stage.select("xylophone zebra quantum")

    assert selection.matched is False
    assert selection.skill is None
    assert selection.reason == "no skill matched the requirement"
    assert runner.calls == []


def test_no_match_still_serialises_for_the_audit_log():
    selection = SkillSelection()

    assert selection.to_dict() == {
        "skill": None,
        "matched": False,
        "risk_level": None,
        "reason": "",
        "candidates": [],
        "unavailable": [],
    }


def test_executing_a_non_match_is_a_programmer_error():
    stage, _, runner, _ = make_stage()

    with pytest.raises(ValueError, match="matched nothing"):
        stage.execute(SkillSelection(), make_task())

    assert runner.calls == []


# ============================================================
# RUNNABLE BEFORE RUN
# ============================================================

def test_a_skill_needing_unregistered_tools_is_not_selected():
    stage, _, _, _ = make_stage(tools=())

    selection = stage.select("read a note and list its lines")

    assert selection.skill is None
    assert "plain-reader" in selection.candidates
    assert "fs_read_file" in selection.reason


def test_a_skill_with_registered_tools_is_selected():
    stage, _, _, _ = make_stage(tools=("fs_read_file",))

    selection = stage.select("read a note and list its lines")

    assert selection.skill == "plain-reader"
    assert selection.risk_level == RiskLevel.SAFE.value
    assert selection.matched is True


def test_tool_availability_is_reported_per_skill():
    stage, _, _, _ = make_stage(tools=("fs_read_file",))

    assert stage.missing_tools("plain-reader") == []
    assert stage.missing_tools("memory-helper") == ["memory_search"]


def test_selection_falls_through_to_the_next_runnable_match():
    stage, _, _, _ = make_stage(tools=())

    # Both skills rank for this wording; memory-helper and
    # plain-reader cannot run, the tool-less one can.
    selection = stage.select(
        "read a note, list its lines, explain the permission model"
    )

    assert selection.skill == "no-tools-needed"
    assert set(selection.unavailable) == {"plain-reader"}


# ============================================================
# DELEGATION (no second execution path)
# ============================================================

def test_execution_delegates_to_the_shared_runner():
    stage, _, runner, _ = make_stage(tools=("fs_read_file",))

    selection = stage.select("read a note")
    task = make_task("read the note")

    result = stage.execute(selection, task)

    assert result.status is TaskStatus.COMPLETED
    assert runner.calls[0][0] == "plain-reader"
    assert runner.calls[0][1] is task


def test_requirement_is_offered_under_each_input_key():
    stage, _, runner, _ = make_stage(tools=("fs_read_file",))

    selection = stage.select("read a note")

    stage.execute(selection, make_task("summarise the meeting"))

    params = runner.calls[0][2]

    assert params["goal"] == "summarise the meeting"
    assert params["text"] == "summarise the meeting"
    assert params["query"] == "summarise the meeting"


def test_explicit_params_win_over_the_requirement():
    stage, _, runner, _ = make_stage(tools=("fs_read_file",))

    selection = stage.select("read a note")

    stage.execute(
        selection,
        make_task("the requirement"),
        {"text": "the document body"},
    )

    params = runner.calls[0][2]

    assert params["text"] == "the document body"
    assert params["goal"] == "the requirement"


# ============================================================
# LAZY LOADING PRESERVED
# ============================================================

def test_routing_never_imports_a_skill_implementation():
    stage, _, _, loader = make_stage(tools=("fs_read_file",))

    stage.select("read a note and list its lines")
    stage.select("recall memory")
    stage.select("xylophone")

    assert loader.is_loaded("plain-reader") is False
    assert loader.is_loaded("memory-helper") is False


# ============================================================
# PRODUCTION WIRING (singletons, real skill)
# ============================================================

def test_the_pipeline_stage_reuses_the_existing_skill_objects():
    stage = agent_services.skill_stage

    assert stage._registry is agent_services.skill_registry
    assert stage._router is agent_services.skill_router
    assert stage._runner is agent_services.skill_runner
    assert stage._tools is agent_services.tool_registry


def test_production_routing_selects_a_real_runnable_skill():
    selection = agent_services.skill_stage.select(
        "break this task down into subtasks"
    )

    assert selection.skill == "task-breakdown"
    assert selection.risk_level == RiskLevel.SAFE.value


def test_production_selection_feeds_the_execution_spec():
    from backend.core.execution_spec import build_execution_spec
    from backend.core.planner import PlanningResult, PlanningSource

    selection = agent_services.skill_stage.select(
        "break this task down into subtasks"
    )

    planning = PlanningResult(
        request="break this task down into subtasks",
        ready=True,
        task_title="Plan",
        task_description="Plan the work",
        steps=[],
        assumptions=[],
        source=PlanningSource.DETERMINISTIC,
        planned_at="2026-01-01T00:00:00",
    )

    spec = build_execution_spec(
        planning,
        task_id="spec-with-skill",
        registry=agent_services.tool_registry,
        skill_name=selection.skill,
        skill_risk_level=selection.risk_level,
    )

    assert spec.skill == "task-breakdown"
    assert spec.skill_risk_level == "SAFE"

    # A skill-only spec has no tool steps: the pipeline runs the
    # skill instead of the automation engine.
    assert spec.has_executable_steps is False


def test_production_skill_execution_goes_through_the_real_runner():
    selection = agent_services.skill_stage.select(
        "break this task down into subtasks"
    )

    task = make_task("read the file; summarise it; save the note")

    result = agent_services.skill_stage.execute(selection, task)

    assert result.skill_name == "task-breakdown"
    assert result.status is TaskStatus.COMPLETED
    assert [item["title"] for item in result.output] == [
        "read the file",
        "summarise it",
        "save the note",
    ]


# ============================================================
# CATALOG ENDPOINT (metadata only)
# ============================================================

def login(client):
    import os

    response = client.post(
        "/api/auth/login",
        json={"password": os.environ["GHOST_AUTH_PASSWORD"]},
    )

    assert response.status_code == 200

    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_skills_endpoint_requires_a_session():
    from fastapi.testclient import TestClient

    from backend.main import app

    response = TestClient(app).get("/api/skills")

    assert response.status_code == 401


def test_skills_endpoint_lists_metadata_only():
    from fastapi.testclient import TestClient

    from backend.main import app

    client = TestClient(app)
    headers = login(client)

    response = client.get("/api/skills", headers=headers)

    assert response.status_code == 200

    body = response.json()

    assert body["total"] == agent_services.skill_registry.count()
    assert [skill["name"] for skill in body["skills"]] == sorted(
        skill["name"] for skill in body["skills"]
    )

    for skill in body["skills"]:
        # No entrypoint: the implementation path is never
        # published, and listing cannot load skill code.
        assert "entrypoint" not in skill
        assert skill["risk_level"] in {"SAFE", "SENSITIVE", "DANGEROUS"}
        assert skill["available"] is (not skill["missing_tools"])


def test_skills_endpoint_reports_unavailable_skills_honestly():
    from fastapi.testclient import TestClient

    from backend.main import app

    client = TestClient(app)

    skills = client.get(
        "/api/skills",
        headers=login(client),
    ).json()["skills"]

    by_name = {skill["name"]: skill for skill in skills}

    # memory_search is a real registered tool now (M4), so
    # memory-recall reports itself honestly as runnable.
    assert by_name["memory-recall"]["available"] is True
    assert by_name["memory-recall"]["missing_tools"] == []

    assert by_name["task-breakdown"]["available"] is True
