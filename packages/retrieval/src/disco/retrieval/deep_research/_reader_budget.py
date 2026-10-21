"""Finite reader output capacity, charged through the existing source checkpoint."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from disco.core.env import disco_env
from disco.core.llm import ModelRole

from ._source_notes import SourceNotes

_DEFAULT_CALL_TOKENS = 12_000
_DEFAULT_STAGE_TOKENS = 48_000


def _configured_limit(name: str, default: int) -> int:
    try:
        value = int(disco_env(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


def charged_output(note: SourceNotes, call_limit: int) -> int:
    if note.reader_output_tokens_charged is not None:
        return note.reader_output_tokens_charged
    # Old checkpoints have actual usage but no pre-dispatch reservation. When
    # usage is absent, retain a conservative allowance for the attempted calls.
    return note.output_tokens or note.calls * call_limit


@dataclass
class ReaderBudget:
    call_limit: int
    stage_limit: int
    used: int

    @classmethod
    def start(cls, retained: Mapping[str, SourceNotes]) -> ReaderBudget:
        call = _configured_limit("RESEARCH_READER_MAX_OUTPUT_TOKENS", _DEFAULT_CALL_TOKENS)
        stage = _configured_limit("RESEARCH_READER_OUTPUT_BUDGET_TOKENS", _DEFAULT_STAGE_TOKENS)
        return cls(call, stage, sum(charged_output(note, call) for note in retained.values()))

    def reserve(self, note: SourceNotes, router: Any, *, allocation_slots: int) -> int:
        remaining = max(0, self.stage_limit - self.used)
        # Share available capacity before dispatch. A one-token floor permits
        # progress with tiny budgets without ever reserving beyond the remainder.
        share = max(1, remaining // allocation_slots)
        capacity = min(self.call_limit, remaining, share)
        declared_limit = getattr(router, "output_token_limit", None)
        if callable(declared_limit):
            configured = declared_limit(ModelRole.RAG_ANSWERER)
            if type(configured) is int and configured > 0:
                capacity = min(capacity, configured)
        note.reader_output_tokens_charged = charged_output(note, self.call_limit) + capacity
        self.used += capacity
        return capacity

    def settle(self, note: SourceNotes, reserved: int, reported: int) -> None:
        # Missing usage and interrupted/provider-failed calls retain their full
        # reservation. Never use streamed delta counts as token accounting.
        if reported <= 0:
            return
        adjustment = reported - reserved
        note.reader_output_tokens_charged = (note.reader_output_tokens_charged or 0) + adjustment
        self.used += adjustment
