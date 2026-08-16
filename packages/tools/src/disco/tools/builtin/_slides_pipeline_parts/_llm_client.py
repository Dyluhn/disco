"""Chat-completions transport + completion-status handling for the deck author.

Extracted from ``_slides_pipeline.py``. ``_call_llm`` is re-exported as a
module-level attribute of ``_slides_pipeline`` and accessed directly by tests
(``patch.object(slides, "_call_llm", ...)``) — the real callers
(``_stage_outline``/``_stage_fill``) stay physically in ``_slides_pipeline.py``
and resolve the name at call time through that module's own globals, so the
patch keeps working regardless of where the implementation itself lives.
"""

from __future__ import annotations

import httpx

from ._constants import _VALID_THEMES_STR
from ._types import LLMResponse, LLMResponseLike, SlidesGenerationIncompleteError


def _retry_msg(err: str) -> str:
    """Build a targeted retry prompt that names the specific parse error and
    lists ALL valid theme values.  The theme enum is the most common mismatch
    (models hallucinate values like ``"dark-research"`` when only shown 3 of the
    8 valid strings) so we call it out explicitly on every retry."""
    return (
        "Your previous response was not valid JSON or did not match the AuthoredDeck schema.\n\n"
        f"Error detail: {err}\n\n"
        f'The "theme" field MUST be exactly one of: {_VALID_THEMES_STR}\n\n'
        "Please fix all errors and output ONLY valid JSON matching the AuthoredDeck schema.\n"
        "No markdown, no prose — only the raw JSON object."
    )


async def _call_llm(
    messages: list[dict[str, str]],
    llm_url: str,
    model: str,
    *,
    api_key: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 24576,
) -> LLMResponse:
    """Call the endpoint without discarding its completion status.

    Sends a Bearer Authorization header when `api_key` is provided so a remote
    driver (OpenRouter / paid endpoint) authenticates; local keyless endpoints
    pass api_key=None and send no auth header (unchanged).

    Gauntlet run-1 root cause (2026-07-07): reasoning drivers (MiniMax M3) think
    for minutes on outline/fill-sized prompts — the old 120s cap produced
    ``httpx.ReadTimeout`` whose ``str()`` is EMPTY, so the degraded note carried a
    blank reason and every deck silently fell back to the plain renderer. Read
    timeout is now generous (the deck author is a background step, not a UI
    turn), timeouts raise with a NAMED reason, and the returned content goes
    through the canonical think-strip — this raw path bypasses the router, so
    nothing else removes a reasoning model's ``<think>`` span before JSON
    parsing."""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data: dict | None = None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(600.0, connect=30.0),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            for attempt in range(2):
                resp = await client.post(
                    f"{llm_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                if attempt == 0 and (resp.status_code == 429 or resp.status_code >= 500):
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
    except httpx.TimeoutException as e:
        raise RuntimeError(
            f"{type(e).__name__} after 600s from {llm_url} (reasoning models can "
            "exceed short caps; the driver endpoint may be slow or wedged)"
        ) from e
    if data is None:  # pragma: no cover - the bounded loop returns or raises
        raise RuntimeError("slide author returned no response payload")
    from disco.core.think import strip_think_spans

    choice = data["choices"][0]
    content = choice["message"].get("content") or ""
    return LLMResponse(
        content=strip_think_spans(content),
        finish_reason=choice.get("finish_reason"),
    )


def _complete_response_content(response: LLMResponseLike, *, stage: str) -> str:
    """Return content only when the provider did not report an incomplete stop.

    Plain strings remain accepted for test and legacy adapters whose completion
    status is unknown. The production HTTP adapter always returns ``LLMResponse``.
    """
    if isinstance(response, str):
        return response
    if not isinstance(response, LLMResponse):
        raise TypeError(
            f"Slide LLM adapter returned unsupported response type {type(response).__name__}"
        )
    finish_reason = (response.finish_reason or "").strip().lower()
    if finish_reason == "length":
        raise SlidesGenerationIncompleteError(stage, finish_reason)
    if finish_reason not in ("", "stop", "end_turn", "eos", "eos_token"):
        raise SlidesGenerationIncompleteError(stage, finish_reason)
    return response.content
