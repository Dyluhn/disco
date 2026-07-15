"""A2.1 / A2.3 — the in-app deck editor routes.

GET returns the lowered deck for an editable (authored-sidecar-carrying) deck;
PUT applies a JSON Patch, re-renders, writes back, and returns the new lowered
deck. The jail: only a DECLARED authored.json is reachable; a bad patch → 422 with
the workspace untouched; no live session → 409 (no writes).

The stub session records writes so the "untouched on 422 / no-write on 409" and
"all three written on success" invariants are checked directly.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from disco.agent_server import create_app
from disco.core import ObservationEvent, SqliteEventStore
from disco.core.events import ToolResult
from disco.tools.builtin._deck_schema import AuthoredDeck
from fastapi.testclient import TestClient

# A minimal real AuthoredDeck that lowers + renders cleanly.
_AUTHORED = {
    "title": "Quarterly Review",
    "theme": "disco-light",
    "slides": [
        {
            "type": "title",
            "title": "Q4 Highlights",
            "body": ["A strong close to the year"],
            "layout_hint": None,
            "image_prompt": None,
            "chart": None,
            "table": None,
            "notes": None,
        },
        {
            "type": "bullets",
            "title": "Wins",
            "body": ["Revenue up 20%", "Churn down 5%"],
            "layout_hint": None,
            "image_prompt": None,
            "chart": None,
            "table": None,
            "notes": None,
        },
    ],
}


class _Session:
    """Serves the authored.json + records every write so invariants are checkable."""

    def __init__(self, authored: dict) -> None:
        self._files: dict[str, bytes] = {
            "deck.authored.json": json.dumps(authored, indent=2).encode()
        }
        self.writes: list[str] = []

    async def read_file(self, path: str) -> bytes:
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self._files[path] = data
        self.writes.append(path)


class _FakeExec:
    """A minimal ExecResult stand-in for the throwaway PDF-render sandbox."""

    def __init__(self, exit_code: int = 0, stderr: str = "", timed_out: bool = False) -> None:
        self.exit_code = exit_code
        self.stderr = stderr
        self.timed_out = timed_out


class _FakeSandboxInstance:
    """Records the convert command + serves a soffice-shaped %PDF on read.

    ``probe_exit`` controls the BW-10 ``command -v soffice`` probe result
    independently of the convert exec, so tests can simulate a stale image
    (no LibreOffice → probe_exit != 0) vs a conversion-time failure."""

    def __init__(self, exec_result: _FakeExec, pdf_bytes: bytes, *, probe_exit: int = 0) -> None:
        self._exec = exec_result
        self._pdf = pdf_bytes
        self._probe_exit = probe_exit
        self.cmds: list[str] = []
        self.destroyed = False
        self.written: dict[str, bytes] = {}

    async def write_file(self, path: str, data: bytes) -> None:
        self.written[path] = data

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> _FakeExec:
        self.cmds.append(cmd)
        if cmd.strip() == "command -v soffice":
            return _FakeExec(exit_code=self._probe_exit)
        return self._exec

    async def read_file(self, path: str) -> bytes:
        return self._pdf

    async def destroy(self) -> None:
        self.destroyed = True


class _FakeSandboxService:
    def __init__(self, name: str, instance: _FakeSandboxInstance) -> None:
        self.name = name
        self._instance = instance
        self.created = False

    async def create(
        self, spec: object, *, owner_id: str, conversation_id: str
    ) -> _FakeSandboxInstance:
        self.created = True
        return self._instance


class _LiveRuntime:
    def __init__(
        self,
        session: _Session | None,
        ps: object = None,
        *,
        backend: str = "process",
        sandbox_service: _FakeSandboxService | None = None,
    ) -> None:
        self._session = session
        self._ps = ps
        self._backend = backend
        self._svc = sandbox_service
        self._sandbox_spec = object()

    def set_surface(self, cid: str, surface: object) -> None: ...
    def set_model_override(self, cid: str, model: object) -> None: ...
    def set_depth(self, cid: str, tier: object) -> None: ...
    def get_last_selected_model(self) -> str | None:
        return None

    def sandbox_backend_name(self) -> str | None:
        return self._backend

    def _sandbox_service_now(self) -> _FakeSandboxService:
        assert self._svc is not None, "no sandbox service injected"
        return self._svc

    def live_session(self, cid: str) -> _Session | None:
        return self._session

    def project_store(self) -> object:
        # No host snapshot by default — the live session is the only source.
        return self._ps


def _declare_editable_slides(store: SqliteEventStore, cid: str) -> None:
    """A successful C2 slides_generate observation carrying editable_source — the
    only thing that makes deck.authored.json a reachable artifact."""
    asyncio.run(
        store.append(
            cid,
            ObservationEvent(
                action_id="a1",
                tool_result=ToolResult(
                    call_id="c1",
                    tool_name="slides_generate",
                    success=True,
                    content="ok",
                    structured={
                        "filename": "deck.html",
                        "base_name": "deck",
                        "editable_source": "deck.authored.json",
                    },
                ),
            ),
        )
    )


def _declare_marp_slides(store: SqliteEventStore, cid: str, base: str = "deck") -> None:
    """A LATER same-base render to a non-editable format (Marp/fallback): sets
    base_name but NO editable_source. It supersedes any earlier editable sidecar."""
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
                    structured={"filename": f"{base}.html", "base_name": base},
                ),
            ),
        )
    )


def _client(session: _Session | None, ps: object = None) -> tuple[TestClient, SqliteEventStore]:
    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store, runtime=_LiveRuntime(session, ps)))  # type: ignore[arg-type]
    return client, store


def _create(client: TestClient) -> str:
    return client.post("/conversations", json={"owner_id": "local"}).json()["conversation_id"]


# ---- GET --------------------------------------------------------------------


def test_get_returns_lowered_deck() -> None:
    session = _Session(_AUTHORED)
    client, store = _client(session)
    cid = _create(client)
    _declare_editable_slides(store, cid)

    r = client.get(f"/conversations/{cid}/deck/editor?path=deck")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "Quarterly Review"
    assert len(body["slides"]) == 2
    # The first slide's title element points at /slides/0/title with the right text.
    titles = [
        el
        for s in body["slides"]
        for el in s["elements"]
        if el["json_pointer"] == "/slides/0/title"
    ]
    assert titles and titles[0]["content"] == "Q4 Highlights"


def test_get_undeclared_deck_404() -> None:
    """A deck whose authored.json was never declared (no editable_source) → 404."""
    session = _Session(_AUTHORED)
    client, store = _client(session)
    cid = _create(client)
    # declare a NON-editable slides obs (no editable_source key)
    asyncio.run(
        store.append(
            cid,
            ObservationEvent(
                action_id="a1",
                tool_result=ToolResult(
                    call_id="c1",
                    tool_name="slides_generate",
                    success=True,
                    content="ok",
                    structured={"filename": "deck.html", "base_name": "deck"},
                ),
            ),
        )
    )
    assert client.get(f"/conversations/{cid}/deck/editor?path=deck").status_code == 404


def test_get_superseded_sidecar_404() -> None:
    """An editable C2 deck later RE-rendered to the same base as a non-editable Marp
    deck: the stale sidecar must NOT be offered (data-loss + false-affordance guard)."""
    session = _Session(_AUTHORED)
    client, store = _client(session)
    cid = _create(client)
    _declare_editable_slides(store, cid)  # editable C2 render
    _declare_marp_slides(store, cid)  # LATER non-editable same-base render
    assert client.get(f"/conversations/{cid}/deck/editor?path=deck").status_code == 404


def test_put_superseded_sidecar_404_no_overwrite() -> None:
    """And PUT refuses too — editing the stale sidecar would overwrite the newer
    (Marp) deck. Nothing is written."""
    session = _Session(_AUTHORED)
    client, store = _client(session)
    cid = _create(client)
    _declare_editable_slides(store, cid)
    _declare_marp_slides(store, cid)
    r = client.put(
        f"/conversations/{cid}/deck/editor?path=deck",
        json={"patch": [{"op": "replace", "path": "/slides/0/title", "value": "x"}]},
    )
    assert r.status_code == 404
    assert session.writes == []


@pytest.mark.parametrize("path", ["../etc/passwd", "/etc/passwd", "sub/deck"])
def test_get_traversal_404(path: str) -> None:
    session = _Session(_AUTHORED)
    client, store = _client(session)
    cid = _create(client)
    _declare_editable_slides(store, cid)
    assert client.get(f"/conversations/{cid}/deck/editor", params={"path": path}).status_code == 404


# ---- PUT --------------------------------------------------------------------


def test_put_text_replace_updates_lowered() -> None:
    session = _Session(_AUTHORED)
    client, store = _client(session)
    cid = _create(client)
    _declare_editable_slides(store, cid)

    r = client.put(
        f"/conversations/{cid}/deck/editor?path=deck",
        json={"patch": [{"op": "replace", "path": "/slides/0/title", "value": "Q4 Recap"}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["html_file"] == "deck.html"
    assert body["pptx_file"] == "deck.pptx"
    # The returned lowered deck reflects the new title.
    titles = [
        el
        for s in body["lowered"]["slides"]
        for el in s["elements"]
        if el["json_pointer"] == "/slides/0/title"
    ]
    assert titles and titles[0]["content"] == "Q4 Recap"
    # All three files were written back, and the persisted authored.json validates.
    assert set(session.writes) == {"deck.authored.json", "deck.html", "deck.pptx"}
    persisted = AuthoredDeck.model_validate(json.loads(session._files["deck.authored.json"]))
    assert persisted.slides[0].title == "Q4 Recap"


def test_put_invalid_patch_422_workspace_untouched() -> None:
    session = _Session(_AUTHORED)
    client, store = _client(session)
    cid = _create(client)
    _declare_editable_slides(store, cid)

    # A patch that removes a required field → AuthoredDeck validation fails.
    r = client.put(
        f"/conversations/{cid}/deck/editor?path=deck",
        json={"patch": [{"op": "remove", "path": "/title"}]},
    )
    assert r.status_code == 422, r.text
    # NOTHING was written — the workspace is untouched.
    assert session.writes == []


def test_put_bad_pointer_422() -> None:
    """A patch against a nonexistent path → PatchError → 422, no writes."""
    session = _Session(_AUTHORED)
    client, store = _client(session)
    cid = _create(client)
    _declare_editable_slides(store, cid)

    r = client.put(
        f"/conversations/{cid}/deck/editor?path=deck",
        json={"patch": [{"op": "replace", "path": "/slides/9/title", "value": "x"}]},
    )
    assert r.status_code == 422, r.text
    assert session.writes == []


def test_put_no_live_session_409(tmp_path) -> None:
    """A finished run: the authored.json is readable from the host snapshot, but
    there is NO live session to write back → 409 {reason: no_live_sandbox}."""
    from disco.tools.projects import ProjectStore

    store = SqliteEventStore(":memory:")
    ps = ProjectStore(str(tmp_path))
    client = TestClient(create_app(store, runtime=_LiveRuntime(None, ps)))  # type: ignore[arg-type]
    cid = _create(client)
    _declare_editable_slides(store, cid)
    # Snapshot the authored.json into the project workspace (sandbox reaped).
    ws = ps.path_for(cid)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "deck.authored.json").write_text(json.dumps(_AUTHORED, indent=2))

    r = client.put(
        f"/conversations/{cid}/deck/editor?path=deck",
        json={"patch": [{"op": "replace", "path": "/slides/0/title", "value": "Q4 Recap"}]},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["reason"] == "no_live_sandbox"
    # The snapshot was NOT mutated (no write path on a finished run).
    snapshot = json.loads((ws / "deck.authored.json").read_text())
    assert snapshot["slides"][0]["title"] == "Q4 Highlights"


def test_put_undeclared_404() -> None:
    session = _Session(_AUTHORED)
    client, store = _client(session)
    cid = _create(client)  # nothing declared
    r = client.put(
        f"/conversations/{cid}/deck/editor?path=deck",
        json={"patch": [{"op": "replace", "path": "/slides/0/title", "value": "x"}]},
    )
    assert r.status_code == 404


def test_put_bad_move_from_is_clean_422() -> None:
    """A move/copy with an invalid `from` raises a raw KeyError/IndexError inside
    apply_patch (not a PatchError) — the route must still surface a clean 422, never
    a 500, and never write."""
    session = _Session(_AUTHORED)
    client, store = _client(session)
    cid = _create(client)
    _declare_editable_slides(store, cid)

    r = client.put(
        f"/conversations/{cid}/deck/editor?path=deck",
        json={"patch": [{"op": "move", "from": "/slides/9/title", "path": "/title"}]},
    )
    assert r.status_code == 422, r.text
    assert session.writes == []


def test_put_flags_stale_pdf_when_one_exists() -> None:
    """A deck with a previously-exported deck.pdf: editing re-renders html+pptx but
    NOT the pdf, so the response must flag pdf_stale=True (detected by the PDF's
    actual existence, since the executor never declares it)."""
    session = _Session(_AUTHORED)
    session._files["deck.pdf"] = b"%PDF-1.7 stale"  # a prior export
    client, store = _client(session)
    cid = _create(client)
    _declare_editable_slides(store, cid)

    r = client.put(
        f"/conversations/{cid}/deck/editor?path=deck",
        json={"patch": [{"op": "replace", "path": "/slides/0/title", "value": "Q4 Recap"}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["pdf_stale"] is True
    # The stale PDF itself was NOT rewritten (only authored/html/pptx).
    assert "deck.pdf" not in session.writes


def test_put_rolls_back_on_mid_write_failure() -> None:
    """All-or-nothing write-back: if the HTML write fails after the authored.json
    write, the authored.json is rolled back to its prior content (no half-updated
    workspace), and the route returns 500 — never a silent partial success."""

    class _FailHtml(_Session):
        async def write_file(self, path: str, data: bytes) -> None:
            if path == "deck.html":
                raise OSError("disk full")
            await super().write_file(path, data)

    session = _FailHtml(_AUTHORED)
    session._files["deck.html"] = b"<old/>"  # prior renders that must survive intact
    session._files["deck.pptx"] = b"OLDPPTX"
    client, store = _client(session)
    cid = _create(client)
    _declare_editable_slides(store, cid)

    r = client.put(
        f"/conversations/{cid}/deck/editor?path=deck",
        json={"patch": [{"op": "replace", "path": "/slides/0/title", "value": "Q4 Recap"}]},
    )
    assert r.status_code == 500, r.text
    # authored.json was written, then rolled back to the ORIGINAL on the html failure.
    restored = AuthoredDeck.model_validate(json.loads(session._files["deck.authored.json"]))
    assert restored.slides[0].title == "Q4 Highlights"
    # The prior PPTX was never touched (the failure happened before it).
    assert session._files["deck.pptx"] == b"OLDPPTX"


def test_put_rollback_restores_the_corrupted_failed_target() -> None:
    """The hardest partial-write case: the write that FAILS may have already
    truncated/corrupted its target. Rollback must restore THAT file too (not just the
    ones that wrote cleanly), leaving the whole workspace at its prior content."""

    class _CorruptPptx(_Session):
        """The first write to deck.pptx truncates then fails (transient); the rollback
        restore (a later write of the smaller prior bytes) succeeds."""

        def __init__(self, authored: dict) -> None:
            super().__init__(authored)
            self._pptx_attempts = 0

        async def write_file(self, path: str, data: bytes) -> None:
            if path == "deck.pptx":
                self._pptx_attempts += 1
                if self._pptx_attempts == 1:
                    self._files[path] = b"CORRUPT-HALF-WRITTEN"  # partial write
                    raise OSError("disk full mid-write")
            await super().write_file(path, data)

    session = _CorruptPptx(_AUTHORED)
    session._files["deck.html"] = b"<old/>"
    session._files["deck.pptx"] = b"OLDPPTX"
    client, store = _client(session)
    cid = _create(client)
    _declare_editable_slides(store, cid)

    r = client.put(
        f"/conversations/{cid}/deck/editor?path=deck",
        json={"patch": [{"op": "replace", "path": "/slides/0/title", "value": "Q4 Recap"}]},
    )
    assert r.status_code == 500, r.text
    # Every file is back to its prior content — including the corrupted pptx.
    assert session._files["deck.pptx"] == b"OLDPPTX"
    assert session._files["deck.html"] == b"<old/>"
    assert (
        AuthoredDeck.model_validate(json.loads(session._files["deck.authored.json"]))
        .slides[0]
        .title
        == "Q4 Highlights"
    )


# ---- GET /deck/editor/render (inline HTML, no attachment) ------------------


def test_render_returns_200_html_no_attachment() -> None:
    """The inline render route returns text/html, has data-element-id stamps, and
    does NOT set Content-Disposition: attachment (it is an inline view, not a download)."""
    session = _Session(_AUTHORED)
    client, store = _client(session)
    cid = _create(client)
    _declare_editable_slides(store, cid)

    r = client.get(f"/conversations/{cid}/deck/editor/render?path=deck")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    # No attachment header — the inline render must not trigger a download.
    cd = r.headers.get("content-disposition", "")
    assert "attachment" not in cd, f"Unexpected attachment header: {cd!r}"
    # The render stamps data-element-id on editable elements (required for overlay measurement).
    assert "data-element-id=" in r.text
    # Slide sections carry data-slide-id so the editor can toggle the active slide.
    assert "data-slide-id=" in r.text


def test_render_undeclared_404() -> None:
    """A render request for an undeclared deck → 404 (mirrors GET /editor)."""
    session = _Session(_AUTHORED)
    client, store = _client(session)
    cid = _create(client)
    r = client.get(f"/conversations/{cid}/deck/editor/render?path=deck")
    assert r.status_code == 404


def test_render_superseded_sidecar_404() -> None:
    """A later non-editable render supersedes the sidecar → inline render also 404."""
    session = _Session(_AUTHORED)
    client, store = _client(session)
    cid = _create(client)
    _declare_editable_slides(store, cid)
    _declare_marp_slides(store, cid)  # supersedes the editable render
    r = client.get(f"/conversations/{cid}/deck/editor/render?path=deck")
    assert r.status_code == 404


def test_render_invalid_template_400() -> None:
    """An unknown template id → 400 (not silently treated as the default)."""
    session = _Session(_AUTHORED)
    client, store = _client(session)
    cid = _create(client)
    _declare_editable_slides(store, cid)
    r = client.get(f"/conversations/{cid}/deck/editor/render?path=deck&template=does-not-exist")
    assert r.status_code == 400


def test_render_uses_deck_own_theme_when_no_template() -> None:
    """When template is omitted the route uses the deck's own theme (no 400)."""
    session = _Session(_AUTHORED)
    client, store = _client(session)
    cid = _create(client)
    _declare_editable_slides(store, cid)
    r = client.get(f"/conversations/{cid}/deck/editor/render?path=deck")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ---- W-22: themed deck → PDF export (throwaway-sandbox soffice) -------------


def _pdf_client(
    session: _Session | None, *, backend: str, svc: _FakeSandboxService | None
) -> tuple[TestClient, SqliteEventStore]:
    store = SqliteEventStore(":memory:")
    rt = _LiveRuntime(session, None, backend=backend, sandbox_service=svc)
    client = TestClient(create_app(store, runtime=rt))  # type: ignore[arg-type]
    return client, store


def test_export_pdf_container_backend_returns_real_pdf() -> None:
    """fmt=pdf on a container backend renders the .pptx (real render_pptx) then converts
    it in a THROWAWAY sandbox; the route returns application/pdf %PDF bytes and tears the
    box down. The soffice convert command is the expected headless one."""
    inst = _FakeSandboxInstance(_FakeExec(exit_code=0), b"%PDF-1.7\n<<soffice output>>\n%%EOF")
    svc = _FakeSandboxService("gvisor", inst)
    session = _Session(_AUTHORED)
    client, store = _pdf_client(session, backend="gvisor", svc=svc)
    cid = _create(client)
    _declare_editable_slides(store, cid)

    r = client.get(f"/conversations/{cid}/deck/export?path=deck&fmt=pdf&template=disco-light")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert 'filename="deck.pdf"' in r.headers.get("content-disposition", "")
    assert r.content.startswith(b"%PDF")
    # The throwaway sandbox actually ran soffice and was destroyed.
    assert svc.created is True
    assert inst.destroyed is True
    assert "_deck.pptx" in inst.written
    assert any("soffice --headless --convert-to pdf" in c for c in inst.cmds)


def test_export_pdf_no_container_backend_409_typed_reason() -> None:
    """fmt=pdf on the host `process` backend → 409 with a typed no_container_backend
    reason (the UI hides the button on the same gate — no false affordance). No sandbox
    is created and no render work is done."""
    svc = _FakeSandboxService("process", _FakeSandboxInstance(_FakeExec(), b""))
    session = _Session(_AUTHORED)
    client, store = _pdf_client(session, backend="process", svc=svc)
    cid = _create(client)
    _declare_editable_slides(store, cid)

    r = client.get(f"/conversations/{cid}/deck/export?path=deck&fmt=pdf")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["reason"] == "no_container_backend"
    assert svc.created is False  # gated BEFORE any sandbox spin / render


def test_export_pdf_soffice_failure_422() -> None:
    """A non-zero soffice exit inside the sandbox → 422 render_failed (box still torn down)."""
    inst = _FakeSandboxInstance(_FakeExec(exit_code=1, stderr="boom"), b"")
    svc = _FakeSandboxService("local", inst)
    session = _Session(_AUTHORED)
    client, store = _pdf_client(session, backend="local", svc=svc)
    cid = _create(client)
    _declare_editable_slides(store, cid)

    r = client.get(f"/conversations/{cid}/deck/export?path=deck&fmt=pdf")
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["reason"] == "render_failed"
    assert inst.destroyed is True


def test_export_pdf_stale_image_409_pdf_unavailable() -> None:
    """BW-10: a container backend whose image has NO LibreOffice (soffice probe fails)
    → honest 409 pdf_unavailable, NOT a generic render_failed. The box is torn down."""
    inst = _FakeSandboxInstance(_FakeExec(exit_code=0), b"", probe_exit=127)
    svc = _FakeSandboxService("gvisor", inst)
    session = _Session(_AUTHORED)
    client, store = _pdf_client(session, backend="gvisor", svc=svc)
    cid = _create(client)
    _declare_editable_slides(store, cid)

    r = client.get(f"/conversations/{cid}/deck/export?path=deck&fmt=pdf")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["reason"] == "pdf_unavailable"
    assert "this sandbox image" in r.json()["detail"]["message"]
    # Probe ran but no convert was attempted; box still torn down.
    assert any(c.strip() == "command -v soffice" for c in inst.cmds)
    assert not any("--convert-to pdf" in c for c in inst.cmds)
    assert inst.destroyed is True


def test_capabilities_reports_pdf_true_when_soffice_present() -> None:
    """BW-10: the capabilities probe spins a box, finds soffice, reports pdf:true."""
    inst = _FakeSandboxInstance(_FakeExec(exit_code=0), b"", probe_exit=0)
    svc = _FakeSandboxService("gvisor", inst)
    client, _store = _pdf_client(_Session(_AUTHORED), backend="gvisor", svc=svc)
    cid = _create(client)
    r = client.get(f"/conversations/{cid}/deck/export/capabilities")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"pptx": True, "html": True, "pdf": True, "pdf_reason": None}
    assert inst.destroyed is True


def test_capabilities_reports_pdf_false_on_stale_image() -> None:
    """BW-10: container backend but soffice absent → pdf:false, reason soffice_missing."""
    inst = _FakeSandboxInstance(_FakeExec(exit_code=0), b"", probe_exit=127)
    svc = _FakeSandboxService("local", inst)
    client, _store = _pdf_client(_Session(_AUTHORED), backend="local", svc=svc)
    cid = _create(client)
    r = client.get(f"/conversations/{cid}/deck/export/capabilities")
    assert r.json() == {"pptx": True, "html": True, "pdf": False, "pdf_reason": "soffice_missing"}


def test_capabilities_reports_pdf_false_no_container_backend() -> None:
    """BW-10: host process backend → pdf:false WITHOUT spinning a box (cheap pre-gate)."""
    svc = _FakeSandboxService("process", _FakeSandboxInstance(_FakeExec(), b""))
    client, _store = _pdf_client(_Session(_AUTHORED), backend="process", svc=svc)
    cid = _create(client)
    r = client.get(f"/conversations/{cid}/deck/export/capabilities")
    assert r.json()["pdf"] is False
    assert r.json()["pdf_reason"] == "no_container_backend"
    assert svc.created is False  # no sandbox spun for a non-container backend


def test_put_no_pdf_means_not_stale() -> None:
    """No prior PDF export → nothing to go stale → pdf_stale False."""
    session = _Session(_AUTHORED)
    client, store = _client(session)
    cid = _create(client)
    _declare_editable_slides(store, cid)

    r = client.put(
        f"/conversations/{cid}/deck/editor?path=deck",
        json={"patch": [{"op": "replace", "path": "/slides/0/title", "value": "Q4 Recap"}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["pdf_stale"] is False
