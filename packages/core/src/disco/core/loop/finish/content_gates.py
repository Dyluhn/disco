"""Definition-of-Done, dictated-content, and execution-nudge gates."""

from __future__ import annotations

from .common import *
from .common import (
    _DICTATED_CONTENT_REFUSAL_CAP,
    _DOD_REFUSAL_CAP,
    _EXECUTION_NUDGE,
    _EXECUTION_NUDGE_CAP,
    _FinishGateProto,
    _DoDWorkspaceUnavailable,
    _LOG,
    _deliverable_event_paths,
    _is_web_deliverable,
    _latest_plan_revision,
    _plan_file_exists_paths,
    _safe_deliverable_file_path,
)


class _ContentGateMixin(_FinishGateProto):
    def _contract_required_deliverable_paths(self) -> list[str]:
        """Best-effort bridge from a contract finalizer alias to required files.

        The loop only stores the finalizer alias, not the whole contract. When
        present, match it against the registry and reuse the contract's required
        files as primary deliverable candidates. No alias/no match is inert.
        """

        alias = getattr(self._loop, "_finish_alias", None)
        if not alias:
            return []
        try:
            from ...contract.registry import BuildContractRegistry

            reg = BuildContractRegistry.default()
            out: list[str] = []
            for kind in reg.kinds():
                c = reg.get(kind)
                if c is None or c.verify.finalizer != alias:
                    continue
                for p in c.artifact.required_files:
                    safe = _safe_deliverable_file_path(p)
                    if safe is not None:
                        out.append(safe)
            return out
        except Exception:  # noqa: BLE001 — finish gate degrades to other path sources
            return []

    async def _dictated_content_deliverable_paths(self, events: list[Event]) -> list[str]:
        """Primary deliverable files for dictated-content checking.

        Sources are deliberately narrow: files named by Plan/DoD/contract
        deliverable declarations, explicit handoff events, plus the existing web
        finish-gate convention that an index.html write makes a static web
        deliverable. This avoids scanning arbitrary workspace files.
        """

        paths: list[str] = []
        paths.extend(_plan_file_exists_paths(events))
        paths.extend(_deliverable_event_paths(events))
        spec = await self._loop.store.get_dod_spec(self._loop.conversation_id)
        if spec is not None:
            for pred in spec.predicates:
                if isinstance(pred, FileExistsPredicate):
                    p = _safe_deliverable_file_path(pred.path)
                    if p is not None:
                        paths.append(p)
        paths.extend(self._contract_required_deliverable_paths())
        if _is_web_deliverable(events):
            paths.append("index.html")

        out: list[str] = []
        seen: set[str] = set()
        for path in paths:
            norm = posixpath.normpath(path)
            if norm in seen:
                continue
            seen.add(norm)
            out.append(norm)
        return out

    async def _read_deliverable_bytes(self, path: str) -> bytes | None:
        """Read one deliverable file, returning None only when no host/sandbox
        read surface is available. Missing/empty files return b"" so the content
        condition fails loudly against the named file."""

        sbx = getattr(self._loop.executor, "sandbox", None)
        if sbx is not None and hasattr(sbx, "read_file"):
            try:
                data = await sbx.read_file(path)
            except FileNotFoundError:
                return b""
            except Exception as exc:  # noqa: BLE001 — gate is best-effort if read infra breaks
                _LOG.warning(
                    "dictated-content read failed for %s:%s via sandbox: %s",
                    self._loop.conversation_id,
                    path,
                    exc,
                )
                return b""
            if isinstance(data, bytes):
                return data
            return str(data).encode("utf-8", "surrogatepass")

        workspace = getattr(sbx, "workspace_path", None) if sbx is not None else None
        if not workspace:
            return None
        from pathlib import Path

        try:
            root = Path(workspace).resolve()
            candidate = (root / path).resolve()
            root_s = str(root)
            cand_s = str(candidate)
            if not (cand_s == root_s or cand_s.startswith(root_s.rstrip("/") + "/")):
                return b""
            if not candidate.is_file():
                return b""
            return candidate.read_bytes()
        except OSError as exc:
            _LOG.warning(
                "dictated-content read failed for %s:%s from workspace: %s",
                self._loop.conversation_id,
                path,
                exc,
            )
            return b""

    async def _first_dictated_content_miss(
        self,
        conditions: list[DictatedContentCondition],
        paths: list[str],
    ) -> tuple[DictatedContentCondition, list[str]] | None:
        readable = False
        contents: list[tuple[str, bytes]] = []
        for path in paths:
            data = await self._read_deliverable_bytes(path)
            if data is None:
                continue
            readable = True
            contents.append((path, data))
        if not readable:
            _LOG.warning(
                "dictated-content conditions present for %s, but no readable "
                "deliverable file surface is available; skipping gate.",
                self._loop.conversation_id,
            )
            return None

        for cond in conditions:
            needle = cond.literal.encode("utf-8", "surrogatepass")
            if any(needle in data for _, data in contents):
                continue
            checked = [path for path, _ in contents] or paths
            return cond, checked
        return None

    async def dictated_content_gate_passed(self, events: list[Event]) -> bool:
        """REL-RC-O finish gate: quoted user literals must be present verbatim.

        Conditions are reconstructed from USER messages bound to PlanEvent
        revisions. The current revision inherits all prior revisions, so a later
        phase cannot silently drop content quoted earlier in the conversation.
        """

        if not self._loop._planning_tools or self._loop.mode == OperatingMode.PLANNING:
            return True
        current_revision = _latest_plan_revision(events)
        if current_revision is None:
            return True
        conditions = [
            c
            for c in dictated_content_conditions_from_events(events)
            if c.revision <= current_revision
        ]
        if not conditions:
            self._loop._dictated_content_refusals = 0
            return True
        paths = await self._dictated_content_deliverable_paths(events)
        if not paths:
            return True

        miss = await self._first_dictated_content_miss(conditions, paths)
        if miss is None:
            self._loop._dictated_content_refusals = 0
            return True

        cond, checked_paths = miss
        file_word = "file" if len(checked_paths) == 1 else "files"
        files = ", ".join(f"`{p}`" for p in checked_paths)
        literal = cond.literal

        if self._loop._dictated_content_refusals >= _DICTATED_CONTENT_REFUSAL_CAP:
            if self._loop._dictated_content_refusals == _DICTATED_CONTENT_REFUSAL_CAP:
                await self._loop._emit(
                    StatusEvent(
                        status=ConversationStatus.RUNNING,
                        detail="dictated_content_release",
                    )
                )
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "⚠ Finished despite missing dictated content after "
                                f"{self._loop._dictated_content_refusals} refusals: "
                                f"literal {literal!r} from plan revision {cond.revision} "
                                f"still was not found in deliverable {file_word} {files}. "
                                "Releasing the finish gate to avoid an unbounded loop; "
                                "the deliverable may fail content review."
                            ),
                        ),
                    )
                )
                self._loop._dictated_content_refusals += 1
            return True

        self._loop._dictated_content_refusals += 1
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        "You called finish, but a quoted user literal is missing "
                        f"from the deliverable {file_word} {files}: {literal!r}.\n\n"
                        f"This literal was dictated in the user instruction for plan "
                        f"revision {cond.revision} and is carried forward into the "
                        "current revision. The match is case-sensitive and exact. "
                        "Update the deliverable so it contains that exact text, then "
                        "finish again.\n"
                        "</system-reminder>"
                    ),
                ),
            )
        )
        return False

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
        # INFRA-vs-TASK release (DoD v2.1): if EVERY unmet predicate is UNVERIFIABLE — the
        # check could not be RUN (a hard-denied command, an unprobeable/not-yet-serving URL,
        # egress denied) — do NOT block the finish on infra noise. Only a real TASK failure
        # (a command that RAN and exited wrong; a server that SERVED the wrong status) is a
        # genuine "not done". This is what lets `command`/`http_ok` predicates gate safely.
        failed = [r for r in verdict.results if not r.passed]
        task_failures = [r for r in failed if not getattr(r, "unverifiable", False)]
        if not task_failures:
            _LOG.info(
                "DoD for %s: %d unmet predicate(s), ALL unverifiable (infra, not a task "
                "verdict) — releasing the finish gate rather than blocking on a check that "
                "could not run.",
                self._loop.conversation_id,
                len(failed),
            )
            if failed:
                # HONEST-INCOMPLETE surface (the research's UNVERIFIED third state): we are
                # allowing finish, but the run did NOT verify these acceptance checks. Record
                # an ADVISORY note (visible, non-blocking, NOT a refusal) so the user/audit
                # sees "finished, but couldn't confirm X" instead of a silent clean pass.
                unverified = "\n".join(
                    f"  - {r.predicate!r}\n      could not verify: {r.reason}" for r in failed
                )
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                f"<note>\nFinished, but {len(failed)} acceptance check(s) "
                                "could NOT be verified (the check could not run — e.g. a "
                                "denied command or a server that was not serving). These are "
                                "NOT failures, but they were NOT confirmed either:\n\n"
                                f"{unverified}\n</note>"
                            ),
                        ),
                        meta={"advisory": "dod_unverified_at_finish"},
                    )
                )
            self._loop._dod_refusals = 0
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
            if result.passed or result.unverifiable:
                # Skip passes AND unverifiable (infra) failures — name only the actionable
                # TASK failures the agent can actually fix (we only reach here BECAUSE there
                # is at least one). An infra failure named here would mislead the model into
                # "fixing" something that simply could not be checked.
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
            # inspect.iscoroutine is a TypeGuard (narrows only the positive
            # branch), so the awaited-away Coroutine lingers in the static type;
            # at runtime `result` is always the resolved DoDEvaluator here.
            return cast(DoDEvaluator, result)
        sbx = getattr(self._loop.executor, "sandbox", None)
        workspace = getattr(sbx, "workspace_path", None) if sbx is not None else None
        if not workspace:
            raise _DoDWorkspaceUnavailable(
                f"no sandbox.workspace_path on executor {type(self._loop.executor).__name__}"
            )
        from pathlib import Path

        # An `http_ok` serve-check must probe from INSIDE the sandbox: a build's dev
        # server binds the SANDBOX's localhost, not the host's. The default host-side
        # urlopen would hit the agent-server (404 on `/`), false-failing every sandboxed
        # serve and spinning the model into re-serve/re-verify loops. Route the probe
        # through `exec_shell` + curl when the backend supports it; otherwise fall back
        # to the default host probe (e.g. the process backend shares the host network).
        http_probe = None
        if sbx is not None and hasattr(sbx, "exec_shell"):
            import shlex

            async def _in_sandbox_http_probe(
                url: str, expected_status: int
            ) -> HttpProbeResult:
                cmd = (
                    "curl -s -o /dev/null -w '%{http_code}' --max-time 10 "
                    + shlex.quote(url)
                )
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
                            f"sandbox curl produced no HTTP status "
                            f"(got {out!r}, exit {res.exit_code})"
                        ),
                    )
                return HttpProbeResult(status_code=int(out))

            http_probe = _in_sandbox_http_probe

        return DoDEvaluator(Path(workspace), http_probe=http_probe)

    async def gate_execution_nudge(self, step: AgentStep, events: list[Event]) -> Disp:
        # PLAN-MODE EXECUTION GATE — a forcing function, NOT a prompt. If
        # the loop is in execution mode (planning_tools configured) and the
        # agent declares "done" without any productive action since plan
        # approval, refuse the finish: append an IMPLICIT system-reminder
        # and re-enter the loop. W5: capped at _EXECUTION_NUDGE_CAP (3) for
        # parity with the other finish-path gates — after cap the gate
        # RELEASES with a visible warning rather than running forever.
        if (
            self._loop._planning_tools  # plan-first lifecycle is configured
            and self._loop.mode != OperatingMode.PLANNING  # we're executing
            and not signals.productive_action_since_approval(events)
        ):
            # W5 cap: after _EXECUTION_NUDGE_CAP nudges without productive
            # action, TERMINALIZE the run as a FAILURE — NOT a false FINISHED.
            # Spec §11.2: after approval, execution must produce ≥1 action OR
            # a terminal explicit failure. A plan that is approved but never
            # executed is the latter, so land STUCK (bounded no-progress /
            # model-stall — same family as the other no-progress terminals;
            # ERROR is reserved for thrown exceptions). The cap is the
            # terminal exit for this path: HALT so the engine exits the loop
            # and `get_state()` surfaces STUCK/approve_plan_no_execution.
            if self._loop._execution_nudges >= _EXECUTION_NUDGE_CAP:
                _LOG.warning(
                    "execution-nudge cap (%d) reached for %s — plan approved but "
                    "never executed; terminalizing STUCK (approve_plan_no_execution).",
                    _EXECUTION_NUDGE_CAP,
                    self._loop.conversation_id,
                )
                await self._loop._land_blocked(
                    reason="approve_plan_no_execution",
                    guidance=(
                        "Plan approved but no execution action was taken after "
                        f"{self._loop._execution_nudges} execution reminders. "
                        "The plan was not executed."
                    ),
                    legacy_status=ConversationStatus.STUCK,
                    legacy_detail="approve_plan_no_execution",
                )
                return Disp.HALT

            self._loop._execution_nudges += 1  # telemetry + cap counter
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
            # The execution-nudge cap (_EXECUTION_NUDGE_CAP) is the
            # backstop for this path: after N nudges the gate emits
            # FINISHED and halts above. The old noop valve call has
            # been removed — it fired at _ACTIONLESS_BREAK_CAP (3),
            # same count as the cap, and would PAUSED the run before
            # the cap's FINISHED release could land (W5 fix).
            return Disp.CONTINUE
        # Productive action found — reset the nudge streak so a fresh
        # plan-approval cycle gets a full _EXECUTION_NUDGE_CAP budget.
        self._loop._execution_nudges = 0
        return Disp.FALLTHROUGH
