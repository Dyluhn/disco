"""Grounding & citation pipeline — retrieval-grounding-contract.md §5.

Faithfulness is enforced, not asked (principle 5): the RAG_ANSWERER generates
cited prose, then every atomic claim is NLI-verified against its cited passage,
unsupported claims are self-corrected, and weak/unsupported claims + failed
sources are surfaced honestly (never hidden, BoD §13.3).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal, NamedTuple, Protocol

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


_CITE = re.compile(r"\[\[([\w-]+)\]\]")
_SENT = re.compile(r"(?<=[.!?])\s+")
_CLUSTER = re.compile(r"(.*?)((?:\[\[[\w-]+\]\]\s*)+)", re.DOTALL)
_MD = re.compile(r"[*`#_>]+")
_MD_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_WORDISH = re.compile(r"[A-Za-z][\w'-]*")
_ENTAIL_MIN = 0.5


class _NLILike(Protocol):
    """The structural NLI interface used by live and test verifiers."""

    def entail(self, premise: str, hypothesis: str) -> str: ...
    def score(self, premise: str, hypothesis: str) -> float: ...


class _ClaimSpan(NamedTuple):
    start: int
    end: int
    text: str
    cited_passage_ids: tuple[str, ...]


def _plain(text: str) -> str:
    """Strip markdown emphasis so the NLI hypothesis is clean prose."""
    return _MD.sub("", text).strip()


def _clean_premise(text: str) -> str:
    """Remove markdown links and images before sentence-level comparison."""
    text = _MD_IMG.sub(" ", text)
    text = _MD_LINK.sub(r"\1", text)
    return _MD.sub("", text)


def _best_entail(passage_text: str, claim: str, nli: _NLILike) -> tuple[str, float]:
    """Return the verdict of the passage sentence most relevant to a claim."""
    cleaned = _clean_premise(passage_text)
    sentences = [
        sentence.strip() for sentence in _SENT.split(cleaned) if len(sentence.strip()) > 15
    ]
    sentences = sentences[:12] or [cleaned]
    best_score, best_label = -1.0, "neutral"
    for sentence in sentences:
        score = nli.score(sentence, claim)
        if score > best_score:
            best_score, best_label = score, nli.entail(sentence, claim)
    if best_score >= _ENTAIL_MIN:
        return "entail", best_score
    if best_label == "contradict":
        return "contradict", max(best_score, 0.0)
    return "neutral", max(best_score, 0.0)


def _is_gfm_table_divider(line: str) -> bool:
    """Return whether ``line`` is a GFM table divider row."""
    line = line.strip()
    if not line.startswith("|") or not line.endswith("|"):
        return False
    parts = [part.strip() for part in line.split("|")][1:-1]
    return all(re.match(r"^[:\- ]+$", part) and "-" in part for part in parts)


def _claim_start_in_lead(lead: str) -> int:
    """Find the final sentence or line immediately preceding a citation."""
    start = 0
    for boundary in _SENT.finditer(lead.rstrip()):
        start = boundary.end()
    start = max(start, lead.rfind("\n", start) + 1)
    while start < len(lead) and lead[start].isspace():
        start += 1
    return start


def _cited_claim_spans(text: str) -> list[_ClaimSpan]:
    spans: list[_ClaimSpan] = []
    for match in _CLUSTER.finditer(text):
        lead = match.group(1)
        local_start = _claim_start_in_lead(lead)
        claim_text = _CITE.sub("", lead[local_start:]).strip(" .\n")
        if not claim_text:
            continue
        end = match.end(2)
        if end < len(text) and text[end] in ".!?":
            end += 1
        spans.append(
            _ClaimSpan(
                start=match.start(1) + local_start,
                end=end,
                text=claim_text,
                cited_passage_ids=tuple(_CITE.findall(match.group(2))),
            )
        )
    return spans


def _substantive_uncited_span(text: str, start: int, end: int) -> _ClaimSpan | None:
    raw = text[start:end].strip()
    plain = _plain(raw).strip("-|: .\t")
    if len(plain) < 8 or len(_WORDISH.findall(plain)) < 2:
        return None
    leading = len(text[start:end]) - len(text[start:end].lstrip())
    trailing = len(text[start:end]) - len(text[start:end].rstrip())
    return _ClaimSpan(
        start=start + leading,
        end=end - trailing,
        text=raw.strip(" .\n"),
        cited_passage_ids=(),
    )


def _mask_cited_spans(text: str, cited: Sequence[_ClaimSpan]) -> str:
    masked = list(text)
    for span in cited:
        for index in range(span.start, span.end):
            if masked[index] != "\n":
                masked[index] = " "
    return "".join(masked)


def _line_is_layout(lines: list[str], index: int, stripped: str) -> bool:
    if not stripped or stripped.startswith("#") or _is_gfm_table_divider(stripped):
        return True
    return (
        stripped.startswith("|")
        and index + 1 < len(lines)
        and _is_gfm_table_divider(lines[index + 1].strip())
    )


def _uncited_line_spans(text: str, line: str, offset: int) -> list[_ClaimSpan]:
    content_start = len(line) - len(line.lstrip())
    prefix = re.match(r"(?:[-+*>]|\d+[.)])\s+", line[content_start:])
    if prefix is not None:
        content_start += prefix.end()
    content_end = len(line.rstrip("\r\n"))
    cursor = content_start
    spans: list[_ClaimSpan] = []
    for boundary in _SENT.finditer(line[content_start:content_end]):
        end = content_start + boundary.start() + 1
        span = _substantive_uncited_span(text, offset + cursor, offset + end)
        if span is not None:
            spans.append(span)
        cursor = content_start + boundary.end()
    span = _substantive_uncited_span(text, offset + cursor, offset + content_end)
    if span is not None:
        spans.append(span)
    return spans


def _uncited_claim_spans(text: str, cited: list[_ClaimSpan]) -> list[_ClaimSpan]:
    """Find substantive generated statements not covered by citation spans."""
    lines = _mask_cited_spans(text, cited).splitlines(keepends=True)
    spans: list[_ClaimSpan] = []
    offset = 0
    in_fence = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and not _line_is_layout(lines, index, stripped):
            spans.extend(_uncited_line_spans(text, line, offset))
        offset += len(line)
    return spans


def _claim_spans(text: str) -> list[_ClaimSpan]:
    cited = _cited_claim_spans(text)
    return sorted([*cited, *_uncited_claim_spans(text, cited)], key=lambda span: span.start)


def _verify_claims(text: str, by_id: dict[str, Passage], nli: _NLILike) -> list[dict]:
    """Verify every substantive statement, including uncited and unknown-id claims."""
    out: list[dict] = []
    for span in _claim_spans(text):
        ids = list(span.cited_passage_ids)
        hypothesis = _plain(span.text)
        best_id, best_score, best_verdict = None, -1.0, "neutral"
        unknown_id = not ids or any(passage_id not in by_id for passage_id in ids)
        if not unknown_id:
            for passage_id in ids:
                verdict, score = _best_entail(by_id[passage_id].text, hypothesis, nli)
                if score > best_score:
                    best_id, best_score, best_verdict = passage_id, score, verdict
        out.append(
            {
                "claim": {"text": span.text, "cited_passage_ids": ids},
                "verdict": "unsupported" if unknown_id else _VERDICT.get(best_verdict, "weak"),
                "best_passage_id": best_id,
                "entailment_score": max(best_score, 0.0),
            }
        )
    return out


def _claim_key(text: str, cited_ids: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    return " ".join(_plain(text).lower().split()).strip(" ."), tuple(cited_ids)


def _retain_claim_verdicts(
    text: str, claims: Sequence[dict], allowed_verdicts: frozenset[str]
) -> str:
    """Remove complete statements whose grounding verdict is not allowed."""
    verdict_rank = {"supported": 0, "weak": 1, "unsupported": 2}
    verdicts: dict[tuple[str, tuple[str, ...]], str] = {}
    for claim in claims:
        body = claim.get("claim", {})
        key = _claim_key(str(body.get("text", "")), body.get("cited_passage_ids", []))
        verdict = str(claim.get("verdict", "unsupported"))
        prior = verdicts.get(key)
        if prior is None or verdict_rank.get(verdict, 2) > verdict_rank.get(prior, 2):
            verdicts[key] = verdict

    pieces: list[str] = []
    cursor = 0
    for span in _claim_spans(text):
        pieces.append(text[cursor : span.start])
        key = _claim_key(span.text, span.cited_passage_ids)
        if verdicts.get(key) in allowed_verdicts:
            pieces.append(text[span.start : span.end])
        else:
            pieces.append("\n" * text[span.start : span.end].count("\n"))
        cursor = span.end
    pieces.append(text[cursor:])
    cleaned = "".join(pieces)
    cleaned = re.sub(r"(?m)^\s*(?:[-+*>]|\d+[.)])\s*$", "", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _retain_supported_claims(text: str, claims: Sequence[dict]) -> str:
    return _retain_claim_verdicts(text, claims, frozenset({"supported"}))


def _remove_unknown_citation_claims(text: str, known_ids: set[str]) -> str:
    """Remove statements carrying invented or expired source ids before rendering."""
    pieces: list[str] = []
    cursor = 0
    for span in _cited_claim_spans(text):
        pieces.append(text[cursor : span.start])
        if all(passage_id in known_ids for passage_id in span.cited_passage_ids):
            pieces.append(text[span.start : span.end])
        else:
            pieces.append("\n" * text[span.start : span.end].count("\n"))
        cursor = span.end
    pieces.append(text[cursor:])
    return re.sub(r"\n{3,}", "\n\n", "".join(pieces)).strip()


def _drop_weak(answer: dict) -> dict:
    """Drop weak/unsupported statements themselves, never just their markers."""
    if all(claim["verdict"] == "supported" for claim in answer["claims"]):
        return answer
    weak_ids = {
        passage_id
        for claim in answer["claims"]
        if claim["verdict"] != "supported"
        for passage_id in claim["claim"]["cited_passage_ids"]
    }
    blocks = []
    for block in answer["blocks"]:
        if block.get("kind") == "prose":
            filtered = _retain_supported_claims(block["text"], answer["claims"])
            if filtered:
                blocks.append(
                    {
                        **block,
                        "text": filtered,
                        "cited_passage_ids": sorted(set(_CITE.findall(filtered))),
                    }
                )
        elif block.get("kind") in {"table", "chart"}:
            cited = set(block.get("cited_passage_ids", []))
            if cited and cited.isdisjoint(weak_ids):
                blocks.append(block)
        else:
            blocks.append(block)
    return {
        **answer,
        "blocks": blocks,
        "claims": [claim for claim in answer["claims"] if claim["verdict"] == "supported"],
        "unsupported_count": 0,
    }
