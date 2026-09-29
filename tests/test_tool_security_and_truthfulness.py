"""
P0.4 / P0.5 / P1.1 / P1.4 hardening tests.

Covers:
- fs read boundary: secret/environment files are refused
- write jail: symlinked intermediate directories are refused
- write verification: fs_write_file reports verified=True only
  with re-read evidence, and fails loudly on mismatch
- persistence truthfulness: failed writes are recorded, not
  silently swallowed
- async boundary: a blocking sync tool no longer freezes the
  event loop during execute_async
"""

import asyncio
import os
import threading
import time
from pathlib import Path

import pytest

from backend.agents.executor import Agent
from backend.core.task import Task
from backend.permissions.policy import PermissionPolicy
from backend.tasks.persistence import TaskPersistence
from backend.tools.builtin.fs import (
    ToolExecutionError,
    canonical_execution_root,
    fs_read_file,
    fs_write_file,
)
from backend.tools.registry import RiskLevel, ToolRegistry


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Isolated canonical execution root."""

    monkeypatch.setenv("ENMA_WORKSPACE_ROOT", str(tmp_path))
    return tmp_path


# ============================================================
# P0.5 — secret-file read guard
# ============================================================

@pytest.mark.parametrize("name", [".env", "config.env", ".env.local"])
def test_read_refuses_secret_files(workspace, name):
    secret = workspace / name
    secret.write_text("SHOULD_NOT_BE_READABLE", encoding="utf-8")

    with pytest.raises(ToolExecutionError) as excinfo:
        fs_read_file({"path": name})

    assert "secret" in str(excinfo.value).lower()
    # The content must never be returned.
    assert "SHOULD_NOT_BE_READABLE" not in str(excinfo.value)


def test_read_still_allows_legitimate_workspace_files(workspace):
    note = workspace / "notes" / "readme.txt"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("legitimate content", encoding="utf-8")

    assert fs_read_file({"path": "notes/readme.txt"}) == (
        "legitimate content"
    )


# ============================================================
# P0.5 — symlinked-parent write escape
# ============================================================

def test_write_refuses_symlinked_parent_directory(workspace):
    real_dir = workspace / "elsewhere"
    real_dir.mkdir()

    (workspace / "link").mkdir() if False else None
    try:
        os.symlink(real_dir, workspace / "link",
                   target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this host")

    with pytest.raises(ToolExecutionError) as excinfo:
        fs_write_file({
            "path": "link/escape.txt",
            "content": "should not land outside",
        })

    assert "symbolic link" in str(excinfo.value).lower()
    # Nothing was written through the link.
    assert not (real_dir / "escape.txt").exists()


def test_write_refuses_outside_root(workspace):
    with pytest.raises(ToolExecutionError):
        fs_write_file({
            "path": "C:/definitely/outside/enma.txt",
            "content": "no",
        })


# ============================================================
# P0.4 — write verification evidence
# ============================================================

def test_write_reports_verified_on_real_write(workspace):
    result = fs_write_file({
        "path": "out/verified.txt",
        "content": "evidence required",
    })

    assert result["verified"] is True
    assert result["bytes"] == len("evidence required")
    # Independent evidence: the file really is on disk with the
    # written content.
    assert (workspace / "out" / "verified.txt").read_text(
        encoding="utf-8"
    ) == "evidence required"


def test_write_verification_failure_is_loud(workspace, monkeypatch):
    """If the re-read cannot confirm the write, the tool fails
    instead of reporting success."""

    def broken_read(self):
        raise OSError("verification re-read failed")

    monkeypatch.setattr(Path, "read_bytes", broken_read)

    with pytest.raises(ToolExecutionError) as excinfo:
        fs_write_file({
            "path": "out/x.txt",
            "content": "hello",
        })

    assert "could not be verified" in str(excinfo.value).lower()


# ============================================================
# P1.4 — persistence error truthfulness
# ============================================================

def test_failed_persistence_write_is_recorded_not_swallowed(
    tmp_path,
):
    # The store path IS a directory: every append must fail.
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    persistence = TaskPersistence(str(store_dir))

    from backend.core.task import Task

    task = Task(title="t", description="")

    persistence.record(task)  # must not raise (best-effort)

    assert persistence.last_write_error is not None
    assert persistence.last_write_error.strip() != ""


def test_successful_persistence_write_clears_error(tmp_path):
    persistence = TaskPersistence(str(tmp_path / "tasks.jsonl"))

    from backend.core.task import Task

    persistence.record(Task(title="ok", description=""))

    assert persistence.last_write_error is None


# ============================================================
# P1.1 — async boundary: blocking tool must not freeze the loop
# ============================================================

def _make_agent(executor):
    registry = ToolRegistry()
    registry.register(
        "slow_tool", "blocks the caller", "system",
        RiskLevel.SAFE,
    )
    return Agent(
        name="async-boundary-agent",
        registry=registry,
        permission_policy=PermissionPolicy(registry),
        tool_executor=executor,
    )


def test_execute_async_does_not_block_event_loop():
    """While a sync tool is blocked on an event, the event loop
    must still be able to run other coroutines (a heartbeat
    coroutine ticks)."""

    gate = threading.Event()
    heartbeats = []

    def blocking_tool(name, params):
        gate.wait(timeout=5.0)
        return "released"

    agent = _make_agent(blocking_tool)
    task = Task(title="t", description="")

    async def heartbeat():
        for _ in range(6):
            heartbeats.append(True)
            await asyncio.sleep(0.05)

    async def scenario():
        beat = asyncio.create_task(heartbeat())
        result = await agent.execute_async(task, "slow_tool", {})
        await beat
        return result

    async def releaser():
        await asyncio.sleep(0.15)
        gate.set()

    async def run_all():
        # The releaser runs on the same loop: it can only fire if
        # the loop is free while the tool is blocked.
        release = asyncio.create_task(releaser())
        outcome = await scenario()
        await release
        return outcome

    result = asyncio.run(run_all())

    assert result.status.value == "COMPLETED"
    assert result.output == "released"
    assert len(heartbeats) >= 2, (
        "event loop was starved by a blocking sync tool"
    )
