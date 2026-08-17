"""Definition-of-Done (DoD) evaluator — a fresh-context, read-only judge.

C1a stored the agent's acceptance criteria (the DoD spec) OUTSIDE the
agent-editable event stream, write-once, so the model cannot rewrite what it
is being judged against.  C1b is the JUDGE: a SEPARATE, FRESH context that
sees only the spec and the deliverable evidence (the workspace), never the
agent's transcript, view, tool registry, or self-justify prose.

The three predicate kinds (file_exists / command / http_ok) are all
DETERMINISTIC — the evaluator re-runs each check from scratch.  A future
subjective kind would need an LLM judgment; the ``SubjectiveJudge`` seam is
here but C1b does NOT add a new predicate kind.  An unwired subjective
predicate is a HARD-FAIL (we never silently pass a check we did not run).

Read-only contract: no ``disco.tools.*`` import, no ``EventStore`` write, no
agent view mutation.  The command runner is a bounded subprocess; the HTTP
probe is a read-only GET with loopback/RFC1918 egress by default.

The verdict's ``spec_fingerprint`` is a SHA-256 over the spec's JSON form so
the audit trail can pair "agent tried to finish" with "evaluator checked this
exact spec" without re-deriving it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .dod import (
    CommandExitPredicate,
    DoDPredicate,
    DoDSpec,
    FileExistsPredicate,
    HTTPOkPredicate,
)
from .dod_util import _egress_allowed, _hard_deny_reason, tail
from .host_egress import EgressDenied, guarded_get

# ---- result types ----------------------------------------------------------


class CommandResult(BaseModel):
    """Outcome of a ``command`` predicate (``exit_code`` None if denied/timeout)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error_message: str = ""
    duration_seconds: float = 0.0
    denied: bool = False  # destructive-command deny-list refused to run
    deny_reason: str = ""
    timed_out: bool = False  # executor-owned deadline, not a command exit code


class HttpProbeResult(BaseModel):
    """Outcome of an ``http_ok`` probe (``status_code`` None if no wire)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status_code: int | None = None
    error_message: str = ""
    duration_seconds: float = 0.0
    egress_denied: bool = False
    egress_reason: str = ""


class DoDPredicateResult(BaseModel):
    """Per-predicate outcome (``predicate`` is the specific instance)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    predicate: DoDPredicate
    passed: bool
    reason: str
    # INFRA-vs-TASK (DoD v2.1): True iff FAILED because the check could not RUN
    # (denied, timeout, egress denied, missing judge).  The finish gate treats
    # unverifiable-only failures as a RELEASE; a real task failure still blocks.
    unverifiable: bool = False
    # Kind-specific structured evidence (exit_code, status_code, file size,
    # observed predicate field, ...). The verdict carries the FULL record so
    # a human reviewer can re-derive the judgment without re-running.
    details: dict[str, Any] = Field(default_factory=dict)


class DoDVerdict(BaseModel):
    """Structured judgment: ``passed`` = AND of all predicates, ``unmet`` names failures."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    unmet: list[DoDPredicate]
    results: list[DoDPredicateResult]
    spec_predicate_count: int
    evaluated_at: str  # ISO-8601 UTC
    conversation_id: str | None = None
    spec_fingerprint: str


# ---- subjective-judge seam (future-proofing; no subjective kind in C1a) -----


class SubjectiveVerdict(BaseModel):
    """A future subjective predicate's judgment (seam for a non-deterministic kind)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    passed: bool
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)


class SubjectiveJudgeRequest(BaseModel):
    """Predicate + spec + workspace root for a future judge (never transcript)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    predicate: DoDPredicate
    spec_predicates: list[DoDPredicate]
    workspace_root: str
    evidence_excerpt: str = ""


@runtime_checkable
class SubjectiveJudge(Protocol):
    """LLM-judge seam: fresh-context, spec + evidence only, never transcript."""

    async def judge(self, req: SubjectiveJudgeRequest) -> SubjectiveVerdict: ...


# ---- default command runner (subprocess, with destructive-command gate) ---


_DEFAULT_COMMAND_TIMEOUT_SECONDS = 30.0
_DEFAULT_COMMAND_TERMINATION_GRACE_SECONDS = 1.0
_COMMAND_CAPTURE_HEAD_BYTES = 64 * 1024
_COMMAND_CAPTURE_TAIL_BYTES = 64 * 1024
_DEFAULT_HTTP_TIMEOUT_SECONDS = 10.0

# Egress allow-list for the HTTP probe. Loopback + private RFC1918 by default
# — mirrors the live loop's server-aware finish probe (`_app_verify_command`),
# which only GETs `http://localhost:<port>/…` against the just-served
# deliverable. Real public-URL probes should be added to this list explicitly
# (tested by injecting a custom probe); we do NOT default to "any host".
_DEFAULT_HTTP_ALLOW_HOSTS = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "::1",
        # RFC1918 is the in-cluster / dev-server shape the deliverable
        # actually serves from. A real public-URL probe belongs in a custom
        # `http_probe` argument; this default is the safe one.
    }
)


class _BoundedCapture:
    """Bounded head+tail capture of one subprocess stream."""

    def __init__(self, stream: str) -> None:
        self.stream = stream
        self.total = 0
        self.small = bytearray()
        self.head = bytearray()
        self.tail = bytearray()

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        return_cap = _COMMAND_CAPTURE_HEAD_BYTES + _COMMAND_CAPTURE_TAIL_BYTES
        if len(self.small) <= return_cap:
            room = return_cap + 1 - len(self.small)
            self.small.extend(chunk[:room])
        if len(self.head) < _COMMAND_CAPTURE_HEAD_BYTES:
            self.head.extend(chunk[: _COMMAND_CAPTURE_HEAD_BYTES - len(self.head)])
        self.tail.extend(chunk)
        if len(self.tail) > _COMMAND_CAPTURE_TAIL_BYTES:
            del self.tail[:-_COMMAND_CAPTURE_TAIL_BYTES]

    def render(self) -> str:
        return_cap = _COMMAND_CAPTURE_HEAD_BYTES + _COMMAND_CAPTURE_TAIL_BYTES
        if self.total <= return_cap:
            data = bytes(self.small[: self.total])
        else:
            omitted = max(0, self.total - len(self.head) - len(self.tail))
            marker = (
                f"\n[disco: {self.stream} truncated; {self.total} bytes total, "
                f"{omitted} omitted]\n"
            ).encode()
            data = bytes(self.head) + marker + bytes(self.tail)
        return data.decode("utf-8", errors="replace")


def _drain_streams(
    proc: subprocess.Popen[bytes],
    selector: selectors.DefaultSelector,
    streams: dict[int, Any],
    deadline: float,
) -> bool:
    """Drain stdout/stderr until the deadline or the process exits."""

    while True:
        if proc.poll() is not None and not selector.get_map():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return proc.poll() is not None and not selector.get_map()
        for key, _mask in selector.select(timeout=min(remaining, 0.1)):
            fd = key.fd
            try:
                chunk = os.read(fd, 64 * 1024)
            except BlockingIOError:
                continue
            if chunk:
                key.data.feed(chunk)
            else:
                selector.unregister(fd)
                streams.pop(fd).close()


def _run_command(
    command: str,
    cwd: Path,
    timeout_seconds: float,
) -> tuple[int | None, str, str, bool]:
    """Run ``command`` in a fresh subprocess with bounded capture and timeout."""

    proc = subprocess.Popen(  # noqa: S602 - model command is deny-gated above
        command,
        shell=True,
        executable="/bin/bash",  # H342: match sandbox exec_shell
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env={"PATH": os.environ.get("PATH", "")},  # no leaked agent secrets
    )
    stdout_capture = _BoundedCapture("stdout")
    stderr_capture = _BoundedCapture("stderr")
    selector = selectors.DefaultSelector()
    assert proc.stdout is not None and proc.stderr is not None
    streams = {proc.stdout.fileno(): proc.stdout, proc.stderr.fileno(): proc.stderr}
    selector.register(proc.stdout.fileno(), selectors.EVENT_READ, stdout_capture)
    selector.register(proc.stderr.fileno(), selectors.EVENT_READ, stderr_capture)

    timed_out = not _drain_streams(proc, selector, streams, time.monotonic() + timeout_seconds)
    if timed_out:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                pass
            if _drain_streams(
                proc, selector, streams,
                time.monotonic() + _DEFAULT_COMMAND_TERMINATION_GRACE_SECONDS,
            ):
                break
    for key in list(selector.get_map().values()):
        selector.unregister(key.fd)
        streams.pop(key.fd).close()
    selector.close()
    if proc.poll() is None:
        try:
            proc.wait(timeout=_DEFAULT_COMMAND_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
    return (
        None if timed_out else int(proc.returncode),
        stdout_capture.render(),
        stderr_capture.render(),
        timed_out,
    )


async def _default_command_runner(
    command: str,
    *,
    cwd: Path,
    timeout_seconds: float,
) -> CommandResult:
    """Default command runner: bounded subprocess with hard timeout.

    A denied command is ``denied=True`` with ``exit_code=None`` — "not run",
    not "run and failed".
    """
    deny = _hard_deny_reason(command)
    if deny is not None:
        return CommandResult(
            exit_code=None,
            stdout="",
            stderr="",
            error_message=f"hard-denied: {deny}",
            duration_seconds=0.0,
            denied=True,
            deny_reason=deny,
        )
    started = datetime.now(UTC).timestamp()

    try:
        exit_code, stdout, stderr, timed_out = await asyncio.to_thread(
            _run_command, command, cwd, timeout_seconds
        )
    except Exception as exc:  # pragma: no cover - defensive executor surface
        duration = datetime.now(UTC).timestamp() - started
        return CommandResult(
            exit_code=None,
            stdout="",
            stderr="",
            error_message=f"executor error: {type(exc).__name__}: {exc}",
            duration_seconds=duration,
        )
    duration = datetime.now(UTC).timestamp() - started
    return CommandResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        error_message=f"timeout after {timeout_seconds}s" if timed_out else "",
        duration_seconds=duration,
        timed_out=timed_out,
    )


# ---- default http probe ----------------------------------------------------


async def _default_http_probe(
    url: str,
    *,
    expected_status: int,
    timeout_seconds: float,
    allow_hosts: frozenset[str],
) -> HttpProbeResult:
    """Default HTTP probe: read-only GET with egress gate checked before wire."""
    allowed, reason = _egress_allowed(url, allow_hosts)
    if not allowed:
        return HttpProbeResult(
            status_code=None,
            error_message=f"egress denied: {reason}",
            duration_seconds=0.0,
            egress_denied=True,
            egress_reason=reason,
        )
    started = datetime.now(UTC).timestamp()

    try:
        resp = await guarded_get(
            url,
            timeout_s=timeout_seconds,
            allow_hosts=allow_hosts if allow_hosts else None,
        )
        status, err = resp.status_code, ""
    except (EgressDenied, TimeoutError, OSError) as exc:
        duration = datetime.now(UTC).timestamp() - started
        denied = isinstance(exc, EgressDenied)
        return HttpProbeResult(
            status_code=None,
            error_message=f"{type(exc).__name__}: {exc}",
            duration_seconds=duration,
            egress_denied=denied,
            egress_reason=str(exc) if denied else "",
        )
    except Exception as exc:  # pragma: no cover - defensive executor surface
        duration = datetime.now(UTC).timestamp() - started
        return HttpProbeResult(
            status_code=None,
            error_message=f"probe error: {type(exc).__name__}: {exc}",
            duration_seconds=duration,
        )
    duration = datetime.now(UTC).timestamp() - started
    return HttpProbeResult(
        status_code=status,
        error_message=err,
        duration_seconds=duration,
    )


# ---- the evaluator ---------------------------------------------------------


# Dependency-injected seams — typed explicitly so the test can substitute.
# Each default is the safe production implementation; the test substitutes
# fakes to keep the unit test hermetic (no real subprocess, no real network).
CommandRunner = Callable[[str], Awaitable[CommandResult]]
HttpProbe = Callable[[str, int], Awaitable[HttpProbeResult]]
FileChecker = Callable[[FileExistsPredicate], Awaitable[DoDPredicateResult]]


def _file_exists_details(
    predicate: FileExistsPredicate, resolved: Path, **extra: Any
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "kind": "file_exists",
        "path": predicate.path,
        "resolved": str(resolved),
    }
    details.update(extra)
    return details


def _command_details(predicate: CommandExitPredicate, result: CommandResult) -> dict[str, Any]:
    return {
        "kind": "command",
        "cmd": predicate.cmd,
        "expect_exit": predicate.expect_exit,
        "exit_code": result.exit_code,
        "stdout_tail": tail(result.stdout, 2000),
        "stderr_tail": tail(result.stderr, 2000),
        "error_message": result.error_message,
        "denied": result.denied,
        "deny_reason": result.deny_reason,
        "timed_out": result.timed_out,
        "duration_seconds": result.duration_seconds,
    }


def _http_details(predicate: HTTPOkPredicate, result: HttpProbeResult) -> dict[str, Any]:
    return {
        "kind": "http_ok",
        "url": predicate.url,
        "expect_status": predicate.expect_status,
        "status_code": result.status_code,
        "error_message": result.error_message,
        "egress_denied": result.egress_denied,
        "egress_reason": result.egress_reason,
        "duration_seconds": result.duration_seconds,
    }


def _command_reason(result: CommandResult, expect_exit: int) -> str:
    if result.denied:
        return f"command not run: hard-denied ({result.deny_reason})"
    if result.error_message:
        return f"command not run: {result.error_message}"
    return f"command exited {result.exit_code}, expected {expect_exit}"


def _http_reason(result: HttpProbeResult, expect_status: int) -> str:
    if result.egress_denied:
        return f"probe not run: egress denied ({result.egress_reason})"
    if result.error_message:
        return f"probe not run: {result.error_message}"
    return f"HTTP {result.status_code}, expected {expect_status}"


class DoDEvaluator:
    """The fresh-context, read-only judge for a ``DoDSpec``.

    Construction is dependency-injected: ``command_runner``, ``http_probe``,
    ``file_checker``, and ``subjective_judge`` seams default to safe production
    implementations (bounded subprocess, loopback GET, ``Path.exists``, and
    HARD-FAIL respectively).  The evaluator holds a ``workspace_root`` for
    resolving ``file_exists`` paths and as ``cwd`` for command predicates;
    path-escape attempts are a hard fail.
    """

    def __init__(
        self,
        workspace_root: Path | str,
        *,
        command_runner: CommandRunner | None = None,
        http_probe: HttpProbe | None = None,
        file_checker: FileChecker | None = None,
        subjective_judge: SubjectiveJudge | None = None,
        command_timeout_seconds: float = _DEFAULT_COMMAND_TIMEOUT_SECONDS,
        http_timeout_seconds: float = _DEFAULT_HTTP_TIMEOUT_SECONDS,
        http_allow_hosts: frozenset[str] = _DEFAULT_HTTP_ALLOW_HOSTS,
    ) -> None:
        self._workspace_root = Path(workspace_root).resolve()
        # A missing workspace is a misconfiguration; construct anyway so every
        # check fails closed rather than crashing at construction.
        self._subjective_judge = subjective_judge
        self._file_checker = file_checker
        self._command_timeout_seconds = float(command_timeout_seconds)
        self._http_timeout_seconds = float(http_timeout_seconds)
        self._http_allow_hosts = frozenset(http_allow_hosts)
        # Then build the default seams (the closures capture the scalars
        # above). If the caller injected a `command_runner` / `http_probe`,
        # we use that and skip the default.
        self._command_runner = command_runner or self._build_default_command_runner()
        self._http_probe = http_probe or self._build_default_http_probe(
            http_timeout_seconds=http_timeout_seconds,
            http_allow_hosts=http_allow_hosts,
        )

    def _build_default_command_runner(self) -> CommandRunner:
        cwd = self._workspace_root
        timeout = self._command_timeout_seconds

        async def runner(command: str) -> CommandResult:
            return await _default_command_runner(command, cwd=cwd, timeout_seconds=timeout)

        return runner

    def _build_default_http_probe(
        self,
        *,
        http_timeout_seconds: float,
        http_allow_hosts: frozenset[str],
    ) -> HttpProbe:
        timeout = http_timeout_seconds
        allow = http_allow_hosts

        async def probe(url: str, expected_status: int) -> HttpProbeResult:
            return await _default_http_probe(
                url,
                expected_status=expected_status,
                timeout_seconds=timeout,
                allow_hosts=allow,
            )

        return probe

    # ---- public surface -----------------------------------------------------

    @property
    def workspace_root(self) -> Path:
        """The (resolved) workspace root. Read-only — the evaluator never
        mutates files in it."""
        return self._workspace_root

    async def evaluate(
        self,
        spec: DoDSpec,
        *,
        conversation_id: str | None = None,
    ) -> DoDVerdict:
        """Run every predicate's check and return the structured verdict.

        Predicates are evaluated in spec order; ALL must pass.  The verdict's
        ``unmet`` lists the specific predicates that did not pass.  Never
        raises — every failure mode is recorded as a ``DoDPredicateResult``.
        """
        results: list[DoDPredicateResult] = []
        for predicate in spec.predicates:
            results.append(await self._evaluate_one(predicate, spec))
        unmet = [r.predicate for r in results if not r.passed]
        return DoDVerdict(
            passed=(len(unmet) == 0 and len(results) > 0),
            unmet=unmet,
            results=results,
            spec_predicate_count=len(spec.predicates),
            evaluated_at=datetime.now(UTC).isoformat(),
            conversation_id=conversation_id,
            spec_fingerprint=spec_fingerprint(spec),
        )

    # ---- per-predicate dispatch --------------------------------------------

    async def _evaluate_one(self, predicate: DoDPredicate, spec: DoDSpec) -> DoDPredicateResult:
        # Discriminate by concrete type — the Pydantic `kind` discriminator
        # is the canonical field, but `isinstance` is the cheapest dispatch
        # and the union is closed (only three kinds as of C1a).
        if isinstance(predicate, FileExistsPredicate):
            return await self._check_file_exists(predicate)
        if isinstance(predicate, CommandExitPredicate):
            return await self._check_command_exit(predicate)
        if isinstance(predicate, HTTPOkPredicate):
            return await self._check_http_ok(predicate)
        # Future subjective kinds land here. The seam exists, but C1a has
        # none. A `subjective_judge is None` case is a HARD-FAIL.
        return await self._check_subjective(predicate, spec)

    # ---- deterministic checks ---------------------------------------------

    async def _check_file_exists(self, predicate: FileExistsPredicate) -> DoDPredicateResult:
        """Resolve the predicate's path against the workspace root and check
        existence. A path-escape is a hard fail; a directory or empty file is
        unmet (anti-gaming: a declared deliverable must be a non-empty regular file)."""
        if self._file_checker is not None:
            return await self._file_checker(predicate)
        try:
            resolved = resolve_under_workspace(self._workspace_root, predicate.path)
        except PathEscapeError as exc:
            return DoDPredicateResult(
                predicate=predicate,
                passed=False,
                reason=f"path escapes workspace: {exc}",
                details={
                    "kind": "file_exists",
                    "path": predicate.path,
                    "workspace_root": str(self._workspace_root),
                    "escape": True,
                },
            )
        exists = resolved.exists()
        if exists:
            stat = resolved.stat()
            is_file = resolved.is_file()
            if is_file and stat.st_size > 0:
                return DoDPredicateResult(
                    predicate=predicate,
                    passed=True,
                    reason=f"file exists at {resolved} ({stat.st_size} bytes)",
                    details=_file_exists_details(
                        predicate, resolved, size_bytes=stat.st_size
                    ),
                )
            why = "is a directory" if not is_file else "is empty (0 bytes)"
            return DoDPredicateResult(
                predicate=predicate,
                passed=False,
                reason=(
                    f"path exists but {why}: {predicate.path} — a declared deliverable "
                    f"must be a non-empty regular file (write its real content)"
                ),
                details=_file_exists_details(
                    predicate,
                    resolved,
                    size_bytes=stat.st_size,
                    is_file=is_file,
                    empty_or_dir=True,
                ),
            )
        return DoDPredicateResult(
            predicate=predicate,
            passed=False,
            reason=f"file does not exist: {predicate.path} (resolved {resolved})",
            details=_file_exists_details(predicate, resolved, exists=False),
        )

    async def _check_command_exit(self, predicate: CommandExitPredicate) -> DoDPredicateResult:
        """Re-run the predicate's command in a fresh subprocess. A hard-denied
        command is "not run", not "run and failed"."""
        result = await self._command_runner(predicate.cmd)
        passed = (
            not result.denied
            and not result.timed_out
            and result.exit_code is not None
            and result.exit_code == predicate.expect_exit
        )
        if passed:
            return DoDPredicateResult(
                predicate=predicate,
                passed=True,
                reason=f"command exited {result.exit_code} as expected",
                details={
                    "kind": "command",
                    "cmd": predicate.cmd,
                    "expect_exit": predicate.expect_exit,
                    "exit_code": result.exit_code,
                    "duration_seconds": result.duration_seconds,
                },
            )
        return DoDPredicateResult(
            predicate=predicate,
            passed=False,
            unverifiable=bool(
                result.denied
                or result.timed_out
                or result.error_message
                or result.exit_code is None
            ),
            reason=_command_reason(result, predicate.expect_exit),
            details=_command_details(predicate, result),
        )

    async def _check_http_ok(self, predicate: HTTPOkPredicate) -> DoDPredicateResult:
        """Probe the URL and compare to `expect_status`. An egress-denied URL
        is "not probed", not "probed and got a bad status"."""
        result = await self._http_probe(predicate.url, predicate.expect_status)
        passed = (
            not result.egress_denied
            and result.status_code is not None
            and result.status_code == predicate.expect_status
        )
        if passed:
            return DoDPredicateResult(
                predicate=predicate,
                passed=True,
                reason=f"HTTP {result.status_code} as expected",
                details={
                    "kind": "http_ok",
                    "url": predicate.url,
                    "expect_status": predicate.expect_status,
                    "status_code": result.status_code,
                    "duration_seconds": result.duration_seconds,
                },
            )
        return DoDPredicateResult(
            predicate=predicate,
            passed=False,
            unverifiable=bool(
                result.egress_denied or result.error_message or result.status_code is None
            ),
            reason=_http_reason(result, predicate.expect_status),
            details=_http_details(predicate, result),
        )

    # ---- subjective-judge seam (future) -----------------------------------

    async def _check_subjective(self, predicate: DoDPredicate, spec: DoDSpec) -> DoDPredicateResult:
        """Delegate to the subjective judge if wired, else HARD-FAIL.

        We never silently pass a check we did not run — the same discipline
        C1a exists to fix, one layer down.
        """
        if self._subjective_judge is None:
            return DoDPredicateResult(
                predicate=predicate,
                passed=False,
                reason=(
                    f"no subjective judge wired for predicate kind "
                    f"{type(predicate).__name__!r}; refusing to silently pass"
                ),
                details={
                    "kind": "subjective_unwired",
                    "predicate_type": type(predicate).__name__,
                },
            )
        req = SubjectiveJudgeRequest(
            predicate=predicate,
            spec_predicates=list(spec.predicates),
            workspace_root=str(self._workspace_root),
        )
        try:
            v = await self._subjective_judge.judge(req)
        except Exception as exc:
            return DoDPredicateResult(
                predicate=predicate,
                passed=False,
                reason=f"subjective judge raised: {type(exc).__name__}: {exc}",
                details={
                    "kind": "subjective_judge_error",
                    "predicate_type": type(predicate).__name__,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        return DoDPredicateResult(
            predicate=predicate,
            passed=bool(v.passed),
            reason=v.reason,
            details={"kind": "subjective", **(v.details or {})},
        )


# ---- path resolution + fingerprinting helpers -----------------------------


class PathEscapeError(ValueError):
    """A predicate path resolved outside the workspace root."""

    def __init__(self, message: str, *, resolved: Path, root: Path) -> None:
        super().__init__(message)
        self.resolved = resolved
        self.root = root


def resolve_under_workspace(root: Path, raw: str) -> Path:
    """Resolve ``raw`` against ``root`` and assert it stays under ``root``.

    Symlinks are followed by ``Path.resolve``; a target outside ``root`` is
    a path-escape and is rejected.
    """
    if not raw:
        raise PathEscapeError("empty path", resolved=Path("."), root=root)
    candidate = (root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PathEscapeError(
            f"path {raw!r} resolves to {candidate}, which is outside workspace {root}",
            resolved=candidate,
            root=root,
        ) from exc
    return candidate


def spec_fingerprint(spec: DoDSpec) -> str:
    """SHA-256 over the spec's JSON form (predicates + meta). Stable across
    processes and serialization paths. The C1c finish gate will log this
    alongside the finish attempt so the audit pairs "agent tried to finish"
    with "evaluator was asked to check this exact spec"."""
    payload = spec.to_json_dict()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


# ---- an LLM-backed subjective judge (production default; tests use a fake)


class LLMSubjectiveJudge:
    """The production ``SubjectiveJudge`` — a fresh-context LLM call.

    Built on the live loop's ``LLMRouter``.  The judge sends a tiny prompt
    asking PASS or FAIL with one reason; tests substitute a fake router.
    """

    def __init__(self, router: Any, *, profile: Any) -> None:
        # The router seam (`LLMRouter` Protocol) is used instead of pinning
        # a concrete class so the test can inject a fake. `profile` is a
        # `CapabilityProfile` from `disco.core.llm.types` — typed as Any to
        # avoid a circular import (llm.types imports events, dod_evaluator
        # is a sibling).
        self._router = router
        self._profile = profile

    async def judge(self, req: SubjectiveJudgeRequest) -> SubjectiveVerdict:
        # Fresh-context prompt: predicate + spec + workspace path, NEVER the agent's transcript.
        prompt = (
            "You are an independent judge of one acceptance predicate. "
            "Read the predicate, the spec's full predicate list, and (if "
            "useful) the files under the workspace root. Do NOT assume the "
            "agent's working memory is correct; re-derive from evidence. "
            "Answer with PASS or FAIL on the first line, then one short "
            "reason on the second line.\n\n"
            f"Predicate: {req.predicate!r}\n"
            f"Spec predicates: {req.spec_predicates!r}\n"
            f"Workspace root: {req.workspace_root}\n"
        )
        completion_req = self._build_completion_request(prompt, profile=self._profile)
        resp = await self._router.complete(completion_req)
        return self._parse_response(resp.text)

    def _build_completion_request(self, prompt: str, *, profile: Any) -> Any:
        from .llm.types import CompletionRequest, LLMMessage

        messages = [LLMMessage(role="user", content=prompt)]
        return CompletionRequest(profile=profile, messages=messages, response_format="text")

    @staticmethod
    def _parse_response(text: str) -> SubjectiveVerdict:
        """Tiny parser: PASS/FAIL on the first line, reason is the remainder."""
        first, _, rest = (text or "").strip().partition("\n")
        passed = first.strip().upper().startswith("PASS")
        return SubjectiveVerdict(
            passed=passed,
            reason=rest.strip() or first.strip(),
            details={"raw": text or ""},
        )


# Re-export for convenience (mirrors how the C1a `dod.py` symbols are
# re-exported at the `disco.core` package level).
__all__ = [
    "CommandResult",
    "DoDEvaluator",
    "DoDPredicateResult",
    "DoDVerdict",
    "HttpProbeResult",
    "LLMSubjectiveJudge",
    "PathEscapeError",
    "SubjectiveJudge",
    "SubjectiveJudgeRequest",
    "SubjectiveVerdict",
    "resolve_under_workspace",
    "spec_fingerprint",
    "tail",
]
