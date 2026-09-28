"""
GHOST — memory bridge (M4 step 6).

Closes the loop the target architecture asks for:

  Result -> Reflection -> Memory

ReflectionEngine has produced ``memory_write_recommended``
since M3-G, and no component ever consumed it. This is that
consumer: one place that turns a reflection into a durable
memory, with the existing MemoryService as the only writer.

Rules enforced here:

- ONE MEMORY SYSTEM. Writes go through MemoryService.add_memory
  (which already deduplicates and can upgrade an existing
  entry's importance); nothing is appended to a file here.
- RECOMMENDED WRITES ONLY. Only a reflection the engine flagged
  is stored. A plain success stores nothing, so ordinary
  requests cannot fill memory — retrieved memory is injected
  into every later prompt.
- NOTHING INVENTED. The stored text is assembled only from
  fields reflection already derived (summary, causes, lessons,
  next action) plus the task title. No raw model output, tool
  parameters or document text is copied in, and the content is
  length-bounded.
- A MEMORY FAILURE IS NEVER A TASK FAILURE. add_memory errors
  are captured, audited and returned as a report; they do not
  propagate into the pipeline that just succeeded.
"""

from dataclasses import dataclass
from typing import List, Optional

from backend.audit.log import AuditLog, AuditStage, redact_text
from backend.core.memory import MemoryService
from backend.core.task import Task
from backend.reflection.result import ReflectionOutcome, ReflectionResult


# Retrieved memory goes back into the model context, so an
# unbounded reflection would be replayed on every future turn.
MAX_MEMORY_CHARS = 700

MAX_LIST_ITEMS = 3

MEMORY_TYPE = "task_outcome"

MEMORY_SOURCE = "reflection"

# Failures and denials are the lessons worth keeping; the number
# is a prior on how much a single outcome should weigh.
IMPORTANCE_BY_OUTCOME = {
    ReflectionOutcome.FAILED: 0.7,
    ReflectionOutcome.PARTIAL: 0.6,
    ReflectionOutcome.DENIED: 0.6,
}


@dataclass
class MemoryWrite:
    """
    Report of one bridge call. Always returned, even when
    nothing was written, so the caller can audit the outcome
    without inspecting the store.
    """

    attempted: bool = False
    written: bool = False
    memory_id: Optional[str] = None
    content: str = ""
    reason: str = ""
    error: Optional[str] = None

    @property
    def status(self) -> str:
        if self.written:
            return "written"

        if self.attempted:
            return "failed"

        return "skipped"

    def to_dict(self) -> dict:
        return {
            "attempted": self.attempted,
            "written": self.written,
            "status": self.status,
            "memory_id": self.memory_id,
            "content": self.content,
            "reason": self.reason,
            "error": self.error,
        }


class MemoryBridge:
    """Turn recommended reflections into durable memories."""

    def __init__(
        self,
        memory: MemoryService,
        audit: Optional[AuditLog] = None,
        project: Optional[str] = None,
    ):
        self._memory = memory
        self._audit = audit
        self._project = project

    # --------------------------------------------------------
    # WRITE PATH
    # --------------------------------------------------------

    def record_outcome(
        self,
        task: Task,
        reflection: Optional[ReflectionResult],
    ) -> MemoryWrite:
        """
        Store one memory when the reflection asks for it.

        Never raises: a store failure is reported in the
        returned MemoryWrite and the audit row.
        """

        write = self._evaluate(task, reflection)

        if write is None:
            report = MemoryWrite(reason="nothing recommended")
        else:
            report = self._store(task, reflection, write)

        self._audit_record(task, reflection, report)

        return report

    def record_research(
        self,
        task: Task,
        research_output: dict,
    ) -> MemoryWrite:
        """
        Store one bounded research memory (M5.9) through the
        same bridge, only for explicitly completed research
        with real sources.

        Rules:
        - never called for raw page dumps: the memory carries
          the topic, a short synthesis excerpt, and source
          DOMAINS (provenance), not page text
        - provenance is preserved in the record metadata so a
          future reader can trace which external sources
          produced this memory
        - never raises
        """

        sources = research_output.get("sources") or []

        domains = sorted(
            {
                str(s.get("domain", "")).strip()
                for s in sources
                if isinstance(s, dict)
                and s.get("domain")
            }
        )

        retrieved = [
            s
            for s in sources
            if isinstance(s, dict)
            and s.get("status") == "retrieved"
        ]

        if not retrieved:
            return MemoryWrite(
                reason="no retrieved sources; nothing to remember"
            )

        topic = str(research_output.get("topic", "")).strip()

        summary = str(
            research_output.get("summary", "")
        ).strip()

        parts = [f"Research completed on '{topic}'."]

        if summary:
            parts.append(
                "Synthesis: " + summary[:400]
            )

        if domains:
            parts.append("Sources: " + ", ".join(domains[:10]))

        content = redact_text(" ".join(parts))

        if len(content) > MAX_MEMORY_CHARS:
            content = (
                content[:MAX_MEMORY_CHARS].rstrip() + "..."
            )

        try:
            record = self._memory.add_memory(
                content=content,
                memory_type="research",
                importance=0.6,
                project=self._project,
                source=MEMORY_SOURCE,
                confidence=0.8,
                tags=["research", "external-sources"],
                metadata={
                    "task_id": task.id,
                    "task_title": task.title,
                    "origin": "research-workflow",
                    # Provenance: which external sources this
                    # memory derives from (URLs, not content).
                    "source_urls": [
                        str(s.get("url", ""))
                        for s in retrieved
                        if s.get("url")
                    ][:10],
                },
            )
        except Exception as exc:
            return MemoryWrite(
                attempted=True,
                content=content,
                error=f"{type(exc).__name__}: {exc}",
            )

        memory_id = (
            record.get("id") if isinstance(record, dict) else None
        )

        if not memory_id:
            return MemoryWrite(
                attempted=True,
                content=content,
                error="MemoryService returned no memory id.",
            )

        return MemoryWrite(
            attempted=True,
            written=True,
            memory_id=memory_id,
            content=record.get("content", content),
        )

    # --------------------------------------------------------
    # DECISION
    # --------------------------------------------------------

    def _evaluate(
        self,
        task: Task,
        reflection: Optional[ReflectionResult],
    ) -> Optional[str]:
        """Return the content to store, or None to skip."""

        if reflection is None:
            return None

        if not reflection.memory_write_recommended:
            return None

        if not reflection.failures:
            # The engine only sets the flag with failures
            # present; a hand-built result without evidence is
            # not worth a permanent memory.
            return None

        return self._content(task, reflection)

    def _content(
        self,
        task: Task,
        reflection: ReflectionResult,
    ) -> str:
        """
        Assemble the memory text from reflection output only.
        """

        parts: List[str] = [
            f"Task '{task.title}' ended "
            f"{reflection.outcome.value}.",
        ]

        if reflection.summary:
            parts.append(reflection.summary)

        causes = _clean_items(reflection.causes)

        if causes:
            parts.append("Cause: " + "; ".join(causes))

        lessons = _clean_items(reflection.lessons)

        if lessons:
            parts.append("Lesson: " + "; ".join(lessons))

        if reflection.recommended_next_action:
            parts.append(
                f"Next time: {reflection.recommended_next_action}"
            )

        content = " ".join(parts)

        if len(content) > MAX_MEMORY_CHARS:
            content = content[:MAX_MEMORY_CHARS].rstrip() + "..."

        # Memory persists across sessions and is re-injected into
        # future prompts: scrub credential-shaped text (model
        # reflections can quote tool output containing secrets)
        # before it leaves the write boundary. Mirrors the audit
        # log's sanitize-before-store guarantee.
        content = redact_text(content)

        return content.strip()

    # --------------------------------------------------------
    # STORAGE
    # --------------------------------------------------------

    def _store(
        self,
        task: Task,
        reflection: ReflectionResult,
        content: str,
    ) -> MemoryWrite:
        try:
            record = self._memory.add_memory(
                content=content,
                memory_type=MEMORY_TYPE,
                importance=IMPORTANCE_BY_OUTCOME.get(
                    reflection.outcome,
                    0.5,
                ),
                project=self._project,
                source=MEMORY_SOURCE,
                confidence=reflection.confidence,
                tags=_tags(reflection),
                metadata={
                    "task_id": task.id,
                    "task_title": task.title,
                    # Provenance: produced by a task's reflection,
                    # not typed by the user.
                    "origin": "task-execution",
                    "outcome": reflection.outcome.value,
                    "tools": _failing_tools(reflection),
                },
            )
        except Exception as exc:
            # A broken memory store must not fail the task.
            return MemoryWrite(
                attempted=True,
                content=content,
                error=f"{type(exc).__name__}: {exc}",
            )

        memory_id = record.get("id") if isinstance(record, dict) else None

        if not memory_id:
            return MemoryWrite(
                attempted=True,
                content=content,
                error="MemoryService returned no memory id.",
            )

        return MemoryWrite(
            attempted=True,
            written=True,
            memory_id=memory_id,
            content=record.get("content", content),
        )

    def _audit_record(
        self,
        task: Task,
        reflection: Optional[ReflectionResult],
        report: MemoryWrite,
    ) -> None:
        if self._audit is None:
            return

        # Best effort: the memory itself is already stored, and a
        # store that cannot take a row must not turn a completed
        # write into a failed request.
        try:
            self._audit.append(
                task_id=task.id,
                stage=AuditStage.MEMORY,
                event=report.status,
                status=(
                    reflection.outcome.value if reflection else None
                ),
                data={
                    "memory_id": report.memory_id,
                    "written": report.written,
                    "reason": report.reason,
                    "error": report.error,
                    "content": report.content,
                },
            )
        except Exception:
            pass


# --------------------------------------------------------
# HELPERS
# --------------------------------------------------------

def _clean_items(values: Optional[List[str]]) -> List[str]:
    if not values:
        return []

    return [
        str(value).strip()
        for value in list(values)[:MAX_LIST_ITEMS]
        if str(value).strip()
    ]


def _tags(reflection: ReflectionResult) -> List[str]:
    tags = ["reflection", reflection.outcome.value.lower()]

    tags.extend(
        f"tool:{name}" for name in _failing_tools(reflection)
    )

    return tags


def _failing_tools(
    reflection: ReflectionResult,
) -> List[str]:
    names: List[str] = []

    for failure in reflection.failures or []:
        name = failure.tool_name

        if name and name not in names:
            names.append(name)

    return names[:MAX_LIST_ITEMS]
