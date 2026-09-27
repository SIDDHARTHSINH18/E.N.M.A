"""
Temporary provider-selection test (M3 testing support).

Proves the production Planner is wired to the Groq provider
(the working generation path while Gemini quota is exhausted)
and that the provider name flows through the existing gateway
call unchanged. This pins the provider-selection change only;
no M3 lifecycle behavior is asserted here.
"""

import pytest

from backend.core.agent_services import planner as production_planner
from backend.core.planner import Planner


def test_production_planner_uses_groq_provider():
    assert production_planner._provider_name == "groq"


def test_production_planner_passes_groq_model():
    # Groq requires an explicit model on every request; the
    # wiring must pass the configured GROQ_MODEL.
    assert production_planner._model == "openai/gpt-oss-120b"


def test_planner_still_defaults_to_gateway_default_without_override():
    # A Planner constructed without provider_name keeps the
    # orchestrator default (gemini) — the override is opt-in
    # at the wiring site only.
    p = Planner(object())
    assert p._provider_name is None


@pytest.mark.asyncio
async def test_planner_passes_provider_name_to_gateway():
    captured = {}

    class FakeGateway:
        async def generate(self, **kwargs):
            captured.update(kwargs)
            return (
                '{"enough_information": true, "task_title": "T", '
                '"task_description": "D", "steps": [], '
                '"assumptions": [], "clarifying_questions": []}'
            )

    planner = Planner(FakeGateway(), provider_name="groq")
    await planner.plan("do a thing")

    assert captured["provider_name"] == "groq"
    assert captured["model"] is None
