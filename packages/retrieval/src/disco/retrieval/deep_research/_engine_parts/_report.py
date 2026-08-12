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


def collect_report_data(
    sections: list[ReportSection],
    results: list[SubQuestionResult],
    carried_passages: list[RetrievalPassage],
    carried_hits: list[Any],
) -> tuple[list[RetrievalPassage], list[Any], int]:
    """Collect the passages actually cited by some section (deduped) and the
    deduped all_hits, plus the total unsupported-claim count. Carried
    (resumed) passages/hits come first so a resumed run's citations resolve
    against the sources its earlier sections actually used. Returns
    `(all_passages, all_hits, unsupported_total)`."""
    cited_ids: set[str] = set()
    for s in sections:
        cited_ids.update(s.cited_passage_ids)
    all_passages: list[RetrievalPassage] = []
    seen: set[str] = set()
    for p in carried_passages:
        if p.id in cited_ids and p.id not in seen:
            seen.add(p.id)
            all_passages.append(p)
    for r in results:
        for p in r.passages:
            if p.id in cited_ids and p.id not in seen:
                seen.add(p.id)
                all_passages.append(p)
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
    return all_passages, all_hits, unsupported_total
