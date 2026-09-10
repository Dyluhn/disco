"""WO-C2 red matrix (part 1) — immutable, real source binding on ``GET /.../release``.

Plan §6 (WO-C2) acceptance criteria 1, 2, 5, 6, 7, 10 (the assessment side). The
download-binding side (criteria 3, 4, 8, 9, 11) lives in
``test_c2_bound_download.py``.

Locked semantics (plan §2): #4 "every non-null ``(version_seq, tree_digest)`` names a
real immutable ``VersionRecord`` — a hypothetical next sequence is forbidden"; #5 "a
self-host download is source-bound"; #6 "runtime uncertainty fails closed."

Boundary (plan §1.2): these drive the REAL FastAPI release router through the ASGI app
(``create_app`` + a real ``ConversationRuntime`` + a real ``ProjectStore`` on a real
on-disk workspace with real committed version cuts). No release function is mocked;
the only ``monkeypatch`` is ``ConfigStore.load`` (a config seam, the same injection the
settings PUT performs), never the code under test.

RED vs GREEN on baseline ``2ec1ceba`` (see each test):
  * RED (behavior absent): speculative ``version_seq`` when the live tree diverges
    (crit 5), no-version-record fail-closed (crit 6), corrupt version-workspace
    integrity (crit 7).
  * GREEN (preservation): a clean candidate is fully source-bound (crit 1+2), the GET
    does not mutate storage (crit 6), assessment determinism (crit 10).

Randomized (plan §4 crit 8): the conversation id is drawn from the seeded
``closeout_name`` factory; the detector reads file CONTENTS only, never the id.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, ProjectStorageSettings, RouterConfig
from disco.core.release.spec import ReleaseIntent
from disco.tools.projects import ProjectStore
from disco.tools.projects.store import tree_digest as compute_tree_digest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.export_track1_closeout

# A conventional Node web app: a clean, recognizable `candidate` on baseline so the
# ONLY thing under test is the source-binding invariant, not detection ambiguity.
_NODE_FILES: dict[str, bytes] = {
    "server.js": b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n",
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
}
_NODE_INTENT = ReleaseIntent(
    start_cmd=("node", "server.js"),
    required_env=("API_BASE_URL", "SESSION_SECRET"),
)


@pytest.fixture
def _store() -> Iterator[SqliteEventStore]:
    yield SqliteEventStore(":memory:")


def _client(
    store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, ProjectStore]:
    cfg = RouterConfig.model_validate(
        {
            "models": {"m": {"model_id": "m", "provider": "fake", "context_window": 8192}},
            "default_model": "m",
        }
    )
    cfg = cfg.model_copy(update={"projects": ProjectStorageSettings(projects_root=str(tmp_path))})
    cfg_store = ConfigStore(path=Path("/dev/null"))
    monkeypatch.setattr(cfg_store, "load", lambda: cfg)
    runtime = ConversationRuntime(store, config=cfg, config_store=cfg_store)
    return TestClient(create_app(store, runtime=runtime)), ProjectStore(str(tmp_path))


def _seed(
    ps: ProjectStore,
    store: SqliteEventStore,
    cid: str,
    title: str,
    *,
    intent: ReleaseIntent | None = _NODE_INTENT,
    owner_id: str = "local",
) -> None:
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
        owner_id=owner_id,
        created_at="2026-06-06T00:00:00Z",
        file_count=len(_NODE_FILES),
        total_bytes=total,
        imported=False,
    )
    if intent is not None:
        ps.write_release_intent(cid, intent)
    store.create_conversation(cid, owner_id=owner_id, title=title, surface="build")


def _cid(make_name: object, prefix: str) -> str:
    """A seeded id that keeps the canonical ``conv_`` namespace the release route
    guard requires (``^conv_[A-Za-z0-9_-]+$``) while its tail varies from the seed."""
    assert callable(make_name)
    return str(make_name(prefix))


# ---- criterion 1 + 2: a clean candidate is fully source-bound (GREEN) ----------


def test_clean_candidate_names_a_real_version_that_rehashes(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C2 acc. 1+2 — GREEN preservation.

    With the live tree EQUAL to committed version 1, ``/release`` must return a
    ``version_seq`` that names a real ``VersionRecord`` whose digest equals the
    response ``tree_digest``, and rehashing that version's workspace must reproduce
    the same digest. Already correct on baseline (the live tree matches v1 exactly);
    the fix must keep it green."""
    cid = _cid(closeout_name, "conv_c2clean")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, title=_cid(closeout_name, "proj"))

    v1 = ps.cut_version(cid, trigger="closeout")
    assert v1 is not None and v1.seq == 1

    body = client.get(f"/api/projects/{cid}/release").json()
    assert body["assessment"] == "candidate" and body["self_host"] is True

    saved = {v.seq: v.tree_digest for v in ps.list_versions(cid)}
    seq = body["version_seq"]
    assert seq in saved, f"version_seq {seq} names no VersionRecord (saved={sorted(saved)})"
    assert saved[seq] == body["tree_digest"], "named version's digest != response tree_digest"

    rehashed = compute_tree_digest(ps.version_workspace_path(cid, seq))
    assert rehashed == body["tree_digest"], (
        "rehashing the bound version workspace did not reproduce the response tree_digest"
    )


# ---- criterion 5: never a speculative version_seq (RED) ------------------------


def test_release_never_returns_a_speculative_version_seq(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C2 acc. 5 — RED on baseline.

    With version 1 committed and DIVERGENT live bytes, ``routes/release._source_binding``
    returns ``max(seq)+1`` — a speculative ``version_seq`` (here ``2``) that names NO
    ``VersionRecord`` — paired with the divergent live digest. Locked §2 #4 forbids
    that; the target state is ``needs_review`` + blocker ``source_not_snapshotted`` +
    no speculative v2."""
    cid = _cid(closeout_name, "conv_c2spec")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, title=_cid(closeout_name, "proj"))

    v1 = ps.cut_version(cid, trigger="closeout")
    assert v1 is not None and v1.seq == 1
    (ps.path_for(cid) / "server.js").write_bytes(b"// diverged\n" + _NODE_FILES["server.js"])

    body = client.get(f"/api/projects/{cid}/release").json()
    saved = {v.seq: v.tree_digest for v in ps.list_versions(cid)}
    returned_seq = body["version_seq"]

    assert returned_seq in saved, (
        f"speculative version_seq {returned_seq} names no VersionRecord "
        f"(saved seqs={sorted(saved)}); the route invented the next sequence for a "
        "tree that was never snapshotted (WO-C2 §6.5)."
    )
    assert saved[returned_seq] == body["tree_digest"], (
        "version_seq and tree_digest describe different trees — a mixed source binding."
    )
    # The fail-closed target: divergent, unsnapshotted bytes must not self-host.
    blocker_codes = {b["code"] for b in body["blockers"]}
    assert body["assessment"] == "needs_review" and body["self_host"] is False, (
        "divergent live bytes with no matching version must fail closed to needs_review."
    )
    assert "source_not_snapshotted" in blocker_codes, (
        f"expected a source_not_snapshotted blocker, saw {sorted(blocker_codes)}."
    )


# ---- criterion 6: no version record → fail closed; GET does not mutate (RED) ----


def test_no_version_record_fails_closed_without_mutating_storage(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C2 acc. 6 — RED on baseline.

    With a workspace present but NO committed version, ``/release`` must fail closed
    (null source fields / ``source_not_snapshotted``) and must NOT mutate storage from
    a GET. Baseline's ``_source_binding`` returns ``(1, live_digest)`` — a
    ``version_seq`` that names no record. The GET-is-read-only half is already true on
    baseline; the speculative source field is the RED half."""
    cid = _cid(closeout_name, "conv_c2none")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, title=_cid(closeout_name, "proj"))

    before = ps.list_versions(cid)
    assert before == [], "precondition: no versions committed yet"

    body = client.get(f"/api/projects/{cid}/release").json()

    # (a) a GET must never cut/mutate a version (this holds on baseline).
    assert ps.list_versions(cid) == before, "GET /release mutated version storage"

    # (b) with no committed version, the source binding must be null or name a real
    #     record — never a fabricated sequence.
    saved = {v.seq for v in ps.list_versions(cid)}
    seq = body["version_seq"]
    assert seq is None or seq in saved, (
        f"version_seq {seq} was fabricated with NO version record on disk (saved={sorted(saved)}); "
        "an unsnapshotted workspace must not invent a sequence (WO-C2 §6.6)."
    )
    # (c) and it must fail closed rather than offer a self-host candidate.
    assert body["self_host"] is False, "an unsnapshotted workspace must not be self-hostable"


# ---- criterion 7: corrupt version workspace → integrity failure (RED) ----------


def test_corrupt_version_workspace_is_source_integrity_failure(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C2 acc. 7 — RED on baseline.

    Commit version 1, then TAMPER the stored version-1 workspace so its real contents
    no longer match its recorded ``tree_digest`` (the live mirror is left equal to v1's
    recorded digest, so baseline's digest match still 'succeeds'). Before use the
    version directory must be hashed and equal its record; a mismatch is a typed
    source-integrity blocker/error — never a clean candidate. Baseline never rehashes
    the stored version, so it returns a candidate bound to a corrupt source."""
    cid = _cid(closeout_name, "conv_c2corrupt")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, title=_cid(closeout_name, "proj"))

    v1 = ps.cut_version(cid, trigger="closeout")
    assert v1 is not None and v1.seq == 1

    # Tamper the STORED version workspace (a first cut copies files, never hardlinks,
    # so the live mirror is untouched and still matches v1's recorded digest).
    version_ws = ps.version_workspace_path(cid, 1)
    (version_ws / "server.js").write_bytes(b"// TAMPERED stored version bytes\n")
    assert compute_tree_digest(version_ws) != v1.tree_digest, "tamper precondition"

    res = client.get(f"/api/projects/{cid}/release")
    is_clean_candidate = res.status_code == 200 and res.json().get("assessment") == "candidate"
    assert not is_clean_candidate, (
        "a version workspace whose real contents no longer match its recorded digest "
        "was returned as a clean candidate; the source binding must verify the version "
        "hash and fail closed with a typed integrity blocker/error (WO-C2 §6.7)."
    )


# ---- criterion 10: assessment determinism (GREEN) ------------------------------


def test_two_assessments_are_deterministic(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C2 acc. 10 (assessment half) — GREEN preservation.

    Two unchanged assessments of the same committed version return byte-identical JSON
    (same source binding + spec digest). Already true on baseline; must stay green."""
    cid = _cid(closeout_name, "conv_c2det")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, title=_cid(closeout_name, "proj"))
    ps.cut_version(cid, trigger="closeout")

    first = client.get(f"/api/projects/{cid}/release")
    second = client.get(f"/api/projects/{cid}/release")
    assert first.status_code == 200 and second.status_code == 200
    assert first.json() == second.json(), "assessment is not deterministic"
    assert first.json()["spec_digest"] == second.json()["spec_digest"]
