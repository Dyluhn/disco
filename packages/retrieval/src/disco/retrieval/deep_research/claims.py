"""A4.1 — extract judgeable claims from a synthesized section.

A "claim" is the sentence that PRECEDES a citation cluster in the section markdown —
the same definition the per-claim NLI verifier uses (streaming.py `_verify_claims`).
This bridges a `ReportSection`'s markdown + the run's passages into the
``(claim_text, [cited_passage_text, …])`` tuples the LLM judge (A4.0) consumes.

The citation syntax ``[[passage_id]]`` is a stable rendering contract (it is what the
citation UI resolves), so the three small parse regexes are duplicated here rather than
importing streaming.py's privates — keeping this module light + decoupled.
"""

from __future__ import annotations

import re

_CITE = re.compile(r"\[\[([\w-]+)\]\]")
# A cited sentence: lead text (non-greedy) followed by one-or-more [[id]] markers.
_CLUSTER = re.compile(r"(.*?)((?:\[\[[\w-]+\]\]\s*)+)", re.DOTALL)
_MD = re.compile(r"[*`#_>]+")  # emphasis/code/heading marks — noise for the judge


def _plain(text: str) -> str:
    return _MD.sub("", text).strip()


def extract_section_claims(
    markdown: str, passages_by_id: dict[str, str]
) -> list[tuple[str, list[str]]]:
    """Return ``[(claim_text, [passage_text, …]), …]`` for each cited sentence.

    Skips a cluster whose lead is empty (a citation with no preceding sentence) and
    one whose cited ids are all unknown (no passage text to judge against). The claim
    text is stripped of markdown emphasis so the judge sees clean prose.
    """
    out: list[tuple[str, list[str]]] = []
    for m in _CLUSTER.finditer(markdown):
        lead, cluster = m.group(1), m.group(2)
        ids = _CITE.findall(cluster)
        if not ids:
            continue
        claim = _plain(lead)
        if not claim:
            continue
        texts = [passages_by_id[i] for i in ids if i in passages_by_id]
        if not texts:
            continue
        out.append((claim, texts))
    return out
