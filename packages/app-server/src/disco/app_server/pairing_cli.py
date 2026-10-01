"""Print this install's admin pairing token on demand.

The boot banner prints the token once, and healthcheck lines push it out of
`compose logs app-server | tail` within minutes. The token is not minted per
boot: it is an HMAC tag over the install secret (see
``disco.core.auth.pairing_token``), so any process that can read the secret can
recompute it. Run it where the secret lives — inside the app-server container:

    podman compose exec app-server python -m disco.app_server.pairing_cli

Deliberately a module entry point rather than a ``[project.scripts]`` console
script: every packages/*/pyproject.toml is a pinned contract file in
``development/architecture/public-api.json``, and re-pinning one needs a
single-use governance authority that is already spent. ``python -m`` ships the
same command with no packaging-contract change.
"""

from __future__ import annotations

from pathlib import Path

from disco.core.auth import pairing_token
from disco.core.env import disco_env
from disco.core.llm.secrets import _default_app_secret_path, ensure_process_secret_key

from .auth import public_ui_url


def _install_key_present() -> bool:
    primary = _default_app_secret_path()
    for path in (primary, primary.parent / ".secret_key"):
        try:
            path.stat()
        except FileNotFoundError:
            continue
        return True
    return False


def _has_persisted_auth_secret() -> bool:
    path = Path(disco_env("DATA_DIR", "") or "~/.local/share/disco").expanduser() / "auth_secret"
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def _ensure_install_secret_when_needed() -> None:
    """Match the server install key while preserving explicit and auth-only CLI configuration."""
    if disco_env("AUTH_SECRET") or disco_env("SECRET_KEY") or disco_env("AUTH_SECRET_FILE", ""):
        return
    if _install_key_present():
        # A prior CLI-created auth_secret must not override the server's key.
        # File access errors propagate rather than printing a foreign token.
        ensure_process_secret_key()
        return
    if _has_persisted_auth_secret():
        return
    try:
        ensure_process_secret_key()
    except OSError:
        # Preserve the existing auth fallback when a missing key cannot be created.
        pass


def main() -> int:
    _ensure_install_secret_when_needed()
    print(f"UI: {public_ui_url()}")
    print(f"Pairing token: {pairing_token()}")
    print("Paste it if the browser asks to pair; it is valid until the install secret changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
