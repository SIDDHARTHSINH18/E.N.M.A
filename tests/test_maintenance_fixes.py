"""
Maintenance-pass regression tests (2026-09).

Covers the fixes verified in this pass:
- obvious filesystem requests plan deterministically (no
  clarification round for a read of a nonexistent file);
- local date/time questions are detected and answered from the
  runtime clock (never web search);
- weather questions extract a location and build a grounded
  provenance context;
- current-events context carries actually retrieved page content;
- a skill "completion" with no steps and no output is treated as
  NO_STEPS, never as executed work.
"""

from datetime import datetime

import pytest

from backend.api.chat import (
    build_current_events_context,
    build_weather_context,
    detect_local_datetime_request,
    extract_weather_location,
    format_local_datetime,
)
from backend.core.planner import Planner


@pytest.fixture
def planner():
    return Planner(None)


def test_read_of_nonexistent_file_plans_directly(planner):
    import asyncio

    planned = asyncio.run(
        planner.plan(
            "Create a task that attempts to read a file named "
            "this_file_definitely_does_not_exist_98765.txt, "
            "then execute it."
        )
    )

    assert planned.ready is True
    assert planned.questions == []
    assert planned.steps[0].tool == "fs_read_file"
    assert (
        planned.steps[0].params["path"]
        == "this_file_definitely_does_not_exist_98765.txt"
    )


def test_direct_read_without_the_word_file_plans_directly(planner):
    """The exact manual-test wording: 'Read <name>.txt'."""

    import asyncio

    planned = asyncio.run(
        planner.plan(
            "Read this_file_definitely_does_not_exist_98765.txt"
        )
    )

    assert planned.ready is True
    assert planned.questions == []
    assert planned.steps[0].tool == "fs_read_file"
    assert (
        planned.steps[0].params["path"]
        == "this_file_definitely_does_not_exist_98765.txt"
    )


def test_write_file_request_plans_write_and_verify(planner):
    import asyncio

    planned = asyncio.run(
        planner.plan(
            "Create a task that writes a file named "
            "enma_task_test.txt containing exactly "
            "TASK EXECUTION VERIFIED, then execute the task."
        )
    )

    assert planned.ready is True
    tools = [step.tool for step in planned.steps]
    assert tools == ["fs_write_file", "fs_read_file"]
    assert planned.steps[0].params["path"] == "enma_task_test.txt"
    assert planned.steps[0].params["content"] == "TASK EXECUTION VERIFIED"


def test_non_file_request_is_not_captured_by_fs_shortcut(planner):
    import asyncio

    planned = asyncio.run(planner.plan("organize my project notes"))

    # No model gateway: deterministic plan, but not a filesystem one.
    assert planned.ready is True
    assert all(step.tool != "fs_read_file" for step in planned.steps)


def test_local_datetime_detection():
    assert detect_local_datetime_request(
        "What is the current date and time on this computer?"
    )
    assert detect_local_datetime_request("what time is it")
    assert not detect_local_datetime_request("what time is it in Tokyo")
    assert not detect_local_datetime_request("latest AI news")


def test_format_local_datetime_reports_timezone():
    text = format_local_datetime()

    now = datetime.now().astimezone()
    assert now.strftime("%d %B %Y") in text
    assert "Timezone:" in text
    assert "UTC" in text


def test_weather_location_extraction():
    assert (
        extract_weather_location(
            "whats the current weather of ganfhinaar"
        )
        == "ganfhinaar"
    )
    assert (
        extract_weather_location("What is the weather in Gandhinagar?")
        == "Gandhinagar"
    )
    assert extract_weather_location("tell me about weather") is None


def test_weather_context_carries_provenance():
    context = build_weather_context(
        "weather in Gandhinagar?",
        {
            "location": "Gandhinagar",
            "temperature_c": 31.2,
            "source": "open-meteo.com current-conditions API",
            "observed_at": "2026-09-30T16:30",
        },
    )

    assert "open-meteo.com" in context
    assert "31.2" in context


def test_current_events_context_includes_retrieved_pages():
    search_result = {
        "query": "latest AI news",
        "provider": "tavily",
        "results": [
            {
                "title": "AI weekly",
                "url": "https://example.com/ai",
                "domain": "example.com",
                "snippet": "snippet text",
            }
        ],
    }

    context = build_current_events_context(
        "latest AI news",
        search_result,
        retrieved_pages=[
            {
                "url": "https://example.com/ai",
                "title": "AI weekly",
                "text": "actual page content about AI",
                "retrieved_at": "2026-09-30T00:00:00+00:00",
            }
        ],
    )

    assert "actual page content about AI" in context
    assert "fetched in full" in context


def test_current_events_context_without_pages_is_honest():
    context = build_current_events_context(
        "latest AI news",
        {
            "query": "latest AI news",
            "provider": "duckduckgo",
            "results": [],
        },
    )

    assert "no page could be fetched in full" in context


def test_unproven_skill_completion_is_not_executed():
    """A skill COMPLETED with no steps and no output stays PENDING."""

    from backend.agents.pipeline import AgentPipeline, PipelineOutcome
    from backend.core.task import Task, TaskStatus
    from backend.skills.skill import SkillResult
    from unittest.mock import MagicMock

    task = Task(id="t1", title="t", description="t")

    outcome = MagicMock(spec=PipelineOutcome)
    outcome.task = task
    outcome.skill_result = SkillResult(
        skill_name="task_breakdown",
        status=TaskStatus.COMPLETED,
        output=None,
        steps=[],
    )

    AgentPipeline._execution_from_skill(MagicMock(), outcome)

    envelope = outcome.execution
    assert envelope["state"] == "NO_STEPS"
    assert task.status == TaskStatus.PENDING
