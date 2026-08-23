"""Post-research report compilation for :mod:`deep_research.engine`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from disco.core import ReportSection
from disco.core.llm import CallContext

from ...models import Passage as RetrievalPassage
from .. import engine
from ..decompose import SubQuestion
from ..gather import GatherLegContext, SubQuestionResult
from ..report_compiler import ReportCompilationError, ReportSectionSpec

if TYPE_CHECKING:
    from ..engine import DeepResearchRun, EmitFn


async def _synthesize_spec(
    run: DeepResearchRun,
    spec: ReportSectionSpec,
    passages: list[RetrievalPassage],
    *,
    emit: EmitFn,
    target_words: tuple[int, int],
) -> ReportSection:
    sub_result = SubQuestionResult(subq=SubQuestion(title=spec.title), passages=passages)
    context = GatherLegContext(
        subq_id=spec.id,
        namespace=f"{run._namespace}/report",
        call_context=CallContext(conversation_id=f"{run._namespace}/report/{spec.id}"),
    )
    section = await engine.synthesize_section(
        sub_result,
        router=run._router,
        embedder=run._embedder,
        vector_store=run._vector_store,
        namespace=context.namespace,
        nli=run._nli,
        section_id=spec.id,
        top_k_for_section=run._bound.rerank_top_k,
        emit=emit,
        leg_context=context,
        recency_window=run._recency_window,
        target_words=spec.target_words or target_words,
        report_thesis=spec.report_thesis,
        report_outline=spec.report_outline,
        section_purpose=spec.purpose,
    )
    return section.model_copy(update={"title": spec.title})


async def compile_report_sections(
    run: DeepResearchRun,
    results: list[SubQuestionResult],
    carried_passages: list[RetrievalPassage],
    *,
    emit: EmitFn,
) -> list[ReportSection]:
    """Compile final sections from pooled evidence, never probe titles.

    The one report path: an evidence-led outline (one planner call plus one
    repair, inside `compile_report_plan`), then one written section per spec.
    Any stage that cannot produce a valid artifact raises to the run. Writing
    carries no global deadline — it is structurally bounded by the outline's
    section count and each call's own bounded retries.
    """
    specs, evidence = await engine.compile_report_plan(
        run._query,
        results,
        router=run._router,
        carried_passages=carried_passages,
        max_sections=run._bound.max_subquestions,
        report_spec=run._bound.report_spec,
    )
    by_id = {passage.id: passage for passage in evidence}
    section_count = max(1, len(specs))
    target_words = (
        max(180, run._bound.report_min_words // section_count),
        max(280, run._bound.report_max_words // section_count),
    )
    compiled: list[ReportSection] = []
    for spec in specs:
        passages = [by_id[pid] for pid in spec.evidence_ids if pid in by_id]
        if not passages:
            raise ReportCompilationError(
                f"planned section {spec.title!r} has no resolvable evidence"
            )
        await emit("phase", {"phase": "synthesize", "section": len(compiled) + 1})
        section = await _synthesize_spec(
            run, spec, passages, emit=emit, target_words=target_words
        )
        compiled.append(section)
        await emit(
            "section_done",
            {"section_id": section.id, "title": spec.title, "done": len(compiled)},
        )
    return compiled


__all__ = ["compile_report_sections"]
