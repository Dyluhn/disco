"""Mutable evidence state owned by the agent loop."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..models import Passage, RetrievalResult, SearchHit
from ..ranking import merge_search_hits
from ..url_policy import source_url_key
from ._budget import SourceBudget


def report_usable_passage(passage: Passage) -> bool:
    text = " ".join(passage.text.split())
    if len(re.findall(r"[A-Za-z][\w'-]*", text)) < 5:
        return False
    lowered = text.casefold()
    return not (
        lowered.startswith(("last verified:", "last updated:", "published:"))
        or "privacy policy" in lowered
        or "terms of service" in lowered
        or text.count("|") >= 2
    )


@dataclass
class AgentState:
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
        default_factory=lambda: {"covered": [], "open": [], "contradictions_checked": []}
    )
    turns_completed: int = 0
    feedback: str = ""
    searches: int = 0

    def admit_exempt(self, passages: list[Passage]) -> list[Passage]:
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
        candidates: list[Passage] = []
        candidate_ids: set[str] = set()
        candidate_urls: set[str] = set()
        for passage in retrieval.passages:
            key = source_url_key(passage.source_url)
            if (
                passage.id in self.seen_ids
                or passage.id in candidate_ids
                or key in self.seen_urls
                or key in candidate_urls
                or not report_usable_passage(passage)
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
        self.turns_completed = len(search_turns)
