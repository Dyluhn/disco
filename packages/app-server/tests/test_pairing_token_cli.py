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
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from disco.app_server.pairing_cli import main
from disco.core.auth import pairing_token

_PAIRING_DOMAIN = b"disco-pairing-token-v1"


def _expected_token(secret: str) -> str:
    """Independent HMAC derivation of the pairing token for a fixture secret."""
    digest = hmac.new(secret.encode(), _PAIRING_DOMAIN, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _fresh_env(data_dir: Path, data_env: str = "DISCO_DATA_DIR") -> dict[str, str]:
    """Isolated subprocess env: fixture data dir only, no secret exports."""
    env = dict(os.environ)
    for prefix in ("DISCO_", "PMX_"):
        for suffix in ("SECRET_KEY", "AUTH_SECRET", "AUTH_SECRET_FILE", "DATA_DIR"):
            env.pop(prefix + suffix, None)
    env[data_env] = str(data_dir)
    return env


def _run_cli(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the CLI as a fresh process, as `compose exec` does."""
    return subprocess.run(
        [sys.executable, "-m", "disco.app_server.pairing_cli"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )


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
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    expected = (
        base64.urlsafe_b64encode(
            hmac.new(secret.encode(), b"disco-pairing-token-v1", hashlib.sha256).digest()
        )
        .decode()
        .rstrip("=")
    )
    assert f"Pairing token: {expected}" in completed.stdout
    assert secret not in completed.stdout
    assert not (tmp_path / "auth_secret").exists()
    assert (tmp_path / filename).read_text().strip() == secret


@pytest.mark.parametrize(
    "secret_env", ["DISCO_SECRET_KEY", "PMX_SECRET_KEY", "DISCO_AUTH_SECRET", "PMX_AUTH_SECRET"]
)
def test_exec_explicit_secret_env_still_wins(tmp_path, secret_env):
    """Explicit operator secret env keeps working beside persisted files."""
    explicit = "explicit-operator-secret-with-enough-entropy-for-test"
    install_key = "install-key-that-must-be-ignored-for-this-test-x"
    persisted = "persisted-auth-secret-that-must-be-ignored-here-xyz"
    (tmp_path / ".secret_key").write_text(install_key + "\n", encoding="utf-8")
    (tmp_path / "auth_secret").write_text(persisted + "\n", encoding="utf-8")
    env = _fresh_env(tmp_path)
    env[secret_env] = explicit
    completed = _run_cli(env)
    assert f"Pairing token: {_expected_token(explicit)}" in completed.stdout
    assert explicit not in completed.stdout
    assert "UI: http" in completed.stdout
    assert (tmp_path / ".secret_key").read_text().strip() == install_key
    assert (tmp_path / "auth_secret").read_text().strip() == persisted
    assert not (tmp_path / "secret-key").exists()


@pytest.mark.parametrize("file_env", ["DISCO_AUTH_SECRET_FILE", "PMX_AUTH_SECRET_FILE"])
def test_exec_explicit_auth_secret_file_stays_authoritative(tmp_path, file_env):
    """An explicit auth-secret file wins over install keys and default files."""
    file_secret = "explicit-file-secret-with-enough-entropy-for-test"
    install_key = "install-key-that-must-be-ignored-for-this-test-y"
    default_persisted = "default-auth-secret-that-must-be-ignored-here-12"
    explicit_path = tmp_path / "custom-secret-file"
    explicit_path.write_text(file_secret + "\n", encoding="utf-8")
    (tmp_path / ".secret_key").write_text(install_key + "\n", encoding="utf-8")
    (tmp_path / "auth_secret").write_text(default_persisted + "\n", encoding="utf-8")
    env = _fresh_env(tmp_path)
    env[file_env] = str(explicit_path)
    completed = _run_cli(env)
    assert f"Pairing token: {_expected_token(file_secret)}" in completed.stdout
    assert file_secret not in completed.stdout
    assert explicit_path.read_text().strip() == file_secret
    assert (tmp_path / ".secret_key").read_text().strip() == install_key
    assert (tmp_path / "auth_secret").read_text().strip() == default_persisted
    assert not (tmp_path / "secret-key").exists()


def test_exec_persisted_auth_secret_keeps_its_contract(tmp_path):
    """An auth-secret-only standalone install retains its existing CLI token."""
    persisted = "already-persisted-auth-secret-with-enough-entropy-1"
    (tmp_path / "auth_secret").write_text(persisted + "\n", encoding="utf-8")
    completed = _run_cli(_fresh_env(tmp_path))
    assert f"Pairing token: {_expected_token(persisted)}" in completed.stdout
    assert persisted not in completed.stdout
    assert (tmp_path / "auth_secret").read_text().strip() == persisted
    assert not (tmp_path / ".secret_key").exists()
    assert not (tmp_path / "secret-key").exists()


def test_exec_no_persisted_file_matches_server_bootstrap(tmp_path):
    """Empty data dir bootstraps the server install key (not a rival auth_secret)."""
    env = _fresh_env(tmp_path)
    first = _run_cli(env)
    created = (tmp_path / "secret-key").read_text(encoding="utf-8").strip()
    assert created
    assert f"Pairing token: {_expected_token(created)}" in first.stdout
    assert created not in first.stdout
    assert not (tmp_path / "auth_secret").exists()
    second = _run_cli(env)
    assert f"Pairing token: {_expected_token(created)}" in second.stdout
    assert (tmp_path / "secret-key").read_text(encoding="utf-8").strip() == created


def test_exec_install_key_usable_on_read_only_dir(tmp_path):
    """A readable install key on a read-only dir/file needs no writes or leaks."""
    secret = "read-only-install-secret-with-enough-entropy-for-test"
    key_path = tmp_path / ".secret_key"
    key_path.write_text(secret + "\n", encoding="utf-8")
    os.chmod(key_path, 0o400)
    os.chmod(tmp_path, 0o555)
    try:
        completed = _run_cli(_fresh_env(tmp_path))
    finally:
        os.chmod(tmp_path, 0o755)
        os.chmod(key_path, 0o600)
    assert f"Pairing token: {_expected_token(secret)}" in completed.stdout
    assert secret not in completed.stdout
    assert key_path.read_text(encoding="utf-8").strip() == secret
    assert not (tmp_path / "auth_secret").exists()
    assert not (tmp_path / "secret-key").exists()


def test_exec_install_key_file_mode_stays_owner_only(tmp_path):
    """Adopting an install key does not loosen its file mode bits."""
    secret = "mode-check-install-secret-with-enough-entropy-for-test"
    key_path = tmp_path / ".secret_key"
    key_path.write_text(secret + "\n", encoding="utf-8")
    os.chmod(key_path, 0o600)
    completed = _run_cli(_fresh_env(tmp_path))
    assert f"Pairing token: {_expected_token(secret)}" in completed.stdout
    mode = stat.S_IMODE(key_path.stat().st_mode)
    assert not (mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH))
    assert key_path.read_text(encoding="utf-8").strip() == secret


@pytest.mark.parametrize("filename", [".secret_key", "secret-key"])
@pytest.mark.parametrize("data_env", ["DISCO_DATA_DIR", "PMX_DATA_DIR"])
def test_exec_matches_server_with_stale_cli_auth_secret(tmp_path, filename, data_env):
    """A token from the old CLI must not shadow the key used by server startup."""
    server_key = "actual-server-install-key-fixture-for-stale-cli-control"
    stale = "prior-cli-auth-secret-fixture-that-server-does-not-use"
    key_path = tmp_path / filename
    stale_path = tmp_path / "auth_secret"
    key_path.write_text(server_key + "\n", encoding="utf-8")
    stale_path.write_text(stale + "\n", encoding="utf-8")
    env = _fresh_env(tmp_path, data_env)
    server = subprocess.run(
        [
            sys.executable,
            "-c",
            "from disco.core.llm.secrets import ensure_process_secret_key; "
            "from disco.core.auth import pairing_token; "
            "ensure_process_secret_key(); print(pairing_token())",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    expected = _expected_token(server_key)
    assert server.stdout.strip() == expected
    completed = _run_cli(env)
    assert f"Pairing token: {expected}" in completed.stdout
    assert server_key not in completed.stdout and stale not in completed.stdout
    assert key_path.read_text(encoding="utf-8") == server_key + "\n"
    assert stale_path.read_text(encoding="utf-8") == stale + "\n"


@pytest.mark.parametrize(
    "secret_env",
    [
        "DISCO_SECRET_KEY",
        "PMX_SECRET_KEY",
        "DISCO_AUTH_SECRET",
        "PMX_AUTH_SECRET",
    ],
)
def test_exec_explicit_secret_needs_no_data_directory_write(tmp_path, secret_env):
    """Explicit secrets work where a bootstrap would require a forbidden write."""
    explicit = "explicit-read-only-install-key-fixture-for-cli-contract"
    env = _fresh_env(tmp_path)
    env[secret_env] = explicit
    tmp_path.chmod(0o555)
    try:
        assert not os.access(tmp_path, os.W_OK)
        completed = _run_cli(env)
        assert list(tmp_path.iterdir()) == []
    finally:
        tmp_path.chmod(0o755)
    assert f"Pairing token: {_expected_token(explicit)}" in completed.stdout
    assert explicit not in completed.stdout


@pytest.mark.parametrize("file_env", ["DISCO_AUTH_SECRET_FILE", "PMX_AUTH_SECRET_FILE"])
def test_exec_explicit_secret_file_needs_no_install_key_write(tmp_path, file_env):
    """Explicit auth-file CLI compatibility is preserved on a read-only install."""
    explicit = "explicit-read-only-auth-file-fixture-for-cli-contract"
    key = tmp_path / "custom-secret"
    key.write_text(explicit + "\n", encoding="utf-8")
    env = _fresh_env(tmp_path)
    env[file_env] = str(key)
    key.chmod(0o400)
    tmp_path.chmod(0o555)
    try:
        assert not os.access(tmp_path, os.W_OK)
        completed = _run_cli(env)
        assert list(tmp_path.iterdir()) == [key]
    finally:
        tmp_path.chmod(0o755)
        key.chmod(0o600)
    assert f"Pairing token: {_expected_token(explicit)}" in completed.stdout
    assert explicit not in completed.stdout
    assert key.read_text(encoding="utf-8") == explicit + "\n"


@pytest.mark.parametrize("inaccessible", ["key", "directory"])
def test_exec_unreadable_install_key_never_prints_a_foreign_token(tmp_path, inaccessible):
    """A real key access error must not turn into a different pairing identity."""
    key = tmp_path / ".secret_key"
    server_key = "server-install-secret-fixture-that-is-now-unreadable"
    key.write_text(server_key + "\n", encoding="utf-8")
    (tmp_path / "auth_secret").write_text(
        "stale-cli-auth-secret-fixture-that-must-not-be-used\n",
        encoding="utf-8",
    )
    env = _fresh_env(tmp_path)
    target = key if inaccessible == "key" else tmp_path
    target.chmod(0)
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "disco.app_server.pairing_cli"],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
    finally:
        tmp_path.chmod(0o755)
        key.chmod(0o600)
    assert completed.returncode != 0
    assert "PermissionError" in completed.stderr
    assert "Pairing token:" not in completed.stdout
    assert server_key not in completed.stdout + completed.stderr
