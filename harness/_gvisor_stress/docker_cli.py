"""Independent bounded Docker CLI/Engine transport."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import sys
import time
from typing import Any

from disco.tools.sandbox.naming import LABEL_CONV

from .constants import _ENGINE_API_HELPER, CLI_TIMEOUT_S, LEAK_CHECK_TIMEOUT_S
from .types import CommandResult, HarnessSetupError
from .util import clipped


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
