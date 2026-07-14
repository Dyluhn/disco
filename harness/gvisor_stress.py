#!/usr/bin/env python3
"""Stress the real docker-py sandbox path against Docker + gVisor.

Run this on VM 201 *inside* the disco-server image, with the host Docker socket
and this directory mounted (the image supplies the installed ``disco`` package):

    docker run --rm \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -v "$PWD/harness:/stress" \
      disco-server:latest \
      python /stress/gvisor_stress.py --out /stress/report.json

The harness deliberately selects ``backend="local"`` through
``service_from_config``.  That is the real docker-py ``LocalSandboxService``;
setting ``runtime="runsc"`` still asks Docker to start every sandbox with gVisor,
while ``--runtime runc`` gives an A/B run through the identical service path.
The spawned image is always ``disco-sandbox:base``.

No package code is changed.  A harness-only subclass observes the real
``_classify_failure_async`` verdict before it reaches the caller.  Every death
verdict is immediately checked with an independent, bounded ``docker inspect``
subprocess.  Because the stock disco-server image contains docker-py but not the
Docker CLI, the harness uses the literal CLI when present and otherwise starts a
fresh Python child that calls the same Engine inspect endpoint over the socket.
Periodic calls to the instance's bounded ``_safe_reload`` add status/inspect
pressure through the same cached docker-py client used by the real service.
All SDK work runs in a supervised child process; after that process and its
threads have exited (or been terminated at the cap), the parent performs the
authoritative final label-filtered cleanup/check, preventing late create workers
from racing past the last leak snapshot.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import multiprocessing
import os
import re
import shutil
import sys
import time
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from disco.tools.sandbox import (
    LocalSandboxService,
    SandboxConfig,
    SandboxError,
    SandboxSpec,
    SandboxUnavailableError,
    service_from_config,
)
from disco.tools.sandbox.naming import LABEL_CONV

IMAGE = "disco-sandbox:base"
OWNER_ID = "gvisor-stress"
PER_EXEC_TIMEOUT_S = 5
PER_EXEC_OUTER_TIMEOUT_S = 30
CREATE_TIMEOUT_S = 90
HEALTHCHECK_TIMEOUT_S = 35
CLI_TIMEOUT_S = 5
LEAK_CHECK_TIMEOUT_S = 2
PROBE_TIMEOUT_S = 2
PROBE_INTERVAL_S = 0.10
MAX_EVENT_SAMPLES = 100
MAX_CONTAINERS = 128
MAX_EXECS_PER_CONTAINER = 1_000_000
MAX_CONCURRENCY_PER_CONTAINER = 64
MIN_DURATION_CAP_S = 10.0
DEATH_WORDS = re.compile(r"\b(?:dead|died)\b", re.IGNORECASE)

# The documented disco-server image intentionally ships docker-py but no Docker
# CLI.  Prefer the literal CLI when an operator has added it; otherwise this tiny
# child program talks directly to the mounted Unix socket.  It remains independent
# of the service's process, DockerClient, object cache, and urllib3 pool, and uses
# the same Engine endpoints as ``docker version``, ``docker inspect``, ``docker ps
# --filter label=...``, and ``docker rm -fv``.
_ENGINE_API_HELPER = r"""
import http.client
import json
import socket
import sys
import urllib.parse

socket_path, operation, payload_json = sys.argv[1:4]
payload = json.loads(payload_json)

class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=10)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)

def request(method, target, accepted):
    connection = UnixHTTPConnection(socket_path)
    try:
        connection.request(method, target)
        response = connection.getresponse()
        body = response.read()
    except OSError as exc:
        print(f"Docker Engine socket error: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(4)
    finally:
        connection.close()
    if response.status not in accepted:
        text = body.decode("utf-8", "replace")
        print(f"Docker Engine HTTP {response.status}: {text}", file=sys.stderr)
        raise SystemExit(3)
    return body

if operation == "version":
    data = json.loads(request("GET", "/version", {200}))
    print(data.get("Version", ""))
elif operation == "inspect":
    container_id = urllib.parse.quote(payload["container_id"], safe="")
    data = json.loads(request("GET", f"/containers/{container_id}/json", {200}))
    print(json.dumps(data.get("State", {}), separators=(",", ":")))
elif operation == "ps":
    filters = json.dumps({"label": [payload["label"]]}, separators=(",", ":"))
    query = urllib.parse.urlencode({"all": "1", "filters": filters})
    data = json.loads(request("GET", f"/containers/json?{query}", {200}))
    for entry in data:
        names = entry.get("Names") or []
        name = (names[0] if names else "").lstrip("/")
        print(f"{entry.get('Id', '')}\t{name}\t{entry.get('Status', '')}")
elif operation == "volumes":
    filters = json.dumps({"label": [payload["label"]]}, separators=(",", ":"))
    query = urllib.parse.urlencode({"filters": filters})
    data = json.loads(request("GET", f"/volumes?{query}", {200}))
    for entry in data.get("Volumes") or []:
        print(f"{entry.get('Name', '')}\t{entry.get('Driver', '')}")
elif operation == "rm":
    for raw_id in payload["container_ids"]:
        container_id = urllib.parse.quote(raw_id, safe="")
        request("DELETE", f"/containers/{container_id}?force=1&v=1", {204, 404})
        print(raw_id)
elif operation == "rm_volumes":
    for raw_name in payload["volume_names"]:
        name = urllib.parse.quote(raw_name, safe="")
        request("DELETE", f"/volumes/{name}?force=1", {204, 404})
        print(raw_name)
else:
    print(f"unknown Engine helper operation: {operation}", file=sys.stderr)
    raise SystemExit(2)
"""

_LOG = logging.getLogger("disco.tools.sandbox.gvisor_stress")


class HarnessSetupError(RuntimeError):
    """The host cannot provide the real Docker/runsc test prerequisites."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def clipped(value: object, limit: int = 2_000) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def positive_int_at_most(maximum: int) -> Any:
    """Build an argparse type that keeps user-shaped task allocation finite."""

    def parse(value: str) -> int:
        parsed = positive_int(value)
        if parsed > maximum:
            raise argparse.ArgumentTypeError(f"must be <= {maximum}")
        return parsed

    return parse


def positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return parsed


def duration_cap_value(value: str) -> float:
    parsed = positive_float(value)
    if parsed < MIN_DURATION_CAP_S:
        raise argparse.ArgumentTypeError(
            f"must be >= {MIN_DURATION_CAP_S:g} seconds to reserve supervised cleanup"
        )
    return parsed


def normalize_docker_socket(value: str) -> str:
    """Turn the required plain-path default into docker-py's URI form."""
    value = value.strip()
    if not value:
        raise HarnessSetupError("--docker-socket may not be empty")
    if "://" in value:
        return value
    return f"unix://{os.path.abspath(value)}"


def debug_log_path(report_path: Path) -> Path:
    if report_path.suffix:
        return report_path.with_name(f"{report_path.stem}.debug.log")
    return report_path.with_name(f"{report_path.name}.debug.log")


class SandboxLogCapture:
    """Capture every sandbox descendant logger at DEBUG beside the JSON report."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.logger = logging.getLogger("disco.tools.sandbox")
        self.handler: logging.FileHandler | None = None
        self.old_level = self.logger.level
        self.old_propagate = self.logger.propagate

    def __enter__(self) -> SandboxLogCapture:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(self.path, mode="w", encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            "%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s thread=%(threadName)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        formatter.converter = time.gmtime
        handler.setFormatter(formatter)
        self.logger.setLevel(logging.DEBUG)
        # This is a standalone process.  Keeping propagation local avoids spraying
        # the package's DEBUG evidence onto any root console handler in the image.
        self.logger.propagate = False
        self.logger.addHandler(handler)
        self.handler = handler
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.handler is not None:
            self.handler.flush()
            self.logger.removeHandler(self.handler)
            self.handler.close()
        self.logger.setLevel(self.old_level)
        self.logger.propagate = self.old_propagate


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    spawn_error: str | None = None
    elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.spawn_error is None


class DockerCLI:
    """Independent Docker inspect/ps/rm subprocesses with hard bounds.

    The literal CLI is used when installed.  The stock disco-server image has
    no CLI, so the fallback is a fresh Python child that directly calls the
    corresponding Docker Engine endpoints over the mounted Unix socket.
    """

    def __init__(self, docker_socket: str) -> None:
        self.docker_socket = docker_socket
        self.cli_path = shutil.which("docker")
        self.transport = "docker_cli" if self.cli_path else "python_engine_api_subprocess"
        self.prefix = [self.cli_path or "docker", "--host", docker_socket]
        self.unix_socket_path = (
            docker_socket.removeprefix("unix://") if docker_socket.startswith("unix://") else None
        )

    async def run(self, *args: str, timeout_s: float = CLI_TIMEOUT_S) -> CommandResult:
        argv = [*self.prefix, *args]
        return await self._run_argv(argv, timeout_s=timeout_s)

    async def _engine_run(
        self, operation: str, payload: dict[str, Any], *, timeout_s: float
    ) -> CommandResult:
        if self.unix_socket_path is None:
            return CommandResult(
                argv=[sys.executable, "<engine-api-helper>", operation],
                returncode=None,
                spawn_error=(
                    "the Docker CLI is absent and the independent Engine-API "
                    "subprocess fallback supports unix:// sockets only"
                ),
            )
        argv = [
            sys.executable,
            "-I",
            "-S",
            "-c",
            _ENGINE_API_HELPER,
            self.unix_socket_path,
            operation,
            json.dumps(payload, separators=(",", ":")),
        ]
        result = await self._run_argv(argv, timeout_s=timeout_s)
        # Do not retain the embedded helper source in diagnostics/report samples.
        result.argv = [sys.executable, "<engine-api-helper>", operation]
        return result

    async def _run_argv(self, argv: list[str], *, timeout_s: float) -> CommandResult:
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return CommandResult(
                argv=argv,
                returncode=None,
                spawn_error=f"{type(exc).__name__}: {exc}",
                elapsed_s=time.monotonic() - started,
            )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.communicate(), timeout=1)
            return CommandResult(
                argv=argv,
                returncode=proc.returncode,
                timed_out=True,
                stderr=f"independent Docker subprocess exceeded {timeout_s:.2f}s",
                elapsed_s=time.monotonic() - started,
            )
        except asyncio.CancelledError:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.communicate(), timeout=1)
            raise

        return CommandResult(
            argv=argv,
            returncode=proc.returncode,
            stdout=stdout.decode("utf-8", "replace"),
            stderr=stderr.decode("utf-8", "replace"),
            elapsed_s=time.monotonic() - started,
        )

    async def preflight(self, *, timeout_s: float = CLI_TIMEOUT_S) -> str:
        if self.cli_path:
            result = await self.run(
                "version", "--format", "{{.Server.Version}}", timeout_s=timeout_s
            )
        else:
            result = await self._engine_run("version", {}, timeout_s=timeout_s)
        if not result.ok:
            raise HarnessSetupError(
                f"independent Docker preflight ({self.transport}) failed: "
                f"rc={result.returncode} timeout={result.timed_out} "
                f"spawn={result.spawn_error!r} stderr={clipped(result.stderr)!r}"
            )
        version = result.stdout.strip()
        if not version:
            raise HarnessSetupError("Docker preflight returned an empty server version")
        return version

    async def inspect_state(
        self, container_id: str, *, timeout_s: float = CLI_TIMEOUT_S
    ) -> dict[str, Any]:
        if self.cli_path:
            result = await self.run(
                "inspect",
                "--format",
                "{{json .State}}",
                container_id,
                timeout_s=timeout_s,
            )
        else:
            result = await self._engine_run(
                "inspect", {"container_id": container_id}, timeout_s=timeout_s
            )
        evidence: dict[str, Any] = {
            "container_id": container_id,
            "transport": self.transport,
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "spawn_error": result.spawn_error,
            "stderr": clipped(result.stderr.strip()),
            "elapsed_s": round(result.elapsed_s, 6),
            "running": None,
            "status": None,
            "oom_killed": None,
            "exit_code": None,
            "state_error": None,
            "parse_error": None,
        }
        if not result.ok:
            return evidence
        try:
            # Docker normally emits exactly one JSON line.  Taking the last
            # non-empty line tolerates a harmless CLI warning before the payload.
            lines = [line for line in result.stdout.splitlines() if line.strip()]
            state = json.loads(lines[-1])
            running = state.get("Running")
            if not isinstance(running, bool):
                raise ValueError(f"State.Running is not boolean: {running!r}")
            evidence.update(
                {
                    "running": running,
                    "status": state.get("Status"),
                    "oom_killed": state.get("OOMKilled"),
                    "exit_code": state.get("ExitCode"),
                    "state_error": state.get("Error"),
                }
            )
        except (IndexError, json.JSONDecodeError, TypeError, ValueError) as exc:
            evidence["parse_error"] = f"{type(exc).__name__}: {exc}"
        return evidence

    async def labeled_containers(
        self, conversation_id: str, *, timeout_s: float = CLI_TIMEOUT_S
    ) -> tuple[list[dict[str, str]], str | None]:
        if self.cli_path:
            result = await self.run(
                "ps",
                "--all",
                "--filter",
                f"label={LABEL_CONV}={conversation_id}",
                "--format",
                "{{.ID}}\t{{.Names}}\t{{.Status}}",
                timeout_s=timeout_s,
            )
        else:
            result = await self._engine_run(
                "ps",
                {"label": f"{LABEL_CONV}={conversation_id}"},
                timeout_s=timeout_s,
            )
        if not result.ok:
            error = (
                f"docker ps-equivalent label check ({self.transport}) failed: "
                f"rc={result.returncode} "
                f"timeout={result.timed_out} spawn={result.spawn_error!r} "
                f"stderr={clipped(result.stderr.strip())!r}"
            )
            return [], error
        containers: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            fields = line.split("\t", 2)
            containers.append(
                {
                    "id": fields[0],
                    "name": fields[1] if len(fields) > 1 else "",
                    "status": fields[2] if len(fields) > 2 else "",
                }
            )
        return containers, None

    async def force_remove(
        self, container_ids: list[str], *, timeout_s: float = 15
    ) -> CommandResult | None:
        if not container_ids:
            return None
        if self.cli_path:
            return await self.run("rm", "--force", "--volumes", *container_ids, timeout_s=timeout_s)
        return await self._engine_run("rm", {"container_ids": container_ids}, timeout_s=timeout_s)

    async def labeled_volumes(
        self, conversation_id: str, *, timeout_s: float = LEAK_CHECK_TIMEOUT_S
    ) -> tuple[list[dict[str, str]], str | None]:
        if self.cli_path:
            result = await self.run(
                "volume",
                "ls",
                "--filter",
                f"label={LABEL_CONV}={conversation_id}",
                "--format",
                "{{.Name}}\t{{.Driver}}",
                timeout_s=timeout_s,
            )
        else:
            result = await self._engine_run(
                "volumes",
                {"label": f"{LABEL_CONV}={conversation_id}"},
                timeout_s=timeout_s,
            )
        if not result.ok:
            return [], (
                f"docker volume ls-equivalent label check ({self.transport}) failed: "
                f"rc={result.returncode} timeout={result.timed_out} "
                f"spawn={result.spawn_error!r} stderr={clipped(result.stderr.strip())!r}"
            )
        volumes: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            fields = line.split("\t", 1)
            volumes.append(
                {
                    "name": fields[0],
                    "driver": fields[1] if len(fields) > 1 else "",
                }
            )
        return volumes, None

    async def force_remove_volumes(
        self, volume_names: list[str], *, timeout_s: float = 15
    ) -> CommandResult | None:
        if not volume_names:
            return None
        if self.cli_path:
            return await self.run("volume", "rm", "--force", *volume_names, timeout_s=timeout_s)
        return await self._engine_run(
            "rm_volumes", {"volume_names": volume_names}, timeout_s=timeout_s
        )


@dataclass
class Metrics:
    planned_execs: int
    start_monotonic: float
    containers_created: int = 0
    container_create_failures: int = 0
    container_create_cancellations: int = 0
    execs_scheduled: int = 0
    execs_attempted: int = 0
    execs_completed: int = 0
    execs_succeeded: int = 0
    execs_cancelled: int = 0
    death_verdicts: int = 0
    confirmed_deaths: int = 0
    confirmation_alive: int = 0
    confirmation_inconclusive: int = 0
    transient_downgrades: int = 0
    per_op_classifications: int = 0
    op_failures: int = 0
    op_exceptions: int = 0
    op_nonzero_results: int = 0
    op_timed_out_results: int = 0
    op_outer_timeouts: int = 0
    status_inspect_probes: int = 0
    status_probe_running: int = 0
    status_probe_not_running: int = 0
    status_probe_failures: int = 0
    status_probe_completed_instance_ids: set[str] = field(default_factory=set)
    instrumentation_errors: int = 0
    create_phase_timed_out: bool = False
    outstanding_create_tasks: int = 0
    work_deadline_hit: bool = False
    duration_cap_hit: bool = False
    workload_timed_out: bool = False
    setup_errors: list[str] = field(default_factory=list)
    orchestration_errors: list[str] = field(default_factory=list)
    create_error_samples: list[dict[str, Any]] = field(default_factory=list)
    op_failure_samples: list[dict[str, Any]] = field(default_factory=list)
    death_confirmations: list[dict[str, Any]] = field(default_factory=list)
    status_probe_error_samples: list[dict[str, Any]] = field(default_factory=list)

    def since_start(self) -> float:
        return time.monotonic() - self.start_monotonic

    def add_sample(self, target: list[dict[str, Any]], sample: dict[str, Any]) -> None:
        if len(target) < MAX_EVENT_SAMPLES:
            target.append(sample)


class ClassifierObserver:
    """Count exact final package verdicts without changing their behavior."""

    def __init__(self, metrics: Metrics, docker_cli: DockerCLI) -> None:
        self.metrics = metrics
        self.docker_cli = docker_cli

    async def observe(self, instance: Any, raw_exc: Exception, verdict: SandboxError) -> None:
        verdict_text = str(verdict)
        if isinstance(verdict, SandboxError) and not isinstance(verdict, SandboxUnavailableError):
            # Every raw backend throw that ends here is classified as a per-op
            # error rather than container death.  Keep that broad count, while
            # transient_downgrades is the narrower re-verify-window branch that
            # proves the shared hardening specifically overturned a death candidate.
            self.metrics.per_op_classifications += 1
            if "transient sandbox api error" in verdict_text.lower():
                self.metrics.transient_downgrades += 1
                _LOG.info(
                    "harness observed transient downgrade instance=%s verdict=%s",
                    instance.id,
                    verdict_text,
                )
            return

        if not (isinstance(verdict, SandboxUnavailableError) and DEATH_WORDS.search(verdict_text)):
            return

        # This counter is incremented only at the real final classifier boundary.
        # The independent inspect happens before this hook returns the verdict to
        # exec_shell's caller, so no caller can destroy/recreate the box first.
        self.metrics.death_verdicts += 1
        docker_id = str(getattr(getattr(instance, "_container", None), "id", "") or "")
        event: dict[str, Any] = {
            "at": utc_now(),
            "elapsed_s": round(self.metrics.since_start(), 6),
            "instance_id": instance.id,
            "docker_id": docker_id,
            "raw_exception": clipped(f"{type(raw_exc).__name__}: {raw_exc}"),
            "verdict": clipped(f"{type(verdict).__name__}: {verdict}"),
        }
        if not docker_id:
            evidence = {
                "running": None,
                "parse_error": "instance had no docker container id",
            }
        else:
            evidence = await self.docker_cli.inspect_state(docker_id)
        event["direct_inspect"] = evidence

        if evidence.get("running") is False:
            self.metrics.confirmed_deaths += 1
            outcome = "confirmed_dead"
        elif evidence.get("running") is True:
            self.metrics.confirmation_alive += 1
            outcome = "alive_needless_verdict"
        else:
            # An inspect 404/error is not proof of death.  This conservative
            # treatment is intentional: the bug under test is a transient 404.
            self.metrics.confirmation_inconclusive += 1
            outcome = "inconclusive_not_confirmed"
        event["outcome"] = outcome
        self.metrics.add_sample(self.metrics.death_confirmations, event)
        _LOG.warning(
            "harness death confirmation instance=%s docker_id=%s outcome=%s evidence=%s",
            instance.id,
            docker_id,
            outcome,
            evidence,
        )


def install_classifier_observer(service: LocalSandboxService, observer: ClassifierObserver) -> str:
    """Give newly-created instances a harness-only observer subclass."""
    base_cls = service._instance_cls  # type: ignore[attr-defined]  # harness instrumentation

    class ObservedInstance(base_cls):  # type: ignore[valid-type, misc]
        async def _classify_failure_async(self, exc: Exception) -> SandboxError:
            verdict = await super()._classify_failure_async(exc)
            try:
                await observer.observe(self, exc, verdict)
            except asyncio.CancelledError:
                raise
            except Exception as observer_exc:  # noqa: BLE001 - evidence must not alter verdict
                observer.metrics.instrumentation_errors += 1
                _LOG.exception(
                    "classifier observer failed for instance=%s: %s",
                    getattr(self, "id", "unknown"),
                    observer_exc,
                )
            return verdict

    ObservedInstance.__name__ = f"Observed{base_cls.__name__}"
    ObservedInstance.__qualname__ = ObservedInstance.__name__
    service._instance_cls = ObservedInstance  # type: ignore[attr-defined]
    return f"{base_cls.__module__}.{base_cls.__name__}"


COMMANDS: tuple[tuple[str, str], ...] = (
    ("echo", "echo ok"),
    ("true", "true"),
    ("python", "python3 -c 'print(6 * 7)'"),
)


async def run_one_exec(
    *,
    instance: Any,
    container_index: int,
    exec_index: int,
    semaphore: asyncio.Semaphore,
    start_gate: asyncio.Event,
    work_deadline: float,
    metrics: Metrics,
) -> None:
    await start_gate.wait()
    try:
        async with semaphore:
            remaining = work_deadline - time.monotonic()
            if remaining <= 0:
                metrics.execs_cancelled += 1
                return
            metrics.execs_attempted += 1
            command_name, command = COMMANDS[(container_index + exec_index) % len(COMMANDS)]
            started = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    instance.exec_shell(command, timeout_s=PER_EXEC_TIMEOUT_S),
                    timeout=min(PER_EXEC_OUTER_TIMEOUT_S, remaining),
                )
            except asyncio.CancelledError:
                metrics.execs_cancelled += 1
                raise
            except TimeoutError as exc:
                metrics.op_failures += 1
                metrics.op_outer_timeouts += 1
                metrics.execs_completed += 1
                metrics.add_sample(
                    metrics.op_failure_samples,
                    {
                        "container_index": container_index,
                        "instance_id": instance.id,
                        "exec_index": exec_index,
                        "command": command_name,
                        "kind": "harness_outer_timeout",
                        "error": clipped(exc),
                        "elapsed_s": round(time.monotonic() - started, 6),
                    },
                )
                return
            except Exception as exc:  # noqa: BLE001 - caller-visible operation failure
                metrics.op_failures += 1
                metrics.op_exceptions += 1
                metrics.execs_completed += 1
                metrics.add_sample(
                    metrics.op_failure_samples,
                    {
                        "container_index": container_index,
                        "instance_id": instance.id,
                        "exec_index": exec_index,
                        "command": command_name,
                        "kind": "exception",
                        "error": clipped(f"{type(exc).__name__}: {exc}"),
                        "elapsed_s": round(time.monotonic() - started, 6),
                    },
                )
                return

            metrics.execs_completed += 1
            if result.timed_out or result.exit_code != 0:
                metrics.op_failures += 1
                if result.timed_out:
                    metrics.op_timed_out_results += 1
                if result.exit_code != 0:
                    metrics.op_nonzero_results += 1
                metrics.add_sample(
                    metrics.op_failure_samples,
                    {
                        "container_index": container_index,
                        "instance_id": instance.id,
                        "exec_index": exec_index,
                        "command": command_name,
                        "kind": "failed_exec_result",
                        "exit_code": result.exit_code,
                        "timed_out": result.timed_out,
                        "stdout": clipped(result.stdout),
                        "stderr": clipped(result.stderr),
                        "elapsed_s": round(time.monotonic() - started, 6),
                    },
                )
            else:
                metrics.execs_succeeded += 1
    except asyncio.CancelledError:
        raise


async def status_probe_loop(
    *,
    instance: Any,
    container_index: int,
    start_gate: asyncio.Event,
    stop_event: asyncio.Event,
    work_deadline: float,
    metrics: Metrics,
) -> None:
    """Continuously reload state through the real shared docker-py client."""
    await start_gate.wait()
    while not stop_event.is_set() and time.monotonic() < work_deadline:
        try:
            alive = await asyncio.wait_for(
                asyncio.to_thread(instance._safe_reload),  # harness-level status instrumentation
                timeout=min(PROBE_TIMEOUT_S, max(0.001, work_deadline - time.monotonic())),
            )
            if alive:
                metrics.status_probe_running += 1
            else:
                metrics.status_probe_not_running += 1
            metrics.status_inspect_probes += 1
            metrics.status_probe_completed_instance_ids.add(instance.id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - probe evidence, not an exec verdict
            metrics.status_probe_failures += 1
            metrics.status_inspect_probes += 1
            metrics.status_probe_completed_instance_ids.add(instance.id)
            metrics.add_sample(
                metrics.status_probe_error_samples,
                {
                    "container_index": container_index,
                    "instance_id": instance.id,
                    "error": clipped(f"{type(exc).__name__}: {exc}"),
                    "elapsed_s": round(metrics.since_start(), 6),
                },
            )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=PROBE_INTERVAL_S)
        except TimeoutError:
            pass


def cleanup_reserve(duration_cap_s: float) -> float:
    """Reserve time to drain bounded SDK workers, then sweep real resources."""
    return min(150.0, max(10.0, duration_cap_s * 0.25), duration_cap_s * 0.50)


async def await_with_deadline(
    awaitable: Awaitable[Any], *, deadline: float, max_timeout_s: float
) -> Any:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        if hasattr(awaitable, "close"):
            awaitable.close()  # type: ignore[union-attr]
        raise TimeoutError("global deadline exhausted")
    return await asyncio.wait_for(awaitable, timeout=min(max_timeout_s, remaining))


async def stop_tasks(
    tasks: list[asyncio.Task[Any]], *, deadline: float, max_timeout_s: float = 3
) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    remaining = deadline - time.monotonic()
    if tasks and remaining > 0:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                min(max_timeout_s, remaining),
            )


async def cleanup(
    *,
    service: LocalSandboxService | None,
    instances: list[Any],
    conversation_id: str,
    docker_cli: DockerCLI,
    global_deadline: float,
    empty_stability_s: float = 0.15,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "instance_destroy_attempts": len(instances),
        "instance_destroy_failures": [],
        "destroy_by_conversation_attempted": False,
        "destroy_by_conversation_error": None,
        "leak_check_error": None,
        "leaks_before_forced_cleanup": [],
        "forced_cleanup": None,
        "forced_cleanup_attempts": [],
        "leak_sweeps": [],
        "required_empty_stability_s": round(empty_stability_s, 3),
        "leaks_remaining": [],
        "volume_leak_check_error": None,
        "volumes_before_forced_cleanup": [],
        "volume_cleanup_attempts": [],
        "volumes_remaining": [],
    }

    async def destroy_one(instance: Any) -> None:
        try:
            await await_with_deadline(
                instance.destroy(), deadline=global_deadline, max_timeout_s=20
            )
        except Exception as exc:  # noqa: BLE001 - continue with label cleanup
            result["instance_destroy_failures"].append(
                {
                    "instance_id": getattr(instance, "id", "unknown"),
                    "error": clipped(f"{type(exc).__name__}: {exc}"),
                }
            )

    if instances:
        await asyncio.gather(*(destroy_one(instance) for instance in instances))

    if service is not None:
        result["destroy_by_conversation_attempted"] = True
        try:
            await await_with_deadline(
                service.destroy_by_conversation(conversation_id),
                deadline=global_deadline,
                max_timeout_s=20,
            )
        except Exception as exc:  # noqa: BLE001 - direct CLI cleanup follows
            result["destroy_by_conversation_error"] = clipped(f"{type(exc).__name__}: {exc}")

    # Repeated ps -> rm -> ps closes two failure-path holes: a container that
    # appears just after an earlier snapshot, and a transient failed ps followed
    # by a successful authoritative check.  Create workers are drained before
    # entering cleanup; the repeated sweep is the final belt-and-suspenders.
    empty_since: float | None = None
    consecutive_check_errors = 0
    for sweep_index in range(1_000):
        remaining = global_deadline - time.monotonic()
        if remaining <= 0:
            result["leak_check_error"] = (
                result["leak_check_error"]
                or "global deadline exhausted before an empty docker ps label check"
            )
            break
        leaks, error = await docker_cli.labeled_containers(
            conversation_id, timeout_s=min(LEAK_CHECK_TIMEOUT_S, remaining)
        )
        result["leak_sweeps"].append(
            {"sweep": sweep_index + 1, "error": error, "containers": leaks}
        )
        if error:
            empty_since = None
            consecutive_check_errors += 1
            result["leak_check_error"] = error
            if consecutive_check_errors >= 3:
                break
            remaining = global_deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(0.10, remaining))
            continue

        # A successful check supersedes a prior transient check error.
        consecutive_check_errors = 0
        result["leak_check_error"] = None
        result["leaks_remaining"] = leaks
        if not leaks:
            now = time.monotonic()
            if empty_since is None:
                empty_since = now
            if now - empty_since >= empty_stability_s:
                break
            remaining = global_deadline - now
            if remaining > 0:
                await asyncio.sleep(min(0.10, empty_stability_s - (now - empty_since), remaining))
            continue
        empty_since = None
        if not result["leaks_before_forced_cleanup"]:
            result["leaks_before_forced_cleanup"] = leaks

        remaining = global_deadline - time.monotonic()
        if remaining <= 0:
            result["leak_check_error"] = "global deadline exhausted before forced cleanup"
            break
        removal = await docker_cli.force_remove(
            [entry["id"] for entry in leaks], timeout_s=min(15, remaining)
        )
        if removal is None:
            continue
        removal_record = {
            "returncode": removal.returncode,
            "timed_out": removal.timed_out,
            "spawn_error": removal.spawn_error,
            "stdout": clipped(removal.stdout.strip()),
            "stderr": clipped(removal.stderr.strip()),
        }
        result["forced_cleanup"] = removal_record
        result["forced_cleanup_attempts"].append(removal_record)
    else:
        if result["leak_check_error"] is None:
            result["leak_check_error"] = "no stable empty result after 1000 label sweeps"

    # LocalSandboxService uses labeled named volumes. The public service cleanup
    # normally removes them; this independent pass also covers a supervisor kill
    # between volume creation and construction of a trackable instance.
    for _attempt in range(3):
        remaining = global_deadline - time.monotonic()
        if remaining <= 0:
            result["volume_leak_check_error"] = (
                "global deadline exhausted before docker volume label check"
            )
            break
        volumes, error = await docker_cli.labeled_volumes(
            conversation_id, timeout_s=min(LEAK_CHECK_TIMEOUT_S, remaining)
        )
        result["volume_leak_check_error"] = error
        result["volumes_remaining"] = volumes
        if error:
            continue
        if not volumes:
            break
        if not result["volumes_before_forced_cleanup"]:
            result["volumes_before_forced_cleanup"] = volumes
        remaining = global_deadline - time.monotonic()
        if remaining <= 0:
            result["volume_leak_check_error"] = (
                "global deadline exhausted before forced volume cleanup"
            )
            break
        removal = await docker_cli.force_remove_volumes(
            [entry["name"] for entry in volumes], timeout_s=min(15, remaining)
        )
        if removal is not None:
            result["volume_cleanup_attempts"].append(
                {
                    "returncode": removal.returncode,
                    "timed_out": removal.timed_out,
                    "spawn_error": removal.spawn_error,
                    "stdout": clipped(removal.stdout.strip()),
                    "stderr": clipped(removal.stderr.strip()),
                }
            )
    return result


async def run_harness(
    args: argparse.Namespace, log_path: Path, *, conversation_id: str | None = None
) -> dict[str, Any]:
    started_at = utc_now()
    started = time.monotonic()
    global_deadline = started + args.duration_cap
    reserve = cleanup_reserve(args.duration_cap)
    work_deadline = global_deadline - reserve
    planned_execs = args.containers * args.execs_per_container
    metrics = Metrics(planned_execs=planned_execs, start_monotonic=started)
    socket_normalization_error: str | None = None
    try:
        docker_socket = normalize_docker_socket(args.docker_socket)
    except HarnessSetupError as exc:
        # No resource can have been created yet.  Use the normal local socket only
        # so the report/empty cleanup check can still be produced.
        docker_socket = "unix:///var/run/docker.sock"
        socket_normalization_error = clipped(f"{type(exc).__name__}: {exc}")
    docker_cli = DockerCLI(docker_socket)
    conversation_id = conversation_id or f"gvisor-stress-{uuid.uuid4().hex}"
    service: LocalSandboxService | None = None
    instances: list[Any] = []
    create_tasks: list[asyncio.Task[Any]] = []
    create_task_indices: dict[asyncio.Task[Any], int] = {}
    harvested_create_tasks: set[asyncio.Task[Any]] = set()
    create_group: asyncio.Future[Any] | None = None
    exec_tasks: list[asyncio.Task[Any]] = []
    probe_tasks: list[asyncio.Task[Any]] = []
    stop_probes = asyncio.Event()
    start_gate = asyncio.Event()
    docker_version: str | None = None
    observed_instance_base: str | None = None
    cleanup_report: dict[str, Any] = {}
    # A canceled asyncio.to_thread call keeps its worker alive.  Size the SDK's
    # own socket bound from the cleanup reserve so even a late create worker has
    # time to settle before the final label sweep. The default 600s run preserves
    # SandboxConfig's real 30s client timeout; only unusually tiny duration caps
    # lower it so the advertised global bound remains meaningful.
    effective_client_timeout_s = max(1, min(30, int(max(1.0, reserve / 4))))
    effective_stop_timeout_s = max(1, min(5, int(max(1.0, reserve / 12))))

    def harvest_finished_creates() -> None:
        """Collect every retained service.create result exactly once."""
        for task in create_tasks:
            if task in harvested_create_tasks or not task.done():
                continue
            harvested_create_tasks.add(task)
            index = create_task_indices[task]
            if task.cancelled():
                metrics.container_create_cancellations += 1
                continue
            try:
                instance = task.result()
            except Exception as exc:  # noqa: BLE001 - retain exact create failure
                metrics.container_create_failures += 1
                metrics.add_sample(
                    metrics.create_error_samples,
                    {
                        "container_index": index,
                        "error": clipped(f"{type(exc).__name__}: {exc}"),
                    },
                )
                continue
            instances.append(instance)
            metrics.containers_created += 1
            _LOG.debug(
                "created container index=%d instance=%s docker_id=%s",
                index,
                instance.id,
                getattr(getattr(instance, "_container", None), "id", "unknown"),
            )

    try:
        if socket_normalization_error:
            raise HarnessSetupError(socket_normalization_error)
        docker_version = await await_with_deadline(
            docker_cli.preflight(), deadline=work_deadline, max_timeout_s=CLI_TIMEOUT_S + 1
        )

        config = SandboxConfig(
            backend="local",
            docker_socket=docker_socket,
            runtime=args.runtime,
            image=IMAGE,
            client_timeout_s=effective_client_timeout_s,
            stop_timeout_s=effective_stop_timeout_s,
        )
        resolved_service = service_from_config(config)
        if not isinstance(resolved_service, LocalSandboxService):
            raise HarnessSetupError(
                "service_from_config did not select LocalSandboxService; "
                f"got {type(resolved_service).__module__}.{type(resolved_service).__name__}. "
                "Use a real Docker socket path (a socket containing 'podman' is intentionally "
                "routed to PodmanSandboxService)."
            )
        service = resolved_service
        observer = ClassifierObserver(metrics, docker_cli)
        observed_instance_base = install_classifier_observer(service, observer)

        # Warm the lazily-created, cached DockerClient before concurrent create.
        # Otherwise simultaneous first use can construct multiple clients/pools and
        # accidentally reduce the contention this harness is meant to measure.
        await await_with_deadline(
            service.healthcheck(),
            deadline=work_deadline,
            max_timeout_s=HEALTHCHECK_TIMEOUT_S,
        )
        _LOG.info(
            "preflight complete docker=%s service=%s runtime=%s image=%s socket=%s",
            docker_version,
            type(service).__name__,
            args.runtime,
            IMAGE,
            docker_socket,
        )

        # Retain the actual service.create tasks and shield their group from
        # phase-timeout cancellation. service.create awaits asyncio.to_thread;
        # canceling it does not stop _start_container and could otherwise create
        # an untracked container after the final leak check.
        for index in range(args.containers):
            task = asyncio.create_task(
                service.create(
                    SandboxSpec(),
                    owner_id=OWNER_ID,
                    conversation_id=conversation_id,
                ),
                name=f"create-{index}",
            )
            create_tasks.append(task)
            create_task_indices[task] = index
        create_group = asyncio.gather(*create_tasks, return_exceptions=True)
        try:
            await await_with_deadline(
                asyncio.shield(create_group),
                deadline=work_deadline,
                max_timeout_s=CREATE_TIMEOUT_S,
            )
        except TimeoutError as exc:
            metrics.create_phase_timed_out = True
            metrics.workload_timed_out = True
            metrics.work_deadline_hit = time.monotonic() >= work_deadline
            raise HarnessSetupError(
                "concurrent container creation exceeded its bounded phase; "
                "retained workers will be drained before label cleanup"
            ) from exc
        harvest_finished_creates()
        if metrics.containers_created != args.containers:
            raise HarnessSetupError(
                f"only {metrics.containers_created}/{args.containers} containers created; "
                "a partial load is not a valid stress run"
            )

        # The append order is completion order, which is fine; assigning a stable
        # local index now makes every report deterministic within this run.
        per_container_semaphores = [asyncio.Semaphore(args.concurrency) for _instance in instances]
        for container_index, instance in enumerate(instances):
            probe_tasks.append(
                asyncio.create_task(
                    status_probe_loop(
                        instance=instance,
                        container_index=container_index,
                        start_gate=start_gate,
                        stop_event=stop_probes,
                        work_deadline=work_deadline,
                        metrics=metrics,
                    ),
                    name=f"status-probe-{container_index}",
                )
            )

        # Use a bounded M*concurrency worker set instead of eagerly allocating
        # M*K tasks. Workers are created round-robin by slot across containers,
        # then released together. Each worker claims one logical exec at a time.
        next_exec_index = [0 for _instance in instances]

        async def exec_worker(container_index: int, worker_slot: int) -> None:
            instance = instances[container_index]
            while True:
                if time.monotonic() >= work_deadline:
                    return
                exec_index = next_exec_index[container_index]
                if exec_index >= args.execs_per_container:
                    return
                # There is no await between this read and increment, so workers
                # on the same event loop cannot claim the same logical operation.
                next_exec_index[container_index] += 1
                await run_one_exec(
                    instance=instance,
                    container_index=container_index,
                    exec_index=exec_index,
                    semaphore=per_container_semaphores[container_index],
                    start_gate=start_gate,
                    work_deadline=work_deadline,
                    metrics=metrics,
                )

        workers_per_container = min(args.concurrency, args.execs_per_container)
        for worker_slot in range(workers_per_container):
            for container_index, _instance in enumerate(instances):
                exec_tasks.append(
                    asyncio.create_task(
                        exec_worker(container_index, worker_slot),
                        name=f"exec-worker-{worker_slot}-{container_index}",
                    )
                )
        metrics.execs_scheduled = len(instances) * args.execs_per_container
        start_gate.set()

        if exec_tasks:
            try:
                await await_with_deadline(
                    asyncio.gather(*exec_tasks),
                    deadline=work_deadline,
                    max_timeout_s=max(0.001, work_deadline - time.monotonic()),
                )
            except TimeoutError:
                metrics.workload_timed_out = True
                metrics.work_deadline_hit = time.monotonic() >= work_deadline
                await stop_tasks(exec_tasks, deadline=global_deadline)

    except HarnessSetupError as exc:
        metrics.setup_errors.append(clipped(f"{type(exc).__name__}: {exc}"))
        _LOG.error("setup failed: %s", exc)
    except TimeoutError as exc:
        metrics.workload_timed_out = True
        metrics.work_deadline_hit = time.monotonic() >= work_deadline
        metrics.orchestration_errors.append(clipped(f"TimeoutError: {exc}"))
        _LOG.error("workload deadline reached: %s", exc)
    except Exception as exc:  # noqa: BLE001 - report and still tear down
        metrics.orchestration_errors.append(clipped(f"{type(exc).__name__}: {exc}"))
        _LOG.exception("unexpected harness orchestration error")
    finally:
        # Unblock any task created immediately before an exception, then stop all
        # workload/probe activity before touching container lifecycle.
        start_gate.set()
        await stop_tasks(exec_tasks, deadline=global_deadline)
        if metrics.execs_completed < metrics.execs_scheduled:
            metrics.execs_cancelled = max(
                metrics.execs_cancelled,
                metrics.execs_scheduled - metrics.execs_completed,
            )
        stop_probes.set()
        await stop_tasks(probe_tasks, deadline=global_deadline)

        # Drain retained create tasks before the first label sweep. The configured
        # client timeout is <= reserve/4, while this drain gets ~3/4 of the reserve,
        # enough for all three bounded Docker calls in _start_container to settle.
        pending_creates = [task for task in create_tasks if not task.done()]
        cleanup_tail = min(30.0, max(1.0, reserve * 0.25))
        drain_deadline = global_deadline - cleanup_tail
        if pending_creates and drain_deadline > time.monotonic():
            await asyncio.wait(
                pending_creates,
                timeout=max(0.0, drain_deadline - time.monotonic()),
            )
        harvest_finished_creates()
        pending_creates = [task for task in create_tasks if not task.done()]
        metrics.outstanding_create_tasks = len(pending_creates)
        if pending_creates:
            metrics.orchestration_errors.append(
                f"{len(pending_creates)} retained container-create task(s) did not "
                "settle before the cleanup tail"
            )
            # Cancel the coroutines so they cannot construct untracked instance
            # objects. Their socket-bounded workers may still finish; the repeated
            # label sweep below removes any container they managed to create.
            await stop_tasks(
                pending_creates,
                deadline=global_deadline,
                max_timeout_s=min(3.0, cleanup_tail),
            )
            harvest_finished_creates()
        if create_group is not None and create_group.done():
            with contextlib.suppress(Exception):
                create_group.result()

        try:
            cleanup_report = await cleanup(
                service=service,
                instances=instances,
                conversation_id=conversation_id,
                docker_cli=docker_cli,
                global_deadline=global_deadline,
                empty_stability_s=(
                    min(10.0, max(0.15, cleanup_tail * 0.8))
                    if metrics.outstanding_create_tasks
                    else 0.15
                ),
            )
        except Exception as exc:  # noqa: BLE001 - preserve a JSON failure report
            metrics.orchestration_errors.append(clipped(f"cleanup {type(exc).__name__}: {exc}"))
            cleanup_report = {
                "leak_check_error": clipped(f"cleanup crashed: {type(exc).__name__}: {exc}"),
                "leaks_remaining": [],
            }

    elapsed_s = time.monotonic() - started
    if time.monotonic() >= global_deadline:
        metrics.duration_cap_hit = True

    needless_recreates = metrics.death_verdicts - metrics.confirmed_deaths
    # At most 1% caller-visible failures are tolerated.  floor() makes small runs
    # strict (zero allowed below 100 ops); the default 240-op run permits only two.
    allowed_op_failures = math.floor(planned_execs * 0.01)
    core_pass = needless_recreates == 0 and metrics.op_failures <= allowed_op_failures
    leak_check_ok = cleanup_report.get("leak_check_error") is None and not cleanup_report.get(
        "leaks_remaining"
    )
    leak_check_ok = (
        leak_check_ok
        and cleanup_report.get("volume_leak_check_error") is None
        and not cleanup_report.get("volumes_remaining")
    )
    completed_all = (
        metrics.containers_created == args.containers
        and metrics.execs_scheduled == planned_execs
        and metrics.execs_completed == planned_execs
    )
    created_instance_ids = {instance.id for instance in instances}
    probe_coverage_ok = created_instance_ids.issubset(metrics.status_probe_completed_instance_ids)
    validity_pass = (
        not metrics.setup_errors
        and not metrics.orchestration_errors
        and metrics.instrumentation_errors == 0
        and completed_all
        and probe_coverage_ok
        and not metrics.workload_timed_out
        and metrics.outstanding_create_tasks == 0
        and not metrics.duration_cap_hit
        and leak_check_ok
    )
    passed = core_pass and validity_pass
    failure_reasons: list[str] = []
    if needless_recreates != 0:
        failure_reasons.append(f"needless_recreates={needless_recreates} (must be 0)")
    if metrics.op_failures > allowed_op_failures:
        failure_reasons.append(
            f"op_failures={metrics.op_failures} exceeds allowed {allowed_op_failures}"
        )
    if metrics.setup_errors:
        failure_reasons.append("setup/preflight failed")
    if metrics.orchestration_errors:
        failure_reasons.append("harness orchestration failed")
    if metrics.instrumentation_errors:
        failure_reasons.append("classifier instrumentation failed")
    if not completed_all:
        failure_reasons.append(
            "incomplete load: "
            f"containers={metrics.containers_created}/{args.containers}, "
            f"execs={metrics.execs_completed}/{planned_execs}"
        )
    if not probe_coverage_ok:
        failure_reasons.append("periodic status/inspect probes did not cover every container")
    if metrics.create_phase_timed_out:
        failure_reasons.append("container creation phase timed out")
    if metrics.work_deadline_hit:
        failure_reasons.append("workload deadline reached; reserved cleanup window began")
    elif metrics.workload_timed_out:
        failure_reasons.append("a bounded workload phase timed out")
    if metrics.outstanding_create_tasks:
        failure_reasons.append("container create workers remained unsettled at cleanup")
    if metrics.duration_cap_hit:
        failure_reasons.append("global duration cap was reached")
    if not leak_check_ok:
        failure_reasons.append("sandbox resource leak verification failed or resources remain")

    counters = {
        "death_verdicts": metrics.death_verdicts,
        "confirmed_deaths": metrics.confirmed_deaths,
        "needless_recreates": needless_recreates,
        "transient_downgrades": metrics.transient_downgrades,
        "per_op_classifications": metrics.per_op_classifications,
        "op_failures": metrics.op_failures,
        "confirmation_alive": metrics.confirmation_alive,
        "confirmation_inconclusive": metrics.confirmation_inconclusive,
        "containers_created": metrics.containers_created,
        "container_create_failures": metrics.container_create_failures,
        "container_create_cancellations": metrics.container_create_cancellations,
        "execs_planned": planned_execs,
        "execs_scheduled": metrics.execs_scheduled,
        "execs_attempted": metrics.execs_attempted,
        "execs_completed": metrics.execs_completed,
        "execs_succeeded": metrics.execs_succeeded,
        "execs_cancelled": metrics.execs_cancelled,
        "op_exceptions": metrics.op_exceptions,
        "op_nonzero_results": metrics.op_nonzero_results,
        "op_timed_out_results": metrics.op_timed_out_results,
        "op_outer_timeouts": metrics.op_outer_timeouts,
        "status_inspect_probes": metrics.status_inspect_probes,
        "status_probe_running": metrics.status_probe_running,
        "status_probe_not_running": metrics.status_probe_not_running,
        "status_probe_failures": metrics.status_probe_failures,
        "status_probe_containers_covered": len(metrics.status_probe_completed_instance_ids),
        "instrumentation_errors": metrics.instrumentation_errors,
        "outstanding_create_tasks": metrics.outstanding_create_tasks,
    }
    return {
        "schema_version": 1,
        "harness": "gvisor/docker-py sandbox concurrency stress",
        "started_at": started_at,
        "finished_at": utc_now(),
        "elapsed_s": round(elapsed_s, 6),
        "timing": {
            "workload_timed_out": metrics.workload_timed_out,
            "create_phase_timed_out": metrics.create_phase_timed_out,
            "work_deadline_hit": metrics.work_deadline_hit,
            "global_duration_cap_hit": metrics.duration_cap_hit,
        },
        "configuration": {
            "backend": "local",
            "service_expected": "LocalSandboxService",
            "runtime": args.runtime,
            "docker_socket_argument": args.docker_socket,
            "docker_socket_normalized": docker_socket,
            "independent_inspect_transport": docker_cli.transport,
            "docker_server_version": docker_version,
            "image": IMAGE,
            "containers": args.containers,
            "execs_per_container": args.execs_per_container,
            "concurrency_per_container": args.concurrency,
            "maximum_simultaneous_execs": args.containers * args.concurrency,
            "per_exec_timeout_s": PER_EXEC_TIMEOUT_S,
            "per_exec_outer_timeout_s": PER_EXEC_OUTER_TIMEOUT_S,
            "docker_client_timeout_s": effective_client_timeout_s,
            "container_stop_timeout_s": effective_stop_timeout_s,
            "status_probe_interval_s": PROBE_INTERVAL_S,
            "duration_cap_s": args.duration_cap,
            "cleanup_reserve_s": round(reserve, 3),
            "conversation_id": conversation_id,
        },
        "real_service": {
            "class": (
                f"{type(service).__module__}.{type(service).__name__}"
                if service is not None
                else None
            ),
            "observed_instance_base_class": observed_instance_base,
        },
        "real_apis_used": [
            "SandboxConfig(backend='local', runtime=<runsc|runc>, image='disco-sandbox:base')",
            "service_from_config(config) -> LocalSandboxService",
            "LocalSandboxService.healthcheck()",
            "LocalSandboxService.create(SandboxSpec(), owner_id=..., conversation_id=...)",
            "LocalSandboxInstance.exec_shell(command, timeout_s=...)",
            "LocalSandboxInstance.destroy()",
            "LocalSandboxService.destroy_by_conversation(conversation_id)",
        ],
        "counters": counters,
        "metric_semantics": {
            "death_verdicts": (
                "Final SandboxUnavailableError verdicts with dead/died phrasing observed "
                "at the real classifier boundary."
            ),
            "confirmed_deaths": (
                "Death verdicts whose immediate independent inspect successfully read "
                "State.Running=false. Inspect errors/404s are not confirmation."
            ),
            "needless_recreates": "death_verdicts - confirmed_deaths",
            "transient_downgrades": (
                "Provisional death classifications overturned by the shared re-verify "
                "window into 'transient sandbox API error' per-op verdicts."
            ),
            "op_failures": (
                "Logical execs that raised to the caller or returned nonzero/timed-out results."
            ),
        },
        "pass_policy": {
            "needless_recreates_required": 0,
            "allowed_op_failures": allowed_op_failures,
            "op_failure_rate_ceiling": 0.01,
            "op_failure_threshold_justification": (
                "Allow at most floor(1% of planned execs): transient per-op API errors "
                "are retryable upstream, but more than 1% is too disruptive for a build. "
                "Small runs below 100 execs allow zero failures."
            ),
            "core_pass": core_pass,
            "validity_pass": validity_pass,
            "validity_requirements": [
                "all requested containers created",
                "all planned execs completed",
                "status/inspect probes covered every created container",
                "classifier instrumentation remained operational",
                "no retained container-create worker remained unsettled",
                "the workload completed before the reserved cleanup window",
                "global duration cap was not reached",
                "final docker ps and volume label checks succeeded with no leaks",
            ],
        },
        "verdict": "pass" if passed else "fail",
        "passed": passed,
        "failure_reasons": failure_reasons,
        "setup_errors": metrics.setup_errors,
        "orchestration_errors": metrics.orchestration_errors,
        "events": {
            "create_error_samples": metrics.create_error_samples,
            "op_failure_samples": metrics.op_failure_samples,
            "death_confirmations": metrics.death_confirmations,
            "status_probe_error_samples": metrics.status_probe_error_samples,
            "samples_capped_at": MAX_EVENT_SAMPLES,
        },
        "cleanup": cleanup_report,
        "debug_log": str(log_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Stress the real LocalSandboxService/docker-py path with concurrent "
            "runsc or runc containers and emit a JSON evidence report."
        )
    )
    parser.add_argument(
        "--containers",
        type=positive_int_at_most(MAX_CONTAINERS),
        default=6,
        metavar="M",
    )
    parser.add_argument(
        "--execs-per-container",
        type=positive_int_at_most(MAX_EXECS_PER_CONTAINER),
        default=40,
        metavar="K",
    )
    parser.add_argument(
        "--concurrency",
        type=positive_int_at_most(MAX_CONCURRENCY_PER_CONTAINER),
        default=4,
        help="maximum concurrent execs per container (default: 4)",
    )
    parser.add_argument(
        "--runtime",
        choices=("runsc", "runc"),
        default="runsc",
        help="Docker runtime; use runc for an A/B control (default: runsc)",
    )
    parser.add_argument(
        "--docker-socket",
        default="/var/run/docker.sock",
        help="Docker socket path or Docker base URL (default: /var/run/docker.sock)",
    )
    parser.add_argument(
        "--duration-cap",
        type=duration_cap_value,
        default=600.0,
        metavar="SECONDS",
        help="global workload plus teardown cap, minimum 10s (default: 600)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        metavar="FILE",
        help="write the JSON report to FILE; DEBUG evidence is written beside it",
    )
    return parser


def human_summary(report: dict[str, Any], report_path: Path) -> str:
    counters = report["counters"]
    allowed = report["pass_policy"]["allowed_op_failures"]
    cleanup_data = report.get("cleanup", {})
    authoritative_cleanup = cleanup_data.get("supervisor_final") or cleanup_data
    leaks: int | str = (
        "UNKNOWN"
        if authoritative_cleanup.get("leak_check_error")
        else len(authoritative_cleanup.get("leaks_remaining", []))
    )
    volumes: int | str = (
        "UNKNOWN"
        if authoritative_cleanup.get("volume_leak_check_error")
        else len(authoritative_cleanup.get("volumes_remaining", []))
    )
    return (
        f"{report['verdict'].upper()} gvisor-stress: "
        f"execs={counters['execs_completed']}/{counters['execs_planned']} "
        f"death_verdicts={counters['death_verdicts']} "
        f"confirmed_deaths={counters['confirmed_deaths']} "
        f"needless_recreates={counters['needless_recreates']} "
        f"transient_downgrades={counters['transient_downgrades']} "
        f"op_failures={counters['op_failures']} (allowed<={allowed}) "
        f"container_leaks={leaks} volume_leaks={volumes} report={report_path}"
    )


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def minimal_failure_report(
    args: argparse.Namespace,
    *,
    log_path: Path,
    conversation_id: str,
    reason: str,
    started_at: str,
    elapsed_s: float,
) -> dict[str, Any]:
    """Fail-closed report used only if the supervised worker cannot report."""
    planned = args.containers * args.execs_per_container
    allowed = math.floor(planned * 0.01)
    counters = {
        "death_verdicts": 0,
        "confirmed_deaths": 0,
        "needless_recreates": 0,
        "transient_downgrades": 0,
        "per_op_classifications": 0,
        "op_failures": 0,
        "containers_created": 0,
        "execs_planned": planned,
        "execs_scheduled": 0,
        "execs_attempted": 0,
        "execs_completed": 0,
        "execs_succeeded": 0,
        "execs_cancelled": 0,
    }
    return {
        "schema_version": 1,
        "harness": "gvisor/docker-py sandbox concurrency stress",
        "started_at": started_at,
        "finished_at": utc_now(),
        "elapsed_s": round(elapsed_s, 6),
        "timing": {
            "workload_timed_out": True,
            "create_phase_timed_out": False,
            "work_deadline_hit": False,
            "global_duration_cap_hit": True,
        },
        "configuration": {
            "backend": "local",
            "runtime": args.runtime,
            "docker_socket_argument": args.docker_socket,
            "image": IMAGE,
            "containers": args.containers,
            "execs_per_container": args.execs_per_container,
            "concurrency_per_container": args.concurrency,
            "duration_cap_s": args.duration_cap,
            "conversation_id": conversation_id,
        },
        "real_service": {"class": None, "observed_instance_base_class": None},
        "real_apis_used": [
            "SandboxConfig",
            "service_from_config -> LocalSandboxService",
            "healthcheck/create/exec_shell/destroy/destroy_by_conversation",
        ],
        "counters": counters,
        "pass_policy": {
            "needless_recreates_required": 0,
            "allowed_op_failures": allowed,
            "op_failure_rate_ceiling": 0.01,
            "core_pass": False,
            "validity_pass": False,
        },
        "verdict": "fail",
        "passed": False,
        "failure_reasons": [reason],
        "setup_errors": [],
        "orchestration_errors": [reason],
        "events": {},
        "cleanup": {},
        "debug_log": str(log_path),
    }


def worker_process_entry(
    args: argparse.Namespace,
    report_path: Path,
    log_path: Path,
    conversation_id: str,
) -> None:
    """Run all real SDK activity in a process the parent can bound/terminate."""
    started = time.monotonic()
    started_at = utc_now()
    try:
        with SandboxLogCapture(log_path):
            report = asyncio.run(run_harness(args, log_path, conversation_id=conversation_id))
    except BaseException as exc:  # noqa: BLE001 - child must leave a failure artifact
        report = minimal_failure_report(
            args,
            log_path=log_path,
            conversation_id=conversation_id,
            reason=clipped(f"worker {type(exc).__name__}: {exc}"),
            started_at=started_at,
            elapsed_s=time.monotonic() - started,
        )
    write_report(report_path, report)


def supervisor_cleanup(
    *,
    args: argparse.Namespace,
    conversation_id: str,
    deadline: float,
    worker_was_terminated: bool,
) -> tuple[dict[str, Any], str]:
    """Authoritative cleanup after the SDK worker and all its threads are gone."""
    try:
        docker_socket = normalize_docker_socket(args.docker_socket)
    except HarnessSetupError:
        docker_socket = "unix:///var/run/docker.sock"
    docker_cli = DockerCLI(docker_socket)
    remaining = deadline - time.monotonic()
    # If the worker was killed mid-request, keep watching for almost the entire
    # reserved tail: Docker may finish an already-received create after its
    # client disappeared. A normal, quiescent worker needs only a double-check.
    stability = max(0.15, remaining - 4.0) if worker_was_terminated else 0.15
    try:
        cleanup_report = asyncio.run(
            cleanup(
                service=None,
                instances=[],
                conversation_id=conversation_id,
                docker_cli=docker_cli,
                global_deadline=deadline,
                empty_stability_s=stability,
            )
        )
    except BaseException as exc:  # noqa: BLE001 - fail closed in the parent
        cleanup_report = {
            "leak_check_error": clipped(f"supervisor cleanup {type(exc).__name__}: {exc}"),
            "leaks_remaining": [],
        }
    return cleanup_report, docker_cli.transport


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    overall_started = time.monotonic()
    overall_started_at = utc_now()
    overall_deadline = overall_started + args.duration_cap
    report_path = args.out.expanduser().resolve()
    log_path = debug_log_path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.unlink(missing_ok=True)
    log_path.unlink(missing_ok=True)
    conversation_id = f"gvisor-stress-{uuid.uuid4().hex}"

    # The parent stays free of docker-py/default-executor work. If a package call
    # ignores cancellation, terminating this child eliminates its threads first;
    # only then does the parent perform the authoritative final label sweep.
    supervisor_tail = min(30.0, max(5.0, args.duration_cap * 0.10), args.duration_cap * 0.40)
    worker_budget = max(0.10, args.duration_cap - supervisor_tail)
    worker_args = argparse.Namespace(**vars(args))
    worker_args.duration_cap = max(0.10, worker_budget - min(2.0, worker_budget * 0.10))
    process = multiprocessing.get_context("spawn").Process(
        target=worker_process_entry,
        args=(worker_args, report_path, log_path, conversation_id),
        name="gvisor-stress-worker",
    )
    process.start()
    worker_stop_deadline = overall_deadline - supervisor_tail
    process.join(timeout=max(0.0, worker_stop_deadline - time.monotonic()))
    worker_was_terminated = process.is_alive()
    if process.is_alive():
        process.terminate()
        process.join(timeout=max(0.0, min(2.0, overall_deadline - time.monotonic())))
    if process.is_alive():
        process.kill()
        process.join(timeout=max(0.0, min(1.0, overall_deadline - time.monotonic())))
    worker_stopped = not process.is_alive()
    worker_exitcode = process.exitcode

    if worker_stopped:
        final_cleanup, cleanup_transport = supervisor_cleanup(
            args=args,
            conversation_id=conversation_id,
            deadline=overall_deadline,
            worker_was_terminated=worker_was_terminated,
        )
    else:
        final_cleanup = {
            "leak_check_error": (
                "SDK worker was still alive; a final label snapshot would not be authoritative"
            ),
            "leaks_remaining": [],
        }
        cleanup_transport = "not_run_worker_alive"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        report = minimal_failure_report(
            args,
            log_path=log_path,
            conversation_id=conversation_id,
            reason=clipped(f"worker report unavailable: {type(exc).__name__}: {exc}"),
            started_at=overall_started_at,
            elapsed_s=time.monotonic() - overall_started,
        )

    report.setdefault("cleanup", {})["supervisor_final"] = final_cleanup
    supervisor_failures: list[str] = []
    if worker_was_terminated:
        supervisor_failures.append(
            "supervisor terminated the SDK worker at its wall-clock boundary"
        )
    if not worker_stopped:
        supervisor_failures.append("SDK worker could not be stopped before final cleanup")
    if worker_exitcode not in (0, None) and not worker_was_terminated:
        supervisor_failures.append(f"SDK worker exited with status {worker_exitcode}")
    if final_cleanup.get("leak_check_error"):
        supervisor_failures.append("supervisor container leak verification failed")
    if final_cleanup.get("leaks_remaining"):
        supervisor_failures.append("supervisor found containers remaining after cleanup")
    if final_cleanup.get("volume_leak_check_error"):
        supervisor_failures.append("supervisor volume leak verification failed")
    if final_cleanup.get("volumes_remaining"):
        supervisor_failures.append("supervisor found volumes remaining after cleanup")

    total_elapsed = time.monotonic() - overall_started
    cap_hit = total_elapsed >= args.duration_cap
    if cap_hit:
        supervisor_failures.append("global wall-clock duration cap was reached")
    report["elapsed_s"] = round(total_elapsed, 6)
    report["finished_at"] = utc_now()
    report.setdefault("configuration", {})["conversation_id"] = conversation_id
    report["configuration"]["duration_cap_s"] = args.duration_cap
    report["configuration"]["worker_duration_cap_s"] = round(worker_args.duration_cap, 3)
    report["configuration"]["supervisor_cleanup_reserve_s"] = round(supervisor_tail, 3)
    report.setdefault("timing", {})["global_duration_cap_hit"] = cap_hit
    report["supervisor"] = {
        "worker_budget_s": round(worker_budget, 3),
        "worker_was_terminated": worker_was_terminated,
        "worker_stopped_before_final_cleanup": worker_stopped,
        "worker_exitcode": worker_exitcode,
        "final_cleanup_transport": cleanup_transport,
        "final_cleanup_authoritative": worker_stopped,
    }
    if supervisor_failures:
        report["passed"] = False
        report["verdict"] = "fail"
        reasons = report.setdefault("failure_reasons", [])
        reasons.extend(reason for reason in supervisor_failures if reason not in reasons)
        report.setdefault("pass_policy", {})["validity_pass"] = False
    else:
        # The parent check occurs after every SDK thread is gone, so it supersedes
        # a transient worker-side ps failure. Rebuild validity with the parent cap
        # and cleanup as the authoritative answers; all non-cleanup requirements
        # remain fail-closed from the worker evidence.
        counters = report.get("counters", {})
        timing = report.get("timing", {})
        completed_all = (
            counters.get("containers_created") == args.containers
            and counters.get("execs_scheduled") == args.containers * args.execs_per_container
            and counters.get("execs_completed") == args.containers * args.execs_per_container
        )
        probes_covered = counters.get("status_probe_containers_covered", 0) >= args.containers
        other_validity = (
            not report.get("setup_errors")
            and not report.get("orchestration_errors")
            and counters.get("instrumentation_errors", 0) == 0
            and counters.get("outstanding_create_tasks", 0) == 0
            and completed_all
            and probes_covered
            and not timing.get("workload_timed_out", False)
            and not cap_hit
        )
        policy = report.setdefault("pass_policy", {})
        policy["validity_pass"] = other_validity
        report["passed"] = bool(policy.get("core_pass", False) and other_validity)
        report["verdict"] = "pass" if report["passed"] else "fail"
        report["failure_reasons"] = [
            reason
            for reason in report.get("failure_reasons", [])
            if reason
            not in {
                "sandbox resource leak verification failed or resources remain",
                "global duration cap was reached",
            }
        ]

    write_report(report_path, report)
    print(human_summary(report, report_path), flush=True)
    print(
        "REAL_APIS: SandboxConfig -> service_from_config -> LocalSandboxService."
        "healthcheck/create -> LocalSandboxInstance.exec_shell -> "
        "destroy/destroy_by_conversation",
        flush=True,
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
