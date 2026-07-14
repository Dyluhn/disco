from __future__ import annotations

import logging

from disco.agent_server import auth as agent_auth
from disco.app_server import auth as app_auth


def test_auto_pair_omits_pairing_token_from_both_server_logs(monkeypatch, caplog, capsys) -> None:
    sentinel = "pairing-secret-must-not-appear"
    monkeypatch.setenv("DISCO_AUTH_DEV_AUTO_PAIR", "1")
    monkeypatch.setattr(agent_auth, "pairing_token", lambda: sentinel)
    monkeypatch.setattr(app_auth, "pairing_token", lambda: sentinel)
    caplog.set_level(logging.INFO)

    agent_auth.make_auth_router()
    app_auth.make_auth_router()

    rendered = caplog.text + capsys.readouterr().out
    assert sentinel not in rendered
    assert "pairing token omitted" in rendered
    assert "no pairing token is printed" in rendered


def test_manual_pairing_still_prints_operator_token(monkeypatch, capsys) -> None:
    sentinel = "manual-pairing-token"
    monkeypatch.setenv("DISCO_AUTH_DEV_AUTO_PAIR", "0")
    monkeypatch.setattr(app_auth, "pairing_token", lambda: sentinel)

    app_auth._print_boot_banner()

    assert sentinel in capsys.readouterr().out
