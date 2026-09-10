"""Provider request assembly — payload shaping for OpenAI-compatible backends.

Extracted from ``openai_provider.py`` so the provider class stays a thin facade
with its exact public method signatures and monkeypatch seams. The functions
here are pure over (provider-state, CompletionRequest) and produce the exact
serialized request bytes the provider sends on the wire.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import urlsplit

from ..events import WORKSPACE_SNAPSHOT_SENTINEL, LLMMessage, find_elided_arg_markers
from .provider_ledger import ProviderRequestShape
from .types import CompletionRequest, ToolSpec

FinishReason = Literal["stop", "length", "tool_calls", "content_filter", "error"]

_FINISH: dict[str, FinishReason] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "content_filter": "content_filter",
}

_ELIDED_HISTORY_NOTE = (
    "[Host transcript — a historical tool-call record had one or more large "
    "arguments omitted from prompt history. Its paired outcome follows; this entry "
    "is metadata, not reusable tool input or file content. Read current state only "
    "if the next task needs it.]"
)


def _map_finish(raw: str | None) -> FinishReason:
    return _FINISH.get(raw or "stop", "stop")


def _is_anthropic(model: str) -> bool:
    """Whether a model id routes to Anthropic (direct or via OpenRouter), so we
    emit Claude-style `cache_control` breakpoints. Other providers get plain
    string content + `prompt_cache_key` only."""
    m = model.lower()
    return "claude" in m or m.startswith("anthropic/")


def _canonical_json_bytes(value: object) -> int:
    """Deterministic UTF-8 JSON size for accounting, explicitly not wire bytes."""

    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def _image_shape(messages: object) -> tuple[int, int]:
    """Return image count and URL characters without retaining either URL."""

    if not isinstance(messages, list):
        return 0, 0
    image_count = 0
    image_url_chars = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            image_count += 1
            image_url = part.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else None
            if isinstance(url, str):
                image_url_chars += len(url)
    return image_count, image_url_chars


def provider_request_shape(
    req: CompletionRequest,
    payload: dict,
) -> ProviderRequestShape:
    """Reduce an already-final payload to a fixed, content-free scalar shape."""

    messages = payload.get("messages", payload.get("input"))
    message_list = messages if isinstance(messages, list) else []
    tools = payload.get("tools")
    tool_list = tools if isinstance(tools, list) else []
    role_counts = {"system": 0, "user": 0, "assistant": 0, "tool": 0}
    for message in message_list:
        if isinstance(message, dict) and message.get("role") in role_counts:
            role_counts[message["role"]] += 1
    image_count, image_url_chars = _image_shape(message_list)
    raw_context_window = (req.metadata or {}).get("driver_context_window")
    context_window = (
        raw_context_window if type(raw_context_window) is int and raw_context_window > 0 else None
    )
    max_output_tokens = payload.get("max_tokens", payload.get("max_output_tokens"))
    if type(max_output_tokens) is not int or max_output_tokens <= 0:
        max_output_tokens = None
    return ProviderRequestShape(
        request_id=req.request_id,
        driver_context_window=context_window,
        model_repair_attempt=req.attempt,
        stream=payload.get("stream") is True,
        max_output_tokens=max_output_tokens,
        canonical_payload_bytes=_canonical_json_bytes(payload),
        messages_json_bytes=_canonical_json_bytes(message_list),
        tools_json_bytes=_canonical_json_bytes(tool_list) if tool_list else 0,
        message_count=len(message_list),
        tool_count=len(tool_list),
        system_message_count=role_counts["system"],
        user_message_count=role_counts["user"],
        assistant_message_count=role_counts["assistant"],
        tool_message_count=role_counts["tool"],
        image_count=image_count,
        image_url_chars=image_url_chars,
    )


def is_context_overflow(err_type: str, message: str) -> bool:
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

_F5_THINK_BUDGET_CHARS = 2_000
_F5_THINK_BUDGET_HEAD = 1_000
_F5_THINK_BUDGET_TAIL = 500

_F5_THINK_BLOCK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


def truncate_think_block(content: str) -> str:
    """F5(a): head+tail truncate any over-budget ``<think>`` block in ``content``."""

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


def host_speaks_chat_template_kwargs(base_url: str) -> bool:
    """``chat_template_kwargs`` is a llama.cpp / vLLM SERVER extension, not part of
    the OpenAI schema. Send it only to hosts that plausibly run a self-hosted
    server."""
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


def host_requires_explicit_nonthinking(base_url: str) -> bool:
    """Whether the provider requires an explicit non-thinking request policy.

    DeepSeek enables thinking by default and requires its private
    ``reasoning_content`` field to be replayed after assistant tool calls. The
    provider-neutral Disco message/event contract deliberately does not retain
    private reasoning traces, so this adapter must select DeepSeek's documented
    non-thinking mode before the first turn rather than emit history it cannot
    round-trip on the next one.
    """
    from urllib.parse import urlsplit

    return (urlsplit(base_url).hostname or "").lower() == "api.deepseek.com"


def glm53_lightweight_reasoning(base_url: str, model: str, enabled: bool | None) -> bool:
    """Translate non-thinking intent for the documented always-thinking GLM 5.3 API.

    Z.ai's GLM 5.3 migration specifies enabled thinking with low effort instead
    of disabled thinking. OpenCode Go forwards this native model policy.
    """
    from urllib.parse import urlsplit

    host = (urlsplit(base_url).hostname or "").lower()
    model_name = model.rsplit("/", 1)[-1].casefold()
    return (
        enabled is False
        and host in {"api.z.ai", "opencode.ai"}
        and model_name
        in {
            "glm-5.3",
            "glm-5.3-flash",
        }
    )


def requires_user_after_terminal_response(base_url: str) -> bool:
    """Whether this compatibility endpoint rejects a trailing response turn."""
    from urllib.parse import urlsplit

    parsed = urlsplit(base_url)
    return (parsed.hostname or "").lower() == "opencode.ai" and parsed.path.rstrip("/").endswith(
        "/zen/go/v1"
    )


def host_requires_serial_tool_calls(base_url: str) -> bool:
    """Whether a compatibility relay drops identity from parallel call deltas."""
    return requires_user_after_terminal_response(base_url)


def _tool_payload(tools: list[ToolSpec], sanitize_fn: Callable[[str], str], base_url: str) -> dict:
    """Build tool fields, including compatibility routing policy."""
    result: dict = {"tools": _tool_wire_list(tools, sanitize_fn)}
    if host_requires_serial_tool_calls(base_url):
        # OpenCode Go has emitted multiple streamed calls without either index
        # or id. One call per turn removes that provider-side ambiguity.
        result["parallel_tool_calls"] = False
    return result


def sanitize_tool_name(name: str) -> str:
    """Sanitize tool names for OpenAI boundary (dots are forbidden).
    Strip everything before the last dot and filter to [a-zA-Z0-9_-]."""
    if "." in name:
        name = name.split(".")[-1]
    return re.sub(r"[^a-zA-Z0-9_-]", "", name)


def normalize_tool_call_ordering(msgs: list[dict]) -> list[dict]:
    """Enforce the strict tool-call/result adjacency the wire format requires.

    The pass rebuilds the list so that each assistant ``tool_calls`` is
    immediately followed by its result messages, in the SAME order the calls
    were declared; a declared tool_call with no result gets a minimal stub
    result; an orphan ``role:"tool"`` result is removed. It is a NO-OP when
    the list already satisfies the invariant.
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


def message_to_wire(
    m: LLMMessage,
    *,
    sanitize_fn: Callable[[str], str] = sanitize_tool_name,
) -> dict:
    """Convert an LLMMessage to the OpenAI wire dict format."""
    content: str | list = m.content
    if m.images:
        parts: list[dict[str, Any]] = [{"type": "text", "text": m.content}]
        for img_url in m.images:
            parts.append({"type": "image_url", "image_url": {"url": img_url}})
        content = parts

    role = m.role
    tool_call_id = getattr(m, "tool_call_id", None)

    # DEFECT-6: a role:"tool" message MUST have a tool_call_id on the wire.
    if role == "tool" and not tool_call_id:
        role = "user"
        content = f"Tool error: {content}"

    d: dict = {"role": role, "content": content}
    if tool_call_id:
        d["tool_call_id"] = tool_call_id

    tool_calls = m.tool_calls
    if tool_calls:
        wire_calls: list[dict[str, object]] = []
        for tc in tool_calls:
            wire_arguments, arguments_elided = _tool_arguments_to_wire(tc.get("arguments"))
            if arguments_elided:
                # Never teach the model an invalid historical call by deleting a
                # required argument (for example successful file_write(path=...)
                # with no content). build_payload externalizes the paired result as
                # a host transcript entry instead.
                continue
            wire_calls.append(
                {
                    "id": tc.get("id"),
                    "type": "function",
                    "function": {
                        "name": sanitize_fn(tc.get("name") or ""),
                        "arguments": wire_arguments,
                    },
                }
            )
        if wire_calls:
            d["tool_calls"] = wire_calls
    return d


def _tool_arguments_to_wire(raw_arguments: object) -> tuple[str, bool]:
    """Omit render-only argument fields without perturbing ordinary history."""
    if isinstance(raw_arguments, str):
        if not find_elided_arg_markers({"argument": raw_arguments}):
            return raw_arguments, False
        try:
            parsed_arguments = json.loads(raw_arguments)
        except (TypeError, ValueError):
            return "{}", True
        if not isinstance(parsed_arguments, dict):
            return "{}", True
        raw_arguments = parsed_arguments

    if not isinstance(raw_arguments, dict):
        return "{}", False

    arguments = {
        key: value
        for key, value in raw_arguments.items()
        if not find_elided_arg_markers({str(key): value})
    }
    return json.dumps(arguments, sort_keys=True), len(arguments) != len(raw_arguments)


def thinking_payload(
    base_url: str, model: str, enabled: bool | None, speaks_ctk: bool
) -> dict[str, Any]:
    from urllib.parse import urlsplit

    if host_requires_explicit_nonthinking(base_url):
        return {"thinking": {"type": "disabled"}}
    if (
        enabled is False
        and (urlsplit(base_url).hostname or "").lower() == "ollama.com"
        and model.rsplit("/", 1)[-1].casefold().split(":", 1)[0] == "deepseek-v4-flash"
    ):
        # Ollama exposes thinking control through its compatible API field.
        # Preserve the caller's explicit nonthinking request on this tested model.
        return {"reasoning_effort": "none"}
    if glm53_lightweight_reasoning(base_url, model, enabled):
        return {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
    if (
        enabled is False
        and (urlsplit(base_url).hostname or "").lower() == "opencode.ai"
        and model.rsplit("/", 1)[-1].casefold() == "qwen3.8-flash"
    ):
        # Qwen's native flag is forwarded by OpenCode's compatible endpoint.
        # chat_template_kwargs is a self-hosted server option, not this API.
        return {"enable_thinking": False}
    if enabled is not None and speaks_ctk:
        return {"chat_template_kwargs": {"enable_thinking": enabled}}
    return {}


def _elided_history_call_ids(messages: list[LLMMessage]) -> set[str]:
    """Return exact historical calls whose provider-visible arguments were reduced."""
    call_ids: set[str] = set()
    for message in messages:
        if message.role != "assistant":
            continue
        for tool_call in message.tool_calls or []:
            _, arguments_elided = _tool_arguments_to_wire(tool_call.get("arguments"))
            call_id = tool_call.get("id")
            if arguments_elided and isinstance(call_id, str) and call_id:
                call_ids.add(call_id)
    return call_ids


def _externalize_elided_tool_results(msgs: list[dict], call_ids: set[str]) -> list[dict]:
    """Replace paired results for elided calls with ordinary host transcript entries.

    ``message_to_wire`` drops the whole historical call rather than leaving a
    schema-invalid partial argument object. Its result therefore cannot retain a
    ``role=tool``/``tool_call_id`` pair. Preserve the useful result as a host-authored
    user message, with omission metadata outside its body. No placeholder or fake
    argument value is exposed for the model to copy.
    """
    if not call_ids:
        return msgs
    externalized: set[str] = set()
    out: list[dict] = []
    for message in msgs:
        call_id = message.get("tool_call_id")
        if message.get("role") != "tool" or call_id not in call_ids or call_id in externalized:
            out.append(message)
            continue
        content = message.get("content")
        if isinstance(content, str) and content:
            transcript = f"{_ELIDED_HISTORY_NOTE}\n\n{content}"
        else:
            transcript = _ELIDED_HISTORY_NOTE
        out.append({"role": "user", "content": transcript})
        externalized.add(call_id)
    return out


def _shape_message_for_payload(
    m: LLMMessage,
    assist: bool,
    truncate_fn: Callable[[str], str],
) -> LLMMessage:
    """Apply F5 think-block truncation and all-tiers think-history strip."""
    content = m.content
    if m.role == "assistant" and "<think>" in content and "</think>" in content:
        content = _F5_THINK_BLOCK_RE.sub("", content)
    if assist and "<think>" in content and "</think>" in content:
        content = truncate_fn(content)
    if content is m.content:
        return m
    return m.model_copy(update={"content": content})


def _finalize_messages(
    msgs: list[dict],
    *,
    base_url: str,
    assistant_prefill: str | None,
    requires_user_fn: Callable[[str], bool],
) -> list[dict]:
    """Apply terminal-response continuation and assistant prefill."""
    terminal_role = msgs[-1].get("role") if msgs else None
    if requires_user_fn(base_url) and terminal_role in {
        "assistant",
        "tool",
    }:
        msgs.append(
            {
                "role": "user",
                "content": (
                    "Continue from the tool result above."
                    if terminal_role == "tool"
                    else "Continue from the assistant response above."
                ),
            }
        )
    if assistant_prefill:
        msgs.append({"role": "assistant", "content": assistant_prefill})
    return msgs


def _tool_wire_list(
    tools: list[ToolSpec],
    sanitize_fn: Callable[[str], str],
) -> list[dict]:
    """Build the OpenAI tools wire list from ToolSpecs."""
    return [
        {
            "type": "function",
            "function": {
                "name": sanitize_fn(t.name),
                "description": t.description,
                "parameters": t.parameters_schema,
            },
        }
        for t in tools
    ]


def _resolve_enable_thinking(
    *,
    req: CompletionRequest,
    enable_thinking: bool | None,
) -> bool | None:
    """Resolve the effective enable_thinking value, applying F5(b) repair gate."""
    et = req.enable_thinking if req.enable_thinking is not None else enable_thinking
    if req.assist and (req.attempt or 1) >= 2:
        et = False
    return et


def build_payload(
    *,
    base_url: str,
    name: str,
    req: CompletionRequest,
    model: str,
    stream: bool,
    enable_thinking: bool | None,
    speaks_ctk: bool,
    message_fn: Callable[[LLMMessage], dict],
    normalize_fn: Callable[[list[dict]], list[dict]],
    requires_user_fn: Callable[[str], bool],
    truncate_fn: Callable[[str], str],
    sanitize_fn: Callable[[str], str],
    prompt_cache_key_fn: Callable[[CompletionRequest], str | None],
    is_anthropic_fn: Callable[[str], bool],
    mark_anthropic_cache_fn: Callable[[dict], None],
) -> dict:
    """Build the OpenAI chat-completions request payload.

    This is the exact request-assembly logic extracted from
    ``OpenAIProvider._payload`` so the serialized request bytes stay identical.
    """
    source_messages = [_shape_message_for_payload(m, req.assist, truncate_fn) for m in req.messages]
    elided_call_ids = _elided_history_call_ids(source_messages)
    msgs = [message_fn(m) for m in source_messages]
    msgs = _externalize_elided_tool_results(msgs, elided_call_ids)
    msgs = normalize_fn(msgs)
    msgs = _finalize_messages(
        msgs,
        base_url=base_url,
        assistant_prefill=req.assistant_prefill,
        requires_user_fn=requires_user_fn,
    )

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
        body.update(_tool_payload(req.tools, sanitize_fn, base_url))
    is_openrouter = "openrouter" in base_url.lower() or "openrouter" in name.lower()
    if is_openrouter:
        body["provider"] = {
            "require_parameters": True,
            "allow_fallbacks": True,
            **(req.provider_prefs or {}),
        }
    et = _resolve_enable_thinking(req=req, enable_thinking=enable_thinking)
    body.update(thinking_payload(base_url, model, et, speaks_ctk))
    if stream:
        body["stream_options"] = {"include_usage": True}
    ckey = prompt_cache_key_fn(req)
    if ckey:
        body["prompt_cache_key"] = ckey
    if is_anthropic_fn(model):
        mark_anthropic_cache_fn(body)
    return body


def _prompt_cache_key(req: CompletionRequest) -> str | None:
    """A deterministic cache-routing key from the stable prefix."""
    parts = [m.content or "" for m in req.messages if m.role == "system"]
    parts.extend(sorted(t.name for t in (req.tools or [])))
    if not parts:
        return None
    return "pmx-" + hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:24]


def _mark_anthropic_cache(body: dict) -> None:
    """Add Anthropic ``cache_control: ephemeral`` breakpoints on the big stable
    prefix blocks so Claude caches them."""
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
            break
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
        tools[-1]["cache_control"] = {"type": "ephemeral"}


def build_headers(
    *,
    base_url: str,
    api_key: str | None,
    name: str,
    req: CompletionRequest | None,
    conversation_header: str,
    fallback_session_id: str = "",
) -> dict[str, str]:
    """Build the HTTP headers for a provider request."""
    h = {"content-type": "application/json"}
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    raw_cid = (req.metadata or {}).get("conversation_id") if req is not None else None
    cid = str(raw_cid).strip() if raw_cid is not None else ""
    if cid:
        h[conversation_header] = cid
    if req is not None:
        host = (urlsplit(base_url).hostname or "").lower()
        if host == "opencode.ai":
            request_id = req.request_id.strip() if isinstance(req.request_id, str) else ""
            session_id = cid or request_id or fallback_session_id
            if session_id:
                h["x-opencode-session"] = session_id
            if request_id:
                h["x-opencode-request"] = request_id
            h["x-opencode-client"] = "disco"
    return h
