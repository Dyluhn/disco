"""Finish-step normalization, finalization, and synthetic finish valve."""

from __future__ import annotations

from dataclasses import dataclass

from .common import (
    _APP_VERIFY_PREFIX,
    _STATIC_VERIFY_PREFIX,
    AgentStep,
    ConversationState,
    ConversationStatus,
    Disp,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
    ToolCall,
    VerifierVerdictEvent,
    _app_verify_command,
    _FinishGateComponent,
    _static_verify_command,
    signals,
)
from .finalize_parts import finish_step, seal_gate


@dataclass(frozen=True)
class _ResolvedVerify:
    """Typed result of resolving a finish verify directive.

    When *ignored_reason* is set the optional model-owned verifier was
    rejected at the validation boundary — *command* is empty and a
    structured environment reminder must be emitted by the caller.
    """

    command: str
    ignored_reason: str | None = None


def _unverified_release_final_message(events: list[Event]) -> str:
    """The host-owned final message for an unverified release, RENDERED.

    GROUNDED FEEDBACK constraints 1 and 4: this replaces the model's own closing
    summary, so it is the last thing the reader sees — and it used to be one
    canned paragraph that never said WHICH verification was missing, though the
    verdict log holds exactly that. It now names the host verdict on record (or
    states that none was produced), which is the same source the release marker
    itself is derived from, so message and marker cannot drift.
    """
    latest = next(
        (event for event in reversed(events) if isinstance(event, VerifierVerdictEvent)),
        None,
    )
    if latest is None:
        observed = "No host verifier verdict was produced at all for this run."
    else:
        verdict = (latest.verdict or "none").strip() or "none"
        observed = (
            f"The last host verifier verdict on record is {verdict!r} "
            f"(verified={latest.verified})."
        )
    return (
        "⚠ UNVERIFIED FINAL RESULT: Required host/browser verification did not pass "
        "or could not run. Browser rendering remains UNVERIFIED, and the deliverable "
        "may be INCOMPLETE. Any model-authored verification claim is superseded by "
        f"this host-owned result. {observed}"
    )


def _unverified_release_active(events: list[Event]) -> bool:
    """Whether finalization is under the current unverified-release marker.

    The marker is a RUNNING status scoped to the current finish attempt. A later
    lifecycle transition makes an old marker stale, and a later passing host
    verdict explicitly supersedes it even when a stop-hook kept the same run
    segment alive. This is deliberately event-only: finalization must not spend
    another provider call asking the model to correct its pre-gate summary.
    """
    active = False
    for event in events:
        if isinstance(event, StatusEvent):
            active = (
                event.status == ConversationStatus.RUNNING and event.detail == "unverified_release"
            )
        elif (
            active
            and isinstance(event, VerifierVerdictEvent)
            and event.verified
            and event.verdict == "pass"
        ):
            active = False
    return active


class _FinalizeService(_FinishGateComponent):
    async def resolve_verify_command(self, args: dict) -> _ResolvedVerify:
        verify_cmd = str(args.get("verify") or "").strip()
        # E4: a `static` directive verifies a static page WITHOUT a server
        # (files present + HTML parses) — the honest check for a page build.
        if verify_cmd == _STATIC_VERIFY_PREFIX:
            # Bare `static` → default index.html HTML probe.
            return _ResolvedVerify(command=_static_verify_command(""))
        if verify_cmd.startswith(_STATIC_VERIFY_PREFIX + ":"):
            _, _, _path = verify_cmd.partition(":")
            # Normalize exactly as _static_verify_command does internally,
            # then validate it is an HTML file path (case-insensitive).
            _normalized = _path.strip().strip("'\"")
            if _normalized.lower().endswith((".html", ".htm")):
                return _ResolvedVerify(command=_static_verify_command(_path))
            # Non-HTML static path — malformed optional model-owned verifier.
            return _ResolvedVerify(
                command="",
                ignored_reason=(
                    f"structured static verify directive `{verify_cmd}` requires an .html"
                    " or .htm file path; non-HTML paths are not supported as static "
                    "verifiers"
                ),
            )
        # app / app:<url> — verify the RUNNING deliverable actually serves
        # (HTTP 200 + non-trivial body), not just that a file exists.
        if verify_cmd == _APP_VERIFY_PREFIX or verify_cmd.startswith(_APP_VERIFY_PREFIX + ":"):
            _, _, _url = verify_cmd.partition(":")
            _url = _url.strip()
            if not _url:
                # Bare `verify="app"` (no explicit URL). The platform assigns the preview
                # port — there is NO fixed :8000 inside the sandbox (a curl there 404s),
                # so resolve the URL the live deliverable ACTUALLY serves on (the same
                # backend-aware detection the verify_web_app gate uses). If no live preview
                # can be located, degrade to the server-free static check rather than
                # asserting :8000 — a 404 on a guessed port is a FALSE failure that would
                # spin the model into re-serve/re-verify loops.
                _url = await self._coordinator._detect_preview_url() or ""
                if not _url:
                    return _ResolvedVerify(command=_static_verify_command("index.html"))
            return _ResolvedVerify(command=_app_verify_command(_url))
        return _ResolvedVerify(command=verify_cmd)

    async def normalize_finish_step(
        self, step: AgentStep, events: list[Event]
    ) -> tuple[AgentStep, Disp]:
        # VERIFY-ON-FINISH is model-authored advisory evidence. Run a safe
        # supplied check once and retain its result in the trace, but never let
        # it veto completion or reopen build. External DoD and host-owned
        # verification remain authoritative below.
        assert step.tool_call is not None  # caller (engine loop) enters only on the finish tool
        resolved = await self.resolve_verify_command(step.tool_call.arguments)

        # E4a: malformed structured static directive — emit reminder, skip optional verifier.
        if resolved.ignored_reason:
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=(
                            "<system-reminder>\n"
                            f"The {resolved.ignored_reason}. "
                            "This directive has been ignored.\n"
                            "</system-reminder>"
                        ),
                    ),
                    meta={
                        "finish_verify_ignored": True,
                        "verify_kind": "static",
                        "directive": str(step.tool_call.arguments.get("verify") or ""),
                    },
                )
            )
            verify_cmd = ""
        elif resolved.command:
            verify_cmd = resolved.command
        else:
            verify_cmd = ""

        if verify_cmd:
            disp = await finish_step.resolve_finish_verify_disposition(
                self._loop,
                verify_cmd,
                events,
                finish_verify_passed=self._coordinator.verification.finish_verify_passed,
            )
            if disp is not None:
                return step, disp
        # C1c — external DoD evaluator gate. Runs AFTER the
        # agent's advisory check but BEFORE the step is committed to a
        # FINISHED status. The spec is captured at task start
        # and lives outside the agent's tool surface, so the
        # predicates are NOT the agent's own — they're a
        # structural, write-once acceptance bar (see
        # `core/dod.py`). When the spec exists and the verdict
        # fails, the finish is REFUSED and the run CONTINUES. When no spec is set for the
        # conversation, the gate is a no-op (legacy path is
        # byte-identical). See `_finish_dod_gate_passed` for
        # the full algorithm + the byte-identical-no-spec
        # proof.
        if not await self._coordinator.verification.finish_dod_gate_passed():
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

    async def seal_gate_allows_finish(self) -> bool:
        """REL-27 (F-27) — finish-time workspace sealability gate.

        Called immediately before EVERY affirmative FINISHED emission: the
        finish battery's finalize, the completed-via-notify path, and the
        browserless honest-static valve. The full behavioral contract
        (semantics for no-probe / sealable / blocking-under-cap /
        blocking-at-cap / probe-error) lives on
        ``finalize_parts.seal_gate.seal_gate_allows_finish``, which owns
        this policy; this method is a thin delegator so every external
        caller (finalize_finish, the completed-via-notify path, and the
        browserless honest-static valve) keeps calling
        ``self.seal_gate_allows_finish()`` unchanged.
        """
        return await seal_gate.seal_gate_allows_finish(self._loop)

    async def finalize_finish(
        self, step: AgentStep, state: ConversationState, events: list[Event]
    ) -> Disp:
        if await self._loop._stop_allowed(state, events):
            if not await self._coordinator.verification.seal_gate_allows_finish():
                return Disp.CONTINUE
            unverified_release = _unverified_release_active(events)
            final_content = (
                _unverified_release_final_message(events)
                if unverified_release
                else step.thought
            )
            # Record the agent's final message (the answer) before
            # finishing — the deliverable text belongs on the log, not
            # discarded on the finish signal. (When the model just
            # answers a question, this IS the response the UI renders.) An active
            # unverified release replaces the model-authored pre-gate summary: the
            # host owns the final truth and cannot allow a stale "Verified" claim
            # to become the last assistant message.
            if final_content.strip():
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.AGENT,
                        message=LLMMessage(role="assistant", content=final_content),
                        meta=(
                            {
                                "host_owned_terminal_warning": True,
                                "unverified_release": True,
                            }
                            if unverified_release
                            else {}
                        ),
                    )
                )
            # runthru-v2 (#3): "done" is driven by the REAL finish gate —
            # `_stop_allowed` plus the DoD/verify gates already run in
            # `handle_finish_path` (gate_execution_nudge, gate_browser_verify,
            # finish_dod_gate) — NOT by plan_step bookkeeping. EVERY model tier
            # (Qwen 27B through DeepSeek/GPT/Claude) under-reports per-step
            # progress, so gating finish on "all steps marked done" bounced
            # genuinely-complete builds up to the auto-continue cap (≈3×),
            # burning rework (a chunk of the "too slow" complaint) and leaving
            # the run looking half-done. The plan is now an explanation marked
            # done when the build actually finishes; per-step progress (capable
            # models only, via the declarative update_plan_progress tool) is
            # advisory UI and never gates anything. Real incompleteness is still
            # caught by the verify gates above, not by a bookkeeping proxy.
            await self._loop._emit(StatusEvent(status=ConversationStatus.FINISHED))
            return Disp.HALT
        # Constraint 4: the stop-hook veto can refuse FINISHED repeatedly in one
        # run. `_veto_feedback` is host-injectable configuration and cannot carry
        # run state, so the repetition is rendered HERE, at the seam that fires.
        vetoes = 1 + sum(
            1
            for event in events
            if isinstance(event, MessageEvent) and event.meta.get("diagnostic") == "finish_vetoed"
        )
        veto_content = self._loop._veto_feedback
        if vetoes > 1:
            veto_content = (
                f"{veto_content}\n<system-reminder>\nThis is veto {vetoes} of this "
                f"run: {vetoes - 1} earlier `finish` attempt(s) were refused by the "
                "same stop hook. Repeating the same finish will be refused again — "
                "change the deliverable, not the attempt.\n</system-reminder>"
            )
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=veto_content),
                meta={"diagnostic": "finish_vetoed"},
            )
        )
        return Disp.CONTINUE

    async def handle_finish_path(
        self, step: AgentStep, state: ConversationState, events: list[Event]
    ) -> Disp:
        disp = await self._coordinator.gate_execution_nudge(step, events)
        if disp is Disp.CONTINUE:
            return Disp.CONTINUE
        if disp is Disp.HALT:
            return Disp.HALT

        events = await self._loop._events()
        if not await self._coordinator.dictated_content_gate_passed(events):
            return Disp.CONTINUE

        events = await self._loop._events()
        disp = await self._coordinator.verification.run_finish_verify_gates(step, events)
        if disp is Disp.CONTINUE:
            return Disp.CONTINUE
        if disp is Disp.HALT:
            # W-45 loop breaker: a verify gate marked the run STUCK (same failure
            # fingerprint with no productive edit) and already emitted the terminal
            # status — propagate the halt so the engine exits the loop.
            return Disp.HALT
        events = await self._loop._events()

        disp = await self.finalize_finish(step, state, events)
        if disp is Disp.CONTINUE:
            return Disp.CONTINUE
        if disp is Disp.HALT:
            return Disp.HALT
        return Disp.FALLTHROUGH

    async def synthetic_finish_after_actionless_pauses(
        self, state: ConversationState, events: list[Event]
    ) -> Disp:
        """REL-RC-P — synthesize a finish call after repeated actionless pauses.

        This emits an explicit host reminder and durable marker, then routes a
        synthetic ``finish(summary=...)`` through the same two-stage path a real
        model finish call uses: ``normalize_finish_step`` followed by
        ``handle_finish_path``. Gate refusal therefore lands the existing concrete
        reminder and returns CONTINUE; gate success lands FINISHED normally.
        """
        summary = signals.latest_agent_prose_message(events) or "work complete"
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        "REL-RC-P SYNTHETIC FINISH: host is attempting completion: "
                        "work appears done and the loop has paused actionless "
                        f"{signals.actionless_pause_count_current_execution_segment(events)} "
                        "time(s) in this execution segment. Routing a synthetic "
                        "finish(summary=...) through the normal finish gates; any "
                        "refusal below is authoritative and should be fixed before "
                        "finishing.\n"
                        "</system-reminder>"
                    ),
                ),
            )
        )
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail=signals.SYNTHETIC_FINISH_ATTEMPT_DETAIL,
            )
        )
        step = AgentStep(tool_call=ToolCall(tool_name="finish", arguments={"summary": summary}))
        step, disp = await self.normalize_finish_step(step, await self._loop._events())
        if disp is Disp.CONTINUE:
            return Disp.CONTINUE
        if disp is Disp.HALT:
            return Disp.HALT
        return await self.handle_finish_path(step, state, await self._loop._events())
