"""C1c — the external Definition-of-Done finish gate, with the BOUNDED refusal
cap (the fix for the uncapped first cut that OOM'd: an unmet DoD looped
refuse-and-continue forever, accumulating events without end).

These tests exercise `AgentLoop._finish_dod_gate_passed` DIRECTLY (no full loop
run) so the cap is proven without any risk of the very unbounded loop it guards
against. The cap mirrors `_FINISH_VERIFY_CAP`: refuse while under the cap, then
RELEASE the gate (finish lands) with a loud log + the prior refusal events as the
visible audit trail.
"""

from __future__ import annotations

import pytest
from disco.core import MessageEvent
from disco.core.dod import DoDSpec, FileExistsPredicate
from disco.core.dod_evaluator import DoDPredicateResult, DoDVerdict
from disco.core.llm import DefaultLLMRouter, ProposedToolCall
from disco.core.loop import RouterAgent
from disco.core.loop.engine import _DOD_REFUSAL_CAP
from llm_fakes import simple_config
from loop_fakes import SequenceProvider, build_loop

pytestmark = pytest.mark.asyncio

CID = "conv"
_PRED = FileExistsPredicate(path="out.txt")


def _agent():
    provider = SequenceProvider([{"text": "noop"}])
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    return RouterAgent(router, conversation_id=CID)


class _FixedEvaluator:
    """A fake DoD evaluator returning a fixed verdict — the C1c factory seam."""

    def __init__(self, passed: bool):
        self._passed = passed
        self.calls = 0

    async def evaluate(self, spec, *, conversation_id):  # noqa: ARG002
        self.calls += 1
        result = DoDPredicateResult(
            predicate=_PRED,
            passed=self._passed,
            reason="ok" if self._passed else "out.txt is missing",
        )
        return DoDVerdict(
            passed=self._passed,
            unmet=[] if self._passed else [_PRED],
            results=[result],
            spec_predicate_count=1,
            evaluated_at="2026-06-13T00:00:00Z",
            spec_fingerprint="fp-test",
        )


async def _loop_with_dod(evaluator):
    loop, store = build_loop(_agent())
    await store.set_dod_spec(CID, DoDSpec(predicates=[_PRED]))
    loop._dod_evaluator_factory = lambda: evaluator
    return loop, store


# ---- the cap (the OOM fix) --------------------------------------------------


async def test_unmet_dod_refuses_then_RELEASES_at_the_cap():
    """The load-bearing regression: an always-failing DoD must NOT refuse
    forever. Refuse while under the cap, then release — bounded, no infinite
    refuse-and-continue loop (the OOM the uncapped first cut caused)."""
    loop, _ = await _loop_with_dod(_FixedEvaluator(passed=False))
    verdicts = [await loop._finish_dod_gate_passed() for _ in range(_DOD_REFUSAL_CAP + 1)]
    # First _DOD_REFUSAL_CAP calls refuse (False); the next RELEASES (True).
    assert verdicts == [False] * _DOD_REFUSAL_CAP + [True]
    # And it STAYS released on every subsequent call — never loops back to refuse.
    assert await loop._finish_dod_gate_passed() is True


async def test_met_dod_passes_and_resets_the_refusal_streak():
    """A passing verdict allows finish AND resets the streak, so a later failure
    starts a fresh cap window (mirrors verify-cap reset-on-clean-pass)."""
    fail, ok = _FixedEvaluator(passed=False), _FixedEvaluator(passed=True)
    loop, _ = await _loop_with_dod(fail)
    assert await loop._finish_dod_gate_passed() is False  # 1 refusal banked
    loop._dod_evaluator_factory = lambda: ok
    assert await loop._finish_dod_gate_passed() is True
    assert loop._dod_refusals == 0  # streak reset on the clean pass
    # Fresh failure → refuses again from zero (not already at the cap).
    loop._dod_evaluator_factory = lambda: fail
    assert await loop._finish_dod_gate_passed() is False


async def test_unmet_dod_emits_a_visible_event_naming_the_predicate():
    loop, store = await _loop_with_dod(_FixedEvaluator(passed=False))
    assert await loop._finish_dod_gate_passed() is False
    events = await store.get_events(CID)
    msgs = [e for e in events if isinstance(e, MessageEvent)]
    assert msgs, "a refused DoD finish must leave a visible trace event"
    body = msgs[-1].message.content
    assert "Definition-of-Done" in body and "out.txt" in body  # names the unmet predicate


async def test_no_dod_spec_is_legacy_byte_identical_no_events():
    """No spec for the conversation → the gate is a no-op: returns True, emits
    NO events, changes no state (the pre-C1c finish path, reproduced exactly)."""
    loop, store = build_loop(_agent())  # NO set_dod_spec
    before = await store.get_events(CID)
    assert await loop._finish_dod_gate_passed() is True
    after = await store.get_events(CID)
    assert before == after  # zero side effects on the legacy path
