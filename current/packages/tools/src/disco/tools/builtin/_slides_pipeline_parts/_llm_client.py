"""Chat-completions transport + completion-status handling for the deck author.

Extracted from ``_slides_pipeline.py``. ``_call_llm`` is re-exported as a
module-level attribute of ``_slides_pipeline`` and accessed directly by tests
(``patch.object(slides, "_call_llm", ...)``) — the real callers
(``_stage_outline``/``_stage_fill``) stay physically in ``_slides_pipeline.py``
and resolve the name at call time through that module's own globals, so the
patch keeps working regardless of where the implementation itself lives.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Literal, cast

import httpx  # noqa: F401 - compatibility re-export from the pipeline facade
from disco.core import LLMMessage
from disco.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    CompletionResponse,
    Difficulty,
    ModelRole,
)

from ._constants import _VALID_THEMES_STR
from ._types import LLMResponse, LLMResponseLike, SlidesGenerationIncompleteError

type ProviderCompletion = Callable[[CompletionRequest], Awaitable[CompletionResponse]]


def _provider_neutral_request(
    messages: Sequence[dict[str, str]], *, model: str
) -> CompletionRequest:
    """Build the one provider-neutral request used by the slide author.

    ``model`` is retained as opaque metadata for injected adapters. The router
    remains the authority for concrete model selection; the field is useful to
    small host seams that already have a pinned model and harmlessly ignored by
    router-backed callbacks.
    """
    return CompletionRequest(
        profile=CapabilityProfile(
            role=ModelRole.AGENT_DRIVER,
            difficulty=Difficulty.HARD,
        ),
        messages=[
            LLMMessage(
                role=cast(Literal["system", "user", "assistant", "tool"], m["role"]),
                content=m["content"],
            )
            for m in messages
        ],
        temperature=0.7,
        max_tokens=24576,
        response_format="json",
        metadata={"slides": True, "requested_model": model},
    )


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
    completion: ProviderCompletion | None = None,
    temperature: float = 0.7,
    max_tokens: int = 24576,
) -> LLMResponse:
    """Call the host-provided canonical completion adapter.

    The old slides-specific HTTP transport is intentionally gone. Production
    hosts must inject ``DefaultLLMRouter.complete`` through the host-only
    ``ToolContext`` seam; standalone callers fail closed instead of bypassing
    provider routing, secrets, policy, or shape normalization.
    """
    del llm_url, api_key, temperature, max_tokens
    if completion is None:
        raise RuntimeError(
            "slides_generate requires the host canonical completion adapter; "
            "raw slides-specific HTTP is disabled"
        )
    response = await completion(_provider_neutral_request(messages, model=model))
    from disco.core.think import strip_think_spans

    return LLMResponse(
        content=strip_think_spans(response.text),
        finish_reason=response.finish_reason,
    )


def _complete_response_content(response: LLMResponseLike, *, stage: str) -> str:
    """Return content only when the provider did not report an incomplete stop.

    Plain strings remain accepted for test and legacy adapters whose completion
    status is unknown. The canonical host adapter always returns ``LLMResponse``.
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
