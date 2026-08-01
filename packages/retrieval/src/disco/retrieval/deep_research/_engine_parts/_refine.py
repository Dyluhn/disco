"""A4.4 iterative-refinement loop, extracted from `DeepResearchRun._iterative_refine`.

The nested `refine_section` closure originally mixed re-search-corpus
seeding, namespace upserts, task-await error handling, re-synthesis,
degraded-result detection and index-merging in one 16-branch body. Each is
now a single-purpose helper; `refine_section` stays a nested closure (it
needs `passages_by_id_obj` / `passages_text` / `fresh_passages` / `fresh_hits`
local state, and `run_iterative_refinement`'s frozen call signature expects a
plain `(sec, weak) -> ReportSection` callable) but now owns only its own
control flow.

Every helper that needs the live `DeepResearchRun` takes it as an explicit
`run` parameter (never a method) — see `_engine_parts/__init__.py`.

Calls `synthesize_section` through `engine.synthesize_section(...)`
(module-attribute lookup at call time) rather than importing it directly —
same reasoning as `_drain.py`: tests monkeypatch
`disco.retrieval.deep_research.engine.synthesize_section`, and a captured
`from ..synthesis import synthesize_section` binding here would silently
defeat that patch.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

from disco.core import ReportSection

from ...models import Passage as RetrievalPassage
from .. import engine
from ..claims import extract_section_claims
from ..decompose import SubQuestion
from ..gather import GatherLegContext, SubQuestionResult
from ..iterate import run_iterative_refinement
from ..judge import ClaimVerdict, _Completer, judge_claims

if TYPE_CHECKING:
    from ..engine import DeepResearchRun, EmitFn


def _index_passages_by_id(
    results: list[SubQuestionResult], carried_passages: list[RetrievalPassage]
) -> dict[str, RetrievalPassage]:
    """Index every passage across all sub-results + carried (resumed)
    passages by id, first-seen-wins. Used to re-seed a re-search leg's
    corpus and as the base for the judge/extractor's `passages_text`."""
    passages_by_id_obj: dict[str, RetrievalPassage] = {}
    for r in results:
        for p in r.passages:
            passages_by_id_obj.setdefault(p.id, p)
    for p in carried_passages:
        passages_by_id_obj.setdefault(p.id, p)
    return passages_by_id_obj


def _seed_orig_passages(
    sec: ReportSection, passages_by_id_obj: dict[str, RetrievalPassage]
) -> list[RetrievalPassage]:
    """The section's ORIGINAL cited passages — seeded into the re-search leg
    so re-synthesis sees the COMBINED corpus (fresh evidence + originals)."""
    return [passages_by_id_obj[i] for i in sec.cited_passage_ids if i in passages_by_id_obj]


def _build_refine_subq(sec: ReportSection, weak: list[ClaimVerdict]) -> SubQuestion:
    """Build a targeted sub-question: the section topic + its weak claims.
    `title` is the FULL verbose query (section topic + claim digest) used for
    the actual search and synthesis prompt — preserves search quality.
    `label` is the SHORT user-visible string shown in the activity feed."""
    weak_titles = " | ".join(v.claim for v in weak[:3])
    title = f"{sec.title}: verify — {weak_titles}" if weak_titles else sec.title
    return SubQuestion(title=title, label=sec.title)


async def _seed_refine_namespace(
    run: DeepResearchRun, orig: list[RetrievalPassage], namespace: str
) -> None:
    """Defect-1 fix (combined corpus under an embedder): the gather leg only
    upserts its FRESH passages into `namespace`, and `_retrieve_for_section`
    queries that namespace when an embedder is present — so the seeded
    `orig` (which otherwise rides only in `SubQuestionResult.passages`, the
    embedder-absent fallback path) would be DROPPED from re-synthesis.
    Upsert `orig` into the same namespace here so the namespace query returns
    orig+fresh — the true COMBINED corpus. No-op without an embedder or
    without any `orig` passages to seed (synthesis then uses the fallback
    passages, which include `orig` already)."""
    if run._embedder is None or not orig:
        return
    try:
        vecs = await run._embedder.embed([p.text for p in orig])
        await run._vector_store.upsert(namespace, orig, vecs)
    except Exception:  # noqa: BLE001 — fallback path still carries orig
        pass


async def _run_refine_leg(task: asyncio.Task[SubQuestionResult]) -> SubQuestionResult | None:
    """Await the refine leg's gather task. `None` signals "a failed re-search
    must not regress" — the caller keeps the section's prior version."""
    try:
        return await task
    except Exception:  # noqa: BLE001 — a failed re-search must not regress
        return None


async def _synthesize_refine_section(
    run: DeepResearchRun,
    new_result: SubQuestionResult,
    sec: ReportSection,
    namespace: str,
    leg_context: GatherLegContext,
    emit: EmitFn,
) -> ReportSection | None:
    """Synthesize the refine leg's section, then keep the ORIGINAL section's
    heading stable (the leg's title was a search probe, not a display
    title). `None` signals a synthesis failure — never let it regress."""
    try:
        new_section = await engine.synthesize_section(
            new_result,
            router=run._router,
            embedder=run._embedder,
            vector_store=run._vector_store,
            namespace=namespace,
            nli=run._nli,
            section_id=sec.id,
            top_k_for_section=run._bound.rerank_top_k,
            emit=emit,
            leg_context=leg_context,
            recency_window=run._recency_window,
        )
    except Exception:  # noqa: BLE001 — never let a synth failure regress
        return None
    return new_section.model_copy(update={"title": sec.title})


def _is_degraded_refine_section(section: ReportSection) -> bool:
    """True when the re-search produced an empty/degraded section — the
    caller keeps the original so the loop's no-improvement break fires
    (never regress)."""
    return not section.cited_passage_ids or "[[" not in section.markdown


def _merge_refine_passages(
    new_result: SubQuestionResult,
    passages_by_id_obj: dict[str, RetrievalPassage],
    passages_text: dict[str, str],
    fresh_passages: dict[str, RetrievalPassage],
    fresh_hits: list[Any],
) -> None:
    """Defect-2 fix (fresh evidence must not be discarded): merge the refine
    leg's passages into the shared index so (i) the NEXT judge round can see
    fresh-cited claims (extract_section_claims skips ids with no text, so
    without this a fresh citation vanishes and the section scores as
    vacuously "converged"), and (ii) `fresh_passages` carries them out to the
    final report so `_assemble_report` resolves the new `[[id]]` citations to
    source cards. The refine leg's discovered urls join the report's
    all-searched set."""
    for p in new_result.passages:
        if p.id not in passages_by_id_obj:
            passages_by_id_obj[p.id] = p
        passages_text.setdefault(p.id, p.text)
        fresh_passages.setdefault(p.id, p)
    fresh_hits.extend(getattr(new_result, "all_hits", None) or [])


async def iterative_refine(
    run: DeepResearchRun,
    sections: list[ReportSection],
    results: list[SubQuestionResult],
    carried_passages: list[RetrievalPassage],
    emit: EmitFn,
) -> tuple[list[ReportSection], list[RetrievalPassage], list[Any]]:
    """A4.4 — the iterative-research loop, wired to the real engine.

    Consumes the A4.0/A4.1/A4.2 core: judge every section's claims, re-search
    the weak ones (seeded with that section's ORIGINAL passages so
    re-synthesis sees the COMBINED corpus), re-synthesize, and re-judge — up
    to the loop's round cap, stopping once enough claims are SUPPORTED. Only
    reached when `run._iterative` is True; the OFF path never calls this.

    Returns `(refined_sections, fresh_passages, fresh_hits)`. `fresh_passages`
    are the passages the refine legs newly gathered; `run()` folds them into
    the report's passage set so a section's fresh `[[id]]` citations resolve
    to source cards (without them, `_assemble_report` could not see them)."""
    passages_by_id_obj = _index_passages_by_id(results, carried_passages)
    # `passages_text` is the {id: text} the judge/extractor need.
    passages_text = {pid: p.text for pid, p in passages_by_id_obj.items()}
    # Passages the refine legs newly gather — returned so they reach the final
    # report (so fresh `[[id]]` citations resolve to source cards). Keyed by id
    # to dedup across rounds; insertion order preserved for stable assembly.
    fresh_passages: dict[str, RetrievalPassage] = {}
    # Refine legs also DISCOVER urls; collect their all_hits so the report's
    # "All-Searched" audit trail stays the COMPLETE discovery set (else
    # iterative runs would silently omit the refine legs' searches).
    fresh_hits: list[Any] = []

    async def judge_section(sec: ReportSection) -> list[ClaimVerdict]:
        claims = extract_section_claims(sec.markdown, passages_text)
        # The judge's `_Completer` protocol requires the `context=` kwarg the
        # public `LLMRouter` Protocol omits but the concrete DefaultLLMRouter
        # accepts (same omission gather.py/synthesis.py cast around). The
        # cast is a typing-only narrowing — identity at runtime.
        return await judge_claims(claims, router=cast(_Completer, run._router))

    async def refine_section(sec: ReportSection, weak: list[ClaimVerdict]) -> ReportSection:
        orig = _seed_orig_passages(sec, passages_by_id_obj)
        new_subq = _build_refine_subq(sec, weak)
        # One fresh gather leg, seeded with `orig` (NOT the upload passages).
        steer_budget = max(1, run._bound.max_sources // max(1, len(sections)))
        _subq, task, _sid, namespace, leg_context = run._start_one_steer_task(
            new_subq, steer_budget, emit=emit, extra_passages=orig
        )
        await _seed_refine_namespace(run, orig, namespace)
        new_result = await _run_refine_leg(task)
        if new_result is None:
            return sec
        new_section = await _synthesize_refine_section(
            run, new_result, sec, namespace, leg_context, emit
        )
        if new_section is None or _is_degraded_refine_section(new_section):
            return sec
        _merge_refine_passages(
            new_result, passages_by_id_obj, passages_text, fresh_passages, fresh_hits
        )
        return new_section

    await emit("phase", {"phase": "iterate"})
    res = await run_iterative_refinement(
        sections,
        judge_section=judge_section,
        refine_section=refine_section,
        emit=emit,
    )
    await emit(
        "phase",
        {
            "phase": "iterate",
            "rounds": res.rounds,
            "supported": res.final_supported,
            "converged": res.converged,
        },
    )
    return res.sections, list(fresh_passages.values()), fresh_hits
