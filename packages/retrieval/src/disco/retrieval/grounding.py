"""Grounding & citation pipeline — retrieval-grounding-contract.md §5.

Faithfulness is enforced, not asked (principle 5): the RAG_ANSWERER generates
cited prose, then every atomic claim is NLI-verified against its cited passage,
unsupported claims are self-corrected, and weak/unsupported claims + failed
sources are surfaced honestly (never hidden, BoD §13.3).
"""

from __future__ import annotations

import re
from typing import Literal, Protocol

from disco.core import LLMMessage
from disco.core.llm import CapabilityProfile, CompletionRequest, LLMRouter, ModelRole

from .models import Claim, GroundedAnswer, Passage, RetrievalResult, VerifiedClaim

# Maps the NLI 3-way label → the claim verdict (§5 step 3/5).
_VERDICT: dict[str, Literal["supported", "weak", "unsupported"]] = {
    "entail": "supported",
    "neutral": "weak",
    "contradict": "unsupported",
}

# Captures "claim text [id1, id2]" units in the generated answer.
_CITED = re.compile(r"([^.\[\]]*?)\s*\[([^\]]+)\]")


class _NLIVerifierLike(Protocol):
    def entail(self, premise: str, hypothesis: str) -> str: ...
    def score(self, premise: str, hypothesis: str) -> float: ...


def extract_claims(answer_text: str) -> list[Claim]:
    """Split generated prose into atomic claims by their inline `[ids]` (§5 step 3)."""
    claims: list[Claim] = []
    for m in _CITED.finditer(answer_text):
        text = m.group(1).strip(" .,\n")
        ids = [i.strip() for i in m.group(2).split(",") if i.strip()]
        if text and ids:
            claims.append(Claim(text=text, cited_passage_ids=ids))
    return claims


class GroundingPipeline:
    """[CONTRACT] Constrained generation → NLI verification → self-correction →
    honest surfacing. `strictness="drop"` removes unsupported claims from the
    rendered answer; `"keep"` keeps all but still marks verdicts (§5 step 4)."""

    def __init__(
        self,
        router: LLMRouter,
        nli: _NLIVerifierLike,
        *,
        strictness: Literal["drop", "keep"] = "drop",
    ) -> None:
        self._router = router
        self._nli = nli
        self._strictness = strictness

    def _prompt(self, query: str, passages: list[Passage]) -> list[LLMMessage]:
        numbered = "\n".join(f"[{p.id}] {p.text}" for p in passages)
        return [
            LLMMessage(
                role="system",
                content=(
                    "Answer using ONLY the numbered passages below — not your own prior "
                    "knowledge, EVEN IF you already know the answer. End EVERY factual "
                    "claim with its supporting source ids in brackets, e.g. [src1_p0]. "
                    "Every claim MUST carry at least one citation — an answer with no "
                    "[id] citations is invalid, even for well-known facts. If the passages "
                    "do not contain the answer, say exactly that."
                ),
            ),
            LLMMessage(role="user", content=f"Passages:\n{numbered}\n\nQuestion: {query}"),
        ]

    def _verify(self, claim: Claim, by_id: dict[str, Passage]) -> VerifiedClaim:
        cited = [by_id[i] for i in claim.cited_passage_ids if i in by_id]
        premise = "\n".join(p.text for p in cited)
        label = self._nli.entail(premise, claim.text)
        # strongest individual entailing passage, for the UI hover card.
        best_id, best_score = None, -1.0
        for p in cited:
            s = self._nli.score(p.text, claim.text)
            if s > best_score:
                best_id, best_score = p.id, s
        return VerifiedClaim(
            claim=claim,
            verdict=_VERDICT.get(label, "weak"),
            best_passage_id=best_id,
            entailment_score=max(best_score, 0.0),
        )

    async def answer(self, query: str, retrieval: RetrievalResult) -> GroundedAnswer:
        by_id = {p.id: p for p in retrieval.passages}

        # 1–2. constrained generation via the RAG_ANSWERER role.
        # `enable_thinking=False`: grounded answering is extraction, not reasoning. A
        # reasoning model (Qwen3.6 et al) left to think spends its whole budget in
        # `reasoning_content`, hits `finish_reason=length` mid-thought, and returns
        # EMPTY `content` — no claims, empty prose (the canary's "up but not grounding"
        # failure). With thinking off it answers directly and cites; max_tokens just
        # bounds the answer length.
        resp = await self._router.complete(
            CompletionRequest(
                profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
                messages=self._prompt(query, retrieval.passages),
                temperature=0.0,
                max_tokens=2048,
                enable_thinking=False,
            )
        )

        # 3. split into claims + verify each (NLI, not the LLM path).
        verified = [self._verify(c, by_id) for c in extract_claims(resp.text)]

        # 4. self-correction: drop unsupported (or keep, marked) — §5 step 4.
        kept = (
            [v for v in verified if v.verdict != "unsupported"]
            if self._strictness == "drop"
            else verified
        )
        answer_markdown = " ".join(
            f"{v.claim.text} [{', '.join(v.claim.cited_passage_ids)}]." for v in kept
        )

        # 5. surface honestly: weak/unsupported claims stay in `claims`, never hidden.
        cited_ids = {i for v in verified for i in v.claim.cited_passage_ids}
        return GroundedAnswer(
            answer_markdown=answer_markdown,
            claims=verified,
            passages=[p for p in retrieval.passages if p.id in cited_ids],
            all_hits=retrieval.all_hits,
            unsupported_count=sum(v.verdict == "unsupported" for v in verified),
        )
