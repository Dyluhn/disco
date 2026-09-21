"""ROUTE (B) — DRIVEN COMPOSITION for constraint 4's unreached targets.

GROUNDED FEEDBACK constraint 4 binds every message surface that can fire more
than once per run. Four surfaces were repaired; two live-corpus campaigns (07j:
18 cells; 07l: 24 runs, 9 lanes, 15 scenarios) each confirmed exactly ONE of them
by live reach and each explained why the other three could not be reached:

* `_blocked_prompt` and F53's `governed_contract_refusal` HALT are **the same
  event** — `host_disposition.py`'s HALT branch calls `_land_blocked`, which
  renders through `_blocked_prompt` — and both need a **breaker landing**. No
  landing occurred in 24 runs. The HALT precondition is the same failure
  fingerprint with NO intervening verification authority, and a live agent that
  keeps producing plan verifications never satisfies it: measured in `g2-r1`, the
  same fingerprint `host:4add30e2849ffc1cc9867b31` repeated 4x and the HALT
  branch never fired, because five `plan_verification_passed` events interleaved
  and each advanced `_last_verification_authority_seq`.
* `_plan_nudge` needs a planning-mode no-op; the corpus produced none in 24 runs.

07m Adjudication 1 refined the closure criterion on that measurement: a repaired
target is CONFIRMED by **(A) live reach** *or* **(B) driven composition**, where
(B) requires ALL FOUR of:

  1. a reachability attestation measuring WHY the live corpus does not reach it —
     **already held** by all three targets (07l measured it, 07m reproduced it).
     Cited here, deliberately NOT re-measured;
  2. a deterministic test driving the **real production call chain** into the
     multi-fire condition **twice** — the actual caller reaching the actual
     emitter — with the emitted bodies compared;
  3. a **mutation control** that removes the repair and shows the same test
     failing, so the test's own reach is attested rather than assumed (F52);
  4. a `SCOPE-ATTESTATION:` naming (B), passing `check-discharge-scope.py`.

(B) is a HARDER bar than (A), not an escape from it, and it is unavailable to any
target not first measured unreachable.

# What "driven" means here, and why the existing tests do not already do it

A renderer exercised in isolation does **NOT** satisfy clause 2. That is exactly
`_plan_nudge`'s own 07h defect: a correct renderer with a caller nobody checked.
Neither does a test that hand-builds the "prior firing" event it wants the second
fire to see — that asserts the counter's arithmetic, not the composition.

So every drive below runs on ONE loop instance whose `_events()` returns its own
emitted events, exactly as a store does. The second fire therefore sees the FIRST
FIRE'S OWN EMITTED EVENTS, not a synthetic stand-in, and "fires twice in one run"
is literal rather than simulated.

# Attestation-Binding Invariant (F58)

No production sentence is restated here. The guidance driven into the HALT branch
is DERIVED from `host_claims.handoff_refusal_detail`, and the prefix the branch
composes is DERIVED from `host_disposition._TARGET_REPEAT_HALT_PREFIX`. This file
is a member of the constraint-4 test family policed by
`development/tests/architecture/test_attestation_binding.py`.
"""

from __future__ import annotations

import hashlib

import pytest
from disco.core import (
    ConversationStatus,
    Event,
    EventSource,
    MessageEvent,
    StatusEvent,
)
from disco.core.loop.boundaries import AgentStep
from disco.core.loop.control import Disp
from disco.core.loop.finish.common import _VERIFY_MARKER_PREFIX
from event_fakes import user_msg, with_seqs

# ---------------------------------------------------------------------------
# A loop facet that behaves like a STORE, not like a recorder.
#
# `_events()` returns the log plus everything this loop has emitted, so a second
# fire in the same run sees the first fire's own durable events. Every other
# member is the minimum the real seams touch.
# ---------------------------------------------------------------------------


class _RunLoop:
    def __init__(self, seed: list[Event] | None = None) -> None:
        self.emitted: list[Event] = []
        self._seed: list[Event] = list(seed or [])
        self._autonomous = False

    async def _emit(self, event: Event) -> Event:
        self.emitted.append(event)
        return event

    async def _events(self) -> list[Event]:
        return with_seqs([*self._seed, *self.emitted])

    def env_bodies(self) -> list[str]:
        return [
            e.message.content
            for e in self.emitted
            if isinstance(e, MessageEvent)
            and e.message is not None
            and e.source is EventSource.ENVIRONMENT
        ]


# ---------------------------------------------------------------------------
# TARGET 1+2 — `_blocked_prompt` AND F53's `governed_contract_refusal` HALT.
# One driven breaker landing serves both, because they are one event.
# ---------------------------------------------------------------------------

_FAILURE_KEY = "target:missing_or_foreign_handoff"


def _fingerprint(failure_key: str = _FAILURE_KEY) -> str:
    """The fingerprint `governed_contract_refusal` derives, computed the same way."""
    return "host:" + hashlib.sha256(failure_key.encode()).hexdigest()[:24]


def _marker(fingerprint: str) -> StatusEvent:
    return StatusEvent(
        status=ConversationStatus.RUNNING,
        detail=f"{_VERIFY_MARKER_PREFIX}{fingerprint}",
    )


def _halt_precondition_seed(fingerprint: str) -> list[Event]:
    """The state in which the HALT branch — not the CONTINUE branch — fires.

    `_prior_verify_marker_fp(events, authority_seq)` must equal the current
    fingerprint: the same governed failure already marked, with NO intervening
    verification authority. The USER message sets the authority floor and the
    marker sits ABOVE it, which is the loop breaker's own question.

    This is the precondition 07l measured as unreachable in 24 live runs — an
    agent that keeps producing plan verifications advances the floor past the
    marker on every pass and never lands here.
    """
    return with_seqs([user_msg("ship the target"), _marker(fingerprint)])


class _RealLandingLoop(_RunLoop):
    """A run loop whose `_land_blocked` reaches the REAL landing seam.

    This is the whole point of clause 2. The existing constraint-4 fakes record
    `_land_blocked(**kwargs)` and stop there, so the HALT branch's caller was
    never shown reaching `_blocked_prompt` at all. Here `_land_blocked` delegates
    exactly as `loop_runtime.LoopRuntime._land_blocked` does — "delegate all
    breaker dead-ends to the shared explain+ask lander" — so the composition
    under test is the production one.
    """

    def __init__(self, seed: list[Event] | None = None) -> None:
        super().__init__(seed)
        from disco.core.loop.valve_landing import ValveLandingMixin

        class _Landing(ValveLandingMixin):
            def __init__(self, loop: _RealLandingLoop) -> None:
                self._loop = loop  # type: ignore[assignment]

        self._valve = _Landing(self)

    async def _land_blocked(
        self,
        *,
        reason: str,
        guidance: str = "",
        legacy_status: ConversationStatus = ConversationStatus.STUCK,
        legacy_detail: str | None = None,
        extra_meta: dict[str, str | int] | None = None,
    ) -> None:
        await self._valve.land_blocked(
            reason=reason,
            guidance=guidance,
            legacy_status=legacy_status,
            legacy_detail=legacy_detail,
            extra_meta=extra_meta,
        )


class _RefusalGate:
    """The gate facet `governed_contract_refusal`'s HALT branch touches."""

    def __init__(self, loop: _RealLandingLoop) -> None:
        self._loop = loop


def _driven_guidance() -> str:
    """The guidance the real upstream caller hands the refusal, DERIVED.

    `render_sequence._render_verify_missing_handoff_disposition` composes
    `handoff_refusal_detail(handoff, contract)` into the refusal's guidance; with
    no handoff at all that describer returns the exact sentence F53's measured
    instance carried at `p4_ff_node_restart@99603` seqs 238/289. Derived from the
    describer rather than restated (F58).
    """
    from disco.core.loop.finish.verify_gate_parts.host_claims import (
        handoff_refusal_detail,
    )

    return handoff_refusal_detail(None, None)


async def _fire_refusal(loop: _RealLandingLoop, guidance: str) -> Disp:
    """One trip through the REAL chain, from the real caller's entry point.

    `governed_contract_refusal` reads the events the caller hands it, exactly as
    `render_sequence` does (`events = await gate._loop._events()`).
    """
    from disco.core.loop.finish.verify_gate_parts.host_disposition import (
        governed_contract_refusal,
    )

    return await governed_contract_refusal(
        _RefusalGate(loop),
        await loop._events(),
        failure_key=_FAILURE_KEY,
        guidance=guidance,
    )


async def test_route_b_halt_landing_fires_twice_in_one_run_and_does_not_repeat():
    """ROUTE (B) CLAUSE 2 — `_blocked_prompt` + F53's HALT, one driven landing.

    The actual caller (`governed_contract_refusal`) reaches the actual emitter
    (`_blocked_prompt`, via `_land_blocked` -> `land_blocked`) TWICE in one run,
    with the second fire reading the first fire's own emitted events, and the two
    emitted bodies are compared.
    """
    guidance = _driven_guidance()
    loop = _RealLandingLoop(_halt_precondition_seed(_fingerprint()))

    first_disp = await _fire_refusal(loop, guidance)
    second_disp = await _fire_refusal(loop, guidance)

    # The HALT branch really was taken, twice — not the CONTINUE branch.
    assert first_disp is Disp.HALT, "fire 1 did not reach the HALT branch"
    assert second_disp is Disp.HALT, "fire 2 did not reach the HALT branch"

    bodies = loop.env_bodies()
    assert len(bodies) == 2, (
        f"expected exactly two landing prompts from two landings, got {len(bodies)}"
    )
    first, second = bodies

    assert first != second, (
        "the SAME breaker landing rendered byte-identically twice in one run — "
        "this is F53's measured defect at p4_ff_node_restart@99603 seqs 238/289, "
        "where `governed_contract_refusal`'s authored text rode the unrepaired "
        "landing seam and reached the agent unchanged"
    )
    assert "this same blocked context" in second, (
        "the escalation must name the HANDED text, which is the specific fact "
        "F53 says the agent was never told"
    )


async def test_route_b_halt_landing_preserves_the_authored_fact_on_both_fires():
    """Constraint 1 is not traded for constraint 4: the composed guidance — the
    HALT prefix plus the describer's fact — survives BOTH fires intact."""
    from disco.core.loop.finish.verify_gate_parts.host_disposition import (
        _TARGET_REPEAT_HALT_PREFIX,
    )

    guidance = _driven_guidance()
    loop = _RealLandingLoop(_halt_precondition_seed(_fingerprint()))
    await _fire_refusal(loop, guidance)
    await _fire_refusal(loop, guidance)

    composed = f"{_TARGET_REPEAT_HALT_PREFIX}{guidance}"
    for index, body in enumerate(loop.env_bodies()):
        assert composed in body, (
            f"fire {index + 1} lost the authored guidance the HALT branch "
            f"composes; repetition-awareness is ADDED at the emitting seam, "
            f"never traded against the upstream surface's content"
        )


async def test_route_b_halt_landing_MUTATION_CONTROL(monkeypatch: pytest.MonkeyPatch):
    """ROUTE (B) CLAUSE 3 — remove the repair; the same drive must go identical.

    F53's repair is the durable, ledger-derived landing count that
    `_blocked_prompt` escalates on. Restore the pre-repair behaviour — a landing
    that cannot see the run's own history, so every fire is a first fire — and
    the identical drive above must reproduce the ORIGINAL DEFECT: two
    byte-identical bodies.

    Without this control the test above would merely assert that two bodies
    differ, with no evidence that the difference comes from the repair rather
    than from anything else in the composition (F52, applied to the test).
    """
    from disco.core.loop.valve_landing import ValveLandingMixin

    async def _pre_repair_landing_repeats(
        self, *, reason: str, guidance_fp: str
    ) -> tuple[int, int]:
        """`_blocked_prompt` before the repair: a pure function of its arguments
        with no access to the log at all, so both counts are always 1."""
        return (1, 1)

    monkeypatch.setattr(
        ValveLandingMixin, "_landing_repeats", _pre_repair_landing_repeats
    )

    guidance = _driven_guidance()
    loop = _RealLandingLoop(_halt_precondition_seed(_fingerprint()))
    assert await _fire_refusal(loop, guidance) is Disp.HALT
    assert await _fire_refusal(loop, guidance) is Disp.HALT

    bodies = loop.env_bodies()
    assert len(bodies) == 2
    assert bodies[0] == bodies[1], (
        "MUTATION CONTROL FAILED — with the repair removed the two landings did "
        "NOT go byte-identical, so the passing test above is not attesting the "
        "repair and its reach is unproven"
    )


# ---------------------------------------------------------------------------
# TARGET 3 — `_plan_nudge`, driven from the real planning gate.
#
# The multi-fire condition is a PLANNING-MODE NO-OP: a step in planning mode with
# no tool call. `PlanningGateController._gate_planning_mode` routes exactly that
# to `_handle_planning_prose`, which falls through the harvest and force-submit
# gates to `_nudge_planner`, which computes the count and emits.
#
# Driving the RENDERER here would prove nothing: `_plan_nudge` already took a
# `repeats` before the repair and rendered it correctly. The defect was in the
# CALLER, which computed that count from a segment window that resets on every
# re-entry into planning. So the drive starts at the gate.
# ---------------------------------------------------------------------------


class _PlanningRunLoop(_RunLoop):
    """A run loop in PLANNING mode, driven through the real planning gate."""

    def __init__(self, seed: list[Event] | None = None) -> None:
        super().__init__(seed)
        self._plan_nudges = 0
        self._quiet = False
        self._revision_force_submit_enabled = False
        self._workflow_run = None
        self._plan_tool = "submit_plan"

    def _reconcile_mode_from_events(self, events: list[Event]):
        from disco.core.llm.types import OperatingMode

        return OperatingMode.PLANNING

    def _workflow_router_phase_active(self) -> bool:
        return False

    async def _post_noop_valve(self) -> Disp:
        return Disp.CONTINUE


async def _fire_planning_noop(loop: _PlanningRunLoop, thought: str) -> Disp:
    """One planning-mode no-op through the REAL gate.

    `tool_call=None` in PLANNING mode is the no-op `_gate_planning_mode` routes
    to `_handle_planning_prose`. Nothing about `_nudge_planner` is reached
    directly — the gate decides to reach it.
    """
    from disco.core.loop.planning_gates import PlanningGateController

    controller = PlanningGateController(loop)  # type: ignore[arg-type]
    step = AgentStep(thought=thought, tool_call=None)
    return await controller._gate_planning_mode(step, await loop._events())


async def test_route_b_plan_nudge_fires_twice_in_one_run_and_does_not_repeat():
    """ROUTE (B) CLAUSE 2 — `_plan_nudge`, driven from the real planning gate.

    Two planning-mode no-ops in one run, the second reading the first's own
    emitted nudge, with the emitted bodies compared. This is the composition
    07h's defect hid: the renderer was right, the caller's count reset.
    """
    loop = _PlanningRunLoop(with_seqs([user_msg("build me a thing")]))

    first_disp = await _fire_planning_noop(loop, "I am still thinking about it.")
    second_disp = await _fire_planning_noop(loop, "Still thinking, honestly.")

    assert first_disp is Disp.CONTINUE
    assert second_disp is Disp.CONTINUE

    bodies = loop.env_bodies()
    assert len(bodies) == 2, (
        f"expected exactly two nudges from two planning no-ops, got {len(bodies)}"
    )
    first, second = bodies

    assert first != second, (
        "the planning nudge rendered byte-identically twice in one run — this is "
        "the confirmed defect at p4_ff_node_restart@99603 seqs 11/86, where the "
        "nudge count was scoped to the planning SEGMENT and reset on re-entry "
        "while constraint 4 is scoped to the RUN"
    )
    assert "planning nudge 2" in second
    assert "in this run" in second, "the wording must match the scope of the count"


async def test_route_b_plan_nudge_survives_a_re_entry_into_planning():
    """The exact 99603 shape, driven: the run cycles back through planning
    between the two no-ops. A segment-scoped count returns 0 here; a run-scoped
    one does not."""
    loop = _PlanningRunLoop(with_seqs([user_msg("build me a thing")]))

    await _fire_planning_noop(loop, "thinking")
    # The run re-enters planning, exactly as it did between seqs 11 and 86.
    await loop._emit(
        StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved")
    )
    await loop._emit(StatusEvent(status=ConversationStatus.RUNNING, detail="planning"))
    await _fire_planning_noop(loop, "thinking again")

    bodies = loop.env_bodies()
    assert len(bodies) == 2
    assert bodies[0] != bodies[1], (
        "a nudge count scoped to the planning SEGMENT resets on re-entry, so the "
        "second nudge renders as a first firing — the seqs 11/86 defect exactly"
    )
    assert "planning nudge 2" in bodies[1]


async def test_route_b_plan_nudge_MUTATION_CONTROL(monkeypatch: pytest.MonkeyPatch):
    """ROUTE (B) CLAUSE 3 — remove the repair; the same drive must go identical.

    The repair reads the WHOLE durable log through `_is_plan_nudge_event`.
    Restore the pre-repair reach — a counter that cannot recognise the run's own
    prior nudges, which is what a segment window that has just reset looks like —
    and the identical drive must reproduce the original defect.
    """
    import disco.core.loop.planning_gates as planning_gates

    def _blind_to_prior_nudges(event: Event, plan_nudge: str) -> bool:
        return False

    # `_nudge_planner` imports this inside its body, so patch it at its source.
    monkeypatch.setattr(
        "disco.core.loop._signals_planning._is_plan_nudge_event",
        _blind_to_prior_nudges,
    )
    assert planning_gates is not None  # the gate module under drive

    loop = _PlanningRunLoop(with_seqs([user_msg("build me a thing")]))
    await _fire_planning_noop(loop, "thinking")
    await _fire_planning_noop(loop, "thinking again")

    bodies = loop.env_bodies()
    assert len(bodies) == 2
    assert bodies[0] == bodies[1], (
        "MUTATION CONTROL FAILED — with the whole-log count removed the two "
        "nudges did NOT go byte-identical, so the passing test above is not "
        "attesting the repair and its reach is unproven"
    )


async def test_route_b_plan_nudge_first_firing_is_still_the_legacy_constant():
    """The escalation is additive. A first nudge in a driven run must still be
    byte-identical to `_PLAN_NUDGE`, so pre-2026-08-07b durable logs stay
    recognisable by content to the counters that read them."""
    from disco.core.loop.engine_contracts import _PLAN_NUDGE

    loop = _PlanningRunLoop(with_seqs([user_msg("build me a thing")]))
    await _fire_planning_noop(loop, "thinking")

    assert loop.env_bodies()[0] == _PLAN_NUDGE
