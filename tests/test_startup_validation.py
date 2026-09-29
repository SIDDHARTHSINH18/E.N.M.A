"""
Regression tests for packaged startup validation.

The desktop layer generates config.env with ENMA_DEFAULT_PROVIDER
(the provider the app actually talks to) and expects startup
validation to check THAT provider's key — not a hardcoded one.
A clean packaged install with the intended provider configured
must pass validation; a missing key must still fail closed.
"""

import pytest

from backend.main import validate_startup_config


PROVIDER_ENV_VARS = [
    "ENMA_DEFAULT_PROVIDER",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "NVIDIA_API_KEY",
    "HF_API_KEY",
    "HUGGINGFACE_API_KEY",
]


@pytest.fixture
def clean_packaged_env(monkeypatch):
    """Simulate a fresh packaged install: only config.env content."""
    for var in PROVIDER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GHOST_AUTH_PASSWORD", "test-passphrase")
    return monkeypatch


def test_clean_install_with_intended_provider_passes(
    clean_packaged_env,
):
    """GROQ_API_KEY set + ENMA_DEFAULT_PROVIDER=groq (as the
    packaged config template writes it) passes validation even
    though the NVIDIA/Gemini keys are absent."""

    clean_packaged_env.setenv("ENMA_DEFAULT_PROVIDER", "groq")
    clean_packaged_env.setenv("GROQ_API_KEY", "gsk_test-value")

    status = validate_startup_config()

    assert status["provider"] == "groq"
    assert status["api_key_present"] is True


def test_default_provider_gemini_still_validates(monkeypatch):
    """Without the override, the historical default (Gemini) is
    what validation checks — existing dev setups are unchanged."""

    for var in PROVIDER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GHOST_AUTH_PASSWORD", "test-passphrase")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    status = validate_startup_config()

    assert status["provider"] == "gemini"


def test_missing_intended_provider_key_fails_closed(
    clean_packaged_env,
):
    """Intended provider named but its key absent: startup must
    refuse, naming that provider — not silently pass."""

    clean_packaged_env.setenv("ENMA_DEFAULT_PROVIDER", "groq")

    with pytest.raises(RuntimeError) as excinfo:
        validate_startup_config()

    assert "GROQ_API_KEY" in str(excinfo.value)


def test_no_key_at_all_fails_closed(clean_packaged_env):
    """Fully unconfigured install still refuses to start."""

    with pytest.raises(RuntimeError) as excinfo:
        validate_startup_config()

    assert "API_KEY" in str(excinfo.value)


def test_missing_auth_password_fails_closed(clean_packaged_env):
    """Provider configured but no passphrase: still refuses."""

    clean_packaged_env.setenv("ENMA_DEFAULT_PROVIDER", "groq")
    clean_packaged_env.setenv("GROQ_API_KEY", "gsk_test-value")
    clean_packaged_env.delenv("GHOST_AUTH_PASSWORD", raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        validate_startup_config()

    assert "GHOST_AUTH_PASSWORD" in str(excinfo.value)
