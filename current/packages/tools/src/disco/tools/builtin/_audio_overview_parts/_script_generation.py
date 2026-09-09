"""The bounded turn-script assembly loop.

The loop asks the driver for turns and keeps whatever usable turns come back.
Batching is OUR implementation detail, so a disagreement about it is never a
failure: a whole script, a short batch, a renumbered batch, a bare array, extra
fields or prose around the JSON all harvest to the turns they contain
(``_harvest_turns``), duplicates are dropped by text, and a window the driver
under-filled is simply asked for again. Three walls, each removing one wrong
outcome and pointing at the next step:

  * a bounded call/wall-clock budget, so a driver that will not converge stops
    costing time instead of looping;
  * a truncated answer (``finish_reason='length'``) halves the window asked for,
    so the next answer fits;
  * an answer with nothing usable in it gets ONE corrective nudge quoting what
    was wrong, then counts against the barren budget.

When the budget is spent the script is whatever was assembled; if that is too
short to narrate, the script is built deterministically from the report itself
(``_fallback_turns``). The only remaining failure is a report with no text at
all, and it says exactly that. The fallback is never mentioned to the driver.

``_generate_segmented_turn_script`` is re-exported as a module-level attribute
of ``audio_overview`` and accessed via
``audio_overview._generate_segmented_turn_script(...)`` from ``report_audio.py``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from ._fallback_script import _TARGET_SECONDS, _cap_script_duration, _fallback_turns
from ._payload import _SCRIPT_BATCH_TURNS, _build_segment_payload_for_model
from ._types import (
    AudioScriptGenerationError,
    LLMResponse,
    LLMResponseLike,
    ScriptResult,
    Turn,
    _min_viable_turns,
    _turn_band,
)
from ._validation import (
    _coerce_llm_response,
    _harvest_turns,
    _malformed_retry_payload,
)

_LOG = logging.getLogger(__name__)

# Work budget: at four turns a batch, a twenty-turn script needs five calls.
# Twelve leaves room for truncation shrink and corrective nudges and still ends.
_SCRIPT_MAX_PROVIDER_CALLS = 12
# Consecutive answers with nothing usable in them before we stop asking.
_SCRIPT_MAX_BARREN_ATTEMPTS = 3
# Consecutive transport failures (connection reset, 5xx, read timeout) before we
# stop asking. The first failure is retried; the second ends the stage.
_SCRIPT_MAX_TRANSPORT_FAILURES = 2
# Wall-clock backstop for the whole script stage. It never fails the run -- it
# stops issuing calls, and the script becomes whatever is already assembled.
_SCRIPT_STAGE_DEADLINE_S = 420.0

# Kept for the compatibility re-export in ``audio_overview``; the truncation
# response is now "halve the window" with no separate per-batch attempt count.
_SCRIPT_MAX_TRUNCATIONS_PER_BATCH = 3

# NOTE: there is deliberately no "acceptable finish_reason" allow-list any more.
# A provider whose completion vocabulary we don't recognise used to kill a run
# that had returned a perfectly good script. Only "length" still means anything
# here, and it means "ask for less", not "give up".


def _turn_key(turn: Turn) -> str:
    """Identity of a turn's CONTENT, so a driver that restates the script (with
    or without indexes) contributes only what is genuinely new."""
    return " ".join(turn.text.lower().split())


async def _fetch_batch_response(
    call_llm: Callable[[dict[str, Any]], Awaitable[LLMResponseLike]],
    payload: dict[str, Any],
) -> tuple[LLMResponse | None, str]:
    """One provider round-trip. Returns (response, error) -- never raises, so a
    transport failure is retried or degrades instead of killing the overview."""
    try:
        return _coerce_llm_response(await call_llm(payload)), ""
    except Exception as exc:  # transport, auth, decode -- all recoverable here
        return None, f"{type(exc).__name__}: {exc}"


def _target_for(harvest_hint: int | None, accepted: int, mode: str) -> int:
    """How many turns the finished script should have.

    The driver's proposal is a hint pinned into the mode's band; with no
    proposal, a driver that already wrote more than the band's minimum sets the
    length itself, and anything shorter aims at the minimum.
    """
    min_turns, max_turns = _turn_band(mode)
    if harvest_hint is not None:
        return harvest_hint
    return min(max(accepted, min_turns), max_turns)


def _notes_for(source: str, capped: bool, minutes: int) -> str:
    parts: list[str] = []
    if source == "fallback":
        parts.append(
            "The narration was built straight from the report's own sections "
            "because the model did not return a usable script."
        )
    elif source == "partial":
        parts.append("The narration is shorter than usual — the model stopped early.")
    if capped:
        parts.append(
            f"The report was long, so the overview was summarised to about {minutes} minutes."
        )
    return " ".join(parts)


async def _assemble_from_model(
    report_text: str,
    model: str,
    *,
    mode: str,
    call_llm: Callable[[dict[str, Any]], Awaitable[LLMResponseLike]],
    system_messages: Sequence[dict[str, str]],
    max_output_tokens: int | None,
    deadline_s: float,
) -> tuple[list[Turn], int | None, str]:
    """Ask the driver for turns until the target is met or a budget is spent.

    Returns ``(turns, target, last_error)``. Never raises: an empty list means
    the caller should fall back, and ``last_error`` says why in one line.
    """
    accepted: list[Turn] = []
    seen: set[str] = set()
    target: int | None = None
    batch_size = _SCRIPT_BATCH_TURNS
    calls = barren = transport_failures = 0
    correction: dict[str, Any] | None = None
    last_error = ""
    started = time.monotonic()

    while calls < _SCRIPT_MAX_PROVIDER_CALLS and (target is None or len(accepted) < target):
        if time.monotonic() - started > deadline_s:
            last_error = f"script stage passed its {deadline_s:.0f}s budget"
            _LOG.warning("audio turn-script: %s after %d call(s)", last_error, calls)
            break
        start_turn = len(accepted) + 1
        remaining = batch_size if target is None else target - len(accepted)
        requested = max(1, min(batch_size, remaining))
        payload = correction or _build_segment_payload_for_model(
            report_text,
            model,
            mode=mode,
            start_turn=start_turn,
            requested_turns=requested,
            total_turns=target,
            prior_turns=accepted[-2:],
            system_messages=system_messages,
            max_output_tokens=max_output_tokens,
        )
        calls += 1
        response, error = await _fetch_batch_response(call_llm, payload)
        if response is None:
            transport_failures += 1
            last_error = f"LLM call failed at turn {start_turn}: {error}"
            _LOG.warning(
                "audio turn-script: %s (%d/%d)",
                last_error,
                transport_failures,
                _SCRIPT_MAX_TRANSPORT_FAILURES,
            )
            correction = None
            if transport_failures >= _SCRIPT_MAX_TRANSPORT_FAILURES:
                break
            continue

        finish = (response.finish_reason or "").strip().lower()
        harvest = _harvest_turns(response.content, mode=mode)
        fresh = [turn for turn in harvest.turns if _turn_key(turn) not in seen]
        if finish == "length" and requested > 1:
            # The answer was cut off mid-JSON: ask for a smaller window next
            # time rather than replaying the same oversized request.
            batch_size = max(1, requested // 2)

        if fresh:
            seen.update(_turn_key(turn) for turn in fresh)
            accepted.extend(fresh)
            barren = 0
            correction = None
            if target is None:
                target = _target_for(harvest.total_hint, len(accepted), mode)
            del accepted[target:]
            if harvest.whole_script:
                break  # a bare array is the driver saying "that's the script"
            continue

        barren += 1
        last_error = (
            f"no usable turns in the answer at turn {start_turn} "
            f"(finish_reason={finish or 'unset'!r}, {len(response.content)} chars)"
        )
        _LOG.warning(
            "audio turn-script: %s; barren attempt %d/%d",
            last_error,
            barren,
            _SCRIPT_MAX_BARREN_ATTEMPTS,
        )
        if barren >= _SCRIPT_MAX_BARREN_ATTEMPTS:
            break
        correction = (
            None
            if finish == "length"
            else _malformed_retry_payload(
                payload,
                response.content,
                f"it contained no turns with speakable text (finish_reason={finish or 'unset'})",
            )
        )

    _LOG.info(
        "audio turn-script: mode=%s assembled %d/%s turn(s) in %d call(s), %.1fs",
        mode,
        len(accepted),
        target if target is not None else "?",
        calls,
        time.monotonic() - started,
    )
    return accepted, target, last_error


async def _generate_turn_script_result(
    report_text: str,
    model: str,
    *,
    mode: str,
    call_llm: Callable[[dict[str, Any]], Awaitable[LLMResponseLike]],
    system_messages: Sequence[dict[str, str]] = (),
    max_output_tokens: int | None = None,
    target_seconds: int = _TARGET_SECONDS,
    deadline_s: float = _SCRIPT_STAGE_DEADLINE_S,
) -> ScriptResult:
    """Produce a narratable script, saying where it came from.

    Raises :class:`AudioScriptGenerationError` only when there is nothing to
    narrate at all -- no usable model output AND no report text to fall back on
    -- and the message says exactly that.
    """
    accepted, target, last_error = await _assemble_from_model(
        report_text,
        model,
        mode=mode,
        call_llm=call_llm,
        system_messages=system_messages,
        max_output_tokens=max_output_tokens,
        deadline_s=deadline_s,
    )

    source = "model"
    if len(accepted) < _min_viable_turns(mode):
        fallback = _fallback_turns(report_text, mode=mode)
        if fallback:
            _LOG.warning(
                "audio turn-script: mode=%s falling back to the report-derived script "
                "(%d turn(s)); last error: %s",
                mode,
                len(fallback),
                last_error or "none",
            )
            accepted, source = fallback, "fallback"
        elif not accepted:
            raise AudioScriptGenerationError(
                "There was no narration script and no report text to build one from"
                + (f" ({last_error})" if last_error else "")
            )
    elif target is not None and len(accepted) < target:
        source = "partial"

    accepted, capped = _cap_script_duration(accepted, mode, target_seconds=target_seconds)
    if capped:
        _LOG.info(
            "audio turn-script: mode=%s summarised to %d turn(s) for the ~%d min cap",
            mode,
            len(accepted),
            round(target_seconds / 60),
        )
    return ScriptResult(
        turns=accepted,
        source=source,
        note=_notes_for(source, capped, round(target_seconds / 60)),
    )


async def _generate_segmented_turn_script(
    report_text: str,
    model: str,
    *,
    mode: str,
    call_llm: Callable[[dict[str, Any]], Awaitable[LLMResponseLike]],
    system_messages: Sequence[dict[str, str]] = (),
    max_output_tokens: int | None = None,
) -> list[Turn]:
    """Turns only -- the long-standing entry point. See
    :func:`_generate_turn_script_result` for how the script was obtained."""
    result = await _generate_turn_script_result(
        report_text,
        model,
        mode=mode,
        call_llm=call_llm,
        system_messages=system_messages,
        max_output_tokens=max_output_tokens,
    )
    return result.turns
