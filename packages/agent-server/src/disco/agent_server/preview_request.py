"""Request transport bound to an existing Preview resource generation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from disco.tools.sandbox.session_parts.request_inside import request_inside

from .preview_projection import SealedPreviewRuntimeContract

if TYPE_CHECKING:
    from .preview_resource import PreviewResource


async def request_finished_inside(
    resource: PreviewResource,
    contract: SealedPreviewRuntimeContract,
    port: int,
    method: str,
    target: str,
    headers: list[tuple[str, str]],
    body: bytes,
    *,
    projection_id: str,
) -> tuple[int, list[tuple[str, str]], bytes] | None:
    """Recheck exact runtime after body collection, under the caller's capture lease."""
    resolved = await resource.resolve_active_projection(contract.projection)
    if resolved is None:
        resolved = await resource.resolve_sealed_contract(contract)
    if resolved is None or resolved.port != port or resolved.projection_id != projection_id:
        return None
    return await request_inside(resource._sandbox, port, method, target, headers, body)
