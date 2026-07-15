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
from typing import Any, Literal

import httpx

from ..events import WORKSPACE_SNAPSHOT_SENTINEL, LLMMessage
from .errors import (
    LLMAuthError,
    LLMContentFiltered,
    LLMContextWindowExceeded,
    LLMError,
    LLMProviderUnavailable,
    LLMTransientError,
)
from .provider_ledger import emit_provider_attempt
from .toolcall_recovery import recover_tool_calls
from .types import (
    EMPTY_REASONING_ONLY_METADATA_KEY,
    CompletionRequest,
    CompletionResponse,
    ProposedToolCall,
    Requirement,
    StreamChunk,
    TokenUsage,
    ToolSpec,
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
_DISCO_CONVERSATION_HEADER = "X-Disco-Conversation"


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


# ---- F5: GATED thinking-budget (assist tier only) -------------------------
# Prior art: SmallCode F5 (thinking_budget.js). When the assist gate is ON, a
# reasoning model can dump a very long `<think>` block into its prior turn's
# content. If that block survives into the next request it eats the context
# budget for the model to actually ANSWER. F5 does two things, BOTH gated on
# `req.assist`:
#
#   (a) EMERGENCY HEAD+TAIL TRUNCATE — when an assistant message contains a
#       `<think>...</think>` block whose inner reasoning is over the budget,
#       keep the head (the model's plan) and the tail (the model's
#       conclusion), drop the verbose middle, and emit a recoverable marker.
#       Under-budget blocks and messages without a closed `<think>` block are
#       passed through byte-identically.
#
#   (b) DISABLE THINKING ON REPAIR ATTEMPT ≥ 2 — when the engine retries a
#       call (the inner `attempts` counter in engine.py's driver loop is > 0),
#       force `chat_template_kwargs.enable_thinking=False` so a failed call
#       does NOT burn its whole budget re-thinking. This is the SmallCode
#       `disable-repair` path.
#
# Assist OFF (the capable-model default) → both are skipped, the payload is
# byte-identical to today. Per SmallCode, injecting `enable_thinking` on some
# small models (e.g. lfm2) also suppresses tool-calls — the gate keeps the
# capable-model path free of that risk.

# Threshold above which a `<think>` block is over-budget and gets truncated.
# 2,000 chars ~ 500 tokens; well below a 4K context but enough that a normal
# reasoning pass is preserved. Tunable here (single source of truth).
_F5_THINK_BUDGET_CHARS = 2_000
# When truncating, keep the first 1,000 chars (where the model states its
# plan) and the last 500 chars (where it concludes). The middle is dropped.
_F5_THINK_BUDGET_HEAD = 1_000
_F5_THINK_BUDGET_TAIL = 500

# Match a `<think>...</think>` block. Non-greedy so consecutive blocks (rare,
# but possible) each get handled independently. `re.DOTALL` so the inner
# content can include newlines. Requires BOTH opening and closing tags — an
# unclosed `<think>` (model ran out of tokens) is passed through unchanged;
# the caller will see the truncation in `finish_reason: length` and the
# condenser handles it via the existing path.
_F5_THINK_BLOCK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


def _truncate_think_block(content: str) -> str:
    """F5(a): head+tail truncate any over-budget `<think>` block in `content`.

    Returns `content` unchanged when no closed block is present, or when the
    block is within budget. Truncation preserves head + tail + a recoverable
    marker so the model (or the engine) can recognize what was dropped. Pure
    + deterministic (same input → same output) so the request payload stays
    cache-stable across retries."""

    def _maybe(m: re.Match) -> str:
        inner = m.group(1)
        if len(inner) <= _F5_THINK_BUDGET_CHARS:
            return m.group(0)  # under budget — leave the block intact
        head = inner[:_F5_THINK_BUDGET_HEAD]
        tail = inner[-_F5_THINK_BUDGET_TAIL:]
        dropped = len(inner) - _F5_THINK_BUDGET_HEAD - _F5_THINK_BUDGET_TAIL
        return (
            f"<think>{head}"
            f"\n…[F5 truncated {dropped:,} chars of reasoning; "
            f"head (plan) above, tail (conclusion) below]…\n"
            f"{tail}</think>"
        )

    return _F5_THINK_BLOCK_RE.sub(_maybe, content)


def _host_speaks_chat_template_kwargs(base_url: str) -> bool:
    """`chat_template_kwargs` is a llama.cpp / vLLM SERVER extension, not part of
    the OpenAI schema. Lenient clouds ignore it, but strict ones (e.g. Fireworks
    behind an aggregator) reject the whole request: HTTP 400 "Extra inputs are
    not permitted" — one hidden field bricks the model. Send it only to hosts
    that plausibly run a self-hosted server: loopback / RFC-1918 / CGNAT
    (tailscale) IPs, dot-less LAN hostnames, or explicitly local suffixes."""
    import ipaddress
    from urllib.parse import urlsplit

    host = (urlsplit(base_url).hostname or "").lower()
    if not host:
        return False
    if host.endswith((".ts.net", ".local", ".lan", ".home.arpa", ".internal")):
        return True
    if "." not in host:  # bare intranet hostname (e.g. "blackbox")
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10")


def _requires_user_after_tool_result(base_url: str) -> bool:
    """Whether this compatibility endpoint rejects a trailing ``tool`` turn.

    OpenCode Go currently routes some models through a strict Fireworks chat
    template which returns HTTP 400 when the request ends immediately after a
    tool result. The same history succeeds once followed by a user continuation.
    Keep the shim pinned to that endpoint so conforming OpenAI-compatible hosts
    retain their byte-identical payloads.
    """
    from urllib.parse import urlsplit

    parsed = urlsplit(base_url)
    return (parsed.hostname or "").lower() == "opencode.ai" and parsed.path.rstrip("/").endswith(
        "/zen/go/v1"
    )


def _sanitize_tool_name(name: str) -> str:
    """Sanitize tool names for OpenAI boundary (dots are forbidden).
    Strip everything before the last dot and filter to [a-zA-Z0-9_-]."""
    if "." in name:
        name = name.split(".")[-1]
    return re.sub(r"[^a-zA-Z0-9_-]", "", name)


def _normalize_tool_call_ordering(msgs: list[dict]) -> list[dict]:
    """Enforce the strict tool-call/result adjacency the wire format requires.

    MiniMax (and, per the OpenAI spec, every compliant provider) rejects a
    message list where a ``role:"tool"`` result does not IMMEDIATELY follow the
    assistant message whose ``tool_calls`` declared its ``tool_call_id`` —
    observed live as ``bad_request_error`` 2013, "tool call result does not
    follow tool call". Our render pipeline (``view_render``) runs many history
    transforms — microcompact tombstoning, context-pack insertion at index 1,
    model-summarization condensation, prefix/tail snapshot injection, the
    DR→agent workflow handoff — any of which can drop a turn or splice a message
    between an assistant tool-call and its result, leaving the pair non-adjacent
    (or the result orphaned). Lenient servers (llama.cpp, vLLM) tolerate it;
    MiniMax does not. This is a harness-side wire-assembly defect, NOT a model
    failure, so the repair belongs here at the serialization boundary — the
    single choke point that owns the wire contract.

    The pass rebuilds the list so that:
      * each assistant ``tool_calls`` is immediately followed by its result
        messages, in the SAME order the calls were declared;
      * a declared tool_call with no result anywhere gets a minimal stub result
        (a dropped/condensed observation — the model continues; it does not 400);
      * an orphan ``role:"tool"`` result (its declaring assistant turn was
        dropped) is removed.

    It is a NO-OP (identical output ordering) when the list already satisfies the
    invariant, so the cache-stable capable-model prefix is untouched on the
    healthy path.
    """
    result_by_id: dict[str, dict] = {}
    for m in msgs:
        if m.get("role") == "tool":
            cid = m.get("tool_call_id")
            if cid is not None and cid not in result_by_id:
                result_by_id[cid] = m

    out: list[dict] = []
    for m in msgs:
        if m.get("role") == "tool":
            # Re-emitted (in declaration order) right after its assistant turn
            # below — skip the free-standing occurrence. An orphan result (no
            # declaring assistant) is simply never re-emitted → dropped.
            continue
        out.append(m)
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                cid = tc.get("id")
                if cid is None:
                    continue
                res = result_by_id.get(cid)
                if res is not None:
                    out.append(res)
                else:
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": cid,
                            "content": "[tool result unavailable — omitted from context]",
                        }
                    )
    return out


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
        self._speaks_ctk = _host_speaks_chat_template_kwargs(base_url)
        self._caps = frozenset(capabilities)
        self._transport = transport  # test seam (httpx.MockTransport); None = real

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout,
            transport=self._transport,
            trust_env=False,
            follow_redirects=False,
        )

    def _is_openrouter(self) -> bool:
        """True when this provider routes through OpenRouter. Reuses the same
        idiom as runtime_settings.py:155 (_is_small_assist_default). Both
        self._base (base_url) and self.name are checked so an operator who
        names a provider "openrouter-free" without changing the base_url still
        gets the block — and vice versa."""
        return "openrouter" in self._base.lower() or "openrouter" in self.name.lower()

    # -- request shaping ------------------------------------------------------

    def _headers(self, req: CompletionRequest | None = None) -> dict[str, str]:
        h = {"content-type": "application/json"}
        if self._key:
            h["Authorization"] = f"Bearer {self._key}"
        raw_cid = (req.metadata or {}).get("conversation_id") if req is not None else None
        cid = str(raw_cid).strip() if raw_cid is not None else ""
        if cid:
            h[_DISCO_CONVERSATION_HEADER] = cid
        return h

    @staticmethod
    def _message(m: LLMMessage) -> dict:
        content: str | list = m.content
        if m.images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": m.content}]
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
        # F5(a): GATED think-block truncation. When the assist gate is ON, scan
        # every message for an over-long `<think>...</think>` block and head+tail
        # truncate it. A frozen LLMMessage requires a `model_copy(update=...)` to
        # mutate, so the transform may rebuild a subset of the message list.
        # When `req.assist` is OFF (the capable-model default) F5's head+tail
        # truncation is skipped.
        #
        # ALL-TIERS think-history strip (reasoning-model context poisoning):
        # INDEPENDENT of `req.assist`, ALWAYS fully strip CLOSED
        # `<think>...</think>` blocks from ASSISTANT-ROLE history (keeping the
        # text before `<think>` and the answer after `</think>`). A reasoning
        # model running as the CAPABLE driver (assist=False) emits its chain of
        # thought inline in `content`; re-feeding it verbatim every turn poisons
        # the growing context until the model degenerates to tool-less turns and
        # the actionless valve pauses the build (live MiniMax-M3 build, 2026-06).
        # Why this is safe:
        #   * ASSISTANT-ROLE ONLY — user/tool/environment/system content can
        #     legitimately contain a literal `<think>` (task data, a tool
        #     observation echoing a model's output); stripping those would
        #     corrupt data.
        #   * Reuses `_F5_THINK_BLOCK_RE` (requires BOTH tags) so an UNCLOSED
        #     `<think>` from a W-31 truncated turn is PRESERVED.
        #   * Byte-identical guarantee: an assistant message with no closed
        #     `<think>` block is returned unchanged (the regex sub is a no-op).
        #   * Tool-call args arrive via structured `tool_calls`, separate from
        #     `content`, so they are never touched here.
        # The raw reasoning is preserved in the event log/audit — this strip is
        # render-time only (not persisted before storage).
        def _shape(m: LLMMessage) -> LLMMessage:
            content = m.content
            if m.role == "assistant" and "<think>" in content and "</think>" in content:
                content = _F5_THINK_BLOCK_RE.sub("", content)
            if req.assist and "<think>" in content and "</think>" in content:
                content = _truncate_think_block(content)
            if content is m.content:
                return m
            return m.model_copy(update={"content": content})

        source_messages = [_shape(m) for m in req.messages]
        msgs = [self._message(m) for m in source_messages]
        # Provider wire-contract guard: guarantee every tool result immediately
        # follows the assistant tool_call that declared it (MiniMax 2013; OpenAI
        # spec). No-op when the render pipeline already produced an adjacent list.
        msgs = _normalize_tool_call_ordering(msgs)
        # Live reliability runs exposed a deterministic 400/requery pair after
        # every tool on OpenCode Go: that endpoint's strict template requires a
        # user turn after the tool result. Previously the generic repair path
        # appended an alarming "provider rejected" prompt and paid for a second
        # request. Add the neutral continuation on the first request instead.
        if _requires_user_after_tool_result(self._base) and msgs and msgs[-1].get("role") == "tool":
            msgs.append(
                {
                    "role": "user",
                    "content": "Continue from the tool result above.",
                }
            )
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
        # P1 — OpenRouter provider routing preference injection. The `provider`
        # block steers OpenRouter away from upstreams that don't support tool
        # calls (e.g. free Chutes tier) and enables fallback on transient
        # upstream failures. GATED on _is_openrouter() so local/OpenAI payloads
        # are byte-identical to today (non-OR servers reject unknown top-level
        # keys). Caller-supplied req.provider_prefs extends/overrides the floor.
        if self._is_openrouter():
            body["provider"] = {
                "require_parameters": True,
                "allow_fallbacks": True,
                **(req.provider_prefs or {}),
            }
        # Per-call override wins over the provider/model default (the answerer turns
        # thinking OFF so a reasoning model doesn't spend its whole budget thinking).
        et = req.enable_thinking if req.enable_thinking is not None else self._enable_thinking
        # F5(b): GATED disable-thinking on a repair attempt ≥ 2. When the engine
        # retries a call (`req.attempt >= 2` AND `req.assist` is ON), force
        # `enable_thinking=False` so a failed call does NOT re-burn its whole
        # budget on thinking. This is the SmallCode `disable-repair` path and
        # overrides both the per-call `req.enable_thinking` and the provider
        # default — the goal is to GUARANTEE thinking is off on a repair.
        # Per SmallCode: injecting `enable_thinking` on some small models also
        # suppresses tool-calls — the assist-OFF gate (above) keeps the
        # capable-model path free of that risk.
        if req.assist and (req.attempt or 1) >= 2:
            et = False
        if et is not None and self._speaks_ctk:
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
        """Add Anthropic `cache_control: ephemeral` breakpoints on the big stable
        prefix blocks so Claude caches them — the system prompt, the last tool
        definition, and (CW-3) the pinned CURRENT WORKSPACE snapshot when it sits in
        the cacheable prefix. Converts a string content into the single-text-block
        array Anthropic requires for the marker; non-marked turns and OpenAI-shaped
        payloads are left untouched."""
        msgs = body.get("messages", [])
        sys_idx: int | None = None
        for i, m in enumerate(msgs):
            if m.get("role") == "system" and isinstance(m.get("content"), str):
                m["content"] = [
                    {
                        "type": "text",
                        "text": m["content"],
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                sys_idx = i
                break  # one breakpoint at the end of the system prompt is enough
        # CW-3 — the pinned workspace block is positioned in the PREFIX (immediately
        # after the system prompt) for capable models, so a breakpoint AFTER it caches
        # the system+tools+workspace prefix as a unit. GATED on the block actually
        # being in that prefix slot: assist-ON keeps it in the volatile TAIL (not at
        # sys_idx+1) → not marked → byte-identical to today.
        if sys_idx is not None and sys_idx + 1 < len(msgs):
            nxt = msgs[sys_idx + 1]
            c = nxt.get("content")
            if (
                nxt.get("role") == "user"
                and isinstance(c, str)
                and c.startswith(WORKSPACE_SNAPSHOT_SENTINEL)
            ):
                nxt["content"] = [
                    {
                        "type": "text",
                        "text": c,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
        tools = body.get("tools")
        if tools:
            # The tool surface is stable too — a breakpoint after the last tool caches it.
            tools[-1]["cache_control"] = {"type": "ephemeral"}

    # -- error mapping (classify on raw body, raise only a safe summary) -------

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
        safe_message = self._safe_provider_error(status, err_type)
        if _is_context_overflow(err_type, message):
            raise LLMContextWindowExceeded(safe_message, provider=self.name)
        if status in (401, 403) or "auth" in err_type.lower():
            raise LLMAuthError(safe_message, provider=self.name)
        if "content_filter" in err_type.lower() or status == 451:
            raise LLMContentFiltered(safe_message, provider=self.name)
        if status == 429 or status >= 500:
            raise LLMTransientError(safe_message, provider=self.name)
        # P2 — provider-availability rejection classification. These messages
        # are upstream routing failures (Chutes/OpenRouter), NOT model errors:
        # the model payload is fine; an upstream provider is unavailable or
        # excluded by routing rules. Raising LLMProviderUnavailable (a
        # LLMTransientError subclass) lets the driver escalate provider_prefs
        # (routing retry) instead of blaming the model ("check your JSON").
        _msg_lower = message.lower()
        _type_lower = err_type.lower()
        _provider_phrases = (
            "no cookie auth",
            "no allowed providers",
            "no instances available",
            "no endpoints found",
            "provider returned error",
            "requires moderation",
        )
        if any(p in _msg_lower for p in _provider_phrases) or _type_lower.startswith("provider"):
            raise LLMProviderUnavailable(safe_message, provider=self.name)
        raise LLMError(safe_message, provider=self.name)

    def _safe_provider_error(self, status: int, err_type: str) -> str:
        typ = "".join(ch for ch in (err_type or "") if ch.isalnum() or ch in {"_", "-", "."})
        suffix = f" type={typ[:80]}" if typ else ""
        return f"provider {self.name} returned HTTP {status}{suffix}"

    # -- response shaping -----------------------------------------------------

    @staticmethod
    def _cached_tokens(usage: dict) -> int:
        """Cluster 8: extract cached prompt tokens across provider shapes —
        OpenAI's `prompt_tokens_details.cached_tokens` and Anthropic's
        `cache_read_input_tokens`. 0 when not reported."""
        details = usage.get("prompt_tokens_details") or {}
        return int(details.get("cached_tokens", 0) or usage.get("cache_read_input_tokens", 0) or 0)

    @staticmethod
    def _empty_reasoning_only_metadata(
        *,
        finish_reason: FinishReason,
        content_len: int,
        reasoning_len: int,
        tool_call_count: int,
    ) -> dict:
        if finish_reason == "stop" and content_len == 0 and tool_call_count == 0:
            return {
                EMPTY_REASONING_ONLY_METADATA_KEY: {
                    "finish_reason": finish_reason,
                    "content_len": content_len,
                    "reasoning_len": reasoning_len,
                    "tool_call_count": tool_call_count,
                }
            }
        return {}

    def _to_response(self, req: CompletionRequest, model: str, data: dict) -> CompletionResponse:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        model_content = msg.get("content") or ""
        reasoning_content = msg.get("reasoning_content")
        reasoning_len = len(reasoning_content) if isinstance(reasoning_content, str) else 0
        # B9: Completion is a continuation of the prefill.
        text = (req.assistant_prefill or "") + model_content
        # F1: weak-model recovery. When the structured tool_calls channel is empty
        # and req.assist is on, scan this response's content + reasoning_content
        # for the multi-format text encodings weak models leak (Hermes, fenced
        # json, bare json, etc). Structured wins when present; capable models
        # (req.assist=False) see byte-identical behavior to today.
        raw_tool_calls = self._tool_calls(msg.get("tool_calls"), tools=req.tools)
        recovered: list[ProposedToolCall] = []
        finish_reason = _map_finish(choice.get("finish_reason"))
        if not raw_tool_calls and req.assist:
            recovered = recover_tool_calls(model_content, reasoning_content)
            if recovered:
                finish_reason = "tool_calls"
        tool_calls = raw_tool_calls or recovered
        return CompletionResponse(
            text=text,
            tool_calls=tool_calls,
            usage=TokenUsage(
                input_tokens=int(usage.get("prompt_tokens", 0) or 0),
                output_tokens=int(usage.get("completion_tokens", 0) or 0),
                cost_usd=0.0,  # local models are free
                cached_tokens=self._cached_tokens(usage),
            ),
            finish_reason=finish_reason,
            model_used=data.get("model", model),
            request_id=req.request_id,
            routing=None,  # the router attaches the RoutingDecision (RT1)
            response_metadata=self._empty_reasoning_only_metadata(
                finish_reason=finish_reason,
                content_len=len(model_content),
                reasoning_len=reasoning_len,
                tool_call_count=len(tool_calls),
            ),
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
        if not isinstance(args, dict):
            return args  # defense in depth — never .items() a non-dict
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
    def _tool_calls(
        cls, raw: list | None, tools: list[ToolSpec] | None = None
    ) -> list[ProposedToolCall]:
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
            if not isinstance(parsed, dict):
                # A provider dialect can emit arguments as a bare JSON ARRAY —
                # _coerce_args/.items() on a list crashed the whole run task
                # (dt5 autopsy). Route it through the same honest-failure shape
                # as unparseable JSON: validation refuses with feedback the
                # model can act on, the run never dies.
                parsed = {"_raw": args_raw if isinstance(args_raw, str) else json.dumps(parsed)}

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

    def _ledger_emit(self, req: CompletionRequest, model: str) -> None:
        """Provider-request ledger (2026-07-09): when DISCO_PROVIDER_LEDGER names a
        file, append one relay-format JSONL record per outbound completion request —
        {ts (epoch float), host, model, has_tools, conversation_id}. This is the
        DIRECT-driver equivalent of the MiniMax relay's log: the build-soak harness
        adjudicates provider-after-terminal (runaway loops) from it, and previously
        REFUSED to run against direct drivers (no relay in the path = nothing to
        audit). Schema matches harness provider_ledger.parse_relay_log's structured
        branch; conversation_id scoping keeps PARALLEL soak lanes from
        cross-contaminating each other's after-terminal counts. Best-effort:
        ledger failure must never fail a live request. Unset env ⇒ zero overhead."""
        raw_cid = (req.metadata or {}).get("conversation_id")
        emit_provider_attempt(
            base_url=self._base,
            model=model,
            has_tools=bool(req.tools),
            conversation_id=str(raw_cid).strip() if raw_cid else None,
        )

    async def complete(self, req: CompletionRequest, *, model: str) -> CompletionResponse:
        self._ledger_emit(req, model)
        try:
            async with self._client() as client:
                resp = await client.post(
                    f"{self._base}/chat/completions",
                    json=self._payload(req, model, stream=False),
                    headers=self._headers(req),
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
        # F1: accumulator for reasoning_content deltas (reasoning models stream
        # the thinking separately from `content`; weak models may emit the
        # tool call in the thinking channel instead of the answer channel).
        reasoning_buf: list[str] = []
        tool_buf: dict[int, dict] = {}
        # Streaming tool-call assembly state. The OpenAI streaming contract puts
        # an `index` on EVERY tool_call delta, but real OpenAI-COMPATIBLE servers
        # (llama.cpp, some OpenRouter upstreams — the exact backends this adapter
        # targets) OMIT it on argument-continuation fragments. `last_idx` and
        # `id_to_idx` let a fragment that dropped its index still route to the
        # right slot instead of collapsing onto slot 0 (see the loop below).
        last_idx: int | None = None
        id_to_idx: dict[str, int] = {}
        finish: str | None = None
        usage: dict = {}
        model_used = model
        self._ledger_emit(req, model)
        try:
            async with self._client() as client:
                async with client.stream(
                    "POST",
                    f"{self._base}/chat/completions",
                    json=self._payload(req, model, stream=True),
                    headers=self._headers(req),
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
                        # F1: weak models may stream the tool call as thinking;
                        # capture reasoning_content for the recovery scan below.
                        reasoning_piece = delta.get("reasoning_content")
                        if reasoning_piece:
                            reasoning_buf.append(reasoning_piece)
                        for tc in delta.get("tool_calls") or []:
                            # Resolve the accumulation slot. Defaulting a MISSING
                            # `index` to 0 (the old behavior) mis-routes every
                            # index-less continuation fragment onto the FIRST tool
                            # call: later parallel calls then receive no arguments
                            # (they parse to `{}`) and the first call is corrupted
                            # by foreign concatenated JSON — the streaming
                            # truncation the plan.py validator only masks. Resolve
                            # in priority order so NO fragment is lost:
                            #   1. explicit `index` — authoritative (compliant
                            #      servers send it on every delta);
                            #   2. an open slot whose `id` matches — interleaved
                            #      continuations that re-send the id but drop index;
                            #   3. the most-recently-touched slot — sequential
                            #      continuations that drop both id and index (the
                            #      common llama.cpp shape: one call's args stream to
                            #      completion before the next call begins).
                            raw_idx = tc.get("index")
                            tc_id = tc.get("id")
                            if raw_idx is not None:
                                idx = raw_idx
                            elif tc_id and tc_id in id_to_idx:
                                idx = id_to_idx[tc_id]
                            elif last_idx is not None:
                                idx = last_idx
                            else:
                                idx = 0
                            last_idx = idx
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
                                # Surface the tool-call arg fragment so consumers can
                                # watch the file body assemble live (watch-it-write).
                                yield StreamChunk(
                                    tool_name=slot["name"],
                                    tool_args_delta=args_frag,
                                    tool_index=idx,
                                )
                        if ch.get("finish_reason"):
                            finish = ch["finish_reason"]
        except httpx.TimeoutException as exc:
            raise LLMTransientError(f"request timed out: {exc}", provider=self.name) from exc
        except httpx.HTTPError as exc:
            raise LLMTransientError(f"connection error: {exc}", provider=self.name) from exc

        accumulated_text = "".join(content)
        reasoning_content = "".join(reasoning_buf)
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
        # F1: weak-model recovery. Mirror the non-streaming gate: structured
        # empty + req.assist on -> scan this response's accumulated content +
        # reasoning_content. Structured wins; capable models (req.assist=False)
        # see byte-identical behavior to today.
        finish_reason = _map_finish(finish)
        if not tool_calls and req.assist:
            recovered = recover_tool_calls(accumulated_text, reasoning_content)
            if recovered:
                tool_calls = recovered
                finish_reason = "tool_calls"
        final = CompletionResponse(
            text=accumulated_text,
            tool_calls=tool_calls,
            usage=TokenUsage(
                input_tokens=int(usage.get("prompt_tokens", 0) or 0),
                output_tokens=int(usage.get("completion_tokens", 0) or 0),
                cost_usd=0.0,
                cached_tokens=self._cached_tokens(usage),
            ),
            finish_reason=finish_reason,
            model_used=model_used,
            request_id=req.request_id,
            routing=None,
            response_metadata=self._empty_reasoning_only_metadata(
                finish_reason=finish_reason,
                content_len=len(accumulated_text) - len(req.assistant_prefill or ""),
                reasoning_len=len(reasoning_content),
                tool_call_count=len(tool_calls),
            ),
        )
        yield StreamChunk(done=True, final=final)

    def supports(self, requirement: Requirement, *, model: str) -> bool:
        # Advisory metadata (router §8 / security principle 8): informs assignment,
        # does not gate. The provider sends the request as assigned regardless.
        return requirement in self._caps
