"""
GHOST — memory retrieval tool (M4).

memory_search: query the user's persistent memory store.

Safety constraints (params are untrusted):
- read-only: it never writes, updates or deletes memory
- it goes through the single existing MemoryService
  (backend/core/services.py ``memory_service``); no second
  memory store is opened here
- the query is length-bounded and results are capped, so a
  planner cannot request an unbounded dump
- RiskLevel.SAFE: reading the user's own memory has no
  external side effects
"""

from backend.core.services import memory_service

MAX_QUERY_CHARS = 500

MAX_LIMIT = 20


class MemoryToolError(Exception):
    """Raised for invalid params so the Agent maps it to a
    FAILED step with this message."""


def memory_search(params: dict) -> list[dict]:
    """
    Search the persistent memory store.

    params: {"query": <required text>,
             "limit": <optional int, default 5, max 20>}

    Returns a list of memory records (id, content, metadata
    as produced by MemoryService.search). Empty list when
    nothing matches — never a fabricated result.
    """

    params = params if isinstance(params, dict) else {}

    query = params.get("query")

    if not isinstance(query, str) or not query.strip():
        raise MemoryToolError(
            "memory_search requires a 'query' parameter."
        )

    query = query.strip()

    if len(query) > MAX_QUERY_CHARS:
        raise MemoryToolError(
            f"memory_search 'query' is too long "
            f"({len(query)} chars; limit {MAX_QUERY_CHARS})."
        )

    limit = params.get("limit", 5)

    if not isinstance(limit, int) or isinstance(limit, bool):
        raise MemoryToolError(
            "memory_search 'limit' must be an integer."
        )

    if limit < 1 or limit > MAX_LIMIT:
        raise MemoryToolError(
            f"memory_search 'limit' must be between 1 and "
            f"{MAX_LIMIT}."
        )

    try:
        results = memory_service.search(
            query=query,
            limit=limit,
        )
    except Exception as error:
        raise MemoryToolError(
            f"memory_search failed: {error}"
        ) from None

    return list(results or [])
