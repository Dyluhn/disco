from __future__ import annotations

from typing import Any

from disco.agent_server import __main__ as agent_main
from disco.app_server import __main__ as app_main


def _capture_uvicorn(monkeypatch, module: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(module, "ensure_process_secret_key", lambda: None)
    monkeypatch.setattr(module, "SqliteEventStore", lambda _path: object())
    monkeypatch.setattr(module, "create_app", lambda *args, **kwargs: object())

    def fake_run(_app: object, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(module.uvicorn, "run", fake_run)
    return captured


def test_app_entrypoint_uses_maintained_websocket_adapter(monkeypatch) -> None:
    captured = _capture_uvicorn(monkeypatch, app_main)

    app_main.main()

    assert captured["ws"] == "websockets-sansio"


def test_agent_entrypoint_uses_maintained_websocket_adapter(monkeypatch) -> None:
    captured = _capture_uvicorn(monkeypatch, agent_main)
    monkeypatch.setattr(agent_main, "_sandbox_service", lambda: None)
    monkeypatch.setattr(
        agent_main,
        "ConversationRuntime",
        lambda _store, sandbox_service=None: object(),
    )

    agent_main.main()

    assert captured["ws"] == "websockets-sansio"
