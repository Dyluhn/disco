"""OpenAI-compatible ModelProvider — the real HTTP adapter (llm-router §3).

ONE client for any OpenAI chat-completions backend, parameterized by `base_url` +
`model` + optional `api_key`. Both local servers (llama.cpp Qwen, Gemma) and a
future OpenRouter speak this, so the contract's notional `ollama`/`openrouter`
providers collapse into this single client pointed at different base URLs.

Reasoning models (e.g. Qwen3.6): the ANSWER is `message.content`;
`reasoning_content` (the thinking) is parsed separately and not treated as answer
text. `enable_thinking` toggles it where the server honors `chat_template_kwargs`
(llama.cpp does) — leave it None to use the server default.

Errors map to the typed hierarchy (errors.py §6), PRESERVING the provider's real
message so it surfaces cleanly (the reactive-error rule). The context-window error
is classified to `LLMContextWindowExceeded` — what the condenser's hard reset and
the loop's reactive surfacing depend on.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from typing import Literal

import httpx

from ..events import LLMMessage
from .errors import (
    LLMAuthError,
    LLMContentFiltered,
    LLMContextWindowExceeded,
    LLMError,
    LLMTransientError,
)
from .types import (
    CompletionRequest,
    CompletionResponse,
    ProposedToolCall,
    Requirement,
    StreamChunk,
    TokenUsage,
)

FinishReason = Literal["stop", "length", "tool_calls", "content_filter", "error"]

_FINISH: dict[str, FinishReason] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "content_filter": "content_filter",
}


def _map_finish(raw: str | None) -> FinishReason:
    return _FINISH.get(raw or "stop", "stop")


def _is_context_overflow(err_type: str, message: str) -> bool:
    """Classify the OpenAI-compatible server's context-length error. llama.cpp
    uses type `exceed_context_size_error`; vLLM/others phrase it in the message
    (`maximum context length`, `context_length_exceeded`). Match both."""
    t = err_type.lower()
    m = message.lower()
    return (
        "exceed_context_size" in t
        or "context_length_exceeded" in t
        or "context size" in m
        or "context length" in m
        or "maximum context" in m
        or "exceeds the available context" in m
    )


class OpenAIProvider:
    """[CONTRACT — ModelProvider] A real OpenAI chat-completions backend."""

    def __init__(
        self,
        base_url: str,
        *,
        name: str = "openai",
        api_key: str | None = None,
        timeout_s: float = 180.0,
        enable_thinking: bool | None = None,
        capabilities: Iterable[Requirement] = (),
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self.name = name
        self._key = api_key
        self._timeout = timeout_s
        self._enable_thinking = enable_thinking
        self._caps = frozenset(capabilities)
        self._transport = transport  # test seam (httpx.MockTransport); None = real

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport)

    # -- request shaping ------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {"content-type": "application/json"}
        if self._key:
            h["Authorization"] = f"Bearer {self._key}"
        return h

    @staticmethod
    def _message(m: LLMMessage) -> dict:
        d: dict = {"role": m.role, "content": m.content}
        if getattr(m, "tool_call_id", None):
            d["tool_call_id"] = m.tool_call_id
        tool_calls = m.tool_calls
        if tool_calls:
            # The event layer's internal tool_calls shape is {id, name, arguments(dict)}
            # (ActionEvent.to_llm_message). The OpenAI/llama-server wire format an
            # assistant turn must echo back is {id, type:"function", function:{name,
            # arguments:<JSON STRING>}} — without this conversion the server rejects the
            # next turn ("Failed to parse messages"). The Agent surface is the first to
            # send assistant tool-calls back (Research never does), so it surfaces here.
            d["tool_calls"] = [
                {
                    "id": tc.get("id"),
                    "type": "function",
                    "function": {
                        "name": tc.get("name"),
                        "arguments": (
                            tc["arguments"]
                            if isinstance(tc.get("arguments"), str)
                            else json.dumps(tc.get("arguments") or {})
                        ),
                    },
                }
                for tc in tool_calls
            ]
        return d

    def _payload(self, req: CompletionRequest, model: str, *, stream: bool) -> dict:
        body: dict = {
            "model": model,
            "messages": [self._message(m) for m in req.messages],
            "temperature": req.temperature,
            "stream": stream,
        }
        if req.max_tokens is not None:
            body["max_tokens"] = req.max_tokens
        if req.response_format == "json":
            body["response_format"] = {"type": "json_object"}
        if req.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters_schema,
                    },
                }
                for t in req.tools
            ]
        if self._enable_thinking is not None:
            body["chat_template_kwargs"] = {"enable_thinking": self._enable_thinking}
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    # -- error mapping (preserve the real message) ----------------------------

    def _raise_typed(self, status: int, body_text: str) -> None:
        message, err_type = body_text, ""
        try:
            j = json.loads(body_text)
            err = j.get("error", j) if isinstance(j, dict) else {}
            if isinstance(err, dict):
                message = err.get("message") or body_text
                err_type = err.get("type") or ""
        except (json.JSONDecodeError, ValueError):
            pass
        message = message.strip() or f"HTTP {status}"
        if _is_context_overflow(err_type, message):
            raise LLMContextWindowExceeded(message, provider=self.name)
        if status in (401, 403) or "auth" in err_type.lower():
            raise LLMAuthError(message, provider=self.name)
        if "content_filter" in err_type.lower() or status == 451:
            raise LLMContentFiltered(message, provider=self.name)
        if status == 429 or status >= 500:
            raise LLMTransientError(message, provider=self.name)
        raise LLMError(message, provider=self.name)

    # -- response shaping -----------------------------------------------------

    def _to_response(self, req: CompletionRequest, model: str, data: dict) -> CompletionResponse:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        return CompletionResponse(
            text=msg.get("content") or "",  # reasoning_content (thinking) is NOT the answer
            tool_calls=self._tool_calls(msg.get("tool_calls")),
            usage=TokenUsage(
                input_tokens=int(usage.get("prompt_tokens", 0) or 0),
                output_tokens=int(usage.get("completion_tokens", 0) or 0),
                cost_usd=0.0,  # local models are free
            ),
            finish_reason=_map_finish(choice.get("finish_reason")),
            model_used=data.get("model", model),
            request_id=req.request_id,
            routing=None,  # the router attaches the RoutingDecision (RT1)
        )

    @staticmethod
    def _tool_calls(raw: list | None) -> list[ProposedToolCall]:
        out: list[ProposedToolCall] = []
        for tc in raw or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            try:
                parsed = json.loads(args) if isinstance(args, str) else (args or {})
            except (json.JSONDecodeError, ValueError):
                parsed = {"_raw": args}
            out.append(
                ProposedToolCall(
                    tool_name=fn.get("name", ""), arguments=parsed, provider_call_id=tc.get("id")
                )
            )
        return out

    # -- the ModelProvider protocol -------------------------------------------

    async def complete(self, req: CompletionRequest, *, model: str) -> CompletionResponse:
        try:
            async with self._client() as client:
                resp = await client.post(
                    f"{self._base}/chat/completions",
                    json=self._payload(req, model, stream=False),
                    headers=self._headers(),
                )
        except httpx.TimeoutException as exc:
            raise LLMTransientError(f"request timed out: {exc}", provider=self.name) from exc
        except httpx.HTTPError as exc:
            raise LLMTransientError(f"connection error: {exc}", provider=self.name) from exc
        if resp.status_code >= 400:
            self._raise_typed(resp.status_code, resp.text)
        return self._to_response(req, model, resp.json())

    async def stream_complete(
        self, req: CompletionRequest, *, model: str
    ) -> AsyncIterator[StreamChunk]:
        content: list[str] = []
        tool_buf: dict[int, dict] = {}
        finish: str | None = None
        usage: dict = {}
        model_used = model
        try:
            async with self._client() as client:
                async with client.stream(
                    "POST",
                    f"{self._base}/chat/completions",
                    json=self._payload(req, model, stream=True),
                    headers=self._headers(),
                ) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        self._raise_typed(resp.status_code, body.decode("utf-8", "replace"))
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:") :].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        model_used = chunk.get("model", model_used)
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        ch = choices[0]
                        delta = ch.get("delta") or {}
                        piece = delta.get("content")  # answer tokens only (not reasoning)
                        if piece:
                            content.append(piece)
                            yield StreamChunk(delta_text=piece)
                        for tc in delta.get("tool_calls") or []:
                            slot = tool_buf.setdefault(
                                tc.get("index", 0), {"id": None, "name": "", "args": ""}
                            )
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["args"] += fn["arguments"]
                        if ch.get("finish_reason"):
                            finish = ch["finish_reason"]
        except httpx.TimeoutException as exc:
            raise LLMTransientError(f"request timed out: {exc}", provider=self.name) from exc
        except httpx.HTTPError as exc:
            raise LLMTransientError(f"connection error: {exc}", provider=self.name) from exc

        tool_calls: list[ProposedToolCall] = []
        for slot in tool_buf.values():
            try:
                args = json.loads(slot["args"]) if slot["args"] else {}
            except (json.JSONDecodeError, ValueError):
                args = {"_raw": slot["args"]}
            tool_calls.append(
                ProposedToolCall(
                    tool_name=slot["name"], arguments=args, provider_call_id=slot["id"]
                )
            )
        final = CompletionResponse(
            text="".join(content),
            tool_calls=tool_calls,
            usage=TokenUsage(
                input_tokens=int(usage.get("prompt_tokens", 0) or 0),
                output_tokens=int(usage.get("completion_tokens", 0) or 0),
                cost_usd=0.0,
            ),
            finish_reason=_map_finish(finish),
            model_used=model_used,
            request_id=req.request_id,
            routing=None,
        )
        yield StreamChunk(done=True, final=final)

    def supports(self, requirement: Requirement, *, model: str) -> bool:
        # Advisory metadata (router §8 / security principle 8): informs assignment,
        # does not gate. The provider sends the request as assigned regardless.
        return requirement in self._caps
