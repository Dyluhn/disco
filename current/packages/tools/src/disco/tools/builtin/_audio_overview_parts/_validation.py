"""Turn-script JSON parsing and validation.

Extracted from ``audio_overview.py``. ``_extract_json``, ``_validate_turn_script``
and ``_validate_turn_script_single`` are re-exported as module-level attributes
of ``audio_overview`` and imported directly by tests
(``test_audio_overview_tool.py``), so their names and behavior must stay exactly
as they were.

``_validate_script_batch`` is split into ``_parse_batch_total`` (the
``total_turns`` shape/range/consistency checks) and ``_parse_batch_items`` (the
per-item loop) purely to bring its cyclomatic complexity under budget.
The one deliberate behavior change since the split: a first-batch
``total_turns`` outside the mode's band is pinned into the band rather than
rejected (see ``_parse_batch_total``).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ._types import LLMResponse, LLMResponseLike, Turn, _ScriptBatch

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


def _extract_script_json(text: str) -> str:
    """Extract the first complete JSON object or array without joining fragments."""

    stripped = text.strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, (dict, list)):
        return stripped

    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char not in "[{":
            continue
        try:
            value, end = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, (dict, list)):
            return stripped[index : index + end]
    return stripped


def _parse_batch_total(
    data: dict,
    *,
    mode: str,
    start_turn: int,
    expected_total: int | None,
) -> tuple[int | None, str | None]:
    """Validate the batch's ``total_turns`` field: type, consistency with any
    already-agreed total / the current start_turn, and pin it into the mode's
    length band. Returns the total to use, or an error string."""

    total = data.get("total_turns")
    if isinstance(total, bool) or not isinstance(total, int):
        return None, "Script batch total_turns must be an integer"
    min_turns, max_turns = (10, 16) if mode == "single" else (12, 20)
    # The band is OUR length requirement for the finished overview, not the
    # driver's plan: the model only picks the total on the FIRST batch, and
    # every batch after that is handed the agreed number verbatim. Rejecting an
    # out-of-band choice killed the whole audio overview after one correction
    # attempt on any driver that stubbornly picks its own length (deepseek-v4
    # -flash on ollama.com does, twice in a row) — so pin the number into the
    # band and carry on. Drift INSIDE the band is still a real inconsistency
    # and is still rejected below.
    clamped = min(max(total, min_turns), max_turns)
    if clamped != total:
        _LOG.warning(
            "audio turn-script: driver chose total_turns=%d outside the %d..%d band "
            "for mode=%s; pinned to %d",
            total,
            min_turns,
            max_turns,
            mode,
            clamped,
        )
    if expected_total is not None and clamped != expected_total:
        return None, f"Script batch changed total_turns from {expected_total} to {total}"
    if start_turn > clamped:
        return None, f"Script batch starts at {start_turn} after total_turns={clamped}"
    return clamped, None


def _parse_batch_items(
    data: dict,
    *,
    start_turn: int,
    requested_turns: int,
    total: int,
    mode: str,
) -> tuple[list[Turn] | None, str | None]:
    """Validate the batch's ``turns`` array: shape, contiguous indexing, and
    each turn's own fields."""

    items = data.get("turns")
    if not isinstance(items, list):
        return None, "Script batch turns must be a JSON array"
    expected_count = min(requested_turns, total - start_turn + 1)
    if len(items) != expected_count:
        return (
            None,
            f"Script batch must contain exactly {expected_count} contiguous turns; "
            f"received {len(items)}",
        )

    turns: list[Turn] = []
    for offset, item in enumerate(items):
        expected_index = start_turn + offset
        if not isinstance(item, dict):
            return None, f"Turn {expected_index} is not an object"
        index = item.get("index")
        if index != expected_index:
            return (
                None,
                f"Expected turn index {expected_index}, received {index!r}; "
                "turns must not be skipped or repeated",
            )
        speaker = item.get("speaker")
        if speaker not in ("A", "B"):
            return None, f"Turn {expected_index}: invalid speaker {speaker!r}"
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            return None, f"Turn {expected_index}: text must be a non-empty string"
        turns.append(Turn(speaker="A" if mode == "single" else speaker, text=text.strip()))
    return turns, None


def _validate_script_batch(
    raw_json: str,
    *,
    mode: str,
    start_turn: int,
    requested_turns: int,
    expected_total: int | None,
) -> tuple[_ScriptBatch | None, list[Turn] | None, str | None]:
    """Validate one indexed batch, or an explicit legacy complete-array response."""

    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        return None, None, f"Invalid JSON: {exc}"

    validate_full = _validate_turn_script_single if mode == "single" else _validate_turn_script
    if isinstance(data, list):
        if start_turn != 1:
            return None, None, "Continuation returned an unindexed legacy turn array"
        legacy_turns, error = validate_full(raw_json)
        return None, legacy_turns, error
    if not isinstance(data, dict):
        return None, None, "Script batch must be a JSON object"

    total, error = _parse_batch_total(
        data, mode=mode, start_turn=start_turn, expected_total=expected_total
    )
    if error is not None:
        return None, None, error
    assert total is not None

    turns, error = _parse_batch_items(
        data, start_turn=start_turn, requested_turns=requested_turns, total=total, mode=mode
    )
    if error is not None:
        return None, None, error
    assert turns is not None

    return _ScriptBatch(total_turns=total, turns=turns), None, None


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
                    f"Your JSON batch was invalid: {error}\n\n"
                    "Return ONLY a corrected, complete JSON object for the exact indexed "
                    "batch requested. Do not repeat, skip, or renumber turns."
                ),
            },
        ]
    )
    return retry_payload
