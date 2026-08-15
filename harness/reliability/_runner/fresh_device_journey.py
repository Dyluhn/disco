"""Ordered fresh-device journey with explicit phase bindings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.reliability.state import PASS


@dataclass(frozen=True)
class FreshDeviceJourneyBindings:
    install_and_boot: Callable[..., tuple[str, str, str, list[str]]]
    pair_and_config: Callable[..., Any]
    verify_and_scenarios: Callable[..., None]
    build_search_export: Callable[..., tuple[list[str], dict[str, str]]]
    cold_restart: Callable[..., dict[str, Any]]
    upgrade: Callable[..., dict[str, Any]]
    backup_restore: Callable[..., dict[str, Any]]
    uninstall: Callable[..., None]


def _record_pass(
    checks: list[dict[str, Any]],
    check_id: str,
    detail: str,
    **evidence: Any,
) -> None:
    checks.append({"id": check_id, "status": PASS, "detail": detail, "evidence": evidence})


def _validated_image_ids(value: Any, *, phase: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(image_id, str) or not image_id for image_id in value)
    ):
        raise RuntimeError(f"{phase} violated its nonempty image-inventory contract")
    return sorted(set(value))


def _validated_project_digests(
    value: Any, *, project_ids: list[str], phase: str
) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or not value
        or set(value) != set(project_ids)
        or any(
            not isinstance(project_id, str)
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            for project_id, digest in value.items()
        )
    ):
        raise RuntimeError(f"{phase} violated its complete project-digest contract")
    return {project_id: value[project_id] for project_id in sorted(value)}


def _run_recovery_phases(
    *,
    bindings: FreshDeviceJourneyBindings,
    runner: Any,
    engine: Any,
    project: str,
    checkout: Path,
    lifecycle_script: Path,
    compose_env: dict[str, str],
    out: Path,
    front: str,
    app: str,
    agent: str,
    api: Any,
    projects_before: list[str],
    project_digests: dict[str, str],
    known_image_ids: list[str],
    checks: list[dict[str, Any]],
) -> None:
    evidence = bindings.backup_restore(
        runner,
        engine,
        project,
        checkout,
        lifecycle_script,
        compose_env,
        front,
        app,
        agent,
        api,
        projects_before,
        project_digests,
        out,
    )
    _validated_project_digests(
        evidence.get("project_digests"),
        project_ids=projects_before,
        phase="backup_restore",
    )
    _record_pass(
        checks,
        "backup-restore",
        "selected backup restored the complete data volume and all committed projects",
        **evidence,
    )
    bindings.uninstall(
        runner,
        engine,
        project,
        checkout,
        compose_env,
        out,
        known_image_ids,
    )
    _record_pass(
        checks, "uninstall", "containers, networks, volumes, local images, and clone were removed"
    )


def _run_persistence_phases(
    *,
    bindings: FreshDeviceJourneyBindings,
    runner: Any,
    engine: Any,
    project: str,
    checkout: Path,
    lifecycle_script: Path,
    compose_env: dict[str, str],
    out: Path,
    front: str,
    app: str,
    agent: str,
    api: Any,
    projects_before: list[str],
    project_digests: dict[str, str],
    known_image_ids: list[str],
    base_commit: str,
    upgrade_commit: str,
    checks: list[dict[str, Any]],
) -> None:
    restart_evidence = bindings.cold_restart(
        runner,
        engine,
        project,
        checkout,
        lifecycle_script,
        compose_env,
        front,
        app,
        agent,
        api,
        projects_before,
        project_digests,
        out,
    )
    _validated_project_digests(
        restart_evidence.get("project_digests"),
        project_ids=projects_before,
        phase="cold_restart",
    )
    _record_pass(
        checks,
        "cold-restart",
        "session, projects, and exact committed data persisted across service restart",
        **restart_evidence,
    )
    upgrade_evidence = bindings.upgrade(
        runner,
        engine,
        project,
        checkout,
        lifecycle_script,
        compose_env,
        front,
        app,
        agent,
        api,
        projects_before,
        project_digests,
        known_image_ids,
        upgrade_commit,
        base_commit,
        out,
    )
    _validated_project_digests(
        upgrade_evidence.get("project_digests"),
        project_ids=projects_before,
        phase="upgrade",
    )
    _record_pass(
        checks,
        "upgrade-in-place",
        "candidate front door retained the session/configuration/projects "
        "and completed an edge build",
        **upgrade_evidence,
    )
    upgraded_image_ids = _validated_image_ids(upgrade_evidence.get("image_ids"), phase="upgrade")
    all_image_ids = sorted(set(known_image_ids) | set(upgraded_image_ids))
    _run_recovery_phases(
        bindings=bindings,
        runner=runner,
        engine=engine,
        project=project,
        checkout=checkout,
        lifecycle_script=lifecycle_script,
        compose_env=compose_env,
        out=out,
        front=front,
        app=app,
        agent=agent,
        api=api,
        projects_before=projects_before,
        project_digests=project_digests,
        known_image_ids=all_image_ids,
        checks=checks,
    )


def run_device_journey(
    *,
    bindings: FreshDeviceJourneyBindings,
    runner: Any,
    engine: Any,
    project: str,
    checkout: Path,
    lifecycle_script: Path,
    compose_env: dict[str, str],
    out: Path,
    ui_port: int,
    app_port: int,
    agent_port: int,
    base_commit: str,
    upgrade_commit: str,
    checks: list[dict[str, Any]],
) -> None:
    front, app, agent, known_image_ids = bindings.install_and_boot(
        runner, engine, project, checkout, compose_env, ui_port, app_port, agent_port
    )
    known_image_ids = _validated_image_ids(known_image_ids, phase="install")
    _record_pass(
        checks,
        "install-and-boot",
        "compose built all images; frontend assets and both backend health endpoints became ready",
        ui=front,
        image_ids=known_image_ids,
    )
    api = bindings.pair_and_config(app, front, agent)
    _record_pass(
        checks,
        "first-pair-and-config",
        "historical base was configured through its loopback migration-fixture APIs",
    )
    bindings.verify_and_scenarios(runner, engine, project, checkout, compose_env)
    _record_pass(
        checks,
        "model-and-internet",
        "configured model completed text/tool calls and the real grounded Internet path passed",
    )
    projects_before, project_digests = bindings.build_search_export(
        runner, engine, project, checkout, compose_env, out, api, agent
    )
    if not projects_before:
        raise RuntimeError("build_search_export violated its nonempty project contract")
    project_digests = _validated_project_digests(
        project_digests, project_ids=projects_before, phase="build_search_export"
    )
    _record_pass(
        checks,
        "build-search-export",
        "fresh Build and Deep Search finished; report and nonempty project ZIP were exported",
        project_count=len(projects_before),
        project_digests=project_digests,
    )
    _run_persistence_phases(
        bindings=bindings,
        runner=runner,
        engine=engine,
        project=project,
        checkout=checkout,
        lifecycle_script=lifecycle_script,
        compose_env=compose_env,
        out=out,
        front=front,
        app=app,
        agent=agent,
        api=api,
        projects_before=projects_before,
        project_digests=project_digests,
        known_image_ids=known_image_ids,
        base_commit=base_commit,
        upgrade_commit=upgrade_commit,
        checks=checks,
    )
