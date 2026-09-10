"""The research loop's mutable state, and the quality filter that guards it.

Split out of ``agent.py`` when the loop grew turn accounting, a system-owned
re-issue queue, and a hold: the loop file has to stay readable as a loop, and
the state it folds evidence into is a separate concern with its own invariants
(dedupe by id AND by url key, charge the source budget once, keep the audit
trail cumulative across a resume).

``agent.py`` re-exports both names, so ``agent._AgentState`` still resolves for
every existing caller and test.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from .._extraction_text import challenge_page
from ..models import Passage, RetrievalResult, SearchHit
from ..ranking import merge_search_hits
from ..url_policy import source_url_key
from ._budget import SourceBudget
from ._exhaustion import ExtractionOutcomes
from ._reissue_queue import ReissueQueue

_METADATA_LINE = re.compile(
    r"^(?:last (?:verified|updated)|published):[^.!?\n]{0,100}$", re.IGNORECASE
)
_FURNITURE_WORDS = frozenset(
    "home about contact us privacy policy terms of service copyright all rights reserved".split()
)


def _report_usable_passage(passage: Passage) -> bool:
    """Reject obvious extraction furniture before it consumes source capacity.
    Retrieval remains auditable through ``all_hits``; the filter is structural
    and conservative so substantive claims are never dropped on an inferred
    topic judgment. (Moved from the v1 gather loop.)"""
    if passage.corpus_id is None and challenge_page(passage.source_title, passage.text):
        return False
    text = " ".join(
        line for line in passage.text.splitlines() if not _METADATA_LINE.fullmatch(line.strip())
    )
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    words = re.findall(r"[^\W\d_]+", text.casefold())
    # Judge empty/navigation-only extraction, not the topic, table syntax, or
    # writing system. A privacy policy can itself be the requested primary source.
    return sum(len(word) for word in words) >= 20 and bool(set(words) - _FURNITURE_WORDS)


def _retrieved_source_passages(retrieval: RetrievalResult) -> list[Passage]:
    """Retain each selected web document instead of losing its unranked context.

    Ranking still selects which sources enter. Admission already deduplicates
    URLs, so retaining only a ranked chunk would permanently hide the rest of
    an already-fetched source. A new content-derived ID preserves the narrower
    passage's identity; corpus and passage-only providers keep their spans.
    """
    documents = {
        source_url_key(doc.url): doc
        for doc in retrieval.extracted
        if doc.fetched_ok and doc.status == "ok" and doc.content.strip()
    }
    passages: list[Passage] = []
    for passage in retrieval.passages:
        key = source_url_key(passage.source_url)
        document = documents.get(key)
        if passage.corpus_id is not None or document is None or document.content == passage.text:
            passages.append(passage)
            continue
        identity = hashlib.sha256(f"{key}\0{document.content}".encode()).hexdigest()[:24]
        passages.append(
            passage.model_copy(
                update={
                    "id": f"web_{identity}",
                    "source_title": document.title or passage.source_title,
                    "text": document.content,
                    "char_start": 0,
                    "char_end": len(document.content),
                }
            )
        )
    return passages


@dataclass
class _AgentState:
    budget: SourceBudget
    pool: list[Passage] = field(default_factory=list)
    all_hits: list[SearchHit] = field(default_factory=list)
    trail: list[dict[str, Any]] = field(default_factory=list)
    seen_ids: set[str] = field(default_factory=set)
    seen_urls: set[str] = field(default_factory=set)
    seen_hit_urls: set[str] = field(default_factory=set)
    last_admitted: set[str] = field(default_factory=set)
    brief: str = ""
    decision_summary: str = ""
    coverage: dict[str, Any] = field(
        default_factory=lambda: {
            "covered": [],
            "open": [],
            "contradictions_checked": [],
        }
    )
    turns_completed: int = 0
    # How much of THIS execution's research-turn budget the model has spent.
    # A turn whose every query died in infrastructure is not the model's, so it
    # is not charged here (`_turn_accounting`); a resumed run starts at zero and
    # gets its own full allowance, exactly as it gets its own SourceBudget.
    turns_charged: int = 0
    feedback: str = ""
    searches: int = 0
    # Untested queries the LOOP owns re-running, so the model never spends its
    # turns doing the host's retry.
    reissue: ReissueQueue = field(default_factory=ReissueQueue)
    # Per-URL extraction results by failure class. An empty pool at the budget
    # wall is a different finding depending on these: a run that searched the
    # world and found nothing, or a run whose extractor was down the whole time.
    extraction: ExtractionOutcomes = field(default_factory=ExtractionOutcomes)

    def admit_exempt(self, passages: list[Passage]) -> list[Passage]:
        """Admit user-provided passages directly: exempt from the quality
        filter AND the web-source budget (uploads/injections never consume
        the retrieval allowance)."""
        fresh: list[Passage] = []
        for passage in passages:
            if passage.id in self.seen_ids:
                continue
            self.seen_ids.add(passage.id)
            if passage.source_url:
                self.seen_urls.add(source_url_key(passage.source_url))
            self.pool.append(passage)
            fresh.append(passage)
        return fresh

    def admit_retrieved(self, retrieval: RetrievalResult) -> int:
        """Quality-filter, dedup (id + url), and budget-charge one retrieval
        result's passages; accumulate its deduped discovery hits."""
        self.extraction.record(retrieval.extracted)
        candidates: list[Passage] = []
        candidate_ids: set[str] = set()
        candidate_urls: set[str] = set()
        for passage in _retrieved_source_passages(retrieval):
            key = source_url_key(passage.source_url)
            if (
                passage.id in self.seen_ids
                or passage.id in candidate_ids
                or key in self.seen_urls
                or key in candidate_urls
                or not _report_usable_passage(passage)
            ):
                continue
            candidate_ids.add(passage.id)
            candidate_urls.add(key)
            candidates.append(passage)
        admitted, _charged = self.budget.admit(candidates)
        for passage in admitted:
            self.seen_ids.add(passage.id)
            self.seen_urls.add(source_url_key(passage.source_url))
            self.pool.append(passage)
            self.last_admitted.add(passage.id)
        for hit in retrieval.all_hits:
            key = source_url_key(hit.url)
            if key in self.seen_hit_urls:
                for index, existing in enumerate(self.all_hits):
                    if source_url_key(existing.url) == key:
                        self.all_hits[index] = merge_search_hits(existing, hit)
                        break
                continue
            self.seen_hit_urls.add(key)
            self.all_hits.append(hit)
        return len(admitted)

    def restore_trail_state(self) -> None:
        """Restore model-owned state from a stopped run's audit trail."""
        search_turns = {
            entry["turn"]
            for entry in self.trail
            if entry.get("kind") == "search" and isinstance(entry.get("turn"), int)
        }
        for entry in self.trail:
            if entry.get("kind") == "brief" and isinstance(entry.get("text"), str):
                self.brief = entry["text"]
            elif entry.get("kind") == "decision" and isinstance(entry.get("text"), str):
                self.decision_summary = entry["text"]
            elif entry.get("kind") == "coverage" and isinstance(entry.get("coverage"), dict):
                self.coverage = entry["coverage"]
        # A research turn earns effort credit only when it issued a fresh
        # query. Multiple queries in one turn count once; queryless decisions,
        # malformed turns, and rejected repeats do not.
        self.turns_completed = len(search_turns)


__all__ = ["_AgentState", "_report_usable_passage"]
