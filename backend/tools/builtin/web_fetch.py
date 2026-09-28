"""
GHOST — controlled URL retrieval tool (M5.2).

web_fetch retrieves ONE publicly accessible HTTP(S) page and
extracts readable text. This is the network-facing tool, so
its defenses are explicit and code-enforced (params are
untrusted model output):

SSRF / protocol hardening:
- http and https only; every other scheme (file:, ftp:,
  javascript:, ...) is refused before any connection
- userinfo in the URL is refused
- the destination host is DNS-resolved and EVERY resolved IP
  must be public — loopback, private (RFC1918/CGNAT),
  link-local, ULA, and reserved ranges are refused, which
  blocks localhost, 169.254.* metadata endpoints, and
  internal-network targets regardless of DNS tricks
- redirects are followed MANUALLY: every hop is re-validated
  (scheme + IP) and bounded by MAX_REDIRECTS, so a redirect
  cannot smuggle the request to a private address

Resource limits:
- bounded total wall-clock budget and per-request timeout
- response bodies larger than MAX_RESPONSE_BYTES are refused
  (Content-Length checked first, streamed read enforced)
- one tool call fetches exactly one page; per-task page
  budgets live in the research skill

Content handling:
- HTML is reduced to text with the stdlib (title + visible
  text); scripts/styles are dropped
- retrieved text is UNTRUSTED DATA: callers must treat it as
  content, never as instructions (the research skill wraps it
  in untrusted-content framing before any model call)
- failures are honest and specific: timeout, HTTP error
  status, oversized body, blocked host, or unparseable page
  each produce a distinct, auditable error

Risk: SAFE — read-only retrieval of public pages, no side
effects beyond the fetch itself.
"""

import ipaddress
import re
import socket
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from backend.audit.log import redact_text


MAX_RESPONSE_BYTES = 512 * 1024  # 512 KB

MAX_REDIRECTS = 5

REQUEST_TIMEOUT_SECONDS = 15

MAX_TEXT_CHARS = 40_000

_ALLOWED_SCHEMES = ("http", "https")

_USER_AGENT = (
    "Mozilla/5.0 (compatible; ENMA-Research/1.0; personal AI "
    "assistant)"
)


class WebFetchError(Exception):
    """Raised when a page cannot be retrieved honestly."""


def _check_public_ip(ip_text: str, host: str) -> None:
    """Refuse every non-public resolved address."""

    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        raise WebFetchError(
            f"Cannot resolve host to a valid IP: '{host}'."
        )

    if not ip.is_global:
        raise WebFetchError(
            f"Refusing non-public address for '{host}' "
            f"({ip})."
        )


def _validate_url(url: str) -> tuple:
    """Scheme/userinfo validation + host resolution gate."""

    try:
        parts = urlsplit(url.strip())
    except ValueError:
        raise WebFetchError("Malformed URL.")

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise WebFetchError(
            f"Unsupported protocol: '{parts.scheme or 'none'}'. "
            "Only http and https are allowed."
        )

    if parts.username or parts.password:
        raise WebFetchError(
            "Refusing URLs with embedded credentials."
        )

    host = (parts.hostname or "").strip().rstrip(".")

    if not host:
        raise WebFetchError("URL has no host.")

    # Literal IPs are validated directly; hostnames are
    # resolved and every answer must be public (blocks DNS
    # rebinding toward private space at fetch time).
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            answers = socket.getaddrinfo(
                host, None, proto=socket.IPPROTO_TCP
            )
        except OSError as error:
            raise WebFetchError(
                f"Cannot resolve host '{host}': "
                f"{getattr(error, 'strerror', '') or 'dns error'}."
            )

        seen = set()

        for answer in answers:
            ip_text = answer[4][0]

            if ip_text in seen:
                continue

            seen.add(ip_text)
            _check_public_ip(ip_text, host)
    else:
        # A literal IP host must pass the same public-address
        # gate as any resolved name (blocks http://10.0.0.9/,
        # metadata endpoints, loopback, etc.).
        _check_public_ip(host, host)

    return parts, host


def _extract_html(html: str) -> tuple:
    """
    Title + visible text from HTML. Regex-based rather than
    HTMLParser: real-world pages (and malicious ones) routinely
    carry unclosed tags, and HTMLParser's CDATA mode for
    <title>/<script> silently swallows everything after an
    unclosed opener. Here, worst case is noisy text — never a
    total extraction failure.
    """

    title_match = re.search(
        r"<title[^>]*>(.*?)</title>", html, flags=re.S | re.I
    )

    if title_match:
        title = " ".join(title_match.group(1).split())[:300]
    else:
        # Malformed fallback: first title opener to first '<'.
        loose = re.search(
            r"<title[^>]*>([^<]*)", html, flags=re.I
        )
        title = (
            " ".join(loose.group(1).split())[:300] if loose else ""
        )

    # Closed script/style/noscript/template blocks removed
    # whole; then any dangling opener drops the rest of the
    # document (script content must never leak into text).
    cleaned = re.sub(
        r"(?is)<(script|style|noscript|template|svg)\b.*?</\1\s*>",
        " ",
        html,
    )
    cleaned = re.sub(
        r"(?is)<(script|style|noscript|template|svg)\b.*$",
        " ",
        cleaned,
    )
    # A title block whose content leaked past the title regex
    # (unclosed title) is dropped up to its opener end.
    cleaned = re.sub(
        r"(?is)<title[^>]*>", " ", cleaned
    )

    text = re.sub(r"(?s)<[^>]*>", "\n", cleaned)

    import html as html_lib

    text = html_lib.unescape(text)

    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)

    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "...[truncated]"

    return title, text


def _extract_body_text(content_type: str, body: str) -> tuple:
    """Dispatch extraction by content type; unknown types are
    returned as-is (bounded) rather than guessed at."""

    content_type = (content_type or "").lower()

    if "html" in content_type:
        return _extract_html(body)

    if content_type and not (
        content_type.startswith("text/")
        or "json" in content_type
        or "xml" in content_type
    ):
        raise WebFetchError(
            f"Unsupported content type: '{content_type}'. "
            "Only text/HTML pages are extracted."
        )

    text = body.strip()

    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "...[truncated]"

    return "", text


def _bounded_error(error: Exception) -> str:
    message = redact_text(
        f"{type(error).__name__}: "
        f"{' '.join(str(error).split())}"
    )

    return message[:300]


def fetch_page(url: str) -> dict:
    """
    Retrieve one public page. Raises WebFetchError on any
    failure (the Agent maps it to a FAILED step).
    """

    url = (url or "").strip()

    if not url:
        raise WebFetchError("web_fetch requires a 'url'.")

    current = url

    # Manual redirect loop: every hop re-validated.
    for hop in range(MAX_REDIRECTS + 1):
        parts, _ = _validate_url(current)

        base = (
            f"{parts.scheme.lower()}://"
            f"{parts.netloc.lower()}"
        )

        request_url = base + parts.path + (
            f"?{parts.query}" if parts.query else ""
        )

        try:
            with httpx.stream(
                "GET",
                request_url,
                timeout=REQUEST_TIMEOUT_SECONDS,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "text/html, text/*;q=0.9, */*;q=0.1",
                },
                follow_redirects=False,
            ) as response:

                if response.status_code in (
                    301, 302, 303, 307, 308
                ):
                    if hop >= MAX_REDIRECTS:
                        raise WebFetchError(
                            "Too many redirects "
                            f"(limit {MAX_REDIRECTS})."
                        )

                    location = response.headers.get("location", "")

                    if not location:
                        raise WebFetchError(
                            "Redirect without a location."
                        )

                    # Resolve relative redirects against the
                    # current URL, then re-validate from the top.
                    from urllib.parse import urljoin

                    current = urljoin(current, location.strip())
                    continue

                if response.status_code >= 400:
                    raise WebFetchError(
                        f"HTTP {response.status_code} for "
                        f"'{current}'."
                    )

                content_type = (
                    response.headers.get("content-type", "")
                )

                content_length = response.headers.get(
                    "content-length"
                )

                if content_length and content_length.isdigit():
                    if int(content_length) > MAX_RESPONSE_BYTES:
                        raise WebFetchError(
                            "Response too large "
                            f"({content_length} bytes; limit "
                            f"{MAX_RESPONSE_BYTES})."
                        )

                body_parts = []
                total = 0
                final_status = response.status_code
                final_content_type = content_type

                for chunk in response.iter_bytes():
                    total += len(chunk)

                    if total > MAX_RESPONSE_BYTES:
                        raise WebFetchError(
                            "Response exceeded streaming size "
                            f"limit ({MAX_RESPONSE_BYTES} bytes)."
                        )

                    body_parts.append(chunk)

        except WebFetchError:
            raise
        except httpx.TimeoutException:
            raise WebFetchError(
                f"Timeout retrieving '{url}' "
                f"({REQUEST_TIMEOUT_SECONDS}s limit)."
            ) from None
        except httpx.HTTPError as error:
            raise WebFetchError(
                f"Retrieval failed for '{url}': "
                + _bounded_error(error)
            ) from None
        except OSError as error:
            raise WebFetchError(
                f"Connection failed for '{url}': "
                + _bounded_error(error)
            ) from None

        break

    try:
        body = b"".join(body_parts).decode("utf-8", errors="replace")
    except Exception:
        raise WebFetchError(
            f"Could not decode response body of '{url}'."
        )

    title, text = _extract_body_text(final_content_type, body)

    if not text.strip():
        raise WebFetchError(
            f"Page at '{url}' contained no extractable text "
            "(empty or non-textual content)."
        )

    return {
        "url": url,
        "final_url": current,
        "status_code": final_status,
        "content_type": final_content_type.split(";")[0].strip(),
        "title": title,
        "text": text,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "bytes": total,
    }


def web_fetch(params: dict) -> dict:
    """Tool entry point. params: {"url": <required>}."""

    params = params if isinstance(params, dict) else {}

    url = params.get("url")

    if not isinstance(url, str) or not url.strip():
        raise WebFetchError(
            "web_fetch requires a 'url' parameter."
        )

    try:
        return fetch_page(url.strip())
    except WebFetchError:
        raise
    except Exception as error:
        # Defense in depth: nothing escapes as a raw traceback.
        raise WebFetchError(
            f"web_fetch failed: {_bounded_error(error)}"
        ) from None
