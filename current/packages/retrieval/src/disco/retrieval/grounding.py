"""Grounding & citation pipeline — retrieval-grounding-contract.md §5.

Faithfulness is enforced, not asked (principle 5): the RAG_ANSWERER generates
cited prose, then every atomic claim is NLI-verified against its cited passage,
unsupported claims are self-corrected, and weak/unsupported claims + failed
sources are surfaced honestly (never hidden, BoD §13.3).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from typing import Literal, NamedTuple, Protocol

from disco.core import LLMMessage
from disco.core.llm import CapabilityProfile, CompletionRequest, LLMRouter, ModelRole

from .models import Claim, GroundedAnswer, Passage, RetrievalResult, VerifiedClaim
from .source_excerpts import relevant_excerpt

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
_ABBREVIATION = re.compile(
    r"(?:\bet\s+al|\be\.g|\bi\.e|\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|vs|Fig|Eq|No))\.$",
    re.IGNORECASE,
)
# Short lowercase abbreviations that qualify a following number (for example
# ``c. 1200`` or ``ca. 1200``) are not sentence endings.  Keep this generic:
# the same shape occurs in dates, measurements, page references, and ranges.
_NUMERIC_ABBREVIATION = re.compile(r"\b[a-z]{1,3}\.$")
# Initialisms can be part of a hyphenated compound (``U.S.-China``) or
# qualify a following word (``the U.S. Department``).  In either case, the
# period after the final initial is not a prose boundary. A citation follows
# the sentence-ending form, so bracketed text remains a valid boundary.
_INITIALISM = re.compile(r"\b(?:[A-Za-z]\.){2,}$")
_PERSON_INITIAL = re.compile(r"\b[A-Z]\.$")
# ``v.`` between two party names (``Bartz v. Anthropic``) is a case-name
# separator, never a sentence end. Without this the verifier reported
# fragments such as ``In Bartz v`` as unsupported sentences and sent the writer
# back to cite text that does not exist (phase-6 run-02: 8 of 11 findings).
_CASE_V = re.compile(r"\bv\.$")
_CLUSTER = re.compile(r"(.*?)((?:\[\[[\w-]+\]\]\s*)+)", re.DOTALL)
_MD = re.compile(r"[*`#_>]+")
_MD_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_WORDISH = re.compile(r"[A-Za-z][\w'-]*")


class _NLILike(Protocol):
    """The structural NLI interface used by live and test verifiers."""

    def entail(self, premise: str, hypothesis: str) -> str: ...
    def score(self, premise: str, hypothesis: str) -> float: ...


class _ClaimSpan(NamedTuple):
    start: int
    end: int
    text: str
    cited_passage_ids: tuple[str, ...]


def _sentence_boundaries(text: str) -> list[re.Match[str]]:
    """Return prose boundaries, excluding abbreviations and open quotations."""
    boundaries: list[re.Match[str]] = []
    for boundary in _SENT.finditer(text):
        prefix = text[: boundary.start()]
        following = text[boundary.end() :].lstrip()
        if _ABBREVIATION.search(prefix):
            continue
        if _NUMERIC_ABBREVIATION.search(prefix) and following[:1].isdigit():
            continue
        if _INITIALISM.search(prefix) and (following[:1].isalpha() or following.startswith("-")):
            continue
        if _PERSON_INITIAL.search(prefix) and following[:1].isupper():
            continue
        if _CASE_V.search(prefix) and following[:1].isupper():
            continue
        if prefix.count('"') % 2 or prefix.count("“") > prefix.count("”"):
            continue
        boundaries.append(boundary)
    return boundaries


def _split_sentences(text: str) -> list[str]:
    parts: list[str] = []
    cursor = 0
    for boundary in _sentence_boundaries(text):
        parts.append(text[cursor : boundary.start() + 1])
        cursor = boundary.end()
    parts.append(text[cursor:])
    return parts


def _plain(text: str) -> str:
    """Strip markdown emphasis so the NLI hypothesis is clean prose."""
    return _MD.sub("", text).strip()


def _clean_premise(text: str) -> str:
    """Remove markdown links and images before sentence-level comparison."""
    text = _MD_IMG.sub(" ", text)
    text = _MD_LINK.sub(r"\1", text)
    return _MD.sub("", text)


def _best_entail(passage_text: str, claim: str, nli: _NLILike) -> tuple[str, float]:
    """Compatibility view of a bounded, contextual passage check."""
    label, score, _available = _passage_check(passage_text, claim, nli)
    return label, score


def _passage_check(passage_text: str, claim: str, nli: _NLILike) -> tuple[str, float, bool]:
    # Keep surrounding qualifications together rather than selecting the most
    # agreeable of twelve isolated opening sentences. Relevant late text stays
    # reachable while each model input remains bounded.
    premise = relevant_excerpt(_clean_premise(passage_text), claim, max_chars=1600).text
    label = nli.entail(premise, claim)
    score = nli.score(premise, claim)
    availability = getattr(nli, "verification_available", None)
    available = bool(availability(premise, claim)) if callable(availability) else True
    if label not in _VERDICT or not math.isfinite(score):
        return "neutral", 0.0, False
    # A score never overrules the actual classifier label.
    return label, min(1.0, max(0.0, score)), available


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
    for boundary in _sentence_boundaries(lead.rstrip()):
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
    for boundary in _sentence_boundaries(line[content_start:content_end]):
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


def _raw_claim_spans(text: str) -> list[_ClaimSpan]:
    tables = _table_claim_spans(text)
    cited = [
        span
        for span in _cited_claim_spans(text)
        if not any(row.start <= span.start < row.end for row in tables)
    ] + tables
    return sorted([*cited, *_uncited_claim_spans(text, cited)], key=lambda span: span.start)


def _claim_spans(text: str) -> list[_ClaimSpan]:
    return _join_sentence_spans(text, _raw_claim_spans(text))


def _join_sentence_spans(text: str, spans: list[_ClaimSpan]) -> list[_ClaimSpan]:
    """Citation placement does not turn a sentence into independent assertions.

    Keep the subject, qualifications and every citation of an inline-cited
    sentence together, including its uncited tail. Actual sentence and layout
    boundaries stay separate, so a neighboring unsupported sentence cannot
    silently acquire a citation. Source offsets still address the original prose.
    """
    joined: list[_ClaimSpan] = []
    for span in spans:
        previous = joined[-1] if joined else None
        if previous is not None:
            prior = _CITE.sub("", text[previous.start : previous.end]).rstrip()
            gap = text[previous.end : span.start]
            if (
                not prior.endswith((".", "!", "?"))
                and not gap.strip()
                and "\n" not in text[previous.end : span.start + 1]
            ):
                combined = _CITE.sub("", text[previous.start : span.end])
                combined = re.sub(r"[ \t]+([,;:.!?])", r"\1", combined)
                combined = re.sub(r"[ \t]+", " ", combined).strip(" .\n")
                joined[-1] = _ClaimSpan(
                    previous.start,
                    span.end,
                    combined,
                    tuple(dict.fromkeys((*previous.cited_passage_ids, *span.cited_passage_ids))),
                )
                continue
        joined.append(span)
    return joined


def _table_claim_spans(text: str) -> list[_ClaimSpan]:
    """Check a table row with its headers, never a contextless cited cell.

    Preserve row offsets for prose retention and all row citations for checking.
    Irregular tables fall back to ordinary claim extraction.
    """
    rows: list[_ClaimSpan] = []
    lines = text.splitlines(keepends=True)
    headers: list[str] = []
    offset = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            headers = []
        elif not _is_gfm_table_divider(stripped):
            cells = re.split(r"(?<!\\)\|", stripped)[1:-1]
            if index + 1 < len(lines) and _is_gfm_table_divider(lines[index + 1]):
                headers = [_plain(cell) for cell in cells]
            elif headers and len(cells) == len(headers):
                hypothesis = "; ".join(
                    f"{header}: {_CITE.sub('', cell).strip()}"
                    for header, cell in zip(headers, cells, strict=True)
                )
                rows.append(
                    _ClaimSpan(
                        offset,
                        offset + len(line.rstrip("\r\n")),
                        hypothesis,
                        tuple(dict.fromkeys(_CITE.findall(line))),
                    )
                )
        offset += len(line)
    return rows


_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")


def _paragraph_citations(text: str, spans: Sequence[_ClaimSpan]) -> dict[int, tuple[str, ...]]:
    """The passage ids cited anywhere in each blank-line-separated paragraph.

    Keyed by the paragraph's start offset; ids in first-cited order, each once.
    """
    starts = [0] + [match.end() for match in _PARAGRAPH_BREAK.finditer(text)]
    by_paragraph: dict[int, list[str]] = {start: [] for start in starts}
    for span in spans:
        paragraph = max(start for start in starts if start <= span.start)
        for passage_id in span.cited_passage_ids:
            if passage_id not in by_paragraph[paragraph]:
                by_paragraph[paragraph].append(passage_id)
    return {start: tuple(ids) for start, ids in by_paragraph.items()}


def _measurement_status(measurements: list[tuple[str, str, float, bool]]) -> tuple[str, str, str]:
    """Combine measurements without turning absent or conflicting support into truth."""
    if not all(available for _id, _label, _score, available in measurements):
        return "neutral", "unavailable", "verifier_unavailable"
    labels = {label for _id, label, _score, _available in measurements}
    if {"entail", "contradict"} <= labels:
        return "neutral", "unresolved", "conflicting_evidence"
    if "entail" in labels:
        return "entail", "supported", "entailment"
    if "contradict" in labels:
        return "contradict", "contradicted", "contradiction"
    return "neutral", "unresolved", "not_established"


def _verify_claim_span(
    span: _ClaimSpan,
    checked: Sequence[str],
    by_id: dict[str, Passage],
    nli: _NLILike,
    borrowed: Sequence[str],
) -> dict:
    """Verify one span; policy and progress remain owned by ``_verify_claims``."""
    hypothesis = _plain(span.text)
    best_id, best_score, best_verdict = None, 0.0, "neutral"
    unknown_id = not checked or any(passage_id not in by_id for passage_id in checked)
    status, reason = "unresolved", "missing_evidence"
    if not unknown_id:
        measurements: list[tuple[str, str, float, bool]] = []
        for passage_id in checked:
            verdict, score, available = _passage_check(by_id[passage_id].text, hypothesis, nli)
            measurements.append((passage_id, verdict, score, available))
        best_verdict, status, reason = _measurement_status(measurements)
        candidates = [row for row in measurements if row[1] == best_verdict and row[3]]
        if candidates:
            best_id, _label, best_score, _available = max(candidates, key=lambda row: row[2])
    record = {
        "claim": {"text": span.text, "cited_passage_ids": list(span.cited_passage_ids)},
        "verdict": "unsupported" if unknown_id else _VERDICT.get(best_verdict, "weak"),
        "best_passage_id": best_id,
        "entailment_score": max(best_score, 0.0),
        "verification_status": status,
        "verification_reason": reason,
    }
    if borrowed and not span.cited_passage_ids:
        record["borrowed_passage_ids"] = list(borrowed)
    return record


def _verify_claims(
    text: str,
    by_id: dict[str, Passage],
    nli: _NLILike,
    progress: Callable[[int, int], None] | None = None,
    *,
    borrow_paragraph_citations: bool = False,
) -> list[dict]:
    """Verify every substantive statement, including uncited and unknown-id claims.

    ``progress`` is called with ``(verified_so_far, total)`` — once before the
    first statement and once after each one. The loop scores every statement
    against every passage it cites, which on a long answer over a large saved
    corpus runs for minutes; a caller that reports nothing for that long is
    indistinguishable from a hang, and the browser's stale-frame watchdog
    treats it as one. Callers that have nowhere to report to pass nothing.

    With ``borrow_paragraph_citations`` a statement that cites nothing is held
    to the same bar as one that does, against the passages cited elsewhere in
    its own paragraph: a paragraph's topic sentence or the judgment it draws
    from the cited sentences beside it is entailed by that evidence or
    neutral to it, and only a statement that evidence contradicts — or one in
    a paragraph that cites nothing at all — comes back unsupported. The ids
    it was checked against are reported as ``borrowed_passage_ids``. Without
    the flag an uncited statement is unsupported outright, which is what a
    short answer wants and what a report's connective prose cannot survive.
    """
    out: list[dict] = []
    spans = _claim_spans(text)
    borrowed_by_paragraph = _paragraph_citations(text, spans) if borrow_paragraph_citations else {}
    if progress is not None:
        progress(0, len(spans))
    for span in spans:
        ids = list(span.cited_passage_ids)
        borrowed: list[str] = []
        if not ids and borrow_paragraph_citations:
            paragraph = max(start for start in borrowed_by_paragraph if start <= span.start)
            borrowed = [
                passage_id for passage_id in borrowed_by_paragraph[paragraph] if passage_id in by_id
            ]
        checked = ids or borrowed
        record = _verify_claim_span(span, checked, by_id, nli, borrowed)
        if borrow_paragraph_citations and not ids and not borrowed:
            record["borrowed_passage_ids"] = []
        out.append(record)
        if progress is not None:
            progress(len(out), len(spans))
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
    raw_spans = _raw_claim_spans(text)
    for span in _join_sentence_spans(text, raw_spans):
        pieces.append(text[cursor : span.start])
        key = _claim_key(span.text, span.cited_passage_ids)
        verdict = verdicts.get(key)
        if verdict is None:
            # Older saved answers have one ledger row per cited clause. Accept
            # their whole sentence only if every original part was accepted;
            # never discard a qualification while keeping its leading claim.
            parts = [part for part in raw_spans if span.start <= part.start < span.end]
            old_verdicts = [
                verdicts.get(_claim_key(part.text, part.cited_passage_ids), "unsupported")
                for part in parts
            ]
            verdict = max(old_verdicts, key=lambda value: verdict_rank.get(value, 2))
        if verdict in allowed_verdicts:
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


def _retain_grounded_claims(text: str, claims: Sequence[dict]) -> str:
    """Keep entailed AND neutral statements; remove only the unsupported ones.

    This is the REPORT's policy, in one place. The writer treats a `weak`
    (NLI-neutral against its cited passage) statement as shippable and turns
    only `unsupported` into rework feedback — because "the cited passage does
    not entail this sentence on its own" is not "this sentence is false", and a
    judgement, a comparison, or a synthesis across two passages is neutral by
    construction. `_retain_supported_claims` above is the stricter policy the
    `GroundedAnswer` path uses for a single-shot answer over freshly retrieved
    passages; a follow-up over a whole saved report is the report's case, not
    that one.
    """
    return _retain_claim_verdicts(text, claims, frozenset({"supported", "weak"}))


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
