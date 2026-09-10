"""Turn-script JSON parsing and validation.

Extracted from ``audio_overview.py``. ``_extract_json``, ``_validate_turn_script``
and ``_validate_turn_script_single`` are re-exported as module-level attributes
of ``audio_overview`` and imported directly by tests
(``test_audio_overview_tool.py``), so their names and behavior must stay exactly
as they were.

``_harvest_turns`` replaces the old strict ``_validate_script_batch``. Batching
is OUR implementation detail, so the harvester never rejects an answer for
disagreeing about it: a whole script, a partial batch, a per-batch answer, a
bare array, extra fields, prose or reasoning around the JSON, renumbered or
missing indexes, and "Host A"-style speaker labels all harvest to the turns
they actually contain. The only thing it can return is "no usable turns", and
the assembly loop answers that by asking again, then by falling back to a
deterministic report-derived script -- never by failing the overview.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from ._types import LLMResponse, LLMResponseLike, Turn, _turn_band

_LOG = logging.getLogger(__name__)


def _extract_json(text: str) -> str:
    """Extract a JSON array from LLM output that may have surrounding text."""
    # Try direct parse first
    text = text.strip()
    if text.startswith("["):
        # Find the matching closing bracket
        depth = 0
        end = 0
        for i, ch in enumerate(text):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end > 0:
            return text[:end]
    # Try markdown code block
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    # Last resort: find first [ and last ]
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def _validate_turn_script(raw_json: str) -> tuple[list[Turn] | None, str | None]:
    """Validate the JSON turn-script. Returns (turns, error_message)."""
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON: {e}"

    if not isinstance(data, list):
        return None, "Turn script must be a JSON array of turns"

    if len(data) < 2:
        return None, "Turn script must have at least 2 turns"

    turns: list[Turn] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            return None, f"Turn {i} is not an object"
        speaker = item.get("speaker")
        if speaker not in ("A", "B"):
            return None, f"Turn {i}: speaker must be 'A' or 'B', got {speaker!r}"
        text = item.get("text", "")
        if not isinstance(text, str) or not text.strip():
            return None, f"Turn {i}: text must be a non-empty string"
        turns.append(Turn(speaker=speaker, text=text.strip()))

    return turns, None


def _validate_turn_script_single(raw_json: str) -> tuple[list[Turn] | None, str | None]:
    """Validate a single-speaker turn script (all speakers must be 'A').

    Relaxed rules vs :func:`_validate_turn_script`:
    - At least **1** turn (not 2) — a single-voice monologue may be terse.
    - Speaker must be ``'A'``; any ``'B'`` that slips through the prompt is
      silently coerced to ``'A'`` so the whole overview uses ``voice_a``.
    """
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON: {e}"

    if not isinstance(data, list):
        return None, "Turn script must be a JSON array of turns"

    if len(data) < 1:
        return None, "Turn script must have at least 1 turn"

    turns: list[Turn] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            return None, f"Turn {i} is not an object"
        speaker = item.get("speaker", "A")
        if speaker not in ("A", "B"):
            return None, f"Turn {i}: speaker must be 'A' or 'B', got {speaker!r}"
        text = item.get("text", "")
        if not isinstance(text, str) or not text.strip():
            return None, f"Turn {i}: text must be a non-empty string"
        # Coerce any 'B' to 'A' — single-speaker always maps to voice_a.
        turns.append(Turn(speaker="A", text=text.strip()))

    return turns, None


@dataclass(frozen=True)
class _Harvest:
    """What one provider answer actually contained.

    ``turns`` are every usable turn found, in the order they appeared.
    ``total_hint`` is the driver's own proposed length (already pinned into the
    mode's band) or ``None``. ``whole_script`` is True when the answer was a
    bare JSON array -- the legacy "here is the entire script" shape -- so the
    loop knows not to keep asking for more.
    """

    turns: list[Turn]
    total_hint: int | None
    whole_script: bool


def _speaker_for(raw: Any, mode: str, previous: str | None) -> str:
    """Map whatever the driver called the speaker onto ``A``/``B``.

    Single mode is always ``A``. Otherwise accept ``"A"``/``"b"``/``"Host A"``/
    ``"speaker_b"``/``"1"``/``"2"``; anything unrecognised alternates away from
    the previous turn so a dialogue stays a dialogue instead of failing.
    """
    if mode == "single":
        return "A"
    text = str(raw or "").strip().lower()
    if text in ("1", "host 1", "speaker 1"):
        return "A"
    if text in ("2", "host 2", "speaker 2"):
        return "B"
    for ch in reversed(text):
        if ch in ("a", "b"):
            return ch.upper()
    return "B" if previous == "A" else "A"


def _turn_from_item(item: Any, mode: str, previous: str | None) -> Turn | None:
    """One harvested turn, or ``None`` when the item carries no speakable text."""
    if isinstance(item, str):
        text = item.strip()
        return Turn(speaker=_speaker_for(None, mode, previous), text=text) if text else None
    if not isinstance(item, dict):
        return None
    raw_text = item.get("text")
    if not isinstance(raw_text, str):
        for key in ("content", "line", "utterance", "dialogue"):
            candidate = item.get(key)
            if isinstance(candidate, str):
                raw_text = candidate
                break
    if not isinstance(raw_text, str) or not raw_text.strip():
        return None
    speaker_raw = item.get("speaker", item.get("host", item.get("role")))
    return Turn(speaker=_speaker_for(speaker_raw, mode, previous), text=raw_text.strip())


def _items_from_payload(value: Any) -> tuple[list[Any], int | None, bool]:
    """Find the turn list inside whatever JSON shape came back."""
    if isinstance(value, list):
        return value, None, True
    if not isinstance(value, dict):
        return [], None, False
    total = value.get("total_turns")
    hint = total if isinstance(total, int) and not isinstance(total, bool) else None
    for key in ("turns", "script", "batch", "dialogue", "lines"):
        items = value.get(key)
        if isinstance(items, list):
            return items, hint, False
    # A single turn object returned bare (no wrapper list) still carries a turn.
    if any(k in value for k in ("text", "content", "line", "utterance")):
        return [value], hint, False
    return [], hint, False


def _json_fragments(text: str) -> list[Any]:
    """Every top-level JSON value in ``text``, in order.

    Handles prose or a reasoning block around the JSON, a markdown fence, and a
    TRUNCATED array: when the whole payload will not decode, the individual
    complete objects inside it still do, so a cut-off answer yields the turns
    that did arrive instead of nothing.
    """
    stripped = text.strip()
    if not stripped:
        return []
    decoder = json.JSONDecoder()
    found: list[Any] = []
    index = 0
    length = len(stripped)
    while index < length:
        char = stripped[index]
        if char not in "[{":
            index += 1
            continue
        try:
            value, end = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            index += 1
            continue
        found.append(value)
        index += end
    return found


def _harvest_turns(text: str, *, mode: str) -> _Harvest:
    """Take every usable turn out of one provider answer. Never raises."""
    fragments = _json_fragments(text)
    turns: list[Turn] = []
    hint: int | None = None
    whole_script = False
    for fragment in fragments:
        items, fragment_hint, is_array = _items_from_payload(fragment)
        if fragment_hint is not None and hint is None:
            hint = fragment_hint
        if not items:
            continue
        harvested = [
            turn
            for turn in (
                _turn_from_item(item, mode, turns[-1].speaker if turns else None) for item in items
            )
            if turn is not None
        ]
        # A fragment that yielded nothing speakable is reasoning noise, not the
        # script -- it must not claim the "whole script" shape.
        if not harvested:
            continue
        turns.extend(harvested)
        whole_script = whole_script or is_array
    if hint is not None:
        min_turns, max_turns = _turn_band(mode)
        pinned = min(max(hint, min_turns), max_turns)
        if pinned != hint:
            _LOG.info(
                "audio turn-script: driver proposed total_turns=%d outside the %d..%d "
                "band for mode=%s; using %d",
                hint,
                min_turns,
                max_turns,
                mode,
                pinned,
            )
        hint = pinned
    return _Harvest(turns=turns, total_hint=hint, whole_script=whole_script)


def _coerce_llm_response(value: LLMResponseLike) -> LLMResponse:
    """Keep string-returning test/provider adapters compatible, with unknown status."""

    if isinstance(value, LLMResponse):
        return value
    if isinstance(value, str):
        return LLMResponse(content=value, finish_reason=None)
    raise TypeError(f"LLM adapter returned unsupported response type {type(value).__name__}")


def _malformed_retry_payload(payload: dict[str, Any], response: str, error: str) -> dict[str, Any]:
    retry_payload = dict(payload)
    retry_payload["messages"] = list(payload["messages"])
    retry_payload["messages"].extend(
        [
            {"role": "assistant", "content": response},
            {
                "role": "user",
                "content": (
                    f"That answer could not be used: {error}\n\n"
                    "Send the turns again as one JSON object with a "
                    '"turns" array; every turn needs a "speaker" of '
                    '"A" or "B" and non-empty "text" to speak. '
                    "Nothing outside the JSON."
                ),
            },
        ]
    )
    return retry_payload
