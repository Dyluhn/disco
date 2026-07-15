from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
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


def test_child_environment_never_forwards_master_or_authentication_keys() -> None:
    source = {
        "DISCO_RELIABILITY_SEED_SECRET_KEY": "seed-master",
        "DISCO_SECRET_KEY": "runtime-master",
        "PMX_SECRET_KEY": "legacy-master",
        "DISCO_AUTH_SECRET": "session-signer",
        "PMX_AUTH_SECRET": "legacy-session-signer",
        "RETAIN_ME": "yes",
    }

    environment = _child_environment(["npx", "playwright", "test"], source)

    assert environment == {"RETAIN_ME": "yes"}


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
    assert manager.env["DISCO_AGENT_PORT"] == "18000"
    assert manager.env["DISCO_APP_PORT"] == "18800"
    assert manager.env["DISCO_UI_PORT"] == "5274"
    assert "DISCO_SANDBOX" not in manager.env
    assert manager.env["DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV"] == "1"


def test_stack_close_removes_secret_artifacts_but_preserves_diagnostics(
    monkeypatch, tmp_path: Path
) -> None:
    config = tmp_path / "seed-config.json"
    secrets = tmp_path / "seed-secrets.json"
    approvals = tmp_path / "seed-approvals.json"
    config.write_text('{"config": true}', encoding="utf-8")
    secrets.write_text('{"secrets": true}', encoding="utf-8")
    approvals.write_text('{"approvals": true}', encoding="utf-8")
    monkeypatch.setattr(StackManager, "_prepare_config", lambda self: None)
    manager = StackManager(
        repo=tmp_path,
        root=tmp_path / "stack-cleanup",
        agent_port=18000,
        app_port=18800,
        ui_port=5274,
        seed_config=config,
        seed_secrets=secrets,
        seed_approvals=approvals,
    )
    temporary_secrets = manager.secrets_path.with_suffix(".json.tmp")
    temporary_secrets.write_text('{"partial": true}', encoding="utf-8")

    manager.close()

    assert not manager.secrets_path.exists()
    assert not temporary_secrets.exists()
    assert manager.config_path.read_bytes() == config.read_bytes()
    assert manager.approvals_path.read_bytes() == approvals.read_bytes()
    manifest = json.loads((manager.root / "stack.json").read_text(encoding="utf-8"))
    assert manifest["sandbox_backend"] == "process"


def test_stack_removes_copied_secrets_when_initialization_fails(
    monkeypatch, tmp_path: Path
) -> None:
    secrets = tmp_path / "seed-secrets.json"
    secrets.write_text('{"secrets": true}', encoding="utf-8")

    def fail_preparation(_manager: StackManager) -> None:
        raise RuntimeError("config preparation failed")

    monkeypatch.setattr(StackManager, "_prepare_config", fail_preparation)
    root = tmp_path / "stack-init-failure"

    with pytest.raises(RuntimeError, match="config preparation failed"):
        StackManager(
            repo=tmp_path,
            root=root,
            agent_port=18000,
            app_port=18800,
            ui_port=5274,
            seed_config=None,
            seed_secrets=secrets,
            seed_approvals=None,
        )

    assert not (root / "secrets.json").exists()


def test_stack_removes_secrets_even_when_diagnostic_manifest_fails(
    monkeypatch, tmp_path: Path
) -> None:
    secrets = tmp_path / "seed-secrets.json"
    secrets.write_text('{"secrets": true}', encoding="utf-8")
    monkeypatch.setattr(StackManager, "_prepare_config", lambda self: None)
    manager = StackManager(
        repo=tmp_path,
        root=tmp_path / "stack-manifest-failure",
        agent_port=18000,
        app_port=18800,
        ui_port=5274,
        seed_config=None,
        seed_secrets=secrets,
        seed_approvals=None,
    )

    def fail_manifest() -> None:
        raise OSError("manifest unavailable")

    monkeypatch.setattr(manager, "_write_manifest", fail_manifest)

    with pytest.raises(OSError, match="manifest unavailable"):
        manager.close()

    assert not manager.secrets_path.exists()


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
    assert "DISCO_SANDBOX" not in manager.env
    assert "DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV" not in manager.env
    manager._write_manifest()
    manifest = json.loads((manager.root / "stack.json").read_text(encoding="utf-8"))
    assert manifest["sandbox_backend"] == "gvisor"


def test_build_lane_can_select_namespaced_podman_without_process_fallback(
    monkeypatch, tmp_path: Path
) -> None:
    """H170: literal container port 8000 must never occupy host port 8000."""

    from disco.agent_server.sandbox_runtime_service import build_sandbox_service

    repo = Path(__file__).resolve().parents[3]
    seed = tmp_path / "seed-config.json"
    ConfigStore(seed).save(default_config())
    monkeypatch.setenv("DISCO_RELIABILITY_SEED_SECRET_KEY", "x" * 64)
    # Ambient overrides are hostile input: the disposable config must still win.
    monkeypatch.setenv("DISCO_SANDBOX", "process")
    monkeypatch.setenv("DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV", "1")

    manager = StackManager(
        repo=repo,
        root=tmp_path / "stack-podman",
        agent_port=18101,
        app_port=18901,
        ui_port=5291,
        seed_config=seed,
        seed_secrets=None,
        seed_approvals=None,
        sandbox_backend="podman",
    )

    with _temporary_environment(
        manager.env,
        ("DISCO_CONFIG", "DISCO_SECRETS", "DISCO_APPROVALS", "DISCO_SECRET_KEY"),
    ):
        persisted = ConfigStore(manager.config_path).load().sandbox
    service = build_sandbox_service(persisted)
    assert persisted.backend == "podman"
    assert persisted.runtime == "crun"
    assert service.name == "podman"
    assert service.is_production_valid is True
    assert "DISCO_SANDBOX" not in manager.env
    assert "DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV" not in manager.env
    manager._write_manifest()
    manifest = json.loads((manager.root / "stack.json").read_text(encoding="utf-8"))
    assert manifest["sandbox_backend"] == "podman"


def test_stack_rejects_conflicting_preserved_and_explicit_sandbox(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        StackManager(
            repo=tmp_path,
            root=tmp_path / "stack-conflict",
            agent_port=18000,
            app_port=18800,
            ui_port=5274,
            seed_config=None,
            seed_secrets=None,
            seed_approvals=None,
            preserve_seed_sandbox=True,
            sandbox_backend="podman",
        )
