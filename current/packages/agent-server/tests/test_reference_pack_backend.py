from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path

import pytest
from disco.agent_server.reference_pack_binding import (
    ReferencePackBindingConflict,
    ReferencePackBindingStore,
    ReferencePackSelection,
    _pdf_text_with_page_markers,
    amaterialize_reference_binding,
    materialize_reference_binding,
    materialized_pack_index_paths,
)
from disco.agent_server.reference_pack_inspection import reference_inspect
from disco.agent_server.reference_pack_store import (
    ReferencePackError,
    ReferencePackLimits,
    ReferencePackNotFound,
    ReferencePackStore,
)
from disco.agent_server.routes import reference_packs as reference_pack_routes
from disco.agent_server.routes.reference_packs import make_reference_packs_router
from fastapi import FastAPI
from fastapi.testclient import TestClient


class Reader:
    def __init__(self, files: dict[str, bytes], *, symlinks: set[str] | None = None) -> None:
        self.files = files
        self.symlinks = symlinks or set()

    def is_file(self, path: str) -> bool:
        return path in self.files and path not in self.symlinks

    def is_symlink(self, path: str) -> bool:
        return path in self.symlinks

    async def read_file(self, path: str) -> bytes:
        return self.files[path]


class RemoteTarget:
    """A remote-like target with no host path or directory semantics."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data

    async def read_file(self, path: str) -> bytes:
        return self.files[path]


def _create(store: ReferencePackStore, owner: str, files: dict[str, bytes]):
    return asyncio.run(
        store.acreate(owner, "Pack", "description", list(files), reader=Reader(files))
    )


def test_sync_create_runs_inside_the_store_mutation_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ReferencePackStore(tmp_path / "packs")
    lock_held = False

    @contextmanager
    def mutation_lock():
        nonlocal lock_held
        assert not lock_held
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    class LockCheckingReader:
        def is_file(self, _path: str) -> bool:
            return True

        def is_symlink(self, _path: str) -> bool:
            return False

        def read_bytes(self, _path: str) -> bytes:
            assert lock_held
            return b"reference"

    monkeypatch.setattr(store, "mutation_lock", mutation_lock)
    pack = store.create("owner", "Pack", "", ["reference.txt"], reader=LockCheckingReader())

    assert pack.current.files[0].name == "reference.txt"
    assert not lock_held


def test_owner_isolation_and_exact_order_hashes(tmp_path: Path) -> None:
    store = ReferencePackStore(tmp_path / "packs")
    pack = _create(store, "owner/a", {"z.txt": b"z", "a.txt": b"a"})
    assert [item.name for item in pack.current.files] == ["a.txt", "z.txt"]
    assert store.get("owner/a", pack.id).current.files[0].sha256
    with pytest.raises((ReferencePackNotFound, PermissionError)):
        store.get("owner/b", pack.id)
    assert store.list("owner/b") == ()


def test_ingress_rejects_paths_duplicates_symlinks_and_limits(tmp_path: Path) -> None:
    store = ReferencePackStore(
        tmp_path / "packs",
        limits=ReferencePackLimits(max_files=1, max_file_bytes=2, max_total_bytes=2),
    )
    reader = Reader({"ok.txt": b"ok", "two.txt": b"ok", "link.txt": b"ok"}, symlinks={"link.txt"})
    for files in (
        ("../ok.txt",),
        ("PACK.md",),
        ("ok.txt", "OK.TXT"),
        ("link.txt",),
        ("ok.txt", "two.txt"),
    ):
        with pytest.raises(ReferencePackError):
            asyncio.run(store.acreate("owner", "P", "", list(files), reader=reader))


def test_async_reader_must_prove_regular_file(tmp_path: Path) -> None:
    class Untrusted:
        async def read_file(self, path: str) -> bytes:
            return b"x"

    with pytest.raises(ReferencePackError, match="prove"):
        asyncio.run(
            ReferencePackStore(tmp_path).acreate("owner", "P", "", ["x"], reader=Untrusted())
        )


def test_snapshot_survives_update_delete_and_restart(tmp_path: Path) -> None:
    packs = ReferencePackStore(tmp_path / "packs")
    original = _create(packs, "owner", {"note.txt": b"old"})
    snapshots = ReferencePackBindingStore(tmp_path / "snapshots")
    selected = ReferencePackSelection(
        original.id, original.current_version_id, original.current.content_sha256
    )
    snapshots.bind("owner", "conv_1", [selected], packs=packs)
    updated = asyncio.run(
        packs.aupdate("owner", original.id, files=["note.txt"], reader=Reader({"note.txt": b"new"}))
    )
    assert updated.current_version_id != original.current_version_id
    pack_root = packs._pack_root("owner", original.id)
    assert not (pack_root / "versions" / original.current_version_id).exists()
    assert (pack_root / "versions" / updated.current_version_id).is_dir()
    with pytest.raises(ReferencePackBindingConflict):
        snapshots.bind(
            "owner",
            "conv_1",
            [
                ReferencePackSelection(
                    original.id, updated.current_version_id, updated.current.content_sha256
                )
            ],
            packs=packs,
        )
    packs.delete("owner", original.id)
    assert not pack_root.exists()
    assert not tuple(pack_root.parent.glob(f".deleted-{original.id}-*"))
    restored = ReferencePackBindingStore(tmp_path / "snapshots").get("owner", "conv_1")
    assert restored is not None
    assert snapshots.read_file(restored, original.id, "note.txt") == b"old"


def test_settings_file_batch_replaces_adds_and_removes_atomically(tmp_path: Path) -> None:
    packs = ReferencePackStore(tmp_path / "packs")
    original = _create(packs, "owner", {"note.txt": b"old", "keep.txt": b"keep"})
    snapshots = ReferencePackBindingStore(tmp_path / "snapshots")
    binding = snapshots.bind(
        "owner",
        "conv_files",
        [
            ReferencePackSelection(
                original.id, original.current_version_id, original.current.content_sha256
            )
        ],
        packs=packs,
    )
    updated = packs.update_files(
        "owner",
        original.id,
        uploads=[("note.txt", b"new", "text/plain"), ("added.md", b"added", "text/markdown")],
        remove=["keep.txt"],
    )
    assert {item.name for item in updated.current.files} == {"added.md", "note.txt"}
    assert (
        packs.read_version_file("owner", original.id, updated.current_version_id, "note.txt")
        == b"new"
    )
    assert snapshots.read_file(binding, original.id, "note.txt") == b"old"

    before = packs.get("owner", original.id)
    with pytest.raises(ReferencePackError):
        packs.update_files(
            "owner",
            original.id,
            uploads=[("../escape.txt", b"bad", "text/plain")],
            remove=[],
        )
    after = packs.get("owner", original.id)
    assert after.current_version_id == before.current_version_id
    assert [item.name for item in after.current.files] == [
        item.name for item in before.current.files
    ]


def test_corrupt_preexisting_binding_fails_closed(tmp_path: Path) -> None:
    packs = ReferencePackStore(tmp_path / "packs")
    pack = _create(packs, "owner", {"note.txt": b"old"})
    snapshots = ReferencePackBindingStore(tmp_path / "snapshots")
    target = snapshots._conversation_root("owner", "conv_corrupt")
    target.mkdir(parents=True)
    with pytest.raises(ReferencePackError, match="unreadable"):
        snapshots.bind(
            "owner",
            "conv_corrupt",
            [ReferencePackSelection(pack.id, pack.current_version_id, pack.current.content_sha256)],
            packs=packs,
        )


def test_existing_exact_binding_wins_repeated_admission(tmp_path: Path) -> None:
    packs = ReferencePackStore(tmp_path / "packs")
    pack = _create(packs, "owner", {"note.txt": b"old"})
    snapshots = ReferencePackBindingStore(tmp_path / "snapshots")
    selection = ReferencePackSelection(
        pack.id, pack.current_version_id, pack.current.content_sha256
    )
    first = snapshots.bind("owner", "conv_race", [selection], packs=packs)
    second = snapshots.bind("owner", "conv_race", [selection], packs=packs)
    assert second.id == first.id


def test_materialization_and_bounded_pack_markdown(tmp_path: Path) -> None:
    packs = ReferencePackStore(tmp_path / "packs")
    pack = _create(packs, "owner", {"image.png": b"pixels"})
    snapshots = ReferencePackBindingStore(tmp_path / "snapshots")
    binding = snapshots.bind(
        "owner",
        "conv_1",
        [ReferencePackSelection(pack.id, pack.current_version_id, pack.current.content_sha256)],
        packs=packs,
    )
    materialize_reference_binding(
        binding, snapshots, tmp_path / "workspace", max_pack_markdown_chars=100
    )
    pack_md = next((tmp_path / "workspace" / "references").glob("*/PACK.md"))
    assert len(pack_md.read_text()) <= 100
    assert (pack_md.parent / "image.png").read_bytes() == b"pixels"


@pytest.mark.asyncio
async def test_async_materialization_uses_remote_protocol_and_exact_pinned_bytes(
    tmp_path: Path,
) -> None:
    packs = ReferencePackStore(tmp_path / "packs")
    source = {"docs/readme.md": b"pinned readme", "paper.pdf": b"%PDF-not-a-real-pdf"}
    pack = await packs.acreate(
        "owner", "Remote / Pack", "desc", list(source), reader=Reader(source)
    )
    snapshots = ReferencePackBindingStore(tmp_path / "snapshots")
    binding = snapshots.bind(
        "owner",
        "conv_remote",
        [ReferencePackSelection(pack.id, pack.current_version_id, pack.current.content_sha256)],
        packs=packs,
    )
    target = RemoteTarget()
    indexes = await amaterialize_reference_binding(binding, snapshots, target)
    assert indexes == materialized_pack_index_paths(binding)
    assert indexes[0].startswith("references/Remote-Pack")
    assert target.files[indexes[0]].startswith(b"# Reference Pack:")
    assert target.files["references/Remote-Pack/docs/readme.md"] == b"pinned readme"
    companion = next(path for path in target.files if ".disco-reference-text/" in path)
    assert target.files[companion].startswith(b"--- Page 1 ---")
    assert all(not path.startswith("/") for path in target.files)


@pytest.mark.asyncio
async def test_async_materialization_concurrent_targets_do_not_cross_leak(tmp_path: Path) -> None:
    packs = ReferencePackStore(tmp_path / "packs")
    first = await packs.acreate("owner-a", "Same", "", ["x.txt"], reader=Reader({"x.txt": b"A"}))
    second = await packs.acreate("owner-b", "Same", "", ["x.txt"], reader=Reader({"x.txt": b"B"}))
    first_snapshots = ReferencePackBindingStore(tmp_path / "snapshots-a")
    second_snapshots = ReferencePackBindingStore(tmp_path / "snapshots-b")
    first_binding = first_snapshots.bind(
        "owner-a",
        "conv",
        [ReferencePackSelection(first.id, first.current_version_id, first.current.content_sha256)],
        packs=packs,
    )
    second_binding = second_snapshots.bind(
        "owner-b",
        "conv",
        [
            ReferencePackSelection(
                second.id, second.current_version_id, second.current.content_sha256
            )
        ],
        packs=packs,
    )
    left, right = RemoteTarget(), RemoteTarget()
    await asyncio.gather(
        amaterialize_reference_binding(first_binding, first_snapshots, left),
        amaterialize_reference_binding(second_binding, second_snapshots, right),
    )
    left_payloads = [value for key, value in left.files.items() if key.endswith("x.txt")]
    right_payloads = [value for key, value in right.files.items() if key.endswith("x.txt")]
    assert left_payloads == [b"A"]
    assert right_payloads == [b"B"]


def test_pack_update_and_binding_admission_are_serialized(tmp_path: Path) -> None:
    packs = ReferencePackStore(tmp_path / "packs")
    original = _create(packs, "owner", {"note.txt": b"old"})
    snapshots = ReferencePackBindingStore(tmp_path / "snapshots")
    selection = ReferencePackSelection(
        original.id, original.current_version_id, original.current.content_sha256
    )

    async def race() -> tuple[object, object]:
        return await asyncio.gather(
            asyncio.to_thread(
                packs.update_files,
                "owner",
                original.id,
                uploads=[("note.txt", b"new", "text/plain")],
            ),
            asyncio.to_thread(
                snapshots.bind,
                "owner",
                "conv_race_update",
                [selection],
                packs=packs,
            ),
            return_exceptions=True,
        )

    update_result, bind_result = asyncio.run(race())
    assert not isinstance(update_result, Exception)
    if isinstance(bind_result, Exception):
        assert isinstance(bind_result, ReferencePackBindingConflict)
    else:
        assert bind_result.packs[0].version_id == original.current_version_id


def test_reference_inspect_is_pinned_and_truthful_without_vision(tmp_path: Path) -> None:
    packs = ReferencePackStore(tmp_path / "packs")
    pack = _create(packs, "owner", {"photo.png": b"not-text"})
    snapshots = ReferencePackBindingStore(tmp_path / "snapshots")
    binding = snapshots.bind(
        "owner",
        "conv_1",
        [ReferencePackSelection(pack.id, pack.current_version_id, pack.current.content_sha256)],
        packs=packs,
    )
    result = asyncio.run(
        reference_inspect(binding, snapshots=snapshots, pack_id=pack.id, file_name="photo.png")
    )
    assert result["status"] == "asset_only"
    assert result["content"] == "Asset only"
    with pytest.raises(ReferencePackError):
        asyncio.run(
            reference_inspect(
                binding, snapshots=snapshots, pack_id=pack.id, file_name="missing.txt"
            )
        )


def test_visual_observer_has_one_explicit_signature(tmp_path: Path) -> None:
    packs = ReferencePackStore(tmp_path / "packs")
    pack = _create(packs, "owner", {"photo.png": b"pixels"})
    snapshots = ReferencePackBindingStore(tmp_path / "snapshots")
    binding = snapshots.bind(
        "owner",
        "conv_1",
        [ReferencePackSelection(pack.id, pack.current_version_id, pack.current.content_sha256)],
        packs=packs,
    )
    calls: list[tuple[bytes, str, str]] = []

    async def observer(data: bytes, file_name: str, question: str) -> object:
        calls.append((data, file_name, question))
        return {"description": "a photo"}

    result = asyncio.run(
        reference_inspect(
            binding,
            snapshots=snapshots,
            pack_id=pack.id,
            file_name="photo.png",
            question="what is shown?",
            visual_observer=observer,
        )
    )
    assert result["status"] == "visual"
    assert calls == [(b"pixels", "photo.png", "what is shown?")]


def test_runtime_visual_factory_uses_the_bound_inspection_route(tmp_path: Path) -> None:
    packs = ReferencePackStore(tmp_path / "packs")
    pack = _create(packs, "owner", {"photo.png": b"pixels"})
    snapshots = ReferencePackBindingStore(tmp_path / "snapshots")
    snapshots.bind(
        "owner",
        "conv_1",
        [ReferencePackSelection(pack.id, pack.current_version_id, pack.current.content_sha256)],
        packs=packs,
    )
    calls: list[tuple[bytes, str, str]] = []

    async def observer(data: bytes, file_name: str, question: str) -> object:
        calls.append((data, file_name, question))
        return {"description": "a photo"}

    from disco.agent_server.reference_pack_runtime import ReferencePackRuntime

    runtime = ReferencePackRuntime(
        store=packs,
        bindings=snapshots,
        visual_observer_factory=lambda conversation_id: observer
        if conversation_id == "conv_1"
        else None,
    )
    result = asyncio.run(
        runtime.reference_inspect_callback()(
            owner_id="owner",
            conversation_id="conv_1",
            pack_id=pack.id,
            file_name="photo.png",
            question="what is shown?",
        )
    )
    assert result["status"] == "visual"
    assert calls == [(b"pixels", "photo.png", "what is shown?")]


def test_pdf_companion_is_page_marked(tmp_path: Path) -> None:
    pdf = b"%PDF-1.4\nBT (Page one text) Tj ET\n%%EOF"
    text = _pdf_text_with_page_markers(pdf)
    assert text.startswith("--- Page 1 ---")
    assert "Page one text" in text


def test_reference_pack_routes_authorize_binding_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packs = ReferencePackStore(tmp_path / "packs")
    snapshots = ReferencePackBindingStore(tmp_path / "snapshots")
    pack = _create(packs, "owner", {"note.txt": b"hello"})
    binding = snapshots.bind(
        "owner",
        "conv_good",
        [ReferencePackSelection(pack.id, pack.current_version_id, pack.current.content_sha256)],
        packs=packs,
    )
    monkeypatch.setattr(reference_pack_routes, "current_owner_id", lambda _request: "owner")
    app = FastAPI()
    app.include_router(
        make_reference_packs_router(
            packs,
            snapshots,
            conversation_authorizer=lambda _request, cid: cid == "conv_good",
        )
    )
    with TestClient(app) as client:
        response = client.get("/api/conversations/conv_good/reference-packs/bind")
        assert response.status_code == 200
        assert response.json()["binding_id"] == binding.id
        forbidden = client.get("/api/conversations/conv_bad/reference-packs/bind")
        assert forbidden.status_code == 403


def test_reference_pack_file_route_accepts_one_atomic_multipart_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packs = ReferencePackStore(tmp_path / "packs")
    original = _create(packs, "owner", {"note.txt": b"old", "remove.md": b"gone"})
    monkeypatch.setattr(reference_pack_routes, "current_owner_id", lambda _request: "owner")
    app = FastAPI()
    app.include_router(make_reference_packs_router(packs))
    with TestClient(app) as client:
        response = client.put(
            f"/api/reference-packs/{original.id}/files",
            files=[
                ("files", ("note.txt", b"new", "text/plain")),
                ("files", ("added.md", b"added", "text/markdown")),
                ("remove", (None, "remove.md")),
            ],
        )
        assert response.status_code == 200, response.text
        current = packs.get("owner", original.id)
        assert {item.name for item in current.current.files} == {"added.md", "note.txt"}
        assert (
            packs.read_version_file("owner", original.id, current.current_version_id, "note.txt")
            == b"new"
        )


def test_reference_pack_file_route_bounds_upload_before_mutating_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packs = ReferencePackStore(
        tmp_path / "packs",
        limits=ReferencePackLimits(max_file_bytes=3, max_total_bytes=4),
    )
    original = _create(packs, "owner", {"note.txt": b"old"})
    monkeypatch.setattr(reference_pack_routes, "current_owner_id", lambda _request: "owner")
    app = FastAPI()
    app.include_router(make_reference_packs_router(packs))

    with TestClient(app) as client:
        response = client.put(
            f"/api/reference-packs/{original.id}/files",
            files=[("files", ("too-large.txt", b"four", "text/plain"))],
        )

    assert response.status_code == 422
    current = packs.get("owner", original.id)
    assert current.current_version_id == original.current_version_id
    assert [item.name for item in current.current.files] == ["note.txt"]
