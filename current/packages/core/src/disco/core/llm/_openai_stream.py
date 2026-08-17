"""OpenAI-compatible streaming response assembly.

Extracted from ``openai_provider.py`` so the provider class stays a thin facade.
This module owns the SSE stream-decoding, tool-call fragment accumulation, and
weak-model recovery for the streaming path. Never restarts after yielded bytes.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable

import httpx

from ._request_assembly import FinishReason
from .errors import LLMTransientError
from .types import (
    CompletionRequest,
    CompletionResponse,
    ProposedToolCall,
    StreamChunk,
    TokenUsage,
    ToolSpec,
)

_RecoveryFn = Callable[[str, str | None], list[ProposedToolCall]]
_ToolCallsFn = Callable[
    [list | None, list[ToolSpec] | None],
    list[ProposedToolCall],
]


def _resolve_slot_index(
    raw_idx: int | None,
    tc_id: str | None,
    id_to_idx: dict[str, int],
    last_idx: int | None,
) -> int:
    """Resolve the accumulation slot for a streaming tool-call fragment.

    Defaulting a MISSING ``index`` to 0 mis-routes every index-less
    continuation fragment onto the FIRST tool call. Resolve in priority order
    so NO fragment is lost:
      1. explicit ``index`` — authoritative (compliant servers send it);
      2. an open slot whose ``id`` matches — interleaved continuations;
      3. the most-recently-touched slot — sequential continuations that drop
         both id and index (the common llama.cpp shape).
    """
    if raw_idx is not None:
        return raw_idx
    if tc_id and tc_id in id_to_idx:
        return id_to_idx[tc_id]
    if last_idx is not None:
        return last_idx
    return 0


def _complete_json_object(raw: str) -> bool:
    """Whether ``raw`` is exactly one complete JSON object.

    Some compatible providers omit both ``index`` and ``id`` even when starting
    another tool call. A completed object followed by a fresh call header is the
    only safe boundary available in that dialect.
    """
    if not raw.strip():
        return False
    try:
        value, end = json.JSONDecoder().raw_decode(raw)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(value, dict) and not raw[end:].strip()


def _starts_indexless_call(
    tc: dict,
    *,
    tool_buf: dict[int, dict],
    id_to_idx: dict[str, int],
    last_idx: int | None,
) -> bool:
    """Detect a new call header from providers that omit call identity.

    Continuation fragments have neither a new id nor a function name. A fresh
    id/name arriving after the current slot already contains one complete JSON
    object is therefore a new call, even when the provider forgot its index.
    """
    if tc.get("index") is not None or last_idx is None:
        return False
    tc_id = tc.get("id")
    if tc_id and tc_id in id_to_idx:
        return False
    fn = tc.get("function") or {}
    if not tc_id and not fn.get("name"):
        return False
    current = tool_buf.get(last_idx)
    return bool(current and current.get("name") and _complete_json_object(current.get("args", "")))


def _accumulate_tool_call_fragment(
    tc: dict,
    *,
    tool_buf: dict[int, dict],
    id_to_idx: dict[str, int],
    last_idx: int | None,
) -> tuple[int, str | None]:
    """Accumulate one tool-call delta fragment into ``tool_buf``.

    Returns ``(idx, args_frag)`` where ``args_frag`` is the argument fragment
    string (or None) so the caller can yield the watch-it-write chunk.
    """
    raw_idx = tc.get("index")
    tc_id = tc.get("id")
    if _starts_indexless_call(
        tc,
        tool_buf=tool_buf,
        id_to_idx=id_to_idx,
        last_idx=last_idx,
    ):
        idx = max(tool_buf, default=-1) + 1
    else:
        idx = _resolve_slot_index(raw_idx, tc_id, id_to_idx, last_idx)
    slot = tool_buf.setdefault(idx, {"id": None, "name": "", "args": ""})
    if tc_id:
        slot["id"] = tc_id
        id_to_idx[tc_id] = idx
    fn = tc.get("function") or {}
    if fn.get("name"):
        slot["name"] = fn["name"]
    args_frag = fn.get("arguments")
    if args_frag:
        slot["args"] += args_frag
    return idx, args_frag


def _build_stream_final_response(
    *,
    accumulated_text: str,
    reasoning_content: str,
    tool_buf: dict[int, dict],
    usage: dict,
    finish: str | None,
    model_used: str,
    req: CompletionRequest,
    tool_calls_fn: _ToolCallsFn,
    recovery_fn: _RecoveryFn,
    cached_tokens_fn: Callable[[dict], int],
    metadata_fn: Callable[..., dict],
    map_finish_fn: Callable[[str | None], FinishReason],
) -> CompletionResponse:
    """Assemble the final CompletionResponse from streaming accumulators."""
    tool_calls: list[ProposedToolCall] = tool_calls_fn(
        [
            {
                "id": slot["id"],
                "function": {"name": slot["name"], "arguments": slot["args"]},
            }
            for slot in tool_buf.values()
        ],
        req.tools,
    )
    finish_reason = map_finish_fn(finish)
    if not tool_calls and req.assist:
        recovered = recovery_fn(accumulated_text, reasoning_content)
        if recovered:
            tool_calls = recovered
            finish_reason = "tool_calls"
    return CompletionResponse(
        text=accumulated_text,
        tool_calls=tool_calls,
        usage=TokenUsage(
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
            cost_usd=0.0,
            cached_tokens=cached_tokens_fn(usage),
        ),
        finish_reason=finish_reason,
        model_used=model_used,
        request_id=req.request_id,
        routing=None,
        response_metadata=metadata_fn(
            finish_reason=finish_reason,
            content_len=len(accumulated_text) - len(req.assistant_prefill or ""),
            reasoning_len=len(reasoning_content),
            tool_call_count=len(tool_calls),
        ),
    )


async def _iter_stream_chunks(
    *,
    client_factory: Callable[[], httpx.AsyncClient],
    base_url: str,
    payload: dict,
    headers: dict[str, str],
    raise_typed_fn: Callable[[int, str], None],
) -> AsyncIterator[str]:
    """Open the streaming connection and yield raw SSE lines.

    Yields ``(line_str, resp)`` pairs. The caller is responsible for parsing
    each line. Raises typed errors for non-2xx responses.
    """
    async with client_factory() as client:
        async with client.stream(
            "POST",
            f"{base_url}/chat/completions",
            json=payload,
            headers=headers,
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise_typed_fn(resp.status_code, body.decode("utf-8", "replace"))
            async for line in resp.aiter_lines():
                yield line


def _process_delta(
    ch: dict,
    *,
    content: list[str],
    reasoning_buf: list[str],
    tool_buf: dict[int, dict],
    id_to_idx: dict[str, int],
    last_idx: int | None,
) -> tuple[int | None, list[StreamChunk], str | None]:
    """Process one SSE choice delta.

    Returns ``(last_idx, chunks_to_yield, finish_reason)``. The caller yields
    the chunks and updates ``last_idx``. ``finish_reason`` is None when this
    delta has no finish.
    """
    chunks: list[StreamChunk] = []
    delta = ch.get("delta") or {}
    piece = delta.get("content")
    if piece:
        content.append(piece)
        chunks.append(StreamChunk(delta_text=piece))
    reasoning_piece = delta.get("reasoning_content")
    if reasoning_piece:
        reasoning_buf.append(reasoning_piece)
    new_last_idx = last_idx
    for tc in delta.get("tool_calls") or []:
        idx, args_frag = _accumulate_tool_call_fragment(
            tc,
            tool_buf=tool_buf,
            id_to_idx=id_to_idx,
            last_idx=new_last_idx,
        )
        new_last_idx = idx
        if args_frag:
            chunks.append(
                StreamChunk(
                    tool_name=tool_buf[idx]["name"],
                    tool_args_delta=args_frag,
                    tool_index=idx,
                )
            )
    finish = ch.get("finish_reason") or None
    return new_last_idx, chunks, finish


async def stream_complete(
    *,
    provider_name: str,
    base_url: str,
    payload: dict,
    headers: dict[str, str],
    req: CompletionRequest,
    model: str,
    client_factory: Callable[[], httpx.AsyncClient],
    raise_typed_fn: Callable[[int, str], None],
    tool_calls_fn: _ToolCallsFn,
    recovery_fn: _RecoveryFn,
    cached_tokens_fn: Callable[[dict], int],
    metadata_fn: Callable[..., dict],
    map_finish_fn: Callable[[str | None], FinishReason],
) -> AsyncIterator[StreamChunk]:
    """Stream chat-completions, assembling tool calls and yielding chunks.

    This is the exact streaming logic extracted from
    ``OpenAIProvider.stream_complete``. Never restarts after yielded bytes.
    """
    content: list[str] = [req.assistant_prefill] if req.assistant_prefill else []
    reasoning_buf: list[str] = []
    tool_buf: dict[int, dict] = {}
    last_idx: int | None = None
    id_to_idx: dict[str, int] = {}
    finish: str | None = None
    usage: dict = {}
    model_used = model
    try:
        async for line in _iter_stream_chunks(
            client_factory=client_factory,
            base_url=base_url,
            payload=payload,
            headers=headers,
            raise_typed_fn=raise_typed_fn,
        ):
            line_stripped = line.strip()
            if not line_stripped.startswith("data:"):
                continue
            data_str = line_stripped[len("data:") :].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except (json.JSONDecodeError, ValueError):
                continue
            model_used = chunk.get("model", model_used)
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if not choices:
                continue
            last_idx, delta_chunks, delta_finish = _process_delta(
                choices[0],
                content=content,
                reasoning_buf=reasoning_buf,
                tool_buf=tool_buf,
                id_to_idx=id_to_idx,
                last_idx=last_idx,
            )
            for dc in delta_chunks:
                yield dc
            if delta_finish:
                finish = delta_finish
    except httpx.TimeoutException as exc:
        raise LLMTransientError(
            f"request timed out ({type(exc).__name__})", provider=provider_name
        ) from exc
    except httpx.HTTPError as exc:
        raise LLMTransientError(
            f"connection error ({type(exc).__name__})", provider=provider_name
        ) from exc

    accumulated_text = "".join(content)
    reasoning_content = "".join(reasoning_buf)
    final = _build_stream_final_response(
        accumulated_text=accumulated_text,
        reasoning_content=reasoning_content,
        tool_buf=tool_buf,
        usage=usage,
        finish=finish,
        model_used=model_used,
        req=req,
        tool_calls_fn=tool_calls_fn,
        recovery_fn=recovery_fn,
        cached_tokens_fn=cached_tokens_fn,
        metadata_fn=metadata_fn,
        map_finish_fn=map_finish_fn,
    )
    yield StreamChunk(done=True, final=final)
