from __future__ import annotations

import os
from pathlib import Path

from harness.reliability.isolated_stack import StackManager, _temporary_environment


def test_temporary_environment_restores_parent_values(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_CONFIG", "parent")
    monkeypatch.delenv("DISCO_APPROVALS", raising=False)
    source = {"DISCO_CONFIG": "child", "DISCO_APPROVALS": "child-approvals"}

    with _temporary_environment(source, ("DISCO_CONFIG", "DISCO_APPROVALS")):
        assert os.environ["DISCO_CONFIG"] == "child"
        assert os.environ["DISCO_APPROVALS"] == "child-approvals"

    assert os.environ["DISCO_CONFIG"] == "parent"
    assert "DISCO_APPROVALS" not in os.environ


def test_stack_copies_all_signed_model_state(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "seed-config.json"
    secrets = tmp_path / "seed-secrets.json"
    approvals = tmp_path / "seed-approvals.json"
    config.write_text('{"config": true}', encoding="utf-8")
    secrets.write_text('{"secrets": true}', encoding="utf-8")
    approvals.write_text('{"approvals": true}', encoding="utf-8")
    monkeypatch.setattr(StackManager, "_prepare_config", lambda self: None)

    manager = StackManager(
        repo=tmp_path,
        root=tmp_path / "stack",
        agent_port=18000,
        app_port=18800,
        ui_port=5274,
        seed_config=config,
        seed_secrets=secrets,
        seed_approvals=approvals,
    )

    assert manager.config_path.read_bytes() == config.read_bytes()
    assert manager.secrets_path.read_bytes() == secrets.read_bytes()
    assert manager.approvals_path.read_bytes() == approvals.read_bytes()
    assert manager.env["DISCO_CONFIG"] == str(manager.config_path)
    assert manager.env["DISCO_SECRETS"] == str(manager.secrets_path)
    assert manager.env["DISCO_APPROVALS"] == str(manager.approvals_path)
