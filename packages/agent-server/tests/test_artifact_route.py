"""rp-11 residue — the declared-artifact download route: the allowlist (only files
the conversation EMITTED as results), the extension pin, traversal defense, and the
finished-run ProjectStore fallback.

Like test_workspace_route, the stub session serves ANY path, so a 404 proves the
route's OWN jail rejected the path before any read.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from perpleximanus.agent_server import create_app
from perpleximanus.core import ObservationEvent, SqliteEventStore
from perpleximanus.core.events import ToolResult
from perpleximanus.tools.projects import ProjectStore, StorageStatus

XLSX = b"PK\x03\x04" + b"stub-workbook"  # zip magic — stands in for a real .xlsx


class _Session:
    """Serves bytes for EVERY path — a 404 means the route's jail fired first."""

    async def read_file(self, path: str) -> bytes:
        return XLSX


class _LiveRuntime:
    def set_surface(self, cid: str, surface: object) -> None: ...
    def set_model_override(self, cid: str, model: object) -> None: ...
    def set_depth(self, cid: str, tier: object) -> None: ...
    def sandbox_backend_name(self) -> str | None:
        return "process"

    def live_session(self, cid: str) -> _Session:
        return _Session()


class _SnapshotRuntime:
    """No live session — exercises the ProjectStore fallback (finished run)."""

    def __init__(self, ps: ProjectStore) -> None:
        self._ps = ps

    def set_surface(self, cid: str, surface: object) -> None: ...
    def set_model_override(self, cid: str, model: object) -> None: ...
    def set_depth(self, cid: str, tier: object) -> None: ...
    def sandbox_backend_name(self) -> str | None:
        return "process"

    def live_session(self, cid: str) -> None:
        return None

    def project_store(self) -> ProjectStore:
        return self._ps


def _declare_sheet(store: SqliteEventStore, cid: str, filename: str) -> None:
    """Append a successful sheet_generate observation so `filename` is a declared
    artifact (the only thing that makes a path downloadable)."""
    asyncio.run(
        store.append(
            cid,
            ObservationEvent(
                action_id="a1",
                tool_result=ToolResult(
                    call_id="c1",
                    tool_name="sheet_generate",
                    success=True,
                    content="ok",
                    structured={"filename": filename, "title": "T", "sheet_names": ["S"]},
                ),
            ),
        )
    )


@pytest.fixture
def live_client() -> TestClient:
    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store, runtime=_LiveRuntime()))  # type: ignore[arg-type]
    client._store = store  # type: ignore[attr-defined]
    return client


def _create(client: TestClient) -> str:
    return client.post("/conversations", json={"owner_id": "local"}).json()["conversation_id"]


# ---- declared + present → 200 with attachment headers -----------------------


def test_declared_xlsx_downloads_with_safe_headers(live_client: TestClient) -> None:
    cid = _create(live_client)
    _declare_sheet(live_client._store, cid, "budget.xlsx")  # type: ignore[attr-defined]
    r = live_client.get(f"/conversations/{cid}/artifacts/budget.xlsx")
    assert r.status_code == 200
    assert r.content == XLSX
    assert "spreadsheetml.sheet" in r.headers["content-type"]
    assert r.headers["content-disposition"] == 'attachment; filename="budget.xlsx"'
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "private, no-store"


def test_declared_in_subdir_downloads(live_client: TestClient) -> None:
    cid = _create(live_client)
    _declare_sheet(live_client._store, cid, "reports/q4.xlsx")  # type: ignore[attr-defined]
    r = live_client.get(f"/conversations/{cid}/artifacts/reports/q4.xlsx")
    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="q4.xlsx"'


# ---- the jail: undeclared / wrong-ext / traversal → 404 ---------------------


def test_undeclared_file_404_even_though_session_serves_it(live_client: TestClient) -> None:
    """The session would serve it, but it was never emitted as an artifact → 404."""
    cid = _create(live_client)
    _declare_sheet(live_client._store, cid, "budget.xlsx")  # type: ignore[attr-defined]
    assert live_client.get(f"/conversations/{cid}/artifacts/secrets.xlsx").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "app.py",  # wrong extension (and undeclared)
        "../etc/passwd",
        "%2e%2e/etc/passwd",
        "/etc/passwd",
        "budget.xlsx.exe",
    ],
)
def test_rejected_paths_404(live_client: TestClient, path: str) -> None:
    cid = _create(live_client)
    _declare_sheet(live_client._store, cid, "budget.xlsx")  # type: ignore[attr-defined]
    assert live_client.get(f"/conversations/{cid}/artifacts/{path}").status_code == 404


def test_declared_but_wrong_extension_still_404(live_client: TestClient) -> None:
    """Even a DECLARED non-xlsx artifact is rejected by the extension allowlist (v1)."""
    cid = _create(live_client)
    _declare_sheet(live_client._store, cid, "notes.txt")  # type: ignore[attr-defined]
    assert live_client.get(f"/conversations/{cid}/artifacts/notes.txt").status_code == 404


# ---- finished run → ProjectStore snapshot fallback --------------------------


def test_finished_run_falls_back_to_snapshot(tmp_path: Path) -> None:
    store = SqliteEventStore(":memory:")
    ps = ProjectStore(str(tmp_path))
    runtime = _SnapshotRuntime(ps)
    client = TestClient(create_app(store, runtime=runtime))  # type: ignore[arg-type]
    cid = _create(client)
    _declare_sheet(store, cid, "budget.xlsx")
    # write the file into the project workspace (simulating a snapshotted run)
    ws = ps.path_for(cid)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "budget.xlsx").write_bytes(XLSX)
    assert ps.status() == StorageStatus.OK

    r = client.get(f"/conversations/{cid}/artifacts/budget.xlsx")
    assert r.status_code == 200
    assert r.content == XLSX


def test_no_runtime_404(tmp_path: Path) -> None:
    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store))
    cid = _create(client)
    _declare_sheet(store, cid, "budget.xlsx")
    assert client.get(f"/conversations/{cid}/artifacts/budget.xlsx").status_code == 404
