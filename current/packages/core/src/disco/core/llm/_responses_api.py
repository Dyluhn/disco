"""OpenAI Responses API request/response compatibility.

Disco's neutral provider contract predates the Responses API.  This module
adapts text completion requests for endpoints explicitly configured with a
``/responses`` URL while leaving the established Chat Completions path
untouched.  Tool-bearing transcripts are refused instead of being translated
lossily; Deep Research uses text-only calls through this seam.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from .errors import LLMError
from .types import CompletionRequest, CompletionResponse, TokenUsage


def is_responses_endpoint(base_url: str) -> bool:
    """Return whether ``base_url`` names a Responses API endpoint."""

    return urlsplit(base_url).path.rstrip("/").endswith("/responses")


def _message_input(req: CompletionRequest, provider_name: str) -> list[dict[str, object]]:
    if req.tools or any(message.tool_calls or message.role == "tool" for message in req.messages):
        raise LLMError(
            "Responses API compatibility does not support tool-bearing requests",
            provider=provider_name,
        )
    items: list[dict[str, object]] = []
    for message in req.messages:
        if message.images:
            content: list[dict[str, str]] = [{"type": "input_text", "text": message.content}]
            content.extend({"type": "input_image", "image_url": image} for image in message.images)
            items.append({"role": message.role, "content": content})
        else:
            items.append({"role": message.role, "content": message.content})
    if req.assistant_prefill:
        items.append({"role": "assistant", "content": req.assistant_prefill})
    return items


def build_responses_payload(
    *,
    base_url: str,
    provider_name: str,
    req: CompletionRequest,
    model: str,
) -> dict:
    """Translate one neutral text request into a Responses API payload."""

    is_meta = "api.meta.ai" in urlsplit(base_url).netloc.lower()
    body: dict = {
        "model": model,
        "input": _message_input(req, provider_name),
        "temperature": req.temperature,
    }
    if req.max_tokens is not None:
        output_tokens = req.max_tokens + max(1024, req.max_tokens) if is_meta else req.max_tokens
        # Responses counts private reasoning against the same allowance as
        # visible text.  Disco's neutral max_tokens contract budgets the answer,
        # so Muse needs bounded headroom for its reasoning channel.  Without it a
        # normal report-planning call returns HTTP 200 with 100% reasoning tokens
        # and an empty answer.  The fixed floor also makes tiny one-word verifier
        # calls useful instead of predictably spending their whole budget thinking.
        body["max_output_tokens"] = max(16, output_tokens)
    if req.response_format == "json":
        body["text"] = {"format": {"type": "json_object"}}
    if is_meta:
        # Muse spends part of the output allowance on private reasoning.  Low
        # preserves reasoning while reserving useful capacity for visible text.
        body["reasoning"] = {"effort": "low"}
    return body


def _output_text(data: dict) -> str:
    pieces: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict) or part.get("type") not in {"output_text", "text"}:
                continue
            value = part.get("text")
            if isinstance(value, str):
                pieces.append(value)
    if pieces:
        return "".join(pieces)
    top_level = data.get("output_text")
    return top_level if isinstance(top_level, str) else ""


def decode_responses_response(
    *,
    req: CompletionRequest,
    model: str,
    data: dict,
) -> CompletionResponse:
    """Decode a non-streaming Responses API body to Disco's neutral response."""

    usage = data.get("usage") or {}
    input_details = usage.get("input_tokens_details") or {}
    model_text = _output_text(data)
    status = data.get("status")
    incomplete = data.get("incomplete_details") or {}
    if status == "completed":
        finish_reason = "stop"
    elif incomplete.get("reason") in {"max_output_tokens", "max_tokens"}:
        finish_reason = "length"
    else:
        finish_reason = "error"
    reasoning_details = usage.get("output_tokens_details") or {}
    reasoning_tokens = int(reasoning_details.get("reasoning_tokens", 0) or 0)
    metadata: dict[str, object] = {}
    if reasoning_tokens:
        metadata["reasoning_tokens"] = reasoning_tokens
    return CompletionResponse(
        text=(req.assistant_prefill or "") + model_text,
        tool_calls=[],
        usage=TokenUsage(
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cost_usd=0.0,
            cached_tokens=int(input_details.get("cached_tokens", 0) or 0),
        ),
        finish_reason=finish_reason,
        model_used=data.get("model", model),
        request_id=req.request_id,
        routing=None,
        response_metadata=metadata,
    )
