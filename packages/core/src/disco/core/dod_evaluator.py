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
agent view mutation.

The evaluator NEVER touches the host itself.  Every predicate is model-authored
(a command string, a path, a URL), so all three checks run only through seams
the caller injects — in production the sandbox's own ``exec_shell`` /
``file_exists`` / in-box curl (see
``loop/finish/content_gate_parts/dod_evaluator_build.py``).  With a seam
missing, that predicate is UN-EVALUATABLE: a non-pass flagged ``unverifiable``,
never a host ``subprocess(shell=True)``, a host filesystem read, or a host GET.

The verdict's ``spec_fingerprint`` is a SHA-256 over the spec's JSON form so
the audit trail can pair "agent tried to finish" with "evaluator checked this
exact spec" without re-deriving it.
"""

from __future__ import annotations

import hashlib
import json
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
from .dod_util import tail

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


# ---- check budgets --------------------------------------------------------


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


# ---- injected seams -------------------------------------------------------
#
# Every seam is Optional at construction and has NO host-side default: a missing
# seam makes that predicate un-evaluatable (see the module docstring).
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

    Construction is dependency-injected: ``http_probe``, ``file_checker`` and
    ``subjective_judge`` seams default to safe production implementations
    (loopback GET, ``Path.exists``, and HARD-FAIL respectively).
    ``command_runner`` has NO default — with none injected, a ``command``
    predicate is un-evaluatable (non-pass, ``unverifiable``) rather than a host
    ``subprocess(shell=True)`` of a model-authored string.  The evaluator holds
    a ``workspace_root`` for resolving ``file_exists`` paths; path-escape
    attempts are a hard fail.
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
        # above). If the caller injected an `http_probe`, we use that and skip
        # the default. `command_runner` deliberately has NO default: running a
        # model-authored command is the injected caller's decision (production
        # injects the sandbox's `exec_shell`), never this module's.
        self._command_runner: CommandRunner | None = command_runner
        # `http_probe` has NO default either: the deliverable's server binds the
        # SANDBOX's localhost, not the agent-server's, so a host GET on a
        # model-authored URL both grades the wrong machine and reaches out of the
        # process. Production injects the sandbox's curl probe.
        self._http_probe: HttpProbe | None = http_probe

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
        """Grade the predicate with the INJECTED file checker (in production, the
        sandbox's own `file_exists` + non-empty test).

        There is no host-filesystem fallback: `predicate.path` is model-authored,
        and answering it off the agent-server's own filesystem grades the wrong
        machine (and discloses what is on it). With no checker injected the
        predicate is UN-EVALUATABLE — a non-pass flagged `unverifiable`."""
        if self._file_checker is None:
            return DoDPredicateResult(
                predicate=predicate,
                passed=False,
                unverifiable=True,
                reason=(
                    "file_exists not evaluated: no sandbox available to check it in "
                    "(host filesystem access is not permitted)"
                ),
                details={
                    "kind": "file_exists",
                    "path": predicate.path,
                    "no_file_checker": True,
                },
            )
        return await self._file_checker(predicate)

    async def _check_command_exit(self, predicate: CommandExitPredicate) -> DoDPredicateResult:
        """Re-run the predicate's command through the INJECTED runner (in
        production, the sandbox's `exec_shell`). A hard-denied command is "not
        run", not "run and failed"; no runner at all is "not evaluated"."""
        if self._command_runner is None:
            return DoDPredicateResult(
                predicate=predicate,
                passed=False,
                unverifiable=True,
                reason=(
                    "command not evaluated: no sandbox available to run it in "
                    "(host execution is not permitted)"
                ),
                details={
                    "kind": "command",
                    "cmd": predicate.cmd,
                    "expect_exit": predicate.expect_exit,
                    "no_command_runner": True,
                },
            )
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
        if self._http_probe is None:
            return DoDPredicateResult(
                predicate=predicate,
                passed=False,
                unverifiable=True,
                reason=(
                    "http_ok not evaluated: no sandbox available to probe from "
                    "(host network access is not permitted)"
                ),
                details={
                    "kind": "http_ok",
                    "url": predicate.url,
                    "expect_status": predicate.expect_status,
                    "no_http_probe": True,
                },
            )
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
