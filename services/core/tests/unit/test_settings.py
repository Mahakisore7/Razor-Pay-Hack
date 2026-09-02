"""Settings must fail fast and must never leak a secret through repr (TR-75, T2).

Constructed through environment variables, not keyword arguments -- that is how
Settings is actually built in production (pydantic-settings' whole purpose),
and it sidesteps a real gap: mypy's synthesized __init__ for BaseSettings
checks kwargs against each field's *stored* type (SecretStr, a Literal), not
the *validatable* input type env vars are parsed from (always str). Testing
through the real construction path is both more honest and avoids fighting
the type checker over a mismatch that only exists at the keyword-argument
call site.

Every test clears the env vars Settings reads first, rather than relying on a
clean shell. A dev machine can easily have ANTHROPIC_API_KEY set ambiently
(this one does -- Claude Code itself resolves credentials from it), and a test
that reads the real environment passes or fails depending on who runs it.
See F-003.
"""

import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

from recoup.platform.config import Settings

_ENV_KEYS = (
    "ENVIRONMENT",
    "LOG_LEVEL",
    "DATABASE_URL",
    "REDIS_URL",
    "ANTHROPIC_API_KEY",
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
    "RAZORPAY_WEBHOOK_SECRET",
)


def _settings_ignoring_any_local_dotenv() -> Settings:
    # `_env_file` is a genuine pydantic-settings constructor parameter, accepted
    # at runtime to override SettingsConfigDict.env_file per-call. mypy's
    # synthesized __init__ is built from declared *fields* only and has no way
    # to know about it, so this is a real gap in the stubs, not a mistake here.
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_settings_load_from_defaults() -> None:
    """No required env vars are unset in a fresh checkout -- Phase 0 has no secrets yet."""
    settings = _settings_ignoring_any_local_dotenv()
    assert settings.environment == "development"
    assert settings.anthropic_api_key is None


def test_settings_reject_invalid_environment(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "staging")
    with pytest.raises(ValidationError):
        _settings_ignoring_any_local_dotenv()


def test_settings_repr_masks_secrets(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-super-secret-value")
    settings = _settings_ignoring_any_local_dotenv()
    rendered = repr(settings)
    assert "super-secret-value" not in rendered
    assert "anthropic_api_key" in rendered


def test_settings_str_also_masks_secrets(monkeypatch: MonkeyPatch) -> None:
    """str() must not become a second, unguarded path to the same leak."""
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "whsec_super_secret_value")
    settings = _settings_ignoring_any_local_dotenv()
    assert "super_secret_value" not in str(settings)
