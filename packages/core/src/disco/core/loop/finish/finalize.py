"""Finish-step normalization, finalization, and synthetic finish valve."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass

from ..stuck import successful_mutation_with_receipt
from .common import (
    _APP_VERIFY_PREFIX,
    _FINISH_SEAL_CAP,
    _FINISH_VERIFY_CAP,
    _LOG,
    _STATIC_VERIFY_PREFIX,
    ActionEvent,
    AgentStep,
    ConversationState,
    ConversationStatus,
    Disp,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
    ToolCall,
    VerifierVerdictEvent,
    _app_verify_command,
    _FinishGateProto,
    _static_verify_command,
    predicate_fingerprints,
    signals,
)


@dataclass(frozen=True)
class _ResolvedVerify:
    """Typed result of resolving a finish verify directive.

    When *ignored_reason* is set the optional model-owned verifier was
    rejected at the validation boundary — *command* is empty and a
    structured environment reminder must be emitted by the caller.
    """

    command: str
    ignored_reason: str | None = None


_UNVERIFIED_RELEASE_FINAL_MESSAGE = (
    "⚠ UNVERIFIED FINAL RESULT: Required host/browser verification did not pass "
    "or could not run. Browser rendering remains UNVERIFIED, and the deliverable "
    "may be INCOMPLETE. Any model-authored verification claim is superseded by "
    "this host-owned result."
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


class _FinalizeMixin(_FinishGateProto):
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
                _url = await self._detect_preview_url() or ""
                if not _url:
                    return _ResolvedVerify(command=_static_verify_command("index.html"))
            return _ResolvedVerify(command=_app_verify_command(_url))
        return _ResolvedVerify(command=verify_cmd)

    async def normalize_finish_step(
        self, step: AgentStep, events: list[Event]
    ) -> tuple[AgentStep, Disp]:
        # VERIFY-ON-FINISH (post-condition gate). If the agent attached
        # a `verify` check to finish, RUN it first and refuse the finish
        # if it doesn't pass — the "run the tests before you claim done"
        # forcing function. The check is visible in the trace; on
        # failure the agent sees exactly what broke and adapts, instead
        # of declaring a broken build complete.
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
            # An already-failing authoritative plan gate comes before repeated
            # optional model verification when no trusted repair has landed.
            if await self._preflight_failed_plan_verifier(events):
                return step, Disp.CONTINUE

            if await self._reuse_finish_verify_receipt(verify_cmd, events):
                passed, malformed = True, False
            else:
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

    async def _reuse_finish_verify_receipt(self, verify_cmd: str, events: list[Event]) -> bool:
        """Reuse one exact, immediately preceding executor-backed shell result.

        When the most recent ActionEvent of any tool matches the resolved
        verify command exactly and its correlated ObservationEvent succeeded,
        and no ``plan_verifier_failure`` intervened after that observation,
        skip re-execution and emit an audit marker.
        """
        most_recent_action: ActionEvent | None = None
        for ev in reversed(events):
            if isinstance(ev, ActionEvent):
                most_recent_action = ev
                break
        if most_recent_action is None:
            return False

        if most_recent_action.tool_call.tool_name != "shell":
            return False
        if most_recent_action.tool_call.arguments.get("command") != verify_cmd:
            return False

        # The receipt must be the one-and-only correlated observation after
        # the action.  An earlier, duplicate, or mismatched record is not an
        # executor receipt for this action and cannot authorize reuse.
        action_index = events.index(most_recent_action)
        correlated = [
            ev
            for ev in events[action_index + 1 :]
            if isinstance(ev, ObservationEvent) and ev.action_id == most_recent_action.id
        ]
        if len(correlated) != 1:
            return False
        observation = correlated[0]
        result = observation.tool_result
        if not (
            result.call_id == most_recent_action.tool_call.call_id
            and result.tool_name == "shell"
            and result.success
        ):
            return False

        observation_index = events.index(observation)
        for ev in events[observation_index + 1 :]:
            if isinstance(ev, StatusEvent) and ev.plan_verifier_failure is not None:
                return False

        fingerprint = hashlib.sha256(verify_cmd.encode()).hexdigest()
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        "Finish verify receipt reused from prior successful "
                        "shell execution — the correlated observation's success "
                        "is carried forward without re-executing the command.\n"
                        "</system-reminder>"
                    ),
                ),
                meta={
                    "finish_verify_receipt_reused": True,
                    "prior_action_id": most_recent_action.id,
                    "command_fingerprint_sha256": fingerprint,
                },
            )
        )
        return True

    async def _preflight_failed_plan_verifier(self, events: list[Event]) -> bool:
        """Recheck an unchanged failing plan gate before optional verification.

        Find the currently approved plan predicate fingerprints. If the same
        predicate set has a prior typed ``plan_verifier_failure`` and no
        successful trusted mutation receipt after the latest such failure, call
        ``finish_dod_gate_passed`` first. If it returns False, return True so
        the caller returns ``Disp.CONTINUE`` before the optional verifier.

        Returns True when the optional shell verifier must be skipped (the
        caller should return CONTINUE).
        """
        plan = signals.latest_approved_plan(events)
        if plan is None:
            return False
        plan_predicates = [
            step.done_condition for step in plan.steps if step.done_condition is not None
        ]
        if not plan_predicates:
            return False
        current_predicate_fps = predicate_fingerprints(plan_predicates)

        latest_failure_seq: int | None = None
        for ev in reversed(events):
            if isinstance(ev, StatusEvent) and ev.plan_verifier_failure is not None:
                if ev.plan_verifier_failure.predicate_fingerprints == current_predicate_fps:
                    latest_failure_seq = ev.seq
                    break
        if latest_failure_seq is None:
            return False

        action_by_id = {
            ev.id: ev for ev in events if isinstance(ev, ActionEvent) and ev.tool_call is not None
        }
        for ev in events:
            if (ev.seq or 0) <= latest_failure_seq or not isinstance(ev, ObservationEvent):
                continue
            action = action_by_id.get(ev.action_id) if ev.action_id is not None else None
            if successful_mutation_with_receipt(ev, action):
                return False

        if not await self.finish_dod_gate_passed():
            return True

        return False

    async def seal_gate_allows_finish(self) -> bool:
        """REL-27 (F-27) — finish-time workspace sealability gate.

        Called immediately before EVERY affirmative FINISHED emission: the
        finish battery's finalize, the completed-via-notify path, and the
        browserless honest-static valve. Non-delivery terminals deliberately
        bypass it — forced ``noop_limit`` and the workflow-control
        ``workflow_skipped`` finish — because neither claims a deliverable
        workspace, and the post-terminal honestly-unsealed disclosure remains
        their backstop. (A future workflow surface that DOES ship files must
        route through an affirmative site or add its own gate call.)

        Semantics:
          • no probe injected → True (legacy byte-identical);
          • probe sealable → True (streak reset);
          • probe blocking, streak < cap → emit a refusal naming the EXACT
            blocking entries and the remedy, return False (caller CONTINUEs —
            the constraint reaches the model BEFORE the last point it can act,
            unlike the post-terminal persistence note that F-27 exposed);
          • probe blocking, streak ≥ cap → loud ``unsealed_release`` marker +
            visible warning, then True: the commit-time strict seal will still
            refuse and disclose. Breaking the loop must not fabricate a seal;
          • probe error/timeout → True with a log line. The probe is an
            affordance; the commit-time seal remains the sole publication
            authority, and a broken probe must never cage a valid terminal.

        The default ``finish_seal_timeout_s`` (150 s) deliberately exceeds the
        Podman workspace-export timeout (120 s) so the probe reaches a verdict
        — or the inner export's own timeout — rather than silently
        self-disabling on exactly the large workspaces where an unsealable
        finish matters most.
        """
        probe = self._loop._finish_sealability_probe
        if probe is None:
            return True
        try:
            result = await asyncio.wait_for(probe(), timeout=self._loop._finish_seal_timeout_s)
        except Exception:  # noqa: BLE001 — advisory probe; seal authority is at commit
            _LOG.warning(
                "finish sealability probe failed; the commit-time seal remains authoritative",
                exc_info=True,
            )
            return True
        if result.sealable:
            self._loop._finish_seal_refusals = 0
            return True
        blocking = [str(entry) for entry in result.blocking]
        shown = "\n".join(f"  - {entry}" for entry in blocking[:8])
        if len(blocking) > 8:
            shown += f"\n  … and {len(blocking) - 8} more"
        if self._loop._finish_seal_refusals < _FINISH_SEAL_CAP:
            self._loop._finish_seal_refusals += 1
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=(
                            "<system-reminder>\n"
                            "You called finish, but the final workspace cannot be "
                            "sealed for delivery — these entries would be missing "
                            "from the saved/exported project:\n"
                            f"{shown}\n"
                            "The live preview resolves them, but only regular, "
                            "uniquely-linked files are preserved durably. Replace "
                            "each with real content (for a symlink: delete the "
                            "link and copy its target into place, e.g. "
                            "`rm <link> && cp -r <target> <link-path>`), then "
                            "finish again.\n"
                            "</system-reminder>"
                        ),
                    ),
                    # `blocking` follows the loop-wide meta convention: a STRING
                    # reason tag (signals.py treats any such message as a typed
                    # blocking proof obligation). The entry list rides under its
                    # own key.
                    meta={
                        "blocking": "finish_seal_refused",
                        "seal_blocking": blocking[:32],
                    },
                )
            )
            return False
        # Cap reached: LOUD release — mirror the verify/browser valves. The
        # terminal stays reachable; the unsealed truth stays visible (marker +
        # human-facing warning now, strict-seal refusal + typed persistence
        # disclosure at commit).
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="unsealed_release",
            )
        )
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "⚠ Finished with an UNSEALABLE workspace after "
                        f"{self._loop._finish_seal_refusals} refusals — these "
                        "entries cannot be preserved in the durable project:\n"
                        f"{shown}\n"
                        "The saved/exported project will be missing them; "
                        "review the workspace."
                    ),
                ),
                meta={
                    "unsealed_release": True,
                    "seal_blocking": blocking[:32],
                },
            )
        )
        self._loop._finish_seal_refusals = 0
        return True

    async def finalize_finish(
        self, step: AgentStep, state: ConversationState, events: list[Event]
    ) -> Disp:
        if await self._loop._stop_allowed(state, events):
            if not await self.seal_gate_allows_finish():
                return Disp.CONTINUE
            unverified_release = _unverified_release_active(events)
            final_content = (
                _UNVERIFIED_RELEASE_FINAL_MESSAGE if unverified_release else step.thought
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

        events = await self._loop._events()
        if not await self.dictated_content_gate_passed(events):
            return Disp.CONTINUE

        events = await self._loop._events()
        disp = await self.run_finish_verify_gates(step, events)
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
                        "work appears done and the loop has paused actionless twice. "
                        "Routing a synthetic finish(summary=...) through the normal "
                        "finish gates; any refusal below is authoritative and should "
                        "be fixed before finishing.\n"
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
