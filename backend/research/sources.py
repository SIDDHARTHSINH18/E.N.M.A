"""
GHOST — research source provenance (M5.3).

A first-class, traceable representation of external research
sources. Every piece of external information ENMA reports must
be traceable back to where it came from:

    search result  ->  retrieved source  ->  extracted evidence

Design rules:

- A SEARCH SNIPPET IS NOT A RETRIEVED SOURCE. Snippets are
  hints that lead to sources; only actually-retrieved pages
  produce "retrieved" evidence. A source whose retrieval
  failed keeps its failure status and never masquerades as
  verified content.
- URLs are normalized before deduplication (scheme/host
  lowercase, fragment stripped, trailing slash preserved as
  returned) so the same page is not counted twice.
- Provenance objects are plain data (to_dict) so they can be
  embedded in task results, audit rows and memory without a
  second serialization system.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit


class SourceStatus:
    """Honest retrieval states for one source."""

    RETRIEVED = "retrieved"
    FAILED = "failed"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


def normalize_url(url: str) -> str:
    """
    Canonical form for deduplication:

    - lowercase scheme and host
    - strip fragment (same document)
    - drop default ports (80/443)
    - drop userinfo (never part of identity)
    """

    if not isinstance(url, str) or not url.strip():
        return ""

    parts = urlsplit(url.strip())

    if not parts.scheme or not parts.netloc:
        return url.strip()

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()

    port = parts.port

    if port and not (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    else:
        netloc = host

    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))


def domain_of(url: str) -> str:
    """Hostname of a URL, lowercased; '' when unparseable."""

    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


@dataclass
class ResearchSource:
    """One external source with full provenance."""

    url: str
    title: str = ""
    domain: str = ""
    retrieved_at: Optional[str] = None
    published_at: Optional[str] = None
    content: str = ""
    status: str = SourceStatus.FAILED
    status_code: Optional[int] = None
    error: Optional[str] = None
    # Where this source came from: the search query that
    # surfaced it, so the chain query -> result -> page stays
    # reconstructable.
    via_query: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.domain:
            self.domain = domain_of(self.url)

    def to_dict(self) -> dict:
        """JSON-safe provenance record."""

        return {
            "url": self.url,
            "title": self.title,
            "domain": self.domain,
            "retrieved_at": self.retrieved_at,
            "published_at": self.published_at,
            # Content is truncated here on purpose: provenance
            # records travel through results/audit/memory, and
            # full page text belongs to the evidence layer only.
            "content_excerpt": self.content[:500],
            "status": self.status,
            "status_code": self.status_code,
            "error": self.error,
            "via_query": self.via_query,
        }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def dedupe_sources(sources: List[ResearchSource]) -> List[ResearchSource]:
    """
    Keep the first occurrence of each normalized URL. Later
    duplicates are dropped (already-seen pages are not
    re-retrieved and not double-counted).
    """

    seen = set()
    unique = []

    for source in sources:
        key = normalize_url(source.url)

        if not key or key in seen:
            continue

        seen.add(key)
        unique.append(source)

    return unique


def sources_summary(sources: List[ResearchSource]) -> dict:
    """
    Honest aggregate for results/audit: how many sources were
    searched for, retrieved, and lost — never just a count of
    successes.
    """

    by_status: Dict[str, int] = {}

    for source in sources:
        by_status[source.status] = by_status.get(source.status, 0) + 1

    return {
        "total": len(sources),
        "retrieved": by_status.get(SourceStatus.RETRIEVED, 0),
        "by_status": by_status,
        "domains": sorted(
            {s.domain for s in sources if s.domain}
        ),
    }
