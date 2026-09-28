"""
Shared pytest configuration.

Must set environment BEFORE any backend import:
- GHOST_MEMORY_PATH: keep tests off the real memory store
- NVIDIA_API_KEY: backend.main fails fast without it
- GHOST_AUTH_PASSWORD: backend.main fails fast without it (M2)
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault(
    "GHOST_MEMORY_PATH",
    str(
        Path(tempfile.gettempdir())
        / "ghost-test-memory.json"
    ),
)

# M4: keep the append-only audit store off the real
# backend/data/audit.jsonl, exactly like GHOST_MEMORY_PATH
# does for memory.
os.environ.setdefault(
    "GHOST_AUDIT_PATH",
    str(
        Path(tempfile.gettempdir())
        / "ghost-test-audit.jsonl"
    ),
)

# M4: task persistence must not touch the real
# backend/data/tasks.jsonl during tests, same convention
# as GHOST_MEMORY_PATH / GHOST_AUDIT_PATH. A UNIQUE file per
# session: the app-level task store loads this file once at
# import, so leftover records from a previous run would leak
# into list/count assertions.
os.environ.setdefault(
    "GHOST_TASKS_PATH",
    str(
        Path(tempfile.mkdtemp(prefix="ghost-test-tasks-"))
        / "tasks.jsonl"
    ),
)

os.environ.setdefault(
    "NVIDIA_API_KEY",
    "test-key-not-real",
)

os.environ.setdefault(
    "GHOST_AUTH_PASSWORD",
    "ghost-test-password",
)

# Headroom for the suite: every auth_client fixture use
# performs one login, and the M1 no-count-limit test
# uploads 15 documents. Tests that verify rate limiting
# monkeypatch their own small values.
os.environ.setdefault(
    "GHOST_RATE_LIMIT_LOGIN_PER_MIN",
    "500",
)

os.environ.setdefault(
    "GHOST_RATE_LIMIT_UPLOAD_PER_MIN",
    "200",
)


@pytest.fixture
def auth_client():
    """
    A TestClient with a valid session token baked into
    its default headers — every request is authenticated.
    """

    from fastapi.testclient import TestClient

    from backend.main import app

    client = TestClient(app)

    response = client.post(
        "/api/auth/login",
        json={
            "password": os.environ["GHOST_AUTH_PASSWORD"],
        },
    )

    assert response.status_code == 200, (
        f"login failed in fixture: {response.text}"
    )

    client.headers.update(
        {
            "Authorization": (
                f"Bearer {response.json()['token']}"
            ),
        }
    )

    yield client
