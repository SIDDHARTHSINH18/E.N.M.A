"""
Current / recent information routing tests (truthfulness pass).

The chat endpoint must:
- detect requests that need fresh information and actually run
  the web_search tool (mocked here — real network never used);
- ground the model turn in the retrieved evidence with hard
  provenance rules;
- when retrieval fails or returns nothing, respond honestly
  that it could NOT verify — never from model knowledge.
"""

import pytest

from backend.main import app
from backend.core.services import orchestrator
from fastapi.testclient import TestClient

import backend.api.chat as chat_api

client = TestClient(app)


@pytest.fixture
def fake_provider(monkeypatch):
    captured = {"messages": [], "calls": 0}

    async def fake_generate_stream(messages, model=None, **kwargs):
        captured["messages"].append(messages)
        captured["calls"] += 1
        yield "Grounded answer with [source](https://example.com/a)."

    for provider in orchestrator.providers.values():
        monkeypatch.setattr(
            provider,
            "generate_stream",
            fake_generate_stream,
        )

    return captured


def login_headers():
    import os

    response = client.post(
        "/api/auth/login",
        json={"password": os.environ["GHOST_AUTH_PASSWORD"]},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_detection_covers_the_reported_cases():
    assert chat_api.detect_current_events_request(
        "search on latest quantum computing news and give 5 key points"
    )
    assert chat_api.detect_current_events_request("as of 2026")
    assert chat_api.detect_current_events_request(
        "whats the current weather of gandhinagar"
    )
    assert chat_api.detect_current_events_request("any recent news?")
    assert not chat_api.detect_current_events_request("hello there")
    assert not chat_api.detect_current_events_request("what is 2+2")


def test_current_events_invokes_web_search_and_grounds_evidence(
    auth_client, monkeypatch, fake_provider
):
    calls = []

    def fake_web_search(params):
        calls.append(params)
        return {
            "query": params.get("query"),
            "provider": "tavily",
            "result_count": 2,
            "results": [
                {
                    "title": "Quantum milestone",
                    "url": "https://news.example/quantum",
                    "domain": "news.example",
                    "snippet": "Real retrieved snippet A",
                },
                {
                    "title": "QKD network",
                    "url": "https://news.example/qkd",
                    "domain": "news.example",
                    "snippet": "Real retrieved snippet B",
                },
            ],
        }

    monkeypatch.setattr(chat_api, "web_search", fake_web_search)

    response = auth_client.post(
        "/api/chat",
        json={
            "message": "search on latest quantum computing news",
            "provider": "groq",
        },
    )

    assert response.status_code == 200

    # The web_search tool was ACTUALLY invoked on this request.
    assert len(calls) == 1
    assert "quantum" in calls[0]["query"].lower()

    # The model turn contains the retrieved evidence wrapped as
    # untrusted content with provenance rules.
    messages = fake_provider["messages"][0]
    user_turn = messages[-1]["content"]
    assert "untrusted_content" in user_turn
    assert "https://news.example/quantum" in user_turn
    assert "Real retrieved snippet A" in user_turn
    assert "SOURCES:" in user_turn
    assert "search leads" in user_turn  # honest about not fetching pages


def test_current_events_failure_is_honest_not_fabricated(
    auth_client, monkeypatch, fake_provider
):
    def failing_web_search(params):
        raise chat_api.WebSearchError("no provider configured")

    monkeypatch.setattr(chat_api, "web_search", failing_web_search)

    response = auth_client.post(
        "/api/chat",
        json={
            "message": "latest quantum computing news as of 2026",
            "provider": "groq",
        },
    )

    assert response.status_code == 200

    messages = fake_provider["messages"][0]
    user_turn = messages[-1]["content"]
    assert "could not verify" in user_turn
    assert "Do NOT answer from your training knowledge" in user_turn
    # No evidence block exists: there is nothing to ground on.
    assert "untrusted_content" not in user_turn


def test_current_events_empty_results_are_honest(
    auth_client, monkeypatch, fake_provider
):
    monkeypatch.setattr(
        chat_api,
        "web_search",
        lambda params: {
            "query": params.get("query"),
            "provider": "tavily",
            "result_count": 0,
            "results": [],
        },
    )

    response = auth_client.post(
        "/api/chat",
        json={
            "message": "any recent news about fusion energy?",
            "provider": "groq",
        },
    )

    assert response.status_code == 200
    messages = fake_provider["messages"][0]
    user_turn = messages[-1]["content"]
    assert "could not verify" in user_turn


def test_system_prompt_carries_tool_execution_honesty(
    auth_client, fake_provider
):
    auth_client.post(
        "/api/chat",
        json={
            "message": "hello there friend",
            "provider": "groq",
        },
    )

    messages = fake_provider["messages"][0]
    system = messages[0]["content"]
    assert "TOOL EXECUTION HONESTY" in system
    assert "NOT executed any tools" in system
    assert "simulated" in system


def test_plain_chat_does_not_invoke_web_search(
    auth_client, monkeypatch, fake_provider
):
    def should_not_run(params):
        raise AssertionError("web_search must not run for plain chat")

    monkeypatch.setattr(chat_api, "web_search", should_not_run)

    response = auth_client.post(
        "/api/chat",
        json={"message": "hello there", "provider": "groq"},
    )

    assert response.status_code == 200
    assert fake_provider["calls"] == 1
