"""The host-owned Reference Pack writer capability.

``create_reference_pack`` is the one mutation the agent has on the user's
reusable Reference Pack library: it copies validated workspace files into a new
pack under the ACTIVE projects root. The tool never sees that root; the runtime
injects a narrow async callable into ``ToolContext`` (the release-intent writer
pattern). Absent the capability — a standalone executor — the tool fails closed
and persists nothing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


class ReferencePackWriteError(Exception):
    """Typed, fail-closed failure; ``code`` is model-facing, the message is
    root/path/secret-free."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


#: ``(owner_id, name, description, files) -> pack summary`` where ``files`` is the
#: ordered ``[(name, bytes), …]`` copied verbatim into the pack.
ReferencePackWriter = Callable[
    [str, str, str, list[tuple[str, bytes]]],
    Awaitable[dict[str, Any]],
]
