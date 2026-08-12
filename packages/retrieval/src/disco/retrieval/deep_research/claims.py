"""A4.1 — extract judgeable claims from a synthesized section.

A "claim" is a substantive cited or uncited statement in the section markdown —
the same definition the shared grounding verifier uses.
This bridges a `ReportSection`'s markdown + the run's passages into the
``(claim_text, [cited_passage_text, …])`` tuples the LLM judge (A4.0) consumes.

The citation syntax ``[[passage_id]]`` is a stable rendering contract resolved by the
citation UI. Claim parsing is imported from the canonical grounding component.
"""

from __future__ import annotations

from ..grounding import _claim_spans, _plain


def extract_section_claims(
    markdown: str, passages_by_id: dict[str, str]
) -> list[tuple[str, list[str]]]:
    """Return ``[(claim_text, [passage_text, …]), …]`` for each cited sentence.

    Uncited claims and claims whose ids are unknown are retained with an empty
    passage list, so the judge marks them unsupported instead of treating an
    ungrounded section as vacuously complete.
    """
    out: list[tuple[str, list[str]]] = []
    for span in _claim_spans(markdown):
        texts = [
            passages_by_id[passage_id]
            for passage_id in span.cited_passage_ids
            if passage_id in passages_by_id
        ]
        out.append((_plain(span.text), texts))
    return out
