"""WO-C2 red test — immutable, real source binding on ``GET /.../release``.

Locked semantic §2 #4: "Every non-null ``(version_seq, tree_digest)`` names a real
immutable ``VersionRecord``. A hypothetical next sequence is forbidden." Plan §6
(WO-C2) acceptance 1+5: with a committed version present and DIVERGENT live bytes,
the route must fail closed (``needs_review`` / ``self_host:false`` / blocker
``source_not_snapshotted``) and must NOT invent the next sequence.

Boundary (plan §1.2): this drives the REAL FastAPI release router through the ASGI
app (``create_app`` + a real ``ConversationRuntime`` + a real ``ProjectStore`` on
a real on-disk workspace). No release function is mocked.

WHY IT IS RED ON BASELINE ``2ec1ceba``: ``routes/release._source_binding`` returns
``max(existing seq) + 1`` (a SPECULATIVE ``version_seq``) whenever the live tree
matches no committed version — so after committing v1 and then editing the live
mirror, the route returns ``version_seq == 2``, which names NO ``VersionRecord``,
paired with the divergent live ``tree_digest``. The assertion "every returned
``version_seq`` names a real saved version" therefore fails. The full WO-C2 §6.5
target state (``needs_review`` + blocker ``source_not_snapshotted``) is recorded in
the acceptance manifest metadata as this work order's intended failure code; it is
NOT encoded as an ``xfail`` here because the closeout lane forbids xfail/xpass
(plan §1.3) — a genuine ``failed`` is the honest red signal.

Randomized (plan §4 criterion 8): the conversation id is drawn from the seeded
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


def _seed(ps: ProjectStore, store: SqliteEventStore, cid: str, title: str) -> None:
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
        owner_id="local",
        created_at="2026-06-06T00:00:00Z",
        file_count=len(_NODE_FILES),
        total_bytes=total,
        imported=False,
    )
    ps.write_release_intent(cid, _NODE_INTENT)
    store.create_conversation(cid, owner_id="local", title=title, surface="build")


def test_release_never_returns_a_speculative_version_seq(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    make_name = closeout_name
    assert callable(make_name)
    # The release route guard requires the canonical `conv_` id namespace
    # (`^conv_[A-Za-z0-9_-]+$`), so the seeded id keeps that prefix while its tail
    # still varies from the seed.
    cid = make_name("conv_c2")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, title=make_name("proj"))

    # Commit the seeded tree as version 1, then DIVERGE the live mirror from it.
    v1 = ps.cut_version(cid, trigger="closeout")
    assert v1 is not None and v1.seq == 1
    (ps.path_for(cid) / "server.js").write_bytes(b"// diverged\n" + _NODE_FILES["server.js"])

    body = client.get(f"/api/projects/{cid}/release").json()
    saved = {v.seq: v.tree_digest for v in ps.list_versions(cid)}
    returned_seq = body["version_seq"]

    # PRIMARY (locked §2 #4): a returned version_seq must name a REAL VersionRecord.
    # On baseline this is `2` (max(1)+1), which is absent from `saved == {1: ...}`.
    assert returned_seq in saved, (
        f"speculative version_seq {returned_seq} names no VersionRecord "
        f"(saved seqs={sorted(saved)}); the route invented the next sequence for a "
        "tree that was never snapshotted (WO-C2 §6.5)."
    )
    # And the pair must describe ONE tree: the named record's digest == returned digest.
    assert saved[returned_seq] == body["tree_digest"], (
        "version_seq and tree_digest describe different trees — a mixed source binding."
    )
