from __future__ import annotations

import os
from pathlib import Path

from disco.core.llm import ConfigStore, default_config

from harness.reliability.isolated_stack import (
    StackManager,
    _child_environment,
    _temporary_environment,
)


def test_playwright_child_environment_removes_conflicting_no_color() -> None:
    source = {"NO_COLOR": "1", "RETAIN_ME": "yes"}

    environment = _child_environment(["npx", "playwright", "test"], source)

    assert "NO_COLOR" not in environment
    assert environment["RETAIN_ME"] == "yes"


def test_non_playwright_child_environment_preserves_no_color() -> None:
    source = {"NO_COLOR": "1", "RETAIN_ME": "yes"}

    environment = _child_environment(["python", "-m", "pytest"], source)

    assert environment == source


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


def test_stack_defaults_to_filtered_egress_despite_ambient_open(
    monkeypatch, tmp_path: Path
) -> None:
    """H-001: an operator's ambient open posture must not weaken campaign proof."""

    monkeypatch.setenv("DISCO_BUILD_EGRESS", "open")
    monkeypatch.delenv("DISCO_RELIABILITY_BUILD_EGRESS", raising=False)
    monkeypatch.setattr(StackManager, "_prepare_config", lambda self: None)

    manager = StackManager(
        repo=tmp_path,
        root=tmp_path / "stack-filtered",
        agent_port=18000,
        app_port=18800,
        ui_port=5274,
        seed_config=None,
        seed_secrets=None,
        seed_approvals=None,
    )

    assert manager.env["DISCO_BUILD_EGRESS"] == "filtered"


def test_stack_accepts_only_explicit_reliability_egress_override(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DISCO_BUILD_EGRESS", "open")
    monkeypatch.setenv("DISCO_RELIABILITY_BUILD_EGRESS", "sealed")
    monkeypatch.setattr(StackManager, "_prepare_config", lambda self: None)

    manager = StackManager(
        repo=tmp_path,
        root=tmp_path / "stack-sealed",
        agent_port=18000,
        app_port=18800,
        ui_port=5274,
        seed_config=None,
        seed_secrets=None,
        seed_approvals=None,
    )

    assert manager.env["DISCO_BUILD_EGRESS"] == "sealed"


def test_gvisor_lane_can_preserve_seeded_sandbox(monkeypatch, tmp_path: Path) -> None:
    """The isolated App+Agent pair may retain a real seeded runsc backend."""

    repo = Path(__file__).resolve().parents[3]
    seed = tmp_path / "seed-config.json"
    sandbox = default_config().sandbox.model_copy(
        update={
            "backend": "gvisor",
            "docker_socket": "ssh://sandbox@100.64.0.10",
            "runtime": "runsc",
        }
    )
    ConfigStore(seed).save(default_config().model_copy(update={"sandbox": sandbox}))
    monkeypatch.setenv("DISCO_RELIABILITY_SEED_SECRET_KEY", "x" * 64)

    manager = StackManager(
        repo=repo,
        root=tmp_path / "stack-gvisor",
        agent_port=18000,
        app_port=18800,
        ui_port=5274,
        seed_config=seed,
        seed_secrets=None,
        seed_approvals=None,
        preserve_seed_sandbox=True,
    )

    with _temporary_environment(
        manager.env,
        ("DISCO_CONFIG", "DISCO_SECRETS", "DISCO_APPROVALS", "DISCO_SECRET_KEY"),
    ):
        persisted = ConfigStore(manager.config_path).load().sandbox
    assert persisted.backend == "gvisor"
    assert persisted.runtime == "runsc"
    assert persisted.docker_socket == "ssh://sandbox@100.64.0.10"
