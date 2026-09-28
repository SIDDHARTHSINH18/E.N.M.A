"""
GHOST — summarization tool (M4).

summarize: condense one text through the model gateway.

This is deliberately a thin, SAFE composition over the
existing model_generate tool — the only model-facing seam.
No provider is imported or called here directly, so provider
routing, redaction and audit hooks all behave exactly as for
any other model_generate step.

Params are untrusted: lengths are bounded before the call.
"""

from backend.tools.builtin.model import (
    MAX_PROMPT_CHARS,
    ModelToolError,
    model_generate,
)

MAX_INPUT_CHARS = 6000

MAX_OUTPUT_CHARS = 8000

_SUMMARY_INSTRUCTION = (
    "Summarize the following text. Preserve the key facts, "
    "names and numbers. Be concise and factual; do not add "
    "information that is not present in the text.\n\nTEXT:\n"
)


class SummarizeToolError(ModelToolError):
    """Raised for invalid params or a failed gateway call."""


async def summarize(params: dict) -> str:
    """
    Summarize one text with the configured model.

    params: {"text": <required text>,
             "max_words": <optional int hint>}

    Returns the summary text. Failures surface as
    ModelToolError, identical to model_generate steps.
    """

    params = params if isinstance(params, dict) else {}

    text = params.get("text")

    if not isinstance(text, str) or not text.strip():
        raise SummarizeToolError(
            "summarize requires a 'text' parameter."
        )

    text = text.strip()

    if len(text) > MAX_INPUT_CHARS:
        raise SummarizeToolError(
            f"summarize 'text' is too long "
            f"({len(text)} chars; limit {MAX_INPUT_CHARS})."
        )

    max_words = params.get("max_words")

    if max_words is not None:
        if not isinstance(max_words, int) or isinstance(
            max_words, bool
        ):
            raise SummarizeToolError(
                "summarize 'max_words' must be an integer."
            )
        if max_words < 10 or max_words > 2000:
            raise SummarizeToolError(
                "summarize 'max_words' must be between "
                "10 and 2000."
            )

    prompt = _SUMMARY_INSTRUCTION + text

    if max_words is not None:
        prompt += f"\n\nLimit the summary to about {max_words} words."

    if len(prompt) > MAX_PROMPT_CHARS:
        raise SummarizeToolError(
            f"summarize input is too long "
            f"({len(prompt)} chars assembled; limit "
            f"{MAX_PROMPT_CHARS})."
        )

    summary = await model_generate({"prompt": prompt})

    if len(summary) > MAX_OUTPUT_CHARS:
        summary = (
            summary[:MAX_OUTPUT_CHARS]
            + "...[truncated]"
        )

    return summary
