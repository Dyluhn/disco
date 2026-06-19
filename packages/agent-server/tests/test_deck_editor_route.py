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


class _LiveRuntime:
    def __init__(self, session: _Session | None, ps: object = None) -> None:
        self._session = session
        self._ps = ps

    def set_surface(self, cid: str, surface: object) -> None: ...
    def set_model_override(self, cid: str, model: object) -> None: ...
    def set_depth(self, cid: str, tier: object) -> None: ...
    def get_last_selected_model(self) -> str | None:
        return None
    def sandbox_backend_name(self) -> str | None:
        return "process"

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
                    call_id="c2", tool_name="slides_generate", success=True,
                    content="ok", structured={"filename": f"{base}.html", "base_name": base},
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
        el for s in body["slides"] for el in s["elements"]
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
                    call_id="c1", tool_name="slides_generate", success=True,
                    content="ok", structured={"filename": "deck.html", "base_name": "deck"},
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
    _declare_marp_slides(store, cid)      # LATER non-editable same-base render
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
        el for s in body["lowered"]["slides"] for el in s["elements"]
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
    assert json.loads((ws / "deck.authored.json").read_text())["slides"][0]["title"] == "Q4 Highlights"


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
        .slides[0].title
        == "Q4 Highlights"
    )


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
