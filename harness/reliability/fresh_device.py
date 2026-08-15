"""Destructive acceptance for a machine with no prior Disco installation.

This program is meant to be copied/run *on the candidate device*. It proves a
real clone -> compose build -> first pairing -> configured model -> Build/Search
journeys -> restart -> source upgrade -> persisted export -> uninstall sequence.
It refuses to count a host that already has Disco containers, images, volumes,
data, or occupied default ports. The resulting machine fingerprint is used by
the campaign ledger, so ten labels for one host cannot satisfy ten-device proof.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from ._runner import ApiSession
from ._runner import _configure_driver as _configure_driver
from ._runner import fresh_device_backup as _backup
from ._runner.fresh_device_host import (
    FAIL as FAIL,
)
from ._runner.fresh_device_host import (
    INFRA,
    PASS,
    CommandRunner,
    Engine,
    FreshDeviceError,
    InfraError,
    ProductError,
    _assert_pristine,
    _canonical_image_id,
    _detect_engine,
    _machine_fingerprint,
    _phase_failure_cleanup,
    _safe_device_label,
)
from ._runner.fresh_device_host import (
    _object_lines as _object_lines,
)
from ._runner.fresh_device_host import (
    _port_available as _port_available,
)
from ._runner.fresh_device_host import (
    _utc_now as _utc_now,
)
from ._runner.fresh_device_journey import (
    FreshDeviceJourneyBindings,
    _record_pass,
    run_device_journey,
)


def _wait_url(url: str, *, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - loopback
                if 200 <= response.status < 300:
                    return response.read()
        except (OSError, urllib.error.URLError) as exc:
            last = str(exc)
        time.sleep(2)
    raise ProductError(f"service did not become ready at {url}: {last}")


def _compose_command(engine: Engine, project: str, *args: str) -> list[str]:
    return [*engine.compose, "-p", project, *args]


def _compose_image_ids(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    compose_env: dict[str, str],
    name: str,
) -> list[str]:
    result = runner.run(
        name,
        _compose_command(engine, project, "images", "--quiet"),
        cwd=checkout,
        env=compose_env,
        timeout=300,
    )
    image_ids: set[str] = set()
    for line in result.stdout.splitlines():
        try:
            image_ids.add(_canonical_image_id(line))
        except ValueError:
            continue
    return sorted(image_ids)


def _assert_verify_output(output: str, *, grounding: bool) -> None:
    for name in ("config", "completion", "tool-calling"):
        if not re.search(rf"\bPASS\s+{re.escape(name)}\b", output):
            raise ProductError(f"disco-verify did not PASS {name}: {output[-2_000:]}")
    if grounding and not re.search(r"\bPASS\s+grounding\b", output):
        raise ProductError(
            "fresh-device internet/grounding proof was not a PASS (SKIP never counts): "
            + output[-2_000:]
        )


def _project_ids(payload: Any) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("projects"), list):
        raise ProductError("project list response has the wrong shape")
    project_ids: list[str] = []
    for item in payload["projects"]:
        if not isinstance(item, dict):
            raise ProductError("project list contains a malformed entry")
        if item.get("files_missing"):
            continue
        project_id = item.get("id")
        if (
            not isinstance(project_id, str)
            or not project_id
            or project_id != project_id.strip()
        ):
            raise ProductError("project list contains an invalid project identity")
        project_ids.append(project_id)
    if len(project_ids) != len(set(project_ids)):
        raise ProductError("project list contains a duplicate project identity")
    return sorted(project_ids)


def _validate_zip(data: bytes, target: Path) -> list[str]:
    target.write_bytes(data)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [item.filename for item in archive.infolist() if not item.is_dir()]
            if not names:
                raise ProductError("project export zip contains no files")
            bad = archive.testzip()
            if bad:
                raise ProductError(f"project export zip has a corrupt member: {bad}")
            return names
    except zipfile.BadZipFile as exc:
        raise ProductError(f"project export is not a ZIP: {exc}") from exc


_sha256_bytes = _backup._sha256_bytes


def _export_project_digests(
    api: ApiSession,
    agent: str,
    project_ids: list[str],
    out: Path,
    phase: str,
) -> dict[str, str]:
    """Export and bind every persisted project to deterministic evidence."""
    digests: dict[str, str] = {}
    for index, project_id in enumerate(sorted(project_ids), start=1):
        encoded_id = urllib.parse.quote(project_id, safe="")
        exported = api.bytes(agent, f"/api/projects/{encoded_id}/download")
        _validate_zip(exported, out / f"project-{index:03d}-{phase}.zip")
        digests[project_id] = _sha256_bytes(exported)
    return digests


_backup_entry_rows = _backup._backup_entry_rows
_database_logical_digests = _backup._database_logical_digests
_manifest_digest = _backup._manifest_digest


def _verified_backup_archive(path: Path) -> tuple[dict[str, Any], bytes]:
    return _backup._verified_backup_archive(path, entry_rows=_backup_entry_rows)


def _backup_manifest(path: Path) -> dict[str, Any]:
    manifest, database_bytes = _verified_backup_archive(path)
    committed_digest, schema_digest = _database_logical_digests(database_bytes)
    database = manifest["database"]
    manifest = dict(manifest)
    manifest["database"] = {
        **database,
        "committed_sha256": committed_digest,
        "schema_sha256": schema_digest,
    }
    return manifest


def _assert_duration(name: str, started: float, limit_seconds: float) -> float:
    duration = time.monotonic() - started
    if duration > limit_seconds:
        raise ProductError(f"{name} readiness took {duration:.3f}s; limit is {limit_seconds:.0f}s")
    return round(duration, 3)


def _run_data_lifecycle(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    lifecycle_script: Path,
    compose_env: dict[str, str],
    name: str,
    *arguments: str,
    timeout: float = 1_800,
) -> None:
    runner.run(
        name,
        [
            sys.executable,
            str(lifecycle_script),
            "--engine",
            engine.binary,
            "--compose-file",
            str(checkout / "compose.yaml"),
            "--project-name",
            project,
            *arguments,
        ],
        cwd=checkout,
        env=compose_env,
        timeout=timeout,
    )


def _create_backup(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    lifecycle_script: Path,
    compose_env: dict[str, str],
    out: Path,
    name: str,
) -> tuple[Path, dict[str, Any]]:
    archive = out / f"{name}.tar.gz"
    _run_data_lifecycle(
        runner,
        engine,
        project,
        checkout,
        lifecycle_script,
        compose_env,
        name,
        "backup",
        "--output",
        str(archive),
    )
    if not archive.is_file() or archive.stat().st_size == 0:
        raise ProductError(f"{name} did not produce a backup archive")
    manifest = _backup_manifest(archive)
    (out / f"{name}.manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return archive, manifest


def _write_result(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _phase_clone_and_verify(
    runner: CommandRunner, args: argparse.Namespace, checkout: Path
) -> tuple[str, str]:
    """Clone the repo and verify base/upgrade refs resolve to distinct commits."""
    runner.run(
        "clone",
        ["git", "clone", "--filter=blob:none", "--no-checkout", args.repo_url, str(checkout)],
        timeout=900,
    )
    runner.run(
        "fetch-refs",
        ["git", "fetch", "--force", "origin", args.base_ref, args.upgrade_ref],
        cwd=checkout,
        timeout=900,
    )
    runner.run("checkout-base", ["git", "checkout", "--detach", args.base_ref], cwd=checkout)
    base_commit = runner.run(
        "base-revision", ["git", "rev-parse", "HEAD"], cwd=checkout
    ).stdout.strip()
    upgrade_commit = runner.run(
        "upgrade-revision",
        ["git", "rev-parse", f"{args.upgrade_ref}^{{commit}}"],
        cwd=checkout,
    ).stdout.strip()
    if base_commit == upgrade_commit:
        raise InfraError("base and upgrade refs resolve to the same commit")
    if upgrade_commit != args.expected_upgrade_commit:
        raise InfraError(
            "upgrade ref does not resolve to the campaign revision: "
            f"expected {args.expected_upgrade_commit}, got {upgrade_commit}"
        )
    return base_commit, upgrade_commit


def _phase_compose_env(
    engine: Engine,
    project: str,
    fingerprint: str,
    device_label: str,
    ui_port: int,
    app_port: int,
    agent_port: int,
) -> dict[str, str]:
    """Build the compose environment for the fresh-device stack."""
    secret = secrets.token_urlsafe(48)
    return {
        **os.environ,
        "COMPOSE_PROJECT_NAME": project,
        "DISCO_BIND": "127.0.0.1",
        "DISCO_UI_PORT": str(ui_port),
        "DISCO_APP_PORT": str(app_port),
        "DISCO_AGENT_PORT": str(agent_port),
        "DISCO_PUBLIC_UI_URL": f"http://127.0.0.1:{ui_port}",
        "DISCO_SECRET_KEY": secret,
        "DISCO_SANDBOX_SOCKET": engine.sandbox_socket,
        "DISCO_LOCAL_ENGINE": engine.binary,
        "DISCO_INSPECT": "1",
        "DISCO_LOG_JSON": "1",
    }


def _phase_install_and_boot(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    compose_env: dict[str, str],
    ui_port: int,
    app_port: int,
    agent_port: int,
) -> tuple[str, str, str, list[str]]:
    """Build and boot the compose stack; return front/app/agent URLs."""
    runner.run(
        "compose-config-base",
        _compose_command(engine, project, "config"),
        cwd=checkout,
        env=compose_env,
    )
    runner.run(
        "install-build-up",
        _compose_command(engine, project, "up", "-d", "--build"),
        cwd=checkout,
        env=compose_env,
        timeout=7_200,
    )
    front = f"http://127.0.0.1:{ui_port}"
    app = f"http://127.0.0.1:{app_port}"
    agent = f"http://127.0.0.1:{agent_port}"
    _wait_url(f"{front}/env.js", timeout=600)
    _wait_url(f"{app}/api/health", timeout=600)
    _wait_url(f"{agent}/health", timeout=600)
    index = _wait_url(front, timeout=60).decode("utf-8", errors="replace")
    env_js = _wait_url(f"{front}/env.js", timeout=60).decode("utf-8", errors="replace")
    if "root" not in index or "/svc/app" not in env_js or "/svc/agent" not in env_js:
        raise ProductError("packaged frontend or runtime API routing is incomplete")
    boot_logs = runner.run(
        "initial-app-logs",
        _compose_command(engine, project, "logs", "--no-color", "app-server"),
        cwd=checkout,
        env=compose_env,
    ).stdout
    if "First-run admin pairing" not in boot_logs or str(ui_port) not in boot_logs:
        raise ProductError("boot logs did not print first-run pairing guidance and UI URL")
    image_ids = _compose_image_ids(
        runner,
        engine,
        project,
        checkout,
        compose_env,
        "initial-compose-images",
    )
    if not image_ids:
        raise ProductError("installed Compose stack exposed no image identities")
    return front, app, agent, image_ids


def _phase_verify_and_scenarios(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    compose_env: dict[str, str],
) -> None:
    """Run model verify and surface scenarios inside the agent-server container."""
    quick = runner.run(
        "initial-model-verify",
        _compose_command(engine, project, "exec", "-T", "agent-server", "disco-verify", "--quick"),
        cwd=checkout,
        env=compose_env,
        timeout=1_200,
    )
    _assert_verify_output(quick.stdout, grounding=False)
    grounded = runner.run(
        "initial-grounding-verify",
        _compose_command(engine, project, "exec", "-T", "agent-server", "disco-verify"),
        cwd=checkout,
        env=compose_env,
        timeout=1_800,
    )
    _assert_verify_output(grounded.stdout, grounding=True)
    scenarios = runner.run(
        "initial-surface-scenarios",
        _compose_command(
            engine,
            project,
            "exec",
            "-T",
            "agent-server",
            "python",
            "-m",
            "disco.agent_server.verify.scenarios_run",
            "--only",
            "app_from_build",
            "research_report_export",
        ),
        cwd=checkout,
        env=compose_env,
        timeout=2_400,
    )
    if (
        "[PASS] app_from_build" not in scenarios.stdout
        or "[PASS] research_report_export" not in scenarios.stdout
    ):
        raise ProductError("fresh Build/Search scenarios did not both pass")


def _phase_upgrade(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    lifecycle_script: Path,
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
    from ._runner.fresh_device_phases import phase_upgrade

    return phase_upgrade(
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


def _phase_backup_restore(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    lifecycle_script: Path,
    compose_env: dict[str, str],
    front: str,
    app: str,
    agent: str,
    api: ApiSession,
    original_project_ids: list[str],
    original_project_digests: dict[str, str],
    out: Path,
) -> dict[str, Any]:
    from ._runner.fresh_device_phases import phase_backup_restore

    return phase_backup_restore(
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
        original_project_ids,
        original_project_digests,
        out,
    )


def _phase_uninstall(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    compose_env: dict[str, str],
    out: Path,
    known_image_ids: list[str],
) -> None:
    from ._runner.fresh_device_phases import phase_uninstall

    phase_uninstall(
        runner,
        engine,
        project,
        checkout,
        compose_env,
        out,
        known_image_ids,
    )


def _phase_pair_and_config(app: str, front: str, agent: str) -> ApiSession:
    from ._runner.fresh_device_phases import phase_pair_and_config

    return phase_pair_and_config(app, front, agent)


def _phase_build_search_export(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    compose_env: dict[str, str],
    out: Path,
    api: ApiSession,
    agent: str,
) -> tuple[list[str], dict[str, str]]:
    from ._runner.fresh_device_phases import phase_build_search_export

    return phase_build_search_export(
        runner, engine, project, checkout, compose_env, out, api, agent
    )


def _phase_cold_restart(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    lifecycle_script: Path,
    compose_env: dict[str, str],
    front: str,
    app: str,
    agent: str,
    api: ApiSession,
    projects_before: list[str],
    project_digests: dict[str, str],
    out: Path,
) -> dict[str, Any]:
    from ._runner.fresh_device_phases import phase_cold_restart

    return phase_cold_restart(
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


def _parse_fresh_device_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the fresh-device CLI arguments."""
    parser = argparse.ArgumentParser(description="Run Disco on a demonstrably clean device")
    parser.add_argument("--out", required=True)
    parser.add_argument("--repo-url", default=os.environ.get("DISCO_FRESH_REPO_URL", ""))
    parser.add_argument("--base-ref", default=os.environ.get("DISCO_FRESH_BASE_REF", ""))
    parser.add_argument("--upgrade-ref", default=os.environ.get("DISCO_FRESH_UPGRADE_REF", ""))
    parser.add_argument(
        "--expected-upgrade-commit",
        default=os.environ.get("DISCO_FRESH_EXPECTED_UPGRADE_COMMIT", ""),
    )
    parser.add_argument("--keep-on-failure", action="store_true")
    return parser.parse_args(argv)


def _write_fresh_device_result(
    result_path: Path,
    status: str,
    reason: str,
    fingerprint: str,
    started_at: str,
    checks: list,
) -> None:
    """Write the fresh-device result JSON and print it."""
    payload = {
        "schema_version": 1,
        "status": status,
        "reason": reason,
        "device_fingerprint": fingerprint,
        "device_label": os.environ.get("DISCO_FRESH_DEVICE_ID", fingerprint),
        "started_at": started_at,
        "finished_at": _utc_now(),
        "checks": checks,
    }
    _write_result(result_path, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))


def _phase_preflight(
    args: argparse.Namespace,
    runner: CommandRunner,
) -> tuple[str, str, Engine, int, int, int, dict]:
    """Validate inputs, detect engine, assert pristine; return fingerprint/label/ports/pristine."""
    required = {
        "repo URL": args.repo_url,
        "base ref": args.base_ref,
        "upgrade ref": args.upgrade_ref,
        "expected upgrade commit": args.expected_upgrade_commit,
        "driver base URL": os.environ.get("DISCO_FRESH_DRIVER_BASE_URL", ""),
        "driver model": os.environ.get("DISCO_FRESH_DRIVER_MODEL", ""),
    }
    missing = [name for name, value in required.items() if not str(value).strip()]
    if missing:
        raise InfraError(f"missing fresh-device inputs: {', '.join(missing)}")
    fingerprint = _machine_fingerprint()
    device_label = os.environ.get("DISCO_FRESH_DEVICE_ID", fingerprint)
    engine = _detect_engine(runner)
    ui_port = int(os.environ.get("DISCO_FRESH_UI_PORT", "8088"))
    app_port = int(os.environ.get("DISCO_FRESH_APP_PORT", "8800"))
    agent_port = int(os.environ.get("DISCO_FRESH_AGENT_PORT", "8000"))
    pristine = _assert_pristine(runner, engine, ports=(ui_port, app_port, agent_port))
    return fingerprint, device_label, engine, ui_port, app_port, agent_port, pristine


def _candidate_lifecycle_script() -> Path:
    """Return the candidate controller tool, independent of the mutable stack checkout."""
    script = Path(__file__).resolve().parents[2] / "scripts/self_host_data.py"
    if not script.is_file():
        raise InfraError(f"candidate lifecycle controller is absent: {script}")
    return script


def main(argv: list[str] | None = None) -> int:
    args = _parse_fresh_device_args(argv)

    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    result_path = out / "fresh-device-result.json"
    started_at = _utc_now()
    fingerprint = "unknown"
    checks: list[dict[str, Any]] = []
    checkout = out / "fresh-clone"
    runner = CommandRunner(out)
    engine: Engine | None = None
    compose_env: dict[str, str] | None = None
    project = ""
    status = INFRA
    reason = "fresh-device run did not start"

    try:
        (
            fingerprint,
            device_label,
            engine,
            ui_port,
            app_port,
            agent_port,
            pristine,
        ) = _phase_preflight(args, runner)
        _record_pass(
            checks,
            "pristine-device",
            "no prior Disco containers, images, volumes, data paths, or bound ports",
            device_label=device_label,
            fingerprint=fingerprint,
            engine=engine.binary,
            **pristine,
        )

        base_commit, upgrade_commit = _phase_clone_and_verify(runner, args, checkout)
        lifecycle_script = _candidate_lifecycle_script()
        _record_pass(
            checks,
            "clean-clone",
            "candidate was cloned into a new directory and two distinct refs resolved",
            base_commit=base_commit,
            upgrade_commit=upgrade_commit,
            lifecycle_script_sha256=_sha256_bytes(lifecycle_script.read_bytes()),
        )

        project = f"discofresh-{_safe_device_label(device_label)}-{fingerprint[:8]}"
        compose_env = _phase_compose_env(
            engine, project, fingerprint, device_label, ui_port, app_port, agent_port
        )
        run_device_journey(
            bindings=FreshDeviceJourneyBindings(
                install_and_boot=_phase_install_and_boot,
                pair_and_config=_phase_pair_and_config,
                verify_and_scenarios=_phase_verify_and_scenarios,
                build_search_export=_phase_build_search_export,
                cold_restart=_phase_cold_restart,
                upgrade=_phase_upgrade,
                backup_restore=_phase_backup_restore,
                uninstall=_phase_uninstall,
            ),
            runner=runner,
            engine=engine,
            project=project,
            checkout=checkout,
            lifecycle_script=lifecycle_script,
            compose_env=compose_env,
            out=out,
            ui_port=ui_port,
            app_port=app_port,
            agent_port=agent_port,
            base_commit=base_commit,
            upgrade_commit=upgrade_commit,
            checks=checks,
        )
        status = PASS
        reason = (
            "fresh install, live use/export, restart, upgrade, backup/restore, "
            "and uninstall all passed"
        )
    except FreshDeviceError as exc:
        status = exc.status
        reason = str(exc)
        checks.append({"id": "failure", "status": status, "detail": reason, "evidence": {}})
    except Exception as exc:  # noqa: BLE001 - preserve unexpected harness failures as INFRA
        status = INFRA
        reason = f"unexpected harness error: {type(exc).__name__}: {exc}"
        checks.append({"id": "failure", "status": INFRA, "detail": reason, "evidence": {}})
    finally:
        if engine is not None and compose_env is not None and project and checkout.exists():
            _phase_failure_cleanup(
                runner, engine, project, checkout, compose_env, status, args.keep_on_failure
            )
        _write_fresh_device_result(result_path, status, reason, fingerprint, started_at, checks)
    return 0 if status == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
