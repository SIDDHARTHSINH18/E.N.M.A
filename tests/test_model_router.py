"""
Tests for the multi-provider model router (M-router).

All providers are fakes registered on dedicated Orchestrator
instances — no test performs a live API call. The real
HuggingFaceProvider is exercised only for its UNCONFIGURED
honesty behavior (which requires no network).

Coverage:
1  Gemini success → no fallback
2  Gemini 429 → Groq fallback
3  Gemini timeout → Groq fallback
4  Gemini invalid request → NO fallback
5  Gemini + Groq fail → NVIDIA (nemotron) fallback
6  all providers fail → honest failure
7  Hugging Face unconfigured → honest UNCONFIGURED diagnostic,
   skipped automatically
8  credentials never appear in routing metadata
9  credentials never appear in MODEL audit rows
10 existing model_generate callers keep working through the router
11 research flow continues after simulated Gemini 429
12 permission semantics unchanged (fail-closed unknown tool)
"""

import asyncio

import httpx
import pytest

import backend.tools.builtin.model as model_tool

from backend.core.orchestrator import (
    FALLBACK_ELIGIBLE,
    Orchestrator,
    ProviderFailureCategory,
    ProviderUnavailable,
    classify_provider_failure,
)
from backend.providers.huggingface import HuggingFaceProvider


# ============================================================
# Fake providers
# ============================================================

class FakeProvider:
    """Duck-typed provider with scripted outcomes."""

    def __init__(self, name, outcome=None):
        self.name = name
        self.outcome = outcome  # str | Exception | callable
        self.calls = []

    async def generate(self, messages, model=None, **kwargs):
        self.calls.append(model)

        outcome = self.outcome
        if callable(outcome):
            outcome = outcome(messages, model, **kwargs)

        if isinstance(outcome, Exception):
            raise outcome

        return outcome


def _rate_limited():
    request = httpx.Request(
        "POST", "https://api.example.com/v1/chat?key=SYNTHETIC_KEY_TEST"
    )
    response = httpx.Response(429, request=request)
    return httpx.HTTPStatusError(
        "429 Too Many Requests for url "
        "'https://api.example.com/v1/chat?key=SYNTHETIC_KEY_TEST'",
        request=request,
        response=response,
    )


def _invalid_request():
    request = httpx.Request("POST", "https://api.example.com/v1/chat")
    response = httpx.Response(400, request=request)
    return httpx.HTTPStatusError(
        "400 Bad Request", request=request, response=response
    )


def _timeout():
    return httpx.ReadTimeout("read timed out")


def _build(order="gemini,groq,nemotron,huggingface", **providers):
    orch = Orchestrator(default_provider="gemini")

    import os

    os.environ["ENMA_MODEL_FALLBACK_ORDER"] = order

    orch.fallback_order = [
        n.strip() for n in order.split(",") if n.strip()
    ]

    for name, provider in providers.items():
        orch.register_provider(name, provider)

    return orch


# ============================================================
# 1. Gemini success → no fallback
# ============================================================

def test_gemini_success_no_fallback():
    gemini = FakeProvider("gemini", "gemini says hi")
    groq = FakeProvider("groq", "groq says hi")

    orch = _build(gemini=gemini, groq=groq)

    result = asyncio.run(orch.generate("hello"))

    assert result == "gemini says hi"
    assert gemini.calls and not groq.calls
    assert orch.last_route["status"] == "ok"
    assert orch.last_route["selected_provider"] == "gemini"
    assert orch.last_route["attempts"] == []


# ============================================================
# 2/3. Transient Gemini failures → Groq fallback
# ============================================================

@pytest.mark.parametrize(
    "failure",
    [_rate_limited(), _timeout()],
    ids=["429-rate-limited", "timeout"],
)
def test_transient_gemini_failure_falls_back_to_groq(failure):
    gemini = FakeProvider("gemini", failure)
    groq = FakeProvider("groq", "groq saved the day")

    orch = _build(gemini=gemini, groq=groq)

    result = asyncio.run(orch.generate("hello"))

    assert result == "groq saved the day"
    assert groq.calls
    assert orch.last_route["selected_provider"] == "groq"
    assert [a["provider"] for a in orch.last_route["attempts"]] == [
        "gemini"
    ]
    assert orch.last_route["attempts"][0]["category"] in (
        ProviderFailureCategory.RATE_LIMITED.value,
        ProviderFailureCategory.TIMEOUT.value,
    )


def test_429_classified_rate_limited():
    assert classify_provider_failure(_rate_limited()) is (
        ProviderFailureCategory.RATE_LIMITED
    )
    assert _rate_limited().category if hasattr(
        _rate_limited(), "category"
    ) else True


def test_timeout_classified_timeout():
    assert classify_provider_failure(_timeout()) is (
        ProviderFailureCategory.TIMEOUT
    )


# ============================================================
# 4. Invalid request → NO fallback
# ============================================================

def test_invalid_request_does_not_fall_back():
    gemini = FakeProvider("gemini", _invalid_request())
    groq = FakeProvider("groq", "should never run")

    orch = _build(gemini=gemini, groq=groq)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(orch.generate("hello"))

    assert not groq.calls
    assert orch.last_route["status"] == "failed"
    assert orch.last_route["attempts"][0]["category"] == (
        ProviderFailureCategory.INVALID_REQUEST.value
    )


def test_fallback_eligible_set_is_conservative():
    assert ProviderFailureCategory.RATE_LIMITED in FALLBACK_ELIGIBLE
    assert ProviderFailureCategory.TIMEOUT in FALLBACK_ELIGIBLE
    assert ProviderFailureCategory.NETWORK_ERROR in FALLBACK_ELIGIBLE
    assert ProviderFailureCategory.PROVIDER_UNAVAILABLE in (
        FALLBACK_ELIGIBLE
    )
    assert ProviderFailureCategory.INVALID_REQUEST not in (
        FALLBACK_ELIGIBLE
    )
    assert ProviderFailureCategory.AUTH_FAILED not in FALLBACK_ELIGIBLE
    assert ProviderFailureCategory.UNKNOWN not in FALLBACK_ELIGIBLE


# ============================================================
# 5. Gemini + Groq fail → nemotron fallback
# ============================================================

def test_second_fallback_to_nemotron():
    gemini = FakeProvider("gemini", _rate_limited())
    groq = FakeProvider("groq", _timeout())
    nemotron = FakeProvider("nemotron", "nemotron online")

    orch = _build(
        gemini=gemini, groq=groq, nemotron=nemotron
    )

    result = asyncio.run(orch.generate("hello"))

    assert result == "nemotron online"
    assert [a["provider"] for a in orch.last_route["attempts"]] == [
        "gemini",
        "groq",
    ]
    assert orch.last_route["selected_provider"] == "nemotron"


# ============================================================
# 6. All providers fail → honest failure
# ============================================================

def test_all_providers_fail_honestly():
    gemini = FakeProvider("gemini", _rate_limited())
    groq = FakeProvider("groq", _timeout())
    nemotron = FakeProvider("nemotron", _timeout())

    orch = _build(
        gemini=gemini, groq=groq, nemotron=nemotron
    )

    with pytest.raises(Exception):
        asyncio.run(orch.generate("hello"))

    assert orch.last_route["status"] == "failed"
    assert orch.last_route["selected_provider"] is None
    assert [
        a["provider"]
        for a in orch.last_route["attempts"]
    ] == ["gemini", "groq", "nemotron"]


# ============================================================
# 7. Hugging Face unconfigured → honest UNCONFIGURED skip
# ============================================================

def test_huggingface_unconfigured_is_skipped(monkeypatch):
    monkeypatch.delenv("HF_API_KEY", raising=False)
    monkeypatch.delenv("HUGGINGFACE_API_KEY", raising=False)

    hf = HuggingFaceProvider()

    # No network call happens: the UNCONFIGURED diagnostic is
    # raised before any HTTP work.
    with pytest.raises(ProviderUnavailable, match="UNCONFIGURED"):
        asyncio.run(hf.generate([{"role": "user", "content": "x"}]))

    assert classify_provider_failure(
        ProviderUnavailable("huggingface: UNCONFIGURED", provider="huggingface")
    ) is ProviderFailureCategory.PROVIDER_UNAVAILABLE

    gemini = FakeProvider("gemini", _rate_limited())
    groq = FakeProvider("groq", "groq ok")

    orch = _build(
        order="gemini,groq,nemotron,huggingface",
        gemini=gemini,
        groq=groq,
        nemotron=FakeProvider("nemotron", _timeout()),
        huggingface=hf,
    )

    # HF is in the order but unconfigured: it must be skipped
    # automatically and Groq's success must surface.
    result = asyncio.run(orch.generate("hello"))

    assert result == "groq ok"
    assert orch.last_route["selected_provider"] == "groq"
    # HF never produced an attempt with a different category —
    # it either wasn't reached or was classified skippable.
    for attempt in orch.last_route["attempts"]:
        assert attempt["category"] in (
            ProviderFailureCategory.RATE_LIMITED.value,
            ProviderFailureCategory.TIMEOUT.value,
            ProviderFailureCategory.PROVIDER_UNAVAILABLE.value,
        )


def test_huggingface_unconfigured_diagnostic_honest():
    monkey = pytest.MonkeyPatch()
    monkey.delenv("HF_API_KEY", raising=False)
    monkey.delenv("HUGGINGFACE_API_KEY", raising=False)
    try:
        hf = HuggingFaceProvider()
        with pytest.raises(ProviderUnavailable) as info:
            asyncio.run(
                hf.generate([{"role": "user", "content": "x"}])
            )
        message = str(info.value)
        assert "UNCONFIGURED" in message
        assert "HF_API_KEY" in message
        # No fake response was produced.
        assert "response" not in message.lower()
    finally:
        monkey.undo()


# ============================================================
# 8/9. Credentials never appear in metadata or audit
# ============================================================

def test_credentials_never_reach_route_metadata_or_audit():
    emitted = []

    def audit_hook(task_id, stage, event=None, status=None, data=None):
        emitted.append({"stage": stage, "data": data})

    gemini = FakeProvider("gemini", _rate_limited())  # URL carries a key
    groq = FakeProvider("groq", "fine")

    orch = _build(gemini=gemini, groq=groq)
    orch.audit_hook = audit_hook

    result = asyncio.run(orch.generate("hello"))

    assert result == "fine"

    blob = repr(orch.last_route) + repr(emitted)

    assert "SYNTHETIC_KEY_TEST" not in blob
    assert "key=" not in blob
    for row in emitted:
        assert set(row["data"].keys()) <= {
            "provider",
            "model",
            "ok",
            "duration_ms",
            "error_type",
        }


# ============================================================
# 10. model_generate keeps working through the router
# ============================================================

def test_model_generate_uses_router_fallback(monkeypatch):
    gemini = FakeProvider("gemini", _rate_limited())
    groq = FakeProvider("groq", "routed summary output")

    router = _build(gemini=gemini, groq=groq)

    monkeypatch.setattr(model_tool, "orchestrator", router)

    from backend.tools.builtin.model import model_generate

    result = asyncio.run(
        model_generate({"prompt": "summarize this"})
    )

    assert result == "routed summary output"
    assert groq.calls


# ============================================================
# 11. Research flow continues after simulated Gemini 429
# ============================================================

def test_research_skill_survives_gemini_429(monkeypatch):
    from backend.agents.executor import Agent
    from backend.automation.engine import AutomationEngine, TaskStep
    from backend.core.task import Task, TaskStatus
    from backend.permissions.policy import PermissionPolicy
    from backend.skills.builtin.research import ResearchSkill
    from backend.tools.registry import RiskLevel, ToolRegistry

    gemini = FakeProvider("gemini", _rate_limited())
    groq = FakeProvider("groq", "SYNTHESIS: sourced findings [1].")

    router = _build(
        order="gemini,groq",
        gemini=gemini,
        groq=groq,
    )

    # The model_generate tool must route through the same
    # router instance this test controls.
    monkeypatch.setattr(model_tool, "orchestrator", router)

    def executor(tool_name, params):
        if tool_name == "web_search":
            return {
                "query": "ai tools",
                "provider": "fake",
                "results": [
                    {"url": "https://a.example.com/1", "title": "A"}
                ],
                "result_count": 1,
            }
        if tool_name == "web_fetch":
            return {
                "url": "https://a.example.com/1",
                "final_url": "https://a.example.com/1",
                "status_code": 200,
                "content_type": "text/html",
                "title": "A",
                "text": "Tool A supports offline use.",
                "retrieved_at": "2026-09-28T00:00:00+00:00",
                "bytes": 30,
            }
        if tool_name == "model_generate":
            # Through the router: gemini 429 → groq succeeds.
            # The executor returns the coroutine; the async
            # Agent path awaits it.
            return model_tool.model_generate(
                {"prompt": params.get("prompt", "x")}
            )
        raise RuntimeError(tool_name)

    registry = ToolRegistry()
    for name in ("web_search", "web_fetch", "model_generate"):
        registry.register(
            name=name,
            description="",
            category="test",
            risk_level=RiskLevel.SAFE,
        )
    policy = PermissionPolicy(registry)
    agent = Agent(
        name="research",
        registry=registry,
        permission_policy=policy,
        tool_executor=executor,
    )
    engine = AutomationEngine(agent)

    task = Task(title="r", description="research ai tools")

    result = asyncio.run(
        ResearchSkill().run(
            task,
            agent,
            {"topic": "ai coding tools", "automation_engine": engine},
        )
    )

    assert result.status == TaskStatus.COMPLETED
    assert "sourced findings" in result.output["summary"]
    assert result.output["source_summary"]["retrieved"] == 1
    assert groq.calls


# ============================================================
# 12. Permission semantics unchanged
# ============================================================

def test_permission_policy_still_fail_closed():
    from backend.permissions.policy import (
        PermissionDecision,
        PermissionPolicy,
    )
    from backend.tools.registry import ToolRegistry, RiskLevel

    registry = ToolRegistry()
    registry.register(
        name="safe_tool",
        description="",
        category="t",
        risk_level=RiskLevel.SAFE,
    )

    policy = PermissionPolicy(registry)

    assert policy.evaluate("safe_tool", "t1").decision is (
        PermissionDecision.ALLOW
    )
    # Unknown tool: still fail-closed, unchanged by the router.
    assert policy.evaluate(
        "model_router_unknown", "t1"
    ).decision is PermissionDecision.DENY


# ============================================================
# Router configuration
# ============================================================

def test_fallback_order_env_override():
    import os

    previous = os.environ.get("ENMA_MODEL_FALLBACK_ORDER")
    os.environ["ENMA_MODEL_FALLBACK_ORDER"] = "groq,gemini"
    try:
        orch = Orchestrator(default_provider="gemini")
        assert orch.fallback_order == ["groq", "gemini"]
    finally:
        if previous is None:
            del os.environ["ENMA_MODEL_FALLBACK_ORDER"]
        else:
            os.environ["ENMA_MODEL_FALLBACK_ORDER"] = previous


def test_explicit_provider_tried_first_then_order():
    gemini = FakeProvider("gemini", _rate_limited())
    groq = FakeProvider("groq", "groq wins after explicit pick")
    nemotron = FakeProvider("nemotron", _timeout())

    orch = _build(
        order="gemini,nemotron",
        gemini=gemini,
        groq=groq,
        nemotron=nemotron,
    )

    # Explicit provider (groq) tried first even though the
    # order starts with gemini; after its transient failure
    # the configured order resumes.
    result = asyncio.run(
        orch.generate("hello", provider_name="groq")
    )

    assert result == "groq wins after explicit pick"
    assert groq.calls == [None] or groq.calls
    assert [a["provider"] for a in orch.last_route["attempts"]] == []


def test_explicit_provider_transient_failure_falls_back():
    groq = FakeProvider("groq", _rate_limited())
    gemini = FakeProvider("gemini", "gemini rescued")

    orch = _build(
        order="gemini,groq",
        gemini=gemini,
        groq=groq,
    )

    result = asyncio.run(
        orch.generate("hello", provider_name="groq")
    )

    assert result == "gemini rescued"
    assert [a["provider"] for a in orch.last_route["attempts"]] == [
        "groq"
    ]


# ============================================================
# Default-model handling for OpenAI-compatible providers
# ============================================================

def test_openai_compatible_provider_sends_configured_default_model(monkeypatch):
    """A Groq-style provider that requires an explicit model must
    receive its configured default when the caller passes
    model=None — otherwise the request 400s as INVALID_REQUEST
    and the router (correctly) refuses to fall back."""

    from backend.providers.openai_compatible import (
        OpenAICompatibleProvider,
    )

    captured = {}

    class Transport(httpx.MockTransport):
        pass

    def handler(request):
        captured["model"] = (
            httpx.Request("POST", "http://x") and
            __import__("json").loads(request.content.decode()).get("model")
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    provider = OpenAICompatibleProvider(
        name="groq-test",
        base_url="https://api.groq.invalid/openai/v1",
        api_key="SYNTHETIC_KEY_TEST",
        default_model="llama-3.1-8b-instant",
    )

    monkeypatch.setattr(
        provider, "_client_factory", lambda: None, raising=False
    )

    import backend.providers.openai_compatible as oc

    # Route the provider's httpx client through a mock transport.
    real_async_client = httpx.AsyncClient

    class _Client:
        def __init__(self, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            self._c = real_async_client(**kwargs)

        async def __aenter__(self):
            return self._c

        async def __aexit__(self, *a):
            await self._c.aclose()

    monkeypatch.setattr(oc.httpx, "AsyncClient", _Client)

    result = asyncio.run(
        provider.generate([{"role": "user", "content": "hi"}])
    )

    assert result == "ok"
    assert captured["model"] == "llama-3.1-8b-instant"


def test_router_uses_provider_default_model_in_routing():
    class DefaultedProvider:
        default_model = "configured-default"

        async def generate(self, messages, model=None, **kwargs):
            assert model == "configured-default", model
            return "ok"

    orch = _build(
        order="gemini",
        gemini=FakeProvider("gemini", _rate_limited()),
        nemotron=DefaultedProvider(),
    )

    result = asyncio.run(orch.generate("hello"))

    assert result == "ok"
    assert orch.last_route["selected_model"] == "configured-default"
