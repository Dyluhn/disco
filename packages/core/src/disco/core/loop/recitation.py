"""Recitation cadence (C6) + scheduled re-grounding (HS-03) + MEMORY write-through.

Extracted from engine.py as a stateful collaborator: `RecitationRegrounder`
holds a back-reference to its `AgentLoop` and reads/writes the loop's cadence
counters (`_recitation_*`, `_hs03_reground_*`) and emits through the loop's
primitives. Method bodies are byte-identical to the former AgentLoop methods,
with `self.` mechanically rewritten to `self._loop.`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..events import (
    ActionEvent,
    Event,
    EventSource,
    KnowledgeEvent,
    MessageEvent,
)
from ..view import View, _latest_plan
from . import signals
from .messages import _hs03_reground_message

if TYPE_CHECKING:
    from .engine import AgentLoop

# The view.py tag for the tail recitation. We look at the last rendered
# message to decide whether to keep it — if it starts with this sentinel it
# IS the recap, otherwise there is no plan to recite yet (no recap to gate).
_RECITATION_SENTINEL = "<current-objective>"


class RecitationRegrounder:
    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    def recitation_signature(self, events: list[Event]) -> str | None:
        """C6 — stable signature of (latest plan + plan_step checklist), the
        inputs the view.py tail-recap renders. None iff there is no plan to
        recite (a re-plan or a pre-plan run has no signature).

        Mirrors view.py:_recitation_message's accounting exactly: only
        plan_step marks AFTER the current PlanEvent's seq count, and the
        state set is split into done vs active (the recap's "✓" / "→" /
        "□" markers). Any change in plan.summary, plan.steps, or the
        done/active set flips the signature → DRIFT fires the next
        materialization (once).

        Returned as a string (not bytes) so the comparison in
        `_should_emit_recitation` is cheap and order-insensitive (sorted
        tuples). Hashing is unnecessary — the content is short and we
        never re-hash for any other purpose.
        """
        plan = _latest_plan(events)
        if plan is None or not plan.steps:
            return None
        plan_seq = plan.seq or 0
        done: list[int] = []
        active: list[int] = []
        for e in events:
            if not isinstance(e, ActionEvent) or e.tool_call is None:
                continue
            if e.tool_call.tool_name != "plan_step":
                continue
            if (e.seq or 0) < plan_seq:
                continue  # mark belongs to a superseded plan
            try:
                idx = int(e.tool_call.arguments.get("index"))  # type: ignore[arg-type]
                state = str(e.tool_call.arguments.get("state"))
            except (TypeError, ValueError):
                continue
            if state == "done":
                done.append(idx)
            elif state == "active":
                active.append(idx)
        steps_t = tuple((s.title, s.detail) for s in plan.steps)
        return (
            f"plan:{plan.id}:{plan.revision}:{plan.summary}|"
            f"steps:{steps_t}|"
            f"done:{sorted(done)}|active:{sorted(active)}"
        )

    def should_emit_recitation(self, events: list[Event]) -> bool:
        """C6 — return True iff the view.py tail-recap should be appended on
        THIS step. Pure predicate (no mutation, no logging); the side
        effects (signature update, step counter increment) live in
        `_materialize_view` so a test can call this in isolation.

        Fires when ANY of:
        - The plan is new or has changed since the last recap (DRIFT). The
          first eligible step ALWAYS drifts (signature is None).
        - The current step is a cadence boundary. The step counter is
          1-indexed by the time we get here (incremented in
          `_materialize_view`); the smolagents math is
          `(step_number - 1) % interval == 0` → fires on steps 1, 1+N,
          1+2N, … (the first step is always a boundary, regardless of
          cadence value).
        Returns False when no plan exists yet (view.py already returns
        None for the recap; nothing to gate) or when neither signal
        fires (drop the message — the model still sees the underlying
        PlanEvent + plan_step ActionEvents in the prior messages).
        """
        sig = self._loop._recitation_signature(events)
        if sig is None:
            return False
        on_cadence = ((self._loop._recitation_step_count - 1) % self._loop._recitation_cadence) == 0
        drift = sig != self._loop._recitation_last_signature
        return on_cadence or drift

    def gate_recitation(self, view: View, events: list[Event]) -> View:
        """C6 — drop the tail-recap message unless this step is a cadence
        boundary or the plan/checklist has drifted. The recap CONTENT is
        unchanged (the underlying PlanEvent + plan_step actions remain
        visible earlier in the messages list, so the model never loses
        the goal — just the redundant every-step re-render). No new
        steering text is added; this is a passive recap cadence, not a
        steer (no-automatic-nudge invariant c97c1b3).
        """
        # C6: count the step (0-indexed) — one increment per materialize
        # call, regardless of whether a re-projection happened inside
        # (e.g. after a condensation). That keeps the cadence tied to
        # MODEL TURNS, not View re-materializations.
        self._loop._recitation_step_count += 1
        if not (
            view.messages and view.messages[-1].content.startswith(_RECITATION_SENTINEL)
        ):
            # No tail recap in the rendered View (no plan yet) — nothing
            # to gate. Don't touch the signature: the next step with a
            # plan will drift on signature != None.
            return view
        if self._loop._should_emit_recitation(events):
            # The View already has a tail-recap; the gate fired (cadence
            # boundary or drift). Record the signature so the NEXT step
            # can detect drift.
            self._loop._recitation_last_signature = self._loop._recitation_signature(events)
            return view
        # Gate fires: drop the tail-recap. The model still has the
        # PlanEvent + plan_step events in the prior messages list, so
        # the plan info is not lost — just not redundantly re-rendered.
        return view.model_copy(update={"messages": view.messages[:-1]})

    def should_emit_reground(self, events: list[Event]) -> bool:
        """HS-03 — pure predicate: should this step emit the re-ground
        recap? Fires when EITHER of:
          * POST-RESUME: this is the first eligible step of a fresh run
            segment (actions_since_last_resume == 0) AND the post-resume
            one-shot has not already fired. Mirrors the F4 _bootstrap_emitted
            shape, but is reset on every run() so a resume gets a fresh
            one-shot (the brief is explicit: "ONCE immediately after a
            restart/resume"). The recap is anchored to the fresh
            post-resume state so a model that lost context across the
            pause re-anchors.
          * CADENCE: the current action count is a multiple of
            `self._hs03_reground_cadence` (default
            `_HS03_REGROUND_INTERVAL` = 12; tests inject a smaller
            value) AND we did not already emit at this exact boundary.
            The per-boundary guard
            (`_hs03_reground_last_action_count`) prevents re-firing on
            consecutive steps at the same boundary — e.g. two steps
            in a row both seeing count==12 (a noop interleaved) would
            otherwise emit twice. Only actions since the last resume
            count toward cadence (the brief's wording); a long-running
            session that survives a resume restarts the count.

        Pure predicate: no emission, no side effects on the store. The
        caller is responsible for the actual emit and for updating the
        flags. Returns False when assist is OFF (caller does not need to
        check `self._assist` — this is the gate; the closed-end-to-end
        guarantee lives here), or when the brief's preconditions
        aren't met (no plan yet, no boundary yet, already-fired).

        A no-plan conversation returns False even when the gate
        conditions match: there is no goal to anchor to. The recap is
        a re-ground on the existing plan, not a free-floating
        "remember what we're doing" prompt.
        """
        if not self._loop._assist:
            return False  # closed end-to-end — capable-model default is byte-identical
        if _hs03_reground_message(events) is None:
            return False  # no plan → nothing to recap
        actions = signals.actions_since_last_resume(events)
        if actions == 0 and not self._loop._hs03_reground_post_resume_emitted:
            return True  # first eligible step of a fresh segment
        if actions > 0 and (actions % self._loop._hs03_reground_cadence) == 0:
            # Per-boundary guard: don't re-emit at the same boundary
            # on two consecutive steps. The first time we see a
            # boundary we emit; subsequent steps at the same count
            # are silent.
            return actions != self._loop._hs03_reground_last_action_count
        return False

    async def maybe_emit_reground(self, events: list[Event]) -> list[Event]:
        """HS-03 — gate, build, emit. Returns the (possibly refreshed)
        event list: when an emit fires, the caller should re-poll
        `self._events()` so the materialize step on the same turn
        sees the recap in the View. Mirrors the F4 bootstrap idiom
        (emit + `events = await self._events()`).

        Assist OFF → no-op, the input list is returned unchanged. The
        gate is closed at `_should_emit_reground`; this method is a
        thin wrapper that handles the emit + flag update + event-list
        refresh so the run() call site stays one line.

        Why a MessageEvent (not a separate RecapEvent): the existing
        `MessageEvent` with `source=ENVIRONMENT` + role=user is the
        contract for harness-injected system-reminders across F4
        (bootstrap), F8 (file-write args), C7 (stuck escape), and the
        actionless-valve. A new event type would be a new contract;
        the sentinel-tagged body is enough to identify the recap in
        tests + in the View.

        Why a fresh MessageEvent (not a View-side append like C6): C6
        drops a recap the View ALREADY rendered (the tail-recap is
        built into view.py:_recitation_message and gated at materialize
        time). HS-03 needs a recap that is independent of the View's
        current render — it must persist to the event log so a future
        View materialization includes it, and so tests can assert the
        emit happened (the persisted event IS the test's evidence).
        """
        if not self._loop._should_emit_reground(events):
            return events
        recap = _hs03_reground_message(events)
        if recap is None:  # defensive: the predicate already checked
            return events
        # _actions_since_last_resume is computed again to keep the
        # per-boundary flag precise (the predicate saw the same
        # number; storing it here is the single point of truth).
        actions = signals.actions_since_last_resume(events)
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=recap,
            )
        )
        if actions == 0:
            # Post-resume one-shot fired. Mark so subsequent steps
            # with actions == 0 (e.g. a tool-less first turn) don't
            # re-emit. Cadence still applies once actions > 0.
            self._loop._hs03_reground_post_resume_emitted = True
        # Record the boundary we just emitted at. Even on the
        # post-resume one-shot we record (at count 0) so a step that
        # somehow also hit the cadence condition (count % N == 0
        # trivially when count is 0) doesn't double-fire — though
        # the post-resume flag already prevents that.
        self._loop._hs03_reground_last_action_count = actions
        # Refresh the event list so the materialize step on the same
        # turn sees the recap in the View (F4's idiom: emit then
        # re-poll so the bootstrap is in the next render).
        return await self._loop._events()

    async def write_pmx_memory_fact(self, scope: str, fact: str) -> None:
        """C5 — write-through: append one (scope, fact) pair to `.disco/MEMORY.md`
        in the live sandbox. The file is a MIRROR of the in-View KnowledgeEvent
        channel (which is the authoritative in-session source); the file exists
        only so a hard reset / box wipe can re-read it on the fresh instance.

        Best-effort: a missing sandbox is a no-op (sandbox-less executors in
        tests). The on-disk format is a simple Markdown list grouped by scope
        (`## <scope>` heading + `- <fact>` items), which is human-readable and
        easy to parse on rehydrate. NO deduplication here — the in-View
        `remember` handler already dedupes (see _remember_tool_singleton's
        `seen` set in this file), so the file only ever sees novel facts.
        """
        sbx = getattr(self._loop.executor, "sandbox", None)
        if sbx is None:
            return
        path = self._loop._MEMORY_PATH
        # Read existing content (if any). A missing file means a fresh mirror — but
        # first fall back to the legacy .pmx/ path so a pre-rename workspace's facts
        # migrate forward into the new file on the next write.
        try:
            existing = (await sbx.read_file(path)).decode("utf-8", errors="replace")
        except (FileNotFoundError, NotADirectoryError):
            try:
                existing = (
                    await sbx.read_file(self._loop._LEGACY_MEMORY_PATH)
                ).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 — no legacy mirror either: fresh start
                existing = ""
        except Exception:  # noqa: BLE001 — read flakiness: start clean
            existing = ""
        lines: list[str] = existing.splitlines() if existing.strip() else [
            "# Standing memory",
            "",
            "Durable facts the agent learned this run (C5: write-through mirror "
            "of the in-View KnowledgeEvent channel).",
            "",
        ]
        if scope:
            header = f"## {scope}"
            if header not in lines:
                lines.append("")
                lines.append(header)
                lines.append("")
            # Insert the fact as a list item directly under the scope header.
            idx = lines.index(header)
            lines.insert(idx + 1, f"- {fact}")
        else:
            lines.append("")
            lines.append(f"- {fact}")
        # Trailing newline keeps the file POSIX-clean.
        await sbx.write_file(path, ("\n".join(lines) + "\n").encode("utf-8"))

    async def drain_recovered_memory_facts(self) -> int:
        """C5 — pull recovered facts from the session (set by SandboxSession
        when its post-recreate read-back finds `.pmx/MEMORY.md` on the fresh
        box) and re-emit them as KnowledgeEvents. The View channel is the
        authoritative in-session source, so re-emitting brings the in-memory
        knowledge state back in sync with the surviving on-disk mirror.

        Returns the number of facts re-emitted (0 if the session had no
        recovery data — a fresh box that was never written to, or a
        sandbox-less / fake executor in tests).
        """
        sbx = getattr(self._loop.executor, "sandbox", None)
        take = getattr(sbx, "take_recovered_memory_facts", None) if sbx is not None else None
        if take is None:
            return 0
        try:
            facts = take() or []
        except Exception:  # noqa: BLE001 — recovery is best-effort
            return 0
        count = 0
        for scope, snippet in facts:
            if not snippet:
                continue
            await self._loop._emit(
                KnowledgeEvent(source=EventSource.AGENT, scope=scope, snippet=snippet)
            )
            count += 1
        return count
