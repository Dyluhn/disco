"""Mechanical helpers for `deep_research.writer` (the whole-report writer).

Rendering-safety repairs (chart validation, citation normalization, table
repair), report parsing and rendering, the evidence-pool formatter, the
thinking headroom the writer provisions for on generation AND review calls,
the reviewer's verdict transport (one JSON-mode call, and the fence-tolerant
decode of what comes back), and the last-paragraph replay a cut-off
continuation reads. Everything here is pure text mechanics or one provider
call — no prose is ever deleted here; the only removals are broken ```chart
blocks degraded to tables, which is rendering safety, not verification."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import jsonschema
from disco.core import LLMMessage
from disco.core.env import disco_env
from disco.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    CompletionResponse,
    LLMError,
    LLMRouter,
    ModelRole,
    Requirement,
)
from disco.core.think import strip_think_spans

from ._synthesis_parts import repair_tables
from ._writer_evidence import format_evidence_pool as format_evidence_pool
from ._writer_prompts import CONTINUATION_LAST_PARAGRAPH

# Provider output allowances include reasoning as well as visible report text.
# A preserved full review used 20,822 output tokens before completing normally;
# the previous 8,192-token headroom could exhaust before a verdict arrived.
_DEFAULT_THINK_HEADROOM_TOKENS = 24_000


def _provider_failure(exc: LLMError) -> str:
    """Bounded operator diagnostic; adapters already sanitize account/config detail."""
    message = str(exc)[:500]
    return f"{message} — {exc.provider_detail[:500]}" if exc.provider_detail else message


def think_headroom_tokens() -> int:
    """Tokens reserved for a writer call's think phase, above the body budget.

    Env: ``DISCO_LLM_THINK_HEADROOM_TOKENS`` (the legacy ``PMX_`` name is
    honored by ``disco_env``). An unparseable value falls back to
    the default; negative values clamp to zero. The writer
    resolves its GENERATION budget ONCE per report run and reuses it for the
    draft, the reworks, and the truncation continuations; the review calls
    (`review_max_tokens`) resolve theirs per call, which is the same value.
    """
    raw = disco_env("LLM_THINK_HEADROOM_TOKENS")
    if raw is None:
        return _DEFAULT_THINK_HEADROOM_TOKENS
    try:
        value = int(raw.strip())
    except ValueError:
        return _DEFAULT_THINK_HEADROOM_TOKENS
    return max(0, value)


def review_max_tokens(verdict_tokens: int) -> int:
    """Finite verdict capacity plus the shared reasoning provision.

    Reasoning support and wire representation belong to the provider adapter.
    Output capacity is provisioned regardless of whether that hint is honored.
    """
    return verdict_tokens + think_headroom_tokens()


def _load_json_object(candidate: str) -> tuple[dict[str, Any] | None, str | None]:
    """Strict parse of one JSON object, with the caller's error wording."""
    try:
        value: Any = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON: {exc}"
    if not isinstance(value, dict):
        return None, (f"the top-level JSON value must be an object, got {type(value).__name__}")
    return value, None


def decode_review_json(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse a reviewer's JSON verdict, tolerating one wrapping code fence.

    A clean verdict parses on the FIRST attempt and is never touched by the
    repairs below. That ordering is the point: a reviewer's ``fix`` string may
    itself quote a fenced block, and a pre-emptive fence strip would cut a
    perfectly valid verdict in half. Only when the direct parse fails does this
    peel a single wrapping ```json … ``` fence, then fall back to the span
    between the first ``{`` and the last ``}`` so a line of prose either side of
    the object does not cost the run its research.

    Nothing else is repaired. Still-malformed content returns the FIRST parse's
    error, which is the wording the run has always failed with.
    """
    candidate = text.strip()
    value, error = _load_json_object(candidate)
    if error is None:
        return value, None
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, re.DOTALL)
    if fenced is not None:
        unfenced, fence_error = _load_json_object(fenced.group(1).strip())
        if fence_error is None:
            return unfenced, None
    start, end = candidate.find("{"), candidate.rfind("}")
    if 0 <= start < end:
        spanned, span_error = _load_json_object(candidate[start : end + 1])
        if span_error is None:
            return spanned, None
    return None, error


@dataclass(frozen=True)
class ReviewCall:
    """One completed reviewer call: what was sent, what came back, how long."""

    request: CompletionRequest
    response: CompletionResponse
    text: str  # the content channel with any think span stripped
    latency_ms: int


async def review_call(
    router: LLMRouter,
    messages: Sequence[LLMMessage],
    *,
    max_tokens: int,
    metadata: dict[str, str] | None,
    enable_thinking: bool | None = None,
) -> ReviewCall:
    """Issue one deterministic JSON-mode call on this transport and time it.

    ``enable_thinking=None`` — the default, and what every reviewer call uses —
    leaves reasoning selection to the configured provider/model; adapters may
    ignore or apply that configuration according to their capabilities. A
    caller whose task is extraction rather than judgement passes ``False``.
    """
    request = CompletionRequest(
        profile=CapabilityProfile(
            role=ModelRole.RAG_ANSWERER,
            requirements=frozenset({Requirement.JSON_MODE}),
        ),
        messages=list(messages),
        temperature=0.0,
        max_tokens=max_tokens,
        response_format="json",
        enable_thinking=enable_thinking,
        metadata=metadata,
    )
    started = time.perf_counter()
    response = await router.complete(request)
    latency_ms = max(0, int((time.perf_counter() - started) * 1_000))
    return ReviewCall(request, response, strip_think_spans(response.text), latency_ms)


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


def has_report_prose(markdown: str) -> bool:
    """Whether a writer response carries any usable report prose.

    "Empty after think-stripping" and "no parseable report" are the same
    condition at this boundary: the call succeeded at the provider and produced
    no report. Whitespace, stray citation markers, and punctuation alone all
    count as nothing, because none of them parse into a summary or a section.
    """
    return count_prose_words(markdown) > 0


_HEADING = re.compile(r"(?m)^##\s+(.+?)\s*$")

# A first line that is an h1 heading and nothing else is the report's TITLE,
# not the first sentence of its executive summary. `## ` never matches: the
# second `#` is not whitespace.
_REPORT_TITLE_LINE = re.compile(r"^\s{0,3}#\s+(.+?)\s*#*\s*$")

# Section headings that ARE the executive summary written under another house
# style. Deliberately a SMALL frozen set of titles that can only mean "the
# report's opening answer": a model whose default is `## Executive Summary`
# instead of heading-free prose has written a summary, and reading it as "no
# summary" throws a whole run's research away over a heading. Lifting the body
# out from under the heading keeps every word — it is the markup that goes.
SUMMARY_SECTION_TITLES = frozenset(
    {"executive summary", "summary", "overview", "executive overview"}
)


def split_report_title(text: str) -> tuple[str, str]:
    """Split a leading `# <title>` line off a draft: `(title, remainder)`.

    The title is structural markup the export renders from the query, so it is
    kept out of the summary's word count and grounding checks. An empty title
    means the draft opens with prose, which is the house style the writing
    rules ask for.
    """
    stripped = text.strip()
    head, newline, rest = stripped.partition("\n")
    match = _REPORT_TITLE_LINE.match(head)
    if match is None:
        return "", stripped
    return match.group(1).strip(), rest.strip() if newline else ""


@dataclass(frozen=True)
class ParsedReport:
    """A draft read as the shape it is: title, executive summary, sections."""

    title: str
    summary: str
    sections: tuple[tuple[str, str], ...]


def parse_report_parts(text: str, *, lift_summary_heading: bool = True) -> ParsedReport:
    """Read a whole-report draft into its title, summary, and `## ` sections.

    Two deterministic shape normalizations run here, BEFORE any deterministic
    check sees the draft, and neither one changes a word of prose:

    * a leading `# ` line is the report title, not summary prose;
    * a draft that opens straight into `## Executive Summary` (or another title
      in `SUMMARY_SECTION_TITLES`) HAS an executive summary — that section's
      body is lifted into the summary slot and the heading, which is markup,
      goes with the lift.

    A draft with genuinely no summary-equivalent opening still parses with an
    empty summary, so the structural check's "no executive summary" keeps
    meaning what it says.

    The lift is for a DRAFT. A rework reply is read by the part names the
    findings used, where a section the writer was asked for by the name
    "Overview" is that section and not the summary — its caller passes
    `lift_summary_heading=False` and places the parts itself.
    """
    title, body = split_report_title(text)
    matches = list(_HEADING.finditer(body))
    if not matches:
        return ParsedReport(title=title, summary=body, sections=())
    summary = body[: matches[0].start()].strip()
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        section_title = match.group(1).strip().strip("#").strip()
        sections.append((section_title, body[match.end() : end].strip()))
    if lift_summary_heading and not summary and sections[0][0].casefold() in SUMMARY_SECTION_TITLES:
        summary = sections[0][1]
        sections = sections[1:]
    return ParsedReport(title=title, summary=summary, sections=tuple(sections))


def parse_report(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Split a whole-report draft into (summary, [(title, body), ...]).

    The normalized shape from `parse_report_parts`, in the pair every caller
    already reads. Nothing downstream should ever see the un-normalized one.
    """
    parsed = parse_report_parts(text)
    return parsed.summary, list(parsed.sections)


def render_report_body(summary: str, sections: Sequence[tuple[str, str]]) -> str:
    """The report as one markdown string, minus the title line.

    This is the span the acceptance harness grades — the executive summary and
    the section bodies that become the `ReportEvent` — so it is the span the
    writer's deterministic checks measure too.
    """
    blocks = [summary.strip(), *(f"## {title}\n{body}" for title, body in sections)]
    return "\n\n".join(block for block in blocks if block.strip())


@dataclass(frozen=True)
class FinalReport:
    """The exact report a run ships, after every rendering-safety repair.

    Built by `finalize_report` and used by BOTH the writer's last deterministic
    pass and the final assembly, so the text that is graded and the text that
    reaches `ReportEvent` cannot be two different things.
    """

    title: str
    summary: str
    sections: tuple[tuple[str, str], ...]

    @property
    def body_markdown(self) -> str:
        """Summary + sections: what the harness receives and grades."""
        return render_report_body(self.summary, self.sections)

    @property
    def markdown(self) -> str:
        """The whole rendered report, title line included."""
        body = self.body_markdown
        return f"# {self.title}\n\n{body}" if self.title else body


def finalize_report(draft: str, pool_ids: set[str]) -> FinalReport:
    """Normalize a candidate into the report that would actually ship.

    Shape normalization (`parse_report_parts`) then the rendering-safety
    repairs, in the order final assembly has always applied them. Only markup
    changes: a broken ```chart block degrades to a table, bare `[id]` markers
    become `[[id]]`, malformed tables are repaired, and a one-wall summary is
    re-paragraphed. No prose is added, reordered, or removed.
    """
    parsed = parse_report_parts(draft)
    return FinalReport(
        title=parsed.title,
        summary=normalize_citations(readable_summary(parsed.summary), pool_ids),
        sections=tuple(
            (title, repair_tables(normalize_citations(validate_charts(body), pool_ids)))
            for title, body in parsed.sections
        ),
    )


# ---------------------------------------------------------------------------
# Continuation replay.
# ---------------------------------------------------------------------------

# How much of the report's last paragraph the continuation prompt replays. Long
# enough that the model can see the sentences it must not restate, short enough
# that a run-on paragraph cannot dominate the instruction; an over-long
# paragraph keeps its TAIL, which is the text the continuation joins onto.
_LAST_PARAGRAPH_WORDS = 160
_MARKDOWN_HEADING_LINE = re.compile(r"^\s{0,3}#{1,6}\s")


def _last_paragraph_block(markdown: str) -> str:
    """Replay the report's final paragraph so the continuation can see the
    sentences it must not restate.

    Verbatim, minus the block's own heading — the writer needs the SENTENCES.
    Empty when nothing has been written yet; a run-on paragraph keeps its tail,
    which is the text the continuation joins onto.
    """
    blocks = [block.strip() for block in re.split(r"\n\s*\n", markdown) if block.strip()]
    if not blocks:
        return ""
    paragraph = "\n".join(
        line for line in blocks[-1].splitlines() if not _MARKDOWN_HEADING_LINE.match(line)
    ).strip()
    if not paragraph:
        return ""
    words = paragraph.split()
    if len(words) > _LAST_PARAGRAPH_WORDS:
        paragraph = "…" + " ".join(words[-_LAST_PARAGRAPH_WORDS:])
    return CONTINUATION_LAST_PARAGRAPH.format(paragraph=paragraph)


def _coverage_instruction(coverage: dict[str, Any]) -> str:
    covered = _coverage_items(coverage)
    open_gaps = _coverage_strings(coverage.get("open"))
    contradictions = _coverage_strings(coverage.get("contradictions_checked"))
    if not covered and not open_gaps and not contradictions:
        return ""
    lines = ["COVERAGE MAP (research state; use as guidance, not as a required outline):"]
    if covered:
        lines.append("Covered angles:\n" + "\n".join(covered))
    if open_gaps:
        lines.append("Open gaps:\n" + "\n".join(f"- {gap}" for gap in open_gaps))
    if contradictions:
        lines.append(
            "Contradictions checked:\n" + "\n".join(f"- {claim}" for claim in contradictions)
        )
    lines.append(
        "Do not answer this as a questionnaire, copy these angle names as headings mechanically, "
        "or discuss the coverage map; the evidence and the question decide what the report covers. "
        "The findings are the researcher's provisional analysis, not additional sources. "
        "Check them against the cited text and preserve their material conditions, "
        "uncertainty and unresolved questions in the answer. "
        "State material limits on what the available evidence establishes in plain language. "
        "Missing evidence is not proof of real-world absence. Preserve that distinction "
        "without narrating tool use or internal research steps."
    )
    return "\n".join(lines) + "\n\n"


def _coverage_items(coverage: dict[str, Any]) -> list[str]:
    items = [_coverage_item(item) for item in coverage.get("covered", [])]
    return [item for item in items if item]


def _coverage_item(item: Any) -> str:
    if (
        not isinstance(item, dict)
        or not isinstance(item.get("angle"), str)
        or not item["angle"].strip()
    ):
        return ""
    ids = [
        value.strip()
        for value in item.get("evidence_ids", [])
        if isinstance(value, str) and value.strip()
    ]
    suffix = f" (evidence: {', '.join(ids)})" if ids else ""
    finding = item.get("finding")
    analysis = (
        f"\n  Finding: {finding.strip()}" if isinstance(finding, str) and finding.strip() else ""
    )
    return f"- {item['angle'].strip()}{suffix}{analysis}"


def _coverage_strings(value: Any) -> list[str]:
    return (
        [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if isinstance(value, list)
        else []
    )
