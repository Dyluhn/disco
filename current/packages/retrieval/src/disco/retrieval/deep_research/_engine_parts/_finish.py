"""The one report-finalization funnel after adaptive gathering completes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...models import Passage as RetrievalPassage
from .. import engine
from ..gather import SubQuestionResult
from ._compiler import compile_report_sections
from ._drain import _dominant_bound

if TYPE_CHECKING:
    from ..engine import DeepResearchRun, EmitFn, ReportFromRun


async def finish_report(
    run: DeepResearchRun,
    results: list[SubQuestionResult],
    carried_passages: list[RetrievalPassage],
    carried_hits: list[Any],
    bounded_by: str | None,
    *,
    emit: EmitFn,
) -> ReportFromRun:
    """Compile, verify, summarize, and assemble the single report artifact.

    Every stage either produces a valid artifact or raises to the run; there
    is no alternate constructor and no timeout-degraded output. Calls resolve
    `compile_report_plan`/`synthesize_section`/`coherence_pass` through the
    `engine` module attributes so test monkeypatches keep working.
    """
    sections = await compile_report_sections(run, results, carried_passages, emit=emit)
    if run._source_budget.remaining <= 0:
        bounded_by = _dominant_bound(bounded_by, "sources")

    await emit("phase", {"phase": "coherence"})
    summary_passages = [
        *carried_passages,
        *(passage for result in results for passage in result.passages),
    ]
    summary = await engine.coherence_pass(
        run._query,
        sections,
        router=run._router,
        passages=summary_passages,
        nli=run._nli,
        recency_window=run._recency_window,
    )
    return run._assemble_report(
        sections=sections,
        results=results,
        carried_passages=carried_passages,
        carried_hits=carried_hits,
        summary=summary,
        bounded_by=bounded_by,
    )


__all__ = ["finish_report"]
