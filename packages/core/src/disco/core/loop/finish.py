"""The finish path: verify-on-finish, the C1c DoD gate, the plan-mode execution
nudge, the browser-verify gate, and the auto-continue finalizer.

Extracted from engine.py as a stateful collaborator: `FinishGate` holds a
back-ref to its `AgentLoop` and runs the affirmative-finish pipeline. The
module-level verify-command builders, the web-deliverable / browser-verify
helpers, the finish-cap constants, and the `_DoDWorkspaceUnavailable` exception
moved here too (re-exported from engine for back-compat). Method bodies are
byte-identical with `self.` rewritten to `self._loop.` (sibling calls stay
in-collaborator).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from . import signals
from ..dod_evaluator import DoDEvaluator
from ..events import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    EventSource,
    Event,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
    ToolCall,
)
from ..llm import OperatingMode
from ..state import ConversationState
from .boundaries import AgentStep
from .control import Disp
from .signals import _NON_PRODUCTIVE_TOOLS

if TYPE_CHECKING:
    from .engine import AgentLoop

_LOG = logging.getLogger("disco.loop")

# finish-verify cap (issue B). The model-authored `verify` gate was the ONLY uncapped
# gate in the loop — a broken/always-failing verify command could refuse `finish`
# forever. Mirror the existing _browser_verify_refusals release valve: after N REAL
# failures, finish anyway with a LOUD warning (the failure stays visible — important
# for weak local models — rather than grinding to max_iterations). A MALFORMED verify
# (SyntaxError / command-not-found) is a broken check, not a failed task, so it is
# auto-stripped (bounded by _finish_verify_strips so it can't be gamed as a free finish).
_FINISH_VERIFY_CAP = 3

# C1c DoD-gate refusal cap. The external Definition-of-Done gate refuses `finish`
# while the spec is unmet, but — exactly like the verify cap above — it MUST be
# bounded: an agent that cannot satisfy the external DoD would otherwise be
# trapped in an unbounded refuse-and-continue loop, accumulating events without
# end (this is the OOM the uncapped first cut caused). After N consecutive
# refusals the gate RELEASES (finish lands) with a LOUD warning; the prior
# refusal events remain the visible audit trail.
_DOD_REFUSAL_CAP = 3

# Symmetric to _PLAN_NUDGE on the execution side: a hard gate that refuses FINISHED
# until the agent has done productive work since the most recent plan approval.
# Small open models sometimes echo the plan as prose and declare "done" without
# touching anything — the gate catches that and re-enters the loop. Phrased as a
# `<system-reminder>` (ambient, implicit) rather than a user-tone scolding: the
# model sees an automated environment notification, not a confrontation.
_EXECUTION_NUDGE = (
    "<system-reminder>\n"
    "The approved plan has not been executed yet — no workspace files have been "
    "written, edited, or run since approval. Continue by calling a tool "
    "(file_write, file_edit, shell, code_exec, …) to carry out the plan's steps "
    "in order. The plan is in your context above.\n"
    "</system-reminder>"
)

# E4 — static-site verify. A static deliverable shouldn't have to curl a running
# server to prove it's good; the honest post-condition is "the file exists and is
# parseable HTML." The agent signals this with verify="static" (default index.html)
# or verify="static:<path>". We translate it to a server-free python3 check that
# runs through the SAME safe gate as any verify command (it assesses LOW — no
# confirm). exit 0 ⇔ the page exists, is non-trivial, and parses.
_STATIC_VERIFY_PREFIX = "static"


def _static_verify_command(path: str) -> str:
    p = (path or "index.html").strip().strip("'\"") or "index.html"
    # single-quote the path safely for the shell, then hand to python3 -c
    safe = p.replace("'", "'\\''")
    script = (
        "import sys,os.path,html.parser as H;"
        f"p='{safe}';"
        "(os.path.isfile(p) or sys.exit('missing '+p));"
        "d=open(p,encoding='utf-8',errors='replace').read();"
        "(len(d.strip())>=20 or sys.exit('empty '+p));"
        "t=[];pr=H.HTMLParser();pr.handle_starttag=lambda n,a:t.append(n);pr.feed(d);"
        "print('OK '+p+' tags='+str(len(t)));"
        "sys.exit(0 if t else 'no html tags in '+p)"
    )
    return f'python3 -c "{script}"'


# verify="app" / "app:<url>" — SERVER-AWARE self-verification (the verify_app
# gap). Where `static` only proves a file exists + parses, `app` proves the
# RUNNING deliverable actually serves: it GETs the URL inside the sandbox and
# requires HTTP 200 + a non-trivial body. Portable (python3/urllib — no curl/
# chromium dependency) so it runs on every backend; routes through the same safe
# verify gate. A full pixel screenshot needs a browser in the sandbox image (not
# present on the process backend) — this is the honest server-up post-condition.
_APP_VERIFY_PREFIX = "app"


def _app_verify_command(url: str) -> str:
    u = (url or "http://localhost:8000/").strip().strip("'\"") or "http://localhost:8000/"
    safe = u.replace("'", "'\\''")
    # No try/except (a `-c` one-liner can't carry the block): a connection failure
    # raises URLError → nonzero exit + a traceback the agent reads as "not serving".
    script = (
        "import sys,urllib.request as U;"
        f"u='{safe}';"
        "r=U.urlopen(u,timeout=10);"
        "code=getattr(r,'status',None) or r.getcode();"
        "(code==200 or sys.exit('HTTP '+str(code)+' from '+u));"
        "b=r.read().decode('utf-8','replace');"
        "(len(b.strip())>=20 or sys.exit('empty body from '+u));"
        "print('OK '+u+' '+str(code)+' bytes='+str(len(b)))"
    )
    return f'python3 -c "{script}"'


def _last_productive_seq(events: list[Event]) -> int:
    """Seq of the last state-changing action (not in _NON_PRODUCTIVE_TOOLS).
    If no productive action found, returns 0."""
    for ev in reversed(events):
        if isinstance(ev, ActionEvent):
            if ev.tool_call.tool_name not in _NON_PRODUCTIVE_TOOLS:
                return ev.seq or 0
    return 0


def _is_web_deliverable(events: list[Event]) -> bool:
    """Web deliverable if index.html was written/edited OR port 8000 owned by non-preview.
    Derived from events to keep the check pure (event-list in, verdict out)."""
    for ev in reversed(events):
        if isinstance(ev, ActionEvent):
            if ev.tool_call.tool_name in (
                "file_write",
                "file_edit",
                "file_append",
                "file_replace_lines",
                "file_insert_lines",
            ):
                path = ev.tool_call.arguments.get("path")
                # Canonical workspace-root paths
                if path in ("index.html", "./index.html"):
                    return True
        elif isinstance(ev, ObservationEvent) and ev.tool_result.tool_name == "server_status":
            content = ev.tool_result.content
            # "  - 8000: OWNED by pid 123 (python) [session: dev]"
            for line in content.splitlines():
                if "8000: OWNED" in line and "[session: " in line:
                    session = line.split("[session: ")[1].split("]")[0]
                    if session != "preview":
                        return True
    return False


def _browser_verified(events: list[Event], since_seq: int) -> tuple[bool, str | None]:
    """Scan browser observations since since_seq. Returns (ok, first_error_line).
    An observation is valid if it's from the browser tool, against port 8000,
    and has zero console errors. If not ok, returns the first error from the
    LATEST qualifying observation."""
    valid_obs = []
    for ev in events:
        if ev.seq is None or ev.seq <= since_seq:
            continue
        if isinstance(ev, ObservationEvent) and ev.tool_result.tool_name == "browser":
            res = ev.tool_result
            if res.success and res.structured:
                url = str(res.structured.get("url", ""))
                if url.startswith("http://127.0.0.1:8000") or url.startswith(
                    "http://localhost:8000"
                ):
                    valid_obs.append(res.structured)

    if not valid_obs:
        return False, None

    # ok = at least one observation with zero console errors
    ok = any(
        not any(c.get("level") == "error" for c in obs.get("console", [])) for obs in valid_obs
    )

    # first_error_line = from the LATEST qualifying-URL observation that has error-level entries
    first_error_line = None
    for obs in reversed(valid_obs):
        errors = [
            str(c.get("text", "")) for c in obs.get("console", []) if c.get("level") == "error"
        ]
        if errors:
            first_error_line = errors[0]
            break

    return ok, first_error_line


def _latest_browser_error(events: list[Event]) -> str | None:
    """First error-level console line of the LATEST qualifying browser observation
    (any seq — full history). None if the agent never browsed :8000 or its last
    look was clean. This feeds the gate's human-facing messages: the verdict is
    scoped to since-last-edit (_browser_verified), but "last console errors"
    must report what was actually last SEEN — a post-browse edit moves since_seq
    past the observation and would otherwise erase a real, observed error."""
    for ev in reversed(events):
        if not (isinstance(ev, ObservationEvent) and ev.tool_result.tool_name == "browser"):
            continue
        res = ev.tool_result
        if not (res.success and res.structured):
            continue
        url = str(res.structured.get("url", ""))
        if not (
            url.startswith("http://127.0.0.1:8000") or url.startswith("http://localhost:8000")
        ):
            continue
        errors = [
            str(c.get("text", ""))
            for c in res.structured.get("console", [])
            if c.get("level") == "error"
        ]
        return errors[0] if errors else None
    return None


class _DoDWorkspaceUnavailable(Exception):
    """The C1c gate cannot resolve a workspace_root (no sandbox, no
    `workspace_path` attribute on the executor). Raised by
    `_build_dod_evaluator` and caught by `_finish_dod_gate_passed` to
    degrade the gate to a no-op (logged, never raised past the gate).

    Distinct from a predicate failure: the spec is set but the engine
    has no evidence surface to grade against. Refusing the finish in
    that state would be a silent fail — the agent would loop forever
    on a gate that cannot run. Logging + skipping is the honest
    behavior; the audit trail sees the log line."""


class FinishGate:
    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    async def finish_verify_passed(self, command: str) -> bool:
        """Run the agent's stated acceptance check before allowing `finish`
        (verify-on-finish post-condition gate). The agent attaches a shell
        command to finish whose exit 0 means the deliverable is good; we run it,
        VISIBLE in the trace, and on failure REFUSE the finish so the agent fixes
        the real problem instead of declaring a broken build complete.

        The verify command is NOT privileged: it passes the same hard-deny gate
        AND the same confirmation policy as any action. A command that would
        normally require confirmation is refused here (we don't silently run a
        gated command as a 'verification') — the agent is told to run it as an
        ordinary, gated action first. Ordinary test/build/lint checks assess as
        MEDIUM and run unimpeded. Returns True iff the check ran and passed."""
        call = ToolCall(tool_name="shell", arguments={"command": command})
        # meta marker: this shell action is the GATE'S probe, not the agent's
        # work. Phase-B re-run #6 (2026-06-10): an unmarked probe counted as a
        # real action in _actions_since_last_resume, so a refused first-move
        # finish UNLOCKED the withheld meta tools and the model remember-spammed
        # straight into the valve. The probe must never flip fresh-session.
        action = ActionEvent(
            thought=f"Verifying completion: {command}",
            tool_call=call,
            meta={"verify_probe": True},
        )

        deny = signals.hard_deny_reason(action)
        if deny is not None:
            await self._loop._emit(action)
            await self._loop._emit(
                AgentErrorEvent(
                    error=(
                        "<system-reminder>\n"
                        f"The verify command attached to finish is hard-denied ({deny}); it "
                        "will not run. Provide a safe verify command, or finish without one.\n"
                        "</system-reminder>"
                    ),
                    action_id=action.id,
                    tool_call_id=call.call_id,
                )
            )
            return False, False

        risk = self._loop.analyzer.assess(action)
        if self._loop.policy.should_confirm(risk):
            await self._loop._emit(action)
            await self._loop._emit(
                AgentErrorEvent(
                    error=(
                        "<system-reminder>\n"
                        "The verify command attached to finish needs confirmation to run "
                        "and won't be executed silently as a verification. Run that check "
                        "as a normal action first (it will go through the confirm gate), "
                        "then finish.\n"
                        "</system-reminder>"
                    ),
                    action_id=action.id,
                    tool_call_id=call.call_id,
                )
            )
            return False, False

        await self._loop._emit(action)
        await self._loop._execute_and_observe(action)
        # Find the observation correlated to THIS verify action (robust against a
        # trailing sandbox-restart notice that _execute_and_observe may append).
        events_after = await self._loop._events()
        obs = next(
            (e for e in reversed(events_after) if getattr(e, "action_id", None) == action.id),
            None,
        )
        passed = isinstance(obs, ObservationEvent) and obs.tool_result.success
        # malformed = the verify COMMAND ITSELF is broken (not the deliverable):
        # command-not-found (127) or an interpreter SyntaxError. A non-zero exit from
        # an unrunnable check is NOT evidence the task failed — the carrier was bad.
        # The caller auto-strips a malformed verify rather than counting it as a
        # failed acceptance. Detected from the result text.
        malformed = False
        # Malformed = the verify CARRIER is broken, which only makes sense if the
        # shell actually RAN the command and reported it (an ObservationEvent). An
        # AgentErrorEvent means the executor raised BEFORE any observation (sandbox
        # down, transport error) — that's an environmental failure, NOT a malformed
        # verify, and must stay a real failure so it isn't auto-stripped into a false
        # "done". (Earlier this read AgentErrorEvent.error text and a stray "command
        # not found" substring there would wrongly strip an environmental failure.)
        if not passed and isinstance(obs, ObservationEvent):
            st = obs.tool_result.structured or {}
            ec = st.get("exit_code")
            exit_code = ec if isinstance(ec, int) and not isinstance(ec, bool) else None
            low = f"{obs.tool_result.content or ''} {obs.tool_result.error or ''}".lower()
            # The shell couldn't find/parse the command: exit 127 (command-not-found,
            # authoritative from the structured result — locale-independent, can't be
            # faked by output text) or an interpreter SyntaxError. Use the real exit
            # code, NOT a regex over output: "exit 1, 127 tests failed" is a REAL
            # failure, not a malformed carrier, and must NOT become a false success.
            malformed = (
                exit_code == 127
                or "syntaxerror" in low
                # content fallbacks only when the structured exit code is unavailable
                or (exit_code is None and "command not found" in low)
                or (exit_code is None and ": not found" in low)
            )
        return passed, malformed

    async def finish_dod_gate_passed(self) -> bool:
        """C1c DoD gate. Returns True iff the finish should be allowed.

        Algorithm (in order):
          1. Read the DoD spec from the store. None → no spec → True
             (LEGACY BYTE-IDENTICAL PATH — no events emitted, no state
             changed, control flow identical to pre-C1c).
          2. Build a DoDEvaluator. Factory-injected if the loop was
             constructed with one; otherwise build a default over the
             executor's sandbox workspace_root (or skip the gate if the
             sandbox doesn't expose a path — defensive, never a crash).
          3. Run the verdict against the spec.
          4. Verdict passed → True (the finish lands).
          5. Verdict failed → emit a MessageEvent carrying the SPECIFIC
             unmet predicates (visible to the agent AND the audit), bump
             the refusal streak, return False so the caller `continue`s.

        Visible: the refusal is a `<system-reminder>` MessageEvent with the
        spec fingerprint, the number of unmet predicates, and a per-
        predicate line naming kind + reason. Never silent: a refused finish
        ALWAYS leaves a trace event. Same shape as verify-on-finish's
        refusal, distinct content (the predicates are external, not the
        agent's own command)."""
        spec = await self._loop.store.get_dod_spec(self._loop.conversation_id)
        if spec is None:
            # LEGACY: no DoD spec for this conversation → the gate is a
            # no-op. Today's finish path is reproduced EXACTLY — no events,
            # no state change, no log query beyond a single SELECT. The
            # read is observable as a side-effect-free DB query; it does
            # NOT change the events, status transitions, or final state.
            return True
        # Spec exists → run the evaluator. The factory seam is the test
        # injection point (fakes for command_runner / http_probe); the
        # default builds a real DoDEvaluator over the executor's workspace.
        try:
            evaluator = await self.build_dod_evaluator()
        except _DoDWorkspaceUnavailable:
            # No workspace to grade against. This is a misconfiguration
            # (the spec was set but the sandbox doesn't expose a path),
            # not a predicate failure. We log and skip the gate rather
            # than refusing forever — refusing without a reason would
            # also be a silent failure mode. The audit trail will see the
            # log line; the agent sees no gate, so the run can finish.
            _LOG.warning(
                "DoD spec set for %s but no workspace_root available; "
                "skipping C1c gate (refusing without evidence would be a "
                "silent fail).",
                self._loop.conversation_id,
            )
            return True
        verdict = await evaluator.evaluate(spec, conversation_id=self._loop.conversation_id)
        if verdict.passed:
            self._loop._dod_refusals = 0  # clean pass → reset the streak (mirror verify)
            return True
        # Cap the refusal streak (mirror _FINISH_VERIFY_CAP): after N consecutive
        # DoD refusals, RELEASE the gate so an agent that cannot satisfy the
        # external DoD is not trapped in an unbounded refuse-and-continue loop
        # (that loop accumulates events without end — the OOM the uncapped first
        # cut caused). The release is logged LOUDLY; the prior refusal events
        # remain the visible audit trail of the unmet predicates.
        if self._loop._dod_refusals >= _DOD_REFUSAL_CAP:
            _LOG.warning(
                "DoD for %s still unmet after %d refusals (cap %d) — releasing the "
                "finish gate to avoid an unbounded refuse loop.",
                self._loop.conversation_id,
                self._loop._dod_refusals,
                _DOD_REFUSAL_CAP,
            )
            return True
        # Refuse + keep working. The agent sees the SPECIFIC unmet
        # predicates (named by `kind` + the frozen-predicate `repr`); the
        # audit sees the spec fingerprint + the per-predicate results.
        self._loop._dod_refusals += 1  # bounded by _DOD_REFUSAL_CAP (see above)
        unmet_lines: list[str] = []
        for result in verdict.results:
            if result.passed:
                continue
            # The frozen predicate's repr names kind + fields. Pair with
            # the verdict's reason (the human explanation).
            unmet_lines.append(f"  - {result.predicate!r}\n      reason: {result.reason}")
        unmet_block = "\n".join(unmet_lines) if unmet_lines else "  - (no per-predicate results)"
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        "You called finish, but the external Definition-of-Done "
                        f"evaluator found {len(verdict.unmet)} unmet acceptance "
                        f"predicate(s) (spec fingerprint {verdict.spec_fingerprint}):\n\n"
                        f"{unmet_block}\n\n"
                        "The task is NOT complete. These predicates were captured at "
                        "task start and live outside the agent's tool surface — you "
                        "cannot edit them, you can only satisfy them. Fix what they "
                        "surface (the predicates name the gap), then finish again.\n"
                        "</system-reminder>"
                    ),
                ),
            )
        )
        return False

    async def build_dod_evaluator(self) -> DoDEvaluator:
        """Construct the DoDEvaluator. Two paths:

          * `_dod_evaluator_factory` is set (test seam): call it, ignore args.
          * Otherwise: derive the workspace_root from the executor's sandbox
            (the in-cluster `workspace_path`); if absent, raise
            `_DoDWorkspaceUnavailable` and the gate degrades to "skip".

        The factory is the dependency-injection point — tests close over
        a tmp_path + fake command_runner / http_probe and return a fully
        configured `DoDEvaluator`. Production callers leave the factory
        None and the engine does the workspace resolution here.
        """
        if self._loop._dod_evaluator_factory is not None:
            # The factory is an async-callable in the common case (tests
            # want to close over a `tmp_path` + fakes without performing
            # any I/O at construction time), but a sync callable is also
            # accepted — production callers may want to keep the
            # construction cheap. Awaiting a non-awaitable raises
            # TypeError, which the gate's `_DoDWorkspaceUnavailable`-
            # style `try/except` doesn't catch; the explicit
            # `inspect.iscoroutine` check keeps both shapes working.
            import inspect

            result = self._loop._dod_evaluator_factory()
            if inspect.iscoroutine(result):
                result = await result
            return result
        sbx = getattr(self._loop.executor, "sandbox", None)
        workspace = getattr(sbx, "workspace_path", None) if sbx is not None else None
        if not workspace:
            raise _DoDWorkspaceUnavailable(
                f"no sandbox.workspace_path on executor {type(self._loop.executor).__name__}"
            )
        from pathlib import Path
        return DoDEvaluator(Path(workspace))

    def resolve_verify_command(self, args: dict) -> str:
        verify_cmd = str(args.get("verify") or "").strip()
        # E4: a `static` directive verifies a static page WITHOUT a server
        # (files present + HTML parses) — the honest check for a page build.
        if verify_cmd == _STATIC_VERIFY_PREFIX or verify_cmd.startswith(
            _STATIC_VERIFY_PREFIX + ":"
        ):
            _, _, _path = verify_cmd.partition(":")
            verify_cmd = _static_verify_command(_path)
        # app / app:<url> — verify the RUNNING deliverable actually serves
        # (HTTP 200 + non-trivial body), not just that a file exists.
        elif verify_cmd == _APP_VERIFY_PREFIX or verify_cmd.startswith(
            _APP_VERIFY_PREFIX + ":"
        ):
            _, _, _url = verify_cmd.partition(":")
            verify_cmd = _app_verify_command(_url)
        return verify_cmd

    async def normalize_finish_step(
        self, step: AgentStep, events: list[Event]
    ) -> tuple[AgentStep, Disp]:
        # VERIFY-ON-FINISH (post-condition gate). If the agent attached
        # a `verify` check to finish, RUN it first and refuse the finish
        # if it doesn't pass — the "run the tests before you claim done"
        # forcing function. The check is visible in the trace; on
        # failure the agent sees exactly what broke and adapts, instead
        # of declaring a broken build complete.
        verify_cmd = self.resolve_verify_command(step.tool_call.arguments)
        if verify_cmd:
            passed, malformed = await self.finish_verify_passed(verify_cmd)
            if passed:
                self._loop._finish_verify_refusals = 0  # reset streak on clean pass
            elif malformed and self._loop._finish_verify_strips < 3:
                # Broken CHECK, not a failed task → auto-strip and finish.
                self._loop._finish_verify_strips += 1
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "<system-reminder>\n"
                                f"Your verify command `{verify_cmd}` is not runnable "
                                "(syntax error / command-not-found) — that is a broken "
                                "CHECK, not a failed task, so it is "
                                "being ignored and the "
                                "run is finishing. Next time pass a "
                                "valid shell command "
                                "if you want real verification.\n"
                                "</system-reminder>"
                            ),
                        ),
                    )
                )
                # fall through to finish
            elif self._loop._finish_verify_refusals < _FINISH_VERIFY_CAP:
                # Real failure: refuse + keep working (the forcing function).
                self._loop._finish_verify_refusals += 1
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "<system-reminder>\n"
                                f"You called finish, but the verify "
                                f"command `{verify_cmd}` "
                                "did not pass (see the result above). The task is NOT "
                                "complete. Fix what it surfaced, then "
                                "finish again — or "
                                "finish without a verify command if "
                                "the check itself is "
                                "wrong.\n"
                                "</system-reminder>"
                            ),
                        ),
                    )
                )
                return step, Disp.CONTINUE
            else:
                # Cap reached: LOUD release — don't grind forever on a gate the
                # model can't satisfy (mirrors the browser-verify valve). The
                # failure stays visible (status detail + reminder + summary).
                await self._loop._emit(
                    StatusEvent(
                        status=ConversationStatus.RUNNING,
                        detail="finish_verify_release",
                    )
                )
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "<system-reminder>\n"
                                f"The verify command `{verify_cmd}` has failed "
                                f"{self._loop._finish_verify_refusals} times. "
                                "Finishing anyway "
                                "so the run does not loop forever — "
                                "but the deliverable "
                                "may be incomplete. Note this clearly "
                                "in your summary.\n"
                                "</system-reminder>"
                            ),
                        ),
                    )
                )
                # ⚠ human-facing: surfaces in the UI as a warning chip so the
                # user knows the run finished with a failing check.
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "⚠ Finished despite the verification check failing "
                                f"{self._loop._finish_verify_refusals}× — "
                                "the deliverable may "
                                "be incomplete; review it."
                            ),
                        ),
                    )
                )
                self._loop._finish_verify_refusals = 0
                # fall through to finish
        # C1c — external DoD evaluator gate. Runs AFTER the
        # agent's own verify check (which grades the agent's
        # own command) but BEFORE the step is committed to a
        # FINISHED status. The spec is captured at task start
        # and lives outside the agent's tool surface, so the
        # predicates are NOT the agent's own — they're a
        # structural, write-once acceptance bar (see
        # `core/dod.py`). When the spec exists and the verdict
        # fails, the finish is REFUSED and the run CONTINUES —
        # the same refuse-and-continue discipline verify-on-
        # finish uses. When no spec is set for the
        # conversation, the gate is a no-op (legacy path is
        # byte-identical). See `_finish_dod_gate_passed` for
        # the full algorithm + the byte-identical-no-spec
        # proof.
        if not await self.finish_dod_gate_passed():
            return step, Disp.CONTINUE
        summary = str(step.tool_call.arguments.get("summary") or "").strip()
        step = step.model_copy(
            update={
                "finished": True,
                "tool_call": None,
                "thought": summary or step.thought,
            }
        )
        return step, Disp.FALLTHROUGH

    async def gate_execution_nudge(self, step: AgentStep, events: list[Event]) -> Disp:
        # PLAN-MODE EXECUTION GATE — a forcing function, NOT a prompt. If
        # the loop is in execution mode (planning_tools configured) and the
        # agent declares "done" without any productive action since plan
        # approval, refuse the finish: append an IMPLICIT system-reminder
        # and re-enter the loop. No cap — the reminder keeps firing as long
        # as the agent tries to walk away without acting. The loop's own
        # max_iterations + the user's kill switch are the ultimate exits.
        if (
            self._loop._planning_tools  # plan-first lifecycle is configured
            and self._loop.mode != OperatingMode.PLANNING  # we're executing
            and not signals.productive_action_since_approval(events)
        ):
            self._loop._execution_nudges += 1  # telemetry
            # Surface the model's reasoning before nudging (don't
            # discard it) — mirror the plan-nudge sibling.
            if step.thought.strip():
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.AGENT,
                        message=LLMMessage(
                            role="assistant", content=step.thought
                        ),
                    )
                )
            else:
                # An empty finish-step persists nothing, so it's
                # invisible to every event-derived detector; the
                # instance counter has to carry it (the (g) no-op
                # path does the same).
                self._loop._invisible_steps += 1
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=_EXECUTION_NUDGE),
                )
            )
            # BACKSTOP (was missing — the confirm/reject livelock's
            # second defect): route through the shared actionless
            # valve. A nudge-only turn emits no ActionEvent, so
            # `iteration`/max_iterations NEVER advances on it — the
            # old "max_iterations is the ultimate exit" claim was
            # false and a model that declared done without ever
            # acting spun here forever. The valve now lands it
            # cleanly (FINISHED/PAUSED:noop_limit) after
            # `_max_consecutive_noops` actionless turns; a model that
            # recovers and acts resets the streak (engine §h).
            if await self._loop._post_noop_valve() is Disp.HALT:
                return Disp.HALT
            return Disp.CONTINUE
        return Disp.FALLTHROUGH

    async def gate_browser_verify(self, step: AgentStep, events: list[Event]) -> Disp:
        # BROWSER-VERIFY GATE — §BP-05. If web deliverable holds, refuse finish
        # until a clean browser observation (zero console errors) exists
        # since the last state-changing edit.
        if (
            self._loop._planning_tools
            and self._loop.mode != OperatingMode.PLANNING
            and _is_web_deliverable(events)
        ):
            since_seq = _last_productive_seq(events)
            ok, _ = _browser_verified(events, since_seq)
            # Messaging reads the FULL history: a post-browse edit
            # invalidates the verification but not what was seen.
            first_error = _latest_browser_error(events)
            if ok:
                self._loop._browser_verify_refusals = 0  # reset on clean pass
            elif self._loop._browser_verify_refusals < 3:
                self._loop._browser_verify_refusals += 1
                if first_error:
                    # Variant (2): quote the error
                    nudge = (
                        "Before finishing: verify your app the way a user would. "
                        "Use the browser tool to navigate to http://127.0.0.1:8000/, "
                        "read the CONSOLE output, and fix any errors you see. "
                        f"The last load had errors: {first_error}"
                    )
                else:
                    # Variant (1): verbatim from order
                    nudge = (
                        "Before finishing: verify your app the way a user would. "
                        "Use the browser tool to navigate to http://127.0.0.1:8000/, "
                        "read the CONSOLE output, and fix any errors you see. "
                        "Finish only after a clean load."
                    )
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(role="user", content=nudge),
                    )
                )
                return Disp.CONTINUE
            else:
                # 3-refusal release valve (3): allow but warn visibly
                warn_msg = (
                    "⚠ finished WITHOUT a clean browser verification — "
                    f"last console errors: {first_error or 'none seen'}"
                )
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(role="user", content=warn_msg),
                    )
                )
        return Disp.FALLTHROUGH

    async def finalize_finish(
        self, step: AgentStep, state: ConversationState, events: list[Event]
    ) -> Disp:
        if await self._loop._stop_allowed(state, events):
            # Record the agent's final message (the answer) before
            # finishing — the deliverable text belongs on the log, not
            # discarded on the finish signal. (When the model just
            # answers a question, this IS the response the UI renders.)
            if step.thought.strip():
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.AGENT,
                        message=LLMMessage(role="assistant", content=step.thought),
                    )
                )
            # AUTO-CONTINUE GATE — when the agent declares finished
            # but the plan still has incomplete steps, DO NOT land
            # in STUCK and freeze. The user shouldn't have to poke
            # the loop to keep going. Instead, inject a continuation
            # prompt and re-run automatically, up to AUTO_CONTINUE_CAP
            # times per user message. Only after the cap (typically
            # 3) do we fall through to FINISHED with a "soft" detail
            # so the user sees a clean ending rather than a freeze.
            #
            # The cap resets when the user sends a new message —
            # each fresh prompt gets its own auto-continue budget.
            incomplete, missing = signals.plan_is_incomplete(events)
            if incomplete:
                attempts = signals.auto_continue_attempts(events)
                if attempts < self._loop._auto_continue_cap:
                    await self._loop._emit(
                        MessageEvent(
                            source=EventSource.ENVIRONMENT,
                            message=LLMMessage(
                                role="user",
                                content=(
                                    "<system-reminder>\n"
                                    "You declared the work finished, but plan "
                                    f"steps {missing} are not yet marked done. "
                                    "Keep working: either complete the remaining "
                                    "steps and mark them via "
                                    "plan_step(idx, 'done'), OR — if a step is "
                                    "structurally wrong now — call "
                                    "propose_plan_update to revise the plan. "
                                    "Do not declare finished again until every "
                                    "step is marked done. The user will see this "
                                    "as the agent automatically continuing.\n"
                                    "</system-reminder>"
                                ),
                            ),
                        )
                    )
                    await self._loop._emit(
                        StatusEvent(
                            status=ConversationStatus.RUNNING,
                            detail=f"auto_continue:plan_incomplete:{attempts + 1}",
                        )
                    )
                    return Disp.CONTINUE
                # Cap hit. Land FINISHED with a "partial" detail
                # rather than STUCK; the user sees a clean ending
                # and can steer if more work is needed. STUCK is
                # reserved for genuine confusion (stuck detector),
                # not for "model couldn't quite finish the bookkeeping".
                actions_since = signals.actions_since_last_resume(events)
                if actions_since == 0:
                    await self._loop._emit(
                        MessageEvent(
                            source=EventSource.ENVIRONMENT,
                            message=LLMMessage(
                                role="user",
                                content=(
                                    "<system-reminder>\n⚠ finishing was"
                                    " blocked: plan steps remain undone and"
                                    " no work happened in this run segment."
                                    "\n</system-reminder>"
                                ),
                            )
                        )
                    )
                    await self._loop._emit(
                        StatusEvent(
                            status=ConversationStatus.PAUSED,
                            detail="partial_plan",
                        )
                    )
                else:
                    await self._loop._emit(
                        MessageEvent(
                            source=EventSource.ENVIRONMENT,
                            message=LLMMessage(
                                role="user",
                                content=(
                                    "<system-reminder>\n"
                                    f"After {self._loop._auto_continue_cap} auto-continues, "
                                    f"plan steps {missing} are still not marked done. "
                                    "Landing the run as FINISHED with partial-plan "
                                    "detail — the user can review and steer if more "
                                    "work is needed.\n"
                                    "</system-reminder>"
                                ),
                            ),
                        )
                    )
                    await self._loop._emit(
                        StatusEvent(
                            status=ConversationStatus.FINISHED,
                            detail="partial_plan",
                        )
                    )
                return Disp.HALT
            await self._loop._emit(StatusEvent(status=ConversationStatus.FINISHED))
            return Disp.HALT
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=self._loop._veto_feedback),
            )
        )
        return Disp.CONTINUE

    async def handle_finish_path(
        self, step: AgentStep, state: ConversationState, events: list[Event]
    ) -> Disp:
        disp = await self.gate_execution_nudge(step, events)
        if disp is Disp.CONTINUE:
            return Disp.CONTINUE
        if disp is Disp.HALT:
            return Disp.HALT

        disp = await self.gate_browser_verify(step, events)
        if disp is Disp.CONTINUE:
            return Disp.CONTINUE

        disp = await self.finalize_finish(step, state, events)
        if disp is Disp.CONTINUE:
            return Disp.CONTINUE
        if disp is Disp.HALT:
            return Disp.HALT
        return Disp.FALLTHROUGH
