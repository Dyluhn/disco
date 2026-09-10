"""Sandbox-backed `DoDEvaluator` construction for the finish DoD gate.

Two paths exist upstream (see `_ContentGateMixin.build_dod_evaluator`):
  * a test-seam factory (kept on the mixin — it reaches into `self._loop`
    directly and has nothing to do with sandbox adapter construction).
  * production: the sandbox's own file/command/HTTP surfaces. That path has
    no dependency on `self` at all, so it lives here as a plain function of
    the executor.

W3 C-2/C-4: a DoD `command` predicate is model-authored (copied from a plan
step's `done_condition`). It must NEVER run as a host-side
`subprocess(shell=True)` — routing it through `exec_shell` keeps it inside
the sandbox on a container backend, and behind the hard-deny floor otherwise.
Mirrored for the `http_ok` probe: a build's dev server binds the SANDBOX's
localhost, not the host's, so the probe must run via `exec_shell` + curl too.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from ....dod import FileExistsPredicate
from ....dod_evaluator import (
    CommandResult,
    CommandRunner,
    DoDEvaluator,
    DoDPredicateResult,
    FileChecker,
    HttpProbe,
    HttpProbeResult,
)
from ....dod_util import _hard_deny_reason
from ..common import _DoDWorkspaceUnavailable


async def build_sandbox_dod_evaluator(executor: Any) -> DoDEvaluator:
    """Build a `DoDEvaluator` graded against the executor's sandbox evidence.

    Raises `_DoDWorkspaceUnavailable` when the executor exposes neither a
    host `workspace_path` nor the sandbox evidence API triad — the caller
    degrades that to a logged, fail-closed pause.
    """

    sbx = getattr(executor, "sandbox", None)
    workspace = getattr(sbx, "workspace_path", None) if sbx is not None else None
    sandbox_evidence = sbx is not None and all(
        hasattr(sbx, method) for method in ("file_exists", "exec_shell", "resolve_relpath")
    )
    if not workspace and not sandbox_evidence:
        raise _DoDWorkspaceUnavailable(
            f"no sandbox evidence API on executor {type(executor).__name__}"
        )
    return DoDEvaluator(
        Path(workspace) if workspace else Path("."),
        command_runner=_build_command_runner(sbx),
        http_probe=_build_http_probe(sbx),
        file_checker=_build_file_checker(sbx),
    )


def _build_file_checker(sbx: Any) -> FileChecker | None:
    if not (
        sbx is not None
        and hasattr(sbx, "file_exists")
        and hasattr(sbx, "exec_shell")
        and hasattr(sbx, "resolve_relpath")
    ):
        return None

    async def _in_sandbox_file_checker(predicate: FileExistsPredicate) -> DoDPredicateResult:
        try:
            exists = await sbx.file_exists(predicate.path)
        except Exception as exc:  # noqa: BLE001 — fail closed with evidence
            return DoDPredicateResult(
                predicate=predicate,
                passed=False,
                reason=f"sandbox file check failed: {type(exc).__name__}: {exc}",
                unverifiable=True,
                details={"kind": "file_exists", "path": predicate.path},
            )
        if not exists:
            return DoDPredicateResult(
                predicate=predicate,
                passed=False,
                reason=f"file does not exist as a regular workspace file: {predicate.path}",
                details={"kind": "file_exists", "path": predicate.path},
            )
        try:
            relpath = await sbx.resolve_relpath(predicate.path)
            res = await sbx.exec_shell(
                shlex.join(["sh", "-c", 'test -s "$1"', "disco", relpath]),
                timeout_s=5,
            )
        except Exception as exc:  # noqa: BLE001 — fail closed with evidence
            return DoDPredicateResult(
                predicate=predicate,
                passed=False,
                reason=f"sandbox non-empty check failed: {type(exc).__name__}: {exc}",
                unverifiable=True,
                details={"kind": "file_exists", "path": predicate.path},
            )
        passed = int(res.exit_code) == 0
        return DoDPredicateResult(
            predicate=predicate,
            passed=passed,
            reason=(
                f"regular non-empty workspace file exists: {predicate.path}"
                if passed
                else f"workspace file is empty: {predicate.path}"
            ),
            details={
                "kind": "file_exists",
                "path": predicate.path,
                "exit_code": int(res.exit_code),
            },
        )

    return _in_sandbox_file_checker


def _build_http_probe(sbx: Any) -> HttpProbe | None:
    if sbx is None or not hasattr(sbx, "exec_shell"):
        return None

    async def _in_sandbox_http_probe(url: str, expected_status: int) -> HttpProbeResult:
        cmd = "curl -s -o /dev/null -w '%{http_code}' --max-time 10 " + shlex.quote(url)
        try:
            res = await sbx.exec_shell(cmd, timeout_s=15)
        except Exception as exc:  # noqa: BLE001 — a probe failure is "not probed", never a crash
            return HttpProbeResult(
                status_code=None,
                error_message=f"sandbox http probe failed: {exc}",
            )
        out = (res.stdout or "").strip()
        if not out.isdigit() or out == "000":
            return HttpProbeResult(
                status_code=None,
                error_message=(
                    f"sandbox curl produced no HTTP status (got {out!r}, exit {res.exit_code})"
                ),
            )
        return HttpProbeResult(status_code=int(out))

    return _in_sandbox_http_probe


def _build_command_runner(sbx: Any) -> CommandRunner | None:
    # `None` is ESSENTIAL: `DoDEvaluator` has no default command runner, so a
    # sandbox-less executor makes every `command` predicate un-evaluatable
    # (non-pass, `unverifiable`) instead of running the model's command string on
    # the agent-server host. Never substitute a host runner here.
    if sbx is None or not hasattr(sbx, "exec_shell"):
        return None

    async def _in_sandbox_command_runner(command: str) -> CommandResult:
        deny = _hard_deny_reason(command)
        if deny is not None:
            return CommandResult(
                exit_code=None,
                error_message=f"hard-denied: {deny}",
                denied=True,
                deny_reason=deny,
            )
        try:
            res = await sbx.exec_shell(command, timeout_s=30)
        except Exception as exc:  # noqa: BLE001 — a check failure is "not run", never a crash
            return CommandResult(
                exit_code=None,
                error_message=f"sandbox exec error: {type(exc).__name__}: {exc}",
            )
        if getattr(res, "timed_out", False):
            return CommandResult(
                exit_code=None,
                stdout=res.stdout or "",
                stderr=res.stderr or "",
                error_message="timeout after 30s in sandbox",
                timed_out=True,
            )
        return CommandResult(
            exit_code=int(res.exit_code),
            stdout=res.stdout or "",
            stderr=res.stderr or "",
        )

    return _in_sandbox_command_runner
