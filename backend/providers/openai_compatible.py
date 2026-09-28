import json
import time
import logging
import httpx

from backend.providers.base import AIProvider


logger = logging.getLogger(__name__)


class OpenAICompatibleProvider(AIProvider):
    def __init__(self, name, base_url, api_key, default_model=None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        # Providers whose APIs require an explicit model on
        # every request (Groq, HF router) must have one even
        # when the caller passes model=None — otherwise the
        # request fails as INVALID_REQUEST and the model
        # router correctly refuses to fall back.
        self.default_model = default_model

    async def generate(self, messages, model=None, **kwargs):
        if not self.api_key:
            raise ValueError(f"API key not configured for {self.name}")

        payload = {
            "messages": messages,
		"stream": False,
            **kwargs,
        }

        if model:
            payload["model"] = model
        elif self.default_model:
            payload["model"] = self.default_model

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        request_url = f"{self.base_url}/chat/completions"
        request_start = time.perf_counter()

        # Safe diagnostics: URL, model, timing, status — never
        # headers or key material.
        logger.info(
            "NVIDIA request: url=%s model=%s messages=%d "
            "stream=%s max_tokens=%s",
            request_url,
            payload.get("model"),
            len(payload["messages"]),
            payload.get("stream"),
            payload.get("max_tokens", "unset"),
        )

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=10.0, read=300.0)
        ) as client:
            try:
                response = await client.post(
                    request_url,
                    headers=headers,
                    json=payload,
                )
            except httpx.HTTPError as error:
                logger.error(
                    "NVIDIA request failed before/while receiving "
                    "response: type=%s message=%s elapsed=%.2fs",
                    type(error).__name__,
                    error,
                    time.perf_counter() - request_start,
                )
                raise

            logger.info(
                "NVIDIA response headers: status=%d in %.2fs",
                response.status_code,
                time.perf_counter() - request_start,
            )

            if response.status_code >= 400:
                # Sanitized error body: provider error JSON/text
                # only — never headers, never request material.
                logger.error(
                    "NVIDIA error body[:500]=%r",
                    response.text[:500],
                )

            response.raise_for_status()

            data = response.json()

            content = data["choices"][0]["message"]["content"]

            logger.info(
                "NVIDIA response complete in %.2fs (content_chars=%d)",
                time.perf_counter() - request_start,
                len(content or ""),
            )

            return content

    async def generate_stream(self, messages, model=None, **kwargs):
        if not self.api_key:
            raise ValueError(f"API key not configured for {self.name}")

        payload = {
            "messages": messages,
            "stream": True,
            **kwargs,
        }

        if model:
            payload["model"] = model
        elif self.default_model:
            payload["model"] = self.default_model

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        request_start = time.perf_counter()
        first_chunk_received = False

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=10.0, read=300.0)
        ) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            ) as response:

                logger.info(
                    "NVIDIA response headers: %.2fs",
                    time.perf_counter() - request_start,
                )

                response.raise_for_status()

                async for line in response.aiter_lines():
                    if not line:
                        continue

                    if line.startswith("data: "):
                        data = line[6:]

                        if data == "[DONE]":
                            break

                        try:
                            chunk = json.loads(data)

                            content = (
                                chunk["choices"][0]
                                .get("delta", {})
                                .get("content")
                            )

                            if content:
                                if not first_chunk_received:
                                    first_chunk_received = True

                                    logger.info(
                                        "NVIDIA first content chunk: %.2fs",
                                        time.perf_counter() - request_start,
                                    )

                                yield content

                        except json.JSONDecodeError:
                            continue

                logger.info(
                    "NVIDIA stream completed: %.2fs",
                    time.perf_counter() - request_start,
                )

    async def ping(self):
        """
        Live reachability check (GET /models).

        Any HTTP response means the endpoint is
        reachable; authentication failures surface
        as raise_for_status errors.
        """

        if not self.api_key:
            return {
                "reachable": False,
                "detail": "API key not configured",
            }

        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
            }

            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{self.base_url}/models",
                    headers=headers,
                )

                response.raise_for_status()

            return {
                "reachable": True,
                "status_code": response.status_code,
            }

        except httpx.HTTPError as error:
            return {
                "reachable": False,
                "detail": type(error).__name__,
            }

    async def health_check(self):
        # Metadata-only diagnostics for /health/provider (never
        # the credential itself).
        self.last_health_status_code = None
        self.last_health_error_type = (
            None if self.api_key else "not-configured"
        )
        return bool(self.api_key)