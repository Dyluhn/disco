"""Ordered fresh-device journey with explicit phase bindings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.reliability.state import PASS


@dataclass(frozen=True)
class FreshDeviceJourneyBindings:
    install_and_boot: Callable[..., tuple[str, str, str]]
    pair_and_config: Callable[..., Any]
    verify_and_scenarios: Callable[..., None]
    build_search_export: Callable[..., list[str]]
    cold_restart: Callable[..., None]
    upgrade: Callable[..., None]
    uninstall: Callable[..., None]


def _record_pass(
    checks: list[dict[str, Any]],
    check_id: str,
    detail: str,
    **evidence: Any,
) -> None:
    checks.append({"id": check_id, "status": PASS, "detail": detail, "evidence": evidence})


def run_device_journey(
    *,
    bindings: FreshDeviceJourneyBindings,
    runner: Any,
    engine: Any,
    project: str,
    checkout: Path,
    compose_env: dict[str, str],
    out: Path,
    ui_port: int,
    app_port: int,
    agent_port: int,
    base_commit: str,
    upgrade_commit: str,
    checks: list[dict[str, Any]],
) -> None:
    front, app, agent = bindings.install_and_boot(
        runner, engine, project, checkout, compose_env, ui_port, app_port, agent_port
    )
    _record_pass(
        checks,
        "install-and-boot",
        "compose built all images and the packaged front door plus both APIs became healthy",
        ui=front,
    )
    api = bindings.pair_and_config(app, front, agent)
    _record_pass(
        checks,
        "first-pair-and-config",
        "fresh browser-equivalent session paired and configured a driver",
    )
    bindings.verify_and_scenarios(runner, engine, project, checkout, compose_env)
    _record_pass(
        checks,
        "model-and-internet",
        "configured model completed text/tool calls and the real grounded Internet path passed",
    )
    projects_before = bindings.build_search_export(
        runner, engine, project, checkout, compose_env, out, api, agent
    )
    _record_pass(
        checks,
        "build-search-export",
        "fresh Build and Deep Search finished; report and nonempty project ZIP were exported",
        project_count=len(projects_before),
    )
    bindings.cold_restart(
        runner, engine, project, checkout, compose_env, front, agent, api, projects_before
    )
    _record_pass(
        checks, "cold-restart", "session re-paired and projects persisted across service restart"
    )
    bindings.upgrade(
        runner,
        engine,
        project,
        checkout,
        compose_env,
        front,
        app,
        agent,
        api,
        projects_before,
        upgrade_commit,
        base_commit,
        out,
    )
    _record_pass(
        checks,
        "upgrade-in-place",
        "distinct source upgrade retained configuration/projects and completed a new edge build",
        base_commit=base_commit,
        upgrade_commit=upgrade_commit,
    )
    bindings.uninstall(runner, engine, project, checkout, compose_env, out)
    _record_pass(
        checks, "uninstall", "containers, networks, volumes, local images, and clone were removed"
    )
