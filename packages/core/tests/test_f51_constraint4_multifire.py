"""F51 — the twice-rule (GROUNDED FEEDBACK constraint 4) over surfaces that can
fire more than once per run.

# The defect

The 2026-08-07b twice-rule audit enumerated agent-facing surfaces whose value
"must resolve to a constant `str`" and reported ZERO static multi-fire surfaces,
honestly. 2026-08-07c recorded that as a DISCHARGE of the owner's constraint 4.
It was not one. The owner's constraint binds

    "No message surface that CAN FIRE MORE THAN ONCE PER RUN may be static.
     Repetition-aware at minimum (count acknowledged, escalating), ledger-derived
     where state exists."

and every escaping surface is an f-string composed at the emit seam, so the
enumerator excluded them by its own definition. 2026-08-07d hashed what the agent
was ACTUALLY told across 24 live cells: **112 bodies, 7 byte-identical repeats on
5 surfaces in 3 cells, one firing 15 times in a single run.** That is F51.

# The property under test

**Constraints 1 and 4 are independent.** A surface can be genuinely state-derived
— `host_disposition` renders the artifact kind, path and host label, and the
guidance is projected from the typed receipt's own claim results — and still emit
byte-identical text on every fire, because in a repeat loop the state it renders
has not changed BY CONSTRUCTION. "Derived" does not imply "different". Only a
count breaks the tie, and every count used here is read from the run's durable log
rather than an instance counter, so it survives a restart or condensation exactly
as the run's own evidence does.

Each test below names the live cell whose bytes it locks down.

# Red-on-old-bytes

The new symbols are imported INSIDE the test bodies, deliberately. A single
module-level import of a function that does not exist at `4ce28e50` would collapse
every test in this file into one collection error, and "the file did not import"
is much weaker evidence than "this specific body asserts a property the old bytes
do not have". Each test carries its own red.
"""

from __future__ import annotations

import hashlib

import pytest
from disco.core import (
    ActionEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from disco.core.loop.finish.common import _VERIFY_MARKER_PREFIX
from event_fakes import user_msg, with_seqs

# ---------------------------------------------------------------------------
# Fakes — the minimum surface each seam actually touches
# ---------------------------------------------------------------------------


class _FakeLoop:
    """Captures emitted events instead of persisting them."""

    def __init__(self) -> None:
        self.emitted: list[Event] = []
        self._browser_verify_refusals = 0
        self.blocked: list[dict] = []

    async def _emit(self, event: Event) -> Event:
        self.emitted.append(event)
        return event

    async def _land_blocked(self, **kwargs) -> None:
        self.blocked.append(kwargs)

    def bodies(self) -> list[str]:
        return [
            e.message.content
            for e in self.emitted
            if isinstance(e, MessageEvent) and e.message is not None
        ]


class _FakeGate:
    """The host-verify gate facet `host_disposition` calls into."""

    def __init__(self, loop: _FakeLoop) -> None:
        self._loop = loop

    def _verdict_label(self, verdict: dict) -> str:
        return str(verdict.get("label") or "unavailable")

    def _verdict_first_failure(self, verdict: dict) -> str:
        return str(verdict.get("first_failure") or "")

    async def _record_verifier_failure_to_context(self, **kwargs) -> None:
        return None


class _Deliverable:
    artifact_kind = "app"
    artifact_path = "index.html"


VERDICT = {
    "label": "unavailable",
    "summary": "no current, complete typed receipt certifies the mandatory claims",
    "next_action": "",
    "failure_fingerprint": "missing_or_mismatched_typed_receipt",
}


def _marker(fingerprint: str) -> StatusEvent:
    from disco.core.events import ConversationStatus

    return StatusEvent(
        status=ConversationStatus.RUNNING, detail=f"{_VERIFY_MARKER_PREFIX}{fingerprint}"
    )


def _expected_fingerprint(raw: str = "missing_or_mismatched_typed_receipt") -> str:
    return "host:" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _after_markers(fingerprint: str, count: int) -> list[Event]:
    """`count` prior fires of this failure, with the authority floor raised past them.

    **This shape IS the F51 mechanism, and getting it right matters.** Putting the
    markers on the log alone makes the loop breaker trip
    (`_prior_verify_marker_fp(events, authority_seq) == fingerprint` -> `_land_blocked`,
    no message at all), which is the product working correctly. The 15x streak
    happened precisely BECAUSE that never triggered: `pilota/001` kept doing
    productive work between failures, every productive event raised the authority
    floor above the last marker, and the breaker's window reset on every pass while
    the failure itself never changed.

    So a trailing USER message raises the floor — the same effect the live run got
    from real edits — leaving the breaker silent and the whole-run count as the only
    signal that anything is repeating.
    """
    return with_seqs(
        [user_msg("start"), *[_marker(fingerprint) for _ in range(count)], user_msg("keep going")]
    )


# ---------------------------------------------------------------------------
# Surface 1 — `host_disposition.governed_non_pass_disposition`
# THE 15x SURFACE. pilota/001 @98602, seqs 84…350, fifteen identical bodies.
# ---------------------------------------------------------------------------


async def test_the_15x_surface_does_not_repeat_itself(monkeypatch):
    """`pilota/001` was told the identical sentence FIFTEEN times while failing to
    progress. The loop breaker did not catch it because it scans only markers
    above the authority floor and every productive edit raises that floor — so a
    run that churns productively while failing verification identically resets the
    breaker's window on every pass. The whole-run count is the missing signal.
    """
    from disco.core.loop.finish.verify_gate_parts.host_disposition import (
        governed_non_pass_disposition,
    )

    loop = _FakeLoop()
    gate = _FakeGate(loop)
    fp = _expected_fingerprint()

    # First fire: no prior marker.
    await governed_non_pass_disposition(gate, _Deliverable(), VERDICT, None, [])
    first = loop.bodies()[0]

    # Second fire, with the first fire's durable marker on the log and the
    # authority floor moved past it (a productive edit happened in between), which
    # is exactly the state that produced the 15x streak.
    events = _after_markers(fp, 1)
    loop2 = _FakeLoop()
    await governed_non_pass_disposition(_FakeGate(loop2), _Deliverable(), VERDICT, None, events)
    second = loop2.bodies()[0]

    assert first != second, (
        "F51: this surface fired 15x byte-identically in pilota/001. A derived "
        "surface that takes no repeat count emits identical bytes whenever the "
        "state it renders has not changed — which is every iteration of a repeat loop"
    )
    assert "REPEAT 2" in second
    assert "ENDS the run" in second, "name the cost, not just the count (constraint 4)"


async def test_the_15x_surface_keeps_naming_the_state_it_derives(monkeypatch):
    """Constraint 1 is NOT traded away to satisfy constraint 4. The artifact kind,
    path and projected guidance must still be in the escalated body."""
    from disco.core.loop.finish.verify_gate_parts.host_disposition import (
        governed_non_pass_disposition,
    )

    loop = _FakeLoop()
    events = _after_markers(_expected_fingerprint(), 1)
    await governed_non_pass_disposition(_FakeGate(loop), _Deliverable(), VERDICT, None, events)
    body = loop.bodies()[0]

    assert "'index.html'" in body and "app" in body
    assert "preview_start" in body, "the ONE projected next move survives the escalation"


async def test_the_15x_surface_escalation_count_tracks_the_ledger(monkeypatch):
    """Ledger-derived, not an instance counter: four prior markers must read as
    the fifth fire, and the count must come from the log alone."""
    from disco.core.loop.finish.verify_gate_parts.host_disposition import (
        governed_non_pass_disposition,
    )

    fp = _expected_fingerprint()
    events = _after_markers(fp, 4)
    loop = _FakeLoop()
    await governed_non_pass_disposition(_FakeGate(loop), _Deliverable(), VERDICT, None, events)

    assert "REPEAT 5" in loop.bodies()[0]


async def test_a_DIFFERENT_failure_restarts_the_count(monkeypatch):
    """The count is per-FINGERPRINT. A different governed failure is a different
    question and must not inherit another failure's escalation."""
    from disco.core.loop.finish.verify_gate_parts.host_disposition import (
        governed_non_pass_disposition,
    )

    events = _after_markers(_expected_fingerprint("something_else"), 3)
    loop = _FakeLoop()
    await governed_non_pass_disposition(_FakeGate(loop), _Deliverable(), VERDICT, None, events)
    body = loop.bodies()[0]

    assert "REPEAT" not in body, "a different failure is a first fire, not a repeat"


# ---------------------------------------------------------------------------
# Surface 2 — `host_disposition.governed_contract_refusal`
# Composes `host_claims.handoff_refusal_detail` (F51's fifth surface) into the
# emitted body. pilota/001, 2 identical fires.
# ---------------------------------------------------------------------------


async def test_the_target_verification_surface_does_not_repeat_itself():
    from disco.core.loop.finish.verify_gate_parts.host_claims import handoff_refusal_detail
    from disco.core.loop.finish.verify_gate_parts.host_disposition import (
        governed_contract_refusal,
    )

    # ATTESTATION-BINDING INVARIANT (F58): this was a hand-copied duplicate of
    # `handoff_refusal_detail`'s no-handoff branch. Derived from the describer
    # that owns it, so a reword moves the test with the product instead of
    # leaving it asserting on a string nothing emits.
    guidance = handoff_refusal_detail(None, None)
    loop = _FakeLoop()
    await governed_contract_refusal(
        _FakeGate(loop), [], failure_key="no_handoff", guidance=guidance
    )
    first = loop.bodies()[0]

    fp = "host:" + hashlib.sha256(b"no_handoff").hexdigest()[:24]
    loop2 = _FakeLoop()
    await governed_contract_refusal(
        _FakeGate(loop2),
        _after_markers(fp, 1),
        failure_key="no_handoff",
        guidance=guidance,
    )
    second = loop2.bodies()[0]

    assert first != second
    assert guidance in first and guidance in second, (
        "the fact-naming detail from host_claims.handoff_refusal_detail survives — "
        "repetition-awareness is added at the emit seam, which is the only place "
        "the run's own history is reachable"
    )
    assert "REPEAT 2" in second


# ---------------------------------------------------------------------------
# Surface 3 — `turn_control_support._serve_handoff_guidance`
# pilota/001 3x, canary/000 2x. Its sibling `_serve_duplicate_guidance` has taken
# a repeat count since 2026-07-27; this one sits TEN LINES ABOVE it and did not.
# ---------------------------------------------------------------------------


def test_serve_handoff_guidance_takes_a_repeat_count_like_its_sibling():
    """The signature itself is the repair. On `4ce28e50` this call raises
    TypeError — the function took only `events`."""
    from disco.core.loop.turn_control_support import _serve_handoff_guidance

    first = _serve_handoff_guidance(1, [])
    third = _serve_handoff_guidance(3, [])

    assert first != third
    assert "3th handoff" in third or "3" in third
    assert "ENDS this run" in third, "count acknowledged AND escalating (constraint 4)"
    assert "Handoff recorded" in first, "the first fire is unchanged in substance"


def _retired_delegated_fork_clause() -> str:
    """The delegated-fork clause the owner named, DERIVED — never retyped.

    ATTESTATION-BINDING INVARIANT (F58), the hard case. This clause is asserted
    ABSENT: production deleted it deliberately, so there is no live symbol to
    import and there never will be. The invariant's own escape clause governs —
    *where a value genuinely cannot be imported, the attestation must carry a
    control that fails when the production value changes* — and the control here
    is that production still RECORDS the retired sentence, verbatim, in
    `_serve_next_move`'s docstring as the owner's canonical counter-example.

    So the clause is extracted structurally from that docstring rather than
    restated: the quoted block, the text after its semicolon, unwrapped. If
    someone rewords or removes that counter-example, this raises instead of
    silently guarding a string production no longer remembers — which is exactly
    the failure mode F58 names, one level up.
    """
    from disco.core.loop.turn_control_support import _serve_next_move

    doc = _serve_next_move.__doc__ or ""
    # Non-Empty Population Rule: an absence proved over a corpus that vanished is
    # not evidence of absence. Fail closed if the counter-example is not there.
    assert doc.count('"') >= 2, (
        "`_serve_next_move`'s docstring no longer carries the quoted "
        "counter-example this guard derives from — re-derive it or retire the "
        "guard deliberately, but do not let it rot back into a hand-copy"
    )
    quoted = doc.split('"')[1]
    assert ";" in quoted, "the counter-example lost its delegated fork (the `;`)"
    clause = " ".join(quoted.split(";", 1)[1].split()).split(",")[0].strip()
    assert len(clause.split()) >= 4, f"extracted clause looks degenerate: {clause!r}"
    return clause


def test_serve_handoff_guidance_still_projects_exactly_one_next_move():
    """Constraint 3 (the projection rule) is not traded away for constraint 4:
    both branches must still end in the ONE move `_serve_next_move` derives."""
    from disco.core.loop.turn_control_support import _serve_handoff_guidance

    retired = _retired_delegated_fork_clause()
    for repeats in (1, 4):
        body = _serve_handoff_guidance(repeats, [])
        assert "Next move:" in body
        assert retired not in body, (
            "the delegated-fork counter-example the owner named must not come back"
        )


# ---------------------------------------------------------------------------
# Surface 4 — `finalize_parts/verify_receipt.reuse_finish_verify_receipt`
# canary/000 2x, pilota/001 2x.
# ---------------------------------------------------------------------------


def _shell_pair(command: str, call_id: str = "c1") -> tuple[ActionEvent, ObservationEvent]:
    action = ActionEvent(
        thought="verify",
        tool_call=ToolCall(tool_name="shell", call_id=call_id, arguments={"command": command}),
    )
    obs = ObservationEvent(
        tool_result=ToolResult(
            call_id=call_id, tool_name="shell", success=True, content="HTTP 200"
        ),
        action_id=action.id,
    )
    return action, obs


async def test_receipt_reuse_notice_does_not_repeat_itself():
    """Two finish attempts with no intervening action reuse the SAME observation,
    so both the command and the observation id are unchanged and the pre-repair
    bodies were byte-identical."""
    from disco.core.loop.finish.finalize_parts.verify_receipt import (
        _RECEIPT_REUSE_NOT_BLOCKING,
        reuse_finish_verify_receipt,
    )

    cmd = "curl -fsSL -o /dev/null http://127.0.0.1:4321/"
    action, obs = _shell_pair(cmd)
    events = with_seqs([user_msg("go"), action, obs])

    loop = _FakeLoop()
    assert await reuse_finish_verify_receipt(loop, cmd, events) is True
    first = loop.bodies()[0]

    # Second finish attempt: the prior reuse notice is now on the log.
    events2 = with_seqs([user_msg("go"), action, obs, loop.emitted[0]])
    loop2 = _FakeLoop()
    assert await reuse_finish_verify_receipt(loop2, cmd, events2) is True
    second = loop2.bodies()[0]

    assert first != second
    assert "reuse 2" in second
    # DERIVED from the production symbol that owns the clause (F61, repaired
    # 2026-08-07t). This asserted a 31-char hand-picked SLICE of the sentence until
    # then, because production held it only as a fragment of an f-string
    # concatenation and there was no name to bind to. The assertion is now the WHOLE
    # named clause — a deliberate and disclosed STRENGTHENING, not a like-for-like
    # swap: the slice was only ever a slice because nothing could be derived.
    assert _RECEIPT_REUSE_NOT_BLOCKING in second, (
        "a repeat here means something ELSE refuses the finish — say so, rather "
        "than inviting the agent to re-run the one thing that already works"
    )


async def test_receipt_reuse_count_is_keyed_on_the_COMMAND_not_the_observation():
    """A different verify command is a different question and starts its own count."""
    from disco.core.loop.finish.finalize_parts.verify_receipt import (
        reuse_finish_verify_receipt,
    )

    cmd_a, cmd_b = "pytest -q", "npm test"
    action_a, obs_a = _shell_pair(cmd_a, "a1")
    prior_notice = MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content="<system-reminder>x</system-reminder>"),
        meta={
            "finish_verify_receipt_reused": True,
            "command_fingerprint_sha256": hashlib.sha256(cmd_b.encode()).hexdigest(),
        },
    )
    events = with_seqs([user_msg("go"), prior_notice, action_a, obs_a])

    loop = _FakeLoop()
    await reuse_finish_verify_receipt(loop, cmd_a, events)

    assert "reuse 2" not in loop.bodies()[0], "another command's reuse must not escalate this one"


# ---------------------------------------------------------------------------
# Surface 5 (legacy sibling) — `host_disposition.host_verify_failure_disposition`
# NOT in F51's live list: it never fired twice in the 07d corpus. It is squarely
# in the class the constraint names — capped at 3 fires, body a pure function of
# the verdict — so the widened instrument flags it, and leaving it would mean
# shipping a repair whose own instrument is red on the repaired tree.
# ---------------------------------------------------------------------------


async def test_the_legacy_shadow_verify_surface_escalates_across_its_three_fires():
    from disco.core.loop.finish.verify_gate_parts.host_disposition import (
        host_verify_failure_disposition,
    )

    loop = _FakeLoop()
    gate = _FakeGate(loop)
    for _ in range(3):
        await host_verify_failure_disposition(gate, _Deliverable(), VERDICT)

    bodies = loop.bodies()[:3]
    assert len(set(bodies)) == 3, "three fires, three distinct bodies"
    assert "REPEAT 2" in bodies[1] and "REPEAT 3" in bodies[2]
    assert "RELEASED UNVERIFIED" in bodies[1], (
        "the cost named must be this seam's ACTUAL cost — it releases unverified "
        "rather than halting, unlike the governed seams"
    )


# ---------------------------------------------------------------------------
# The counters themselves — ledger-derived, which is the owner's stronger form
# ---------------------------------------------------------------------------


def test_verify_marker_count_reads_the_durable_log_not_an_instance_counter():
    from disco.core.loop.finish.verify_gate_parts.host_disposition import (
        _verify_marker_fire_count,
    )

    fp = "host:abc123"
    assert _verify_marker_fire_count([], fp) == 1
    assert _verify_marker_fire_count(with_seqs([_marker(fp), _marker(fp)]), fp) == 3
    assert _verify_marker_fire_count(with_seqs([_marker("host:other")]), fp) == 1


def test_receipt_reuse_count_reads_the_durable_log():
    from disco.core.loop.finish.finalize_parts.verify_receipt import _prior_reuse_count

    fp = hashlib.sha256(b"pytest -q").hexdigest()
    notice = MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content="x"),
        meta={"finish_verify_receipt_reused": True, "command_fingerprint_sha256": fp},
    )
    assert _prior_reuse_count([], fp) == 1
    assert _prior_reuse_count(with_seqs([notice, notice]), fp) == 3


@pytest.mark.parametrize("repeats", [2, 3, 15])
def test_repeat_preamble_names_the_count_and_the_cost(repeats):
    from disco.core.loop.finish.verify_gate_parts.host_disposition import _repeat_preamble

    text = _repeat_preamble(repeats, "COST-CLAUSE")
    assert str(repeats) in text
    assert "COST-CLAUSE" in text
