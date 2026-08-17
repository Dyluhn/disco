"""Turn-script LLM request payload construction.

Extracted from ``audio_overview.py``. ``_build_llm_payload`` deliberately imports
``disco.agent_server.audio_config`` INSIDE the function body — this is the
upward ``disco.tools`` -> ``disco.agent_server`` import, whitelisted only as a
tracked debt edge (see ``.importlinter``) and only in this lazy, function-local
form. Never hoist this import to module level and never add a new one.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from ._types import Turn

# Each request produces an independently valid JSON batch.  Four turns keeps the
# spoken output comfortably below the common OpenAI-compatible 4096-token cap,
# including room for a reasoning pass.  A length stop shrinks this batch (4 -> 2
# -> 1) rather than replaying the same oversized whole-script request.
_SCRIPT_BATCH_TURNS = 4
_SCRIPT_REASONING_TOKEN_RESERVE = 3072
_SCRIPT_TOKENS_PER_TURN = 256

_TURN_SCRIPT_PROMPT = """You are a podcast scriptwriter. Given a research report, write a short
two-host audio script covering the key insights. Host A is the lead analyst;
Host B is the co-host who asks follow-ups and adds colour.

Each turn has:
  "speaker": "A" or "B"
  "text": one or two sentences (natural spoken language, no markdown)

Keep turns conversational. ~12-20 turns total. Each turn's text should be
~100-200 tokens (a comfortable spoken sentence or two). Start with Host A
introducing the topic.

Report to convert:
{report_text}

Follow the JSON batch protocol below exactly."""

_SINGLE_SCRIPT_PROMPT = """You are an audio narrator. Given a research report, write an honest,
thorough single-voice walkthrough of the key findings.
Cover the main insights, openly discuss any gaps or tensions in the sources,
and give the listener a balanced, honest assessment of what the research shows.

Each turn has:
  "speaker": "A"
  "text": two to three sentences (natural spoken language, no markdown)

Keep it informative and conversational. ~10-16 turns total. Each turn should be
~100-200 tokens. Start with a brief introduction to the topic.

Report to narrate:
{report_text}

Follow the JSON batch protocol below exactly."""


def _build_llm_payload(report_text: str, mode: str = "podcast") -> dict:
    # Resolved through the parent at call time: it owns the whitelisted
    # upward edge, and a test patching the constant there must be observed.
    from .. import audio_overview

    return _build_llm_payload_for_model(
        report_text, audio_overview._audio_llm_model(), mode=mode
    )


def _build_llm_payload_for_model(report_text: str, model: str, mode: str = "podcast") -> dict:
    return _build_segment_payload_for_model(
        report_text,
        model,
        mode=mode,
        start_turn=1,
        requested_turns=_SCRIPT_BATCH_TURNS,
        total_turns=None,
        prior_turns=(),
    )


def _segment_output_tokens(requested_turns: int, max_output_tokens: int | None) -> int | None:
    """Use a real model capability when known; otherwise defer to the provider."""

    if max_output_tokens is None:
        return None
    requested = _SCRIPT_REASONING_TOKEN_RESERVE + requested_turns * _SCRIPT_TOKENS_PER_TURN
    return min(max_output_tokens, requested)


def _build_segment_payload_for_model(
    report_text: str,
    model: str,
    *,
    mode: str,
    start_turn: int,
    requested_turns: int,
    total_turns: int | None,
    prior_turns: Sequence[Turn],
    system_messages: Sequence[dict[str, str]] = (),
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    prompt = _SINGLE_SCRIPT_PROMPT if mode == "single" else _TURN_SCRIPT_PROMPT
    min_turns, max_turns = (10, 16) if mode == "single" else (12, 20)
    end_turn = start_turn + requested_turns - 1
    if total_turns is None:
        total_instruction = (
            f"Choose total_turns once in the inclusive range {min_turns}..{max_turns}."
        )
    else:
        end_turn = min(end_turn, total_turns)
        total_instruction = f"Use total_turns={total_turns}; do not change it."
    context = ""
    if prior_turns:
        context_rows = [
            {
                "index": start_turn - len(prior_turns) + offset,
                "speaker": turn.speaker,
                "text": turn.text,
            }
            for offset, turn in enumerate(prior_turns)
        ]
        context = (
            "\nFor continuity only, these already accepted turns precede this batch; "
            "do not repeat them:\n" + json.dumps(context_rows, ensure_ascii=False)
        )
    batch_protocol = (
        f"""

Return ONLY one valid JSON object with exactly this shape:
{{
  "total_turns": <integer>,
  "turns": [
    {{"index": <integer>, "speaker": "A" or "B", "text": "spoken text"}}
  ]
}}

{total_instruction}
Generate exactly the contiguous turn indexes {start_turn} through {end_turn}.
Never repeat an earlier turn index. Do not include markdown fences or prose outside JSON.
"""
        + context
    )
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            *system_messages,
            {
                "role": "user",
                "content": prompt.format(report_text=report_text) + batch_protocol,
            },
        ],
        "temperature": 0.7,
    }
    output_tokens = _segment_output_tokens(requested_turns, max_output_tokens)
    if output_tokens is not None:
        payload["max_tokens"] = output_tokens
    return payload
