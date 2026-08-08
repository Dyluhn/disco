"""Regression coverage for FINISHED canonical Preview port selection."""

from __future__ import annotations

from typing import Any, cast

import pytest
from disco.agent_server import ConversationRuntime
from disco.agent_server.routes.preview_capability import (
    PreviewCapabilityBody,
    _selected_capability_port,
)
from disco.agent_server.routes.preview_finished import refresh_canonical_preview_port
from disco.core import ConversationStatus, SqliteEventStore, StatusEvent


class _Store:
    def __init__(self, status: ConversationStatus) -> None:
        self._status = status

    async def get_events(self, _conversation_id: str) -> list[StatusEvent]:
        return [StatusEvent(status=self._status)]


class _Preview:
    def __init__(self) -> None:
        self.port = 8000
        self.ensure_calls = 0
        self.restore = True

    def preview_target_port(self, _conversation_id: str) -> int:
        return self.port

    async def ensure_preview(self, _conversation_id: str) -> bool:
        self.ensure_calls += 1
        if self.restore:
            self.port = 8080
        return self.restore


class _Runtime:
    def __init__(self) -> None:
        self.preview = _Preview()


@pytest.mark.parametrize(
    ("status", "workspace_version", "expected_port", "ensure_calls"),
    (
        (ConversationStatus.FINISHED, None, 8080, 1),
        (ConversationStatus.FINISHED, 1, 8000, 0),
        (ConversationStatus.RUNNING, None, 8000, 0),
    ),
)
async def test_finished_current_canonical_replaces_a_stale_live_port_before_mint(
    status: ConversationStatus,
    workspace_version: int | None,
    expected_port: int,
    ensure_calls: int,
) -> None:
    runtime = _Runtime()
    selected = await _selected_capability_port(
        cast(SqliteEventStore, cast(Any, _Store(status))),
        cast(ConversationRuntime, cast(Any, runtime)),
        "conv_stale_live_port",
        PreviewCapabilityBody(
            transport="canonical",
            workspace_version=workspace_version,
        ),
    )

    assert selected == expected_port
    assert runtime.preview.ensure_calls == ensure_calls


async def test_finished_current_canonical_refuses_a_stale_port_when_restore_fails() -> None:
    runtime = _Runtime()
    runtime.preview.restore = False

    selected = await refresh_canonical_preview_port(
        cast(SqliteEventStore, cast(Any, _Store(ConversationStatus.FINISHED))),
        cast(ConversationRuntime, cast(Any, runtime)),
        "conv_stale_live_port",
        8000,
        canonical=True,
        current=True,
    )

    assert selected is None
    assert runtime.preview.ensure_calls == 1
