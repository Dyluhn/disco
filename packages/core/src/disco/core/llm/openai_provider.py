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

import hashlib
import json
import logging
import re
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

_LOG = logging.getLogger("disco.llm.openai")


def _map_finish(raw: str | None) -> FinishReason:
    return _FINISH.get(raw or "stop", "stop")


def _is_anthropic(model: str) -> bool:
    """Whether a model id routes to Anthropic (direct or via OpenRouter), so we
    emit Claude-style `cache_control` breakpoints. Other providers get plain
    string content + `prompt_cache_key` only."""
    m = model.lower()
    return "claude" in m or m.startswith("anthropic/")


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


def _sanitize_tool_name(name: str) -> str:
    """Sanitize tool names for OpenAI boundary (dots are forbidden).
    Strip everything before the last dot and filter to [a-zA-Z0-9_-]."""
    if "." in name:
        name = name.split(".")[-1]
    return re.sub(r"[^a-zA-Z0-9_-]", "", name)


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
        content: str | list = m.content
        if getattr(m, "images", None):
            parts = [{"type": "text", "text": m.content}]
            for img_url in m.images:
                parts.append({"type": "image_url", "image_url": {"url": img_url}})
            content = parts

        role = m.role
        tool_call_id = getattr(m, "tool_call_id", None)

        # DEFECT-6: a role:"tool" message MUST have a tool_call_id on the wire.
        # If the engine emitted an AgentErrorEvent without one (isolated root cause),
        # downgrade to user role to avoid a 400 from the provider.
        if role == "tool" and not tool_call_id:
            role = "user"
            content = f"Tool error: {content}"

        d: dict = {"role": role, "content": content}
        if tool_call_id:
            d["tool_call_id"] = tool_call_id

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
                        "name": _sanitize_tool_name(tc.get("name") or ""),
                        "arguments": (
                            tc["arguments"]
                            if isinstance(tc.get("arguments"), str)
                            # Cluster 8: deterministic key order so an identical
                            # tool-call serializes byte-identically every turn —
                            # otherwise dict ordering drift silently breaks the
                            # KV-cache prefix.
                            else json.dumps(tc.get("arguments") or {}, sort_keys=True)
                        ),
                    },
                }
                for tc in tool_calls
            ]
        return d

    def _payload(self, req: CompletionRequest, model: str, *, stream: bool) -> dict:
        msgs = [self._message(m) for m in req.messages]
        # B9: Assistant prefill. Append as a trailing
        # assistant message; compatible servers (llama.cpp, vLLM, Anthropic)
        # will continue from here.
        if req.assistant_prefill:
            msgs.append({"role": "assistant", "content": req.assistant_prefill})

        body: dict = {
            "model": model,
            "messages": msgs,
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
                        "name": _sanitize_tool_name(t.name),
                        "description": t.description,
                        "parameters": t.parameters_schema,
                    },
                }
                for t in req.tools
            ]
        # Per-call override wins over the provider/model default (the answerer turns
        # thinking OFF so a reasoning model doesn't spend its whole budget thinking).
        et = req.enable_thinking if req.enable_thinking is not None else self._enable_thinking
        if et is not None:
            body["chat_template_kwargs"] = {"enable_thinking": et}
        if stream:
            body["stream_options"] = {"include_usage": True}
        # GAP H — prompt-cache routing. A stable hint derived from the IMMUTABLE
        # prefix (system prompt + tool surface) so OpenAI/OpenRouter keep prompt-cache
        # affinity across a conversation's turns instead of re-billing the whole
        # prefix each turn. Local llama.cpp prefix-caches the KV automatically and
        # ignores this — harmless. (Prefix byte-stability is already handled:
        # sort_keys on tool args + a single mode-stable prompt.)
        ckey = self._prompt_cache_key(req)
        if ckey:
            body["prompt_cache_key"] = ckey
        # Anthropic (Claude via OpenRouter): explicit cache breakpoints. The OpenAI
        # format has no cache_control, so Anthropic models need the system prompt +
        # tool surface marked as cacheable content blocks. Gated on the model id so
        # local/OpenAI payloads stay plain-string (they'd reject block-array content).
        if _is_anthropic(model):
            self._mark_anthropic_cache(body)
        return body

    @staticmethod
    def _prompt_cache_key(req: CompletionRequest) -> str | None:
        """A deterministic cache-routing key from the stable prefix: the system
        message(s) + the sorted tool names. Identical agent configuration → identical
        key → same prompt-cache bucket across turns (and across conversations that
        share the prefix, which is a cache WIN, not a leak — keys are opaque hashes)."""
        parts = [m.content or "" for m in req.messages if m.role == "system"]
        parts.extend(sorted(t.name for t in (req.tools or [])))
        if not parts:
            return None
        return "pmx-" + hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _mark_anthropic_cache(body: dict) -> None:
        """Add Anthropic `cache_control: ephemeral` breakpoints on the two big stable
        blocks — the system prompt and the last tool definition — so Claude caches
        the prefix. Converts the system message's string content into the single-
        text-block array Anthropic requires for the marker; non-system turns and
        OpenAI-shaped payloads are left untouched."""
        for m in body.get("messages", []):
            if m.get("role") == "system" and isinstance(m.get("content"), str):
                m["content"] = [
                    {
                        "type": "text",
                        "text": m["content"],
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                break  # one breakpoint at the end of the system prompt is enough
        tools = body.get("tools")
        if tools:
            # The tool surface is stable too — a breakpoint after the last tool caches it.
            tools[-1]["cache_control"] = {"type": "ephemeral"}

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

    @staticmethod
    def _cached_tokens(usage: dict) -> int:
        """Cluster 8: extract cached prompt tokens across provider shapes —
        OpenAI's `prompt_tokens_details.cached_tokens` and Anthropic's
        `cache_read_input_tokens`. 0 when not reported."""
        details = usage.get("prompt_tokens_details") or {}
        return int(
            details.get("cached_tokens", 0)
            or usage.get("cache_read_input_tokens", 0)
            or 0
        )

    def _to_response(self, req: CompletionRequest, model: str, data: dict) -> CompletionResponse:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        text = msg.get("content") or ""
        # B9: Completion is a continuation of the prefill.
        if req.assistant_prefill:
            text = req.assistant_prefill + text
        return CompletionResponse(
            text=text,
            tool_calls=self._tool_calls(msg.get("tool_calls"), tools=req.tools),
            usage=TokenUsage(
                input_tokens=int(usage.get("prompt_tokens", 0) or 0),
                output_tokens=int(usage.get("completion_tokens", 0) or 0),
                cost_usd=0.0,  # local models are free
                cached_tokens=self._cached_tokens(usage),
            ),
            finish_reason=_map_finish(choice.get("finish_reason")),
            model_used=data.get("model", model),
            request_id=req.request_id,
            routing=None,  # the router attaches the RoutingDecision (RT1)
        )

    @staticmethod
    def _repair_json(raw: str) -> str:
        """Rung 5: Mechanical JSON repair. Strip fences, fix trailing commas,
        escape control characters, and strip XML-like closing tags."""
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
            raw = raw.strip()

        # DEFECT E3: Strip XML-like closing tags that leak from some models (e.g. </parameter>)
        raw = re.sub(r"</?[a-zA-Z0-9_]+>$", "", raw).strip()

        # Structural fixes: trailing commas
        raw = re.sub(r",\s*([\]}])", r"\1", raw)

        # Escape raw control characters (0x00-0x1F) which are forbidden in JSON strings
        def _escape_ctrl(m):
            return f"\\u{ord(m.group(0)):04x}"
        return re.sub(r"[\x00-\x1f]", _escape_ctrl, raw)

    @staticmethod
    def _coerce_args(args: dict, schema: dict | None) -> dict:
        """Rung 5: Type-coercing validation. Cast strings to expected types."""
        if not schema or schema.get("type") != "object":
            return args
        properties = schema.get("properties", {})
        coerced = {}
        for k, v in args.items():
            prop = properties.get(k)
            if not prop:
                coerced[k] = v
                continue
            target = prop.get("type")
            if target == "integer" and isinstance(v, str):
                try:
                    coerced[k] = int(v)
                except ValueError:
                    coerced[k] = v
            elif target == "number" and isinstance(v, str):
                try:
                    coerced[k] = float(v)
                except ValueError:
                    coerced[k] = v
            elif target == "boolean" and isinstance(v, str):
                if v.lower() in ("true", "1", "yes"):
                    coerced[k] = True
                elif v.lower() in ("false", "0", "no"):
                    coerced[k] = False
                else:
                    coerced[k] = v
            else:
                coerced[k] = v
        return coerced

    @classmethod
    def _tool_calls(cls, raw: list | None, tools: list[ToolSpec] | None = None) -> list[ProposedToolCall]:
        out: list[ProposedToolCall] = []
        spec_map = {t.name: t for t in (tools or [])}
        # Also map by sanitized name for reverse lookup
        sanitized_map = {_sanitize_tool_name(t.name): t for t in (tools or [])}

        for tc in raw or []:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            
            # Sanitize echoed name (DEFECT-6: strip prefix/dots)
            clean_name = _sanitize_tool_name(name)
            
            args_raw = fn.get("arguments")
            parsed = {}
            if isinstance(args_raw, str):
                # Rung 5: (a) strict parse
                try:
                    parsed = json.loads(args_raw)
                except (json.JSONDecodeError, ValueError):
                    # Rung 5: (b) mechanical JSON repair
                    try:
                        repaired = cls._repair_json(args_raw)
                        parsed = json.loads(repaired)
                        _LOG.info(f"Repaired JSON for tool {clean_name}")
                    except Exception:
                        parsed = {"_raw": args_raw}
            else:
                parsed = args_raw or {}
            
            # Rung 5: (c) type-coercing validation
            # Find the spec. Match original or sanitized name.
            spec = spec_map.get(name) or sanitized_map.get(clean_name)
            if spec:
                parsed = cls._coerce_args(parsed, spec.parameters_schema)
                # Use the original canonical name from the spec
                final_name = spec.name
            else:
                # Unknown tool — keep the cleaned name for the reroute path (Rung 7)
                final_name = clean_name

            out.append(
                ProposedToolCall(
                    tool_name=final_name, arguments=parsed, provider_call_id=tc.get("id")
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
        content: list[str] = [req.assistant_prefill] if req.assistant_prefill else []
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
                            idx = tc.get("index", 0)
                            slot = tool_buf.setdefault(
                                idx, {"id": None, "name": "", "args": ""}
                            )
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["args"] += fn["arguments"]
                                # Surface the tool-call arg fragment so consumers can
                                # watch the file body assemble live (watch-it-write).
                                yield StreamChunk(
                                    tool_name=slot["name"],
                                    tool_args_delta=fn["arguments"],
                                    tool_index=idx,
                                )
                        if ch.get("finish_reason"):
                            finish = ch["finish_reason"]
        except httpx.TimeoutException as exc:
            raise LLMTransientError(f"request timed out: {exc}", provider=self.name) from exc
        except httpx.HTTPError as exc:
            raise LLMTransientError(f"connection error: {exc}", provider=self.name) from exc

        tool_calls: list[ProposedToolCall] = self._tool_calls(
            [
                {
                    "id": slot["id"],
                    "function": {"name": slot["name"], "arguments": slot["args"]},
                }
                for slot in tool_buf.values()
            ],
            tools=req.tools,
        )
        final = CompletionResponse(
            text="".join(content),
            tool_calls=tool_calls,
            usage=TokenUsage(
                input_tokens=int(usage.get("prompt_tokens", 0) or 0),
                output_tokens=int(usage.get("completion_tokens", 0) or 0),
                cost_usd=0.0,
                cached_tokens=self._cached_tokens(usage),
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
