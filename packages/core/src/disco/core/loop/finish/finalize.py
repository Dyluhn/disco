"""Finish-step normalization, finalization, and synthetic finish valve."""

from __future__ import annotations

from .common import *
from .common import (
    _APP_VERIFY_PREFIX,
    _FINISH_VERIFY_CAP,
    _FinishGateProto,
    _STATIC_VERIFY_PREFIX,
    _app_verify_command,
    _static_verify_command,
)


class _FinalizeMixin(_FinishGateProto):
    async def resolve_verify_command(self, args: dict) -> str:
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
                    return _static_verify_command("index.html")
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
        assert step.tool_call is not None  # caller (engine loop) enters only on the finish tool
        verify_cmd = await self.resolve_verify_command(step.tool_call.arguments)
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
        step = AgentStep(
            tool_call=ToolCall(tool_name="finish", arguments={"summary": summary})
        )
        step, disp = await self.normalize_finish_step(step, await self._loop._events())
        if disp is Disp.CONTINUE:
            return Disp.CONTINUE
        if disp is Disp.HALT:
            return Disp.HALT
        return await self.handle_finish_path(
            step, state, await self._loop._events()
        )
