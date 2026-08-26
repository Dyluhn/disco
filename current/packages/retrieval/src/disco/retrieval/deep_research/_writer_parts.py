"""Mechanical helpers for `deep_research.writer` (the whole-report writer).

Rendering-safety repairs (chart validation, citation normalization, table
repair), the deterministic pieces of the fixed review bar (word counting,
report parsing), the evidence-pool formatter, and the truncation-continuation
loop. Everything here is either pure text mechanics or a bounded provider
retry — no prose is ever deleted here (decision #5: no scissors); the only
removals are broken ```chart blocks degraded to tables, which is rendering
safety, not verification."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any, Literal

import jsonschema
from disco.core import LLMMessage
from disco.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    ModelRole,
)
from disco.core.think import strip_think_spans

from ..models import Passage
from ._synthesis_parts import _join_continuation_text

# ~4 chars/token: the evidence block stays under ~55k tokens of the writer's
# context regardless of pool size; per-passage share is adaptive.
_EVIDENCE_CHAR_BUDGET = 220_000
_MIN_PASSAGE_CHARS = 280

CHART_SCHEMA = {
    "type": "object",
    "properties": {
        "chart_type": {"enum": ["bar", "line", "pie", "scatter"]},
        "data": {
            "type": "array",
            "items": {
                "type": "object",
                "anyOf": [
                    {
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "number"},
                        },
                        "required": ["label", "value"],
                    },
                    {
                        "properties": {
                            "x": {"type": ["number", "string"]},
                            "y": {"type": "number"},
                            "group": {"type": "string"},
                        },
                        "required": ["x", "y"],
                    },
                ],
            },
        },
        "title": {"type": "string"},
        "x_label": {"type": "string"},
        "y_label": {"type": "string"},
    },
    "required": ["chart_type", "data"],
}


def _degrade_chart_to_table(block: str) -> str | None:
    """Rebuild a broken ```chart block as a markdown table when its data rows
    are recoverable; `None` when nothing usable remains."""
    try:
        payload = json.loads(block[9:-3].strip())
        data = payload.get("data", [])
        if payload.get("chart_type") == "scatter":
            cols = ["Group", payload.get("x_label", "X"), payload.get("y_label", "Y")]
            rows = [
                [str(d.get("group", "")), str(d.get("x", "")), str(d.get("y", ""))] for d in data
            ]
        else:
            cols = [payload.get("x_label", "Label"), payload.get("y_label", "Value")]
            rows = [
                [str(d.get("label", d.get("x", ""))), str(d.get("value", d.get("y", "")))]
                for d in data
            ]
        if not rows:
            return None
        table = f"| {' | '.join(cols)} |\n| {' | '.join(['---'] * len(cols))} |\n"
        for row in rows:
            table += f"| {' | '.join(row)} |\n"
        return f"\n{table}\n"
    except Exception:  # noqa: BLE001 — an unrecoverable chart block is dropped
        return None


def validate_charts(markdown: str) -> str:
    """Validate ```chart blocks against the schema; degrade invalid ones to
    markdown tables (or drop them) so the UI never receives a broken chart."""
    blocks = re.split(r"(```chart\n.*?```)", markdown, flags=re.DOTALL)
    out: list[str] = []
    for block in blocks:
        if not block.startswith("```chart\n"):
            out.append(block)
            continue
        try:
            payload = json.loads(block[9:-3].strip())
            jsonschema.validate(instance=payload, schema=CHART_SCHEMA)
            out.append(block)
        except Exception:  # noqa: BLE001 — degrade, never render broken JSON
            table = _degrade_chart_to_table(block)
            if table is not None:
                out.append(table)
    return "".join(out)


def normalize_citations(markdown: str, valid_ids: set[str]) -> str:
    """Promote bare single-bracket ``[id]`` citations to ``[[id]]`` when the
    id is a known passage id. Only known ids are rewritten, so markdown links
    and bracketed asides stay untouched."""
    if not valid_ids:
        return markdown

    def repl(match: re.Match[str]) -> str:
        return f"[[{match.group(1)}]]" if match.group(1) in valid_ids else match.group(0)

    # (?<!\[) / (?!\]) ensure an already-doubled [[id]] is never touched.
    return re.sub(r"(?<!\[)\[([\w-]+)\](?!\])", repl, markdown)


Confidence = Literal["high", "mixed", "low"]


def confidence_from_claims(claims: list[dict[str, Any]]) -> tuple[Confidence, int]:
    """Roll per-claim verdicts into a section confidence bucket + unsupported
    count: low if ≥30% unsupported, mixed if ≥30% weak, otherwise high."""
    if not claims:
        return "low", 0
    total = len(claims)
    unsupported = sum(1 for claim in claims if claim.get("verdict") == "unsupported")
    weak = sum(1 for claim in claims if claim.get("verdict") == "weak")
    if unsupported / total >= 0.30:
        return "low", unsupported
    if (unsupported + weak) / total >= 0.30:
        return "mixed", unsupported
    return "high", unsupported


_SUMMARY_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9*_`])")


def _paragraphize(sentences: list[str]) -> list[str]:
    """Group ordered sentences into at most four balanced paragraphs."""
    if not sentences:
        return []
    paragraph_count = min(4, len(sentences))
    base_size, larger_count = divmod(len(sentences), paragraph_count)
    paragraphs: list[str] = []
    offset = 0
    for index in range(paragraph_count):
        size = base_size + (1 if index < larger_count else 0)
        paragraphs.append(" ".join(sentences[offset : offset + size]))
        offset += size
    return paragraphs


def readable_summary(summary: str) -> str:
    """Honor the 2–4 paragraph contract when the writer returns one prose wall."""
    cleaned = re.sub(r"(?m)^\s*>\s?", "", summary).strip()
    blocks = [block.strip() for block in re.split(r"\n\s*\n", cleaned) if block.strip()]
    if len(blocks) != 1 or "\n" in blocks[0]:
        return "\n\n".join(blocks)
    sentences = [part.strip() for part in _SUMMARY_SENTENCE_BREAK.split(blocks[0]) if part.strip()]
    if len(sentences) < 2:
        return blocks[0]
    return "\n\n".join(_paragraphize(sentences))


_CITATION = re.compile(r"\[\[([\w-]+)\]\]")
_WORD = re.compile(r"[A-Za-z0-9][\w'-]*")


def cited_ids(markdown: str) -> list[str]:
    return _CITATION.findall(markdown)


def count_prose_words(text: str) -> int:
    return len(_WORD.findall(_CITATION.sub("", text)))


_HEADING = re.compile(r"(?m)^##\s+(.+?)\s*$")


def parse_report(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Split a whole-report draft into (summary, [(title, body), ...]).
    The summary is everything before the first `## ` heading."""
    text = text.strip()
    matches = list(_HEADING.finditer(text))
    if not matches:
        return text, []
    summary = text[: matches[0].start()].strip()
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        title = match.group(1).strip().strip("#").strip()
        sections.append((title, text[match.end() : end].strip()))
    return summary, sections


def format_evidence_pool(
    passages: list[Passage], *, char_budget: int = _EVIDENCE_CHAR_BUDGET
) -> str:
    """Render the full pool as `[id] title\\nURL\\ntext` blocks, adaptively
    truncating each passage (head + tail, keeping conclusions) so the total
    stays inside the writer's context budget."""
    if not passages:
        return "(no evidence)"
    overhead = sum(len(p.id) + len(p.source_title or "") + len(p.source_url) + 12 for p in passages)
    share = max(_MIN_PASSAGE_CHARS, (char_budget - overhead) // len(passages))
    parts: list[str] = []
    for passage in passages:
        body = passage.text.strip()
        if len(body) > share:
            head = body[: int(share * 0.7)].rstrip()
            tail = body[-int(share * 0.25) :].lstrip()
            body = f"{head}\n… [middle omitted] …\n{tail}"
        title = passage.source_title or passage.source_url or passage.id
        parts.append(f"[{passage.id}] {title}\n{passage.source_url}\n{body}")
    return "\n\n".join(parts)


async def continue_truncated_report(
    router: LLMRouter,
    base_messages: list[LLMMessage],
    markdown: str,
    response: CompletionResponse,
    *,
    max_tokens: int,
    conversation_id: str | None = None,
    inspect_stage: str = "report_continuation",
    on_completion: Callable[[CompletionRequest, CompletionResponse, str, int, int], None]
    | None = None,
) -> str:
    """Continue a `finish_reason == "length"` report from its exact cut point,
    bounded to 2 continuations (the v1 truncation guard, whole-report shape)."""
    continuations = 0
    while response.finish_reason == "length" and continuations < 2:
        continuations += 1
        try:
            request = CompletionRequest(
                profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
                messages=[
                    *base_messages,
                    LLMMessage(role="assistant", content=markdown.rstrip()),
                    LLMMessage(
                        role="user",
                        content=(
                            "Your previous reply was cut off. Continue EXACTLY "
                            "where you stopped — do not repeat any earlier text "
                            "and do not add a preamble. If you were mid-table, "
                            "finish the table."
                        ),
                    ),
                ],
                temperature=0.0,
                max_tokens=max_tokens,
                metadata=(
                    {
                        "conversation_id": conversation_id[:256],
                        "inspect_stage": inspect_stage[:128],
                    }
                    if conversation_id
                    else None
                ),
            )
            started = time.perf_counter()
            response = await router.complete(request)
            latency_ms = max(0, int((time.perf_counter() - started) * 1_000))
        except Exception:  # noqa: BLE001 — keep the partial report
            break
        extra = strip_think_spans(response.text, keep_edge_whitespace=True)
        if on_completion is not None:
            on_completion(request, response, extra, continuations, latency_ms)
        if not extra.strip():
            break
        markdown = _join_continuation_text(markdown, extra.lstrip())
    return markdown
