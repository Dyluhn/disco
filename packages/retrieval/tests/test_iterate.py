"""A4.2 — iterative refinement loop logic (pure, injected fakes; no LLM/retrieval)."""

from __future__ import annotations

import pytest

from disco.retrieval.deep_research.iterate import run_iterative_refinement
from disco.retrieval.deep_research.judge import ClaimVerdict


class Sec:
    """A fake section: `supported` SUPPORTED claims + `weak` UNSUPPORTED claims."""

    def __init__(self, supported: int, weak: int) -> None:
        self.supported = supported
        self.weak = weak


async def _judge(sec: Sec) -> list[ClaimVerdict]:
    return [ClaimVerdict(f"s{i}", "SUPPORTED") for i in range(sec.supported)] + [
        ClaimVerdict(f"w{i}", "UNSUPPORTED") for i in range(sec.weak)
    ]


def _make_refine(improve_by: int = 1):
    """refine converts `improve_by` weak claims → supported each call."""
    calls: list[int] = []

    async def refine(sec: Sec, weak: list[ClaimVerdict]) -> Sec:
        calls.append(id(sec))
        if sec.weak == 0:
            return sec
        moved = min(improve_by, sec.weak)
        return Sec(sec.supported + moved, sec.weak - moved)

    return refine, calls


@pytest.mark.asyncio
async def test_converges_round_zero_without_refining() -> None:
    refine, calls = _make_refine()
    res = await run_iterative_refinement(
        [Sec(10, 0), Sec(8, 2)],  # 18/20 = 0.9 ≥ 0.8 already
        judge_section=_judge,
        refine_section=refine,
    )
    assert res.converged is True
    assert res.rounds == 0
    assert res.final_supported == pytest.approx(0.9)
    assert calls == []  # never refined — already grounded enough


@pytest.mark.asyncio
async def test_converges_after_refinement() -> None:
    refine, calls = _make_refine(improve_by=1)
    res = await run_iterative_refinement(
        [Sec(7, 3)],  # 0.7 < 0.8 → refine to 8/10 = 0.8
        judge_section=_judge,
        refine_section=refine,
        target=0.8,
    )
    assert res.converged is True
    assert res.rounds == 1
    assert res.final_supported == pytest.approx(0.8)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_hits_round_cap_without_converging() -> None:
    refine, _ = _make_refine(improve_by=1)
    res = await run_iterative_refinement(
        [Sec(1, 9)],  # 0.1; +1/round → 4/10 after 3 rounds, still < 0.8
        judge_section=_judge,
        refine_section=refine,
        max_rounds=3,
        target=0.8,
    )
    assert res.rounds == 3
    assert res.converged is False
    assert res.final_supported == pytest.approx(0.4)  # 1 → 4 supported of 10


@pytest.mark.asyncio
async def test_no_improvement_breaks_early() -> None:
    async def refine_noop(sec: Sec, weak: list[ClaimVerdict]) -> Sec:
        return sec  # can't improve → loop must stop, not spin to the cap

    res = await run_iterative_refinement(
        [Sec(1, 9)],
        judge_section=_judge,
        refine_section=refine_noop,
        max_rounds=5,
    )
    assert res.rounds == 1  # refined-nothing on round 1 → break
    assert res.converged is False


@pytest.mark.asyncio
async def test_only_weak_sections_are_refined() -> None:
    strong = Sec(10, 0)
    weak = Sec(2, 8)
    refined: list[int] = []

    async def refine(sec: Sec, w: list[ClaimVerdict]) -> Sec:
        refined.append(id(sec))
        return Sec(sec.supported + sec.weak, 0)  # fully fix the weak one

    res = await run_iterative_refinement(
        [strong, weak], judge_section=_judge, refine_section=refine, target=0.8
    )
    # Only the weak section was handed to refine; the strong one never was.
    assert id(strong) not in refined
    assert id(weak) in refined
    assert res.converged is True
