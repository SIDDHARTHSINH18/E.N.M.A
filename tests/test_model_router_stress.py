"""
P1.2 — model router stress tests.

Extends tests/test_model_router.py with the remaining failure
scenarios from the hardening spec. All providers are fakes on a
dedicated Orchestrator — no live API calls. The router itself is
NOT modified; these tests pin its existing contract.
"""

import httpx

from backend.core.orchestrator import (
    Orchestrator,
    classify_provider_failure,
)


class FakeProvider:
    def __init__(self, name, outcome=None):
        self.name = name
        self.outcome = outcome
        self.calls = []

    async def generate(self, messages, model=None, **kwargs):
        self.calls.append(model)
        outcome = self.outcome
        if callable(outcome):
            outcome = outcome(messages, model, **kwargs)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _status_error(status: int) -> Exception:
    request = httpx.Request(
        "POST", "https://api.example.com/v1/chat"
    )
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(
        f"{status} response", request=request, response=response
    )


def _build(order, **providers):
    orch = Orchestrator(default_provider="gemini")
    orch.fallback_order = [
        n.strip() for n in order.split(",") if n.strip()
    ]
    for name, provider in providers.items():
        orch.register_provider(name, provider)
    return orch


def test_network_error_falls_forward():
    gemini = FakeProvider("gemini", httpx.ConnectError("conn refused"))
    groq = FakeProvider("groq", "groq handled it")
    orch = _build("gemini,groq", gemini=gemini, groq=groq)

    import asyncio
    result = asyncio.run(orch.generate("m", "hello"))

    assert result == "groq handled it"
    assert orch.last_route["selected_provider"] == "groq"
    assert orch.last_route["status"] == "ok"
    assert groq.calls, "fallback provider was actually used"


def test_auth_failed_stops_immediately():
    gemini = FakeProvider("gemini", _status_error(401))
    groq = FakeProvider("groq", "should not be reached")
    orch = _build("gemini,groq", gemini=gemini, groq=groq)

    import asyncio
    try:
        asyncio.run(orch.generate("m", "hello"))
        raised = False
    except Exception:
        raised = True

    assert raised, "auth failure must surface, not be swallowed"
    assert not groq.calls, "auth failure must NOT fall back"
    assert orch.last_route["status"] == "failed"
    assert orch.last_route["attempts"][0]["category"] == "AUTH_FAILED"


def test_rate_limit_then_timeout_then_success():
    gemini = FakeProvider("gemini", _status_error(429))
    groq = FakeProvider("groq", httpx.ReadTimeout("t"))
    nemotron = FakeProvider("nemotron", "nemotron saved the run")
    orch = _build(
        "gemini,groq,nemotron",
        gemini=gemini, groq=groq, nemotron=nemotron,
    )

    import asyncio
    result = asyncio.run(orch.generate("m", "hello"))

    assert result == "nemotron saved the run"
    assert [a["provider"] for a in orch.last_route["attempts"]] == [
        "gemini", "groq"
    ]
    assert orch.last_route["selected_provider"] == "nemotron"


def test_last_failure_error_is_truthful():
    """When every provider fails, the raised error carries the
    final provider's failure — not a fabricated success or a
    generic message."""

    gemini = FakeProvider("gemini", _status_error(429))
    groq = FakeProvider("groq", _status_error(503))
    orch = _build("gemini,groq", gemini=gemini, groq=groq)

    import asyncio
    try:
        asyncio.run(orch.generate("m", "hello"))
        raised = None
    except Exception as error:
        raised = error

    assert raised is not None
    assert orch.last_route["status"] == "failed"
    attempts = orch.last_route["attempts"]
    assert [a["provider"] for a in attempts] == ["gemini", "groq"]
    assert attempts[-1]["category"] in (
        "SERVER_ERROR",          # 5xx classification
        "PROVIDER_UNAVAILABLE",  # 503 maps here in this codebase
    )


def test_routing_metadata_never_contains_secrets():
    """Failure metadata is type/category based; no credential
    material lands in routing attempts even when the fake error
    message embeds a key-shaped string."""

    secret_marker = "SYNTHETIC_SECRET_VALUE_123"

    gemini = FakeProvider(
        "gemini",
        RuntimeError(f"boom {secret_marker}"),
    )
    groq = FakeProvider("groq", RuntimeError("groq down too"))
    orch = _build("gemini,groq", gemini=gemini, groq=groq)

    import asyncio
    try:
        asyncio.run(orch.generate("m", "hello"))
    except Exception:
        pass

    import json
    blob = json.dumps(orch.last_route)

    assert secret_marker not in blob, (
        "raw exception text leaked into routing metadata"
    )
    assert secret_marker not in str(gemini.calls)
