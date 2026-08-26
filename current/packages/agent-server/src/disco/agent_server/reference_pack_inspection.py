"""Bounded inspection of pinned Reference Pack snapshots."""

from __future__ import annotations

import inspect as inspect_lib
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Protocol

from .reference_pack_binding import ReferencePackBinding, ReferencePackBindingStore
from .reference_pack_store import ReferencePackError


class VisualObserver(Protocol):
    def __call__(
        self,
        data: bytes,
        file_name: str,
        question: str,
    ) -> object | Awaitable[object]: ...


@dataclass(frozen=True)
class ReferenceInspection:
    status: str
    pack_id: str
    file_name: str
    content: str
    media_type: str
    truncated: bool = False
    visual: object | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "pack_id": self.pack_id,
            "file_name": self.file_name,
            "content": self.content,
            "media_type": self.media_type,
            "truncated": self.truncated,
            "visual": self.visual,
        }


_TEXT_TYPES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
        "application/xml",
        "text/xml",
        "text/html",
        "text/css",
        "text/javascript",
        "application/javascript",
    }
)
_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"})


def _base_media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


async def _call_observer(
    observer: VisualObserver, *, data: bytes, file_name: str, question: str
) -> object:
    """Call the host visual broker's single stable callback signature."""

    value = observer(data, file_name, question)
    if inspect_lib.isawaitable(value):
        return await value
    return value


class ReferenceInspector:
    def __init__(
        self, snapshots: ReferencePackBindingStore, *, max_text_chars: int = 12_000
    ) -> None:
        self._snapshots = snapshots
        self._max_text_chars = max_text_chars

    async def inspect(
        self,
        binding: ReferencePackBinding,
        *,
        pack_id: str,
        file_name: str,
        question: str = "",
        visual_observer: VisualObserver | None = None,
    ) -> ReferenceInspection:
        pack = next((item for item in binding.packs if item.pack_id == pack_id), None)
        if pack is None:
            raise ReferencePackError("reference_inspect can only read a pinned pack")
        file = next((item for item in pack.files if item.name == file_name), None)
        if file is None:
            raise ReferencePackError("reference_inspect can only read a pinned file")
        data = self._snapshots.read_file(binding, pack_id, file_name)
        media_type = _base_media_type(file.media_type)
        if media_type in _TEXT_TYPES or media_type.startswith("text/"):
            text = data.decode("utf-8", errors="replace")
            truncated = len(text) > self._max_text_chars
            if truncated:
                text = text[: self._max_text_chars]
            return ReferenceInspection("text", pack_id, file_name, text, media_type, truncated)
        if media_type in _IMAGE_TYPES and visual_observer is not None:
            result = await _call_observer(
                visual_observer,
                data=data,
                file_name=file_name,
                question=question,
            )
            return ReferenceInspection("visual", pack_id, file_name, "", media_type, visual=result)
        # A binary asset is not silently described.  This is intentionally the
        # exact truthful fallback surfaced to the model/UI when no vision route exists.
        return ReferenceInspection("asset_only", pack_id, file_name, "Asset only", media_type)


async def reference_inspect(
    binding: ReferencePackBinding,
    *,
    snapshots: ReferencePackBindingStore,
    pack_id: str,
    file_name: str,
    question: str = "",
    visual_observer: VisualObserver | None = None,
    max_text_chars: int = 12_000,
) -> dict[str, Any]:
    result = await ReferenceInspector(snapshots, max_text_chars=max_text_chars).inspect(
        binding,
        pack_id=pack_id,
        file_name=file_name,
        question=question,
        visual_observer=visual_observer,
    )
    return result.as_dict()


__all__ = ["ReferenceInspection", "ReferenceInspector", "VisualObserver", "reference_inspect"]
