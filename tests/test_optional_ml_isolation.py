"""
P0.1 — optional ML / packaged startup isolation.

Core ENMA startup must not require the optional ML stack
(sentence_transformers / sklearn / torch / scipy). On locked-down
machines a native scipy DLL can be blocked by Application Control;
a module-level import in backend.core.retriever used to kill the
whole backend for an OPTIONAL capability.
"""

import subprocess
import sys

import pytest

from backend.core.retriever import (
    DocumentRetriever,
    ML_STATE_UNAVAILABLE,
)


@pytest.fixture(autouse=True)
def _reset_ml_state():
    """Keep class-level ML state isolated between tests."""

    saved = (
        DocumentRetriever._embedding_model,
        DocumentRetriever._ml_state,
        DocumentRetriever._ml_unavailable_reason,
    )
    DocumentRetriever._embedding_model = None
    DocumentRetriever._ml_state = None
    DocumentRetriever._ml_unavailable_reason = None
    yield
    (
        DocumentRetriever._embedding_model,
        DocumentRetriever._ml_state,
        DocumentRetriever._ml_unavailable_reason,
    ) = saved


def test_backend_startup_does_not_import_optional_ml():
    """Importing the full backend must not pull in torch /
    sentence_transformers / sklearn — verified in a real
    subprocess so parent-process imports cannot interfere."""

    code = (
        "import backend.main, sys\n"
        "heavy = [m for m in sys.modules if m.split('.')[0] in "
        "('sentence_transformers', 'torch', 'sklearn', 'scipy')]\n"
        "assert not heavy, f'eager ML imports: {heavy}'\n"
        "print('CLEAN')\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert "CLEAN" in result.stdout


def test_ml_status_is_honest_when_unavailable():
    """When the ML stack cannot be imported, the retriever must
    report UNAVAILABLE with a real reason — not silently claim
    semantic retrieval is possible."""

    def broken_loader():
        raise ImportError(
            "DLL load failed: blocked by Application Control policy"
        )

    DocumentRetriever._load_sentence_transformer = staticmethod(
        broken_loader
    )

    with pytest.raises(Exception):
        DocumentRetriever._get_embedding_model()

    status = DocumentRetriever.ml_status()
    assert status["state"] is ML_STATE_UNAVAILABLE
    assert "sentence_transformers unavailable" in status["reason"]
    assert "Application Control" in status["reason"]


def test_retrieval_degrades_honestly_without_ml():
    """With the ML stack broken, retrieve() must still work on
    keyword/phrase evidence and must report semantic_score 0.0 —
    never fabricated semantic similarity."""

    def broken_loader():
        raise ImportError("blocked")

    DocumentRetriever._load_sentence_transformer = staticmethod(
        broken_loader
    )

    retriever = DocumentRetriever()
    chunks = [
        {"chunk_id": 0, "text": "ENMA is a personal AI operating system"},
        {"chunk_id": 1, "text": "The weather is nice today"},
    ]

    results = retriever.retrieve("ENMA operating system", chunks)

    assert results, "keyword retrieval must still match"
    top = results[0]
    assert top["chunk_id"] == 0
    assert top["semantic_score"] == 0.0
    assert top["keyword_score"] > 0.0
    assert DocumentRetriever.ml_status()["state"] in (
        None,
        ML_STATE_UNAVAILABLE,
    )


def test_create_embeddings_raises_honestly_without_ml():
    """create_embeddings must never fabricate embeddings: with
    the ML stack unavailable it raises instead of returning
    fake vectors."""

    def broken_loader():
        raise ImportError("blocked")

    DocumentRetriever._load_sentence_transformer = staticmethod(
        broken_loader
    )

    retriever = DocumentRetriever()

    with pytest.raises(Exception):
        retriever.create_embeddings(
            [{"chunk_id": 0, "text": "hello"}]
        )

    # ...and nothing was fabricated.
    assert DocumentRetriever._embedding_model is None
