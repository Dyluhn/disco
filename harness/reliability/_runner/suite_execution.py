"""Subprocess lifecycle, logs, and one-suite execution."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import errno
import hashlib
import os
import signal
import stat
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.reliability.matrix import Suite
from harness.reliability.state import FAIL, INFRA, INVALID, PASS

from .common import _expand, _utc_now
from .provider_evidence import (
    _EXPECTED_PROVIDER_HOST_ENV,
    _EXPECTED_PROVIDER_MODEL_ENV,
    _PROVIDER_CONVERSATION_MANIFEST_ENV,
    _provider_evidence_result,
)
from .resource_pool import GIB, WeightedSuitePool
from .result_evidence import (
    _build_soak_result,
    _fresh_device_fingerprint,
    _fresh_device_result,
    _playwright_result,
    _pytest_result,
    _vitest_result,
)


async def _wait_for_process_leader(process: asyncio.subprocess.Process) -> int:
    """Wait for the leader only; pipe EOF may depend on longer-lived descendants."""

    while process.returncode is None:
        await asyncio.sleep(0.05)
    return process.returncode


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    """Boundedly terminate the entire session, even if its leader already exited."""

    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    if process.returncode is None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(_wait_for_process_leader(process), timeout=10)

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)

    if process.returncode is None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(_wait_for_process_leader(process), timeout=2)


def _close_subprocess_reader(reader: asyncio.StreamReader) -> None:
    """Close the pipe transport so a detached descriptor holder cannot wedge drain."""

    transport = getattr(reader, "_transport", None)
    if transport is not None:
        transport.close()


def _process_group_exists(process: asyncio.subprocess.Process) -> bool:
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _suite_subprocess_environment(kind: str) -> dict[str, str]:
    env = os.environ.copy()
    if kind == "playwright":
        # Playwright deliberately sets FORCE_COLOR for its web server and
        # workers. Inheriting NO_COLOR as well makes Node warn on every child
        # process, so let Playwright own color policy for Playwright suites.
        env.pop("NO_COLOR", None)
    return env


class _SuiteLogRenderer:
    """Incrementally render UTF-8 evidence without active control characters."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="surrogateescape")
        self._pending_cr = False

    @staticmethod
    def _render_character(character: str) -> bytes:
        value = ord(character)
        if character in {"\t", "\n"}:
            return character.encode("ascii")
        if 0xDC80 <= value <= 0xDCFF:
            return f"\\x{value - 0xDC00:02x}".encode("ascii")
        if unicodedata.category(character).startswith("C"):
            if value <= 0xFF:
                return f"\\x{value:02x}".encode("ascii")
            if value <= 0xFFFF:
                return f"\\u{value:04x}".encode("ascii")
            return f"\\U{value:08x}".encode("ascii")
        return character.encode("utf-8")

    def feed(self, chunk: bytes, *, final: bool = False) -> bytes:
        text = self._decoder.decode(chunk, final=final)
        rendered = bytearray()
        for character in text:
            if self._pending_cr:
                if character == "\n":
                    rendered.extend(b"\r\n")
                    self._pending_cr = False
                    continue
                rendered.extend(b"\\x0d")
                self._pending_cr = False
            if character == "\r":
                self._pending_cr = True
                continue
            rendered.extend(self._render_character(character))
        if final and self._pending_cr:
            rendered.extend(b"\\x0d")
            self._pending_cr = False
        return bytes(rendered)


def _sanitize_suite_log(path: Path) -> None:
    """Atomically canonicalize an existing log using the streaming renderer."""

    if not path.exists():
        return
    temporary = path.with_name(f".{path.name}.sanitize-{os.getpid()}")
    try:
        with path.open("rb") as source, temporary.open("xb") as target:
            os.fchmod(target.fileno(), 0o600)
            renderer = _SuiteLogRenderer()
            while chunk := source.read(64 * 1024):
                target.write(renderer.feed(chunk))
            target.write(renderer.feed(b"", final=True))
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


async def _stream_sanitized_suite_log(
    reader: asyncio.StreamReader,
    path: Path,
) -> tuple[int, int, str]:
    """Write only render-safe bytes, including while the child is still running."""

    renderer = _SuiteLogRenderer()
    digest = hashlib.sha256()
    with path.open("xb") as target:
        os.fchmod(target.fileno(), 0o600)
        while chunk := await reader.read(64 * 1024):
            rendered = renderer.feed(chunk)
            target.write(rendered)
            digest.update(rendered)
            target.flush()
        rendered = renderer.feed(b"", final=True)
        target.write(rendered)
        digest.update(rendered)
        target.flush()
        os.fsync(target.fileno())
        metadata = os.fstat(target.fileno())
        return metadata.st_dev, metadata.st_ino, digest.hexdigest()


def _suite_log_integrity_reason(
    path: Path,
    expected: tuple[int, int, str],
) -> str | None:
    """Validate the exact streamed log inode after all suite processes are gone."""

    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
    except FileNotFoundError:
        return "suite log disappeared before evidence finalization"
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENXIO}:
            return "suite log was replaced by a non-regular or aliased path"
        return f"suite log metadata is unreadable: {type(exc).__name__}"
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return "suite log is not a regular file"
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            return "suite log permissions are not 0600"
        if (metadata.st_dev, metadata.st_ino) != expected[:2]:
            return "suite log pathname no longer names the streamed evidence inode"
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 64 * 1024):
            digest.update(chunk)
        if digest.hexdigest() != expected[2]:
            return "suite log content changed after streaming"
        return None
    except OSError as exc:
        return f"suite log is unreadable: {type(exc).__name__}"
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _structured_command(
    suite: Suite, command: list[str], suite_out: Path, env: dict[str, str]
) -> tuple[list[str], Path | None]:
    if any(Path(part).name in {"pytest", "pytest.exe"} for part in command):
        report = suite_out / "pytest.xml"
        command.append(f"--junitxml={report}")
        return command, report
    if suite.kind == "playwright":
        report = suite_out / "playwright.json"
        env["PLAYWRIGHT_JSON_OUTPUT_FILE"] = str(report)
        return command, report
    if suite.kind == "vitest":
        return command, suite_out / "vitest.json"
    if suite.kind == "fresh_device":
        return command, suite_out / "fresh-device-result.json"
    return command, None


def _configure_provider_evidence_environment(
    suite: Suite,
    suite_out: Path,
    env: dict[str, str],
) -> tuple[Path, Path | None]:
    """Own suite-private provider evidence paths and reject ambient manifests."""

    provider_ledger_path = (suite_out / "provider-ledger.jsonl").resolve()
    env.pop("DISCO_PROVIDER_LEDGER", None)
    env.pop(_PROVIDER_CONVERSATION_MANIFEST_ENV, None)
    conversation_manifest_path: Path | None = None
    if suite.provider_evidence:
        # Never inherit or share an ambient ledger across parallel suites. Each
        # campaign-owned stack writes an isolated, auditable call stream.
        env["DISCO_PROVIDER_LEDGER"] = str(provider_ledger_path)
    if suite.provider_conversation_manifest:
        conversation_manifest_path = (suite_out / "provider-conversations.json").resolve()
        env[_PROVIDER_CONVERSATION_MANIFEST_ENV] = str(conversation_manifest_path)
    return provider_ledger_path, conversation_manifest_path


@dataclass(frozen=True)
class _SuiteLaunch:
    base: dict[str, Any]
    suite_out: Path
    cwd: Path
    command: list[str]
    env: dict[str, str]
    provider_ledger_path: Path
    provider_conversation_manifest_path: Path | None
    structured_path: Path | None
    log_path: Path


def _suite_refusal(base: dict[str, Any], reason: str) -> dict[str, Any]:
    return {**base, "status": INVALID, "reason": reason, "finished_at": _utc_now()}


def _prepare_suite_launch(
    suite: Suite,
    *,
    context: dict[str, str],
    campaign_out: Path,
) -> _SuiteLaunch | dict[str, Any]:
    started_at = _utc_now()
    suite_out = campaign_out / suite.id
    suite_out.mkdir(parents=True, exist_ok=False)
    provider_env = (
        (_EXPECTED_PROVIDER_HOST_ENV, _EXPECTED_PROVIDER_MODEL_ENV)
        if suite.provider_evidence
        else ()
    )
    missing_env = [
        name for name in (*suite.requires_env, *provider_env) if not os.environ.get(name)
    ]
    base = {
        "suite_id": suite.id,
        "proof": suite.proof,
        "kind": suite.kind,
        "claims": list(suite.claims),
        "units_expected": suite.units,
        "units_passed": 0,
        "started_at": started_at,
        "output_dir": str(suite_out),
    }
    if missing_env:
        return _suite_refusal(base, f"missing required environment: {', '.join(missing_env)}")

    suite_context = {**context, "suite_out": str(suite_out)}
    try:
        cwd = Path(_expand(suite.cwd, suite_context))
        command = [_expand(part, suite_context) for part in suite.command]
    except ValueError as exc:
        return _suite_refusal(base, str(exc))
    if not cwd.is_dir():
        return _suite_refusal(base, f"suite working directory does not exist: {cwd}")

    env = _suite_subprocess_environment(suite.kind)
    try:
        env.update({key: _expand(value, suite_context) for key, value in suite.environment.items()})
    except ValueError as exc:
        return _suite_refusal(base, str(exc))
    env["DISCO_RELIABILITY_SUITE_OUT"] = str(suite_out)
    provider_ledger_path, provider_conversation_manifest_path = (
        _configure_provider_evidence_environment(suite, suite_out, env)
    )
    command, structured_path = _structured_command(suite, command, suite_out, env)
    return _SuiteLaunch(
        base=base,
        suite_out=suite_out,
        cwd=cwd,
        command=command,
        env=env,
        provider_ledger_path=provider_ledger_path,
        provider_conversation_manifest_path=provider_conversation_manifest_path,
        structured_path=structured_path,
        log_path=suite_out / "suite.log",
    )


async def _execute_suite_process(
    suite: Suite,
    launch: _SuiteLaunch,
    pool: WeightedSuitePool,
) -> tuple[int, bool] | dict[str, Any]:
    timed_out = False
    try:
        async with pool.slot(int(suite.memory_gib * GIB)):
            print(f"[reliability] START {suite.id}: {' '.join(launch.command)}")
            process = await asyncio.create_subprocess_exec(
                *launch.command,
                cwd=launch.cwd,
                env=launch.env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            if process.stdout is None:
                raise RuntimeError("suite subprocess has no output stream")
            wait_task = asyncio.create_task(_wait_for_process_leader(process))
            log_task = asyncio.create_task(
                _stream_sanitized_suite_log(process.stdout, launch.log_path)
            )
            log_identity: tuple[int, int, str] | None = None
            try:
                await asyncio.wait_for(asyncio.shield(wait_task), timeout=suite.timeout_s)
            except TimeoutError:
                timed_out = True
                await _terminate_process_group(process)
            except BaseException:
                await _terminate_process_group(process)
                _close_subprocess_reader(process.stdout)
                log_task.cancel()
                wait_task.cancel()
                await asyncio.gather(wait_task, log_task, return_exceptions=True)
                raise
            try:
                log_identity = await asyncio.wait_for(asyncio.shield(log_task), timeout=2)
            except TimeoutError as exc:
                await _terminate_process_group(process)
                _close_subprocess_reader(process.stdout)
                log_task.cancel()
                await asyncio.gather(log_task, return_exceptions=True)
                if not timed_out:
                    raise RuntimeError(
                        "suite leader exited but a descendant retained the output pipe"
                    ) from exc
            except BaseException:
                await _terminate_process_group(process)
                _close_subprocess_reader(process.stdout)
                log_task.cancel()
                wait_task.cancel()
                await asyncio.gather(wait_task, log_task, return_exceptions=True)
                raise
            if not wait_task.done():
                wait_task.cancel()
                await asyncio.gather(wait_task, return_exceptions=True)
            if _process_group_exists(process):
                await _terminate_process_group(process)
                raise RuntimeError(
                    "suite leader exited while a descendant process group remained alive"
                )
            if log_identity is None:
                raise RuntimeError("suite log did not finalize after process termination")
            log_reason = _suite_log_integrity_reason(launch.log_path, log_identity)
            if log_reason is not None:
                raise RuntimeError(log_reason)
            exit_code = process.returncode if process.returncode is not None else -1
    except (OSError, RuntimeError, TimeoutError) as exc:
        result = {
            **launch.base,
            "status": INFRA,
            "reason": f"suite could not run: {type(exc).__name__}: {exc}",
            "finished_at": _utc_now(),
            "command": launch.command,
            "log_path": str(launch.log_path),
        }
        print(f"[reliability] INFRA {suite.id}: {result['reason']}")
        return result
    return exit_code, timed_out


def _classify_suite_result(
    suite: Suite,
    launch: _SuiteLaunch,
    *,
    exit_code: int,
    timed_out: bool,
) -> tuple[str, int, str]:
    if timed_out:
        return (
            INFRA,
            0,
            f"suite process exceeded its {suite.timeout_s:g}s outer timeout",
        )
    if launch.structured_path and launch.structured_path.name == "pytest.xml":
        return _pytest_result(launch.structured_path, exit_code=exit_code, units=suite.units)
    if suite.kind == "playwright":
        return _playwright_result(
            launch.structured_path or Path(), exit_code=exit_code, units=suite.units
        )
    if suite.kind == "vitest":
        return _vitest_result(
            launch.structured_path or Path(), exit_code=exit_code, units=suite.units
        )
    if suite.kind == "build_soak":
        return _build_soak_result(launch.suite_out, exit_code=exit_code, units=suite.units)
    if suite.kind == "fresh_device":
        return _fresh_device_result(
            launch.structured_path or Path(), exit_code=exit_code, units=suite.units
        )
    if exit_code == 0:
        return PASS, suite.units, "command completed successfully"
    return FAIL, 0, f"command exited {exit_code}"


def _adjudicate_provider_evidence(
    suite: Suite,
    launch: _SuiteLaunch,
    status: str,
    units_passed: int,
    reason: str,
) -> tuple[str, int, str, dict[str, Any] | None]:
    if not suite.provider_evidence:
        return status, units_passed, reason, None
    provider_status, provider_units, provider_reason = _provider_evidence_result(
        launch.provider_ledger_path,
        expected_host=os.environ.get(_EXPECTED_PROVIDER_HOST_ENV, ""),
        expected_model=os.environ.get(_EXPECTED_PROVIDER_MODEL_ENV, ""),
        units=suite.units,
        conversation_manifest_path=launch.provider_conversation_manifest_path,
        expected_conversations=(suite.provider_conversation_count if status == PASS else None),
    )
    evidence: dict[str, Any] = {
        "status": provider_status,
        "reason": provider_reason,
        "units_passed": provider_units,
        "path": str(launch.provider_ledger_path),
    }
    if launch.provider_conversation_manifest_path is not None:
        evidence["conversation_manifest_path"] = str(launch.provider_conversation_manifest_path)
    if status == PASS and provider_status != PASS:
        return provider_status, 0, provider_reason, evidence
    if status != PASS and provider_status != PASS:
        reason = f"{reason}; provider evidence: {provider_reason}"
    return status, units_passed, reason, evidence


async def _run_suite(
    suite: Suite,
    *,
    context: dict[str, str],
    campaign_out: Path,
    pool: WeightedSuitePool,
) -> dict[str, Any]:
    launch = _prepare_suite_launch(suite, context=context, campaign_out=campaign_out)
    if isinstance(launch, dict):
        return launch
    executed = await _execute_suite_process(suite, launch, pool)
    if isinstance(executed, dict):
        return executed
    exit_code, timed_out = executed
    status, units_passed, reason = _classify_suite_result(
        suite, launch, exit_code=exit_code, timed_out=timed_out
    )
    status, units_passed, reason, provider_evidence = _adjudicate_provider_evidence(
        suite, launch, status, units_passed, reason
    )
    result = {
        **launch.base,
        "status": status,
        "reason": reason,
        "units_passed": units_passed,
        "finished_at": _utc_now(),
        "exit_code": exit_code,
        "command": launch.command,
        "log_path": str(launch.log_path),
    }
    if provider_evidence is not None:
        result["provider_evidence"] = provider_evidence
    if suite.proof == "fresh_device":
        result["fresh_device_id"] = _fresh_device_fingerprint(launch.structured_path or Path())
    print(f"[reliability] {status} {suite.id}: {reason}")
    return result
