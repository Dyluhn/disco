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
def test_durable_kind_matrix_is_nine_singletons() -> None:
    assert _MD_KINDS | _JSON_KINDS == _SINGLETON_KINDS
    assert len(_SINGLETON_KINDS) == 9
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
async def test_ensure_initialized_creates_all_nine() -> None:
    store, fs = _store()
    await store.ensure_initialized()
    paths = {store.path_for(k) for k in _SINGLETON_KINDS}
    assert paths <= set(fs.files)
    assert len(paths) == 9


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
    failures = (VerifierFailureRef(kind="console_error", message="boom", severity=Severity.BLOCKER),)
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
