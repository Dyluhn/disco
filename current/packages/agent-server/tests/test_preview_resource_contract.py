"""Canonical Preview resource contracts independent of implementation layout."""

from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from disco.agent_server import preview_sealed_restore
from disco.agent_server.preview_manager import (
    NoPreviewPortAvailableError,
    PreviewManager,
    PreviewStatus,
)
from disco.agent_server.preview_projection import (
    ActiveLivePreviewProjection,
    SealedPreviewRuntimeContract,
)
from disco.agent_server.preview_runtime_projection import PreviewRuntimeProjection
from disco.agent_server.preview_sealed_restore import SealedPreviewRestorer
from disco.agent_server.preview_service import PreviewService
from disco.agent_server.preview_status import empty_preview_metadata, managed_preview_metadata

_PORT_RE = re.compile(r"(?:http\.server\s+|--port[= ]|-p[= ]|PORT=)(\d+)")


@dataclass
class _View:
    running: bool
    output: str = ""


class _FakeSessions:
    def __init__(self, serving: set[int]) -> None:
        self.namespace = ""
        self._serving = serving
        self._running: dict[str, bool] = {}
        self._name_port: dict[str, int] = {}

    async def exec(self, name: str, command: str, exec_dir: str | None) -> None:
        del exec_dir
        matched = _PORT_RE.search(command)
        if matched is not None:
            port = int(matched.group(1))
            self._name_port[name] = port
            self._serving.add(port)
        self._running[name] = True

    async def view(self, name: str, tail_chars: int = 2000) -> _View:
        del tail_chars
        return _View(running=self._running.get(name, False))

    async def kill_foreground(self, name: str) -> str:
        self._running[name] = False
        port = self._name_port.get(name)
        if port is not None:
            self._serving.discard(port)
        return f"killed {name}"

    async def stop_foreground_server(
        self,
        name: str,
        *,
        expected_command: str,
        expected_port: int,
    ) -> str:
        del expected_command, expected_port
        return await self.kill_foreground(name)


class _FakeSandbox:
    def __init__(
        self,
        *,
        backend_name: str = "gvisor",
        can_expose: bool = True,
    ) -> None:
        self.backend_name = backend_name
        self.id = "sbx_preview_manager_test"
        self.generation = 1
        self.workspace_path = "/workspace"
        self._serving: set[int] = set()
        self.sessions = _FakeSessions(self._serving)
        self._can_expose = can_expose
        self.destroy_calls = 0
        self.fail_destroy = False

    def expose_port(self, port: int) -> str | None:
        return f"http://preview.test/{port}/" if self._can_expose else None

    async def fetch_inside(
        self,
        port: int,
        path: str,
        *,
        timeout_s: int = 5,
    ) -> tuple[int, bytes, str] | None:
        del path, timeout_s
        return (200, b"<html></html>", "text/html") if port in self._serving else None

    async def exec_shell(
        self,
        command: str,
        *,
        timeout_s: int = 5,
    ) -> SimpleNamespace:
        del timeout_s
        if command == "pwd":
            return SimpleNamespace(exit_code=0, stdout="/workspace\n", stderr="")
        raise RuntimeError("bind attribution unavailable in contract fake")

    async def destroy(self) -> None:
        self.destroy_calls += 1
        if self.fail_destroy:
            raise RuntimeError("sandbox termination not confirmed")
        self._serving.clear()
        self.sessions._running = {name: False for name in self.sessions._running}


def _manager(sandbox: _FakeSandbox, *, port_pool: list[int]) -> PreviewManager:
    return PreviewManager(
        sandbox,
        port_pool=port_pool,
        health_attempts=1,
        health_interval_s=0.0,
    )


def _sealed_contract(version_seq: int, marker: str) -> SealedPreviewRuntimeContract:
    projection = ActiveLivePreviewProjection(
        projection_id=f"pv_{marker * 32}",
        session_name="web",
        port=3000,
        launch_kind="custom",
        intent_digest=marker * 64,
        sandbox_instance_id="sbx_preview_manager_test",
        sandbox_generation=1,
        source_action_id=f"action-{marker}",
        source_action_seq=1,
        source_observation_id=f"observation-{marker}",
        source_observation_seq=2,
    )
    return SealedPreviewRuntimeContract(
        contract_id=f"sealed-preview:{marker * 64}",
        conversation_id="conv_preview_resource",
        version_seq=version_seq,
        tree_digest=marker * 64,
        terminal_seq=version_seq * 10,
        app_entry="dist/index.html",
        projection=projection,
        session_name="web",
        serve_dir=None,
        command="python3 server.py",
        framework=None,
        cwd=None,
    )


def test_sealed_runtime_rebinds_source_sandbox_cwd_to_restored_workspace() -> None:
    contract = _sealed_contract(1, "a")
    contract = replace(
        contract,
        cwd="/dev/shm/disco-lane/sbx_test-instance/api",
        projection=replace(contract.projection, sandbox_instance_id="sbx_test-instance"),
    )

    normalized = preview_sealed_restore.normalized_sealed_runtime_contract(contract)

    assert normalized.cwd == "./api"


@pytest.mark.asyncio
async def test_new_committed_revision_revokes_the_stale_resource_binding() -> None:
    sandbox = _FakeSandbox()
    manager = _manager(sandbox, port_pool=[3000, 5173])
    first = _sealed_contract(1, "a")
    second = _sealed_contract(2, "b")

    try:
        assert await manager.restore_sealed(first) is not None
        current = await manager.restore_sealed(second)

        assert current is not None
        assert await manager.resolve_sealed_contract(first) is None
        assert await manager.resolve_sealed_contract(second) is current
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_finished_runtime_accepts_the_exact_still_live_generation() -> None:
    sandbox = _FakeSandbox()
    manager = _manager(sandbox, port_pool=[3000])
    session = await manager.start(command="python3 server.py", name="web", supervise=False)
    current = session.to_dict()
    contract = _sealed_contract(1, "a")
    contract = SealedPreviewRuntimeContract(
        **{
            **contract.__dict__,
            "projection": ActiveLivePreviewProjection(
                projection_id=current["projection_id"],
                session_name=current["name"],
                port=current["port"],
                launch_kind=current["launch_kind"],
                intent_digest=current["intent_digest"],
                sandbox_instance_id=current["sandbox_instance_id"],
                sandbox_generation=current["sandbox_generation"],
                source_action_id="action-live",
                source_action_seq=1,
                source_observation_id="observation-live",
                source_observation_seq=2,
            ),
        }
    )
    # NOT a ConversationRuntime: `PreviewRuntimeProjection` is a peer service that
    # owns methods of the same names (inventory §10.4). Named `projection` so the
    # next receiver-name-driven rewrite cannot mistake it for the runtime.
    projection = PreviewRuntimeProjection(
        SimpleNamespace(live_session=lambda _cid: SimpleNamespace(_preview_manager=manager)),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
    )
    try:
        assert await manager.resolve_sealed_contract(contract) is None
        assert (
            await projection.resolve_finished_preview_runtime(
                contract.conversation_id, contract
            )
            == current
        )
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_finished_restore_reuses_exact_live_generation_before_destructive_rebind() -> None:
    """Capability refresh must not replace the exact host-verified FINISHED runtime."""

    sandbox = _FakeSandbox()
    manager = _manager(sandbox, port_pool=[3000])
    session = await manager.start(command="python3 server.py", name="web", supervise=False)
    current = session.to_dict()
    contract = replace(
        _sealed_contract(1, "a"),
        projection=ActiveLivePreviewProjection(
            projection_id=current["projection_id"],
            session_name=current["name"],
            port=current["port"],
            launch_kind=current["launch_kind"],
            intent_digest=current["intent_digest"],
            sandbox_instance_id=current["sandbox_instance_id"],
            sandbox_generation=current["sandbox_generation"],
            source_action_id="action-live",
            source_action_seq=1,
            source_observation_id="observation-live",
            source_observation_seq=2,
        ),
    )
    service = object.__new__(PreviewService)
    service._runtime_projection = PreviewRuntimeProjection(
        SimpleNamespace(live_session=lambda _cid: SimpleNamespace(_preview_manager=manager)),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
    )
    restore = AsyncMock(return_value=True)
    service._sealed_restorer = SimpleNamespace(restore=restore)
    service._sealed_runtime_contract = lambda *_args: contract  # type: ignore[method-assign]
    committed = object()

    try:
        assert (
            await service._restore_finished(
                contract.conversation_id,
                [],
                committed,
                preserve_exact=True,
            )
            is True
        )
        restore.assert_not_awaited()
        assert manager.canonical_session() is session
        assert session.to_dict() == current

        # The explicit Restart Preview action retains its replacement semantics.
        assert (
            await service._restore_finished(
                contract.conversation_id,
                [],
                committed,
                preserve_exact=False,
            )
            is True
        )
        restore.assert_awaited_once_with(
            contract.conversation_id,
            [],
            committed,
            contract,
        )
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_rebinding_selection_keeps_one_canonical_authority() -> None:
    sandbox = _FakeSandbox()
    manager = _manager(sandbox, port_pool=[3000, 5173])

    try:
        first = await manager.start(serve_dir="first", name="first", supervise=False)
        second = await manager.start(serve_dir="second", name="second", supervise=False)

        live = [session for session in manager.list() if session.status is PreviewStatus.RUNNING]
        assert live == [first, second]
        assert manager.canonical_session() is second
        assert manager.canonical_port() == second.port
        assert sandbox.sessions._running[first.name] is True
        assert first.port in sandbox._serving
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_failed_process_termination_keeps_resource_owned_and_port_leased() -> None:
    port = 39491
    sandbox = _FakeSandbox(backend_name="process")
    sandbox.shares_host_network = True
    manager = _manager(sandbox, port_pool=[port])
    contender = _manager(_FakeSandbox(backend_name="process"), port_pool=[port])
    contender._sandbox.shares_host_network = True
    original_stop = sandbox.sessions.stop_foreground_server

    async def fail_stop(
        name: str,
        *,
        expected_command: str,
        expected_port: int,
    ) -> str:
        raise RuntimeError(f"could not terminate {name} ({expected_command}) on {expected_port}")

    try:
        resource = await manager.start(serve_dir="dist", name="web", supervise=False)
        sandbox.sessions.stop_foreground_server = fail_stop
        with suppress(RuntimeError):
            await manager.stop(resource.name)

        assert resource.status is not PreviewStatus.STOPPED
        assert manager.canonical_lifecycle_session() is resource
        assert resource.port in sandbox._serving
        with pytest.raises(NoPreviewPortAvailableError):
            await contender.start(serve_dir="other", name="other", supervise=False)
    finally:
        sandbox.sessions.stop_foreground_server = original_stop
        await manager.aclose()
        await contender.aclose()


@pytest.mark.asyncio
async def test_failed_owned_sandbox_destroy_keeps_resource_and_lease_owned() -> None:
    port = 39492
    sandbox = _FakeSandbox(backend_name="process")
    sandbox.shares_host_network = True
    manager = PreviewManager(
        sandbox,
        port_pool=[port],
        health_attempts=1,
        health_interval_s=0.0,
        owns_sandbox=True,
    )
    contender_sandbox = _FakeSandbox(backend_name="process")
    contender_sandbox.shares_host_network = True
    contender = _manager(contender_sandbox, port_pool=[port])

    resource = await manager.start(serve_dir="dist", name="web", supervise=False)
    sandbox.fail_destroy = True
    try:
        with pytest.raises(RuntimeError, match="termination not confirmed"):
            await manager.aclose()

        assert manager.owns_sandbox() is True
        assert resource.status is not PreviewStatus.STOPPED
        assert resource.port in sandbox._serving
        with pytest.raises(NoPreviewPortAvailableError):
            await contender.start(serve_dir="other", name="other", supervise=False)
    finally:
        sandbox.fail_destroy = False
        await manager.aclose()
        await contender.aclose()

    assert sandbox.destroy_calls == 2
    assert manager.owns_sandbox() is False
    assert resource.status is PreviewStatus.STOPPED


@pytest.mark.asyncio
async def test_none_and_unavailable_states_never_fabricate_a_resource_url() -> None:
    empty_manager = _manager(_FakeSandbox(), port_pool=[3000])
    unavailable_manager = _manager(
        _FakeSandbox(backend_name="podman", can_expose=False),
        port_pool=[5173],
    )

    try:
        assert empty_manager.canonical_session() is None
        assert empty_manager.canonical_lifecycle_session() is None
        assert empty_manager.canonical_port() is None
        assert managed_preview_metadata(None) == (empty_preview_metadata(), "")

        unavailable = await unavailable_manager.start(
            serve_dir="dist",
            name="web",
            supervise=False,
        )
        rendered = unavailable.to_dict()
        assert unavailable.status is PreviewStatus.UNAVAILABLE
        assert unavailable.url is None
        assert rendered["status"] == "unavailable"
        assert rendered["url"] is None
        assert unavailable_manager.canonical_session() is unavailable
    finally:
        await empty_manager.aclose()
        await unavailable_manager.aclose()


@pytest.mark.asyncio
async def test_owned_preview_sandbox_is_destroyed_exactly_once() -> None:
    sandbox = _FakeSandbox()
    manager = PreviewManager(
        sandbox,
        port_pool=[3000],
        health_attempts=1,
        health_interval_s=0.0,
        owns_sandbox=True,
    )
    await manager.start(serve_dir="dist", name="web", supervise=False)

    await manager.aclose()
    await manager.aclose()

    assert sandbox.destroy_calls == 1
    assert sandbox._serving == set()


@pytest.mark.asyncio
async def test_sealed_rebind_closes_old_resource_before_materializing_isolated_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    class _Previous:
        async def aclose(self) -> None:
            order.append("close-old")

    class _PreviewSandbox:
        async def destroy(self) -> None:
            order.append("destroy-new")

    class _LoopFactory:
        def create_preview_session(self, conversation_id: str) -> _PreviewSandbox:
            assert conversation_id == "conv_preview_resource"
            order.append("create-new")
            return preview_sandbox

    class _Manager:
        def __init__(self, sandbox: object, *, owns_sandbox: bool) -> None:
            assert sandbox is preview_sandbox
            assert owns_sandbox is True
            order.append("bind-new")

        async def restore_sealed(self, contract: object) -> SimpleNamespace:
            assert contract is not None
            order.append("restore-new")
            return SimpleNamespace(status=SimpleNamespace(value="running"))

        async def aclose(self) -> None:
            order.append("close-new")

    async def _materialize(session: object, files: object, **kwargs: object) -> None:
        assert session is preview_sandbox
        assert files == (("index.html", b"sealed"),)
        assert not kwargs
        order.append("materialize-new")

    async def _dependencies(session: object, contract: object) -> None:
        assert session is preview_sandbox
        assert contract is not None
        order.append("dependencies-new")

    preview_sandbox = _PreviewSandbox()
    host_session = SimpleNamespace(_preview_manager=_Previous())
    restorer = object.__new__(SealedPreviewRestorer)
    restorer._loop_factory = _LoopFactory()
    monkeypatch.setattr(preview_sealed_restore, "PreviewManager", _Manager)
    monkeypatch.setattr(
        preview_sealed_restore,
        "replace_with_sealed_workspace",
        _materialize,
    )
    monkeypatch.setattr(
        preview_sealed_restore,
        "prepare_sealed_node_dependencies",
        _dependencies,
    )

    restored = await restorer._launch(
        "conv_preview_resource",
        host_session,
        _sealed_contract(1, "a"),
        (("index.html", b"sealed"),),
    )

    assert restored is True
    assert order == [
        "close-old",
        "create-new",
        "materialize-new",
        "dependencies-new",
        "bind-new",
        "restore-new",
    ]
    assert isinstance(host_session._preview_manager, _Manager)
