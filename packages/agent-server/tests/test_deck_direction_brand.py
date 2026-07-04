"""Deck export direction-brand helper."""

from __future__ import annotations

from typing import Any, cast

from disco.agent_server.routes.deck_editor import _direction_brand_override
from disco.core.design import DIRECTION_BY_ID, render_design_direction


class _Session:
    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files

    async def read_file(self, path: str) -> bytes:
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]


class _Runtime:
    def __init__(self, session: _Session | None) -> None:
        self._session = session

    def live_session(self, conversation_id: str) -> _Session | None:
        return self._session

    def project_store(self) -> None:
        return None


async def test_direction_brand_override_reads_committed_context() -> None:
    markdown = render_design_direction(DIRECTION_BY_ID["warm-craft"])
    runtime = _Runtime(
        _Session({".disco/context/design_direction.md": markdown.encode("utf-8")})
    )

    theme = await _direction_brand_override(cast(Any, runtime), "conv")

    assert theme is not None
    assert theme.name == "direction-warm-craft"
    assert theme.accent == "#8a5a44"
