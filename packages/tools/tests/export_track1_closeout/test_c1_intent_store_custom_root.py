"""WO-C1 red test — host-owned intent persistence for a CUSTOM configured root.

Plan §5 (WO-C1) acceptance 1+2: a real ``ToolExecutor`` configured with a custom
projects root (here, one containing SPACES) must write the sidecar to exactly
``<custom-root>/<cid>/release-intent.json`` and write NOTHING under the default
``DISCO_DATA_DIR`` root.

Boundary (plan §1.2 / §4 criterion 3+4): this drives ``release_declare`` through
the REAL runtime tool path — ``ConversationRuntime.execute_pi_tool`` builds the
conversation's real ``DefaultToolExecutor`` over a real ``ProcessSandboxService``
and runs the real ``ReleaseDeclareTool``. Nothing is mocked; the release-writing
function is exercised, not replaced. The "active configured ProjectStore" is set
the SAME way production sets it — ``ConfigStore.load().projects.projects_root`` —
which the runtime resolves through ``_project_store_now``.

WHY IT IS RED ON BASELINE ``2ec1ceba``: ``ReleaseDeclareTool._resolve_store``
hard-codes ``ProjectStore("")``, which resolves the ``DISCO_DATA_DIR`` DEFAULT
root and ignores the configured custom root entirely. So on baseline the sidecar
lands under the default root and the ``<custom-root>/<cid>/release-intent.json``
assertion fails — the exact gap WO-C1 closes by threading a host-owned
intent-writer capability that resolves the active configured store at call time.

Randomized (plan §4 criterion 8): the conversation id and the (space-bearing)
custom-root directory segment are drawn from the seeded ``closeout_name`` factory,
so the test cannot be satisfied by a hard-coded path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from disco.agent_server import ConversationRuntime
from disco.core import SqliteEventStore, ToolCall
from disco.core.llm import (
    ConfigStore,
    DefaultLLMRouter,
    ModelEntry,
    ProjectStorageSettings,
    RouterConfig,
)
from disco.tools import ProcessSandboxService
from disco.tools.projects import ProjectStore

pytestmark = pytest.mark.export_track1_closeout


class _NeverCalledProvider:
    """A model double that fails LOUD if invoked. ``execute_pi_tool`` runs ONE tool
    call directly against the conversation's executor and never drives a model
    turn, so a correct run never touches this — but if the plumbing changed to
    call the model, the test fails honestly instead of hanging on a real network."""

    name = "fake"

    async def complete(self, req: Any, *, model: Any) -> Any:  # pragma: no cover
        raise AssertionError("release_declare execution must not call the model")

    async def stream_complete(
        self, req: Any, *, model: Any
    ) -> AsyncIterator[Any]:  # pragma: no cover
        raise AssertionError("release_declare execution must not call the model")
        yield  # unreachable; makes this an async generator

    def supports(self, requirement: Any, *, model: Any) -> bool:
        return True


def _runtime_with_root(
    store: SqliteEventStore,
    monkeypatch: pytest.MonkeyPatch,
    *,
    configured_root: Path,
    default_data_dir: Path,
) -> ConversationRuntime:
    """A real runtime whose ACTIVE configured projects root is ``configured_root``
    (set through ``ConfigStore``, exactly as the settings PUT does), while the
    ``DISCO_DATA_DIR`` default resolves to a DIFFERENT ``default_data_dir`` — so a
    tool that writes to the default root writes to a provably-wrong location."""
    cfg = RouterConfig(
        models={"m": ModelEntry(model_id="m", provider="fake", context_window=8192)},
        default_model="m",
    )
    router = DefaultLLMRouter(cfg, {"fake": _NeverCalledProvider()})
    cfg_store = ConfigStore(path=Path("/dev/null"))
    configured = cfg.model_copy(
        update={"projects": ProjectStorageSettings(projects_root=str(configured_root))}
    )
    monkeypatch.setenv("DISCO_DATA_DIR", str(default_data_dir))
    monkeypatch.setattr(cfg_store, "load", lambda: configured)
    return ConversationRuntime(
        store, router=router, config_store=cfg_store, sandbox_service=ProcessSandboxService()
    )


@pytest.fixture
def _store() -> Iterator[SqliteEventStore]:
    store = SqliteEventStore(":memory:")
    yield store


@pytest.mark.asyncio
async def test_release_declare_writes_under_configured_custom_root(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    make_name = closeout_name  # Callable[[str], str] from the seeded conftest
    assert callable(make_name)
    cid = make_name("conv-c1")
    # A CUSTOM root that (a) contains spaces and (b) is seed-randomized, DIFFERENT
    # from the DISCO_DATA_DIR default — the WO-C1 scenario verbatim.
    configured_root = tmp_path / f"custom root {make_name('projects')}"
    default_data_dir = tmp_path / "default data dir"

    runtime = _runtime_with_root(
        _store, monkeypatch, configured_root=configured_root, default_data_dir=default_data_dir
    )
    _store.create_conversation(cid, owner_id="local")
    runtime.set_surface(cid, "build")

    result = await runtime.execute_pi_tool(
        cid,
        ToolCall(
            tool_name="release_declare",
            arguments={
                "start_cmd": ["node", "server.js"],
                "required_env": ["DATABASE_URL"],
            },
            call_id="closeout-c1",
        ),
    )
    # The tool must SUCCEED — a red result here would be an import/wiring failure,
    # not the behavioral gap we are pinning. (On baseline it does succeed; it just
    # writes to the wrong root.)
    assert result.success, f"release_declare failed to run: {result.error} / {result.content}"

    configured_store = ProjectStore(str(configured_root))
    default_store = ProjectStore(str(default_data_dir / "projects"))
    sidecar = configured_store.release_intent_for(cid)
    reported = (result.structured or {}).get("sidecar_path")

    # WO-C1 acceptance 1: the sidecar is under the CONFIGURED custom root.
    assert sidecar.is_file(), (
        "release-intent sidecar was NOT written under the configured custom root "
        f"{configured_root!s} (reported sidecar_path={reported!r}); on baseline "
        "release_declare uses ProjectStore('') and writes to the DISCO_DATA_DIR "
        "default root instead — the WO-C1 gap."
    )
    got = configured_store.read_release_intent(cid)
    assert got is not None and got.start_cmd == ("node", "server.js")
    assert got.required_env == ("DATABASE_URL",)

    # WO-C1 acceptance 2: NOTHING is written under the default root.
    assert not default_store.release_intent_for(cid).exists(), (
        "release-intent leaked into the DISCO_DATA_DIR default root instead of the "
        "configured custom root."
    )
