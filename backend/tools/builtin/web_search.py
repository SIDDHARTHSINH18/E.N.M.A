"""
GHOST — web search tool (M5.1, M5.14).

web_search queries the public web through a replaceable
provider adapter. Architecture:

    web_search tool
        ↓
    WebSearchService (provider selection, validation, limits)
        ↓
    WebSearchProvider adapters (tavily / duckduckgo / future)

Hard rules:

- Provider credentials come ONLY from the environment
  (TAVILY_API_KEY). Nothing is hardcoded; nothing is logged.
- With no provider configured, the tool fails honestly with a
  diagnostic that says how to configure one. Search results
  are NEVER invented as a fallback.
- Malformed provider output is rejected, not normalized into
  fake results: only fields the provider actually returned
  are passed through.
- Duplicates (same normalized URL) are dropped here.
- Risk: SAFE — reading public search results has no external
  side effects. Rate/abuse limits are enforced by the caller
  (research skill) and audited like every tool.
"""

import os
import re
from typing import Any, Dict, List, Optional, Protocol

import httpx

from backend.audit.log import redact_text
from backend.research.sources import (
    SourceStatus,
    ResearchSource,
    dedupe_sources,
    domain_of,
    normalize_url,
)


MAX_QUERY_CHARS = 300

MAX_RESULTS = 10

REQUEST_TIMEOUT_SECONDS = 15

_USER_AGENT = (
    "Mozilla/5.0 (compatible; ENMA-Research/1.0; personal AI "
    "assistant)"
)


class WebSearchError(Exception):
    """Raised when a search cannot be performed honestly."""


class WebSearchProvider(Protocol):
    """Minimal contract every search adapter implements."""

    name: str

    def search(
        self,
        query: str,
        max_results: int,
    ) -> List[Dict[str, Any]]:
        """Return raw structured results (title/url/snippet).
        Must raise WebSearchError on any failure."""
        ...


def _bounded(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return redact_text(text[:limit])


def _make_source(
    item: Dict[str, Any],
    query: str,
) -> Optional[ResearchSource]:
    """
    Convert one provider result into a ResearchSource stub.
    Only real fields are copied; a result without a usable URL
    is skipped entirely (never fabricated).
    """

    url = item.get("url") or item.get("href") or ""

    if not isinstance(url, str) or not url.strip():
        return None

    url = url.strip()

    if not url.lower().startswith(("http://", "https://")):
        return None

    return ResearchSource(
        url=url,
        title=_bounded(item.get("title"), 200),
        content=_bounded(
            item.get("snippet") or item.get("content"), 400
        ),
        status=SourceStatus.SKIPPED,
        via_query=query,
    )


# ============================================================
# Provider adapters
# ============================================================

class TavilyProvider:
    """Tavily search API (https://tavily.com). Key via env."""

    name = "tavily"

    def __init__(self, api_key: str):
        self._api_key = api_key

    def search(
        self,
        query: str,
        max_results: int,
    ) -> List[Dict[str, Any]]:
        try:
            response = httpx.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": self._api_key,
                    "query": query,
                    "max_results": max_results,
                    "search_depth": "basic",
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
                headers={"User-Agent": _USER_AGENT},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as error:
            raise WebSearchError(
                f"tavily search failed: HTTP "
                f"{error.response.status_code}"
            ) from None
        except Exception as error:
            raise WebSearchError(
                f"tavily search failed: {type(error).__name__}"
            ) from None

        results = payload.get("results")

        if not isinstance(results, list):
            raise WebSearchError(
                "tavily returned malformed results "
                "(no results list)"
            )

        return [r for r in results if isinstance(r, dict)]


class DuckDuckGoProvider:
    """DuckDuckGo HTML endpoint. No key required."""

    name = "duckduckgo"

    _RESULT_HREF = re.compile(r"uddg=([^&]+)")

    def search(
        self,
        query: str,
        max_results: int,
    ) -> List[Dict[str, Any]]:
        try:
            response = httpx.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                timeout=REQUEST_TIMEOUT_SECONDS,
                headers={"User-Agent": _USER_AGENT},
                follow_redirects=True,
            )
            response.raise_for_status()
            html = response.text
        except httpx.HTTPStatusError as error:
            raise WebSearchError(
                f"duckduckgo search failed: HTTP "
                f"{error.response.status_code}"
            ) from None
        except Exception as error:
            raise WebSearchError(
                f"duckduckgo search failed: "
                f"{type(error).__name__}"
            ) from None

        return self._parse(html)

    def _parse(self, html: str) -> List[Dict[str, Any]]:
        """
        Minimal structured extraction from the HTML results
        page. Malformed entries are skipped; nothing invented.
        """

        results: List[Dict[str, Any]] = []

        blocks = re.findall(
            r'<a[^>]+class="result__a"[^>]*href="([^"]+)"'
            r'[^>]*>(.*?)</a>',
            html,
            flags=re.S | re.I,
        )

        for href, raw_title in blocks[:MAX_RESULTS * 2]:
            # DDG wraps target URLs in a redirect parameter.
            match = self._RESULT_HREF.search(href)
            url = (
                httpx.QueryParams({"u": match.group(1)})["u"]
                if match
                else href
            )

            try:
                from urllib.parse import unquote

                url = unquote(url)
            except Exception:
                continue

            if not url.lower().startswith(("http://", "https://")):
                continue

            title = re.sub(r"<[^>]+>", "", raw_title)

            results.append(
                {
                    "title": title.strip(),
                    "url": url,
                }
            )

            if len(results) >= MAX_RESULTS:
                break

        return results


# ============================================================
# Service
# ============================================================

class WebSearchService:
    """
    Selects and runs the configured provider. The provider is
    chosen once per process from ENMA_SEARCH_PROVIDER (default:
    tavily when TAVILY_API_KEY exists, duckduckgo otherwise);
    the adapter stays swappable.
    """

    def __init__(
        self,
        provider: Optional[WebSearchProvider] = None,
        provider_name: Optional[str] = None,
    ):
        self._provider = provider
        self._provider_name = provider_name

    def resolve(self) -> WebSearchProvider:
        if self._provider is not None:
            return self._provider

        name = (
            self._provider_name
            or os.getenv("ENMA_SEARCH_PROVIDER", "").strip()
            or (
                "tavily"
                if os.getenv("TAVILY_API_KEY", "").strip()
                else "duckduckgo"
            )
        )

        if name == "tavily":
            api_key = os.getenv("TAVILY_API_KEY", "").strip()

            if not api_key:
                raise WebSearchError(
                    "No search provider configured: set "
                    "TAVILY_API_KEY (or set "
                    "ENMA_SEARCH_PROVIDER=duckduckgo for the "
                    "keyless adapter)."
                )

            self._provider = TavilyProvider(api_key)

        elif name == "duckduckgo":
            self._provider = DuckDuckGoProvider()

        else:
            raise WebSearchError(
                f"Unknown search provider '{name}'. Available: "
                "tavily, duckduckgo."
            )

        return self._provider

    def search(
        self,
        query: str,
        max_results: int = 5,
    ) -> List[ResearchSource]:
        """
        One bounded search. Returns provenance stubs (status
        SKIPPED — a search result is a lead, not a retrieved
        source). Deduplicated by normalized URL.
        """

        query = (query or "").strip()

        if not query:
            raise WebSearchError(
                "web_search requires a non-empty 'query'."
            )

        if len(query) > MAX_QUERY_CHARS:
            raise WebSearchError(
                f"web_search 'query' is too long "
                f"({len(query)} chars; limit {MAX_QUERY_CHARS})."
            )

        if not isinstance(max_results, int) or isinstance(
            max_results, bool
        ):
            raise WebSearchError(
                "web_search 'max_results' must be an integer."
            )

        max_results = max(1, min(max_results, MAX_RESULTS))

        raw = self.resolve().search(query, max_results)

        sources = [
            source
            for source in (
                _make_source(item, query) for item in raw
            )
            if source is not None
        ]

        return dedupe_sources(sources)


# Default service instance (provider resolved lazily so tests
# and alternate runtimes can inject their own).
web_search_service = WebSearchService()


def web_search(params: dict) -> dict:
    """
    Search the public web. SAFE tool.

    params: {"query": <required>,
             "max_results": <optional int, 1..10, default 5>}

    Returns {"query", "provider", "results": [{title, url,
    snippet, domain}], "result_count"}. Results are leads with
    provenance stubs — NOT retrieved sources.
    """

    params = params if isinstance(params, dict) else {}

    query = params.get("query")

    if not isinstance(query, str) or not query.strip():
        raise WebSearchError(
            "web_search requires a 'query' parameter."
        )

    max_results = params.get("max_results", 5)

    try:
        sources = web_search_service.search(
            query.strip(),
            max_results,
        )
    except WebSearchError:
        raise
    except Exception as error:
        raise WebSearchError(
            f"web_search failed: {type(error).__name__}"
        ) from None

    provider = web_search_service.resolve()

    return {
        "query": query.strip(),
        "provider": provider.name,
        "results": [
            {
                "title": s.title,
                "url": s.url,
                "snippet": s.content,
                "domain": s.domain,
            }
            for s in sources
        ],
        "result_count": len(sources),
    }
