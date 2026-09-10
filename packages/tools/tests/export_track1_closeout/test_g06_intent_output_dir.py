"""GAP G06 red (static build ``output_dir`` is undeclarable) — RED on 581dfe.

The typed release intent must be able to DECLARE where a static / Vite build's
output lands (``output_dir``, e.g. ``dist``), so a prebuilt static site can be
served from that directory. ``output_dir`` DOES exist on the internal detection /
``ReleaseService`` spec (``disco.core.release.spec`` service shape) and is
interpolated into the two-stage ``COPY --from=build /app/<output_dir>/`` line — but
it is UNDECLARABLE through the typed ``ReleaseIntent`` the ``release_declare`` tool
accepts. ``ReleaseIntent`` has ``extra="forbid"`` and no ``output_dir`` field, and
the tool's ``ReleaseDeclareArgs`` surface mirrors it, so a declaration that supplies
``output_dir`` is REJECTED with ``extra_forbidden`` (nothing is persisted). This is
the gap; the fix (add ``output_dir`` to the typed intent + lower it through the
tool) is R2 — this file only LANDS the red.

Boundary (plan §1.2 / §4 crit 3): the declaration runs through a REAL
``ConversationRuntime`` executor executing the REAL ``ReleaseDeclareTool`` (the
public tool boundary), and the persisted sidecar is inspected as raw bytes on disk.
Nothing under test is mocked; the ONLY seams are the ``DISCO_DATA_DIR`` env var and
the ``ConfigStore.load`` config loader — the same injection the settings PUT
performs — so the tool writes to a real temporary default root.

The preserved live reds for this same gap are the
``ReleaseIntent(output_dir="dist", ...)`` fixtures in
``current/packages/agent-server/tests/integration/_closeout_live_support.py`` (which fail at
intent CONSTRUCTION on 581dfe); this file pins the property at the ``release_declare``
TOOL boundary in the non-live lane.
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
async def test_declared_static_output_dir_round_trips_into_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """GAP G06 (static ``output_dir`` is undeclarable) — RED on 581dfe.

    Drive the REAL ``release_declare`` tool with the canonical static / Vite build
    declaration: ``build_cmd=["npm","run","build"]``, ``output_dir="dist"``, a health
    path, and no runtime start command (a prebuilt static site is served from
    ``dist``, not launched). The CORRECT behavior asserted here: the tool ACCEPTS the
    declaration and the persisted ``release-intent.json`` sidecar carries
    ``output_dir="dist"``, readable back.

    On 581dfe this fails RED: ``output_dir`` is undeclarable via the typed intent —
    ``ReleaseIntent`` has ``extra="forbid"`` and no ``output_dir`` field, and the
    ``ReleaseDeclareArgs`` surface mirrors it — so the executor rejects the call with
    ``invalid_arguments`` / ``extra_forbidden`` and persists no sidecar. The
    ``build_cmd`` (``npm run build``) is an accepted runtime-grammar command and the
    empty ``start_cmd`` is a no-op, so the ONLY thing making this red is the
    undeclarable ``output_dir`` — gap G06."""
    make_name = closeout_name
    assert callable(make_name)
    cid = f"conv_{str(make_name('g06outdir')).replace('-', '_')}"
    data_dir = tmp_path / "data dir"

    runtime, store = _runtime(monkeypatch, data_dir=data_dir)
    store.create_conversation(cid, owner_id="local")
    runtime.settings._set_surface(cid, "build")
    result = await runtime.execute_disco_tool(
        cid,
        ToolCall(
            tool_name="release_declare",
            arguments={
                "build_cmd": ["npm", "run", "build"],
                "output_dir": "dist",
                "health_path": "/",
            },
            call_id="closeout-g06-outdir",
        ),
    )

    assert result.success, (
        "release_declare REJECTED a static-build declaration carrying "
        f"output_dir='dist' (error={result.error!r}; {result.content}). `output_dir` "
        "is undeclarable via the typed release intent — `ReleaseIntent` has "
        "extra='forbid' and no `output_dir` field, and `ReleaseDeclareArgs` mirrors "
        "it, so the declaration is rejected with `extra_forbidden`. A static / Vite "
        "build must be able to DECLARE where its built output lands; `output_dir` must "
        "be an accepted, typed declaration field — gap G06."
    )

    sidecar = ProjectStore("").release_intent_for(cid)
    assert sidecar.is_file(), (
        "release_declare accepted the declaration but persisted no release-intent "
        "sidecar to read `output_dir` back from."
    )
    parsed: Any = json.loads(sidecar.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)

    assert parsed.get("output_dir") == "dist", (
        "the persisted release-intent.json did not round-trip a declared "
        f"output_dir='dist' (got {parsed.get('output_dir')!r}; keys={sorted(parsed)}). "
        "A static / Vite build's output directory must be a declarable, persisted "
        "field of the typed release intent so release detection can serve the built "
        "site from it — gap G06."
    )
