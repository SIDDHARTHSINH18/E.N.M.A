import asyncio
import logging
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.audit.log import redact_text
from backend.tools.builtin.web_search import web_search, WebSearchError
from backend.core.orchestrator import Orchestrator
from backend.core.security import (
    get_rate_limits,
    rate_limiter,
)
from backend.core.services import (
    context_optimizer,
    document_retriever,
    document_summarizer,
    documents,
    groq_model,
    memory_service,
    nvidia_model,
    orchestrator,
)


logger = logging.getLogger(
    "ghost.chat",
)


# ============================================================
# ROUTER
# ============================================================

router = APIRouter(
    prefix="/api",
    tags=["Chat"],
)


# ============================================================
# UNTRUSTED CONTENT DELIMITERS (M2, threat T3 layer 1)
#
# Document and memory text is data, never instructions.
# It is wrapped in explicit markers and any literal
# closing marker inside the content is escaped, so
# content cannot break out of the delimiters.
# ============================================================

UNTRUSTED_OPEN = "<untrusted_content>"

UNTRUSTED_CLOSE = "</untrusted_content>"

UNTRUSTED_ESCAPED_CLOSE = "</untrusted_content_escaped>"


def escape_untrusted(text: str) -> str:

    return str(text).replace(
        UNTRUSTED_CLOSE,
        UNTRUSTED_ESCAPED_CLOSE,
    )


def wrap_untrusted(label: str, text: str) -> str:

    return (
        f"{UNTRUSTED_OPEN}\n"
        f"{label}\n"
        f"{escape_untrusted(text)}\n"
        f"{UNTRUSTED_CLOSE}"
    )


# ============================================================
# REQUEST MODEL
# ============================================================

class HistoryMessage(BaseModel):

    role: str

    content: str


class ChatRequest(BaseModel):

    message: str

    provider: str = "gemini"

    model: str | None = None

    document_id: str | None = None

    # Recent conversation turns, oldest first.
    # The frontend sends the last MAX_HISTORY_TURNS
    # so GHOST keeps multi-turn context.
    history: list[HistoryMessage] = []


MAX_HISTORY_TURNS = 12

ALLOWED_HISTORY_ROLES = {
    "user",
    "assistant",
}


# ============================================================
# WHOLE DOCUMENT INTENT DETECTION
# ============================================================

def is_whole_document_request(message: str) -> bool:
    text = " ".join(
        message.lower().strip().split()
    )

    # --------------------------------------------------------
    # Explicit whole-document requests
    # --------------------------------------------------------

    whole_document_phrases = [
        # Direct document summaries
        "summarize the doc",
        "summarise the doc",
        "summary of the doc",
        "summarize this doc",
        "summarise this doc",
        "summary of this doc",
        "give me a summary of the doc",
        "give me a summary of this doc",
        "summarize the document",
        "summarise the document",
        "summarize this document",
        "summarise this document",

        "summarize the pdf",
        "summarise the pdf",
        "summarize this pdf",
        "summarise this pdf",

        "summary of the document",
        "summary of this document",
        "summary of the pdf",
        "summary of this pdf",

        # Complete / full summaries
        "give me a summary",
        "give me the summary",
        "give a summary",

        "overall summary",
        "overall overview",

        "complete summary",
        "full summary",
        "detailed summary",
        "comprehensive summary",

        "summarize everything",
        "summarise everything",

        "summarize the whole document",
        "summarise the whole document",

        "summarize entire document",
        "summarise entire document",

        "summarize the entire document",
        "summarise the entire document",

        # Whole-document overview
        "complete overview",
        "full overview",
        "detailed overview",
        "comprehensive overview",

        "overview of the document",
        "overview of this document",

        "give me an overview of the document",
        "give me an overview of this document",

        # Explicit whole-document analysis
        "analyze the entire document",
        "analyse the entire document",

        "analyze the whole document",
        "analyse the whole document",

        "cover the entire document",
        "cover the whole document",

        # Explicit document understanding
        "what is this document about",
        "what does this document contain",

        "explain the document",
        "explain this document",

        "explain the entire document",
        "explain the whole document",

        "analyze this document",
        "analyse this document",

        # Question generation
        "generate questions from the document",
        "generate questions based on the document",

        "create questions from the document",
        "create questions based on the document",

        "make questions from the document",
        "make questions based on the document",

        "generate quiz questions",
        "create quiz questions",
    ]

    # --------------------------------------------------------
    # Direct explicit match
    # --------------------------------------------------------

    if any(
        phrase in text
        for phrase in whole_document_phrases
    ):
        return True

    # --------------------------------------------------------
    # Strong whole-document intent
    #
    # IMPORTANT:
    # "explain" is intentionally NOT included here.
    #
    # Example:
    # "Explain the Bhakti movement from the PDF"
    # must use targeted retrieval, not whole-document
    # summarization.
    # --------------------------------------------------------

    summary_words = [
        "summarize",
        "summarise",
        "summary",
        "overview",
    ]

    document_words = [
        "document",
        "pdf",
        "file",
        "whole",
        "entire",
        "everything",
    ]

    has_summary_intent = any(
        word in text
        for word in summary_words
    )

    has_document_scope = any(
        word in text
        for word in document_words
    )

    if (
        has_summary_intent
        and has_document_scope
    ):
        return True

    return False


# ============================================================
# DOCUMENT QUESTION INTENT ROUTING
# ============================================================

def should_use_document(message: str) -> bool:
    """
    Decide whether an uploaded document should be used for this request.

    IMPORTANT:
    The frontend may keep a document_id selected after upload. That does NOT
    mean every later question is a document question.

    Document mode is enabled for explicit document/page requests and for
    questions that clearly refer to the uploaded file. Project/memory
    questions stay in normal GHOST mode even when a PDF is attached.
    """

    if not message:
        return False

    text = " ".join(
        message.lower().strip().split()
    )

    # ------------------------------------------------------------
    # Exact page requests always use the uploaded document.
    # ------------------------------------------------------------

    if extract_requested_page(message) is not None:
        return True

    # ------------------------------------------------------------
    # Explicit whole-document requests always use the document.
    # ------------------------------------------------------------

    if is_whole_document_request(message):
        return True

    # ------------------------------------------------------------
    # Explicit references to the uploaded document.
    # ------------------------------------------------------------

    document_phrases = [
        "uploaded doc",
        "this doc",
        "the doc",
        "in the doc",
        "from the doc",
        "according to the doc",
        "uploaded document",
        "uploaded pdf",
        "uploaded file",
        "this document",
        "this pdf",
        "this file",
        "the document",
        "the pdf",
        "the file",
        "in the document",
        "in this document",
        "in the pdf",
        "in this pdf",
        "from the document",
        "from this document",
        "from the pdf",
        "from this pdf",
        "according to the document",
        "according to this document",
        "according to the pdf",
        "according to this pdf",
        "according to the uploaded document",
        "according to the uploaded pdf",
    ]

    if any(phrase in text for phrase in document_phrases):
        return True

    # ------------------------------------------------------------
    # Strong document-specific question words.
    # These require document terminology as well, so a generic
    # question like "explain the project" stays out of document mode.
    # ------------------------------------------------------------

    document_words = [
        "document",
        "pdf",
        "file",
        "chapter",
        "section",
        "paragraph",
        "passage",
        "page",
        "pages",
        "table",
        "figure",
        "heading",
        "topic in the pdf",
        "topic in the document",
    ]

    if any(word in text for word in document_words):
        return True

    # ------------------------------------------------------------
    # Project/GHOST questions intentionally remain normal GHOST mode.
    # This is the key fix for the problem where an uploaded history PDF
    # hijacked questions about the GHOST project.
    # ------------------------------------------------------------

    return False


# ============================================================
# EXACT PAGE REQUEST DETECTION
# ============================================================

def extract_requested_page(
    message: str,
):
    """
    Detect explicit page requests.

    Examples:

    What is on page 49?
    Tell me about page 49
    What does page 49 say?
    Summarize page 49
    Explain pg 49
    What is on p. 49?

    Returns:
        int page number
        None when no explicit page request exists.
    """

    if not message:
        return None

    text = " ".join(
        message.lower().strip().split()
    )

    patterns = [
        r"\bpage\s*\.?\s*(\d+)\b",
        r"\bpages?\s*\.?\s*(\d+)\b",
        r"\bpg\s*\.?\s*(\d+)\b",
        r"\bp\s*\.\s*(\d+)\b",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
        )

        if match:

            try:
                page_number = int(
                    match.group(1),
                )

                if page_number > 0:
                    return page_number

            except ValueError:
                pass

    return None


# ============================================================
# CHUNK PAGE RANGE
# ============================================================

def get_chunk_page_range(
    chunk,
):
    """
    Safely determine the page range covered by a chunk.

    Supports:

    page
    start_page
    end_page
    """

    start_page = chunk.get(
        "start_page",
    )

    end_page = chunk.get(
        "end_page",
    )

    if start_page is None:
        start_page = chunk.get(
            "page",
        )

    if end_page is None:
        end_page = start_page

    try:

        if start_page is not None:
            start_page = int(
                start_page,
            )

        if end_page is not None:
            end_page = int(
                end_page,
            )

    except (
        TypeError,
        ValueError,
    ):

        return None, None

    return (
        start_page,
        end_page,
    )


# ============================================================
# FIND EXACT PAGE CHUNKS
# ============================================================

def find_exact_page_chunks(
    chunks,
    requested_page,
):
    """
    Return every chunk whose page range
    contains the requested page.

    This intentionally does NOT use semantic
    similarity.

    Page requests are deterministic.
    """

    exact_chunks = []

    for chunk in chunks:

        start_page, end_page = (
            get_chunk_page_range(
                chunk,
            )
        )

        if (
            start_page is None
            or end_page is None
        ):
            continue

        if (
            start_page
            <= requested_page
            <= end_page
        ):
            exact_chunks.append(
                chunk,
            )

    return exact_chunks


# ============================================================
# CHECK DOCUMENT PAGE INDEX
# ============================================================

def document_has_page(
    document,
    requested_page,
):
    """
    Determine whether the uploaded document
    contains the requested page according to
    its indexed page metadata.
    """

    # --------------------------------------------------
    # Check page metadata
    # --------------------------------------------------

    for page in document.get(
        "pages",
        [],
    ):

        page_number = page.get(
            "page",
        )

        try:

            if int(
                page_number,
            ) == requested_page:

                return True

        except (
            TypeError,
            ValueError,
        ):

            continue

    # --------------------------------------------------
    # Check chunk ranges as fallback
    # --------------------------------------------------

    for chunk in document.get(
        "chunks",
        [],
    ):

        start_page, end_page = (
            get_chunk_page_range(
                chunk,
            )
        )

        if (
            start_page is not None
            and end_page is not None
            and start_page
            <= requested_page
            <= end_page
        ):
            return True

    return False


# ============================================================
# MEMORY CONTEXT
# ============================================================

def get_memory_context(
    message: str,
) -> str:

    try:

        context = (
            memory_service.build_context(
                message,
                limit=5,
            )
        )

        if context:
            return context

    except Exception as error:

        # Metadata only — never log memory content.
        logger.warning(
            "Memory retrieval failed: %s",
            type(error).__name__,
        )

    return ""


# ============================================================
# LOG MEMORY MATCHES (METADATA ONLY)
# ============================================================

def log_memory_matches(
    message: str,
) -> None:
    """
    Log which memory IDs matched, never their
    content: the console is not a private place
    and memory text is user-confidential.
    """

    try:

        matches = memory_service.search(
            message,
            limit=5,
        )

        if matches:

            logger.info(
                "Memory matches: %s",
                [
                    {
                        "id": m.get("id"),
                        "type": m.get("type"),
                        "score": m.get("_score"),
                    }
                    for m in matches
                ],
            )

    except Exception:

        pass


# ============================================================
# SAVE CONVERSATION MEMORY
# ============================================================

def save_conversation_memory(
    user_message,
    assistant_response,
    document_id=None,
):
    """
    Save useful user-provided information.

    We intentionally do not blindly store the
    assistant's generated response.
    """

    try:

        user_text = (
            user_message.strip()
        )

        if not user_text:
            return

        # Ignore very short messages.

        if len(user_text) < 8:
            return

        # Ignore generic greetings.

        ignored_messages = {
            "hello",
            "hi",
            "hey",
            "thanks",
            "thank you",
            "ok",
            "okay",
            "cool",
            "great",
            "bye",
        }

        if (
            user_text.lower()
            in ignored_messages
        ):
            return

        lower = user_text.lower()

        memory_triggers = [
            "remember",
            "my name is",
            "my project is",
            "i prefer",
            "i like",
            "i don't like",
            "i dont like",
            "i use",
            "i want",
            "i need",
            "we decided",
            "keep in mind",
            "from now on",
            "going forward",
        ]

        should_remember = any(
            trigger in lower
            for trigger in memory_triggers
        )

        if not should_remember:
            return

        # --------------------------------------------------
        # Never store obvious secrets
        # --------------------------------------------------

        secret_terms = [
            "password",
            "api key",
            "apikey",
            "token",
            "secret",
            "private key",
            "otp",
            "credit card",
        ]

        if any(
            term in lower
            for term in secret_terms
        ):
            return

        # --------------------------------------------------
        # Determine memory type
        # --------------------------------------------------

        memory_type = "user_fact"

        if "project" in lower:

            memory_type = "project"

        elif (
            "prefer" in lower
            or "like" in lower
        ):

            memory_type = "preference"

        elif "decided" in lower:

            memory_type = "decision"

        # --------------------------------------------------
        # Metadata
        # --------------------------------------------------

        metadata = {
            "automatic": True,
            "source_type": "conversation",
        }

        if document_id:
            metadata[
                "document_id"
            ] = document_id

        # --------------------------------------------------
        # Save memory
        # --------------------------------------------------

        memory_service.add_memory(
            content=user_text,
            memory_type=memory_type,
            importance=0.8,
            source="user",
            metadata=metadata,
        )

    except Exception as error:

        logger.warning(
            "Memory save skipped: %s",
            type(error).__name__,
        )


# ============================================================
# PROMPT ASSEMBLY
# ============================================================

# ============================================================
# CURRENT / RECENT INFORMATION ROUTING (truthfulness)
#
# Requests asking for latest/current/recent information must be
# answered from REAL retrieval evidence, never from model
# knowledge dressed up as news. If live retrieval is
# unavailable, the honest answer is "I could not verify".
# ============================================================

CURRENT_EVENTS_PATTERN = re.compile(
    r"\b("
    r"latest|newest|breaking|recent(ly)?|current(ly)?|today|"
    r"this\s+(week|month|year|morning|evening)|yesterday|"
    r"as\s+of\s+\d{4}|up\s+to\s+date|"
    r"(latest|recent|breaking)\s+(news|updates?|developments?)"
    r")\b",
    re.IGNORECASE,
)

# Questions that are time-anchored but not about events
# (e.g. "current temperature", "what time is it") still require
# live data; the same rule applies: no live data -> say so.
LIVE_DATA_PATTERN = re.compile(
    r"\b(weather|temperature|forecast|stock\s+price|exchange\s+rate|"
    r"time\s+in|what\s+time)\b",
    re.IGNORECASE,
)


def detect_current_events_request(text: str) -> bool:
    """True when the request needs fresh retrieval evidence."""

    if not text:
        return False

    return bool(
        CURRENT_EVENTS_PATTERN.search(text)
        or LIVE_DATA_PATTERN.search(text)
    )


def build_current_events_context(
    message: str,
    search_result: dict,
) -> str:
    """
    Compose the user turn for a current-events request whose
    web search SUCCEEDED. Evidence is provider-returned search
    leads (title/url/domain/snippet) wrapped as untrusted
    content; the model may only ground claims in it and must
    cite provenance.
    """

    results = search_result.get("results") or []

    lines = []
    for index, item in enumerate(results, start=1):
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        domain = str(item.get("domain") or "").strip()
        snippet = str(item.get("snippet") or "").strip()

        lines.append(
            f"[{index}] {title or '(untitled)'}\n"
            f"    URL: {url}\n"
            f"    Domain: {domain or 'unknown'}\n"
            f"    Snippet: {snippet[:600]}"
        )

    evidence = "\n\n".join(lines) if lines else "(no results)"

    return (
        "The user is asking about CURRENT or RECENT information.\n\n"
        "HARD RULES:\n"
        "- Ground every factual claim ONLY in the search results "
        "below. They are real leads returned by a live web search "
        "(title, URL, domain and provider snippet per source).\n"
        "- Cite each claim inline as a markdown link: "
        "[Source title](URL).\n"
        "- Never present your own background knowledge as current "
        "news. If the results do not cover something, say so "
        "explicitly.\n"
        "- If the sources disagree, report the disagreement.\n"
        "- Do not invent sources, URLs, dates or quotes.\n"
        "- End with a 'SOURCES:' list of markdown links you "
        "actually used.\n\n"
        f"SEARCH QUERY USED: {search_result.get('query', message)}\n"
        f"SEARCH PROVIDER: {search_result.get('provider', 'unknown')}\n"
        "NOTE: these are search leads with provider snippets; "
        "pages were not fetched in full.\n\n"
        f"<untrusted_content label=\"web_search_results\">\n"
        f"{evidence}\n"
        f"</untrusted_content>\n\n"
        f"CURRENT USER REQUEST:\n{message}"
    )


def build_current_events_unavailable(
    message: str,
    reason: str,
) -> str:
    """User turn for a current-events request whose retrieval
    FAILED: the model must answer honestly that it could not
    verify, not fabricate."""

    return (
        "The user is asking about CURRENT or RECENT information, "
        "but live web retrieval is NOT available right now "
        f"({reason}).\n\n"
        "HARD RULES:\n"
        "- Respond ONLY that you could not verify current "
        "information because live retrieval is unavailable.\n"
        "- Do NOT answer from your training knowledge, do NOT "
        "produce any news, dates, numbers or events.\n"
        "- Suggest trying again later.\n\n"
        f"CURRENT USER REQUEST:\n{message}"
    )


def build_request_messages(
    user_content: str,
    history: list[HistoryMessage],
) -> list[dict]:
    """
    System prompt first, then recent conversation,
    then the composed request for this turn.

    Previously the live path sent a single user
    message and the GHOST system prompt was dead
    code (defect D2), and history was never sent
    (defect D1).
    """

    messages = [
        {
            "role": "system",
            "content": (
                orchestrator.build_system_prompt()
                + "\n\n"
                "TOOL EXECUTION HONESTY:\n"
                "- In this chat you have NOT executed any tools. "
                "Never claim that a file was created, read, "
                "written or verified, and never produce "
                "simulated tool output such as 'Result "
                "(simulated)'.\n"
                "- If the user asks to perform an action on "
                "files or systems, explain that ENMA performs "
                "actions through its Task system (which asks "
                "for approval where needed) and that nothing "
                "has been executed in this chat.\n"
                "- Claims about current or recent events require "
                "retrieval evidence; without it, say you cannot "
                "verify.\n"
            ),
        },
    ]

    for item in history[-MAX_HISTORY_TURNS:]:

        role = item.role

        content = item.content.strip()

        if (
            role not in ALLOWED_HISTORY_ROLES
            or not content
        ):
            continue

        messages.append(
            {
                "role": role,
                "content": content,
            }
        )

    messages.append(
        {
            "role": "user",
            "content": user_content,
        }
    )

    return messages


# ============================================================
# CHAT ENDPOINT
# ============================================================

@router.post("/chat")
async def chat(
    request: ChatRequest,
    http_request: Request,
):

    # ========================================================
    # VALIDATE MESSAGE
    # ========================================================

    if (
        not request.message
        or not request.message.strip()
    ):

        raise HTTPException(
            status_code=400,
            detail="Message cannot be empty.",
        )

    # ========================================================
    # PER-SESSION CHAT RATE LIMIT
    # (tighter than the general middleware cap)
    # ========================================================

    limits = get_rate_limits()

    session_token = getattr(
        http_request.state,
        "session_token",
        "",
    )

    allowed, retry_after = rate_limiter.check(
        f"chat:{session_token}",
        limits["chat"],
    )

    if not allowed:

        raise HTTPException(
            status_code=429,
            detail=(
                "Chat rate limit exceeded. "
                "Slow down."
            ),
            headers={
                "Retry-After": str(
                    int(retry_after),
                ),
            },
        )

    # ========================================================
    # GET PROVIDER
    # ========================================================

    try:

        provider = (
            orchestrator.get_provider(
                request.provider,
            )
        )

    except ValueError as error:

        raise HTTPException(
            status_code=404,
            detail=str(error),
        )

    provider_name = request.provider or "gemini"

    # Nemotron-specific defaults stay on Nemotron; other
    # providers (Gemini) keep their own configured model.
    if request.model:
        chat_model = request.model
    elif provider_name == "nemotron":
        chat_model = nvidia_model
    elif provider_name == "groq":
        # Groq has no server-side default model: it must be
        # explicit on every request.
        chat_model = groq_model
    else:
        chat_model = None

    logger.info(
        "[ENMA PROVIDER] provider=%s model=%s",
        provider_name,
        chat_model or "<provider default>",
    )

    # ========================================================
    # MEMORY
    # ========================================================

    log_memory_matches(
        request.message,
    )

    memory_context = (
        get_memory_context(
            request.message,
        )
    )

    # ========================================================
    # DOCUMENT MODE
    # ========================================================

    # IMPORTANT: document_id only tells us that a document is selected.
    # It does NOT mean every user message should use that document.
    current_events = detect_current_events_request(request.message)

    use_document = (
        bool(request.document_id)
        and should_use_document(request.message)
    )

    if current_events and not use_document:
        # ====================================================
        # CURRENT / RECENT INFORMATION: retrieval REQUIRED
        # ====================================================

        try:
            search_result = await asyncio.to_thread(
                web_search,
                {
                    'query': request.message.strip()[:380],
                    'max_results': 6,
                },
            )
        except WebSearchError as error:
            search_result = None
            retrieval_reason = str(error)
        except Exception as error:
            search_result = None
            retrieval_reason = f'{type(error).__name__}'

        if search_result and search_result.get('result_count'):
            logger.info(
                'CURRENT EVENTS MODE — sources=%d provider=%s',
                search_result.get('result_count', 0),
                search_result.get('provider', 'unknown'),
            )
            user_content = build_current_events_context(
                request.message,
                search_result,
            )
        else:
            reason = (
                retrieval_reason
                if not search_result
                else 'the web search returned no results'
            )
            logger.warning(
                'CURRENT EVENTS MODE — retrieval unavailable: %s',
                reason,
            )
            user_content = build_current_events_unavailable(
                request.message,
                reason,
            )
    elif use_document:

        document = documents.get(
            request.document_id,
        )

        if document is None:

            raise HTTPException(
                status_code=404,
                detail=(
                    "Document not found. "
                    "Please upload the document again."
                ),
            )

        chunks = document.get(
            "chunks",
            [],
        )

        # ====================================================
        # WHOLE DOCUMENT MODE
        # ====================================================

        if is_whole_document_request(
            request.message,
        ):

            logger.info(
                "WHOLE DOCUMENT MODE — document=%s, chunks=%d",
                document.get("filename", "Unknown"),
                len(chunks),
            )

            # ------------------------------------------------
            # Check cached summary
            # ------------------------------------------------

            cached_summary = (
                document.get(
                    "document_summary",
                )
            )

            if cached_summary:

                logger.info(
                    "Using cached document summary.",
                )

                document_summary = (
                    cached_summary
                )

            else:

                logger.info(
                    "Creating complete document summary...",
                )

                try:

                    document_summary = (
                        await document_summarizer
                        .create_document_summary(
                            chunks,
                        )
                    )

                except Exception as error:

                    logger.error(
                        "Document summarization failed: %s",
                        type(error).__name__,
                    )

                    raise HTTPException(
                        status_code=502,
                        detail=(
                            "Could not create the document "
                            "summary. Check the server logs "
                            "for details."
                        ),
                    )

                document[
                    "document_summary"
                ] = document_summary

                logger.info(
                    "Document summary created successfully.",
                )

            # ------------------------------------------------
            # Whole document source pages
            # ------------------------------------------------

            retrieved_pages = []

            for page in document.get(
                "pages",
                [],
            ):

                page_number = page.get(
                    "page",
                )

                if page_number is not None:

                    try:

                        page_number = int(
                            page_number,
                        )

                        if (
                            page_number
                            not in retrieved_pages
                        ):

                            retrieved_pages.append(
                                page_number,
                            )

                    except (
                        TypeError,
                        ValueError,
                    ):

                        continue

            retrieved_pages.sort()

            source_instruction = (
                "The information comes from "
                "the complete uploaded document."
            )

            # ------------------------------------------------
            # Memory
            # ------------------------------------------------

            memory_section = ""

            if memory_context:

                memory_section = (
                    "\n\n"
                    f"{wrap_untrusted('Source: GHOST long-term memory', memory_context)}\n"
                    "Use this memory only when it is "
                    "relevant to the current request.\n"
                )

            # ------------------------------------------------
            # Prompt
            # ------------------------------------------------

            user_content = (

                "You are analyzing an uploaded document.\n\n"

                "The user has requested a whole-document "
                "analysis rather than a question about "
                "one specific section.\n\n"

                "Use ONLY the document-level understanding "
                "provided below as the document source "
                "of truth.\n\n"

                "DOCUMENT-LEVEL UNDERSTANDING:\n"
                "--------------------------------\n"

                f"{wrap_untrusted('Source: uploaded document (whole-document summary)', document_summary)}\n"

                "--------------------------------\n\n"

                f"{source_instruction}\n"

                f"{memory_section}\n"

                "IMPORTANT:\n"
                "- Give a clear and detailed answer.\n"
                "- Stay faithful to the document.\n"
                "- Do not invent facts.\n"
                "- Cover important topics and relationships.\n"
                "- If the requested information is not "
                "supported by the document understanding, "
                "say so clearly.\n"
                "- If the user asks for questions, create "
                "questions based on different parts of "
                "the document.\n"
                "- Do not mismatch question headings "
                "with their actual questions.\n\n"

                f"USER REQUEST:\n"
                f"{request.message}"
            )

            logger.info(
                "Whole-document pages: %s",
                retrieved_pages,
            )

        # ====================================================
        # TARGETED DOCUMENT MODE
        # ====================================================

        else:

            # ------------------------------------------------
            # EXACT PAGE MODE
            # ------------------------------------------------

            requested_page = (
                extract_requested_page(
                    request.message,
                )
            )

            if requested_page is not None:

                logger.info(
                    "EXACT PAGE MODE — requested page %d",
                    requested_page,
                )

                exact_page_chunks = (
                    find_exact_page_chunks(
                        chunks,
                        requested_page,
                    )
                )

                page_exists = (
                    document_has_page(
                        document,
                        requested_page,
                    )
                )

                # ============================================
                # PAGE NOT FOUND
                # ============================================

                if (
                    not exact_page_chunks
                    or not page_exists
                ):

                    logger.info(
                        "Page %d not available in indexed document.",
                        requested_page,
                    )

                    retrieved_pages = []

                    user_content = (

                        "The user has asked about an exact "
                        "page in an uploaded document.\n\n"

                        f"REQUESTED PAGE: "
                        f"{requested_page}\n\n"

                        "The requested page is NOT available "
                        "in the indexed document context.\n\n"

                        "IMPORTANT:\n"
                        f"- Clearly tell the user that "
                        f"page {requested_page} is not "
                        "available in the uploaded document "
                        "context.\n"
                        "- Do NOT guess what is on the page.\n"
                        "- Do NOT use information from other "
                        "pages to answer the page-specific "
                        "question.\n"
                        "- Do NOT invent document content.\n"
                        "- Do NOT claim that the page exists "
                        "when it cannot be accessed.\n\n"

                        f"USER QUESTION:\n"
                        f"{request.message}"
                    )

                # ============================================
                # PAGE FOUND
                # ============================================

                else:

                    logger.info(
                        "Exact page chunks found: %d",
                        len(exact_page_chunks),
                    )

                    # ----------------------------------------
                    # Optimize only the requested page
                    # ----------------------------------------

                    optimized_chunks = (
                        context_optimizer.optimize(
                            exact_page_chunks,
                        )
                    )

                    # ----------------------------------------
                    # If optimizer removed everything,
                    # fall back to the exact chunks.
                    # ----------------------------------------

                    if not optimized_chunks:

                        optimized_chunks = (
                            exact_page_chunks
                        )

                    selected_context = (
                        context_optimizer.build_context(
                            optimized_chunks,
                        )
                    )

                    if not selected_context:

                        selected_context = (
                            "\n\n".join(
                                chunk.get(
                                    "text",
                                    "",
                                )
                                for chunk
                                in exact_page_chunks
                            )
                        )

                    retrieved_pages = [
                        requested_page,
                    ]

                    source_instruction = (
                        "The answer must be based ONLY "
                        f"on page {requested_page} "
                        "of the uploaded document."
                    )

                    # ----------------------------------------
                    # Memory
                    # ----------------------------------------

                    memory_section = ""

                    if memory_context:

                        memory_section = (
                            "\n\n"
                            f"{wrap_untrusted('Source: GHOST long-term memory', memory_context)}\n"
                            "Use memory only when it is "
                            "relevant to the user's question. "
                            "Do not use memory to replace "
                            "missing page content.\n"
                        )

                    # ----------------------------------------
                    # Exact page prompt
                    # ----------------------------------------

                    user_content = (

                        "You are analyzing an uploaded document.\n\n"

                        f"The user explicitly requested "
                        f"information from page "
                        f"{requested_page}.\n\n"

                        "EXACT PAGE CONTEXT:\n"
                        "================================\n"

                        f"{wrap_untrusted(f'Source: uploaded document, page {requested_page}', selected_context)}\n"

                        "================================\n\n"

                        f"{source_instruction}\n"

                        f"{memory_section}\n"

                        "IMPORTANT:\n"
                        "- Use ONLY the exact page context "
                        "provided above.\n"
                        "- Do not use information from other "
                        "document pages.\n"
                        "- Do not guess or invent missing "
                        "content.\n"
                        "- Answer the user's exact page "
                        "question directly.\n"
                        "- If the requested information is "
                        "not visible in the supplied page "
                        "context, say that it is not available "
                        "in the page context.\n"
                        f"- Treat page {requested_page} as the "
                        "only authoritative document page "
                        "for this request.\n\n"

                        f"USER QUESTION:\n"
                        f"{request.message}"
                    )

                    logger.info(
                        "Exact page source: [%d]",
                        requested_page,
                    )

            # ------------------------------------------------
            # NORMAL SEMANTIC RETRIEVAL
            # ------------------------------------------------

            else:

                relevant_chunks = (
                    document_retriever.retrieve(
                        request.message,
                        chunks,
                    )
                )

                optimized_chunks = (
                    context_optimizer.optimize(
                        relevant_chunks,
                    )
                )

                selected_context = (
                    context_optimizer.build_context(
                        optimized_chunks,
                    )
                )

                if not selected_context:

                    selected_context = (
                        "No relevant section of the "
                        "uploaded document was found "
                        "for this question."
                    )

                # --------------------------------------------
                # Collect source pages
                # --------------------------------------------

                retrieved_pages = []

                for chunk in optimized_chunks:

                    start_page, end_page = (
                        get_chunk_page_range(
                            chunk,
                        )
                    )

                    if (
                        start_page is None
                        or end_page is None
                    ):
                        continue

                    for page in range(
                        start_page,
                        end_page + 1,
                    ):

                        if (
                            page
                            not in retrieved_pages
                        ):

                            retrieved_pages.append(
                                page,
                            )

                retrieved_pages.sort()

                # --------------------------------------------
                # Source instruction
                # --------------------------------------------

                if retrieved_pages:

                    page_text = ", ".join(
                        str(page)
                        for page
                        in retrieved_pages
                    )

                    source_instruction = (
                        "The relevant information was "
                        f"found on page(s): "
                        f"{page_text}."
                    )

                else:

                    source_instruction = (
                        "The page number of the relevant "
                        "information could not be determined."
                    )

                # --------------------------------------------
                # Memory
                # --------------------------------------------

                memory_section = ""

                if memory_context:

                    memory_section = (
                        "\n\n"
                        f"{wrap_untrusted('Source: GHOST long-term memory', memory_context)}\n"
                        "Use this memory only when it "
                        "adds relevant continuity. "
                        "The uploaded document remains "
                        "the primary source for document facts.\n"
                    )

                # --------------------------------------------
                # Normal targeted prompt
                # --------------------------------------------

                user_content = (

                    "You are analyzing an uploaded document.\n\n"

                    "Answer the user's question using the "
                    "relevant document context provided below.\n\n"

                    "RELEVANT DOCUMENT CONTEXT:\n"
                    "--------------------------------\n"

                    f"{wrap_untrusted('Source: uploaded document (relevant sections)', selected_context)}\n"

                    "--------------------------------\n\n"

                    f"{source_instruction}\n"

                    f"{memory_section}\n"

                    "IMPORTANT:\n"
                    "- Give a clear and detailed answer.\n"
                    "- Do not invent information.\n"
                    "- If the answer cannot be found in the "
                    "provided document context, clearly say "
                    "that it was not found.\n"
                    "- Use the uploaded document as the "
                    "primary source of truth for document facts.\n"
                    "- Use GHOST memory only when it adds "
                    "relevant continuity.\n\n"

                    f"USER QUESTION:\n"
                    f"{request.message}"
                )

                logger.info(
                    "Document=%s | Chunks=%d | Retrieved=%d | "
                    "Optimized=%d | Pages=%s",
                    document.get("filename", "Unknown"),
                    len(chunks),
                    len(relevant_chunks),
                    len(optimized_chunks),
                    retrieved_pages,
                )

    # ========================================================
    # NO DOCUMENT MODE
    # ========================================================

    else:

        retrieved_pages = []

        if memory_context:

            user_content = (

                "Use the relevant long-term memory "
                "below when it helps maintain continuity.\n\n"

                "RELEVANT LONG-TERM MEMORY:\n"
                "--------------------------------\n"

                f"{wrap_untrusted('Source: GHOST long-term memory', memory_context)}\n"

                "--------------------------------\n\n"

                "IMPORTANT:\n"
                "- Use memory only when relevant.\n"
                "- Do not assume every memory is correct "
                "if it conflicts with the current request.\n"
                "- The current user request has priority.\n\n"

                f"CURRENT USER REQUEST:\n"
                f"{request.message}"
            )

        else:

            user_content = (

                "Answer the user's request directly, "
                "clearly, and naturally.\n\n"

                f"CURRENT USER REQUEST:\n"
                f"{request.message}"
            )

    # ========================================================
    # ASSEMBLE MESSAGES (system prompt + history + request)
    # ========================================================

    request_messages = build_request_messages(
        user_content=user_content,
        history=request.history,
    )
    logger.info(
    "Chat payload: messages=%d chars=%d",
    len(request_messages),
    sum(len(str(item.get("content", ""))) for item in request_messages),
)

    # ========================================================
    # STREAM RESPONSE
    # ========================================================

    async def generate_response():

        response_text = ""

        try:

            # ------------------------------------------------
            # Special deterministic page-not-found response
            # ------------------------------------------------

            requested_page = (
                extract_requested_page(
                    request.message,
                )
            )

            page_chunks = []

            if (
                use_document
                and requested_page is not None
            ):

                document_for_page_check = (
                    documents.get(
                        request.document_id,
                    )
                )

                if document_for_page_check:

                    page_chunks = (
                        find_exact_page_chunks(
                            document_for_page_check.get(
                                "chunks",
                                [],
                            ),
                            requested_page,
                        )
                    )

            if (
                use_document
                and requested_page is not None
                and not page_chunks
            ):

                # --------------------------------------------
                # No source pages are sent here.
                # --------------------------------------------

                yield (
                    "GHOST could not access "
                    f"page {requested_page} "
                    "in the uploaded document context.\n\n"
                    "I will not guess or use another page "
                    "to answer a page-specific question."
                )

                return

            # ------------------------------------------------
            # Source pages are delivered via the
            # X-Ghost-Sources response header (M2) — the
            # old in-band __SOURCES__ string could be
            # spoofed by document content (threat T7).
            # ------------------------------------------------

            # ------------------------------------------------
            # Generate AI response
            # ------------------------------------------------

            generate_kwargs = {
                "max_tokens": 2048,
                "temperature": 0.0,
            }

            if provider_name == "nemotron":
                generate_kwargs["chat_template_kwargs"] = {
                    "enable_thinking": False,
                }

            async for chunk in (
                provider.generate_stream(
                    messages=request_messages,

                    model=chat_model,

                    **generate_kwargs,
                )
            ):

                yield chunk

                if chunk is not None:

                    response_text += str(
                        chunk,
                    )

            # ------------------------------------------------
            # Save useful memory
            # ------------------------------------------------

            if response_text.strip():

                save_conversation_memory(
                    user_message=request.message,
                    assistant_response=response_text,
                    document_id=(
                        request.document_id
                        if use_document
                        else None
                    ),
                )

        except Exception as error:

            # Log the real error server-side, but scrub it
            # first: provider exception text (and tracebacks)
            # can carry the request URL with an embedded
            # API key (e.g. Gemini's "?key=..."). The client
            # only ever sees the generic failure text below —
            # never exception content (defect D10 / threat T5c).
            logger.error(
                "Chat generation failed: %s: %s",
                type(error).__name__,
                redact_text(str(error)),
            )

            yield (
                "\n\nGHOST could not complete this "
                "response. Check the server logs for "
                "details."
            )

    # ========================================================
    # RETURN STREAM
    # ========================================================

    # Citation pages travel in a response header the
    # document content cannot influence.
    stream_headers = {}

    if (
        use_document
        and retrieved_pages
    ):

        stream_headers["X-Ghost-Sources"] = ",".join(
            map(
                str,
                retrieved_pages,
            )
        )

    return StreamingResponse(
        generate_response(),
        media_type="text/plain",
        headers=stream_headers,
    )
