"""Cited-passage / all-hits collection for the final report.

Pure data in/out (no `DeepResearchRun` needed): splits the admitted evidence
pool into cited vs reviewed-but-uncited against the written sections, and
dedupes discovery hits. Carried (resumed) hits come first so a resumed run's
citations resolve against the sources its earlier evidence actually used.
"""

from __future__ import annotations

from typing import Any

from disco.core import ReportSection

from ...models import Passage as RetrievalPassage
from ...url_policy import source_url_key


def collect_report_data(
    sections: list[ReportSection],
    pool: list[RetrievalPassage],
    carried_hits: list[Any],
    new_hits: list[Any],
) -> tuple[list[RetrievalPassage], list[RetrievalPassage], list[Any]]:
    """Return `(cited_passages, reviewed_passages, all_hits)`.

    Keeping the two passage sets distinct preserves the public cited-source
    contract without discarding evidence needed by later follow-ups."""
    cited_ids: set[str] = set()
    for section in sections:
        cited_ids.update(section.cited_passage_ids)
    seen: set[str] = set()
    corpus: list[RetrievalPassage] = []
    for passage in pool:
        if passage.id in seen:
            continue
        seen.add(passage.id)
        corpus.append(passage)
    cited_passages = [p for p in corpus if p.id in cited_ids]
    reviewed_passages = [p for p in corpus if p.id not in cited_ids]
    all_hits: list[Any] = []
    hit_urls: set[str] = set()
    for hit in [*carried_hits, *new_hits]:
        key = source_url_key(hit.url)
        if key not in hit_urls:
            hit_urls.add(key)
            all_hits.append(hit)
    return cited_passages, reviewed_passages, all_hits
