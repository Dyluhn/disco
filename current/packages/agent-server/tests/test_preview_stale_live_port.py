"""Regression coverage for FINISHED canonical Preview port selection."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from _preview_manager_fakes import _DependencySession
from disco.agent_server import ConversationRuntime
from disco.agent_server.preview_service import PreviewService
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
        self.preserve_exact_calls: list[bool] = []
        self.restore = True

    def preview_target_port(self, _conversation_id: str) -> int:
        return self.port

    async def ensure_preview(
        self,
        _conversation_id: str,
        *,
        preserve_exact_finished: bool = False,
    ) -> bool:
        self.ensure_calls += 1
        self.preserve_exact_calls.append(preserve_exact_finished)
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
    assert runtime.preview.preserve_exact_calls == ([True] if ensure_calls else [])


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
    assert runtime.preview.preserve_exact_calls == [True]


async def test_sealed_node_dependency_restore_requires_a_real_directory() -> None:
    with pytest.raises(RuntimeError, match="did not produce node_modules"):
        await PreviewService._prepare_sealed_node_dependencies(
            _DependencySession(produce_dependency_dir=False),
            SimpleNamespace(command=None, framework="vite", cwd=None),
        )


async def test_sealed_node_dependency_restore_skips_when_package_json_absent() -> None:
    class _AbsentPackageSession:
        async def file_exists(self, path: str) -> bool:
            assert path == "package.json"
            return False

        async def read_file(self, path: str) -> bytes:  # pragma: no cover
            raise AssertionError(path)

    assert (
        await PreviewService._prepare_sealed_node_dependencies(
            _AbsentPackageSession(),
            SimpleNamespace(command="node server.js", framework=None, cwd=None),
        )
        is None
    )


async def test_sealed_node_dependency_restore_rejects_malformed_package_json() -> None:
    class _MalformedSession:
        async def file_exists(self, path: str) -> bool:
            assert path == "package.json"
            return True

        async def read_file(self, path: str) -> bytes:
            assert path == "package.json"
            return b"{not-json"

    with pytest.raises(RuntimeError, match="missing a valid package\\.json"):
        await PreviewService._prepare_sealed_node_dependencies(
            _MalformedSession(),
            SimpleNamespace(command="node server.js", framework=None, cwd=None),
        )
