"""Binding Reference Packs to a Build: snapshots are exact and conversation-owned,
the durable marker/index event is appended, the sandbox gets references/<slug>/,
a moved or missing pack is refused, and a live run refuses rebinding."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from disco.agent_server.reference_pack_binding import (
    BoundReferencePackStore,
    ReferencePackBindError,
    bind_reference_packs,
    context_message,
    markers_from_events,
    materialize,
    pack_index_markdown,
    select_packs,
)
from disco.agent_server.reference_pack_store import JsonReferencePackStore
from disco.core import SqliteEventStore
from disco.tools.projects import ProjectStore


class _Session:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data


class _Settings:
    def __init__(self, settable: bool = True) -> None:
        self.settable = settable

    async def apply_settings_change(self, conversation_id: str, **_: object) -> bool:
        return self.settable


class _Runtime:
    def __init__(self, root: Path, settable: bool = True) -> None:
        self._project_store = ProjectStore(str(root))
        self.projects = SimpleNamespace(current_project_store=lambda: self._project_store)
        self.settings = _Settings(settable)
        self.bound_reference_packs = BoundReferencePackStore(str(root / "db.refpacks"))
        self.session = _Session()
        self.sessions = SimpleNamespace(upload_session=lambda cid: self.session)


CID = "conv_" + "a" * 32


def _library(tmp_path: Path) -> tuple[JsonReferencePackStore, Path]:
    root = tmp_path / "projects"
    root.mkdir()
    return JsonReferencePackStore(root), root


async def test_bind_snapshots_marks_and_materialises(tmp_path: Path) -> None:
    library, root = _library(tmp_path)
    pack = library.create(
        owner_id="local",
        name="Brand Kit",
        description="Logos and copy",
        files=[("copy.md", b"# Voice"), ("logo.png", b"\x89PNG")],
    )
    runtime = _Runtime(root)
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local", surface="build")

    bound = await bind_reference_packs(runtime, store, CID, "local", [pack.pack_id])

    assert [b.slug for b in bound] == ["brand-kit"]
    assert bound[0].digest == pack.digest
    # exact bytes, conversation-owned
    assert runtime.bound_reference_packs.file_bytes(CID, bound[0], "copy.md") == b"# Voice"
    assert (
        root / "db.refpacks" / CID / pack.pack_id / "files" / "logo.png"
    ).read_bytes() == b"\x89PNG"
    # durable marker + compact index in context
    events = await store.get_events(CID)
    assert markers_from_events(events) == [bound[0].marker()]
    assert "references/brand-kit/PACK.md" in events[-1].message.content
    # materialised into the sandbox
    files = runtime.session.files
    assert files["references/brand-kit/copy.md"] == b"# Voice"
    assert files["references/brand-kit/logo.png"] == b"\x89PNG"
    index = files["references/brand-kit/PACK.md"].decode()
    assert (
        "Brand Kit" in index and "Asset only" in index and "`references/brand-kit/copy.md`" in index
    )


async def test_library_edits_after_binding_do_not_touch_the_snapshot(tmp_path: Path) -> None:
    library, root = _library(tmp_path)
    pack = library.create(owner_id="local", name="Spec", files=[("spec.md", b"v1")])
    runtime = _Runtime(root)
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local", surface="build")
    [bound] = await bind_reference_packs(runtime, store, CID, "local", [pack.pack_id])
    library.put_file(pack.pack_id, "local", "spec.md", b"v2")
    library.delete(pack.pack_id, "local")
    assert runtime.bound_reference_packs.file_bytes(CID, bound, "spec.md") == b"v1"
    assert runtime.bound_reference_packs.bound(CID)[0].digest == pack.digest
    # a fresh sandbox is re-materialised from the snapshot, not the library
    fresh = _Session()
    assert await materialize(fresh, runtime.bound_reference_packs, CID) == 1
    assert fresh.files["references/spec/spec.md"] == b"v1"


async def test_rebinding_replaces_the_set_and_an_empty_list_clears(tmp_path: Path) -> None:
    library, root = _library(tmp_path)
    one = library.create(owner_id="local", name="One", files=[("a.txt", b"a")])
    two = library.create(owner_id="local", name="Two", files=[("b.txt", b"b")])
    runtime = _Runtime(root)
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local", surface="build")
    await bind_reference_packs(runtime, store, CID, "local", [one.pack_id, two.pack_id])
    await bind_reference_packs(runtime, store, CID, "local", [two.pack_id])
    assert [b.pack_id for b in runtime.bound_reference_packs.bound(CID)] == [two.pack_id]
    assert not (root / "db.refpacks" / CID / one.pack_id).exists()
    await bind_reference_packs(runtime, store, CID, "local", [])
    assert runtime.bound_reference_packs.bound(CID) == []
    assert markers_from_events(await store.get_events(CID)) == []


async def test_refusals(tmp_path: Path) -> None:
    library, root = _library(tmp_path)
    pack = library.create(owner_id="local", name="P", files=[("a.txt", b"a")])
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local", surface="build")
    with pytest.raises(ReferencePackBindError) as live:
        await bind_reference_packs(
            _Runtime(root, settable=False), store, CID, "local", [pack.pack_id]
        )
    assert live.value.reason == "conversation_not_pristine"
    with pytest.raises(ReferencePackBindError) as other_owner:
        await bind_reference_packs(_Runtime(root), store, CID, "bob", [pack.pack_id])
    assert other_owner.value.reason == "reference_pack_missing"
    with pytest.raises(ReferencePackBindError) as moved:
        select_packs(library, "local", [pack.pack_id], {pack.pack_id: "0" * 64})
    assert moved.value.reason == "reference_pack_changed"
    with pytest.raises(ReferencePackBindError) as dup:
        select_packs(library, "local", [pack.pack_id, pack.pack_id], None)
    assert dup.value.reason == "duplicate_reference_pack"
    assert await store.get_events(CID) == [] or not markers_from_events(await store.get_events(CID))


def test_index_and_context_shapes() -> None:
    from disco.agent_server.reference_pack_binding import BoundPack
    from disco.agent_server.reference_pack_store import ReferencePackFile

    pack = BoundPack(
        pack_id="rp_" + "1" * 32,
        name="Brief",
        description="",
        digest="d" * 64,
        slug="brief",
        bound_at="2026-09-11T00:00:00Z",
        files=(
            ReferencePackFile(
                name="brief.pdf",
                media_type="application/pdf",
                bytes=10,
                sha256="s" * 64,
                state="ready",
            ),
        ),
    )
    text = pack_index_markdown(pack)
    assert "(no description)" in text and "`references/brief/brief.pdf.txt`" in text
    event = context_message([pack])
    assert event.meta == {"reference_packs": [pack.marker()]}
    assert "required reading" in event.message.content
    assert context_message([]).meta == {"reference_packs": []}
