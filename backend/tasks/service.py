"""
GHOST — production task service (M3-H step 2).

In-process store around the existing Task model
(backend/core/task.py). The service owns task
lifecycle bookkeeping only: no execution, no
approval, no planning, no persistence — those live
elsewhere (Agent/AutomationEngine now; approval and
planner in later steps).

Dependency direction is one-way:
    backend/tasks/service.py -> backend/core/task.py
Never the reverse.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.core.task import (
    Task,
    TaskStatus,
    can_transition,
    MAX_TASK_RETRIES,
)


class TaskService:
    """
    Minimal synchronous task store around the existing Task
    model, with optional JSONL persistence (M4).

    Keys are Task.id (UUID4 strings assigned by the
    model's default_factory, so every stored task has
    a unique identity by construction).

    When ``persistence`` is provided (or the default store is
    constructible), tasks are reloaded from disk at startup and
    every mutation is appended, so task history survives a
    process restart. Persistence is strictly bookkeeping: it
    never validates, decides or executes anything.
    """

    def __init__(
        self,
        persistence: Optional["TaskPersistence"] = None,
        persistent: bool = False,
    ):
        self._tasks: Dict[str, Task] = {}

        if persistent:
            # Default store (env GHOST_TASKS_PATH or the
            # backend/data convention) — used by production
            # wiring; plain TaskService() stays in-memory.
            if persistence is None:
                from backend.tasks.persistence import (
                    TaskPersistence,
                )

                persistence = TaskPersistence()

            self._persistence = persistence
            self._load_persisted()
        else:
            # In-memory only: direct constructions (tests,
            # ad-hoc use) keep the exact historical semantics.
            self._persistence = None

    def _load_persisted(self) -> None:
        for task_id, task in self._persistence.load().items():
            self._tasks[task_id] = task

        self._reconcile_recovered()

    def _reconcile_recovered(self) -> None:
        """
        Startup reconciliation: a task persisted as RUNNING was
        interrupted by a process restart — its in-memory workflow
        steps are gone and it can never complete. Mark it FAILED
        with an explicit recovery reason (via the validated state
        machine, RUNNING -> FAILED) so it can be retried like any
        other failed task. Never reported as COMPLETED.
        """

        for task in list(self._tasks.values()):
            if task.status is not TaskStatus.RUNNING:
                continue

            task.error = (
                "Task was RUNNING when the previous process "
                "exited; recovered as FAILED on startup. "
                "In-flight workflow steps were not persisted."
            )

            try:
                self.mark_failed(task, task.error)
            except ValueError:
                # State machine disagreement must never break
                # startup; leave the task inspectable as loaded.
                pass

    def _persist(self, task: Task) -> None:
        if self._persistence is not None:
            self._persistence.record(task)

    def create(
        self,
        title: str,
        description: str = "",
        owner: str | None = None,
    ) -> Task:
        """
        Create and store a new Task. Returns the task.

        ``owner`` is the hashed session identity of the
        authenticated creator (None for legacy/in-process
        tasks, which stay visible to every session).
        """

        if not title or not title.strip():
            raise ValueError("Task title cannot be empty.")

        task = Task(
            title=title.strip(),
            description=description or "",
            owner=owner,
        )

        self._tasks[task.id] = task

        self._persist(task)

        return task

    def get(self, task_id: str, owner: str | None = None) -> Task:
        """
        Return the task for a known ID. Unknown IDs raise
        KeyError. When ``owner`` is given, a task owned by a
        DIFFERENT session also raises KeyError — callers report
        the same 404 either way, so the existence of another
        user's task is never revealed.
        """

        if task_id not in self._tasks:
            raise KeyError(f"Task '{task_id}' not found.")

        task = self._tasks[task_id]

        if (
            owner is not None
            and task.owner is not None
            and task.owner != owner
        ):
            raise KeyError(f"Task '{task_id}' not found.")

        return task

    def list(self, owner: str | None = None) -> List[Task]:
        """
        Return stored tasks ordered by creation time
        (deterministic; ties broken by id).

        With ``owner``, only that owner's tasks plus legacy
        owner-less tasks are returned.
        """

        return sorted(
            (
                task
                for task in self._tasks.values()
                if task.owner is None or owner is None
                or task.owner == owner
            ),
            key=lambda task: (task.created_at, task.id),
        )

    def count(self) -> int:
        return len(self._tasks)

    # --------------------------------------------------------
    # Lifecycle (M3): every status change is validated against
    # the explicit state machine; timestamps and audit-friendly
    # fields are maintained here, never at call sites.
    # --------------------------------------------------------

    def _transition(
        self,
        task: Task,
        new_status: TaskStatus,
    ) -> Task:
        if not can_transition(task.status, new_status):
            raise ValueError(
                f"Invalid task transition: "
                f"{task.status.value} -> {new_status.value}."
            )

        task.status = new_status
        task.updated_at = datetime.now()

        if new_status is TaskStatus.RUNNING:
            task.started_at = task.started_at or datetime.now()
        elif new_status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ):
            task.completed_at = datetime.now()

        self._persist(task)

        return task

    def mark_started(self, task: Task) -> Task:
        return self._transition(task, TaskStatus.RUNNING)

    def mark_completed(self, task: Task, result: Any = None) -> Task:
        task.result = result
        return self._transition(task, TaskStatus.COMPLETED)

    def mark_failed(self, task: Task, error: str) -> Task:
        task.error = error
        return self._transition(task, TaskStatus.FAILED)

    def cancel(
        self,
        task_id: str,
        owner: str | None = None,
    ) -> Task:
        """
        Cancel a PENDING or RUNNING task. Ownership rules match
        get(). CANCELLED is terminal; a task whose workflow has
        already reached a terminal state cannot be cancelled.
        """

        task = self.get(task_id, owner=owner)

        if task.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ):
            raise ValueError(
                f"Task '{task_id}' is {task.status.value} "
                "and cannot be cancelled."
            )

        if task.status is TaskStatus.RUNNING:
            # Cooperative cancellation: the flag survives the
            # engine's per-step status restoration, so the
            # workflow stops at the next step boundary. The
            # step currently executing runs to completion
            # (documented limitation — no mid-step interruption).
            task.cancel_requested = True

        return self._transition(task, TaskStatus.CANCELLED)

    def retry(
        self,
        task_id: str,
        owner: str | None = None,
        max_retries: int = MAX_TASK_RETRIES,
    ) -> Task:
        """
        Controlled retry: only a FAILED task may be re-queued,
        up to max_retries times. retry_count is tracked on the
        task so the limit is auditable.
        """

        task = self.get(task_id, owner=owner)

        if task.status is not TaskStatus.FAILED:
            raise ValueError(
                f"Task '{task_id}' is {task.status.value}; "
                "only FAILED tasks can be retried."
            )

        if task.retry_count >= max_retries:
            raise ValueError(
                f"Task '{task_id}' exceeded the retry limit "
                f"({task.retry_count}/{max_retries})."
            )

        task.retry_count += 1
        task.error = None
        task.completed_at = None
        return self._transition(task, TaskStatus.PENDING)
