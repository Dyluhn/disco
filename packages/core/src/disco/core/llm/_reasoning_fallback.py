"""One request-local retry without optional reasoning controls, before output."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable

from .errors import LLMError
from .types import CompletionRequest, StreamChunk

_LOG = logging.getLogger(__name__)


class OptionalControlsRejected(Exception):  # noqa: N818 - internal control signal
    def __init__(self, status: int, original: LLMError) -> None:
        super().__init__(f"optional controls retry after HTTP {status}")
        self.status = status
        self.original = original


def rejection_handler(
    classify: Callable[[int, str], None],
    payload: dict,
    *,
    baseline: Callable[[], dict],
    allow_fallback: bool,
) -> Callable[[int, str], None]:
    """Keep typed failures intact; only a generic shape rejection earns a retry.

    A 400/422 does not prove which option failed. The successful baseline retry
    is the useful result; no endpoint capability is inferred or cached.
    The baseline is computed only after rejection, never for successful calls.
    """

    def on_error(status: int, body: str) -> None:
        try:
            classify(status, body)
        except LLMError as exc:
            if (
                allow_fallback
                and status in (400, 422)
                and type(exc) is LLMError
                and payload != baseline()
            ):
                raise OptionalControlsRejected(status, exc) from exc
            raise

    return on_error


async def with_reasoning_fallback(
    req: CompletionRequest,
    attempt: Callable[[CompletionRequest, bool], AsyncIterator[StreamChunk]],
) -> AsyncIterator[StreamChunk]:
    emitted = False
    try:
        async for chunk in attempt(req, False):
            emitted = True
            yield chunk
        return
    except OptionalControlsRejected as exc:
        if emitted:
            raise exc.original from exc
        status = exc.status

    # Honour cancellation before starting another paid request. This is local
    # to this call, never a mutation of a shared provider or a capability cache.
    await asyncio.sleep(0)
    _LOG.warning("Retrying without optional reasoning controls after HTTP %d", status)
    details = {"reasoning_control_fallback": True}
    retry = req.model_copy(update={"metadata": {**(req.metadata or {}), **details}})
    async for chunk in attempt(retry, True):
        if chunk.final is not None:
            final = chunk.final.model_copy(
                update={"response_metadata": {**chunk.final.response_metadata, **details}}
            )
            chunk = chunk.model_copy(update={"final": final})
        yield chunk
