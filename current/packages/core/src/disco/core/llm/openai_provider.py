"""OpenAI-compatible ModelProvider — the real HTTP adapter (llm-router §3).

ONE client for any OpenAI chat-completions backend, parameterized by `base_url` +
`model` + optional `api_key`. Both local servers (llama.cpp Qwen, Gemma) and a
future OpenRouter speak this, so the contract's notional `ollama`/`openrouter`
providers collapse into this single client pointed at different base URLs.

Reasoning models (e.g. Qwen3.6): the ANSWER is `message.content`;
`reasoning_content` (the thinking) is parsed separately and not treated as answer
text. `enable_thinking` toggles it where the server honors `chat_template_kwargs`
(llama.cpp does) — leave it None to use the server default. DeepSeek's public API
is explicitly placed in its documented non-thinking mode because it otherwise
requires private reasoning traces to be replayed after tool calls, while Disco's
provider-neutral history deliberately does not persist those traces.

Errors map to the typed hierarchy (errors.py §6), PRESERVING the provider's real
message so it surfaces cleanly (the reactive-error rule). The context-window error
is classified to `LLMContextWindowExceeded` — what the condenser's hard reset and
the loop's reactive surfacing depend on.

The request-assembly, response-decoding, and streaming logic live in the
allowlisted private modules (``_request_assembly``, ``_openai_response``,
``_openai_stream``, ``_responses_api``). This module keeps ``OpenAIProvider`` as
an explicit, thin defining facade with its exact public method signatures and
monkeypatch seams.
"""

from __future__ import annotations

# Compatibility facade: private names are deliberate historical test/import seams.
# ruff: noqa: F401
import logging
from collections.abc import AsyncIterator, Iterable

import httpx

from ..events import WORKSPACE_SNAPSHOT_SENTINEL
from ._openai_response import (
    cached_tokens as _cached_tokens_impl,
)
from ._openai_response import (
    coerce_args as _coerce_args_impl,
)
from ._openai_response import (
    empty_reasoning_only_metadata as _empty_reasoning_only_metadata_impl,
)
from ._openai_response import (
    parse_tool_calls as _parse_tool_calls_impl,
)
from ._openai_response import (
    raise_typed as _raise_typed_impl,
)
from ._openai_response import (
    repair_json as _repair_json_impl,
)
from ._openai_response import (
    safe_provider_error as _safe_provider_error_impl,
)
from ._openai_response import (
    to_response as _to_response_impl,
)
from ._openai_stream import stream_complete as _stream_complete_impl
from ._request_assembly import (
    _F5_THINK_BLOCK_RE,
    _F5_THINK_BUDGET_CHARS,
    _F5_THINK_BUDGET_HEAD,
    _F5_THINK_BUDGET_TAIL,
    _FINISH,
    FinishReason,
    _is_anthropic,
    _map_finish,
    _prompt_cache_key,
)
from ._request_assembly import (
    build_headers as _build_headers_impl,
)
from ._request_assembly import (
    build_payload as _build_payload_impl,
)
from ._request_assembly import (
    host_speaks_chat_template_kwargs as _host_speaks_chat_template_kwargs,
)
from ._request_assembly import (
    is_context_overflow as _is_context_overflow,
)
from ._request_assembly import (
    message_to_wire as _message_to_wire_impl,
)
from ._request_assembly import (
    normalize_tool_call_ordering as _normalize_tool_call_ordering,
)
from ._request_assembly import (
    provider_request_shape as _provider_request_shape,
)
from ._request_assembly import (
    requires_user_after_terminal_response as _requires_user_after_terminal_response,
)
from ._request_assembly import (
    sanitize_tool_name as _sanitize_tool_name,
)
from ._request_assembly import (
    truncate_think_block as _truncate_think_block,
)
from ._responses_api import (
    build_responses_payload as _build_responses_payload,
)
from ._responses_api import (
    decode_responses_response as _decode_responses_response,
)
from ._responses_api import is_responses_endpoint as _is_responses_endpoint
from .errors import (
    LLMAuthError,
    LLMContentFiltered,
    LLMContextWindowExceeded,
    LLMError,
    LLMProviderUnavailable,
    LLMTransientError,
)
from .provider_ledger import (
    ProviderRequestShape,
    emit_provider_attempt,
    provider_ledger_enabled,
)
from .request_budget import RequestBudgetEstimate
from .toolcall_recovery import recover_tool_calls
from .types import (
    EMPTY_REASONING_ONLY_METADATA_KEY,
    CompletionRequest,
    CompletionResponse,
    LLMMessage,
    ProposedToolCall,
    Requirement,
    StreamChunk,
    TokenUsage,
    ToolSpec,
)

_LOG = logging.getLogger("disco.llm.openai")
_DISCO_CONVERSATION_HEADER = "X-Disco-Conversation"


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
        """True when this provider routes through OpenRouter."""
        return "openrouter" in self._base.lower() or "openrouter" in self.name.lower()

    # -- request shaping ------------------------------------------------------

    def _headers(self, req: CompletionRequest | None = None) -> dict[str, str]:
        return _build_headers_impl(
            base_url=self._base,
            api_key=self._key,
            name=self.name,
            req=req,
            conversation_header=_DISCO_CONVERSATION_HEADER,
        )

    @staticmethod
    def _message(m: LLMMessage) -> dict:
        return _message_to_wire_impl(m, sanitize_fn=_sanitize_tool_name)

    def _payload(self, req: CompletionRequest, model: str, *, stream: bool) -> dict:
        if _is_responses_endpoint(self._base):
            return _build_responses_payload(
                base_url=self._base,
                provider_name=self.name,
                req=req,
                model=model,
            )
        return _build_payload_impl(
            base_url=self._base,
            name=self.name,
            req=req,
            model=model,
            stream=stream,
            enable_thinking=self._enable_thinking,
            speaks_ctk=self._speaks_ctk,
            message_fn=self._message,
            normalize_fn=_normalize_tool_call_ordering,
            requires_user_fn=_requires_user_after_terminal_response,
            truncate_fn=_truncate_think_block,
            sanitize_fn=_sanitize_tool_name,
            prompt_cache_key_fn=self._prompt_cache_key,
            is_anthropic_fn=_is_anthropic,
            mark_anthropic_cache_fn=self._mark_anthropic_cache,
        )

    @staticmethod
    def _prompt_cache_key(req: CompletionRequest) -> str | None:
        return _prompt_cache_key(req)

    @staticmethod
    def _mark_anthropic_cache(body: dict) -> None:
        from ._request_assembly import _mark_anthropic_cache

        _mark_anthropic_cache(body)

    # -- error mapping (classify on raw body, raise only a safe summary) -------

    def _raise_typed(self, status: int, body_text: str) -> None:
        _raise_typed_impl(
            self.name,
            status,
            body_text,
            safe_error_fn=self._safe_provider_error,
            context_overflow_fn=_is_context_overflow,
        )

    def _safe_provider_error(self, status: int, err_type: str) -> str:
        return _safe_provider_error_impl(self.name, status, err_type)

    # -- response shaping -----------------------------------------------------

    @staticmethod
    def _cached_tokens(usage: dict) -> int:
        return _cached_tokens_impl(usage)

    @staticmethod
    def _empty_reasoning_only_metadata(
        *,
        finish_reason: FinishReason,
        content_len: int,
        reasoning_len: int,
        tool_call_count: int,
    ) -> dict:
        return _empty_reasoning_only_metadata_impl(
            finish_reason=finish_reason,
            content_len=content_len,
            reasoning_len=reasoning_len,
            tool_call_count=tool_call_count,
        )

    def _to_response(self, req: CompletionRequest, model: str, data: dict) -> CompletionResponse:
        return _to_response_impl(
            provider_name=self.name,
            req=req,
            model=model,
            data=data,
            tool_calls_fn=self._tool_calls,
            recovery_fn=recover_tool_calls,
            cached_tokens_fn=self._cached_tokens,
            metadata_fn=self._empty_reasoning_only_metadata,
            map_finish_fn=_map_finish,
        )

    @staticmethod
    def _repair_json(raw: str) -> str:
        return _repair_json_impl(raw)

    @staticmethod
    def _coerce_args(args: dict, schema: dict | None) -> dict:
        return _coerce_args_impl(args, schema)

    @classmethod
    def _tool_calls(
        cls, raw: list | None, tools: list[ToolSpec] | None = None
    ) -> list[ProposedToolCall]:
        return _parse_tool_calls_impl(
            raw,
            tools=tools,
            repair_fn=cls._repair_json,
            sanitize_fn=_sanitize_tool_name,
            log_repair_fn=lambda name: _LOG.info("Repaired JSON for tool %s", name),
        )

    # -- the ModelProvider protocol -------------------------------------------

    def _ledger_emit(
        self,
        req: CompletionRequest,
        model: str,
        payload: dict,
    ) -> None:
        """Provider-request ledger: when DISCO_PROVIDER_LEDGER names a file,
        append one relay-format JSONL record per outbound completion request.
        Best-effort: accounting or ledger failure must never fail a live request.
        Unset env means no shape work."""
        if not provider_ledger_enabled():
            return
        raw_cid = (req.metadata or {}).get("conversation_id")
        raw_purpose = (req.metadata or {}).get("provider_ledger_purpose")
        raw_call_kind = (req.metadata or {}).get("provider_ledger_call_kind")
        try:
            request_shape = _provider_request_shape(req, payload)
        except Exception:  # noqa: BLE001 - accounting must never block provider traffic
            request_shape = None
        emit_provider_attempt(
            base_url=self._base,
            model=model,
            has_tools=bool(req.tools),
            conversation_id=str(raw_cid).strip() if raw_cid else None,
            purpose=raw_purpose if isinstance(raw_purpose, str) else None,
            call_kind=raw_call_kind if isinstance(raw_call_kind, str) else None,
            request_shape=request_shape,
        )

    def request_budget_preview(
        self, req: CompletionRequest, model: str
    ) -> RequestBudgetEstimate | None:
        """Side-effect-free budget preview. No ledger record, no network I/O."""
        try:
            payload = self._payload(req, model, stream=True)
        except Exception:
            return None
        try:
            shape = _provider_request_shape(req, payload)
        except Exception:
            return None
        if shape.driver_context_window is None:
            return None
        try:
            return RequestBudgetEstimate(
                driver_context_window=shape.driver_context_window,
                max_output_tokens=shape.max_output_tokens,
                canonical_payload_bytes=shape.canonical_payload_bytes,
                messages_json_bytes=shape.messages_json_bytes,
                tools_json_bytes=shape.tools_json_bytes,
                message_count=shape.message_count,
                tool_count=shape.tool_count,
            )
        except ValueError:
            return None

    async def complete(self, req: CompletionRequest, *, model: str) -> CompletionResponse:
        payload = self._payload(req, model, stream=False)
        self._ledger_emit(req, model, payload)
        endpoint = (
            self._base if _is_responses_endpoint(self._base) else (f"{self._base}/chat/completions")
        )
        try:
            async with self._client() as client:
                resp = await client.post(
                    endpoint,
                    json=payload,
                    headers=self._headers(req),
                )
        except httpx.TimeoutException as exc:
            raise LLMTransientError(
                f"request timed out ({type(exc).__name__})", provider=self.name
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMTransientError(
                f"connection error ({type(exc).__name__})", provider=self.name
            ) from exc
        if resp.status_code >= 400:
            self._raise_typed(resp.status_code, resp.text)
        if _is_responses_endpoint(self._base):
            return _decode_responses_response(req=req, model=model, data=resp.json())
        return self._to_response(req, model, resp.json())

    async def stream_complete(
        self, req: CompletionRequest, *, model: str
    ) -> AsyncIterator[StreamChunk]:
        if _is_responses_endpoint(self._base):
            response = await self.complete(req.model_copy(update={"stream": False}), model=model)
            if response.text:
                yield StreamChunk(delta_text=response.text)
            yield StreamChunk(done=True, final=response)
            return
        payload = self._payload(req, model, stream=True)
        self._ledger_emit(req, model, payload)
        async for chunk in _stream_complete_impl(
            provider_name=self.name,
            base_url=self._base,
            payload=payload,
            headers=self._headers(req),
            req=req,
            model=model,
            client_factory=self._client,
            raise_typed_fn=self._raise_typed,
            tool_calls_fn=self._tool_calls,
            recovery_fn=recover_tool_calls,
            cached_tokens_fn=self._cached_tokens,
            metadata_fn=self._empty_reasoning_only_metadata,
            map_finish_fn=_map_finish,
        ):
            yield chunk

    def supports(self, requirement: Requirement, *, model: str) -> bool:
        # Advisory metadata (router §8 / security principle 8): informs assignment,
        # does not gate. The provider sends the request as assigned regardless.
        return requirement in self._caps
