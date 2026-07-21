"""WO-C1 red matrix — host-owned intent persistence for EVERY configured root.

Plan §5 (WO-C1) acceptance criteria 1-9. The problem C1 closes: ``release_declare``
constructs ``ProjectStore("")`` and writes to the DEFAULT (``DISCO_DATA_DIR``) root
even when the runtime selects a CUSTOM ``projects_root``. The required design makes
``release_declare`` an ``in_process`` host tool that receives a narrow host-owned
intent-writer capability in ``ToolContext``; the runtime closure resolves the ACTIVE
configured ``ProjectStore`` at INVOCATION time, the tool gets no raw root and has no
fallback store, and a missing capability / invalid root / owner mismatch / persistence
failure fails closed with a typed error and ZERO bytes persisted.

Boundary (plan §1.2 / §4 crit 3): the end-to-end tests drive ``release_declare``
through the REAL runtime tool path — ``ConversationRuntime.execute_pi_tool`` builds
the conversation's real ``DefaultToolExecutor`` over a real ``ProcessSandboxService``
and runs the real ``ReleaseDeclareTool`` — and ``GET /api/projects/{cid}/release``
through the REAL FastAPI app. Nothing under test is mocked; the ONLY ``monkeypatch``
uses are genuine config/OS seams (``DISCO_DATA_DIR`` and ``ConfigStore.load``, the
SAME injection the settings PUT performs), never the release/tool code under test.

RED vs GREEN on baseline ``2ec1ceba`` (see each test's docstring):
  * RED (behavior absent): custom-root write, nothing-outside-root, /release consumes
    custom-root sidecar, custom-absolute + root-changed authority, invalid/unavailable
    root fail-closed, missing writer capability, persistence-failure fail-closed,
    owner/conversation mismatch, ``runs_in=="in_process"`` + no FS capability, and
    success-output hygiene (no argv / sidecar-path echo).
  * GREEN (preservation): default-root write via the executor path, the default-root
    authority case, and atomic re-declaration.

Randomized (plan §4 crit 8): conversation ids and the (space-bearing) custom-root
directory segments are drawn from the seeded ``closeout_name`` factory; a name only
ever flows into a conversation id / sidecar path, never into workspace CONTENTS the
detector reads — so nothing can be satisfied by a hard-coded path.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.core import SqliteEventStore, ToolCall, ToolResult
from disco.core.llm import (
    ConfigStore,
    DefaultLLMRouter,
    ModelEntry,
    ProjectStorageSettings,
    RouterConfig,
)
from disco.tools import ProcessSandboxService
from disco.tools.builtin import build_default_registry
from disco.tools.executor import DefaultToolExecutor
from disco.tools.projects import ProjectStore
from disco.tools.registry import ToolScope
from fastapi.testclient import TestClient

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


# ---- config/OS-seam harness (never patches the code under test) ----------------


def _base_cfg() -> RouterConfig:
    return RouterConfig(
        models={"m": ModelEntry(model_id="m", provider="fake", context_window=8192)},
        default_model="m",
    )


def _cfg_with_root(projects_root: str) -> RouterConfig:
    return _base_cfg().model_copy(
        update={"projects": ProjectStorageSettings(projects_root=projects_root)}
    )


def _runtime(
    store: SqliteEventStore,
    monkeypatch: pytest.MonkeyPatch,
    *,
    projects_root: str,
    data_dir: Path,
) -> tuple[ConversationRuntime, ConfigStore]:
    """A real runtime whose ACTIVE configured projects root is ``projects_root``
    (set through ``ConfigStore.load``, exactly as the settings PUT does) while the
    ``DISCO_DATA_DIR`` DEFAULT resolves to a DIFFERENT ``data_dir`` — so a tool that
    writes to the default root writes to a provably-wrong location. Returns the
    runtime AND its ``ConfigStore`` so a test can re-point ``load`` mid-run (the
    root-changed authority case)."""
    router = DefaultLLMRouter(_base_cfg(), {"fake": _NeverCalledProvider()})
    cfg_store = ConfigStore(path=Path("/dev/null"))
    monkeypatch.setenv("DISCO_DATA_DIR", str(data_dir))
    configured = _cfg_with_root(projects_root)
    monkeypatch.setattr(cfg_store, "load", lambda: configured)
    runtime = ConversationRuntime(
        store,
        router=router,
        config_store=cfg_store,
        sandbox_service=ProcessSandboxService(),
    )
    return runtime, cfg_store


# Kept for the original criterion-1 test below (byte-for-byte behavior preserved).
def _runtime_with_root(
    store: SqliteEventStore,
    monkeypatch: pytest.MonkeyPatch,
    *,
    configured_root: Path,
    default_data_dir: Path,
) -> ConversationRuntime:
    runtime, _cfg_store = _runtime(
        store, monkeypatch, projects_root=str(configured_root), data_dir=default_data_dir
    )
    return runtime


@pytest.fixture
def _store() -> Iterator[SqliteEventStore]:
    store = SqliteEventStore(":memory:")
    yield store


_DECLARE_ARGS: dict[str, Any] = {
    "start_cmd": ["node", "server.js"],
    "required_env": ["DATABASE_URL"],
}


async def _declare(
    runtime: ConversationRuntime,
    store: SqliteEventStore,
    cid: str,
    *,
    owner_id: str = "local",
    arguments: dict[str, Any] | None = None,
    create: bool = True,
) -> ToolResult:
    """Create the conversation (a build surface so it has a tool executor), then run
    ``release_declare`` through the REAL runtime executor path — NOT by calling
    ``ReleaseDeclareTool.run`` directly (plan §5 crit 9)."""
    if create:
        store.create_conversation(cid, owner_id=owner_id)
        runtime.set_surface(cid, "build")
    return await runtime.execute_pi_tool(
        cid,
        ToolCall(
            tool_name="release_declare",
            arguments=dict(arguments if arguments is not None else _DECLARE_ARGS),
            call_id="closeout-c1",
        ),
    )


def _all_sidecars(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.rglob("release-intent.json"))


# ---- criterion 1: custom root (WITH SPACES) → sidecar at <root>/<cid>/... -------


@pytest.mark.asyncio
async def test_release_declare_writes_under_configured_custom_root(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C1 acc. 1+2 — RED on baseline.

    A real executor configured with a custom projects root containing SPACES must
    write the sidecar to exactly ``<custom-root>/<cid>/release-intent.json`` and
    NOTHING under the default root. Baseline ``ReleaseDeclareTool`` uses
    ``ProjectStore("")`` → resolves the ``DISCO_DATA_DIR`` DEFAULT and ignores the
    configured custom root, so the sidecar lands under the default root and this
    assertion fails."""
    make_name = closeout_name
    assert callable(make_name)
    cid = make_name("conv-c1")
    configured_root = tmp_path / f"custom root {make_name('projects')}"
    default_data_dir = tmp_path / "default data dir"

    runtime = _runtime_with_root(
        _store, monkeypatch, configured_root=configured_root, default_data_dir=default_data_dir
    )
    result = await _declare(
        runtime,
        _store,
        cid,
        arguments={"start_cmd": ["node", "server.js"], "required_env": ["DATABASE_URL"]},
    )
    assert result.success, f"release_declare failed to run: {result.error} / {result.content}"

    configured_store = ProjectStore(str(configured_root))
    default_store = ProjectStore(str(default_data_dir / "projects"))
    sidecar = configured_store.release_intent_for(cid)
    reported = (result.structured or {}).get("sidecar_path")

    assert sidecar.is_file(), (
        "release-intent sidecar was NOT written under the configured custom root "
        f"{configured_root!s} (reported sidecar_path={reported!r}); on baseline "
        "release_declare uses ProjectStore('') and writes to the DISCO_DATA_DIR "
        "default root instead — the WO-C1 gap."
    )
    got = configured_store.read_release_intent(cid)
    assert got is not None and got.start_cmd == ("node", "server.js")
    assert got.required_env == ("DATABASE_URL",)

    assert not default_store.release_intent_for(cid).exists(), (
        "release-intent leaked into the DISCO_DATA_DIR default root instead of the "
        "configured custom root."
    )


# ---- criterion 2: writes NOTHING outside the configured root -------------------


@pytest.mark.asyncio
async def test_release_declare_writes_nothing_outside_configured_root(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C1 acc. 2 — RED on baseline.

    After ONE declare against a custom root, the ONLY ``release-intent.json`` under
    the whole tree must be ``<custom-root>/<cid>/release-intent.json`` — none under
    the default ``DISCO_DATA_DIR`` root and none INSIDE the live ``workspace/`` tree.
    Baseline writes it under ``<default-data-dir>/projects/<cid>/`` (a sibling of the
    configured root, still under ``tmp_path``), so the "sole location is the
    configured root" invariant fails."""
    make_name = closeout_name
    assert callable(make_name)
    cid = make_name("conv-c1-scope")
    configured_root = tmp_path / f"cfg root {make_name('proj')}"
    default_data_dir = tmp_path / "default data dir"

    runtime = _runtime_with_root(
        _store, monkeypatch, configured_root=configured_root, default_data_dir=default_data_dir
    )
    # A real live workspace tree so the "nothing inside workspace/" check is meaningful.
    workspace = ProjectStore(str(configured_root)).path_for(cid)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "server.js").write_bytes(b"console.log('ok');\n")

    result = await _declare(runtime, _store, cid, create=True)
    assert result.success, f"release_declare failed to run: {result.error} / {result.content}"

    found = _all_sidecars(tmp_path)
    expected = ProjectStore(str(configured_root)).release_intent_for(cid)
    assert found == [expected], (
        "release-intent sidecar was persisted OUTSIDE the configured custom root: "
        f"found {[str(p) for p in found]}, expected exactly {[str(expected)]}. On "
        "baseline it leaks into the DISCO_DATA_DIR default root."
    )
    # No sidecar hidden inside the live workspace tree (a workspace file must never
    # be able to masquerade as the host-owned record).
    assert not list(workspace.rglob("release-intent.json"))


# ---- criterion 3: /release consumes the custom-root sidecar --------------------

_NODE_FILES: dict[str, bytes] = {
    "server.js": b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n",
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
}
# A required-env NAME that appears NOWHERE in the workspace source, so it can ONLY
# reach the /release response through the declared intent sidecar.
_DECLARED_ONLY_ENV = "CLOSEOUT_DECLARED_TOKEN"


def _seed_workspace(ps: ProjectStore, cid: str, *, title: str) -> None:
    workspace = ps.path_for(cid)
    workspace.mkdir(parents=True, exist_ok=True)
    total = 0
    for rel, data in _NODE_FILES.items():
        dest = workspace / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        total += len(data)
    ps.write_manifest(
        cid,
        title=title,
        owner_id="local",
        created_at="2026-06-06T00:00:00Z",
        file_count=len(_NODE_FILES),
        total_bytes=total,
        imported=False,
    )


@pytest.mark.asyncio
async def test_release_endpoint_consumes_custom_root_sidecar(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C1 acc. 3 — RED on baseline.

    Declare through the tool, then GET ``/api/projects/{cid}/release`` on the SAME
    runtime (same configured custom root). The declared ``required_env`` NAME —
    absent from the workspace source — must appear in the response, proving the
    endpoint consumed the sidecar the tool wrote. Baseline writes the sidecar to the
    default root while /release reads the configured custom root, so the intent is
    never found and the declared-only NAME is missing."""
    make_name = closeout_name
    assert callable(make_name)
    cid = f"conv_{make_name('c1e2e').replace('-', '_')}"
    configured_root = tmp_path / f"cfg root {make_name('proj')}"
    default_data_dir = tmp_path / "default data dir"

    runtime, _cfg_store = _runtime(
        _store, monkeypatch, projects_root=str(configured_root), data_dir=default_data_dir
    )
    ps = ProjectStore(str(configured_root))
    _store.create_conversation(cid, owner_id="local", title="svc", surface="build")
    runtime.set_surface(cid, "build")
    _seed_workspace(ps, cid, title="svc")

    result = await _declare(
        runtime,
        _store,
        cid,
        arguments={"start_cmd": ["node", "server.js"], "required_env": [_DECLARED_ONLY_ENV]},
        create=False,
    )
    assert result.success, f"release_declare failed to run: {result.error} / {result.content}"

    client = TestClient(create_app(_store, runtime=runtime))
    res = client.get(f"/api/projects/{cid}/release")
    assert res.status_code == 200, res.text
    names = {env["name"] for env in res.json()["required_env"]}
    assert _DECLARED_ONLY_ENV in names, (
        f"/release did not surface the declared env NAME {_DECLARED_ONLY_ENV!r} "
        f"(saw {sorted(names)}); the sidecar the tool wrote is not under the "
        "configured custom root the endpoint reads (WO-C1)."
    )


# ---- criterion 4: active runtime config authoritative in every case ------------


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["default_root", "custom_absolute"])
async def test_active_configured_root_is_authoritative(
    case: str,
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C1 acc. 4 — ``default_root`` is GREEN (preservation), ``custom_absolute``
    is RED.

    In both cases the sidecar must land under the ACTIVE configured root. For
    ``default_root`` the configured root IS the ``DISCO_DATA_DIR`` default, so
    baseline (which always uses that default) is already correct — a preservation
    check. For ``custom_absolute`` the configured root differs from the default;
    baseline writes to the default and this fails RED."""
    make_name = closeout_name
    assert callable(make_name)
    cid = make_name(f"conv-c1-{case}")
    data_dir = tmp_path / "data dir"

    if case == "default_root":
        # Empty projects_root ⇒ resolves to <DISCO_DATA_DIR>/projects (the default).
        projects_root = ""
        expected_root = data_dir / "projects"
    else:
        expected_root = tmp_path / f"custom abs {make_name('proj')}"
        projects_root = str(expected_root)

    runtime, _cfg_store = _runtime(
        _store, monkeypatch, projects_root=projects_root, data_dir=data_dir
    )
    result = await _declare(runtime, _store, cid)
    assert result.success, f"release_declare failed to run: {result.error} / {result.content}"

    active_store = ProjectStore(projects_root)
    sidecar = active_store.release_intent_for(cid)
    assert sidecar.is_file(), (
        f"[{case}] sidecar not under the active configured root {expected_root!s}; "
        "baseline ignores the runtime config and writes to the DISCO_DATA_DIR default."
    )
    if case == "custom_absolute":
        default_store = ProjectStore(str(data_dir / "projects"))
        assert not default_store.release_intent_for(cid).exists(), (
            "sidecar leaked into the DISCO_DATA_DIR default root instead of the "
            "configured custom absolute root."
        )


@pytest.mark.asyncio
async def test_active_root_change_between_construction_and_invocation(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C1 acc. 4 (root CHANGED mid-life) — RED on baseline.

    Build + cache the conversation's executor with configured root A (first declare),
    then re-point ``ConfigStore.load`` to root B and declare AGAIN with the SAME cid.
    The active config at INVOCATION time (B) must own where the second write lands.
    Baseline ignores config entirely (writes to the default root both times), so no
    sidecar appears under B."""
    make_name = closeout_name
    assert callable(make_name)
    cid = make_name("conv-c1-changed")
    data_dir = tmp_path / "data dir"
    root_a = tmp_path / f"root A {make_name('a')}"
    root_b = tmp_path / f"root B {make_name('b')}"

    runtime, cfg_store = _runtime(_store, monkeypatch, projects_root=str(root_a), data_dir=data_dir)
    first = await _declare(runtime, _store, cid, create=True)
    assert first.success, f"first declare failed: {first.error} / {first.content}"

    # Re-point the ACTIVE config to root B (a genuine config seam), then re-declare
    # against the already-built, cached executor.
    cfg_b = _cfg_with_root(str(root_b))
    monkeypatch.setattr(cfg_store, "load", lambda: cfg_b)
    second = await _declare(
        runtime,
        _store,
        cid,
        arguments={"start_cmd": ["node", "app.js"], "required_env": ["B_TOKEN"]},
        create=False,
    )
    assert second.success, f"second declare failed: {second.error} / {second.content}"

    sidecar_b = ProjectStore(str(root_b)).release_intent_for(cid)
    assert sidecar_b.is_file(), (
        f"the second declare did not write under the ACTIVE (changed) root {root_b!s}; "
        "the intent-writer must resolve the configured store at invocation time, not "
        "capture a root at executor construction (and baseline ignores config entirely)."
    )
    intent = ProjectStore(str(root_b)).read_release_intent(cid)
    assert intent is not None and intent.start_cmd == ("node", "app.js")


# ---- criterion 5: fail-closed cases (typed error, ZERO bytes) ------------------


@pytest.mark.asyncio
async def test_invalid_root_fails_closed_zero_bytes(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C1 acc. 5 (invalid root) — RED on baseline.

    The configured projects root points at a regular FILE (``NOT_A_DIRECTORY``). The
    declare must fail closed with a typed error and persist zero bytes. Baseline
    ignores the invalid configured root, writes to the writable ``DISCO_DATA_DIR``
    default, and reports success."""
    make_name = closeout_name
    assert callable(make_name)
    cid = make_name("conv-c1-badroot")
    data_dir = tmp_path / "data dir"
    bogus = tmp_path / "not-a-directory"
    bogus.write_bytes(b"i am a file, not a projects root\n")

    runtime, _cfg_store = _runtime(_store, monkeypatch, projects_root=str(bogus), data_dir=data_dir)
    result = await _declare(runtime, _store, cid)

    assert not result.success, (
        "release_declare succeeded against an invalid (NOT_A_DIRECTORY) configured "
        "root; it must fail closed with a typed error. Baseline succeeds by writing "
        "to the DISCO_DATA_DIR default instead."
    )
    assert result.error, "a failed declare must carry a typed error code"
    assert not _all_sidecars(data_dir), (
        "an invalid-root declare persisted bytes to the default root; it must persist "
        "ZERO bytes anywhere."
    )


@pytest.mark.asyncio
async def test_persistence_failure_fails_closed_zero_bytes(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C1 acc. 5 (persistence failure) — RED on baseline.

    The configured root is a real directory made READ-ONLY, so the atomic write into
    ``<root>/<cid>/`` fails. The declare must return a typed persistence error and
    persist zero bytes. Baseline writes to the writable ``DISCO_DATA_DIR`` default and
    reports success, never touching the configured root."""
    make_name = closeout_name
    assert callable(make_name)
    cid = make_name("conv-c1-persistfail")
    data_dir = tmp_path / "data dir"
    ro_root = tmp_path / "readonly-root"
    ro_root.mkdir()
    os.chmod(ro_root, stat.S_IRUSR | stat.S_IXUSR)  # r-x------ : cannot create children
    try:
        runtime, _cfg_store = _runtime(
            _store, monkeypatch, projects_root=str(ro_root), data_dir=data_dir
        )
        result = await _declare(runtime, _store, cid)

        assert not result.success, (
            "release_declare succeeded despite the configured root being unwritable; "
            "a persistence failure must fail closed with a typed error. Baseline "
            "succeeds by writing to the DISCO_DATA_DIR default instead."
        )
        assert result.error, "a failed declare must carry a typed error code"
        assert not _all_sidecars(data_dir), (
            "a persistence-failing declare wrote bytes to the default root; it must "
            "persist ZERO bytes anywhere."
        )
    finally:
        os.chmod(ro_root, stat.S_IRWXU)


@pytest.mark.asyncio
async def test_missing_intent_writer_capability_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C1 acc. 5 (missing writer capability) — RED on baseline.

    Run ``release_declare`` through a REAL ``DefaultToolExecutor`` (the public
    executor boundary) that carries NO host-owned intent-writer capability — which is
    literally all a standalone executor can do today: the constructor has no such
    wire, so the capability is absent. The tool must fail closed with a typed error
    and persist zero bytes. Baseline instead calls ``ProjectStore("")`` directly and
    succeeds, writing the sidecar to the ``DISCO_DATA_DIR`` default root."""
    make_name = closeout_name
    assert callable(make_name)
    cid = f"conv_{make_name('nocap').replace('-', '_')}"
    data_dir = tmp_path / "data dir"
    monkeypatch.setenv("DISCO_DATA_DIR", str(data_dir))

    executor = DefaultToolExecutor(
        build_default_registry(),
        ToolScope(allowed_tools=frozenset({"release_declare"})),
        owner_id="local",
        conversation_id=cid,
    )
    result = await executor.execute(
        ToolCall(
            tool_name="release_declare",
            arguments={"start_cmd": ["node", "server.js"], "required_env": ["DATABASE_URL"]},
            call_id="closeout-nocap",
        )
    )

    assert not result.success, (
        "release_declare succeeded WITHOUT a host-owned intent-writer capability; it "
        "must fail closed. Baseline uses a hard-coded ProjectStore('') fallback and "
        "writes to the DISCO_DATA_DIR default root regardless of any capability."
    )
    assert not _all_sidecars(data_dir), (
        "a capability-less declare persisted bytes to the default root; it must "
        "persist ZERO bytes when the writer capability is absent."
    )


@pytest.mark.asyncio
async def test_owner_conversation_mismatch_fails_closed(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C1 acc. 5 (owner/conversation mismatch) — RED on baseline.

    The target conversation is owned by ``intruder`` while the invoking executor's
    owner is ``local``. Persisting host-owned intent for a conversation the caller
    does not own must fail closed with a typed error and ZERO bytes. Baseline performs
    NO owner check — it writes ``<root>/<cid>/release-intent.json`` for any id — so it
    succeeds and persists the sidecar."""
    make_name = closeout_name
    assert callable(make_name)
    cid = make_name("conv-c1-mismatch")
    data_dir = tmp_path / "data dir"
    configured_root = tmp_path / f"cfg root {make_name('proj')}"

    runtime, _cfg_store = _runtime(
        _store, monkeypatch, projects_root=str(configured_root), data_dir=data_dir
    )
    # The conversation belongs to a DIFFERENT owner than the executor (owner "local").
    result = await _declare(runtime, _store, cid, owner_id="intruder")

    assert not result.success, (
        "release_declare persisted intent for a conversation owned by another owner; "
        "an owner/conversation mismatch must fail closed. Baseline does no owner check."
    )
    assert not _all_sidecars(tmp_path), (
        "an owner-mismatch declare persisted a sidecar; it must persist ZERO bytes."
    )


# ---- criterion 6: atomic re-declaration (GREEN preservation) -------------------


@pytest.mark.asyncio
async def test_redeclaration_is_atomic_on_forced_write_failure(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C1 acc. 6 — GREEN preservation.

    Configured root == the ``DISCO_DATA_DIR`` default so BOTH baseline and the fixed
    design write to the same place; a first declare succeeds, then the project
    directory is made read-only to force the second (re-)declare's atomic write to
    fail. The prior valid sidecar must remain byte-identical and no ``.tmp`` file may
    remain — the ``_write_json_atomic`` (tmp + replace) discipline the fix must not
    regress."""
    make_name = closeout_name
    assert callable(make_name)
    cid = make_name("conv-c1-atomic")
    data_dir = tmp_path / "data dir"

    runtime, _cfg_store = _runtime(_store, monkeypatch, projects_root="", data_dir=data_dir)
    first = await _declare(
        runtime,
        _store,
        cid,
        arguments={"start_cmd": ["node", "v1.js"], "required_env": ["FIRST_TOKEN"]},
    )
    assert first.success, f"first declare failed: {first.error} / {first.content}"

    ps = ProjectStore("")
    sidecar = ps.release_intent_for(cid)
    assert sidecar.is_file()
    before = sidecar.read_bytes()
    project_dir = sidecar.parent

    os.chmod(project_dir, stat.S_IRUSR | stat.S_IXUSR)  # block child create/replace
    try:
        second = await _declare(
            runtime,
            _store,
            cid,
            arguments={"start_cmd": ["node", "v2.js"], "required_env": ["SECOND_TOKEN"]},
            create=False,
        )
        assert not second.success, (
            "the forced-failure re-declare reported success; it must return a typed "
            "persistence error."
        )
        assert sidecar.read_bytes() == before, (
            "a failed re-declaration mutated the prior valid sidecar; the atomic "
            "tmp+replace must leave it byte-identical."
        )
        assert not list(project_dir.glob("*.tmp")), (
            "a stale .tmp file remained after a failed write"
        )
    finally:
        os.chmod(project_dir, stat.S_IRWXU)


# ---- criterion 7: in_process host tool, no sandbox-fs capability ---------------


def test_release_declare_is_in_process_and_needs_no_filesystem_capability() -> None:
    """WO-C1 acc. 7 — RED on baseline.

    The design makes ``release_declare`` an ``in_process`` host tool that requests no
    sandbox filesystem capability (it writes host state via the injected capability,
    never through the sandbox). Baseline declares ``runs_in="sandbox"`` and
    ``needs={Capability.FILESYSTEM}``."""
    from disco.tools.anatomy import Capability
    from disco.tools.builtin.release_declare import ReleaseDeclareTool

    definition = ReleaseDeclareTool().definition
    assert definition.runs_in == "in_process", (
        f"release_declare.runs_in is {definition.runs_in!r}; WO-C1 requires "
        '"in_process" (a host tool, not a sandbox tool).'
    )
    assert Capability.FILESYSTEM not in definition.needs, (
        "release_declare still requests the sandbox FILESYSTEM capability; the host "
        "intent-writer must not need sandbox filesystem access."
    )


# ---- criterion 8: success output hygiene (no argv / roots / sidecar paths) ------


@pytest.mark.asyncio
async def test_success_output_omits_argv_roots_and_sidecar_path(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C1 acc. 8 — RED on baseline.

    A successful declare's structured output must carry env NAMES + non-sensitive
    structural metadata only — never the full argv, resource URLs, filesystem roots,
    or the sidecar path. Baseline echoes ``sidecar_path`` (a host filesystem path)
    plus the full ``start_cmd`` / ``build_cmd`` argv in ``structured``."""
    make_name = closeout_name
    assert callable(make_name)
    cid = make_name("conv-c1-hygiene")
    data_dir = tmp_path / "data dir"

    runtime, _cfg_store = _runtime(_store, monkeypatch, projects_root="", data_dir=data_dir)
    result = await _declare(runtime, _store, cid)
    assert result.success, f"release_declare failed to run: {result.error} / {result.content}"

    structured = result.structured or {}
    assert "sidecar_path" not in structured, (
        "success output echoes 'sidecar_path' (a host filesystem path); it must expose "
        "env names + non-sensitive structural metadata only."
    )
    assert "start_cmd" not in structured and "build_cmd" not in structured, (
        "success output echoes the full argv (start_cmd/build_cmd); WO-C1 crit 8 "
        "forbids echoing full argv."
    )


# ---- criterion 9: default-root behavior preserved THROUGH the executor path -----


@pytest.mark.asyncio
async def test_default_root_behavior_preserved_through_executor(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C1 acc. 9 — GREEN preservation.

    With the configured root equal to the ``DISCO_DATA_DIR`` default, a declare driven
    THROUGH the real runtime executor path (not by calling ``ReleaseDeclareTool.run``
    directly) writes the sidecar under the default root, and ``GET /release`` on the
    same runtime consumes it. This is already correct on baseline and must stay green
    after the fix."""
    make_name = closeout_name
    assert callable(make_name)
    cid = f"conv_{make_name('defroot').replace('-', '_')}"
    data_dir = tmp_path / "data dir"

    runtime, _cfg_store = _runtime(_store, monkeypatch, projects_root="", data_dir=data_dir)
    ps = ProjectStore("")
    _store.create_conversation(cid, owner_id="local", title="svc", surface="build")
    runtime.set_surface(cid, "build")
    _seed_workspace(ps, cid, title="svc")

    result = await _declare(
        runtime,
        _store,
        cid,
        arguments={"start_cmd": ["node", "server.js"], "required_env": [_DECLARED_ONLY_ENV]},
        create=False,
    )
    assert result.success, f"release_declare failed to run: {result.error} / {result.content}"
    assert ps.release_intent_for(cid).is_file(), "default-root sidecar was not written"

    client = TestClient(create_app(_store, runtime=runtime))
    res = client.get(f"/api/projects/{cid}/release")
    assert res.status_code == 200, res.text
    names = {env["name"] for env in res.json()["required_env"]}
    assert _DECLARED_ONLY_ENV in names, "default-root /release did not consume the declared sidecar"
    # Prove it round-tripped through disk, not just memory.
    parsed = json.loads(ps.release_intent_for(cid).read_text())
    assert _DECLARED_ONLY_ENV in parsed["required_env"]
