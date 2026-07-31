"""First-unmet-condition scan across a deliverable's text candidates.

`_ContentGateMixin._first_dictated_content_miss` is a thin, `self`-bound
wrapper around `first_dictated_content_miss` below — the scan itself only
needs a byte-reader callback, not the mixin's other collaborators, so it is
a plain function.
"""

from __future__ import annotations

import logging
import posixpath
from collections.abc import Awaitable, Callable

from ...plan_conditions import DictatedContentCondition
from .errors import _DictatedContentInspectionIncomplete


def is_text_candidate(path: str, data: bytes, *, binary_suffixes: frozenset[str]) -> bool:
    """Whether bytes may safely participate in a user-visible text floor.

    Known binary extensions are always excluded. Unknown extensions must still
    be valid NUL-free UTF-8, which protects binary artifacts without preventing
    extensionless or uncommon text deliverables from carrying dictated copy.
    Empty non-binary files remain candidates so missing content fails loudly.
    """

    if posixpath.splitext(path)[1].lower() in binary_suffixes:
        return False
    if not data:
        return True
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return False
    return True


async def first_dictated_content_miss(
    conditions: list[DictatedContentCondition],
    paths: list[str],
    *,
    strict: bool,
    read_bytes: Callable[[str], Awaitable[bytes | None]],
    max_file_bytes: int,
    max_total_bytes: int,
    binary_suffixes: frozenset[str],
    log: logging.Logger,
) -> tuple[DictatedContentCondition, list[str]] | None:
    unresolved = list(conditions)
    checked: list[str] = []
    total_bytes = 0
    for path in paths:
        if posixpath.splitext(path)[1].lower() in binary_suffixes:
            continue
        data = await read_bytes(path)
        if data is None:
            continue
        if len(data) > max_file_bytes:
            if not strict:
                continue
            raise _DictatedContentInspectionIncomplete(
                "a text deliverable exceeds the per-file inspection budget"
            )
        total_bytes += len(data)
        if total_bytes > max_total_bytes:
            if not strict:
                log.warning("dictated-content files exceed total inspection budget")
                return None
            raise _DictatedContentInspectionIncomplete(
                "the app bundle exceeds the total byte inspection budget"
            )
        if not is_text_candidate(path, data, binary_suffixes=binary_suffixes):
            continue
        checked.append(path)
        unresolved = [
            condition
            for condition in unresolved
            if condition.literal.encode("utf-8", "surrogatepass") not in data
        ]
        if not unresolved:
            return None
    if not checked:
        if not strict:
            log.warning(
                "dictated-content conditions present but no readable non-app "
                "deliverable surface is available; skipping the compatibility gate"
            )
            return None
        raise _DictatedContentInspectionIncomplete(
            "no readable text deliverable surface was available"
        )
    return (unresolved[0], checked) if unresolved else None
