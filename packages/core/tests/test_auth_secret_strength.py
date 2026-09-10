"""SEC-5: an operator-supplied session secret is entropy-checked like the secret
store's app secret.

The session signer is a plain HMAC, so ONE captured cookie is an offline
brute-force oracle — and the recovered secret also yields `pairing_token()`,
which is derived from it, i.e. full admin. The secret STORE already warns on a
weak app secret; the auth path did not check at all.
"""

from __future__ import annotations

import logging

import pytest
from disco.core import auth as core_auth
from disco.core.auth_parts import secret_strength


@pytest.fixture(autouse=True)
def _reset_latch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secret_strength, "_weak_auth_secret_warned", False)
    for var in ("DISCO_AUTH_SECRET", "PMX_AUTH_SECRET", "DISCO_SECRET_KEY", "PMX_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_weak_auth_secret_warns_loudly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DISCO_AUTH_SECRET", "hunter2")
    with caplog.at_level(logging.WARNING, logger="disco.core.auth_parts.secret_strength"):
        assert core_auth.session_secret() == "hunter2"
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    warning = caplog.records[0].getMessage()
    assert "low-entropy" in warning
    assert "pairing token" in warning
    # The value itself is NEVER logged — only its length.
    assert "hunter2" not in warning


def test_long_but_low_diversity_secret_still_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The reused heuristic is length AND character diversity, so a long
    keyboard-mashed string is caught too."""
    monkeypatch.setenv("DISCO_SECRET_KEY", "aaaaaaaaaaaaaaaaaaaaaaaa")
    with caplog.at_level(logging.WARNING, logger="disco.core.auth_parts.secret_strength"):
        core_auth.session_secret()
    assert caplog.records


def test_strong_auth_secret_is_silent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    strong = "uF3kP1xV9bQwRtY2mN8sJ6hL0cZ4dA7gK5eB3rT9oM="  # `openssl rand -base64 32` shape
    monkeypatch.setenv("DISCO_AUTH_SECRET", strong)
    with caplog.at_level(logging.WARNING, logger="disco.core.auth_parts.secret_strength"):
        assert core_auth.session_secret() == strong
    assert caplog.records == []


def test_weak_auth_secret_warns_once_and_never_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Warn, never raise: a running deployment with a short key must keep
    working — the same contract `llm/secrets.py::_warn_if_weak_secret` carries.
    And the warning is once per process, not once per request."""
    monkeypatch.setenv("DISCO_AUTH_SECRET", "short")
    with caplog.at_level(logging.WARNING, logger="disco.core.auth_parts.secret_strength"):
        for _ in range(5):
            assert core_auth.session_secret() == "short"
        # The pairing token derives from the same secret and also stays working.
        assert core_auth.pairing_token()
    assert len(caplog.records) == 1


def test_detection_is_the_secret_stores_own_helper() -> None:
    """No second heuristic: the two paths cannot drift on what "weak" means."""
    from disco.core.llm import secrets as store_secrets

    assert secret_strength._looks_weak is store_secrets._looks_weak
