"""Design-direction brand bridge for deck renderers."""

from __future__ import annotations

from disco.core.brand.tokens import Theme
from disco.core.context import ArtifactMemoryStore
from disco.core.design import direction_from_markdown, to_brand_tokens

from ..anatomy import ToolContext


async def direction_brand_override(ctx: ToolContext) -> Theme | None:
    """Read the committed direction context and map it to brand Theme tokens."""

    if ctx.sandbox is None:
        return None
    try:
        markdown = await ArtifactMemoryStore(ctx.sandbox).read_design_direction()
    except Exception:
        return None
    direction = direction_from_markdown(markdown or "")
    return to_brand_tokens(direction) if direction is not None else None


__all__ = ["direction_brand_override"]
