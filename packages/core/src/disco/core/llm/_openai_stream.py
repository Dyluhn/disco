"""OpenAI-compatible streaming response assembly.

Extracted from ``openai_provider.py`` so the provider class stays a thin facade.
This module owns the SSE stream-decoding, tool-call fragment accumulation, and
weak-model recovery for the streaming path. Never restarts after yielded bytes.

Both provider entry points ride this path: ``stream_complete`` yields chunks to
the caller, and ``complete`` drives the same accumulation and returns only the
final buffered result. Timeouts are progress-based (see ``_openai_timeouts``) —
there is no total wall-clock ceiling on a streamed generation.

This is also the only layer that can see a model still producing, so it is where
the progress heartbeat is published (``stream_progress``). Chunks that arrive
report; a quiet stream reports nothing.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable

import httpx

from ._openai_timeouts import ProviderTimeouts, iter_with_progress_timeout
from ._request_assembly import FinishReason
from .errors import LLMTransientError
from .stream_progress import reporter_for
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

# Immutable, so one shared instance is safe as the signature default.
_DEFAULT_TIMEOUTS = ProviderTimeouts()


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


def _buffered_body_as_stream_line(body: bytes) -> str | None:
    """Re-shape a whole buffered chat-completions body as one SSE data line.

    Some OpenAI-compatible servers accept ``stream: true`` and answer with a
    single ``application/json`` object anyway. Parsing that as SSE finds no
    ``data:`` lines and yields an EMPTY answer — an infrastructure failure
    wearing a model failure's clothes, which is exactly what this adapter
    exists to prevent. Converting ``message`` to ``delta`` puts the body
    through the same accumulators, so the caller cannot tell the difference.

    Returns None when the body is not a decodable chat-completions object, so
    the caller falls back to normal SSE handling rather than inventing content.
    """
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("choices"), list):
        return None
    choices: list = data["choices"]
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message")
    message = message if isinstance(message, dict) else {}
    raw_tool_calls = message.get("tool_calls")
    delta: dict = {
        "content": message.get("content"),
        "reasoning_content": message.get("reasoning_content"),
    }
    if isinstance(raw_tool_calls, list):
        delta["tool_calls"] = [
            {**tc, "index": tc.get("index", position)}
            for position, tc in enumerate(raw_tool_calls)
            if isinstance(tc, dict)
        ]
    reshaped: dict = {
        "choices": [{"delta": delta, "finish_reason": choice.get("finish_reason")}],
        "usage": data.get("usage") or {},
    }
    if isinstance(data.get("model"), str):
        # Absent stays absent: an explicit null would overwrite the caller's
        # model id with None on the way to the response.
        reshaped["model"] = data["model"]
    return f"data: {json.dumps(reshaped)}"


def _is_buffered_json(resp: httpx.Response) -> bool:
    """Whether the server answered a streamed request with a whole JSON body."""
    return "application/json" in resp.headers.get("content-type", "").lower()


async def _iter_stream_chunks(
    *,
    client_factory: Callable[[], httpx.AsyncClient],
    base_url: str,
    payload: dict,
    headers: dict[str, str],
    raise_typed_fn: Callable[[int, str], None],
    provider_name: str,
    timeouts: ProviderTimeouts,
) -> AsyncIterator[str]:
    """Open the streaming connection and yield raw SSE lines.

    The non-2xx classification happens BEFORE any line is read, so a server that
    refuses the streamed request is always an immediate, un-yielded failure.
    Line delivery is then bounded by progress, never by total duration.
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
            if _is_buffered_json(resp):
                reshaped = _buffered_body_as_stream_line(await resp.aread())
                if reshaped is not None:
                    yield reshaped
                    # A complete buffered JSON envelope supplies the transport
                    # boundary even when its optional finish metadata is absent.
                    yield "data: [DONE]"
                    return
            async for line in iter_with_progress_timeout(
                resp.aiter_lines(),
                provider_name=provider_name,
                first_chunk_s=timeouts.first_chunk_s,
                idle_s=timeouts.idle_s,
            ):
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


def _require_stream_terminal(finish: str | None, saw_done: bool, provider_name: str) -> None:
    """Reject an unmarked EOF before reporting a successful completion."""
    if finish is None and not saw_done:
        raise LLMTransientError(
            "stream ended without a finish reason or completion marker", provider=provider_name
        )


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
    timeouts: ProviderTimeouts = _DEFAULT_TIMEOUTS,
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
    saw_done = False
    usage: dict = {}
    model_used = model
    # Progress reporting starts BEFORE the connection opens, so the first
    # report's `seconds` includes the prefill the caller actually waited out.
    # None whenever nobody installed an observer, which is every ordinary
    # completion in the product.
    reporter = reporter_for(req.metadata)
    try:
        async for line in _iter_stream_chunks(
            client_factory=client_factory,
            base_url=base_url,
            payload=payload,
            headers=headers,
            raise_typed_fn=raise_typed_fn,
            provider_name=provider_name,
            timeouts=timeouts,
        ):
            line_stripped = line.strip()
            if not line_stripped.startswith("data:"):
                continue
            data_str = line_stripped[len("data:") :].strip()
            if data_str == "[DONE]":
                saw_done = True
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
            seen = (len(content), len(reasoning_buf))
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
            if reporter is not None:
                await reporter.observed(len(content) - seen[0], len(reasoning_buf) - seen[1])
            # The LAST chunk carrying a NON-NULL finish_reason wins — not the
            # last chunk. llama.cpp sends the finish on the second-to-last data
            # chunk and then a usage-only chunk with no choices, so reading
            # "the final chunk" would drop it. A stream that never sends one
            # stays None here and needs a separate [DONE] boundary. EOF
            # alone does not establish that the generation completed.
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

    _require_stream_terminal(finish, saw_done, provider_name)

    # The close report is deliberately NOT in a `finally`: a stream that died
    # mid-flight becomes a typed error the router handles, and the last
    # heartbeat's silence is the honest account of what the host saw.
    if reporter is not None:
        await reporter.close()
    final = _build_stream_final_response(
        accumulated_text="".join(content),
        reasoning_content="".join(reasoning_buf),
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
