"""Cited-passage / all-hits collection for the final report, extracted from
`DeepResearchRun._assemble_report`.

Pure data in/out (no `DeepResearchRun` needed) — the class method keeps the
trivial `ReportFromRun(...)` construction itself (it already has `self._query`
/ `self._depth` in scope), so this module never needs to import `engine.py`
at all.
"""

from __future__ import annotations

from typing import Any

from disco.core import ReportSection

from ...models import Passage as RetrievalPassage
from ...url_policy import source_url_key
from ..gather import SubQuestionResult


def _dedupe_passages(
    carried_passages: list[RetrievalPassage], results: list[SubQuestionResult]
) -> list[RetrievalPassage]:
    corpus: list[RetrievalPassage] = []
    seen: set[str] = set()
    groups = [carried_passages, *(result.passages for result in results)]
    for group in groups:
        for passage in group:
            if passage.id in seen:
                continue
            seen.add(passage.id)
            corpus.append(passage)
    return corpus


def collect_report_data(
    sections: list[ReportSection],
    results: list[SubQuestionResult],
    carried_passages: list[RetrievalPassage],
    carried_hits: list[Any],
) -> tuple[list[RetrievalPassage], list[RetrievalPassage], list[Any], int]:
    """Collect cited and reviewed-but-uncited passages plus deduped hits.

    Carried
    (resumed) passages/hits come first so a resumed run's citations resolve
    against the sources its earlier sections actually used. Returns
    `(cited_passages, reviewed_passages, all_hits, unsupported_total)`; keeping
    the two passage sets distinct preserves the public cited-source contract
    without discarding evidence needed by later follow-ups."""
    cited_ids: set[str] = set()
    for s in sections:
        cited_ids.update(s.cited_passage_ids)
    reviewed_corpus = _dedupe_passages(carried_passages, results)
    cited_passages = [p for p in reviewed_corpus if p.id in cited_ids]
    reviewed_passages = [p for p in reviewed_corpus if p.id not in cited_ids]
    all_hits: list[Any] = []
    hit_urls: set[str] = set()
    for h in carried_hits:
        key = source_url_key(h.url)
        if key not in hit_urls:
            hit_urls.add(key)
            all_hits.append(h)
    for r in results:
        for h in r.all_hits:
            key = source_url_key(h.url)
            if key not in hit_urls:
                hit_urls.add(key)
                all_hits.append(h)
    unsupported_total = sum(s.unsupported_count for s in sections)
    return cited_passages, reviewed_passages, all_hits, unsupported_total
