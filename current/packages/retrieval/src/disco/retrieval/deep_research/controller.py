"""Adaptive probe scheduling for Deep Research.

The plan is an initial set of *research probes*.  A probe is a hypothesis about
where useful evidence may be found; it is not a promise that the final report
will contain a section with the same title.  This module deliberately contains
no provider or LLM calls.  It owns the bounded scheduling policy and gives the
engine a small, deterministic seam for global evidence assessment.

The source cap is shared by the gather legs (``SourceBudget``).  Consequently
the controller never leases an irreversible equal slice to a probe: a probe
that finishes early leaves capacity for the next batch or a targeted pivot.
"""

from __future__ import annotations

import hashlib
import re
from collections import deque
from dataclasses import dataclass
from typing import Literal

from ..models import Passage
from .decompose import SubQuestion
from .depth import DepthBound
from .gather import SubQuestionResult

ProbeActionKind = Literal["add", "merge", "retire", "pivot"]
ProbeStatus = Literal["queued", "running", "complete", "retired", "merged"]


@dataclass(frozen=True)
class EvidenceCapacity:
    """Report-worthy evidence capacity accumulated by the run."""

    passages: int
    sources: int
    themes: int
    required_passages: int
    required_sources: int
    required_themes: int

    @property
    def sufficient(self) -> bool:
        return (
            self.passages >= self.required_passages
            and self.sources >= self.required_sources
            and self.themes >= self.required_themes
        )


@dataclass(frozen=True)
class ResearchProbe:
    """A bounded unit of evidence collection."""

    id: str
    query: str
    origin: Literal["initial", "follow_up", "pivot"] = "initial"
    parent_id: str | None = None
    theme_id: str = ""

    def to_subquestion(self) -> SubQuestion:
        return SubQuestion(title=self.query)


@dataclass(frozen=True)
class ProbeAction:
    """A global evidence decision made after a batch completes."""

    kind: ProbeActionKind
    probe: ResearchProbe | None = None
    target_id: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class ProbeAssessment:
    """Observable batch assessment, useful to the run and to focused tests."""

    completed: int
    evidence: int
    actions: tuple[ProbeAction, ...] = ()


def _probe_id(query: str) -> str:
    return "p" + hashlib.sha256(query.strip().casefold().encode()).hexdigest()[:10]


def _tokens(query: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", query.casefold()) if len(token) > 2}


_INTENT_WORDS = {
    "primary", "sources", "source", "evidence", "deeper", "deepen",
    "broader", "context", "counterevidence", "counter", "limitations",
    "competing", "views", "independent", "mechanisms", "perspectives",
}


def _theme_id(query: str) -> str:
    """Stable theme identity independent of search-intent suffixes."""
    tokens = sorted(_tokens(query) - _INTENT_WORDS)
    return "theme:" + "-".join(tokens[:16]) if tokens else "theme:general"


class ResearchController:
    """Bounded adaptive scheduler used for every run.

    Initial work is released in small batches.  After each batch, evidence is
    assessed globally: duplicate probes merge, empty probes pivot, and probes
    whose evidence is already represented retire.  Every action is bounded by
    ``DepthBound.max_subquestions`` and the shared source budget checked by the
    caller.  The controller is intentionally synchronous so scheduling cannot
    race with the event-log consumer.
    """

    def __init__(self, bound: DepthBound, *, batch_size: int | None = None) -> None:
        self.bound = bound
        default_batch = {"shallow": 2, "standard": 3, "deep": 4}[bound.retrieval_depth]
        self.batch_size = max(1, min(batch_size or default_batch, bound.max_subquestions))
        self._queue: deque[ResearchProbe] = deque()
        self._states: dict[str, ProbeStatus] = {}
        self._probes: dict[str, ResearchProbe] = {}
        self._scheduled = 0
        self._completed_queries: list[str] = []
        self._evidence_ids: set[str] = set()
        self._evidence_sources: set[str] = set()
        self._theme_evidence: dict[str, set[str]] = {}
        self._theme_sources: dict[str, set[str]] = {}
        self._capacity_queries: set[str] = set()
        self._initial_probe_ids: list[str] = []
        self._capacity_cursor = 0

    @property
    def scheduled(self) -> int:
        return self._scheduled

    @property
    def active(self) -> int:
        return sum(state in {"queued", "running"} for state in self._states.values())

    @property
    def completed_queries(self) -> list[str]:
        """Probe queries durably completed by this execution, in run order."""
        return list(dict.fromkeys(self._completed_queries))

    @property
    def pending_queries(self) -> list[str]:
        """Queued or interrupted probes that a stopped run must resume."""
        return [
            probe.query
            for probe in self._probes.values()
            if self._states.get(probe.id) in {"queued", "running"}
        ]

    def seed(self, queries: list[str]) -> None:
        """Load the approved plan as probes, de-duplicating empty/repeated items."""
        for query in queries:
            cleaned = query.strip()
            if not cleaned:
                continue
            probe = ResearchProbe(_probe_id(cleaned), cleaned, theme_id=_theme_id(cleaned))
            if probe.id in self._probes:
                continue
            self._probes[probe.id] = probe
            self._states[probe.id] = "queued"
            self._queue.append(probe)
            self._initial_probe_ids.append(probe.id)

    def restore_completed(self, queries: set[str]) -> None:
        """Retire probes recorded by a prior stopped report."""
        for probe in self._probes.values():
            if probe.query in queries:
                self._states[probe.id] = "retired"
                self._completed_queries.append(probe.query)

    def restore_evidence(self, passages: list[Passage], completed_queries: set[str]) -> None:
        """Restore persisted evidence capacity without recreating probe sections."""
        usable_ids: list[str] = []
        for passage in passages:
            text = " ".join(passage.text.split())
            if len(re.findall(r"[A-Za-z][\w'-]*", text)) < 5 or not passage.id:
                continue
            self._evidence_ids.add(passage.id)
            usable_ids.append(passage.id)
            if passage.source_url:
                self._evidence_sources.add(passage.source_url)
        if not usable_ids:
            return
        for index, query in enumerate(sorted(completed_queries)):
            passage_id = usable_ids[index % len(usable_ids)]
            theme = _theme_id(query)
            self._theme_evidence.setdefault(theme, set()).add(passage_id)

    def next_batch(self) -> list[ResearchProbe]:
        """Release at most ``batch_size`` queued probes."""
        batch: list[ResearchProbe] = []
        while (
            self._queue
            and len(batch) < self.batch_size
            and self._scheduled < self.bound.max_subquestions
        ):
            probe = self._queue.popleft()
            if self._states.get(probe.id) != "queued":
                continue
            self._states[probe.id] = "running"
            self._scheduled += 1
            batch.append(probe)
        return batch

    def queue_probe(
        self,
        query: str,
        *,
        origin: Literal["follow_up", "pivot"] = "follow_up",
        parent_id: str | None = None,
        theme_id: str | None = None,
        priority: bool = False,
    ) -> ProbeAction | None:
        """Queue one bounded follow-up probe for the next batch."""
        cleaned = query.strip()
        probe = ResearchProbe(
            _probe_id(cleaned), cleaned, origin=origin, parent_id=parent_id,
            theme_id=theme_id or _theme_id(cleaned),
        )
        if not cleaned:
            return None
        if not self._can_add(probe) and priority:
            victim = next(
                (
                    queued
                    for queued in reversed(self._queue)
                    if self._states.get(queued.id) == "queued" and queued.origin == "initial"
                ),
                None,
            )
            if victim is not None:
                self._queue.remove(victim)
                self._states[victim.id] = "retired"
        if not self._can_add(probe):
            return None
        self._probes[probe.id] = probe
        self._states[probe.id] = "queued"
        self._queue.appendleft(probe)
        return ProbeAction("add", probe, target_id=parent_id, reason="adaptive follow-up")

    def assess(self, results: list[SubQuestionResult]) -> ProbeAssessment:
        """Apply global evidence/gap decisions to a completed batch.

        The gather loop already performs bounded per-probe gap checks.  This
        second, global check catches overlap and empty coverage across probes:
        an empty probe gets one pivot, overlapping probes merge into the first
        representative, and a probe with evidence is complete.  Follow-ups are
        queued for the next bounded batch, never run recursively in this call.
        """
        actions: list[ProbeAction] = []
        evidence = 0
        batch_ids = {_probe_id(result.subq.title) for result in results}
        for result in results:
            probe = self._probes.get(_probe_id(result.subq.title))
            if probe is None:
                continue
            self._states[probe.id] = "complete"
            self._completed_queries.append(probe.query)
            has_usable_evidence, admitted = self._absorb_probe_evidence(probe, result)
            evidence += admitted
            if has_usable_evidence:
                actions.append(ProbeAction("retire", probe, reason="evidence collected"))
            else:
                actions.append(self._gap_action(probe, result))
        actions.extend(self._merge_duplicate_probes(batch_ids))
        return ProbeAssessment(len(results), evidence, tuple(actions))

    def _absorb_probe_evidence(
        self, probe: ResearchProbe, result: SubQuestionResult
    ) -> tuple[bool, int]:
        """Pool a completed probe's usable passages; return (usable, admitted)."""
        theme = probe.theme_id or _theme_id(probe.query)
        theme_ids = self._theme_evidence.setdefault(theme, set())
        theme_sources = self._theme_sources.setdefault(theme, set())
        has_usable_evidence = False
        admitted = 0
        for passage in result.passages:
            # Admission is quality-filtered in gather.py; retain this
            # guard for hermetic callers and resumed probes.
            text = " ".join(getattr(passage, "text", "").split())
            if len(re.findall(r"[A-Za-z][\w'-]*", text)) < 5:
                continue
            passage_id = str(getattr(passage, "id", ""))
            if not passage_id:
                continue
            has_usable_evidence = True
            if passage_id in self._evidence_ids:
                continue
            self._evidence_ids.add(passage_id)
            admitted += 1
            theme_ids.add(passage_id)
            source = str(getattr(passage, "source_url", ""))
            if source:
                self._evidence_sources.add(source)
                theme_sources.add(source)
        return has_usable_evidence, admitted

    def _gap_action(self, probe: ResearchProbe, result: SubQuestionResult) -> ProbeAction:
        """One bounded pivot for an empty initial probe, else an honest retire.

        Empty evidence is a gap, so spend at most one bounded follow-up probe
        with a query that changes retrieval intent.
        """
        pivot_action = None
        if result.rounds_run > 0 and probe.origin == "initial":
            pivot_action = self.queue_probe(
                f"{probe.query} primary sources evidence",
                origin="pivot",
                parent_id=probe.id,
                theme_id=probe.theme_id,
            )
        if pivot_action is not None:
            return ProbeAction(
                "pivot", pivot_action.probe, target_id=probe.id, reason="empty evidence"
            )
        return ProbeAction("retire", probe, reason="empty evidence at probe cap")

    def _merge_duplicate_probes(self, batch_ids: set[str]) -> list[ProbeAction]:
        """Merge lexical duplicates across the completed batch.

        This affects scheduling/report identity, while the evidence itself
        remains pooled.
        """
        actions: list[ProbeAction] = []
        completed = [self._probes[pid] for pid in batch_ids if pid in self._probes]
        for index, probe in enumerate(completed):
            if self._states.get(probe.id) != "complete":
                continue
            for other in completed[index + 1 :]:
                if self._states.get(other.id) != "complete":
                    continue
                left, right = _tokens(probe.query), _tokens(other.query)
                if left and right and len(left & right) / max(1, len(left | right)) >= 0.75:
                    self._states[other.id] = "merged"
                    actions.append(
                        ProbeAction(
                            "merge",
                            other,
                            target_id=probe.id,
                            reason="overlapping evidence probe",
                        )
                    )
        return actions

    def capacity(self) -> EvidenceCapacity:
        """Return the current report-worthy evidence capacity."""
        return EvidenceCapacity(
            passages=len(self._evidence_ids),
            sources=len(self._evidence_sources),
            themes=sum(1 for ids in self._theme_evidence.values() if ids),
            required_passages=self.bound.min_evidence_passages,
            required_sources=self.bound.min_evidence_sources,
            required_themes=self.bound.min_evidence_themes,
        )

    def ensure_capacity(self, results: list[SubQuestionResult]) -> tuple[ProbeAction, ...]:
        """Queue bounded deepen/broaden/counterevidence probes when needed.

        Probes are disposable search inputs. Canonical themes and pooled
        evidence, rather than probe titles, are what the report compiler sees.
        """
        del results  # evidence is accumulated by assess(); retained for API clarity
        capacity = self.capacity()
        if capacity.sufficient or self._scheduled >= self.bound.max_subquestions:
            return ()
        # Cast the approved starting directions as a wide net before punching
        # down. Follow-ups never jump ahead of an untouched initial direction.
        if any(
            self._states.get(probe_id) == "queued" for probe_id in self._initial_probe_ids
        ):
            return ()

        actions: list[ProbeAction] = []
        initial = [self._probes[probe_id] for probe_id in self._initial_probe_ids]
        if not initial:
            return ()
        variants = (
            ("deepen", "mechanisms and primary empirical evidence"),
            ("deepen", "measured outcomes and implementation evidence"),
            ("broaden", "comparative alternatives and tradeoffs"),
            ("broaden", "historical context and current state"),
            ("counterevidence", "limitations counterevidence and failure cases"),
            ("counterevidence", "independent criticism and unresolved disputes"),
            ("deepen", "latest primary data and concrete case studies"),
            ("broaden", "economic institutional and operational implications"),
            ("broaden", "stakeholder perspectives and policy implications"),
        )
        remaining_slots = self.bound.max_subquestions - (
            self._scheduled
            + sum(state == "queued" for state in self._states.values())
        )
        attempts = 0
        max_attempts = len(initial) * len(variants)
        while len(actions) < min(self.batch_size, remaining_slots) and attempts < max_attempts:
            cursor = self._capacity_cursor % max_attempts
            self._capacity_cursor += 1
            attempts += 1
            base = initial[cursor % len(initial)]
            reason, suffix = variants[cursor % len(variants)]
            query = f"{base.query} {suffix}"
            action = self._queue_capacity_probe(
                query,
                reason,
                theme_id=_theme_id(query) if reason == "broaden" else base.theme_id,
            )
            if action is not None:
                actions.append(action)
        return tuple(actions)

    def _queue_capacity_probe(
        self, query: str, reason: str, *, theme_id: str
    ) -> ProbeAction | None:
        cleaned = query.strip()
        if not cleaned or cleaned in self._capacity_queries:
            return None
        self._capacity_queries.add(cleaned)
        action = self.queue_probe(cleaned, origin="follow_up", theme_id=theme_id)
        if action is None:
            return None
        return ProbeAction(action.kind, action.probe, action.target_id, reason=reason)

    def _can_add(self, probe: ResearchProbe) -> bool:
        return (
            self._scheduled
            + sum(state == "queued" for state in self._states.values())
            < self.bound.max_subquestions
            and probe.id not in self._probes
        )
