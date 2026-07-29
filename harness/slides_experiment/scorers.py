"""Objective scorers for the C4 experiment outputs.

Three scorer dimensions:
  1. PARSE — did the output parse correctly? (binary + error)
  2. OVERFLOW — estimated overflow risk based on text density
  3. CONTENT — fidelity to the prompt (structural checkers + heuristics)

The LLM-judge scorer is in run.py (needs the API key at runtime).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ParseResult:
    ok: bool
    strategy: str
    prompt_id: str
    raw: str
    parsed: Any = None
    error: str = ""
    slide_count: int = 0


@dataclass
class OverflowResult:
    overflow_risk: str  # "low" | "medium" | "high"
    score: float  # 0.0 (best) to 1.0 (worst)
    detail: str


@dataclass
class ContentResult:
    fidelity_score: float  # 0.0 to 1.0
    layout_variety: int  # distinct layout types used
    has_image_prompts: bool
    has_charts_or_tables: bool
    slide_count: int
    detail: str


@dataclass
class CellScore:
    prompt_id: str
    strategy: str
    parse: ParseResult
    overflow: OverflowResult | None = None
    content: ContentResult | None = None
    llm_judge: float | None = None  # 0.0 to 1.0 from LLM judge
    llm_judge_detail: str = ""


# ---------------------------------------------------------------------------
# 1. Parse scorer
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> str:
    """Extract JSON from a response that might have markdown fences."""
    text = text.strip()
    # Strip markdown fences
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last fence lines
        inner = []
        in_block = False
        for line in lines:
            if line.startswith("```") and not in_block:
                in_block = True
                continue
            if line.startswith("```") and in_block:
                break
            if in_block:
                inner.append(line)
        text = "\n".join(inner).strip()
    return text


def _try_parse_free_form(raw: str) -> tuple[bool, Any, str]:
    """For free-form, try JSON first, then treat as structured markdown."""
    cleaned = _extract_json(raw)
    try:
        parsed = json.loads(cleaned)
        return True, parsed, ""
    except json.JSONDecodeError:
        pass
    # Try to find any JSON object in the text
    matches = re.findall(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", raw, re.DOTALL)
    for m in matches:
        try:
            parsed = json.loads(m)
            if isinstance(parsed, dict) and ("slides" in parsed or "title" in parsed):
                return True, parsed, ""
        except json.JSONDecodeError:
            pass
    # Treat as structured text — still "parseable" but not JSON
    if len(raw) > 100 and ("slide" in raw.lower() or "---" in raw):
        return True, {"_raw_text": raw}, "non-JSON structured text"
    return False, None, "output is neither JSON nor recognizable slide structure"


def score_parse(strategy_id: str, prompt_id: str, raw: str) -> ParseResult:
    """Attempt to parse the model output; return ParseResult."""
    if not raw or len(raw.strip()) < 20:
        return ParseResult(
            ok=False,
            strategy=strategy_id,
            prompt_id=prompt_id,
            raw=raw,
            error="output too short or empty",
        )

    if strategy_id == "free_form":
        ok, parsed, err = _try_parse_free_form(raw)
    else:
        # rigid_json and loose_hybrid both expect JSON
        cleaned = _extract_json(raw)
        try:
            parsed = json.loads(cleaned)
            ok = True
            err = ""
        except json.JSONDecodeError as e:
            # One more attempt: find the first { ... } span
            m = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if m:
                try:
                    parsed = json.loads(m.group())
                    ok = True
                    err = "extracted from partial match"
                except json.JSONDecodeError:
                    ok = False
                    parsed = None
                    err = f"JSON parse error: {e}"
            else:
                ok = False
                parsed = None
                err = f"JSON parse error: {e}"

    slide_count = 0
    if ok and isinstance(parsed, dict):
        slides = parsed.get("slides", [])
        if isinstance(slides, list):
            slide_count = len(slides)

    return ParseResult(
        ok=ok,
        strategy=strategy_id,
        prompt_id=prompt_id,
        raw=raw,
        parsed=parsed,
        error=err,
        slide_count=slide_count,
    )


# ---------------------------------------------------------------------------
# 2. Overflow scorer
# ---------------------------------------------------------------------------

# Approximate limits for a 16:9 slide at ~28pt body font, 5pt line spacing
_TITLE_CHAR_SOFT = 60  # comfortable title
_TITLE_CHAR_HARD = 90  # definitely overflows
_BULLET_CHAR_SOFT = 80  # comfortable bullet
_BULLET_CHAR_HARD = 120  # definitely overflows
_MAX_BULLETS_SOFT = 6  # comfortable bullet count
_MAX_BULLETS_HARD = 10  # definitely overflows


def _collect_slide_bullets(slide: dict, title: str) -> list:
    """Collect all text content (bullets + nested strings) from a slide."""
    bullets: list = []
    bullets.extend(slide.get("bullets", []) or [])
    bullets.extend(slide.get("body", []) or [])
    bullets.extend(slide.get("left_bullets", []) or [])
    bullets.extend(slide.get("right_bullets", []) or [])
    bullets.extend(slide.get("content", []) or [])
    for v in slide.values():
        if isinstance(v, str) and len(v) > 30 and v not in (title,):
            bullets.append(v)
    return bullets


def _title_overflow_risk(title: str) -> float:
    """Return overflow risk for a slide title."""
    if len(title) > _TITLE_CHAR_HARD:
        return 0.9
    if len(title) > _TITLE_CHAR_SOFT:
        return 0.4
    return 0.0


def _bullets_overflow_risk(bullets: list) -> float:
    """Return overflow risk from bullet count and lengths."""
    risk = 0.0
    if len(bullets) > _MAX_BULLETS_HARD:
        risk = max(risk, 1.0)
    elif len(bullets) > _MAX_BULLETS_SOFT:
        risk = max(risk, 0.6)
    for b in bullets:
        if not isinstance(b, str):
            continue
        if len(b) > _BULLET_CHAR_HARD:
            risk = max(risk, 0.8)
        elif len(b) > _BULLET_CHAR_SOFT:
            risk = max(risk, 0.3)
    return risk


def _overflow_score_for_slide(slide: dict) -> float:
    """Return overflow risk score for a single slide (0=fine, 1=definitely overflows)."""
    risk = 0.0
    title = slide.get("title") or slide.get("heading") or ""
    risk = max(risk, _title_overflow_risk(title))
    bullets = _collect_slide_bullets(slide, title)
    risk = max(risk, _bullets_overflow_risk(bullets))
    return risk


def _score_freeform_overflow(raw_text: str) -> OverflowResult:
    """Estimate overflow risk from free-form text line lengths."""
    lines = raw_text.split("\n")
    long_lines = [line for line in lines if len(line) > 100]
    ratio = len(long_lines) / max(len(lines), 1)
    score = min(ratio * 2, 1.0)
    risk = "high" if score > 0.6 else "medium" if score > 0.3 else "low"
    return OverflowResult(
        overflow_risk=risk,
        score=score,
        detail=f"{len(long_lines)}/{len(lines)} lines >100 chars",
    )


def _risk_label(combined: float) -> str:
    """Map a combined overflow score to a risk label."""
    if combined > 0.6:
        return "high"
    if combined > 0.3:
        return "medium"
    return "low"


def score_overflow(parse_result: ParseResult) -> OverflowResult:
    """Score overflow risk across all slides."""
    if not parse_result.ok or not isinstance(parse_result.parsed, dict):
        return OverflowResult(
            overflow_risk="unknown", score=0.5, detail="cannot score: parse failed"
        )

    parsed = parse_result.parsed
    if "_raw_text" in parsed:
        return _score_freeform_overflow(parsed["_raw_text"])

    slides = parsed.get("slides", [])
    if not slides:
        return OverflowResult(
            overflow_risk="unknown", score=0.5, detail="no slides found in parsed output"
        )

    slide_scores = [_overflow_score_for_slide(s) for s in slides if isinstance(s, dict)]
    if not slide_scores:
        return OverflowResult(overflow_risk="unknown", score=0.5, detail="no scorable slides")

    avg_score = sum(slide_scores) / len(slide_scores)
    max_score = max(slide_scores)
    # Weight: worst slide counts heavily
    combined = 0.4 * avg_score + 0.6 * max_score

    overflow_slides = sum(1 for s in slide_scores if s > 0.5)
    detail = (
        f"{overflow_slides}/{len(slide_scores)} slides at overflow risk; "
        f"avg={avg_score:.2f} max={max_score:.2f}"
    )

    return OverflowResult(overflow_risk=_risk_label(combined), score=combined, detail=detail)


# ---------------------------------------------------------------------------
# 3. Content fidelity scorer
# ---------------------------------------------------------------------------

_LAYOUT_TYPES = {
    # rigid_json types
    "title",
    "bullets",
    "two_column",
    "metrics",
    "section_header",
    "image",
    "table",
    "closing",
    # loose_hybrid additions
    "image_right",
    "image_left",
    "comparison",
    "metrics_grid",
    "announcements",
    "resources",
    "checklist",
    "diagram",
}


def _count_layout_variety(slides: list[dict]) -> int:
    """Count distinct layout types used."""
    types = set()
    for s in slides:
        if not isinstance(s, dict):
            continue
        t = s.get("type") or s.get("layout") or s.get("slide_type")
        if t:
            types.add(str(t).lower())
        # Also check layout_hint
        h = s.get("layout_hint")
        if h:
            types.add(str(h).lower())
    return len(types)


def _has_image_prompts(slides: list[dict]) -> bool:
    for s in slides:
        if not isinstance(s, dict):
            continue
        if s.get("image_prompt") or s.get("image_description"):
            return True
    return False


def _has_data_structures(slides: list[dict]) -> bool:
    for s in slides:
        if not isinstance(s, dict):
            continue
        if s.get("chart") or s.get("table") or s.get("metrics"):
            return True
    return False


def _content_fidelity(goal: str, parsed: dict) -> float:
    """Heuristic content fidelity: how many key terms/concepts from goal appear in output."""
    if "_raw_text" in parsed:
        output_text = parsed["_raw_text"].lower()
    else:
        output_text = json.dumps(parsed).lower()

    # Extract key terms from goal (nouns, numbers, proper nouns)
    # Simple heuristic: words > 4 chars, numbers, capitalized words
    goal_lower = goal.lower()
    goal_tokens = set(re.findall(r"\b[a-z]{4,}\b", goal_lower))
    goal_numbers = set(re.findall(r"\$[\d,.]+|\d+[%xM Bm]+|\d{4}", goal))

    if not goal_tokens:
        return 0.5

    found_tokens = sum(1 for t in goal_tokens if t in output_text)
    token_ratio = found_tokens / len(goal_tokens)

    # Number coverage
    found_numbers = sum(1 for n in goal_numbers if n.lower() in output_text)
    number_ratio = found_numbers / max(len(goal_numbers), 1)

    # Weight: text fidelity + numbers
    return 0.7 * token_ratio + 0.3 * number_ratio


def score_content(parse_result: ParseResult, goal: str) -> ContentResult:
    """Score content fidelity and structural richness."""
    if not parse_result.ok or not isinstance(parse_result.parsed, dict):
        return ContentResult(
            fidelity_score=0.0,
            layout_variety=0,
            has_image_prompts=False,
            has_charts_or_tables=False,
            slide_count=0,
            detail="parse failed",
        )

    parsed = parse_result.parsed
    slides = parsed.get("slides", [])
    if not isinstance(slides, list):
        slides = []

    fidelity = _content_fidelity(goal, parsed)
    variety = _count_layout_variety(slides)
    has_img = _has_image_prompts(slides)
    has_data = _has_data_structures(slides)

    details = []
    if has_img:
        details.append("has image_prompt")
    if has_data:
        details.append("has chart/table/metrics")
    details.append(f"{variety} distinct layouts")
    details.append(f"fidelity={fidelity:.2f}")

    return ContentResult(
        fidelity_score=fidelity,
        layout_variety=variety,
        has_image_prompts=has_img,
        has_charts_or_tables=has_data,
        slide_count=len(slides),
        detail="; ".join(details),
    )


# ---------------------------------------------------------------------------
# Aggregate score
# ---------------------------------------------------------------------------


def aggregate_score(cell: CellScore) -> float:
    """Compute an overall 0-1 score for a cell.

    Weights:
      parse:   0.3 (must parse or it's useless)
      overflow: 0.2 (low overflow = better; inverted)
      content:  0.3 (fidelity to the prompt)
      llm_judge: 0.2 (when available)
    """
    parse_score = 1.0 if cell.parse.ok else 0.0

    overflow_score = 0.5
    if cell.overflow:
        overflow_score = 1.0 - cell.overflow.score  # invert: low overflow = good

    content_score = 0.0
    if cell.content:
        content_score = cell.content.fidelity_score

    if cell.llm_judge is not None:
        return 0.3 * parse_score + 0.2 * overflow_score + 0.3 * content_score + 0.2 * cell.llm_judge
    else:
        # Without LLM judge, redistribute weights
        return 0.35 * parse_score + 0.25 * overflow_score + 0.40 * content_score
