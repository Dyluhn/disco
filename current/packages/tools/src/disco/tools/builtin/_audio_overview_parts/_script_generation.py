"""The bounded, segmented turn-script assembly loop.

Extracted from ``audio_overview.py``. ``_generate_segmented_turn_script`` is
re-exported as a module-level attribute of ``audio_overview`` and accessed via
``audio_overview._generate_segmented_turn_script(...)`` from ``report_audio.py``.

The single round-trip logic (one provider call -> finish_reason check ->
retry/continue/accept decision) was pulled into four small helpers
(``_fetch_batch_response``, ``_next_batch_size_or_raise``,
``_ensure_finish_complete``, ``_retry_payload_or_raise``) purely to bring the
outer loop's cyclomatic complexity under budget. None of the checks, retry
bookkeeping, or error strings changed — only where the branching lives.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from ._payload import _SCRIPT_BATCH_TURNS, _build_segment_payload_for_model
from ._types import AudioScriptGenerationError, LLMResponse, LLMResponseLike, Turn
from ._validation import (
    _coerce_llm_response,
    _extract_script_json,
    _malformed_retry_payload,
    _validate_script_batch,
    _validate_turn_script,
    _validate_turn_script_single,
)

_SCRIPT_MAX_PROVIDER_CALLS = 32
_SCRIPT_MAX_TRUNCATIONS_PER_BATCH = 3


async def _fetch_batch_response(
    call_llm: Callable[[dict[str, Any]], Awaitable[LLMResponseLike]],
    payload: dict[str, Any],
    start_turn: int,
) -> LLMResponse:
    """One provider round-trip; wraps any failure as AudioScriptGenerationError."""
    try:
        return _coerce_llm_response(await call_llm(payload))
    except Exception as exc:
        raise AudioScriptGenerationError(
            f"LLM call failed while generating audio turn {start_turn}: {exc}; "
            "no partial artifact was written"
        ) from exc


def _next_batch_size_or_raise(requested_turns: int, truncations: int, start_turn: int) -> int:
    """After a finish_reason='length' response: shrink the batch, or give up if
    the bounded number of truncation attempts (or a single-turn batch) is spent."""
    if truncations >= _SCRIPT_MAX_TRUNCATIONS_PER_BATCH or requested_turns == 1:
        raise AudioScriptGenerationError(
            "Provider truncated the audio turn-script batch at "
            f"turn {start_turn} after {truncations} bounded attempts "
            "(finish_reason='length'); no partial artifact was written"
        )
    return max(1, requested_turns // 2)


def _ensure_finish_complete(finish_reason: str, start_turn: int) -> None:
    if finish_reason not in ("", "stop", "end_turn", "eos", "eos_token"):
        raise AudioScriptGenerationError(
            "Provider did not complete the audio turn-script batch at "
            f"turn {start_turn} (finish_reason={finish_reason!r}); "
            "no partial artifact was written"
        )


def _retry_payload_or_raise(
    *,
    malformed_retry_used: bool,
    payload: dict[str, Any],
    response_content: str,
    error: str,
    start_turn: int,
) -> dict[str, Any]:
    if malformed_retry_used:
        raise AudioScriptGenerationError(
            "Audio turn-script batch remained invalid after one correction "
            f"at turn {start_turn}: {error}; no partial artifact was written"
        )
    return _malformed_retry_payload(payload, response_content, error)


def _payload_for_attempt(
    retry_payload: dict[str, Any] | None,
    *,
    report_text: str,
    model: str,
    mode: str,
    start_turn: int,
    batch_size: int,
    total_turns: int | None,
    accepted: list[Turn],
    system_messages: Sequence[dict[str, str]],
    max_output_tokens: int | None,
) -> tuple[dict[str, Any], int]:
    """Return (payload, requested_turns) for one attempt: reuse a pending retry
    payload verbatim (it already carries its own requested-turn count baked in
    from when it was built), or build a fresh payload sized to what's left."""
    requested_turns = min(
        batch_size,
        (total_turns - len(accepted)) if total_turns is not None else batch_size,
    )
    payload = retry_payload or _build_segment_payload_for_model(
        report_text,
        model,
        mode=mode,
        start_turn=start_turn,
        requested_turns=requested_turns,
        total_turns=total_turns,
        prior_turns=accepted[-2:],
        system_messages=system_messages,
        max_output_tokens=max_output_tokens,
    )
    return payload, requested_turns


def _finalize_script(accepted: list[Turn], mode: str) -> list[Turn]:
    """Re-validate the fully-assembled script as a whole before returning it."""
    serialized = json.dumps([turn.model_dump() for turn in accepted])
    validate_full = _validate_turn_script_single if mode == "single" else _validate_turn_script
    validated, error = validate_full(serialized)
    if error is not None or validated is None:
        raise AudioScriptGenerationError(
            f"Assembled audio turn-script failed final validation: {error}; "
            "no partial artifact was written"
        )
    return validated


async def _generate_segmented_turn_script(
    report_text: str,
    model: str,
    *,
    mode: str,
    call_llm: Callable[[dict[str, Any]], Awaitable[LLMResponseLike]],
    system_messages: Sequence[dict[str, str]] = (),
    max_output_tokens: int | None = None,
) -> list[Turn]:
    """Generate and deterministically assemble bounded, independently valid batches."""

    accepted: list[Turn] = []
    total_turns: int | None = None
    batch_size = _SCRIPT_BATCH_TURNS
    provider_calls = 0

    while total_turns is None or len(accepted) < total_turns:
        start_turn = len(accepted) + 1
        malformed_retry_used = False
        truncations = 0
        retry_payload: dict[str, Any] | None = None

        while True:
            provider_calls += 1
            if provider_calls > _SCRIPT_MAX_PROVIDER_CALLS:
                raise AudioScriptGenerationError(
                    "Audio turn-script exceeded the bounded 32-call assembly limit; "
                    f"stopped before turn {start_turn} with no partial artifact written"
                )
            payload, requested_turns = _payload_for_attempt(
                retry_payload,
                report_text=report_text,
                model=model,
                mode=mode,
                start_turn=start_turn,
                batch_size=batch_size,
                total_turns=total_turns,
                accepted=accepted,
                system_messages=system_messages,
                max_output_tokens=max_output_tokens,
            )

            response = await _fetch_batch_response(call_llm, payload, start_turn)

            finish_reason = (response.finish_reason or "").strip().lower()
            if finish_reason == "length":
                truncations += 1
                batch_size = _next_batch_size_or_raise(requested_turns, truncations, start_turn)
                retry_payload = None
                continue
            _ensure_finish_complete(finish_reason, start_turn)

            raw_json = _extract_script_json(response.content)
            batch, legacy_turns, error = _validate_script_batch(
                raw_json,
                mode=mode,
                start_turn=start_turn,
                requested_turns=requested_turns,
                expected_total=total_turns,
            )
            if error is not None:
                retry_payload = _retry_payload_or_raise(
                    malformed_retry_used=malformed_retry_used,
                    payload=payload,
                    response_content=response.content,
                    error=error,
                    start_turn=start_turn,
                )
                malformed_retry_used = True
                continue
            if legacy_turns is not None:
                return legacy_turns
            assert batch is not None
            if total_turns is None:
                total_turns = batch.total_turns
            accepted.extend(batch.turns)
            break

    return _finalize_script(accepted, mode)
