"""A4.2 — the iterative refinement loop for Deep Research.

After the first synthesis, JUDGE every claim's grounding (A4.0), and while too many
claims are weak, RE-SEARCH + RE-SYNTHESIZE the sections that carry them, then re-judge
— up to a small round cap. Stop as soon as ≥ `target` of claims are SUPPORTED.

The loop is written with INJECTED effects (`judge_section`, `refine_section`) so the
convergence logic — the part that's easy to get subtly wrong (early-stop vs never-
converge, the round cap, which sections to refine) — is pure and fully unit-tested
without the LLM / retrieval engine. The engine supplies the real effects; the OFF path
(iterative flag false) never calls this at all, so a standard run is byte-identical.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from .judge import ClaimVerdict, fraction_supported, weak_claims

# A section is opaque to the loop — the engine's ReportSection, or a test stand-in.
S = TypeVar("S")

# judge_section(section) -> the per-claim verdicts for that section.
JudgeFn = Callable[[S], Awaitable[list[ClaimVerdict]]]
# refine_section(section, its_weak_claims) -> a refined section (re-searched + re-
# synthesized). May return the SAME section if nothing could be improved.
RefineFn = Callable[[S, list[ClaimVerdict]], Awaitable[S]]
# Optional progress callback (phase/round telemetry); no-op by default.
EmitFn = Callable[[str, dict[str, object]], Awaitable[None]]


@dataclass
class IterationResult(Generic[S]):
    sections: list[S]
    rounds: int  # refinement rounds actually run (0 = converged on the first judging)
    final_supported: float  # fraction SUPPORTED across all claims at the end
    converged: bool  # True iff final_supported >= target (vs hit the round cap)


async def _noop_emit(_phase: str, _data: dict[str, object]) -> None:
    return None


async def run_iterative_refinement(
    sections: list[S],
    *,
    judge_section: JudgeFn[S],
    refine_section: RefineFn[S],
    max_rounds: int = 3,
    target: float = 0.8,
    emit: EmitFn | None = None,
) -> IterationResult[S]:
    """Judge → refine weak sections → re-judge, ≤ max_rounds, stopping at ≥ target.

    Each round judges ALL sections (a refined section can regress a previously-strong
    one, so re-judging everything is the honest measure), refines only the sections
    that still carry weak claims, and stops the moment the overall SUPPORTED fraction
    reaches `target`. Guaranteed to terminate: bounded by `max_rounds`."""
    emit = emit or _noop_emit
    work = list(sections)

    # Round 0: judge once. If already grounded enough, do NOT refine at all.
    verdicts = await _judge_all(work, judge_section)
    supported = fraction_supported(_flatten(verdicts))
    await emit("iterate", {"round": 0, "supported": supported})
    if supported >= target:
        return IterationResult(sections=work, rounds=0, final_supported=supported, converged=True)

    rounds = 0
    for r in range(1, max_rounds + 1):
        rounds = r
        # Refine every section that still has weak (PARTIAL/UNSUPPORTED) claims.
        refined_any = False
        for i, sec in enumerate(work):
            weak = weak_claims(verdicts[i])
            if not weak:
                continue
            new_sec = await refine_section(sec, weak)
            if new_sec is sec:
                continue
            # Accept the refined section ONLY if it does not regress: judge the
            # candidate and keep it just when its SUPPORTED fraction is at least
            # the original section's. A worse refine is dropped (original kept)
            # and counted as no-improvement, so the loop can break instead of
            # swapping in a degraded section. This re-uses the injected judge so
            # the loop stays pure (the engine supplies the real grounding check).
            orig_supported = fraction_supported(verdicts[i])
            new_supported = fraction_supported(await judge_section(new_sec))
            if new_supported < orig_supported:
                continue
            # Zero-to-zero swaps are pure churn (a broken/harsh judge scores
            # everything 0.0; equal-score acceptance would oscillate content
            # for all rounds with no evidence of improvement).
            if new_supported == orig_supported == 0.0:
                continue
            work[i] = new_sec
            refined_any = True
        if not refined_any:
            # Nothing could be improved this round → further rounds won't help.
            break
        # Re-judge everything and check the gate.
        verdicts = await _judge_all(work, judge_section)
        supported = fraction_supported(_flatten(verdicts))
        await emit("iterate", {"round": r, "supported": supported})
        if supported >= target:
            return IterationResult(
                sections=work, rounds=r, final_supported=supported, converged=True
            )

    return IterationResult(
        sections=work, rounds=rounds, final_supported=supported, converged=supported >= target
    )


async def _judge_all(sections: list[S], judge_section: JudgeFn[S]) -> list[list[ClaimVerdict]]:
    out: list[list[ClaimVerdict]] = []
    for sec in sections:
        out.append(await judge_section(sec))
    return out


def _flatten(verdicts: list[list[ClaimVerdict]]) -> list[ClaimVerdict]:
    return [v for section_verdicts in verdicts for v in section_verdicts]
