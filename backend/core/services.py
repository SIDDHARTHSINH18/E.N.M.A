"""
GHOST — shared service singletons.

One instance of each long-lived service for the whole
backend. Always import these instead of constructing
new instances: two MemoryService objects pointing at
the same file caused silent lost-update races
(docs/01-architecture-assessment.md, defect D3).
"""

import os

from dotenv import load_dotenv

from backend.core.context_optimizer import ContextOptimizer
from backend.core.document_processor import DocumentProcessor
from backend.core.document_summarizer import DocumentSummarizer
from backend.core.memory import MemoryService
from backend.core.orchestrator import Orchestrator
from backend.core.retriever import DocumentRetriever
from backend.providers.openai_compatible import (
    OpenAICompatibleProvider,
)
from backend.providers.gemini import GeminiProvider
from backend.providers.huggingface import HuggingFaceProvider


# The project .env is the intended source of truth for provider
# credentials: override=True makes a stale/mismatched OS-level
# NVIDIA_API_KEY (Windows user environment) lose to the .env value
# instead of silently winning via python-dotenv's default behavior.
#
# Packaged runtime: the desktop layer points ENMA_CONFIG_PATH at
# the user's private configuration file (created on first run in
# the app's user-data directory — never inside the install tree,
# never shipped in the installer). It is loaded first so a dev
# .env in the working directory keeps precedence in development.
_config_env = os.getenv("ENMA_CONFIG_PATH")

# Dev .env first (found via the working directory, walking up),
# then the packaged user configuration LAST so it is
# authoritative for the packaged runtime.
load_dotenv(override=True)

if _config_env and os.path.isfile(_config_env):
    load_dotenv(_config_env, override=True)

# Metadata-only startup diagnostic: WHICH source supplied the
# auth credential, never its value. Lets a user tell whether a
# packaged install is using its private config or a development
# .env without exposing anything sensitive.
import logging
import sys

logging.getLogger("ghost.config").info(
    "Config source: ENMA_CONFIG_PATH=%s; "
    "config present=%s; "
    "GHOST_AUTH_PASSWORD source=%s; "
    "backend pid=%s; backend exe=%s; cwd=%s",
    (
        os.path.abspath(_config_env)
        if _config_env
        else "<unset>"
    ),
    bool(_config_env and os.path.isfile(_config_env)),
    "packaged user config"
    if (
        _config_env
        and os.path.isfile(_config_env)
        and bool(os.getenv("GHOST_AUTH_PASSWORD"))
    )
    else "development .env / environment",
    os.getpid(),
    sys.executable,
    os.getcwd(),
)


DEFAULT_NVIDIA_BASE_URL = (
    "https://integrate.api.nvidia.com/v1"
)

DEFAULT_NVIDIA_MODEL = (
    "nvidia/nemotron-3.5-lightning-30b-a3b"
)


# ============================================================
# SINGLETON SERVICES
# ============================================================
#
# GHOST_MEMORY_PATH lets tests redirect the memory store
# away from the real backend/data/memory.json.

memory_service = (
    MemoryService(
        storage_path=os.environ["GHOST_MEMORY_PATH"],
    )
    if os.getenv("GHOST_MEMORY_PATH")
    else MemoryService()
)

document_processor = DocumentProcessor(
    chunk_size=1500,
    chunk_overlap=200,
)

document_retriever = DocumentRetriever(
    top_k=5,
)

context_optimizer = ContextOptimizer(
    max_words=4500,
)

orchestrator = Orchestrator(
    default_provider="gemini",
)


# ============================================================
# DEFAULT PROVIDER (NVIDIA, OpenAI-compatible)
# ============================================================

nvidia_base_url = os.getenv(
    "NVIDIA_BASE_URL",
    DEFAULT_NVIDIA_BASE_URL,
)

nvidia_model = os.getenv(
    "NVIDIA_MODEL",
    DEFAULT_NVIDIA_MODEL,
)

orchestrator.register_provider(
    "nemotron",
    OpenAICompatibleProvider(
        name="nemotron",
        base_url=nvidia_base_url,
        api_key=os.getenv(
            "NVIDIA_API_KEY",
        ),
        default_model=nvidia_model,
    ),
)


# ============================================================
# GEMINI PROVIDER
# ============================================================


# ============================================================
# GROQ PROVIDER (OpenAI-compatible)
# ============================================================
#
# Same OpenAI-compatible abstraction as Nemotron: Groq's API is
# wire-compatible with /chat/completions (including SSE deltas).
# Groq requires an explicit model on every request.

groq_api_key = os.getenv(
    "GROQ_API_KEY",
)

groq_base_url = os.getenv(
    "GROQ_BASE_URL",
    "https://api.groq.com/openai/v1",
)

groq_model = os.getenv(
    "GROQ_MODEL",
    "llama-3.1-8b-instant",
)

orchestrator.register_provider(
    "groq",
    OpenAICompatibleProvider(
        name="groq",
        base_url=groq_base_url,
        api_key=groq_api_key,
        default_model=groq_model,
    ),
)

orchestrator.register_provider(
    "gemini",
    GeminiProvider(),
)


# ============================================================
# HUGGING FACE PROVIDER (OPTIONAL, M-router)
# ============================================================
#
# Optional adapter only: with no HF_API_KEY/HUGGINGFACE_API_KEY
# configured, the provider raises an honest UNCONFIGURED
# diagnostic before any network call and the model router
# classifies it PROVIDER_UNAVAILABLE and skips it. Gemini,
# Groq and NVIDIA remain the primary providers.

orchestrator.register_provider(
    "huggingface",
    HuggingFaceProvider(),
)


document_summarizer = DocumentSummarizer(
    provider=orchestrator.get_provider(
        "nemotron",
    ),
    model=nvidia_model,
    chunk_batch_size=5,
)


# ============================================================
# DOCUMENT REGISTRY (per-process, in-memory)
# ============================================================
#
# Uploaded documents live here until the process exits.
# upload.py writes, chat.py reads, graph.py reads.
# Sharing one registry (instead of importing the dict
# from upload.py) keeps that dependency explicit;
# persistence and eviction arrive in M1.

documents: dict = {}


def provider_status(provider_name: str | None = None) -> dict:
    """
    Report provider configuration without secrets.

    ``provider_name`` selects a specific registered provider
    (so /health/provider can report the provider chat actually
    uses); None reports the orchestrator's default. Reads the
    environment live so startup validation and /health reflect
    the current process state.
    """

    # The active provider is the requested one, falling back to
    # the orchestrator's default — never a hardcoded name.
    default_provider = (
        provider_name
        if provider_name and provider_name in orchestrator.providers
        else orchestrator.default_provider
    )

    if default_provider == "groq":
        return {
            "provider": "groq",
            "api_key_present": bool(os.getenv("GROQ_API_KEY")),
            "base_url": groq_base_url,
            "model": groq_model,
        }

    if default_provider == "huggingface":
        return {
            "provider": "huggingface",
            "api_key_present": bool(
                os.getenv("HF_API_KEY")
                or os.getenv("HUGGINGFACE_API_KEY")
            ),
            "base_url": os.getenv(
                "HF_BASE_URL",
                "https://router.huggingface.co/v1",
            ),
            "model": os.getenv(
                "HF_MODEL",
                "meta-llama/Llama-3.1-8B-Instruct",
            ),
        }

    if default_provider == "gemini":
        return {
            "provider": "gemini",
            "api_key_present": bool(
                os.getenv("GEMINI_API_KEY"),
            ),
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "model": os.getenv(
                "GEMINI_MODEL",
                "gemini-3.5-flash",
            ),
        }

    return {
        "provider": "nemotron",
        "api_key_present": bool(
            os.getenv("NVIDIA_API_KEY"),
        ),
        "base_url": os.getenv(
            "NVIDIA_BASE_URL",
            DEFAULT_NVIDIA_BASE_URL,
        ),
        "model": os.getenv(
            "NVIDIA_MODEL",
            DEFAULT_NVIDIA_MODEL,
        ),
    }