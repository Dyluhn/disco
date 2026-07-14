"""Fix 2 (B-H.1 + codex P1b) — synthetic app DeliverableEvent at run-end snapshot.

A shell-served / npm-built site (`python3 -m http.server`, `npm run build`) writes
index.html but emits NO app DeliverableEvent (handle_serve is the only emitter), so
the user gets no "Open app" card and the snapshot serve path is never advertised.
_maybe_synthesize_app_deliverable closes that: if the snapshot has an index.html and
no app-deliverable was emitted, it appends one THROUGH THE EVENT STORE (P1b), exactly
once (idempotent).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from disco.agent_server.lifecycle import LifecycleManager
from disco.core import DeliverableEvent, EventSource, SqliteEventStore


class _Rt:
    """Minimal runtime back-ref: _maybe_synthesize_app_deliverable only needs _store."""

    def __init__(self, store: SqliteEventStore) -> None:
        self._store = store


@pytest.fixture(autouse=True)
def _close_event_stores(monkeypatch):
    owned: list[SqliteEventStore] = []
    original_init = SqliteEventStore.__init__

    def tracked_init(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        original_init(self, *args, **kwargs)
        owned.append(self)

    monkeypatch.setattr(SqliteEventStore, "__init__", tracked_init)
    yield
    for event_store in owned:
        event_store.close()


def _mgr(store: SqliteEventStore) -> LifecycleManager:
    return LifecycleManager(_Rt(store))  # type: ignore[arg-type]


async def _app_deliverables(store: SqliteEventStore, cid: str) -> list[DeliverableEvent]:
    return [
        e
        for e in await store.get_events(cid)
        if isinstance(e, DeliverableEvent) and e.artifact_kind == "app"
    ]


async def test_synthesizes_app_deliverable_for_shell_built_site(tmp_path: Path):
    store = SqliteEventStore(":memory:")
    cid = "conv-shell-built"
    (tmp_path / "index.html").write_text("<html>shell-served</html>")

    await _mgr(store)._maybe_synthesize_app_deliverable(cid, tmp_path)

    dels = await _app_deliverables(store, cid)
    assert len(dels) == 1
    assert dels[0].artifact_kind == "app"
    assert dels[0].deployment_url == ""
    assert dels[0].path == "."  # root-level index.html


async def test_synthesizes_for_subdir_app(tmp_path: Path):
    store = SqliteEventStore(":memory:")
    cid = "conv-subdir-app"
    sub = tmp_path / "macos-clone"
    sub.mkdir()
    (sub / "index.html").write_text("<html>subdir</html>")

    await _mgr(store)._maybe_synthesize_app_deliverable(cid, tmp_path)

    dels = await _app_deliverables(store, cid)
    assert len(dels) == 1
    assert dels[0].path == "macos-clone"


async def test_idempotent_on_second_snapshot(tmp_path: Path):
    """A second snapshot of the same build must NOT duplicate the card."""
    store = SqliteEventStore(":memory:")
    cid = "conv-idempotent"
    (tmp_path / "index.html").write_text("<html>x</html>")

    mgr = _mgr(store)
    await mgr._maybe_synthesize_app_deliverable(cid, tmp_path)
    await mgr._maybe_synthesize_app_deliverable(cid, tmp_path)

    assert len(await _app_deliverables(store, cid)) == 1


async def test_no_duplicate_when_serve_already_emitted(tmp_path: Path):
    """A real serve-emitted app-deliverable suppresses the synthetic one."""
    store = SqliteEventStore(":memory:")
    cid = "conv-already-served"
    (tmp_path / "index.html").write_text("<html>x</html>")
    await store.append(
        cid,
        DeliverableEvent(
            source=EventSource.AGENT,
            title="My app",
            path=".",
            artifact_kind="app",
            deployment_url="",
        ),
    )

    await _mgr(store)._maybe_synthesize_app_deliverable(cid, tmp_path)

    # still exactly the one the serve tool emitted — no synthetic addition
    assert len(await _app_deliverables(store, cid)) == 1
    assert (await _app_deliverables(store, cid))[0].title == "My app"


async def test_explicit_files_handoff_suppresses_synthetic_app(tmp_path: Path):
    """Any explicit serve handoff wins; lifecycle must not invent a conflicting app."""
    store = SqliteEventStore(":memory:")
    cid = "conv-files-handoff"
    (tmp_path / "index.html").write_text("<html>stale root</html>")
    selected = tmp_path / "release"
    selected.mkdir()
    (selected / "index.html").write_text("<html>selected report</html>")
    await store.append(
        cid,
        DeliverableEvent(
            source=EventSource.AGENT,
            title="Selected report",
            path="release/index.html",
            artifact_kind="files",
        ),
    )

    await _mgr(store)._maybe_synthesize_app_deliverable(cid, tmp_path)

    events = await store.get_events(cid)
    deliverables = [event for event in events if isinstance(event, DeliverableEvent)]
    assert [(event.artifact_kind, event.path) for event in deliverables] == [
        ("files", "release/index.html")
    ]
    assert await _app_deliverables(store, cid) == []


async def test_no_deliverable_when_no_index_html(tmp_path: Path):
    """A build with no index.html (e.g. a CLI tool / data run) gets no app card."""
    store = SqliteEventStore(":memory:")
    cid = "conv-no-index"
    (tmp_path / "report.csv").write_text("a,b\n1,2\n")

    await _mgr(store)._maybe_synthesize_app_deliverable(cid, tmp_path)

    assert await _app_deliverables(store, cid) == []


async def test_skips_internal_dir_index(tmp_path: Path):
    """An index.html buried in node_modules/.pmx must NOT trigger a card."""
    store = SqliteEventStore(":memory:")
    cid = "conv-internal-only"
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "index.html").write_text("<html>vendored</html>")

    await _mgr(store)._maybe_synthesize_app_deliverable(cid, tmp_path)

    assert await _app_deliverables(store, cid) == []
