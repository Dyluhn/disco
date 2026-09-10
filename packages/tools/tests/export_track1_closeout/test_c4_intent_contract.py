"""WO-C4 red matrix (intent contract) — the persisted release-intent shape is versioned.

Plan §8 (WO-C4 "Required intent contract" + §8.11): the release intent must express, as
typed fields, more than baseline can (runtime strategy, install/build/start argv,
package manager/lockfile, static output dir, runtime AND build env NAMES with scope /
requiredness / secret classification, port env, health path, resources). Any such
persisted shape change MUST increment the intent's schema version so an older sidecar is
never silently reinterpreted (§8.11).

This file pins the ONE §8.11 property that is only observable at the ``release_declare``
TOOL boundary (it is not surfaced through ``/release``): the persisted
``release-intent.json`` sidecar the tool writes must carry a ``schema_version`` of at
least 2. The env/build/toolchain LOWERING behaviour (build args, secret mounts, guards,
lockfile conflicts, v1-sidecar migration) is asserted through the real ``/release`` +
``/download`` routes in
``packages/agent-server/tests/export_track1_closeout/test_c4_env_build_toolchain_matrix.py``.

Boundary (plan §1.2 / §4 crit 3): the declaration runs through a REAL
``DefaultToolExecutor`` executing the REAL ``ReleaseDeclareTool`` (the public tool
boundary), and the persisted sidecar is inspected as raw bytes on disk. Nothing under
test is mocked; the ONLY ``monkeypatch`` is ``DISCO_DATA_DIR`` (the OS/config seam the
runtime itself reads), so the tool writes to a real temporary default root.

RED on baseline ``a8e3e710``: ``ReleaseIntent.model_dump`` has no ``schema_version``
field, so the sidecar the tool persists carries no such key — the shape is unversioned.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
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


# ---- real-runtime harness (drives release_declare through the REAL runtime tool
# path exactly like WO-C1's tests; the ONLY seams are the DISCO_DATA_DIR env var and
# the ConfigStore.load config loader — the same injection the settings PUT performs) --


class _NeverCalledProvider:
    """A model double that fails LOUD if invoked. ``execute_disco_tool`` runs ONE tool
    call directly against the conversation's executor and never drives a model turn,
    so a correct run never touches this — but if the plumbing changed to call the
    model, the test fails honestly instead of hanging on a real network."""

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


def _base_cfg() -> RouterConfig:
    return RouterConfig(
        models={"m": ModelEntry(model_id="m", provider="fake", context_window=8192)},
        default_model="m",
    )


def _runtime(
    monkeypatch: pytest.MonkeyPatch, *, data_dir: Path
) -> tuple[ConversationRuntime, SqliteEventStore]:
    """A real runtime whose ACTIVE configured projects root is the DEFAULT
    (``DISCO_DATA_DIR``-derived) root pointed at ``data_dir`` — set through
    ``ConfigStore.load`` exactly as the settings PUT does. ``release_declare`` persists
    via the host-owned intent writer post-C1 and via the ``ProjectStore('')`` fallback
    on baseline, so a valid declaration writes its sidecar under ``data_dir`` in BOTH,
    which ``ProjectStore('').release_intent_for(cid)`` then reads back as raw bytes."""
    store = SqliteEventStore(":memory:")
    router = DefaultLLMRouter(_base_cfg(), {"fake": _NeverCalledProvider()})
    cfg_store = ConfigStore(path=Path("/dev/null"))
    monkeypatch.setenv("DISCO_DATA_DIR", str(data_dir))
    configured = _base_cfg().model_copy(
        update={"projects": ProjectStorageSettings(projects_root="")}
    )
    monkeypatch.setattr(cfg_store, "load", lambda: configured)
    runtime = ConversationRuntime(
        store,
        router=router,
        config_store=cfg_store,
        sandbox_service=ProcessSandboxService(),
    )
    return runtime, store


@pytest.mark.asyncio
async def test_persisted_intent_sidecar_carries_schema_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C4 §8.11 (intent schema version) — RED on baseline.

    Declare a valid release intent through the REAL tool executor (writing to the
    default ``DISCO_DATA_DIR`` root, which baseline already supports). The persisted
    ``release-intent.json`` sidecar must carry a ``schema_version`` of at least 2 — the
    WO-C4 intent contract adds typed fields (build env names / scope / secret class /
    install argv / package manager / output dir), and §8.11 requires any persisted
    shape change to increment the schema version so an older sidecar can never be
    silently reinterpreted. Baseline's ``ReleaseIntent`` has no ``schema_version`` field,
    so the persisted sidecar has no such key and this assertion fails RED."""
    make_name = closeout_name
    assert callable(make_name)
    cid = f"conv_{str(make_name('c4ver')).replace('-', '_')}"
    data_dir = tmp_path / "data dir"

    runtime, store = _runtime(monkeypatch, data_dir=data_dir)
    store.create_conversation(cid, owner_id="local")
    runtime.settings._set_surface(cid, "build")
    result = await runtime.execute_disco_tool(
        cid,
        ToolCall(
            tool_name="release_declare",
            arguments={"start_cmd": ["node", "server.js"], "required_env": ["SESSION_SECRET"]},
            call_id="closeout-c4-ver",
        ),
    )
    assert result.success, (
        f"precondition: a valid declaration must persist a sidecar to inspect "
        f"(error={result.error} / {result.content})"
    )

    sidecar = ProjectStore("").release_intent_for(cid)
    assert sidecar.is_file(), "the tool did not persist a release-intent sidecar to inspect"
    parsed: Any = json.loads(sidecar.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)

    assert "schema_version" in parsed, (
        "the persisted release-intent sidecar carries no `schema_version` — the WO-C4 "
        "intent shape change (build env / scope / secret class / install argv / …) must "
        "increment a schema version per §8.11 so an older sidecar is never silently "
        f"reinterpreted; baseline persists an unversioned shape (keys={sorted(parsed)})."
    )
    version = parsed["schema_version"]
    assert isinstance(version, int) and version >= 2, (
        f"the persisted intent schema_version must be >= 2 after the WO-C4 shape change, "
        f"got {version!r}."
    )
