"""CXT-2 tests: ArtifactMemoryStore — durable files, safe defaults, structured
recovery, and faithful ledger reconstruction."""

from __future__ import annotations

import pytest
from disco.core.context import (
    ArtifactMemoryKind,
    ContextRecoveryError,
    DirectEditKind,
    DirectEditRef,
    ResourceRef,
    Severity,
    SourcePriority,
    VerifierFailureRef,
)
from disco.core.context.store import (
    _JSON_KINDS,
    _MD_KINDS,
    _SINGLETON_KINDS,
    ArtifactMemoryStore,
)


class MemFS:
    """In-memory WorkspaceFS: dict-backed; read of a missing path raises."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def read_file(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data


def _store() -> tuple[ArtifactMemoryStore, MemFS]:
    fs = MemFS()
    return ArtifactMemoryStore(fs), fs


# --- matrix guard -------------------------------------------------------------
def test_durable_kind_matrix_is_eleven_singletons() -> None:
    assert _MD_KINDS | _JSON_KINDS == _SINGLETON_KINDS
    assert len(_SINGLETON_KINDS) == 11  # [REL-2a] +artifact_manifest +design_direction
    assert not (_MD_KINDS & _JSON_KINDS)  # disjoint
    # SUMMARY is intentionally excluded (multi-instance, on-demand)
    assert ArtifactMemoryKind.SUMMARY not in _SINGLETON_KINDS


# --- durable writes create real files -----------------------------------------
@pytest.mark.asyncio
async def test_writes_create_files_with_correct_paths() -> None:
    store, fs = _store()
    await store.write_goal("build a roofing lead site")
    await store.seed_todo("- [ ] hero\n- [ ] form")
    await store.record_resources((ResourceRef(rel_path="a.png", source="upload://a"),))
    assert ".disco/context/current_goal.md" in fs.files
    assert ".disco/context/todo.md" in fs.files
    assert ".disco/context/resource_manifest.json" in fs.files


@pytest.mark.asyncio
async def test_ensure_initialized_creates_all_eleven() -> None:
    store, fs = _store()
    await store.ensure_initialized()
    paths = {store.path_for(k) for k in _SINGLETON_KINDS}
    assert paths <= set(fs.files)
    assert len(paths) == 11  # [REL-2a] +artifact_manifest +design_direction


@pytest.mark.asyncio
async def test_ensure_initialized_does_not_clobber_existing() -> None:
    store, _ = _store()
    await store.write_goal("keep me")
    await store.ensure_initialized()
    assert await store.read_goal() == "keep me"


# --- missing → safe default ---------------------------------------------------
@pytest.mark.asyncio
async def test_missing_files_return_safe_defaults() -> None:
    store, _ = _store()
    assert await store.read_goal() is None
    assert await store.read_todo() is None
    assert await store.read_design_direction() is None
    assert await store.read_resources() == ()
    assert await store.read_direct_edits() == ()
    assert await store.read_verifier_failures() == ()
    assert await store.read_comments() == ()
    assert await store.read_source_priority() == SourcePriority.default()


# --- present but corrupt → structured recovery error --------------------------
@pytest.mark.asyncio
async def test_corrupt_json_raises_structured_recovery_error() -> None:
    store, fs = _store()
    fs.files[store.path_for(ArtifactMemoryKind.DIRECT_EDITS)] = b"{not valid json"
    with pytest.raises(ContextRecoveryError) as ei:
        await store.read_direct_edits()
    assert ei.value.kind is ArtifactMemoryKind.DIRECT_EDITS
    assert ei.value.rel_path.endswith("direct_edits.json")
    assert ei.value.detail


@pytest.mark.asyncio
async def test_schema_mismatch_json_raises_recovery_error() -> None:
    store, fs = _store()
    # valid JSON, wrong shape (failures need kind+message)
    fs.files[store.path_for(ArtifactMemoryKind.VERIFIER_FAILURES)] = b'[{"nope": 1}]'
    with pytest.raises(ContextRecoveryError):
        await store.read_verifier_failures()


# --- full round-trip across all structured kinds ------------------------------
@pytest.mark.asyncio
async def test_full_roundtrip_reconstruct() -> None:
    store, _ = _store()
    await store.write_goal("goal text")
    await store.seed_todo("- [ ] do it")
    await store.write_markdown(ArtifactMemoryKind.DECISIONS, "chose static site")
    await store.write_markdown(ArtifactMemoryKind.ASSUMPTIONS, "no backend needed")
    await store.write_design_direction("## Design Direction: Dark Glass")
    failures = (
        VerifierFailureRef(kind="console_error", message="boom", severity=Severity.BLOCKER),
    )
    resources = (ResourceRef(rel_path="logo.svg", source="gh://o/r/logo.svg", license="MIT"),)
    edits = (DirectEditRef(target_id="hero", rel_path="index.html", kind=DirectEditKind.TEXT),)
    comments = ("needs real testimonials",)
    await store.record_verifier_failures(failures)
    await store.record_resources(resources)
    await store.record_direct_edits(edits)
    await store.record_comments(comments)
    await store.write_source_priority(SourcePriority.default())

    res = await store.reconstruct("conv_1", "/ws/conv_1")
    led = res.ledger
    assert res.recovery_errors == ()
    assert led.conversation_id == "conv_1"
    assert led.workspace_root == "/ws/conv_1"
    assert led.active_goal == "goal text"
    assert led.todo_ref is not None and led.todo_ref.rel_path.endswith("todo.md")
    assert await store.read_design_direction() == "## Design Direction: Dark Glass"
    assert led.latest_verifier_failures == failures
    assert led.resource_manifest == resources
    assert led.direct_edits == edits
    assert led.unresolved_comments == comments
    # decisions/assumptions reconstruct as recoverable refs
    retained_kinds = {r.kind for r in led.retained_refs}
    assert ArtifactMemoryKind.DECISIONS in retained_kinds
    assert ArtifactMemoryKind.ASSUMPTIONS in retained_kinds
    # source_priority round-trips independently of the ledger
    assert await store.read_source_priority() == SourcePriority.default()


# --- resilient reconstruct: one corrupt file ----------------------------------
@pytest.mark.asyncio
async def test_reconstruct_resilient_to_one_corrupt_file() -> None:
    store, fs = _store()
    await store.write_goal("g")
    await store.record_resources((ResourceRef(rel_path="a.png", source="upload://a"),))
    # corrupt ONLY direct_edits
    fs.files[store.path_for(ArtifactMemoryKind.DIRECT_EDITS)] = b"]]garbage"

    res = await store.reconstruct("c")
    # corrupt kind → safe default + exactly one note naming it
    assert res.ledger.direct_edits == ()
    assert len(res.recovery_errors) == 1
    assert res.recovery_errors[0].kind is ArtifactMemoryKind.DIRECT_EDITS
    # other kinds intact
    assert res.ledger.active_goal == "g"
    assert len(res.ledger.resource_manifest) == 1


@pytest.mark.asyncio
async def test_reconstruct_missing_files_no_spurious_notes() -> None:
    store, _ = _store()
    res = await store.reconstruct("c")
    # nothing written → all defaults, NO recovery notes (absent != corrupt)
    assert res.recovery_errors == ()
    assert res.ledger.active_goal is None
    assert res.ledger.direct_edits == ()


# --- [REL-2a] per-artifact runtime manifest --------------------------------------
@pytest.mark.asyncio
async def test_artifact_manifest_roundtrip_and_lazy_default() -> None:
    from disco.core.context.ledger import ArtifactRecord

    store, fs = _store()
    # missing → safe empty default
    assert await store.read_artifacts() == ()
    await store.ensure_initialized()
    assert ".disco/context/artifact_manifest.json" in fs.files  # singleton, like resource_manifest
    assert await store.read_artifacts() == ()  # initialized-empty still reads as ()

    recs = (
        ArtifactRecord(path="index.html", kind="app", sha256="abc", shown=True),
        ArtifactRecord(
            path="report.pdf",
            kind="pdf",
            verified=True,
            verify_verdict="passed",
            export={"pdf": "2026-06-30T00:00:00Z"},
        ),
    )
    await store.record_artifacts(recs)
    assert ".disco/context/artifact_manifest.json" in fs.files
    back = await store.read_artifacts()
    assert back == recs  # frozen models round-trip identically
    assert back[0].shown is True and back[0].verified is False and back[0].verify_verdict is None
    assert back[1].verified is True and back[1].verify_verdict == "passed"
    assert back[1].export == {"pdf": "2026-06-30T00:00:00Z"}


@pytest.mark.asyncio
async def test_artifact_manifest_old_json_without_verification_fields_loads() -> None:
    store, fs = _store()
    fs.files[store.path_for(ArtifactMemoryKind.ARTIFACT_MANIFEST)] = (
        b'[{"path": "legacy/index.html", "kind": "app", "sha256": "abc", "shown": true}]'
    )

    back = await store.read_artifacts()
    assert len(back) == 1
    assert back[0].path == "legacy/index.html"
    assert back[0].verified is False
    assert back[0].verify_verdict is None


@pytest.mark.asyncio
async def test_artifact_manifest_upsert_replaces_whole_list() -> None:
    from disco.core.context.ledger import ArtifactRecord

    store, _ = _store()
    await store.record_artifacts((ArtifactRecord(path="a.html", shown=False),))
    # a read-modify-write upsert (what the per-cid-locked caller does): flip shown=True
    cur = list(await store.read_artifacts())
    cur[0] = cur[0].model_copy(update={"shown": True, "verified": True, "verify_verdict": "passed"})
    await store.record_artifacts(tuple(cur))
    back = await store.read_artifacts()
    assert len(back) == 1 and back[0].shown is True and back[0].verified is True
    assert back[0].verify_verdict == "passed"


# --- [REL-2a] upsert_artifact: per-cid-locked RMW, insert-or-replace, no lost update ----------
@pytest.mark.asyncio
async def test_upsert_artifact_insert_then_replace() -> None:
    from disco.core.context.ledger import ArtifactRecord

    store, _ = _store()
    await store.upsert_artifact(ArtifactRecord(path="index.html", kind="app", shown=False))
    await store.upsert_artifact(ArtifactRecord(path="styles.css", kind="files"))
    # replace index.html (same path) — must NOT duplicate
    await store.upsert_artifact(
        ArtifactRecord(
            path="index.html",
            kind="app",
            shown=True,
            verified=True,
            verify_verdict="passed",
        )
    )
    recs = await store.read_artifacts()
    assert sorted(r.path for r in recs) == ["index.html", "styles.css"]
    idx = next(r for r in recs if r.path == "index.html")
    assert idx.shown is True and idx.verified is True and idx.verify_verdict == "passed"


@pytest.mark.asyncio
async def test_upsert_artifact_concurrent_no_lost_update() -> None:
    import asyncio

    from disco.core.context.ledger import ArtifactRecord

    store, _ = _store()
    # 20 concurrent upserts of DISTINCT paths — without the per-cid RMW lock, the read-modify-write
    # would race and lose updates; with it, all 20 must survive.
    await asyncio.gather(
        *(store.upsert_artifact(ArtifactRecord(path=f"f{i}.html")) for i in range(20))
    )
    recs = await store.read_artifacts()
    assert len(recs) == 20
    assert sorted(r.path for r in recs) == sorted(f"f{i}.html" for i in range(20))


# --- [REL-2a step2b] shadow flag + divergence comparator ----------------------
def test_manifest_shadow_flag_default_off(monkeypatch) -> None:
    from disco.core.context.artifact_projection import manifest_shadow_enabled

    monkeypatch.delenv("DISCO_ARTIFACT_MANIFEST_SHADOW", raising=False)
    monkeypatch.delenv("PMX_ARTIFACT_MANIFEST_SHADOW", raising=False)
    assert manifest_shadow_enabled() is False
    monkeypatch.setenv("DISCO_ARTIFACT_MANIFEST_SHADOW", "1")
    assert manifest_shadow_enabled() is True
    monkeypatch.setenv("DISCO_ARTIFACT_MANIFEST_SHADOW", "off")
    assert manifest_shadow_enabled() is False


def test_manifest_path_divergence() -> None:
    from disco.core.context.artifact_projection import manifest_path_divergence

    # in sync → no divergence
    assert manifest_path_divergence({"a", "b"}, {"a", "b"}) == (set(), set())
    # manifest missing 'b', has stray 'c'
    missing, extra = manifest_path_divergence({"a", "b"}, {"a", "c"})
    assert missing == {"b"} and extra == {"c"}
