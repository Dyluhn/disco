"""C18 — advisory plan-step done-condition evaluation.

Extracted from engine.py as a stateful collaborator: `PlanStepConditions` holds
a back-ref to its `AgentLoop`, reads the loop's per-(revision, index) predicate
map, and emits advisory notes through the loop's primitives. Sibling calls stay
inside the collaborator; loop state/primitives go through `self._loop`. Bodies
byte-identical to the former AgentLoop methods.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from ..dod import (
    CommandExitPredicate,
    DoDPredicate,
    FileExistsPredicate,
    HTTPOkPredicate,
)
from ..events import (
    ActionEvent,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
)

if TYPE_CHECKING:
    from .engine import AgentLoop


class PlanStepConditions:
    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    async def maybe_emit_plan_step_done_condition_note(self, action: ActionEvent) -> None:
        """If `action` is a `plan_step(idx, 'done')` whose plan step has a
        stored `done_condition` predicate, evaluate it inline and emit a
        visible pass/fail note in the trace. ADVISORY ONLY:

          * never blocks the run;
          * never nudges the agent (no <system-reminder>, no auto-continue,
            no speak-back to the model);
          * never duplicates the C1c finish gate (the C18 check uses a
            lightweight inline evaluator; the C1c gate uses the heavy
            fresh-context DoDEvaluator and gates `finish` itself).

        Steps WITHOUT a predicate are a strict no-op (back-compat): no
        lookup, no note, no event. The check fires only on `state="done"`,
        never on `state="active"` (an active mark is the agent saying
        "I am starting" — there is no work to check yet)."""
        tc = action.tool_call
        if tc is None or tc.tool_name != "plan_step":
            return
        args = tc.arguments or {}
        state = str(args.get("state") or "")
        if state != "done":
            return
        # Find the latest plan + the step's (revision, index). The current
        # plan is whichever PlanEvent has the highest revision; a re-plan
        # supersedes, so we must use the LATEST (not just any) — the
        # `_plan_step_predicates` map is revision-scoped for exactly this
        # reason (a stale (revision, idx) must not match a fresh plan).
        try:
            # The value is dynamically typed (arguments: dict[str, Any]); a
            # missing/None/non-numeric index is handled by the except below.
            raw_index: Any = args.get("index")
            idx = int(raw_index)
        except (TypeError, ValueError):
            return
        events = await self._loop._events()
        latest_plan: PlanEvent | None = None
        for e in events:
            if isinstance(e, PlanEvent):
                if latest_plan is None or e.revision >= latest_plan.revision:
                    latest_plan = e
        if latest_plan is None:
            return  # no plan in scope — the plan_step is unanchored
        # 1-based step index. Out-of-range = nothing to look up.
        if idx < 1 or idx > len(latest_plan.steps):
            return
        predicate = self._loop._plan_step_predicates.get((latest_plan.revision, idx))
        if predicate is None:
            # Back-compat: a step with no predicate is the explicit design
            # target (most steps). No note, no extra event. Today was
            # silent here and stays silent.
            return
        passed, reason = await self.evaluate_plan_step_predicate(predicate)
        # The note is a <system-reminder>-less MessageEvent from
        # ENVIRONMENT with source=ADVISORY semantics: it is visible in
        # the trace for the human and the LLM sees it on its next turn
        # as a normal message (NOT a system-reminder, so it does NOT
        # nudge — the model is free to ignore or act on it as it sees
        # fit). The "advisory" framing in the prefix is what makes the
        # no-nudge contract explicit in the trace.
        verdict_word = "met" if passed else "NOT met"
        body = (
            f"[advisory, C18] done-condition for plan step {idx} "
            f"(\"{latest_plan.steps[idx - 1].title}\"): {verdict_word}. "
            f"{reason}"
        )
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=body),
                meta={"advisory": "plan_step_done_condition", "passed": passed},
            )
        )

    async def evaluate_plan_step_predicate(
        self, predicate: DoDPredicate
    ) -> tuple[bool, str]:
        """Lightweight inline evaluation of a single DoDPredicate for the
        C18 advisory note. The three kinds reuse the
        `disco.core.dod.DoDPredicate` discriminated union:

          * `file_exists(path)`: resolved against the executor's sandbox
            workspace root (so the predicate talks about agent-visible
            files, not harness-private paths), with a path-escape check
            mirroring the C1b evaluator's discipline (a `path` that
            resolves outside the workspace is a hard FAIL — the
            predicate is not silently passed by an out-of-scope match).
          * `command(cmd, expect_exit)`: a tight-timeout subprocess run
            against the workspace root. Deny-list failures and timeouts
            count as a non-pass with the reason surfaced.
          * `http_ok(url, expect_status)`: a short-timeout GET against
            the URL; a non-matching status is a non-pass.

        Returns `(passed, reason)`. The reason is a one-line human-
        readable summary the C18 note emits in the trace; on failure it
        names WHY the predicate did not pass (file missing, command
        exited N, http status 5xx, etc.) so the user can see the truth
        of the check, not just a boolean.

        Distinct from the C1c gate's `DoDEvaluator`: this is a single-
        predicate inline check that runs in the same context as the rest
        of the loop, not a fresh-context judge. The C1c gate is the
        authoritative DoD check at finish time; C18 is the
        per-step advisory trail."""
        if isinstance(predicate, FileExistsPredicate):
            return await self.check_file_exists_for_plan_step(predicate)
        if isinstance(predicate, CommandExitPredicate):
            return await self.check_command_for_plan_step(predicate)
        if isinstance(predicate, HTTPOkPredicate):
            return await self.check_http_for_plan_step(predicate)
        # Defensive: the union is closed (three kinds) and
        # `predicate_from_obj` rejects unknown kinds at submit_plan
        # time. A future kind would land here as a fail-loud non-pass
        # rather than a silent pass.
        return (False, f"unsupported predicate kind: {type(predicate).__name__}")

    async def check_file_exists_for_plan_step(
        self, predicate: FileExistsPredicate
    ) -> tuple[bool, str]:
        """Check whether `predicate.path` exists, resolved in the SANDBOX's own
        namespace (not the agent-server host cwd).

        B4 root-cause: the container sandbox backend reports `workspace_path is
        None` (the host has NO view of the box FS), yet the agent's files are
        real INSIDE the box. The old code fell through to a literal host
        `Path(path_str).is_file()` check, which looked in the agent-server cwd,
        found nothing, and emitted a FALSE "done-condition NOT met / file
        missing" advisory for files the agent had just written.

        The fix asks the sandbox itself (`await sbx.file_exists(...)`) via duck
        typing — core never imports `tools`; the `executor.sandbox` object is
        injected at runtime and we call a method on it. Each backend resolves in
        its own namespace (host FS for process, inside-the-box for container).

        The path-escape hard-FAIL is preserved whenever a real `workspace_path`
        is known (process/session backend), mirroring the C1b evaluator's
        discipline: a `path` resolving outside the workspace is a hard FAIL, not
        a coincidental pass. For the container backend (`workspace_path is None`)
        the jail is enforced by the backend's own `file_exists` (`_container_path`
        rejects escapes → False).

        The literal-`Path` branch survives ONLY for the sandbox-less /
        fake-executor path (no `file_exists` capability) the tests rely on."""
        from pathlib import Path
        sbx = getattr(self._loop.executor, "sandbox", None)
        workspace = getattr(sbx, "workspace_path", None) if sbx is not None else None
        path_str = predicate.path
        # Path-escape hard-FAIL when a real host-side workspace root is known.
        # (Container backends expose workspace_path=None and rely on their own
        # in-box jail; this branch is the process/session backend's discipline.)
        if workspace:
            try:
                root = Path(workspace).resolve()
                candidate = (root / path_str).resolve()
                root_str = str(root)
                candidate_str = str(candidate)
                # On Windows the resolved strings may differ in case;
                # `Path.resolve()` is case-aware but the prefix check is
                # not, so do a normalized comparison. The agent runs
                # inside a Linux sandbox, so the suffix match is the
                # load-bearing case in practice.
                if not (
                    candidate_str == root_str
                    or candidate_str.startswith(root_str.rstrip("/") + "/")
                ):
                    return (
                        False,
                        f"file_exists: path escapes workspace ({path_str})",
                    )
            except (OSError, ValueError) as exc:
                return (False, f"file_exists({path_str}): resolve error ({exc})")
        # B4 — sandbox-aware existence check. ASK THE SANDBOX (duck-typed; no
        # upward `tools` import) so the path resolves in the box's namespace.
        # This is the load-bearing fix for the container backend's false
        # "missing" advisory.
        if sbx is not None and hasattr(sbx, "file_exists"):
            try:
                exists = await sbx.file_exists(path_str)
            except Exception as exc:  # noqa: BLE001 — advisory; never wedge the loop
                return (False, f"file_exists({path_str}): check error ({exc})")
            if exists:
                return (True, f"file_exists({path_str}): found")
            return (False, f"file_exists({path_str}): missing")
        # No sandbox (or a sandbox without the file_exists capability): check the
        # literal path. This is the sandbox-less / fake-executor path used in
        # tests; the predicate names a literal path, we check the literal path.
        p = Path(path_str)
        if p.exists():
            return (True, f"file_exists({path_str}): found")
        return (False, f"file_exists({path_str}): missing")

    async def check_command_for_plan_step(
        self, predicate: CommandExitPredicate
    ) -> tuple[bool, str]:
        """Run `predicate.cmd` in a fresh subprocess against the workspace
        root and compare to `predicate.expect_exit` (default 0). Tight
        timeout to keep the loop responsive. Failures (deny, timeout,
        wrong exit) are surfaced with the reason in the note."""
        import asyncio
        import subprocess
        from pathlib import Path
        sbx = getattr(self._loop.executor, "sandbox", None)
        workspace = getattr(sbx, "workspace_path", None) if sbx is not None else None
        cwd = str(Path(workspace).resolve()) if workspace else None
        # 5s is plenty for a per-step done-condition probe — the C1c
        # gate uses the same default. C18 is advisory so we DON'T hang
        # the loop on a stuck command.
        timeout = 5.0

        def _run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                predicate.cmd,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env={"PATH": os.environ.get("PATH", "")},
            )

        started = asyncio.get_event_loop().time()
        try:
            completed = await asyncio.to_thread(_run)
        except subprocess.TimeoutExpired:
            return (False, f"command({predicate.cmd!r}): timeout after {timeout}s")
        except Exception as exc:  # noqa: BLE001 — defensive
            return (
                False,
                f"command({predicate.cmd!r}): executor error "
                f"({type(exc).__name__}: {exc})",
            )
        duration = asyncio.get_event_loop().time() - started
        if completed.returncode == predicate.expect_exit:
            return (
                True,
                f"command({predicate.cmd!r}): exited {completed.returncode} "
                f"as expected (in {duration:.2f}s)",
            )
        return (
            False,
            f"command({predicate.cmd!r}): exited {completed.returncode}, "
            f"expected {predicate.expect_exit}",
        )

    async def check_http_for_plan_step(
        self, predicate: HTTPOkPredicate
    ) -> tuple[bool, str]:
        """GET `predicate.url` and compare to `predicate.expect_status`
        (default 200). Tight timeout; failures surface the reason. Does
        NOT enforce the C1b egress allow-list — this is a per-step
        advisory check the agent opted into by attaching the predicate;
        the C1c gate (fresh-context, with egress discipline) is the
        authoritative check."""
        try:
            import httpx
        except ImportError:
            # httpx is in disco-core's deps (we saw it in pyproject.toml),
            # but be defensive in case the test env differs.
            try:
                import urllib.request
                with urllib.request.urlopen(predicate.url, timeout=2.0) as resp:
                    status = int(resp.status)
            except Exception as exc:  # noqa: BLE001 — defensive
                return (False, f"http_ok({predicate.url}): error ({exc})")
        else:
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    resp = await client.get(predicate.url)
                    status = int(resp.status_code)
            except Exception as exc:  # noqa: BLE001 — defensive
                return (False, f"http_ok({predicate.url}): error ({exc})")
        if status == predicate.expect_status:
            return (True, f"http_ok({predicate.url}): status {status} as expected")
        return (
            False,
            f"http_ok({predicate.url}): status {status}, "
            f"expected {predicate.expect_status}",
        )
