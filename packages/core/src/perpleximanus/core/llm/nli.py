"""NLI verifier — llm-router-contract.md §9.2.

The NLI_VERIFIER role is special: per BoD §14/§15.2 it is **a ~300M cross-encoder,
not an LLM**. It does NOT go through the router's `complete()` (it is not a chat
model); it is registered as a role only for config/routing-eligibility symmetry.
The grounding subsystem (§14, separate doc) consumes `NLIVerifier` directly.

v1 ships the protocol plus a deterministic stub so downstream code can wire
against the interface; the real cross-encoder (e.g. a DeBERTa-v3-MNLI-class
model) is built with the grounding subsystem.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

Entailment = Literal["entail", "neutral", "contradict"]


@runtime_checkable
class NLIVerifier(Protocol):
    """[CONTRACT] 3-way entailment for citation grounding (BoD §14). Premise =
    retrieved passage; hypothesis = a claim. Local cross-encoder by default."""

    def entail(self, premise: str, hypothesis: str) -> Entailment: ...

    def score(self, premise: str, hypothesis: str) -> float: ...  # entailment prob


class StubNLIVerifier:
    """Deterministic placeholder satisfying `NLIVerifier` (no model).

    A naive lexical-overlap heuristic so downstream wiring + tests have concrete
    behavior; replaced by the real cross-encoder in the grounding subsystem.
    NOT a quality signal — it exists to make the interface live.
    """

    def score(self, premise: str, hypothesis: str) -> float:
        hyp_tokens = set(hypothesis.lower().split())
        if not hyp_tokens:
            return 0.0
        prem_tokens = set(premise.lower().split())
        return len(hyp_tokens & prem_tokens) / len(hyp_tokens)

    def entail(self, premise: str, hypothesis: str) -> Entailment:
        s = self.score(premise, hypothesis)
        if s >= 0.6:
            return "entail"
        if s <= 0.1:
            return "contradict"
        return "neutral"
