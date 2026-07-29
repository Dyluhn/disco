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
import contextlib
import io
import json
import os
import re
import secrets
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._runner import ApiSession, _configure_driver
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
    _detect_engine,
    _machine_fingerprint,
    _object_lines,
    _safe_device_label,
)
from ._runner.fresh_device_host import (
    _port_available as _port_available,
)
from ._runner.fresh_device_journey import (
    FreshDeviceJourneyBindings,
    _record_pass,
    run_device_journey,
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


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
    return [
        str(item.get("id"))
        for item in payload["projects"]
        if isinstance(item, dict) and item.get("id") and not item.get("files_missing")
    ]


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
) -> tuple[str, str, str]:
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
    return front, app, agent


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
    compose_env: dict[str, str],
    front: str,
    app: str,
    agent: str,
    api: ApiSession,
    projects_before: list,
    upgrade_commit: str,
    base_commit: str,
    out: Path,
) -> None:
    """Stop, upgrade source, rebuild, verify post-upgrade state."""
    runner.run(
        "stop-before-upgrade",
        _compose_command(engine, project, "down", "--remove-orphans"),
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
        _compose_command(engine, project, "up", "-d", "--build"),
        cwd=checkout,
        env=compose_env,
        timeout=7_200,
    )
    _wait_url(f"{front}/env.js", timeout=600)
    _wait_url(f"{app}/api/health", timeout=600)
    _wait_url(f"{agent}/health", timeout=600)
    api.pair()
    if _project_ids(api.json("GET", agent, "/api/projects")) != projects_before:
        raise ProductError("upgrade lost or rewrote persisted projects")
    if (
        api.json("GET", app, "/api/models/assignments").get("default_model")
        != "fresh-device-driver"
    ):
        raise ProductError("upgrade lost the configured driver assignment")
    post_upgrade = runner.run(
        "post-upgrade-model-verify",
        _compose_command(engine, project, "exec", "-T", "agent-server", "disco-verify", "--quick"),
        cwd=checkout,
        env=compose_env,
        timeout=1_200,
    )
    _assert_verify_output(post_upgrade.stdout, grounding=False)
    edge = runner.run(
        "post-upgrade-edge-scenario",
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
            "missing_file_sandbox_error",
        ),
        cwd=checkout,
        env=compose_env,
        timeout=900,
    )
    if "[PASS] missing_file_sandbox_error" not in edge.stdout:
        raise ProductError("post-upgrade build edge scenario failed")
    export_after = api.bytes(agent, f"/api/projects/{projects_before[0]}/download")
    _validate_zip(export_after, out / "project-after-upgrade.zip")


def _phase_uninstall(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    compose_env: dict[str, str],
    out: Path,
) -> None:
    """Uninstall the stack and verify no remnants remain."""
    runner.run(
        "final-compose-logs",
        _compose_command(engine, project, "logs", "--no-color"),
        cwd=checkout,
        env=compose_env,
        timeout=300,
        check=False,
    )
    runner.run(
        "uninstall",
        _compose_command(engine, project, "down", "--volumes", "--remove-orphans", "--rmi", "all"),
        cwd=checkout,
        env=compose_env,
        timeout=1_200,
    )
    remaining = _object_lines(
        runner,
        engine,
        "post-uninstall-containers",
        ["ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
    )
    remaining_volumes = _object_lines(
        runner,
        engine,
        "post-uninstall-volumes",
        ["volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"],
    )
    if remaining or remaining_volumes:
        raise ProductError(f"uninstall left containers={remaining} volumes={remaining_volumes}")
    shutil.rmtree(checkout)
    if checkout.exists():
        raise ProductError("uninstall left the cloned application directory")


def _phase_failure_cleanup(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    compose_env: dict[str, str],
    status: str,
    keep_on_failure: bool,
) -> None:
    """Best-effort cleanup on failure (logs + teardown)."""
    with contextlib.suppress(Exception):
        runner.run(
            "failure-compose-logs",
            _compose_command(engine, project, "logs", "--no-color"),
            cwd=checkout,
            env=compose_env,
            timeout=180,
            check=False,
        )
    if status != PASS and not keep_on_failure:
        with contextlib.suppress(Exception):
            runner.run(
                "failure-cleanup",
                _compose_command(
                    engine, project, "down", "--volumes", "--remove-orphans", "--rmi", "all"
                ),
                cwd=checkout,
                env=compose_env,
                timeout=1_200,
                check=False,
            )
        shutil.rmtree(checkout, ignore_errors=True)


def _phase_pair_and_config(app: str, front: str, agent: str) -> ApiSession:
    """Pair a fresh session and configure the driver; return the API session."""
    api = ApiSession(app_base=app, agent_base=agent, origin=front)
    api.pair()
    _configure_driver(
        api,
        base_url=os.environ["DISCO_FRESH_DRIVER_BASE_URL"].strip(),
        model=os.environ["DISCO_FRESH_DRIVER_MODEL"].strip(),
        api_key=os.environ.get("DISCO_FRESH_DRIVER_API_KEY", "").strip(),
    )
    assignments = api.json("GET", app, "/api/models/assignments")
    if assignments.get("default_model") != "fresh-device-driver":
        raise ProductError("driver assignment did not round-trip through Settings API")
    return api


def _phase_build_search_export(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    compose_env: dict[str, str],
    out: Path,
    api: ApiSession,
    agent: str,
) -> list:
    """Copy dossiers, verify projects, export ZIP; return project ids."""
    runner.run(
        "copy-initial-dossiers",
        _compose_command(
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
    projects_payload = api.json("GET", agent, "/api/projects")
    projects_before = _project_ids(projects_payload)
    if not projects_before:
        raise ProductError("Build scenario produced no persisted project")
    export_before = api.bytes(agent, f"/api/projects/{projects_before[0]}/download")
    _validate_zip(export_before, out / "project-before-upgrade.zip")
    return projects_before


def _phase_cold_restart(
    runner: CommandRunner,
    engine: Engine,
    project: str,
    checkout: Path,
    compose_env: dict[str, str],
    front: str,
    agent: str,
    api: ApiSession,
    projects_before: list,
) -> None:
    """Restart the stack and verify projects persist."""
    runner.run(
        "restart-stack",
        _compose_command(engine, project, "restart"),
        cwd=checkout,
        env=compose_env,
        timeout=600,
    )
    _wait_url(f"{agent}/health", timeout=600)
    _wait_url(f"{front}/env.js", timeout=600)
    api.pair()
    if _project_ids(api.json("GET", agent, "/api/projects")) != projects_before:
        raise ProductError("project identity changed across a full stack restart")


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
        _record_pass(
            checks,
            "clean-clone",
            "candidate was cloned into a new directory and two distinct refs resolved",
            base_commit=base_commit,
            upgrade_commit=upgrade_commit,
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
                uninstall=_phase_uninstall,
            ),
            runner=runner,
            engine=engine,
            project=project,
            checkout=checkout,
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
        reason = "fresh install, live use, restart, upgrade, export, and uninstall all passed"
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
