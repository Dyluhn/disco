"""`python -m disco.app_server.pairing_cli` — the on-demand replacement for
hunting the boot banner.

Found on a fresh self-host install (2026-09-09): the banner with the pairing
token scrolls out of `compose logs app-server | tail -20` within minutes of
healthcheck noise, and the README had no other way to recover it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import subprocess
import sys

import pytest
from disco.app_server.pairing_cli import main
from disco.core.auth import pairing_token


def test_prints_the_same_token_the_boot_banner_would(capsys, monkeypatch) -> None:
    monkeypatch.setenv("DISCO_SECRET_KEY", "a-long-enough-install-secret-for-this-test")
    monkeypatch.setenv("DISCO_UI_PORT", "8088")
    monkeypatch.delenv("DISCO_PUBLIC_UI_URL", raising=False)

    assert main() == 0

    out = capsys.readouterr().out
    assert pairing_token() in out
    assert "http://localhost:8088" in out


def test_follows_the_configured_public_ui_url(capsys, monkeypatch) -> None:
    monkeypatch.setenv("DISCO_SECRET_KEY", "a-long-enough-install-secret-for-this-test")
    monkeypatch.setenv("DISCO_PUBLIC_UI_URL", "https://disco.example.com/")

    assert main() == 0

    assert "https://disco.example.com" in capsys.readouterr().out


@pytest.mark.parametrize("filename", [".secret_key", "secret-key"])
@pytest.mark.parametrize("data_env", ["DISCO_DATA_DIR", "PMX_DATA_DIR"])
def test_exec_process_uses_the_persisted_server_secret(tmp_path, filename, data_env):
    """Container exec does not inherit exports performed by its entrypoint."""
    secret = "persisted-install-secret-with-enough-entropy-for-this-test"
    (tmp_path / filename).write_text(secret + "\n", encoding="utf-8")
    env = dict(os.environ)
    for prefix in ("DISCO_", "PMX_"):
        for suffix in ("SECRET_KEY", "AUTH_SECRET", "AUTH_SECRET_FILE", "DATA_DIR"):
            env.pop(prefix + suffix, None)
    env[data_env] = str(tmp_path)
    completed = subprocess.run(
        [sys.executable, "-m", "disco.app_server.pairing_cli"],
        env=env, capture_output=True, text=True, check=True, timeout=20,
    )
    expected = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), b"disco-pairing-token-v1", hashlib.sha256).digest()
    ).decode().rstrip("=")
    assert f"Pairing token: {expected}" in completed.stdout
    assert secret not in completed.stdout
    assert not (tmp_path / "auth_secret").exists()
    assert (tmp_path / filename).read_text().strip() == secret
