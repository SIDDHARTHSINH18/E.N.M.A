"""
GHOST — Hugging Face provider adapter (M-router).

An OPTIONAL OpenAI-compatible adapter for Hugging Face's
Inference Providers router. It reuses the existing
OpenAICompatibleProvider implementation — no second HTTP
stack, no second provider registry.

Configuration (environment only, never hardcoded):

- HF_API_KEY or HUGGINGFACE_API_KEY  — the access token
- HF_BASE_URL   (default https://router.huggingface.co/v1)
- HF_MODEL      (default meta-llama/Llama-3.1-8B-Instruct;
                 models are NOT assumed permanently free —
                 the operator chooses what the account allows)

Unconfigured behavior is honest and cheap:

- with no token, generate()/health_check raise
  ProviderUnavailable("huggingface: UNCONFIGURED ...")
  BEFORE any network call, and the model router classifies it
  PROVIDER_UNAVAILABLE and skips to the next provider.

Errors never embed the token: the message text below is
constant, and the underlying client never places the key in
an exception message (it travels only in headers).
"""

import os

from backend.providers.base import ProviderUnavailable
from backend.providers.openai_compatible import (
    OpenAICompatibleProvider,
)


class HuggingFaceProvider(OpenAICompatibleProvider):

    name = "huggingface"

    FALLBACK_MODEL = "meta-llama/Llama-3.1-8B-Instruct"

    UNCONFIGURED_MESSAGE = (
        "huggingface: UNCONFIGURED — set HF_API_KEY "
        "(or HUGGINGFACE_API_KEY) and optionally HF_MODEL to "
        "enable this provider; skipping."
    )

    def __init__(self):
        api_key = (
            os.getenv("HF_API_KEY", "").strip()
            or os.getenv("HUGGINGFACE_API_KEY", "").strip()
        )
        base_url = os.getenv(
            "HF_BASE_URL",
            "https://router.huggingface.co/v1",
        )

        super().__init__(
            name="huggingface",
            base_url=base_url,
            api_key=api_key or None,
        )

    def _default_model(self) -> str:
        return os.getenv("HF_MODEL", self.FALLBACK_MODEL).strip()

    def _require_configured(self):
        if not self.api_key:
            # Constant message: no token material can ever leak.
            raise ProviderUnavailable(
                self.UNCONFIGURED_MESSAGE,
                provider=self.name,
            )

    async def generate(self, messages, model=None, **kwargs):
        self._require_configured()
        return await super().generate(
            messages,
            model=model or self._default_model(),
            **kwargs,
        )

    async def generate_stream(self, messages, model=None, **kwargs):
        self._require_configured()
        return await super().generate_stream(
            messages,
            model=model or self._default_model(),
            **kwargs,
        )

    async def health_check(self):
        self._require_configured()
        return await super().health_check()
