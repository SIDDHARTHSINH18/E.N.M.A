"""
GHOST — task persistence (M4).

A small append-friendly JSONL store behind TaskService, so the
task history survives a process restart. Design constraints:

- One storage file for all tasks, defaulting to
  ``backend/data/tasks.jsonl`` following the same
  __file__-relative convention as memory.json and audit.jsonl.
  The GHOST_TASKS_PATH environment variable redirects it
  (tests and the packaged desktop runtime set it to a
  user-writable location).
- Format is JSON Lines: one task per line, whole-line rewrite
  is avoided by only appending changed tasks; the newest line
  for an id wins on load. Rotation is unnecessary at task
  volumes, but corrupt lines are skipped, never raised.
- Only task bookkeeping persists: identity, lifecycle state,
  result/error, timestamps, retry counts, reflection summary.
  Workflow steps stay in-memory (the audit JSONL remains the
  durable step record), so a restarted task can be inspected
  and audited but not resumed mid-workflow.
- Fail-safe: a failing store never breaks task execution.
  Serialization errors are swallowed with a warning for the
  same reason the audit log never raises into a task.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

from backend.core.task import Task, TaskStatus

TASKS_PATH_ENV = "GHOST_TASKS_PATH"


class TaskPersistence:
    """JSONL-backed store: load() once, record() per change."""

    def __init__(self, path: str | None = None):
        # Truthfulness: the last failed append is recorded here so
        # callers can detect that state was NOT persisted. Best-
        # effort by design (a failing store never breaks task
        # execution), but never silently misrepresented as success.
        self.last_write_error: str | None = None

        if path:
            self.path = Path(path)
        else:
            override = os.getenv(TASKS_PATH_ENV, "").strip()

            if override:
                self.path = Path(override)
            else:
                # Same repo-root convention as
                # backend/core/memory.py and backend/audit/log.py.
                self.path = Path(
                    os.path.abspath(
                        os.path.join(
                            os.path.dirname(__file__),
                            "..",
                            "..",
                            "backend",
                            "data",
                            "tasks.jsonl",
                        )
                    )
                )

    # --------------------------------------------------------
    # Serialization
    # --------------------------------------------------------

    @staticmethod
    def to_record(task: Task) -> dict:
        """JSON-safe view of one task (bookkeeping only)."""

        reflection = task.reflection

        if reflection is not None and hasattr(
            reflection, "to_dict"
        ):
            reflection = reflection.to_dict()

        return {
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "status": task.status.value,
            "priority": task.priority,
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
            "started_at": (
                task.started_at.isoformat()
                if task.started_at
                else None
            ),
            "completed_at": (
                task.completed_at.isoformat()
                if task.completed_at
                else None
            ),
            "result": task.result,
            "error": task.error,
            "retry_count": task.retry_count,
            "cancel_requested": task.cancel_requested,
            "owner": task.owner,
            "metadata": task.metadata,
            "reflection": reflection,
        }

    @staticmethod
    def from_record(record: dict) -> Task | None:
        """Rebuild one Task from a record. Corrupt records
        return None and are skipped by load()."""

        try:
            status = TaskStatus(record["status"])

            task = Task(
                title=str(record.get("title", "")),
                description=str(record.get("description", "")),
                id=str(record["id"]),
                status=status,
                priority=int(record.get("priority", 1)),
                created_at=datetime.fromisoformat(
                    record["created_at"]
                ),
                updated_at=datetime.fromisoformat(
                    record["updated_at"]
                ),
                started_at=(
                    datetime.fromisoformat(record["started_at"])
                    if record.get("started_at")
                    else None
                ),
                completed_at=(
                    datetime.fromisoformat(record["completed_at"])
                    if record.get("completed_at")
                    else None
                ),
                result=record.get("result"),
                error=record.get("error"),
                retry_count=int(record.get("retry_count", 0)),
                cancel_requested=bool(
                    record.get("cancel_requested", False)
                ),
                owner=record.get("owner"),
                metadata=dict(record.get("metadata") or {}),
                # Reflection is restored read-only as a plain
                # dict: it is a post-execution artifact, never
                # re-executed after a restart.
                reflection=record.get("reflection"),
            )

            return task
        except (KeyError, ValueError, TypeError):
            return None

    # --------------------------------------------------------
    # I/O
    # --------------------------------------------------------

    def load(self) -> dict[str, Task]:
        """
        Return {task_id: Task} from disk. The newest line per
        id wins. Corrupt/unknown lines are skipped.
        """

        tasks: dict[str, Task] = {}

        try:
            with self.path.open(
                "r", encoding="utf-8"
            ) as handle:
                for line in handle:
                    line = line.strip()

                    if not line:
                        continue

                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    task = self.from_record(record)

                    if task is not None:
                        tasks[task.id] = task
        except FileNotFoundError:
            return {}
        except OSError:
            # An unreadable store must not break startup.
            return {}

        return tasks

    def record(self, task: Task) -> None:
        """Append one task record. Never raises."""

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)

            with self.path.open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write(
                    json.dumps(
                        self.to_record(task),
                        default=str,
                    )
                    + "\n"
                )

            self.last_write_error = None
        except (OSError, TypeError, ValueError) as error:
            # Persistence is best-effort by design — but the
            # failure is logged and exposed, never swallowed
            # into a fake success.
            self.last_write_error = (
                f"{type(error).__name__}: {error}"
            )
            logger.warning(
                "Task persistence write failed: %s",
                self.last_write_error,
            )
