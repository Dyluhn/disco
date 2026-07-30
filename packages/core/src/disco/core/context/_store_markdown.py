"""ArtifactMemoryStore markdown-kind collaborators.

Extracted from ``store.py`` so the store class stays under the public-method
limit. These functions implement the markdown (narrative) kind read/write
operations over a ``WorkspaceFS``.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Protocol

from .artifact_memory import ArtifactMemoryKind, ArtifactMemoryRef


class _WritableFS(Protocol):
    async def write_file(self, path: str, data: bytes) -> None: ...


_PathFor = Callable[[ArtifactMemoryKind], str]
_RefFactory = Callable[[ArtifactMemoryKind, str], ArtifactMemoryRef]
_ReadOptional = Callable[[str], Awaitable[bytes | None]]


async def write_markdown_kind(
    fs: _WritableFS,
    kind: ArtifactMemoryKind,
    text: str,
    md_kinds: frozenset[ArtifactMemoryKind],
    path_for_fn: _PathFor,
    ref_fn: _RefFactory,
) -> ArtifactMemoryRef:
    """Write a markdown-kind file."""
    if kind not in md_kinds:
        raise ValueError(f"{kind.value} is not a markdown kind")
    path = path_for_fn(kind)
    body = text if text.endswith("\n") else text + "\n"
    await fs.write_file(path, body.encode("utf-8"))
    return ref_fn(kind, path)


async def read_markdown_kind(
    kind: ArtifactMemoryKind,
    md_kinds: frozenset[ArtifactMemoryKind],
    path_for_fn: _PathFor,
    read_opt_fn: _ReadOptional,
) -> str | None:
    """Read a markdown-kind file."""
    if kind not in md_kinds:
        raise ValueError(f"{kind.value} is not a markdown kind")
    data = await read_opt_fn(path_for_fn(kind))
    if data is None:
        return None
    text = data.decode("utf-8", errors="replace").rstrip("\n")
    return text or None


async def write_design_direction_tokens(
    fs: _WritableFS,
    base: str,
    css: str,
    ref_fn: _RefFactory,
) -> ArtifactMemoryRef:
    """Write the mechanical tokens.css companion."""
    path = f"{base}/direction_tokens.css"
    body = css if css.endswith("\n") else css + "\n"
    await fs.write_file(path, body.encode("utf-8"))
    return ref_fn(ArtifactMemoryKind.DESIGN_DIRECTION, path)


async def read_design_direction_tokens(
    base: str,
    read_opt_fn: _ReadOptional,
) -> str | None:
    """Read the committed direction's mechanical CSS companion."""
    data = await read_opt_fn(f"{base}/direction_tokens.css")
    if data is None:
        return None
    text = data.decode("utf-8", errors="replace")
    return text if text else None


async def write_summary(
    fs: _WritableFS,
    base: str,
    range_id: str,
    summary: str,
    ref_fn: _RefFactory,
) -> ArtifactMemoryRef:
    """Write a per-range durable summary."""
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", range_id).strip("._")
    if not safe_id:
        raise ValueError("range_id must contain at least one safe path character")
    path = f"{base}/summary/{safe_id}.md"
    body = summary if summary.endswith("\n") else summary + "\n"
    await fs.write_file(path, body.encode("utf-8"))
    return ref_fn(ArtifactMemoryKind.SUMMARY, path)
