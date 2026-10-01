"""Public mutable Preview launches must not keep serving a sealed workspace."""

from __future__ import annotations

from typing import Any

import pytest
from disco.agent_server.preview_manager import PreviewManager
from disco.tools.anatomy import ToolContext
from disco.tools.builtin.preview import (
    PreviewStartArgs,
    PreviewStartTool,
    PreviewStatusArgs,
    PreviewStatusTool,
)
from pydantic import ValidationError
from test_preview_resource_contract import _FakeSandbox, _sealed_contract

HOST = b"<h1>mutable host</h1>"
EDITED = b"<h1>later host edit</h1>"
SEALED = b"<h1>sealed revision</h1>"


class _BytesSandbox(_FakeSandbox):
    def __init__(self, content: bytes, *, mutable: bool = False) -> None:
        super().__init__()
        if mutable:
            self.id = "sbx_mutable_handoff_host"
        self.files = {"index.html": content}
        self.healthy = True
        self._preview_manager: Any = None

    async def fetch_inside(self, port, path, *, timeout_s=5):
        if not self.healthy or port not in self._serving:
            return None
        return 200, self.files["index.html"], "text/html"


def _ctx(host: _BytesSandbox) -> ToolContext:
    return ToolContext(
        sandbox=host,
        workspace_path="/workspace",
        timeout_s=30,
        capabilities=None,
        owner_id="local",
        conversation_id="conv_preview_resource",
    )


async def _restore():
    host, sealed = _BytesSandbox(HOST, mutable=True), _BytesSandbox(SEALED)
    old = PreviewManager(
        sealed,
        owns_sandbox=True,
        port_pool=[3000, 5173],
        health_attempts=1,
        health_interval_s=0,
    )
    restored = await old.restore_sealed(_sealed_contract(1, "a"))
    assert restored is not None
    # This attachment matches the real SealedPreviewRestorer._launch boundary.
    host._preview_manager = old
    return host, sealed, old, restored


async def _close(host: _BytesSandbox, old: PreviewManager) -> None:
    selected = host._preview_manager
    await old.aclose()
    if selected is not old:
        await selected.aclose()


async def test_explicit_start_serves_host_and_future_edits_after_sealed_restore():
    host, sealed, old, restored = await _restore()
    try:
        before = await sealed.fetch_inside(restored.port, "/")
        assert before is not None and before[1] == SEALED
        outcome = await PreviewStartTool().run(
            PreviewStartArgs(serve_dir=".", name="web"), _ctx(host)
        )
        assert outcome.success and outcome.structured is not None
        selected = host._preview_manager
        assert selected._sandbox is host and not selected.owns_sandbox()
        current = selected.canonical_session()
        assert current is not None
        hit = await host.fetch_inside(current.port, "/")
        assert hit is not None and hit[1] == HOST
        host.files["index.html"] = EDITED
        edited = await host.fetch_inside(current.port, "/")
        assert edited is not None and edited[1] == EDITED
        assert sealed.files["index.html"] == SEALED
        assert host.destroy_calls == 0
        # The retired server may be closed: immutable file bytes must stay fixed.
    finally:
        await _close(host, old)


async def test_failed_mutable_health_keeps_old_sealed_preview_routable():
    host, sealed, old, restored = await _restore()
    host.healthy = False
    try:
        outcome = await PreviewStartTool().run(
            PreviewStartArgs(serve_dir=".", name="web"), _ctx(host)
        )
        assert not outcome.success
        assert host._preview_manager is old
        assert old.canonical_session() is restored
        hit = await sealed.fetch_inside(restored.port, "/")
        assert hit is not None and hit[1] == SEALED
        assert sealed.destroy_calls == host.destroy_calls == 0
        assert not host._serving
    finally:
        await _close(host, old)


async def test_existing_nonisolated_host_launch_keeps_host_ownership():
    host = _BytesSandbox(HOST, mutable=True)
    manager = PreviewManager(
        host,
        port_pool=[3000, 5173],
        health_attempts=1,
        health_interval_s=0,
    )
    host._preview_manager = manager
    try:
        outcome = await PreviewStartTool().run(
            PreviewStartArgs(serve_dir=".", name="web"), _ctx(host)
        )
        assert outcome.success and outcome.structured is not None
        assert host._preview_manager is manager and not manager.owns_sandbox()
        hit = await host.fetch_inside(outcome.structured["port"], "/")
        assert hit is not None and hit[1] == HOST
    finally:
        await manager.aclose()
    assert host.destroy_calls == 0


async def test_readonly_status_keeps_restored_sealed_manager_selected():
    host, sealed, old, restored = await _restore()
    try:
        outcome = await PreviewStatusTool().run(PreviewStatusArgs(), _ctx(host))
        assert outcome.success and outcome.structured is not None
        assert host._preview_manager is old
        assert old.canonical_session() is restored
        assert not host._serving and sealed.destroy_calls == host.destroy_calls == 0
        hit = await sealed.fetch_inside(restored.port, "/")
        assert hit is not None and hit[1] == SEALED
    finally:
        await _close(host, old)


async def test_no_intent_and_explicit_port_guards_remain_closed():
    host = _BytesSandbox(HOST, mutable=True)
    outcome = await PreviewStartTool().run(PreviewStartArgs(), _ctx(host))
    assert not outcome.success and outcome.error == "no_intent"
    assert host._preview_manager is None and not host._serving
    with pytest.raises(ValidationError):
        PreviewStartArgs(serve_dir=".", port=3000)


@pytest.mark.parametrize(
    "install_failure",
    ["", "dependency install failed"],
    ids=["prepared-host", "preparation-failure"],
)
async def test_appkit_sync_uses_mutable_host_or_preserves_old_on_preparation_failure(
    monkeypatch, install_failure
):
    from unittest.mock import AsyncMock

    from disco.agent_server import build_loop_components

    host, sealed, old, restored = await _restore()
    preparation = AsyncMock(
        return_value=build_loop_components._PreviewPreparation(install_failure=install_failure)
    )
    monkeypatch.setattr(build_loop_components, "_prepare_appkit_dependencies", preparation)
    try:
        await build_loop_components._sync_appkit_live_preview(host)
        preparation.assert_awaited_once_with(host)
        selected = host._preview_manager
        if install_failure:
            assert selected is old and old.canonical_session() is restored
            assert install_failure in restored.update_error
            hit = await sealed.fetch_inside(restored.port, "/")
            assert hit is not None and hit[1] == SEALED
            assert not host._serving and sealed.destroy_calls == 0
        else:
            assert selected._sandbox is host and not selected.owns_sandbox()
            current = selected.canonical_session()
            assert current is not None
            hit = await host.fetch_inside(current.port, "/")
            assert hit is not None and hit[1] == HOST
            assert sealed.files["index.html"] == SEALED
        assert host.destroy_calls == 0
    finally:
        await _close(host, old)
