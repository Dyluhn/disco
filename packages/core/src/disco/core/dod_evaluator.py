"""Definition-of-Done (DoD) evaluator — a fresh-context, read-only judge.

# Why this module exists

C1a stored the agent's acceptance criteria (the DoD spec) OUTSIDE the
agent-editable event stream, write-once, so the model cannot rewrite what it
is being judged against. C1b is the JUDGE. The whole point of separating
storage from evaluation is so the agent's self-justifying working transcript
cannot grade itself: the evaluator runs in a SEPARATE, FRESH context. It sees
the spec and the deliverable evidence (the workspace), and nothing else.

Concretely, the evaluator receives ONLY:

  * the `DoDSpec` (the predicates, in their frozen form),
  * a workspace root (so it can resolve `file_exists` paths and run command
    predicates against the same files the agent worked on),

and it does NOT receive:

  * the conversation's event log (the agent's transcript),
  * the agent's `View` / state (the agent's working memory),
  * the agent's tool registry (the agent's action surface),
  * any LLM "self-justify" string the agent may have produced.

To grade a predicate the evaluator re-runs the predicate's own check from
scratch (`os.path.exists`, a fresh `subprocess.run`, a fresh `urllib` GET).
The verdict records WHAT was checked and the per-predicate result, so the
audit trail can answer "did the judge actually look?" without trusting the
agent's prose.

# Deterministic vs LLM split

The three predicate kinds C1a ships are all DETERMINISTIC (machine-checkable,
the agent cannot fake the answer with prose). The evaluator runs each
deterministic kind directly:

    file_exists   → `Path.exists()` (read-only)
    command       → `subprocess.run(cmd, cwd=workspace_root, timeout=…)`
                    (re-runs the agent's own acceptance check; gated through
                    the engine's destructive-command deny-list, same as the
                    agent's own verify-on-finish probe)
    http_ok       → `urllib.request.urlopen(url, timeout=…)`
                    (loopback / private-RFC1918 by default; same egress
                    discipline the live loop uses for its finish-probe)

A future, subjective predicate kind (e.g. "is this design accessible?")
would need an LLM judgment. The seam is here — `SubjectiveJudge` — but
C1b does NOT add a new predicate kind. The default `DoDEvaluator` accepts
an optional `subjective_judge` callable; if a subjective predicate is
encountered and no judge is wired, the predicate is a HARD-FAIL (we never
silently pass a check we did not run). The unit test uses a fake judge
rather than a live LLM.

# Read-only contract

The evaluator is read-only with respect to the agent's state:

  1. It does NOT import or call any of `disco.tools.*` (the agent's tool
     surface). The test for that is in `test_dod_evaluator.py`
     (`test_evaluator_does_not_import_agent_tool_surface`).
  2. It does NOT touch the conversation's `EventStore` (its signature does
     not accept a store at all — the spec is the only state it needs).
  3. Its command runner runs in a subprocess with `cwd=workspace_root` and
     a timeout. It does NOT write to the agent's view or emit any events.
  4. Its HTTP probe is a read-only GET (no POST, no cookie persistence,
     loopback by default).

# Determinism of the verdict

The verdict's `spec_fingerprint` is a SHA-256 over the spec's JSON form. The
audit trail can answer "what was the evaluator actually asked to check?"
without re-deriving the spec — the fingerprint is the same string for the
same predicates and the same meta. A C1c caller (the engine's finish gate)
will log this fingerprint alongside the finish attempt so the audit can pair
"the agent tried to finish" with "the evaluator checked this".

# Open source prior-art (per the task brief)

The "fresh context" idea is the OpenHands `_check_iterative_refinement` /
`critic_mixin` discipline: an external critic with its own context judges the
deliverable against the spec, and the worker is never allowed to grade
itself. C1b is the minimal form of that — a fresh-context judge, deterministic
where possible, with the LLM seam wired in for the future subjective kind.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.request
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

# ---- result types ----------------------------------------------------------


class CommandResult(BaseModel):
    """The outcome of running a `command` predicate's check.

    `exit_code` is None when the subprocess did not produce one (e.g. the
    runner was denied, the subprocess was hard-denied by the destructive-
    command gate, or it timed out). `error_message` carries the human reason
    — "hard-denied: rm -rf", "timeout after 30s", "executor raised
    FileNotFoundError: '/bin/nosuch'". The verdict turns a non-passing
    `CommandResult` into a `DoDPredicateResult(passed=False, ...)` with the
    reason in `details.error_message`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error_message: str = ""  # empty == no executor-level error
    duration_seconds: float = 0.0
    # True when the destructive-command deny-list refused to run the command.
    # Distinct from a non-zero exit: a denied command is "not run", not
    # "run and failed". The verdict names the predicate unmet with the deny
    # reason; the audit can distinguish "the check failed" from "the check
    # was unsafe to run".
    denied: bool = False
    deny_reason: str = ""


class HttpProbeResult(BaseModel):
    """The outcome of an `http_ok` predicate's check.

    `status_code` is None when the probe did not reach the wire (DNS failure,
    connection refused, timeout, URL rejected by the egress allow-list). The
    verdict turns a non-passing result into a `DoDPredicateResult(passed=False,
    ...)` with `error_message` carrying the reason.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status_code: int | None = None
    error_message: str = ""
    duration_seconds: float = 0.0
    # True when the URL was rejected by the egress allow-list (private IP
    # when not on the explicit list, scheme not http/https, etc.). Same
    # discipline as `CommandResult.denied`.
    egress_denied: bool = False
    egress_reason: str = ""


class DoDPredicateResult(BaseModel):
    """The per-predicate outcome. `predicate` is the SPECIFIC `DoDPredicate`
    instance the verdict is about — frozen, Pydantic-equal to the source
    spec, and identical-by-`is` only when Pydantic preserves the reference
    (it doesn't always). The consumer matches on the `predicate` field to
    name the unmet item."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    predicate: DoDPredicate
    passed: bool
    reason: str
    # Kind-specific structured evidence (exit_code, status_code, file size,
    # observed predicate field, ...). The verdict carries the FULL record so
    # a human reviewer can re-derive the judgment without re-running.
    details: dict[str, Any] = Field(default_factory=dict)


class DoDVerdict(BaseModel):
    """The evaluator's structured judgment.

    `passed` is the AND of all per-predicate results — ALL predicates must
    pass for the spec to be satisfied. Partial pass is failure (the
    "predicates are the bar, not a checklist of nice-to-haves" discipline).

    `unmet` is the LIST of the specific `DoDPredicate` instances that did not
    pass. The C1c wire step (engine finish gate) iterates `unmet` to name
    the refusal reason to the agent — "the following acceptance checks did
    not pass: …". The test (`test_dod_evaluator.py`) asserts `unmet` names
    the right predicate.

    `results` is the FULL per-predicate record (passed + failed). The audit
    trail reads `results` so the "what was checked" answer is self-contained.

    `spec_fingerprint` is a SHA-256 over the spec's JSON form (predicates +
    meta). Stable across processes; pairs "finish attempt #N" with "evaluator
    was asked to check this exact spec" in the audit log.
    """

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
    """A future subjective predicate's judgment. NOT in scope for C1a (the
    three current kinds are deterministic), but the seam is here so a new
    kind can be added without rewriting the evaluator. The default judge
    is an LLM call; tests use a fake."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    passed: bool
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)


class SubjectiveJudgeRequest(BaseModel):
    """What a future subjective judge would receive. The predicate itself
    (in its frozen form), plus a tiny evidence packet: the spec's predicates
    and a flag for the workspace root so the judge can read evidence files
    if needed. NEVER the agent's transcript."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    predicate: DoDPredicate
    spec_predicates: list[DoDPredicate]
    workspace_root: str
    evidence_excerpt: str = ""


@runtime_checkable
class SubjectiveJudge(Protocol):
    """The LLM-judge seam. A fresh-context judgment is the whole point — the
    judge sees ONLY the spec + the deliverable evidence, never the agent's
    working memory. The default implementation uses the live loop's router
    (separate from the agent's); tests inject a fake."""

    async def judge(self, req: SubjectiveJudgeRequest) -> SubjectiveVerdict: ...


# ---- default command runner (subprocess, with destructive-command gate) ---


_DEFAULT_COMMAND_TIMEOUT_SECONDS = 30.0
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


async def _default_command_runner(
    command: str,
    *,
    cwd: Path,
    timeout_seconds: float,
) -> CommandResult:
    """The default `command_runner`. Runs `command` in a fresh subprocess
    with `cwd=cwd` and a hard timeout. The subprocess is captured (stdout +
    stderr); we never tee the output to the agent's trace — the evaluator's
    verdict is the only output the agent sees.

    A denied command is recorded as `CommandResult(denied=True,
    deny_reason=…)` with `exit_code=None` — the predicate is "not run", not
    "run and failed". This distinguishes a hard-deny from a real non-zero
    exit in the audit trail."""
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

    def _run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            # Inherit a clean env (no leaked agent secrets). The agent
            # cannot influence the env through the spec — the spec is
            # write-once and captured at task start.
            env={"PATH": os.environ.get("PATH", "")},
        )

    try:
        completed = await asyncio.to_thread(_run)
    except subprocess.TimeoutExpired as exc:
        duration = datetime.now(UTC).timestamp() - started
        return CommandResult(
            exit_code=None,
            stdout=(
                (exc.stdout or b"").decode("utf-8", errors="replace")
                if isinstance(exc.stdout, (bytes, bytearray))
                else (exc.stdout or "")
            ),
            stderr=(
                (exc.stderr or b"").decode("utf-8", errors="replace")
                if isinstance(exc.stderr, (bytes, bytearray))
                else (exc.stderr or "")
            ),
            error_message=f"timeout after {timeout_seconds}s",
            duration_seconds=duration,
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
        exit_code=int(completed.returncode),
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        error_message="",
        duration_seconds=duration,
    )


# ---- default http probe ----------------------------------------------------


async def _default_http_probe(
    url: str,
    *,
    expected_status: int,
    timeout_seconds: float,
    allow_hosts: frozenset[str],
) -> HttpProbeResult:
    """The default HTTP probe. GET only (read-only by construction). The
    egress gate is checked BEFORE the wire call, so a denied URL never
    touches the network.

    The probe DOES NOT use `requests` (no third-party dep) — `urllib.request`
    is stdlib, matches the engine's own `_app_verify_command`."""
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

    def _do() -> tuple[int | None, str]:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            return int(resp.status), ""

    try:
        status, err = await asyncio.to_thread(_do)
    except urllib.error.HTTPError as exc:
        # HTTP 4xx/5xx is a real response — record the status (it's the
        # observed code, which may differ from `expected_status`). The
        # verdict's pass/fail is "status == expected_status".
        duration = datetime.now(UTC).timestamp() - started
        return HttpProbeResult(
            status_code=int(exc.code),
            error_message="",
            duration_seconds=duration,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        duration = datetime.now(UTC).timestamp() - started
        return HttpProbeResult(
            status_code=None,
            error_message=f"{type(exc).__name__}: {exc}",
            duration_seconds=duration,
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


class DoDEvaluator:
    """The fresh-context, read-only judge for a `DoDSpec`.

    Construction is dependency-injected:

      * `command_runner(cmd: str) -> CommandResult`: how a `command`
        predicate is run. Default: `subprocess.run` with a timeout, gated
        through the engine's destructive-command deny-list.
      * `http_probe(url: str, expected_status: int) -> HttpProbeResult`:
        how an `http_ok` predicate is probed. Default: `urllib` GET with a
        loopback + RFC1918 egress allow-list.
      * `subjective_judge`: the future-LLM seam. None means "no subjective
        judgment wired" — a subjective predicate encountered under that
        condition is a HARD-FAIL (we never silently pass a check we did
        not run).

    The evaluator holds a `workspace_root: Path` and uses it to resolve
    `file_exists` paths and as `cwd` for `command` predicates. Path-escape
    attempts (a `file_exists` path that resolves outside `workspace_root`)
    are a hard fail — same discipline the sandbox uses for the agent's
    file tools.
    """

    def __init__(
        self,
        workspace_root: Path | str,
        *,
        command_runner: CommandRunner | None = None,
        http_probe: HttpProbe | None = None,
        subjective_judge: SubjectiveJudge | None = None,
        command_timeout_seconds: float = _DEFAULT_COMMAND_TIMEOUT_SECONDS,
        http_timeout_seconds: float = _DEFAULT_HTTP_TIMEOUT_SECONDS,
        http_allow_hosts: frozenset[str] = _DEFAULT_HTTP_ALLOW_HOSTS,
    ) -> None:
        self._workspace_root = Path(workspace_root).resolve()
        if not self._workspace_root.exists():
            # The evaluator's whole job is to grade evidence in a workspace;
            # a missing workspace is a misconfiguration, not a predicate
            # failure. We construct anyway — every `file_exists`/`command`
            # check will then fail closed (the path doesn't resolve, the
            # cwd is gone), and the verdict's `unmet` will name them. The
            # C1c caller is expected to provide a real workspace path.
            pass
        # Set scalar config FIRST so the builder closures can read them.
        self._subjective_judge = subjective_judge
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
            return await _default_command_runner(
                command, cwd=cwd, timeout_seconds=timeout
            )

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

        Predicates are evaluated in spec order. ALL must pass; partial pass
        is failure. The verdict's `unmet` lists the specific `DoDPredicate`
        instances that did not pass — the C1c finish gate iterates `unmet`
        to name the refusal reason to the agent.

        The evaluator is `async` so the LLM-judge seam (when wired) can be
        async; deterministic checks are themselves async (the subprocess /
        HTTP calls are wrapped in `asyncio.to_thread`). The function never
        raises — every failure mode is recorded as a `DoDPredicateResult`.
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

    async def _evaluate_one(
        self, predicate: DoDPredicate, spec: DoDSpec
    ) -> DoDPredicateResult:
        # Discriminate by concrete type — the Pydantic `kind` discriminator
        # is the canonical field, but `isinstance` is the cheapest dispatch
        # and the union is closed (only three kinds as of C1a).
        if isinstance(predicate, FileExistsPredicate):
            return self._check_file_exists(predicate)
        if isinstance(predicate, CommandExitPredicate):
            return await self._check_command_exit(predicate)
        if isinstance(predicate, HTTPOkPredicate):
            return await self._check_http_ok(predicate)
        # Future subjective kinds land here. The seam exists, but C1a has
        # none. A `subjective_judge is None` case is a HARD-FAIL.
        return await self._check_subjective(predicate, spec)

    # ---- deterministic checks ---------------------------------------------

    def _check_file_exists(self, predicate: FileExistsPredicate) -> DoDPredicateResult:
        """Resolve the predicate's path against the workspace root and check
        existence. A path-escape (resolved path outside `workspace_root`) is
        a hard fail — same discipline the sandbox uses for the agent's file
        tools, and important: a spec that says `file_exists path: ../../etc/
        passwd` would otherwise let the agent pass by *seeing* (or claiming
        to see) an arbitrary file's existence. The evaluator refuses to
        grade it; the verdict names the predicate unmet with the escape
        reason."""
        try:
            resolved = resolve_under_workspace(
                self._workspace_root, predicate.path
            )
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
            return DoDPredicateResult(
                predicate=predicate,
                passed=True,
                reason=f"file exists at {resolved}",
                details={
                    "kind": "file_exists",
                    "path": predicate.path,
                    "resolved": str(resolved),
                    "size_bytes": stat.st_size,
                },
            )
        return DoDPredicateResult(
            predicate=predicate,
            passed=False,
            reason=f"file does not exist: {predicate.path} (resolved {resolved})",
            details={
                "kind": "file_exists",
                "path": predicate.path,
                "resolved": str(resolved),
                "exists": False,
            },
        )

    async def _check_command_exit(
        self, predicate: CommandExitPredicate
    ) -> DoDPredicateResult:
        """Re-run the predicate's own command in a fresh subprocess. The
        verdict turns the `CommandResult` into a `DoDPredicateResult` based
        on the predicate's `expect_exit` (default 0). A hard-denied command
        is "not run", not "run and failed" — the predicate is unmet with the
        deny reason in `details.deny_reason`."""
        result = await self._command_runner(predicate.cmd)
        passed = (
            not result.denied
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
        # Build a precise reason. Three cases:
        #   1. denied → "not run: hard-denied (…)"
        #   2. executor error → "not run: …"
        #   3. wrong exit → "exited N, expected M"
        if result.denied:
            reason = f"command not run: hard-denied ({result.deny_reason})"
        elif result.error_message:
            reason = f"command not run: {result.error_message}"
        else:
            reason = (
                f"command exited {result.exit_code}, expected {predicate.expect_exit}"
            )
        return DoDPredicateResult(
            predicate=predicate,
            passed=False,
            reason=reason,
            details={
                "kind": "command",
                "cmd": predicate.cmd,
                "expect_exit": predicate.expect_exit,
                "exit_code": result.exit_code,
                "stdout_tail": tail(result.stdout, 2000),
                "stderr_tail": tail(result.stderr, 2000),
                "error_message": result.error_message,
                "denied": result.denied,
                "deny_reason": result.deny_reason,
                "duration_seconds": result.duration_seconds,
            },
        )

    async def _check_http_ok(
        self, predicate: HTTPOkPredicate
    ) -> DoDPredicateResult:
        """Probe the URL and compare to `expect_status` (default 200). An
        egress-denied URL is "not probed", not "probed and got a bad
        status" — distinct in the audit trail."""
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
        if result.egress_denied:
            reason = f"probe not run: egress denied ({result.egress_reason})"
        elif result.error_message:
            reason = f"probe not run: {result.error_message}"
        else:
            reason = (
                f"HTTP {result.status_code}, expected {predicate.expect_status}"
            )
        return DoDPredicateResult(
            predicate=predicate,
            passed=False,
            reason=reason,
            details={
                "kind": "http_ok",
                "url": predicate.url,
                "expect_status": predicate.expect_status,
                "status_code": result.status_code,
                "error_message": result.error_message,
                "egress_denied": result.egress_denied,
                "egress_reason": result.egress_reason,
                "duration_seconds": result.duration_seconds,
            },
        )

    # ---- subjective-judge seam (future) -----------------------------------

    async def _check_subjective(
        self, predicate: DoDPredicate, spec: DoDSpec
    ) -> DoDPredicateResult:
        """A predicate kind the evaluator does not handle directly. If a
        `subjective_judge` is wired, delegate to it (fresh context: the
        judge gets the spec + the workspace path, NOT the agent's
        transcript). If not wired, this is a HARD-FAIL: we never silently
        pass a check we did not run.

        The hard-fail default is the "no silent pass" discipline. A future
        subjective kind (e.g. design quality) without a judge would let
        the agent pass a check that was never run — the same failure mode
        C1a exists to fix, just one layer down."""
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
    """A predicate's path resolved to a location outside the workspace root.
    The evaluator refuses to grade such predicates — the verdict names the
    predicate unmet with the escape reason, and the audit trail can see
    "this spec tried to probe outside the workspace"."""

    def __init__(self, message: str, *, resolved: Path, root: Path) -> None:
        super().__init__(message)
        self.resolved = resolved
        self.root = root


def resolve_under_workspace(root: Path, raw: str) -> Path:
    """Resolve `raw` against `root` and assert the resolved path is under
    `root`. Absolute paths are allowed (a spec can probe `/tmp/done.flag`)
    only if they are themselves under `root`; this prevents a spec from
    probing arbitrary system locations.

    Symlinks are NOT followed here — `Path.resolve` follows them in
    Python 3.6+. If the resolved symlink target is outside `root`, it's a
    path-escape and is rejected. The agent cannot use a workspace-relative
    symlink to escape the evaluator's reach."""
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
    """The production `SubjectiveJudge` — a fresh-context LLM call. Built
    on top of the live loop's `LLMRouter` (the same seam the agent uses
    for its own completions). The judge sends a tiny prompt: "is this
    predicate satisfied? answer PASS or FAIL with one reason". The router
    resolves the role; the test substitutes a fake router so no live LLM
    is ever called in unit tests."""

    def __init__(self, router: Any, *, profile: Any) -> None:
        # The router seam (`LLMRouter` Protocol) is used instead of pinning
        # a concrete class so the test can inject a fake. `profile` is a
        # `CapabilityProfile` from `disco.core.llm.types` — typed as Any to
        # avoid a circular import (llm.types imports events, dod_evaluator
        # is a sibling).
        self._router = router
        self._profile = profile

    async def judge(self, req: SubjectiveJudgeRequest) -> SubjectiveVerdict:

        # Fresh-context prompt: just the predicate + the spec + the
        # workspace path. NEVER the agent's transcript, the view, or the
        # event log. The judge's output is the verdict — nothing else.
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

        # The profile comes from the caller (typically `CapabilityProfile(
        # role=ModelRole.NLI_VERIFIER, …)`). We re-construct a
        # `CompletionRequest` from it so the test can vary role/cost.
        messages = [LLMMessage(role="user", content=prompt)]
        return CompletionRequest(profile=profile, messages=messages, response_format="text")

    @staticmethod
    def _parse_response(text: str) -> SubjectiveVerdict:
        """The judge is asked to answer PASS/FAIL on the first line. The
        reason is the remainder. This is intentionally a tiny parser —
        the judge's prompt sets the contract; we don't need a full schema
        to extract the verdict."""
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
