"""NLI verifier — retrieval-grounding-contract.md §5.1.

Implements the router contract's `NLIVerifier` protocol: a ~300M cross-encoder
(DeBERTa-v3-large-MNLI / HHEM class), NOT a chat model — it does not go through
the router's `complete()` (router §9.2). The shipped `CrossEncoderNLIVerifier`
is a deterministic lexical stub standing in for the real checkpoint ([VERIFY])
so the grounding pipeline is testable headless; it satisfies the entail/score
interface.
"""

from __future__ import annotations

import re
from typing import Literal

Entailment = Literal["entail", "neutral", "contradict"]
_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


class CrossEncoderNLIVerifier:
    """[CONTRACT] Satisfies the router's `NLIVerifier`. Local, cheap, off the LLM
    path. The lexical heuristic here is a stand-in for the real cross-encoder —
    `score` is the fraction of hypothesis tokens supported by the premise; a real
    checkpoint distinguishes contradiction from mere non-entailment, which this
    stub approximates (low support → 'contradict')."""

    def __init__(self, *, entail_threshold: float = 0.6, contradict_threshold: float = 0.2) -> None:
        self._entail = entail_threshold
        self._contradict = contradict_threshold

    def score(self, premise: str, hypothesis: str) -> float:
        hyp = _tokens(hypothesis)
        if not hyp:
            return 0.0
        return len(hyp & _tokens(premise)) / len(hyp)

    def entail(self, premise: str, hypothesis: str) -> Entailment:
        s = self.score(premise, hypothesis)
        if s >= self._entail:
            return "entail"
        if s <= self._contradict:
            return "contradict"
        return "neutral"
