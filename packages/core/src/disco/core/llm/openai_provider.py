"""OpenAI-compatible ModelProvider — the real HTTP adapter (llm-router §3).

ONE client for any OpenAI chat-completions backend, parameterized by `base_url` +
`model` + optional `api_key`. Both local servers (llama.cpp Qwen, Gemma) and a
future OpenRouter speak this, so the contract's notional `ollama`/`openrouter`
providers collapse into this single client pointed at different base URLs.

Reasoning controls and optional compatibility extensions come only from an
operator-declared RequestPolicy. Endpoint and model names never select policy.

Errors map to the typed hierarchy (errors.py §6), PRESERVING the provider's real
message so it surfaces cleanly (the reactive-error rule). The context-window error
is classified to `LLMContextWindowExceeded` — what the condenser's hard reset and
the loop's reactive surfacing depend on.

Timeouts are PROGRESS-based, never total wall-clock: ``complete`` streams under
the hood and buffers the result, so a local model that legitimately thinks for
twenty minutes is distinguishable from a hung provider by whether meaningful output is
still arriving. See ``_openai_timeouts`` for the budget. A wall-clock ceiling
survives only where streaming is impossible (the Responses API, and the one-shot
fallback for a server that refuses the streamed request).

The request-assembly, response-decoding, streaming, and timeout logic live in
the allowlisted private modules (``_request_assembly``, ``_openai_response``,
``_openai_stream``, ``_openai_timeouts``, ``_responses_api``). This module keeps
``OpenAIProvider`` as an explicit, thin defining facade with its exact public
method signatures and monkeypatch seams.
"""

from __future__ import annotations

# Compatibility facade: private names are deliberate historical test/import seams.
# ruff: noqa: F401
import logging
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import replace
from uuid import uuid4

import httpx

from ..events import WORKSPACE_SNAPSHOT_SENTINEL
from ._openai_buffered import buffered_complete
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
from ._openai_timeouts import ProviderTimeouts, resolve_timeouts
from ._reasoning_fallback import rejection_handler, with_reasoning_fallback
from ._request_assembly import (
    _F5_THINK_BLOCK_RE,
    _F5_THINK_BUDGET_CHARS,
    _F5_THINK_BUDGET_HEAD,
    _F5_THINK_BUDGET_TAIL,
    _FINISH,
    FinishReason,
    _map_finish,
    _prompt_cache_key,
    _resolve_enable_thinking,
)
from ._request_assembly import (
    build_headers as _build_headers_impl,
)
from ._request_assembly import (
    build_payload as _build_payload_impl,
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
from .request_budget import RequestBudgetEstimate, preview_request_budget
from .request_policy import RequestPolicy
from .stream_progress import reporter_for
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

# Statuses a server uses to refuse the SHAPE of a request. Only these, and only
# when the body names streaming, earn the one-shot buffered fallback: anything
# else (a missing model, a bad key, a filtered prompt) must propagate exactly as
# it did before, never be retried behind the operator's back.
_STREAM_REJECT_STATUSES = frozenset({400, 404, 422, 501})
# Deliberately narrow. A short token like "sse" is a substring of ordinary
# English ("assessment"), and a false positive here would silently double a
# genuine bad request.
_STREAM_REJECT_MARKERS = ("stream", "server-sent")


class _StreamRejected(Exception):  # noqa: N818 - control signal, not a failure
    """The server refused the streamed request itself, before any chunk.

    Transport mechanics, handled once inside ``complete``; never surfaced.
    """

    def __init__(self, status: int) -> None:
        super().__init__(f"server rejected the streamed request (HTTP {status})")
        self.status = status


def _is_stream_rejection(status: int, body_text: str) -> bool:
    """Whether a non-2xx body is the server refusing to stream THIS request."""
    if status not in _STREAM_REJECT_STATUSES:
        return False
    lowered = (body_text or "").lower()
    return any(marker in lowered for marker in _STREAM_REJECT_MARKERS)


class OpenAIProvider:
    """[CONTRACT — ModelProvider] A real OpenAI chat-completions backend."""

    def __init__(
        self,
        base_url: str,
        *,
        name: str = "openai",
        api_key: str | None = None,
        timeout_s: float | None = None,
        enable_thinking: bool | None = None,
        capabilities: Iterable[Requirement] = (),
        transport: httpx.AsyncBaseTransport | None = None,
        request_policy: RequestPolicy | None = None,
        model_policies: dict[str, RequestPolicy] | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self.name = name
        self._key = api_key
        # Unscoped calls share this client session across transport/router retries.
        self._session_id = uuid4().hex
        # `timeout_s` is the BUFFERED ceiling only (Responses API + the
        # stream-reject fallback). Streamed calls are bounded by progress, so
        # there is deliberately no total ceiling for them. Env is read once,
        # here, so a live request never pays for environment lookups.
        self._timeouts: ProviderTimeouts = resolve_timeouts(timeout_s)
        self._timeout = self._timeouts.buffered_total_s
        self._enable_thinking = enable_thinking
        self._request_policy = request_policy or RequestPolicy()
        self._model_policies = dict(model_policies or {})
        self._caps = frozenset(capabilities)
        self._transport = transport  # test seam (httpx.MockTransport); None = real

    def _client(self, timeout: httpx.Timeout | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=timeout if timeout is not None else self._timeouts.buffered_httpx_timeout(),
            transport=self._transport,
            trust_env=False,
            follow_redirects=False,
        )

    def _stream_client(self) -> httpx.AsyncClient:
        """Client factory for a streamed call: connect-bounded, read-unbounded."""
        return self._client(self._timeouts.stream_httpx_timeout())

    # -- request shaping ------------------------------------------------------

    def _headers(self, req: CompletionRequest | None = None) -> dict[str, str]:
        return _build_headers_impl(
            api_key=self._key,
            req=req,
            conversation_header=_DISCO_CONVERSATION_HEADER,
            fallback_session_id=self._session_id,
        )

    @staticmethod
    def _message(m: LLMMessage) -> dict:
        return _message_to_wire_impl(m, sanitize_fn=_sanitize_tool_name)

    def _payload(
        self, req: CompletionRequest, model: str, *, stream: bool, omit_reasoning: bool = False
    ) -> dict:
        policy = self._model_policies.get(model, self._request_policy)
        if omit_reasoning:
            policy = policy.model_copy(
                update={"reasoning_enabled": None, "reasoning_disabled": None}
            )
        if _is_responses_endpoint(self._base):
            body = _build_responses_payload(
                provider_name=self.name,
                req=req,
                model=model,
            )
            intent = _resolve_enable_thinking(req=req, enable_thinking=self._enable_thinking)
            body.update(policy.payload(intent))
            return body
        return _build_payload_impl(
            req=req,
            model=model,
            stream=stream,
            enable_thinking=self._enable_thinking,
            request_policy=policy,
            message_fn=self._message,
            normalize_fn=_normalize_tool_call_ordering,
            truncate_fn=_truncate_think_block,
            sanitize_fn=_sanitize_tool_name,
            prompt_cache_key_fn=self._prompt_cache_key,
            mark_cache_fn=self._mark_cache,
        )

    @staticmethod
    def _prompt_cache_key(req: CompletionRequest) -> str | None:
        return _prompt_cache_key(req)

    @staticmethod
    def _mark_cache(body: dict) -> None:
        from ._request_assembly import _mark_cache

        _mark_cache(body)

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
        *,
        stream_delivery: bool | None = None,
    ) -> None:
        """Provider-request ledger: when DISCO_PROVIDER_LEDGER names a file,
        append one relay-format JSONL record per outbound completion request.
        Best-effort: accounting or ledger failure must never fail a live request.
        Unset env means no shape work.

        ``stream_delivery`` overrides the record's ``stream`` field with the
        CALL KIND rather than the wire flag. ``complete`` now rides the streamed
        transport but still delivers one buffered result, and the ledger's
        downstream consumers read that field as "did the caller stream?"."""
        if not provider_ledger_enabled():
            return
        raw_cid = (req.metadata or {}).get("conversation_id")
        raw_purpose = (req.metadata or {}).get("provider_ledger_purpose")
        raw_call_kind = (req.metadata or {}).get("provider_ledger_call_kind")
        try:
            request_shape = _provider_request_shape(req, payload)
            if request_shape is not None and stream_delivery is not None:
                request_shape = replace(request_shape, stream=stream_delivery)
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
        """Side-effect-free budget preview. No ledger record, no network I/O.

        Shapes the payload exactly as ``complete`` will send it — both ride the
        streamed transport — so the estimate describes the real request."""
        return preview_request_budget(
            lambda: _provider_request_shape(req, self._payload(req, model, stream=True))
        )

    def _raise_typed_streamed(self, status: int, body_text: str) -> None:
        """Classify a non-2xx on the STREAMED request.

        A refusal of streaming itself becomes a private control signal so
        ``complete`` can retry the call once buffered. Every other status is
        classified and raised exactly as on the buffered path.
        """
        try:
            self._raise_typed(status, body_text)
        except LLMError as exc:
            if _is_stream_rejection(status, body_text) and (
                type(exc) is LLMError or (status == 501 and type(exc) is LLMTransientError)
            ):
                raise _StreamRejected(status) from exc
            raise

    def _streamed(
        self,
        req: CompletionRequest,
        model: str,
        payload: dict,
        *,
        raise_typed_fn: Callable[[int, str], None],
    ) -> AsyncIterator[StreamChunk]:
        """The one streamed transport both public entry points ride."""
        return _stream_complete_impl(
            base_url=self._base,
            provider_name=self.name,
            payload=payload,
            headers=self._headers(req),
            req=req,
            model=model,
            client_factory=self._stream_client,
            raise_typed_fn=raise_typed_fn,
            tool_calls_fn=self._tool_calls,
            recovery_fn=recover_tool_calls,
            cached_tokens_fn=self._cached_tokens,
            metadata_fn=self._empty_reasoning_only_metadata,
            map_finish_fn=_map_finish,
            timeouts=self._timeouts,
        )

    async def _complete_buffered(
        self, req: CompletionRequest, *, model: str, omit_reasoning: bool = False
    ) -> CompletionResponse:
        """One buffered POST — the only path left with a wall-clock ceiling.

        Used by the Responses API (which this adapter does not stream) and by
        the one-shot fallback for a server that refuses to stream this request.

        There are no chunks to observe here, so the progress seam gets ONE
        report at call start instead: without it the caller waits out the whole
        call with nothing observed, and a UI reading that silence calls a
        working driver stalled.
        """
        payload = (
            self._payload(req, model, stream=False, omit_reasoning=True)
            if omit_reasoning
            else self._payload(req, model, stream=False)
        )
        self._ledger_emit(req, model, payload)
        return await buffered_complete(
            req=req,
            model=model,
            base_url=self._base,
            provider_name=self.name,
            payload=payload,
            headers=self._headers(req),
            client_factory=self._client,
            raise_typed_fn=rejection_handler(
                self._raise_typed,
                payload,
                baseline=lambda: self._payload(req, model, stream=False, omit_reasoning=True),
                allow_fallback=not omit_reasoning,
            ),
            to_response=self._to_response,
        )

    async def _complete_once(
        self, req: CompletionRequest, *, model: str, omit_reasoning: bool = False
    ) -> CompletionResponse:
        """Buffered result, streamed transport.

        The delivered contract is unchanged — one ``CompletionResponse`` with
        content, tool calls, finish reason and usage. What changes is that the
        adapter can now tell a working model from a hung one: bytes arriving
        keep the call alive for as long as the model needs, while silence past
        the idle budget is a stall the router may correctly retry.
        """
        if _is_responses_endpoint(self._base):
            return await self._complete_buffered(req, model=model, omit_reasoning=omit_reasoning)
        payload = (
            self._payload(req, model, stream=True, omit_reasoning=True)
            if omit_reasoning
            else self._payload(req, model, stream=True)
        )
        self._ledger_emit(req, model, payload, stream_delivery=False)
        final: CompletionResponse | None = None
        try:
            async for chunk in self._streamed(
                req,
                model,
                payload,
                raise_typed_fn=rejection_handler(
                    self._raise_typed_streamed,
                    payload,
                    baseline=lambda: self._payload(req, model, stream=True, omit_reasoning=True),
                    allow_fallback=not omit_reasoning,
                ),
            ):
                if chunk.done and chunk.final is not None:
                    final = chunk.final
        except _StreamRejected as rejected:
            _LOG.warning(
                "provider %s refused the streamed request (HTTP %d); "
                "falling back once to a buffered call",
                self.name,
                rejected.status,
            )
            return await self._complete_buffered(req, model=model, omit_reasoning=omit_reasoning)
        if final is None:
            raise LLMTransientError(
                "provider stream ended without a final result", provider=self.name
            )
        return final

    async def complete(self, req: CompletionRequest, *, model: str) -> CompletionResponse:
        async def attempt(request: CompletionRequest, omit: bool) -> AsyncIterator[StreamChunk]:
            response = await self._complete_once(request, model=model, omit_reasoning=omit)
            yield StreamChunk(done=True, final=response)

        async for chunk in with_reasoning_fallback(req, attempt):
            if chunk.final is not None:
                return chunk.final
        raise LLMTransientError("provider returned no final result", provider=self.name)

    async def stream_complete(
        self, req: CompletionRequest, *, model: str
    ) -> AsyncIterator[StreamChunk]:
        if _is_responses_endpoint(self._base):
            response = await self.complete(req.model_copy(update={"stream": False}), model=model)
            if response.text:
                yield StreamChunk(delta_text=response.text)
            yield StreamChunk(done=True, final=response)
            return

        async def attempt(request: CompletionRequest, omit: bool) -> AsyncIterator[StreamChunk]:
            payload = (
                self._payload(request, model, stream=True, omit_reasoning=True)
                if omit
                else self._payload(request, model, stream=True)
            )
            self._ledger_emit(request, model, payload)
            async for chunk in self._streamed(
                request,
                model,
                payload,
                raise_typed_fn=rejection_handler(
                    self._raise_typed,
                    payload,
                    baseline=lambda: self._payload(
                        request, model, stream=True, omit_reasoning=True
                    ),
                    allow_fallback=not omit,
                ),
            ):
                yield chunk

        async for chunk in with_reasoning_fallback(req, attempt):
            yield chunk

    def supports(self, requirement: Requirement, *, model: str) -> bool:
        # Advisory metadata (router §8 / security principle 8): informs assignment,
        # does not gate. The provider sends the request as assigned regardless.
        return requirement in self._caps
