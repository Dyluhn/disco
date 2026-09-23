"""One bounded buffered HTTP attempt, shared by Responses and stream fallback."""

from collections.abc import Callable

import httpx

from ._provider_retry import attach_retry_after
from ._responses_api import decode_responses_response, is_responses_endpoint
from .errors import LLMTransientError
from .stream_progress import reporter_for
from .types import CompletionRequest, CompletionResponse


async def buffered_complete(
    *,
    req: CompletionRequest,
    model: str,
    base_url: str,
    provider_name: str,
    payload: dict,
    headers: dict[str, str],
    client_factory: Callable[[], httpx.AsyncClient],
    raise_typed_fn: Callable[[int, str], None],
    to_response: Callable[[CompletionRequest, str, dict], CompletionResponse],
) -> CompletionResponse:
    reporter = reporter_for(req.metadata)
    if reporter is not None:
        await reporter.buffered_start()
    endpoint = base_url if is_responses_endpoint(base_url) else f"{base_url}/chat/completions"
    try:
        async with client_factory() as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise LLMTransientError(
            f"request timed out ({type(exc).__name__})", provider=provider_name
        ) from exc
    except httpx.HTTPError as exc:
        raise LLMTransientError(
            f"connection error ({type(exc).__name__})", provider=provider_name
        ) from exc
    if resp.status_code >= 400:
        try:
            raise_typed_fn(resp.status_code, resp.text)
        except LLMTransientError as exc:
            attach_retry_after(exc, resp.headers)
            raise
    if is_responses_endpoint(base_url):
        return decode_responses_response(req=req, model=model, data=resp.json())
    return to_response(req, model, resp.json())
