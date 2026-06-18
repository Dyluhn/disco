"""rp-11 residue / E7 extension — the declared-artifact download route: the
allowlist (only files the conversation EMITTED as results), the extension pin,
traversal defense, and the finished-run ProjectStore fallback.

Like test_workspace_route, the stub session serves ANY path, so a 404 proves the
route's OWN jail rejected the path before any read.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from disco.agent_server import create_app
from disco.core import ObservationEvent, SqliteEventStore
from disco.core.events import ToolResult
from disco.tools.projects import ProjectStore, StorageStatus
from fastapi.testclient import TestClient

XLSX = b"PK\x03\x04" + b"stub-workbook"  # zip magic — stands in for a real .xlsx
PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"stub-image"  # PNG signature
MP3_MAGIC = b"\xff\xfb\x90\x00" + b"stub-audio"   # MPEG frame header


HTML_STUB = b"<!DOCTYPE html><html><body><h1>Deck</h1></body></html>"

_STUB_BYTES: dict[str, bytes] = {
    ".xlsx": XLSX,
    ".pptx": XLSX,  # also a zip container
    ".png": PNG_MAGIC,
    ".mp3": MP3_MAGIC,
    ".md": b"# transcript\n",
    ".html": HTML_STUB,
}


class _Session:
    """Serves stub bytes keyed by extension — a 404 means the route's jail fired first."""

    async def read_file(self, path: str) -> bytes:
        import posixpath

        ext = posixpath.splitext(path)[1].lower()
        return _STUB_BYTES.get(ext, XLSX)


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


def _declare_slides(store: SqliteEventStore, cid: str, filename: str) -> None:
    """Append a successful slides_generate observation (structured["filename"] key)."""
    asyncio.run(
        store.append(
            cid,
            ObservationEvent(
                action_id="a2",
                tool_result=ToolResult(
                    call_id="c2",
                    tool_name="slides_generate",
                    success=True,
                    content="ok",
                    structured={"filename": filename, "base_name": filename.rsplit(".", 1)[0]},
                ),
            ),
        )
    )


def _declare_image(store: SqliteEventStore, cid: str, path: str) -> None:
    """Append a successful image_generate observation (structured["path"] key)."""
    asyncio.run(
        store.append(
            cid,
            ObservationEvent(
                action_id="a3",
                tool_result=ToolResult(
                    call_id="c3",
                    tool_name="image_generate",
                    success=True,
                    content="ok",
                    structured={
                        "path": path,
                        "filename_base": path.rsplit(".", 1)[0],
                        "format": "png",
                        "width": 512,
                        "height": 512,
                        "seed": 0,
                        "prompt": "test",
                        "backend": "stub",
                        "bytes": 16,
                        "magic_hex": "89504e470d0a1a0a",
                    },
                ),
            ),
        )
    )


def _declare_audio(store: SqliteEventStore, cid: str, mp3_path: str, transcript_path: str) -> None:
    """Append a successful audio_overview observation (mp3_path + transcript_path keys)."""
    asyncio.run(
        store.append(
            cid,
            ObservationEvent(
                action_id="a4",
                tool_result=ToolResult(
                    call_id="c4",
                    tool_name="audio_overview",
                    success=True,
                    content="ok",
                    structured={
                        "mp3_path": mp3_path,
                        "transcript_path": transcript_path,
                        "turn_count": 2,
                        "mp3_bytes": 1024,
                        "voice_a": "af_sky",
                        "voice_b": "am_adam",
                        "backend": "kokoro",
                        "silence_ms": 500,
                    },
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


# ---- E7: extended allowlist — slides / images / audio -----------------------


def test_declared_pptx_downloads_with_correct_content_type(live_client: TestClient) -> None:
    """slides_generate emits structured["filename"]; route must serve it as .pptx."""
    cid = _create(live_client)
    _declare_slides(live_client._store, cid, "deck.pptx")  # type: ignore[attr-defined]
    r = live_client.get(f"/conversations/{cid}/artifacts/deck.pptx")
    assert r.status_code == 200
    assert "presentationml.presentation" in r.headers["content-type"]
    assert r.headers["content-disposition"] == 'attachment; filename="deck.pptx"'
    assert r.headers["x-content-type-options"] == "nosniff"


def test_declared_png_downloads_with_correct_content_type(live_client: TestClient) -> None:
    """image_generate emits structured["path"]; route must serve it as image/png."""
    cid = _create(live_client)
    _declare_image(live_client._store, cid, "cover.png")  # type: ignore[attr-defined]
    r = live_client.get(f"/conversations/{cid}/artifacts/cover.png")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.headers["content-disposition"] == 'attachment; filename="cover.png"'


def test_declared_audio_mp3_downloads(live_client: TestClient) -> None:
    """audio_overview emits mp3_path + transcript_path; both must be downloadable."""
    cid = _create(live_client)
    _declare_audio(live_client._store, cid, "podcast.mp3", "podcast.md")  # type: ignore[attr-defined]
    r = live_client.get(f"/conversations/{cid}/artifacts/podcast.mp3")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/mpeg")
    assert r.headers["content-disposition"] == 'attachment; filename="podcast.mp3"'


def test_declared_audio_transcript_downloads(live_client: TestClient) -> None:
    """The transcript (.md) emitted alongside the MP3 must also be downloadable."""
    cid = _create(live_client)
    _declare_audio(live_client._store, cid, "podcast.mp3", "podcast.md")  # type: ignore[attr-defined]
    r = live_client.get(f"/conversations/{cid}/artifacts/podcast.md")
    assert r.status_code == 200
    assert "text/markdown" in r.headers["content-type"]


def test_undeclared_new_format_still_404(live_client: TestClient) -> None:
    """A .png that was NEVER emitted must 404 even though the extension is now allowed."""
    cid = _create(live_client)
    # only the xlsx is declared, not any png
    _declare_sheet(live_client._store, cid, "budget.xlsx")  # type: ignore[attr-defined]
    assert live_client.get(f"/conversations/{cid}/artifacts/secrets.png").status_code == 404


def test_disallowed_extension_still_404_after_e7(live_client: TestClient) -> None:
    """.txt is not in _ARTIFACT_TYPES even after the E7 allowlist expansion."""
    cid = _create(live_client)
    _declare_sheet(live_client._store, cid, "notes.txt")  # type: ignore[attr-defined]
    assert live_client.get(f"/conversations/{cid}/artifacts/notes.txt").status_code == 404


# ---- C5: ?inline=true — HTML inline mode with CSP / non-HTML 404 guard ------

_INLINE_CSP_FRAGMENT = "sandbox allow-scripts"


def test_html_inline_true_serves_inline_with_csp(live_client: TestClient) -> None:
    """C5: ?inline=true on a declared .html returns text/html + inline disposition +
    a strict Content-Security-Policy (sandbox; no allow-same-origin) so the artifact
    can be embedded in a sandboxed iframe without granting API access."""
    cid = _create(live_client)
    _declare_slides(live_client._store, cid, "deck.html")  # type: ignore[attr-defined]
    r = live_client.get(f"/conversations/{cid}/artifacts/deck.html?inline=true")
    assert r.status_code == 200
    assert r.content == HTML_STUB
    assert "text/html" in r.headers["content-type"]
    # Disposition must be INLINE (not attachment) so the browser renders it.
    assert r.headers["content-disposition"].startswith("inline;")
    assert 'filename="deck.html"' in r.headers["content-disposition"]
    # CSP must be present and must restrict the sandboxed page.
    csp = r.headers["content-security-policy"]
    assert _INLINE_CSP_FRAGMENT in csp          # sandbox allow-scripts
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'self'" in csp
    # The existing nosniff header must be preserved.
    assert r.headers["x-content-type-options"] == "nosniff"


def test_html_without_inline_remains_attachment(live_client: TestClient) -> None:
    """C5: the default route (no ?inline=true) continues to return 'attachment' for
    HTML — no regression to the download path."""
    cid = _create(live_client)
    _declare_slides(live_client._store, cid, "deck.html")  # type: ignore[attr-defined]
    r = live_client.get(f"/conversations/{cid}/artifacts/deck.html")
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment;")
    assert "content-security-policy" not in r.headers


def test_inline_true_non_html_returns_404(live_client: TestClient) -> None:
    """C5: ?inline=true is restricted to .html only — all other declared extensions
    must 404 so the inline allowlist stays minimal."""
    cid = _create(live_client)
    _declare_sheet(live_client._store, cid, "budget.xlsx")  # type: ignore[attr-defined]
    _declare_image(live_client._store, cid, "cover.png")  # type: ignore[attr-defined]
    assert live_client.get(f"/conversations/{cid}/artifacts/budget.xlsx?inline=true").status_code == 404
    assert live_client.get(f"/conversations/{cid}/artifacts/cover.png?inline=true").status_code == 404
