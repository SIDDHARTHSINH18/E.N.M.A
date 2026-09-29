"""
P1 hardening regression tests.

Covers the audit's P1 findings:
1. Startup reconciliation — a task persisted as RUNNING must be
   recovered as FAILED with an explicit reason, never left
   RUNNING forever and never falsely completed.
2. Retry refusal — when the runner refuses a retried task, the
   API must surface a structured reason instead of silently
   returning execution=None.
"""

import json

import pytest

from backend.core.task import TaskStatus
from backend.tasks.persistence import TaskPersistence
from backend.tasks.service import TaskService


# ============================================================
# 1. RUNNING-task startup reconciliation
# ============================================================

def _write_record(path, **overrides):
    """Write one minimal valid task record in JSONL form."""

    record = {
        "id": "t-running",
        "title": "Interrupted task",
        "description": "",
        "status": "RUNNING",
        "priority": 1,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "started_at": "2026-01-01T00:00:01",
        "completed_at": None,
        "result": None,
        "error": None,
        "retry_count": 0,
        "cancel_requested": False,
        "owner": None,
        "metadata": {},
        "reflection": None,
    }
    record.update(overrides)

    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def test_running_task_recovered_as_failed_on_startup(tmp_path):
    tasks_path = tmp_path / "tasks.jsonl"
    _write_record(str(tasks_path))
    _write_record(
        str(tasks_path),
        id="t-pending",
        title="Queued task",
        status="PENDING",
        started_at=None,
    )
    _write_record(
        str(tasks_path),
        id="t-completed",
        title="Done task",
        status="COMPLETED",
        completed_at="2026-01-01T00:00:05",
    )

    service = TaskService(
        persistence=TaskPersistence(str(tasks_path)),
        persistent=True,
    )

    recovered = service.get("t-running")
    assert recovered.status is TaskStatus.FAILED
    assert recovered.completed_at is not None
    assert "recovered as FAILED" in recovered.error
    assert "RUNNING" in recovered.error

    # Non-RUNNING tasks are untouched.
    assert service.get("t-pending").status is TaskStatus.PENDING
    assert service.get("t-completed").status is TaskStatus.COMPLETED


def test_recovered_task_is_retryable():
    """Reconciliation must make the task retryable: RUNNING
    tasks can never be retried; FAILED tasks can."""

    service = TaskService()  # in-memory

    task = service.create("Interrupted", "")
    service.mark_started(task)

    service._reconcile_recovered()

    assert task.status is TaskStatus.FAILED
    retried = service.retry(task.id)  # must not raise
    assert retried.status is TaskStatus.PENDING


# ============================================================
# 2. Retry refusal surfaces a structured reason
# ============================================================

from backend.api import tasks as tasks_api  # noqa: E402


class _RefusingPipeline:
    """Pipeline double whose runner refuses the re-execution."""

    def __init__(self, reason):
        self._reason = reason

    async def retry_execution(self, task_id):
        raise ValueError(self._reason)


def test_retry_refusal_returns_truthful_reason(
    auth_client, monkeypatch
):
    created = auth_client.post(
        "/api/tasks",
        json={"request": "Retry refusal probe"},
    )
    assert created.status_code == 201

    task_id = created.json()["task_id"]

    # Get the task into FAILED through the service, like a
    # failed execution would.
    task = tasks_api.task_service.get(task_id)
    tasks_api.task_service.mark_started(task)
    tasks_api.task_service.mark_failed(task, "boom")

    refusal = "Task has no recorded workflow to re-execute."
    monkeypatch.setattr(
        tasks_api,
        "build_pipeline",
        lambda: _RefusingPipeline(refusal),
    )

    response = auth_client.post(f"/api/tasks/{task_id}/retry")

    assert response.status_code == 200
    body = response.json()

    # The refusal must be visible, not silently collapsed to
    # execution=None.
    assert body["execution"] is not None
    assert body["execution"]["executed"] is False
    assert body["execution"]["reason"] == refusal
    assert body["status"] == "PENDING"  # re-queued by TaskService.retry
