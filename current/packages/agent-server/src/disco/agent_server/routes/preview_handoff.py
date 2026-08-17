"""Typed preview capability handoff boundary."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Literal, cast

from disco.core.auth import MAX_PREVIEW_TARGET_PATH_CHARS
from disco.core.store.sqlite import SqliteEventStore
from fastapi import Request, Response
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from disco.core.auth import PreviewCapabilitySigner

    from ..runtime import ConversationRuntime


class PreviewCapabilityBody(BaseModel):
    port: int | None = None
    target_path: str = Field("/", max_length=MAX_PREVIEW_TARGET_PATH_CHARS)
    transport: Literal["host", "path", "path_live", "canonical"] = "host"
    workspace_version: int | None = Field(default=None, ge=1)


CaptureLease = Callable[[str], AbstractAsyncContextManager[None]]
CaptureBegin = Callable[[str], int]


def _capture_hooks(runtime: object) -> tuple[CaptureBegin | None, CaptureLease | None]:
    preview = getattr(runtime, "preview", None)
    begin = getattr(preview, "begin_capture", None)
    lease = getattr(preview, "capture_lease", None)
    return (
        cast(CaptureBegin, begin) if callable(begin) else None,
        cast(CaptureLease, lease) if callable(lease) else None,
    )


async def preview_capability_response(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    signer: PreviewCapabilitySigner,
    conversation_id: str,
    body: PreviewCapabilityBody,
    request: Request,
) -> Response:
    """Mint a capability while retaining the finished preview's sandbox owner."""
    # Import lazily: the unleased implementation owns capability selection and
    # imports this module for the body type.
    from .preview_capability import _preview_capability_response_unleased

    async def render(capture_generation: int | None) -> Response:
        return await _preview_capability_response_unleased(
            store,
            runtime,
            signer,
            conversation_id,
            body,
            request,
            capture_generation,
        )

    if runtime is None:
        return await render(None)
    begin_capture, capture_lease = _capture_hooks(runtime)
    if capture_lease is None:
        # Narrow compatibility for route doubles that intentionally model only
        # capability selection; the composed runtime always supplies the lease.
        return await render(None)
    capture_generation = begin_capture(conversation_id) if begin_capture is not None else None
    async with capture_lease(conversation_id):
        return await render(capture_generation)
