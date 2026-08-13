"""Upgrade, restore and uninstall phases for fresh-device certification."""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from . import ApiSession
from .fresh_device_host import CommandRunner, Engine, ProductError, _canonical_image_id


def phase_pair_and_config(app: str, front: str, agent: str) -> ApiSession:
    """Pair a fresh session and configure the driver."""
    import os

    from .. import fresh_device as fd

    api = fd.ApiSession(app_base=app, agent_base=agent, origin=front)
    api.pair()
    fd._configure_driver(
        api,
        base_url=os.environ["DISCO_FRESH_DRIVER_BASE_URL"].strip(),
        model=os.environ["DISCO_FRESH_DRIVER_MODEL"].strip(),
        api_key=os.environ.get("DISCO_FRESH_DRIVER_API_KEY", "").strip(),
    )
    assignments = api.json("GET", app, "/api/models/assignments")
    if assignments.get("default_model") != "fresh-device-driver":
        raise ProductError("driver assignment did not round-trip through Settings API")
    return api


def phase_build_search_export(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    compose_env: dict[str, str],
    out: Path,
    api: ApiSession,
    agent: str,
) -> tuple[list[str], dict[str, str]]:
    """Copy dossiers, verify projects, and bind every exported ZIP."""
    from .. import fresh_device as fd

    runner.run(
        "copy-initial-dossiers",
        fd._compose_command(
            engine,
            project,
            "cp",
            "agent-server:/app/test-record/disco-verify",
            str(out / "initial-dossiers"),
        ),
        cwd=checkout,
        env=compose_env,
        timeout=300,
    )
    projects_before = fd._project_ids(api.json("GET", agent, "/api/projects"))
    if not projects_before:
        raise ProductError("Build scenario produced no persisted project")
    return projects_before, fd._export_project_digests(
        api, agent, projects_before, out, "before-upgrade"
    )


def phase_cold_restart(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    compose_env: dict[str, str],
    front: str,
    app: str,
    agent: str,
    api: ApiSession,
    projects_before: list[str],
    project_digests: dict[str, str],
    out: Path,
) -> dict[str, Any]:
    """Restart the stack and prove exact committed-data continuity."""
    from .. import fresh_device as fd

    _, manifest_before = fd._create_backup(
        runner, engine, project, checkout, compose_env, out, "restart-before"
    )
    before_digest = fd._manifest_digest(manifest_before, include_database=True)
    started = time.monotonic()
    runner.run(
        "restart-stack",
        fd._compose_command(engine, project, "restart"),
        cwd=checkout,
        env=compose_env,
        timeout=600,
    )
    fd._wait_url(f"{agent}/health", timeout=600)
    fd._wait_url(f"{app}/api/health", timeout=600)
    fd._wait_url(f"{front}/env.js", timeout=600)
    readiness_seconds = fd._assert_duration("restart", started, 900)
    if fd._project_ids(api.json("GET", agent, "/api/projects")) != projects_before:
        raise ProductError("project identity changed across a full stack restart")
    current_project_digests = fd._export_project_digests(
        api, agent, projects_before, out, "after-restart"
    )
    if current_project_digests != project_digests:
        raise ProductError("project bytes changed across a full stack restart")
    _, manifest_after = fd._create_backup(
        runner, engine, project, checkout, compose_env, out, "restart-after"
    )
    after_digest = fd._manifest_digest(manifest_after, include_database=True)
    if after_digest != before_digest:
        raise ProductError("committed data hash changed across a full stack restart")
    return {
        "readiness_seconds": readiness_seconds,
        "data_digest": after_digest,
        "project_digests": current_project_digests,
    }


def _upgrade_stack(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    compose_env: dict[str, str],
    front: str,
    app: str,
    agent: str,
    upgrade_commit: str,
) -> tuple[float, list[str]]:
    from .. import fresh_device as fd

    started = time.monotonic()
    runner.run(
        "stop-before-upgrade",
        fd._compose_command(engine, project, "down", "--remove-orphans"),
        cwd=checkout,
        env=compose_env,
        timeout=600,
    )
    runner.run(
        "checkout-upgrade",
        ["git", "checkout", "--detach", upgrade_commit],
        cwd=checkout,
        timeout=300,
    )
    runner.run(
        "upgrade-build-up",
        fd._compose_command(engine, project, "up", "-d", "--build"),
        cwd=checkout,
        env=compose_env,
        timeout=7_200,
    )
    fd._wait_url(f"{front}/env.js", timeout=600)
    fd._wait_url(f"{app}/api/health", timeout=600)
    fd._wait_url(f"{agent}/health", timeout=600)
    readiness_seconds = fd._assert_duration("upgrade", started, 900)
    image_ids = fd._compose_image_ids(
        runner,
        engine,
        project,
        checkout,
        compose_env,
        "upgraded-compose-images",
    )
    if not image_ids:
        raise ProductError("upgraded Compose stack exposed no image identities")
    return readiness_seconds, image_ids


def _assert_upgrade_persistence(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    compose_env: dict[str, str],
    app: str,
    agent: str,
    api: ApiSession,
    projects_before: list[str],
    project_digests: dict[str, str],
    stable_before: str,
    out: Path,
) -> tuple[dict[str, str], str]:
    from .. import fresh_device as fd

    if fd._project_ids(api.json("GET", agent, "/api/projects")) != projects_before:
        raise ProductError("upgrade lost or rewrote persisted projects")
    assignments = api.json("GET", app, "/api/models/assignments")
    if assignments.get("default_model") != "fresh-device-driver":
        raise ProductError("upgrade lost the configured driver assignment")
    current_project_digests = fd._export_project_digests(
        api, agent, projects_before, out, "after-upgrade"
    )
    if current_project_digests != project_digests:
        raise ProductError("upgrade changed committed project bytes")
    _, manifest_after = fd._create_backup(
        runner,
        engine,
        project,
        checkout,
        compose_env,
        out,
        "upgrade-after",
    )
    stable_after = fd._manifest_digest(manifest_after, include_database=False)
    if stable_after != stable_before:
        raise ProductError("upgrade changed committed config, settings, secrets, or project files")
    return current_project_digests, stable_after


def _run_post_upgrade_checks(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    compose_env: dict[str, str],
) -> None:
    from .. import fresh_device as fd

    verify = runner.run(
        "post-upgrade-model-verify",
        fd._compose_command(
            engine, project, "exec", "-T", "agent-server", "disco-verify", "--quick"
        ),
        cwd=checkout,
        env=compose_env,
        timeout=1_200,
    )
    fd._assert_verify_output(verify.stdout, grounding=False)
    edge = runner.run(
        "post-upgrade-edge-scenario",
        fd._compose_command(
            engine,
            project,
            "exec",
            "-T",
            "agent-server",
            "python",
            "-m",
            "disco.agent_server.verify.scenarios_run",
            "--only",
            "missing_file_sandbox_error",
        ),
        cwd=checkout,
        env=compose_env,
        timeout=900,
    )
    if "[PASS] missing_file_sandbox_error" not in edge.stdout:
        raise ProductError("post-upgrade build edge scenario failed")


def phase_upgrade(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    compose_env: dict[str, str],
    front: str,
    app: str,
    agent: str,
    api: ApiSession,
    projects_before: list[str],
    project_digests: dict[str, str],
    known_image_ids: list[str],
    upgrade_commit: str,
    base_commit: str,
    out: Path,
) -> dict[str, Any]:
    """Stop, upgrade source, rebuild, and prove additive data continuity."""
    from .. import fresh_device as fd

    _, manifest_before = fd._create_backup(
        runner,
        engine,
        project,
        checkout,
        compose_env,
        out,
        "upgrade-before",
    )
    stable_before = fd._manifest_digest(manifest_before, include_database=False)
    readiness_seconds, current_image_ids = _upgrade_stack(
        runner,
        engine,
        project,
        checkout,
        compose_env,
        front,
        app,
        agent,
        upgrade_commit,
    )
    current_project_digests, stable_after = _assert_upgrade_persistence(
        runner,
        engine,
        project,
        checkout,
        compose_env,
        app,
        agent,
        api,
        projects_before,
        project_digests,
        stable_before,
        out,
    )
    _run_post_upgrade_checks(runner, engine, project, checkout, compose_env)
    return {
        "readiness_seconds": readiness_seconds,
        "stable_data_digest": stable_after,
        "project_digests": current_project_digests,
        "base_commit": base_commit,
        "upgrade_commit": upgrade_commit,
        "image_ids": sorted(set(known_image_ids) | set(current_image_ids)),
    }


def phase_backup_restore(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    compose_env: dict[str, str],
    front: str,
    app: str,
    agent: str,
    api: ApiSession,
    original_project_ids: list[str],
    original_project_digests: dict[str, str],
    out: Path,
) -> dict[str, Any]:
    """Destroy the data volume, restore a selected backup, and prove RPO 0."""
    from .. import fresh_device as fd

    projects_at_snapshot = fd._project_ids(api.json("GET", agent, "/api/projects"))
    if projects_at_snapshot != original_project_ids:
        raise ProductError("backup snapshot contains an unbound project identity")
    selected_archive, selected_manifest = fd._create_backup(
        runner, engine, project, checkout, compose_env, out, "selected-backup"
    )
    selected_digest = fd._manifest_digest(selected_manifest, include_database=True)
    readiness_seconds = _restore_selected_archive(
        runner,
        engine,
        project,
        checkout,
        compose_env,
        front,
        app,
        agent,
        selected_archive,
    )
    _assert_restored_volume(
        runner,
        engine,
        project,
        checkout,
        compose_env,
        app,
        agent,
        out,
        selected_digest,
    )
    restored_project_digests = _assert_restored_api_state(
        api,
        app,
        agent,
        projects_at_snapshot,
        original_project_ids,
        original_project_digests,
        out,
    )
    return {
        "readiness_seconds": readiness_seconds,
        "backup_digest": selected_digest,
        "project_digests": restored_project_digests,
        "project_count": len(projects_at_snapshot),
    }


def _restore_selected_archive(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    compose_env: dict[str, str],
    front: str,
    app: str,
    agent: str,
    archive: Path,
) -> float:
    from .. import fresh_device as fd

    started = time.monotonic()
    runner.run(
        "destroy-data-before-restore",
        fd._compose_command(engine, project, "down", "--volumes", "--remove-orphans"),
        cwd=checkout,
        env=compose_env,
        timeout=1_200,
    )
    fd._run_data_lifecycle(
        runner,
        engine,
        project,
        checkout,
        compose_env,
        "restore-selected-backup",
        "restore",
        "--archive",
        str(archive),
        timeout=3_600,
    )
    fd._wait_url(f"{front}/env.js", timeout=600)
    fd._wait_url(f"{app}/api/health", timeout=600)
    fd._wait_url(f"{agent}/health", timeout=600)
    return fd._assert_duration("backup restore", started, 3_600)


def _assert_restored_volume(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    compose_env: dict[str, str],
    app: str,
    agent: str,
    out: Path,
    selected_digest: str,
) -> None:
    from .. import fresh_device as fd

    _, restored_manifest = fd._create_backup(
        runner, engine, project, checkout, compose_env, out, "restored-backup"
    )
    restored_digest = fd._manifest_digest(restored_manifest, include_database=True)
    if restored_digest != selected_digest:
        raise ProductError("restored data does not match the selected backup")
    fd._wait_url(f"{app}/api/health", timeout=600)
    fd._wait_url(f"{agent}/health", timeout=600)


def _assert_restored_api_state(
    api: ApiSession,
    app: str,
    agent: str,
    projects_at_snapshot: list[str],
    original_project_ids: list[str],
    original_project_digests: dict[str, str],
    out: Path,
) -> dict[str, str]:
    from .. import fresh_device as fd

    if fd._project_ids(api.json("GET", agent, "/api/projects")) != projects_at_snapshot:
        raise ProductError("backup restore lost or rewrote persisted projects")
    assignments = api.json("GET", app, "/api/models/assignments")
    if assignments.get("default_model") != "fresh-device-driver":
        raise ProductError("backup restore lost the configured driver assignment")
    restored = fd._export_project_digests(
        api, agent, original_project_ids, out, "after-restore"
    )
    if restored != original_project_digests:
        raise ProductError("backup restore changed committed project bytes")
    return restored


def _known_compose_images(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    compose_env: dict[str, str],
    known_image_ids: list[str],
) -> set[str]:
    from .. import fresh_device as fd

    observed = fd._compose_image_ids(
        runner,
        engine,
        project,
        checkout,
        compose_env,
        "pre-uninstall-compose-images",
    )
    return {_canonical_image_id(image_id) for image_id in known_image_ids} | set(observed)


def _remove_retired_images(
    runner: CommandRunner,
    engine: Engine,
    known_images: set[str],
) -> list[str]:
    from .. import fresh_device as fd

    all_image_ids = {
        _canonical_image_id(image_id)
        for image_id in fd._object_lines(
            runner,
            engine,
            "post-uninstall-all-image-ids",
            ["image", "ls", "--all", "--quiet", "--no-trunc"],
        )
    }
    remaining = sorted(image_id for image_id in known_images if image_id in all_image_ids)
    if not remaining:
        return []
    runner.run(
        "uninstall-retired-images",
        [engine.binary, "image", "rm", *remaining],
        timeout=600,
    )
    final_image_ids = {
        _canonical_image_id(image_id)
        for image_id in fd._object_lines(
            runner,
            engine,
            "post-uninstall-final-image-ids",
            ["image", "ls", "--all", "--quiet", "--no-trunc"],
        )
    }
    return sorted(image_id for image_id in known_images if image_id in final_image_ids)


def _uninstall_inventory(
    runner: CommandRunner,
    engine: Engine,
    project: str,
) -> tuple[list[str], list[str], list[str], list[str]]:
    from .. import fresh_device as fd

    containers = fd._object_lines(
        runner,
        engine,
        "post-uninstall-containers",
        ["ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
    )
    volumes = fd._object_lines(
        runner,
        engine,
        "post-uninstall-volumes",
        ["volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"],
    )
    images = fd._object_lines(
        runner,
        engine,
        "post-uninstall-images",
        [
            "images",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ],
    )
    networks = fd._object_lines(
        runner,
        engine,
        "post-uninstall-networks",
        ["network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"],
    )
    return containers, volumes, images, networks


def phase_uninstall(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    compose_env: dict[str, str],
    out: Path,
    known_image_ids: list[str],
) -> None:
    """Uninstall the stack and verify no remnants remain."""
    from .. import fresh_device as fd

    del out
    known_images = _known_compose_images(
        runner, engine, project, checkout, compose_env, known_image_ids
    )
    runner.run(
        "final-compose-logs",
        fd._compose_command(engine, project, "logs", "--no-color"),
        cwd=checkout,
        env=compose_env,
        timeout=300,
        check=False,
    )
    runner.run(
        "uninstall",
        fd._compose_command(
            engine, project, "down", "--volumes", "--remove-orphans", "--rmi", "all"
        ),
        cwd=checkout,
        env=compose_env,
        timeout=1_200,
    )
    remaining_known_images = _remove_retired_images(runner, engine, known_images)
    containers, volumes, images, networks = _uninstall_inventory(runner, engine, project)
    if containers or volumes or images or remaining_known_images or networks:
        raise ProductError(
            "uninstall left "
            f"containers={containers} volumes={volumes} "
            f"images={images} known_images={remaining_known_images} networks={networks}"
        )
    shutil.rmtree(checkout)
    if checkout.exists():
        raise ProductError("uninstall left the cloned application directory")
