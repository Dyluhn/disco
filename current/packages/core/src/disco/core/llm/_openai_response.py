"""OpenAI-compatible response decoding and error mapping.

Extracted from ``openai_provider.py`` so the provider class stays a thin facade.
These functions are pure over (provider-state, raw-response) and produce the
exact CompletionResponse / typed errors the provider contract requires.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from ._request_assembly import FinishReason, _map_finish, sanitize_tool_name
from .errors import (
    LLMAuthError,
    LLMContentFiltered,
    LLMContextWindowExceeded,
    LLMError,
    LLMProviderUnavailable,
    LLMTransientError,
)
from .toolcall_recovery import recover_tool_calls
from .types import (
    EMPTY_REASONING_ONLY_METADATA_KEY,
    CompletionRequest,
    CompletionResponse,
    ProposedToolCall,
    TokenUsage,
    ToolSpec,
)

_RecoveryFn = Callable[[str, str | None], list[ProposedToolCall]]
_ToolCallsFn = Callable[
    [list | None, list[ToolSpec] | None],
    list[ProposedToolCall],
]

_PROVIDER_UNAVAILABLE_PHRASES = (
    "no cookie auth",
    "no allowed providers",
    "no instances available",
    "no endpoints found",
    "provider returned error",
    "requires moderation",
)


def safe_provider_error(provider_name: str, status: int, err_type: str) -> str:
    """Build a safe, content-free provider error summary."""
    typ = "".join(ch for ch in (err_type or "") if ch.isalnum() or ch in {"_", "-", "."})
    suffix = f" type={typ[:80]}" if typ else ""
    return f"provider {provider_name} returned HTTP {status}{suffix}"


# Statuses whose provider message is ACCOUNT/CONFIG state the operator must
# act on — "requires a subscription", "weekly usage limit reached", "requires
# explicit opt in: <url>". A bare status code makes these undiagnosable, so
# the sanitized provider text is appended for exactly these classes. Model
# output and user content never reach this path: the text comes from the
# provider's own structured error envelope.
_ACTIONABLE_STATUSES = frozenset({401, 402, 403, 429})
_MAX_PROVIDER_DETAIL = 240


def sanitize_provider_detail(message: str) -> str:
    """One-line, length-capped, control-character-free provider detail."""
    collapsed = " ".join((message or "").split())
    printable = "".join(ch for ch in collapsed if ch.isprintable())
    if len(printable) > _MAX_PROVIDER_DETAIL:
        printable = printable[: _MAX_PROVIDER_DETAIL - 1].rstrip() + "…"
    return printable


def _classify_and_raise(
    provider_name: str,
    status: int,
    err_type: str,
    message: str,
    safe_message: str,
    context_overflow_fn: Callable[[str, str], bool],
) -> None:
    """Raise the typed LLM error for a classified non-2xx response."""
    if context_overflow_fn(err_type, message):
        raise LLMContextWindowExceeded(safe_message, provider=provider_name)
    if status in (401, 403) or "auth" in err_type.lower():
        raise LLMAuthError(safe_message, provider=provider_name)
    if "content_filter" in err_type.lower() or status == 451:
        raise LLMContentFiltered(safe_message, provider=provider_name)
    if status == 429 or status >= 500:
        raise LLMTransientError(safe_message, provider=provider_name, http_status=status)
    _msg_lower = message.lower()
    _type_lower = err_type.lower()
    if any(p in _msg_lower for p in _PROVIDER_UNAVAILABLE_PHRASES) or _type_lower.startswith(
        "provider"
    ):
        raise LLMProviderUnavailable(safe_message, provider=provider_name)
    raise LLMError(safe_message, provider=provider_name)


def raise_typed(
    provider_name: str,
    status: int,
    body_text: str,
    *,
    safe_error_fn: Callable[[int, str], str],
    context_overflow_fn: Callable[[str, str], bool],
) -> None:
    """Classify a non-2xx response body and raise the typed LLM error.

    ``str(exc)`` stays content-free. For account/config statuses
    (401/402/403/429) the provider's OWN sanitized message is attached as
    ``exc.provider_detail`` — those messages name the fix ("requires a
    subscription", "requires explicit opt in: <url>", "weekly usage limit")
    and a bare status code leaves the operator guessing. The context-window
    error is classified to ``LLMContextWindowExceeded``.
    """
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
    safe_message = safe_error_fn(status, err_type)
    detail = (
        sanitize_provider_detail(message) if status in _ACTIONABLE_STATUSES else ""
    )
    try:
        _classify_and_raise(
            provider_name,
            status,
            err_type,
            message,
            safe_message,
            context_overflow_fn,
        )
    except LLMError as exc:
        exc.provider_detail = detail
        raise


def cached_tokens(usage: dict) -> int:
    """Extract cached prompt tokens across provider shapes."""
    details = usage.get("prompt_tokens_details") or {}
    return int(details.get("cached_tokens", 0) or usage.get("cache_read_input_tokens", 0) or 0)


def empty_reasoning_only_metadata(
    *,
    finish_reason: FinishReason,
    content_len: int,
    reasoning_len: int,
    tool_call_count: int,
) -> dict:
    """Metadata marking an empty reasoning-only response."""
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


def repair_json(raw: str) -> str:
    """Rung 5: Mechanical JSON repair. Strip fences, fix trailing commas,
    escape control characters, and strip XML-like closing tags."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        raw = raw.strip()
    raw = re.sub(r"</?[a-zA-Z0-9_]+>$", "", raw).strip()
    raw = re.sub(r",\s*([\]}])", r"\1", raw)

    def _escape_ctrl(m):
        return f"\\u{ord(m.group(0)):04x}"

    return re.sub(r"[\x00-\x1f]", _escape_ctrl, raw)


def _coerce_one(k: str, v: Any, prop: dict | None) -> Any:
    """Coerce a single argument value to the schema-declared type."""
    if not prop:
        return v
    target = prop.get("type")
    if target == "integer" and isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            return v
    if target == "number" and isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return v
    if target == "boolean" and isinstance(v, str):
        if v.lower() in ("true", "1", "yes"):
            return True
        if v.lower() in ("false", "0", "no"):
            return False
        return v
    return v


def coerce_args(args: dict, schema: dict | None) -> dict:
    """Rung 5: Type-coercing validation. Cast strings to expected types."""
    if not isinstance(args, dict):
        return args
    if not schema or schema.get("type") != "object":
        return args
    properties = schema.get("properties", {})
    coerced = {}
    for k, v in args.items():
        coerced[k] = _coerce_one(k, v, properties.get(k))
    return coerced


def _parse_tool_args(
    args_raw: Any,
    *,
    repair_fn: Callable[[str], str],
    repaired_fn: Callable[[], None] | None,
) -> Any:
    """Parse tool-call arguments from the wire, with repair rungs.

    A provider dialect can emit arguments as a bare JSON ARRAY — route it
    through the same honest-failure shape as unparseable JSON: validation
    refuses with feedback the model can act on, the run never dies.
    """
    if isinstance(args_raw, str):
        try:
            parsed = json.loads(args_raw)
        except (json.JSONDecodeError, ValueError):
            try:
                repaired = repair_fn(args_raw)
                parsed = json.loads(repaired)
                if repaired_fn is not None:
                    repaired_fn()
            except Exception:
                return {"_raw": args_raw}
    else:
        parsed = args_raw or {}
    if not isinstance(parsed, dict):
        return {"_raw": args_raw if isinstance(args_raw, str) else json.dumps(parsed)}
    return parsed


def parse_tool_calls(
    raw: list | None,
    tools: list[ToolSpec] | None = None,
    *,
    repair_fn: Callable[[str], str] = repair_json,
    sanitize_fn: Callable[[str], str] = sanitize_tool_name,
    log_repair_fn: Callable[[str], None] | None = None,
) -> list[ProposedToolCall]:
    """Parse structured tool_calls from the wire format, with repair rungs."""
    out: list[ProposedToolCall] = []
    spec_map = {t.name: t for t in (tools or [])}
    sanitized_map = {sanitize_fn(t.name): t for t in (tools or [])}

    for tc in raw or []:
        fn = tc.get("function") or {}
        name = fn.get("name", "")
        clean_name = sanitize_fn(name)
        parsed = _parse_tool_args(
            fn.get("arguments"),
            repair_fn=repair_fn,
            repaired_fn=(
                None
                if log_repair_fn is None
                else lambda clean_name=clean_name: log_repair_fn(clean_name)
            ),
        )
        spec = spec_map.get(name) or sanitized_map.get(clean_name)
        if spec:
            parsed = coerce_args(parsed, spec.parameters_schema)
            final_name = spec.name
        else:
            final_name = clean_name
        out.append(
            ProposedToolCall(tool_name=final_name, arguments=parsed, provider_call_id=tc.get("id"))
        )
    return out


def to_response(
    *,
    provider_name: str,
    req: CompletionRequest,
    model: str,
    data: dict,
    tool_calls_fn: _ToolCallsFn = parse_tool_calls,
    recovery_fn: _RecoveryFn = recover_tool_calls,
    cached_tokens_fn: Callable[[dict], int] = cached_tokens,
    metadata_fn: Callable[..., dict] = empty_reasoning_only_metadata,
    map_finish_fn: Callable[[str | None], FinishReason] = _map_finish,
) -> CompletionResponse:
    """Decode a non-streaming chat-completions response."""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    usage = data.get("usage") or {}
    model_content = msg.get("content") or ""
    reasoning_content = msg.get("reasoning_content")
    reasoning_len = len(reasoning_content) if isinstance(reasoning_content, str) else 0
    text = (req.assistant_prefill or "") + model_content
    raw_tool_calls = tool_calls_fn(msg.get("tool_calls"), req.tools)
    recovered: list[ProposedToolCall] = []
    finish_reason = map_finish_fn(choice.get("finish_reason"))
    if not raw_tool_calls and req.assist:
        recovered = recovery_fn(model_content, reasoning_content)
        if recovered:
            finish_reason = "tool_calls"
    tool_calls = raw_tool_calls or recovered
    return CompletionResponse(
        text=text,
        tool_calls=tool_calls,
        usage=TokenUsage(
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
            cost_usd=0.0,
            cached_tokens=cached_tokens_fn(usage),
        ),
        finish_reason=finish_reason,
        model_used=data.get("model", model),
        request_id=req.request_id,
        routing=None,
        response_metadata=metadata_fn(
            finish_reason=finish_reason,
            content_len=len(model_content),
            reasoning_len=reasoning_len,
            tool_call_count=len(tool_calls),
        ),
    )
