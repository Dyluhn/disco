"""`disco-pairing-token` — the on-demand replacement for hunting the boot banner.

Found on a fresh self-host install (2026-09-09): the banner with the pairing
token scrolls out of `compose logs app-server | tail -20` within minutes of
healthcheck noise, and the README had no other way to recover it.
"""

from __future__ import annotations

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
