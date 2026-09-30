"""
M3 task lifecycle: explicit state machine, cancellation,
controlled retry, ownership, and API behavior.
"""

import os

import pytest

from backend.core.task import Task, TaskStatus, can_transition
from backend.main import app
from backend.tasks.service import TaskService

from fastapi.testclient import TestClient


@pytest.fixture
def svc():
    return TaskService()


class TestStateMachine:

    def test_valid_transitions(self):
        assert can_transition(TaskStatus.PENDING, TaskStatus.RUNNING)
        assert can_transition(TaskStatus.PENDING, TaskStatus.CANCELLED)
        assert can_transition(TaskStatus.RUNNING, TaskStatus.COMPLETED)
        assert can_transition(TaskStatus.RUNNING, TaskStatus.FAILED)
        assert can_transition(TaskStatus.RUNNING, TaskStatus.CANCELLED)
        assert can_transition(TaskStatus.FAILED, TaskStatus.PENDING)

    def test_invalid_transitions(self):
        assert not can_transition(TaskStatus.COMPLETED, TaskStatus.RUNNING)
        assert not can_transition(TaskStatus.COMPLETED, TaskStatus.CANCELLED)
        assert not can_transition(TaskStatus.CANCELLED, TaskStatus.RUNNING)
        assert not can_transition(TaskStatus.PENDING, TaskStatus.COMPLETED)

    def test_service_rejects_invalid_transition(self, svc):
        task = svc.create("t", "")
        task.status = TaskStatus.COMPLETED

        with pytest.raises(ValueError, match="Invalid task transition"):
            svc.mark_started(task)


class TestLifecycleTimestamps:

    def test_started_and_completed_set(self, svc):
        task = svc.create("t", "")

        assert task.started_at is None
        assert task.completed_at is None

        svc.mark_started(task)

        assert task.started_at is not None

        svc.mark_completed(task, "done")

        assert task.completed_at is not None
        assert task.result == "done"

    def test_failure_records_error(self, svc):
        task = svc.create("t", "")
        svc.mark_started(task)
        svc.mark_failed(task, "boom")

        assert task.status is TaskStatus.FAILED
        assert task.error == "boom"
        assert task.completed_at is not None


class TestCancellation:

    def test_pending_task_cancels(self, svc):
        task = svc.create("t", "")

        cancelled = svc.cancel(task.id)

        assert cancelled.status is TaskStatus.CANCELLED
        assert cancelled.completed_at is not None

    def test_cancelled_is_terminal(self, svc):
        task = svc.create("t", "")
        svc.cancel(task.id)

        with pytest.raises(ValueError):
            svc.mark_started(task)

    def test_foreign_session_cannot_cancel(self, svc):
        task = svc.create("t", "", owner="hash-a")

        with pytest.raises(KeyError):
            svc.cancel(task.id, owner="hash-b")

    def test_engine_ignores_steps_after_cancellation(self):
        """A RUNNING task cancelled mid-workflow stops at the
        next step boundary — the cancel_requested flag survives
        the engine's per-step status restoration."""

        from backend.automation.engine import AutomationEngine, WorkflowState

        class Step:
            def __init__(self, name):
                self.id = name
                self.order = 0
                self.tool_name = name
                self.params = {}

        executed = []

        from backend.agents.executor import ExecutionResult
        from backend.permissions.policy import PermissionDecision

        def ok(task_id):
            return ExecutionResult(
                task_id=task_id,
                status=TaskStatus.COMPLETED,
                decision=PermissionDecision.ALLOW,
                reason="ok",
            )

        class Agent:
            def execute(self, task, tool, params):
                executed.append(tool)
                return ok(task.id)

            async def execute_async(self, task, tool, params):
                return ok(task.id)

        task = Task(title="t", description="")
        task.cancel_requested = True
        engine = AutomationEngine(agent=Agent())

        result = engine.run(task, [Step("tool-a"), Step("tool-b")])

        assert result.state is WorkflowState.CANCELLED
        assert executed == []  # no step ran after cancellation


class TestRetry:

    def test_failed_task_retries(self, svc):
        task = svc.create("t", "")
        svc.mark_started(task)
        svc.mark_failed(task, "boom")

        retried = svc.retry(task.id)

        assert retried.status is TaskStatus.PENDING
        assert retried.retry_count == 1
        assert retried.error is None

    def test_retry_only_failed_tasks(self, svc):
        task = svc.create("t", "")

        with pytest.raises(ValueError, match="only FAILED"):
            svc.retry(task.id)

    def test_retry_limit_enforced(self, svc):
        task = svc.create("t", "")
        task.status = TaskStatus.FAILED
        task.retry_count = 3

        with pytest.raises(ValueError, match="retry limit"):
            svc.retry(task.id)

    def test_foreign_session_cannot_retry(self, svc):
        task = svc.create("t", "", owner="hash-a")
        task.status = TaskStatus.FAILED

        with pytest.raises(KeyError):
            svc.retry(task.id, owner="hash-b")


class TestTaskLifecycleAPI:
    """API-level cancel/retry with two independent sessions."""

    @pytest.fixture
    def client(self):
        return TestClient(app)

    @pytest.fixture
    def fallback_planner(self, monkeypatch):
        """
        Force the planner's deterministic fallback (model gateway
        error) so created tasks are guaranteed PENDING with zero
        steps — the state cancel/retry tests need, without any
        nondeterministic model behavior.
        """

        import backend.core.agent_services as agent_services

        async def failing_generate(**kwargs):
            raise RuntimeError("planner gateway unavailable (test)")

        monkeypatch.setattr(
            agent_services.orchestrator, "generate", failing_generate
        )

    @pytest.fixture
    def sessions(self, client):
        pw = os.environ["GHOST_AUTH_PASSWORD"]
        ha = {
            "Authorization": "Bearer "
            + client.post("/api/auth/login", json={"password": pw}).json()["token"]
        }
        hb = {
            "Authorization": "Bearer "
            + client.post("/api/auth/login", json={"password": pw}).json()["token"]
        }
        return ha, hb

    def _create(self, client, headers, request):
        return client.post(
            "/api/tasks", headers=headers, json={"request": request}
        ).json()["task_id"]

    def test_owner_cancels_pending_task(self, client, sessions, fallback_planner):
        ha, hb = sessions

        task_id = self._create(client, ha, "organize my project notes")
        r = client.post(f"/api/tasks/{task_id}/cancel", headers=ha)

        assert r.status_code == 200, r.text
        assert r.json()["status"] == "CANCELLED"

    def test_foreign_cancel_is_404(self, client, sessions, fallback_planner):
        ha, hb = sessions

        task_id = self._create(client, ha, "read the file docs")

        assert (
            client.post(f"/api/tasks/{task_id}/cancel", headers=hb).status_code
            == 404
        )

    def test_foreign_retry_is_404(self, client, sessions, fallback_planner):
        ha, hb = sessions

        task_id = self._create(client, ha, "check whether tests exist")

        assert (
            client.post(f"/api/tasks/{task_id}/retry", headers=hb).status_code
            == 404
        )

    def test_cancel_audit_event_recorded(self, client, sessions, fallback_planner):
        from backend.audit.log import AuditStage
        from backend.core.agent_services import audit_log

        ha, _ = sessions
        task_id = self._create(client, ha, "list the files")

        client.post(f"/api/tasks/{task_id}/cancel", headers=ha)

        rows = audit_log.read(task_id=task_id, stage=AuditStage.LIFECYCLE)
        assert any(row["event"] == "task_cancelled" for row in rows)

    def test_retry_audit_event_recorded(self, client, sessions, fallback_planner):
        from backend.audit.log import AuditStage
        from backend.core.agent_services import audit_log

        ha, _ = sessions
        task_id = self._create(
            client, ha, "organize my project notes"
        )

        # Deterministically fail the task through the service's
        # own lifecycle (execution may or may not have run).
        from backend.core.agent_services import task_service
        from backend.core.task import TaskStatus

        task_service.mark_failed(
            task_service.get(task_id), "boom (test)"
        )

        r = client.post(f"/api/tasks/{task_id}/retry", headers=ha)

        assert r.status_code == 200
        assert r.json()["status"] == "PENDING"
        assert r.json()["retry_count"] == 1

        rows = audit_log.read(task_id=task_id, stage=AuditStage.LIFECYCLE)
        assert any(row["event"] == "task_retried" for row in rows)

    def test_cancelling_completed_task_is_409(self, client, sessions):
        ha, _ = sessions
        task_id = self._create(client, ha, "check whether README exists")

        from backend.core.agent_services import task_service
        from backend.core.task import TaskStatus as TS

        task_service.get(task_id).status = TS.COMPLETED

     
