"""WO-C2 red matrix (part 2) — bound, source-locked ``/download`` + non-blocking loop.

Plan §6 (WO-C2) acceptance criteria 3, 4, 8, 9, 11 — the DOWNLOAD-binding side. The
assessment side (1, 2, 5, 6, 7, 10) lives in ``test_c2_release_source_binding.py``.

Required behavior (plan §6): the self-host action downloads with an IMMUTABLE binding
(``/download?version_seq=N&spec_digest=D``, both enforced server-side); a bound download
never silently falls back to an unbound/plain zip and never streams the mutable live
mirror. The bound-download URL shape does not exist on baseline, so these tests are
written to the INTENDED contract and fail RED now.

Boundary (plan §1.2): these drive the REAL FastAPI ``/release`` + ``/download`` routes
through the ASGI app, a real ``ProjectStore``, real on-disk version cuts, and real zip
bytes. No release/download function is mocked; the only ``monkeypatch`` is
``ConfigStore.load`` (a config seam).

RED vs GREEN on baseline ``2ec1ceba``:
  * GREEN (preservation): a clean bound download's source digest + ``release.json`` agree
    with ``/release`` (crit 3+4, clean case); two bound downloads are byte-identical
    (crit 10 download half); another owner's sequence is forbidden (crit 9, owner scope).
  * RED (behavior absent): bound download stays wholly version N under a concurrent
    live-mutation + N+1 cut (crit 8); a wrong ``spec_digest`` / nonexistent seq / another
    conversation's seq is rejected (crit 9); the event loop stays responsive while a
    large fixture is downloaded (crit 11).

Randomized (plan §4 crit 8): conversation ids come from the seeded ``closeout_name``.
"""

from __future__ import annotations

import asyncio
import io
import os
import threading
import zipfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.agent_server.routes import release as release_routes
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, ProjectStorageSettings, RouterConfig
from disco.core.release.local_compose import RELEASE_JSON_PATH
from disco.core.release.spec import ReleaseIntent, load_release_spec, spec_digest
from disco.tools.projects import ProjectStore
from disco.tools.projects.store import tree_digest as compute_tree_digest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.export_track1_closeout

_NODE_FILES: dict[str, bytes] = {
    "server.js": b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n",
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
}
_NODE_INTENT = ReleaseIntent(
    start_cmd=("node", "server.js"),
    required_env=("API_BASE_URL", "SESSION_SECRET"),
)

# The self-host overlay path set (single-service). Stripped from a downloaded zip to
# recover the SOURCE tree so its digest can be compared to a VersionRecord digest.
_OVERLAY_NAMES = {
    "compose.yaml",
    "Dockerfile",
    ".dockerignore",
    ".env.example",
    "SELFHOST.md",
    RELEASE_JSON_PATH,
}


@pytest.fixture
def _store() -> Iterator[SqliteEventStore]:
    yield SqliteEventStore(":memory:")


def _app_and_store(
    store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[FastAPI, ProjectStore]:
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
    return create_app(store, runtime=runtime), ProjectStore(str(tmp_path))


def _client(
    store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, ProjectStore]:
    app, ps = _app_and_store(store, tmp_path, monkeypatch)
    return TestClient(app), ps


def _seed(
    ps: ProjectStore,
    store: SqliteEventStore,
    cid: str,
    title: str,
    *,
    files: dict[str, bytes] | None = None,
    owner_id: str = "local",
) -> None:
    payload = files if files is not None else _NODE_FILES
    workspace = ps.path_for(cid)
    workspace.mkdir(parents=True, exist_ok=True)
    total = 0
    for rel, data in payload.items():
        dest = workspace / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        total += len(data)
    ps.write_manifest(
        cid,
        title=title,
        owner_id=owner_id,
        created_at="2026-06-06T00:00:00Z",
        file_count=len(payload),
        total_bytes=total,
        imported=False,
    )
    ps.write_release_intent(cid, _NODE_INTENT)
    store.create_conversation(cid, owner_id=owner_id, title=title, surface="build")


def _cid(make_name: object, prefix: str) -> str:
    assert callable(make_name)
    return str(make_name(prefix))


def _bound_url(cid: str, version_seq: int | str, spec_digest_value: str) -> str:
    return f"/api/projects/{cid}/download?version_seq={version_seq}&spec_digest={spec_digest_value}"


def _source_digest_from_zip(content: bytes, tmp_root: Path) -> str:
    """Extract the zip's NON-overlay entries and hash them with the SAME algorithm
    the store uses for a ``VersionRecord.tree_digest``, so the bound download's source
    tree can be compared byte-for-byte-of-hash against a committed version."""
    out = tmp_root / f"extract-{os.urandom(6).hex()}"
    out.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for name in zf.namelist():
            if name in _OVERLAY_NAMES or name.startswith("selfhost/") or name.endswith("/"):
                continue
            dest = out / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(name))
    return compute_tree_digest(out)


# ---- criterion 3 + 4: clean bound download source + release.json agree (GREEN) --


def test_clean_bound_download_source_and_release_json_agree(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C2 acc. 3+4 — GREEN preservation (clean case).

    For a candidate bound to committed version 1, hashing the downloaded zip's
    non-overlay source entries reproduces the response ``tree_digest``, and the bundled
    ``release.json`` reports the same ``version_seq`` / ``tree_digest`` / ``spec_digest``
    as ``/release`` and the bound request. Written to the intended
    ``?version_seq=&spec_digest=`` contract; green on baseline because the live tree
    still equals v1 (binding enforcement itself is proven RED elsewhere)."""
    cid = _cid(closeout_name, "conv_c2bound")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, title=_cid(closeout_name, "proj"))
    ps.cut_version(cid, trigger="closeout")

    body = client.get(f"/api/projects/{cid}/release").json()
    assert body["assessment"] == "candidate" and body["self_host"] is True
    seq, digest, spec_d = body["version_seq"], body["tree_digest"], body["spec_digest"]

    res = client.get(_bound_url(cid, seq, spec_d))
    assert res.status_code == 200, res.text

    assert _source_digest_from_zip(res.content, tmp_path) == digest, (
        "the downloaded source tree does not hash to the bound version's tree_digest"
    )
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        spec = load_release_spec(zf.read(RELEASE_JSON_PATH))
    assert spec.version_seq == seq, "release.json version_seq disagrees with the response"
    assert spec.tree_digest == digest, "release.json tree_digest disagrees with the response"
    assert spec_digest(spec) == spec_d, "release.json does not hash to the response spec_digest"


# ---- criterion 9: bound download rejects a mismatched binding ------------------


@pytest.mark.parametrize("case", ["wrong_spec_digest", "nonexistent_seq", "other_conversation_seq"])
def test_bound_download_rejects_invalid_binding(
    case: str,
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C2 acc. 9 — RED on baseline.

    A wrong ``spec_digest``, a nonexistent sequence, or a sequence that belongs to a
    DIFFERENT conversation must fail with a documented 409/410 and emit no bytes.
    Baseline's ``/download`` ignores the query parameters entirely and streams the live
    workspace with HTTP 200."""
    cid = _cid(closeout_name, "conv_c2rej")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, title=_cid(closeout_name, "proj"))
    ps.cut_version(cid, trigger="closeout")
    body = client.get(f"/api/projects/{cid}/release").json()
    good_digest = body["spec_digest"]

    if case == "wrong_spec_digest":
        url = _bound_url(cid, 1, "sha256:" + "0" * 64)
    elif case == "nonexistent_seq":
        url = _bound_url(cid, 999, good_digest)
    else:  # other_conversation_seq: seq 3 exists only in a SIBLING conversation
        other = _cid(closeout_name, "conv_c2other")
        _seed(ps, _store, other, title=_cid(closeout_name, "projb"))
        for i in range(3):
            (ps.path_for(other) / "server.js").write_bytes(
                f"// rev {i}\n".encode() + _NODE_FILES["server.js"]
            )
            ps.cut_version(other, trigger="closeout")
        assert {v.seq for v in ps.list_versions(other)} >= {3}
        url = _bound_url(cid, 3, good_digest)  # this conversation has only seq 1

    res = client.get(url)
    assert res.status_code in (409, 410), (
        f"[{case}] a mismatched bound download returned {res.status_code}; it must be a "
        "documented 409/410. Baseline ignores the binding and returns 200 with the live "
        "workspace zip."
    )
    assert res.headers.get("content-type") != "application/zip", (
        f"[{case}] a rejected bound download emitted zip bytes; it must emit none."
    )


def test_bound_download_other_owner_sequence_is_forbidden(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C2 acc. 9 (other owner) — GREEN preservation.

    A bound download for a sequence in a conversation owned by ANOTHER owner is 403,
    enforced by the shared owner-scoped read preamble before any binding logic. Already
    correct on baseline; the fix must keep it green."""
    cid = _cid(closeout_name, "conv_c2intruder")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, title=_cid(closeout_name, "proj"), owner_id="intruder")
    ps.cut_version(cid, trigger="closeout")

    res = client.get(_bound_url(cid, 1, "sha256:" + "0" * 64))
    assert res.status_code == 403, res.text
    assert res.json()["detail"]["reason"] == "project_forbidden"


# ---- criterion 10 (download half): two bound downloads byte-identical (GREEN) ---


def test_two_bound_downloads_are_byte_identical(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C2 acc. 10 (download half) — GREEN preservation.

    Two bound downloads of the same unchanged version produce byte-identical zip bytes.
    Already deterministic on baseline; must stay green."""
    cid = _cid(closeout_name, "conv_c2twice")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, title=_cid(closeout_name, "proj"))
    ps.cut_version(cid, trigger="closeout")
    body = client.get(f"/api/projects/{cid}/release").json()
    url = _bound_url(cid, body["version_seq"], body["spec_digest"])

    first = client.get(url)
    second = client.get(url)
    assert first.status_code == 200 and second.status_code == 200
    assert first.content == second.content, "two bound downloads were not byte-identical"


# ---- criterion 8: bound download is wholly one version under a concurrent cut ----


def test_bound_download_is_wholly_one_version_under_concurrent_cut(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C2 acc. 8 — RED on baseline. THE load-bearing anti-bypass test.

    After a response bound to version N, a concurrent thread repeatedly edits the live
    mirror and cuts N+1 while the bound zip streams. Across >=50 iterations every
    completed zip must be WHOLLY version N (its source digest == N's tree_digest) or
    fail atomically with a typed 409/410 — never a mixed tree.

    Why it deterministically catches a mixed tree: version N (pinned so it is never
    pruned) is snapshotted BEFORE any divergence; the live mirror is then permanently
    diverged from N (a churn file that N does not contain) and a background thread keeps
    adding new files + cutting fresh versions throughout the download loop. The fixed
    server streams N's immutable version workspace, so every completed download hashes to
    N regardless of the churn. Baseline ignores the version binding and streams the
    live mirror, which ALWAYS contains at least one churn file absent from N — so its
    source digest can never equal N on any of the >=50 iterations, and the very first
    completed download trips the assertion. No timing luck is required: the divergence
    is established before the loop starts and the churner never removes it."""
    cid = _cid(closeout_name, "conv_c2race")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, title=_cid(closeout_name, "proj"))

    n = ps.cut_version(cid, trigger="closeout")
    assert n is not None and n.seq == 1
    ps.set_version_pinned(cid, 1, True)  # pin N so concurrent churn can never prune it
    n_digest = n.tree_digest

    body = client.get(f"/api/projects/{cid}/release").json()
    assert body["version_seq"] == 1 and body["tree_digest"] == n_digest
    url = _bound_url(cid, 1, body["spec_digest"])

    live = ps.path_for(cid)
    # Permanently diverge the live mirror from N BEFORE the loop (N has no churn file).
    (live / "churn.txt").write_bytes(b"diverged from N\n")

    stop = threading.Event()
    errors: list[str] = []

    def _churn() -> None:
        i = 0
        try:
            while not stop.is_set():
                # Add a NEW file each iteration (never rewrite one the reader is reading),
                # then cut a fresh version N+k. N stays pinned and immutable.
                (live / f"churn_{i}.txt").write_bytes(f"iteration {i}\n".encode())
                ps.cut_version(cid, trigger="race")
                i += 1
        except Exception as exc:  # noqa: BLE001 — surface a churner crash to the test
            errors.append(f"churner failed at iteration {i}: {exc!r}")

    churner = threading.Thread(target=_churn, daemon=True)
    churner.start()
    try:
        iterations = 50
        for i in range(iterations):
            res = client.get(url)
            if res.status_code == 200:
                got = _source_digest_from_zip(res.content, tmp_path)
                assert got == n_digest, (
                    f"iteration {i}: a bound download to version {n.seq} returned a tree "
                    f"whose source digest {got} != version {n.seq}'s digest {n_digest} — a "
                    "MIXED/live tree leaked. Baseline ignores the version binding and streams "
                    "the mutating live mirror instead of the immutable version workspace."
                )
            else:
                assert res.status_code in (409, 410), (
                    f"iteration {i}: bound download returned {res.status_code}; a source-bound "
                    "download may only succeed as N or fail atomically with a typed 409/410."
                )
    finally:
        stop.set()
        churner.join(timeout=10)
    assert not errors, "; ".join(errors)
    # Prove the concurrent cutter actually advanced past N (the race window was real).
    assert max(v.seq for v in ps.list_versions(cid)) > 1, "the concurrent cutter never cut N+1"


# ---- criterion 11: event loop stays responsive while assessing a large fixture --


@pytest.mark.asyncio
async def test_event_loop_stays_responsive_during_large_assessment(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C2 acc. 11 — RED on baseline.

    While a large bounded fixture is being assessed/hashed, concurrent ``/health``
    requests must keep completing. Baseline runs the whole read+hash synchronously in
    the async route handler, so it monopolizes the event loop and the health probes can
    only finish once the big request does; moving only metadata work off-loop (or
    nothing, as on baseline) fails this. A causal barrier in the real assessment call
    proves that the health requests complete while the assessment worker is occupied,
    without turning OS scheduling noise into a product verdict."""
    cid = _cid(closeout_name, "conv_c2loop")
    app, ps = _app_and_store(_store, tmp_path, monkeypatch)

    big_blob = os.urandom(64 * 1024 * 1024)  # incompressible: real read+hash work
    files = dict(_NODE_FILES)
    files["assets/big.bin"] = big_blob
    _seed(ps, _store, cid, title=_cid(closeout_name, "proj"), files=files)

    transport = httpx.ASGITransport(app=app, client=("testclient", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        loop = asyncio.get_running_loop()
        event_loop_thread = threading.get_ident()
        assessment_started = threading.Event()
        health_completed = threading.Event()
        assessment_threads: list[int] = []
        real_assess_project = release_routes.assess_project

        def _synchronized_assessment(*args: object, **kwargs: object) -> object:
            assessment_threads.append(threading.get_ident())
            assessment_started.set()
            if not health_completed.wait(timeout=5):
                raise RuntimeError("health probes could not run during release assessment")
            return real_assess_project(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(release_routes, "assess_project", _synchronized_assessment)

        async def _timed(url: str) -> tuple[int, float]:
            start = loop.time()
            resp = await client.get(url)
            return resp.status_code, loop.time() - start

        # Warm the store/runtime path so first-call setup cost is not measured.
        warm_status, _ = await _timed("/health")
        assert warm_status == 200

        big_task = asyncio.create_task(_timed(f"/api/projects/{cid}/release"))
        started = await asyncio.to_thread(assessment_started.wait, 5)
        assert started, "the release request never entered its assessment worker"
        try:
            health = await asyncio.gather(*[_timed("/health") for _ in range(4)])
            assert not big_task.done(), "the assessment did not remain causally concurrent"
        finally:
            health_completed.set()
        big_status, big_dt = await big_task

        assert big_status == 200, "the large assessment did not complete"
        assert assessment_threads and all(
            thread_id != event_loop_thread for thread_id in assessment_threads
        ), "the release assessment ran on the async event-loop thread"
        for status, _dt in health:
            assert status == 200, "a concurrent health probe failed"
        assert max(dt for _status, dt in health) < big_dt


# ---- criterion 8 (regression): a pruned bound version fails TYPED, never 500/torn --


def test_bound_download_of_pruned_version_fails_typed(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C2 acc. 8 (§4.7 harness addition) — the source-binding TOCTOU regression.

    ``_build_bound_download_zip`` used to read the committed version directory THREE
    separate times (integrity digest, release assessment, zip emit). A concurrent
    ``_prune`` deleting an UNPINNED bound version *between* those reads could escape as
    an untyped 500 (a ``StorageError``/``OSError`` outside the ``_BoundReject`` handler)
    or emit a torn/empty 200. The fix collapses the source to ONE in-memory read whose
    hash is the integrity guard, so a bound download for a version whose bytes are gone
    can only fail TYPED (409/410) and never emit a zip.

    Deterministic case: cut enough distinct UNPINNED versions (well past
    ``_MAX_UNLABELED``) that v1 is pruned outright, then assert the bound download is a
    typed failure carrying no zip bytes and is neither 200 nor 500.

    Honest scope: with v1 FULLY pruned the version index no longer lists seq 1, so the
    already-present ``version_workspace_path`` guard rejects it with a typed 410 even
    on the pre-fix code — this deterministic case is a green-at-tip PRESERVATION guard
    for the ``never 200/500`` contract. The mid-read window that was previously an
    untyped 500 (prune landing between the resolve and the integrity read) is closed by
    the single-read refactor by construction rather than by a timing-dependent test."""
    cid = _cid(closeout_name, "conv_c2prune")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, title=_cid(closeout_name, "proj"))

    n = ps.cut_version(cid, trigger="closeout")
    assert n is not None and n.seq == 1
    body = client.get(f"/api/projects/{cid}/release").json()
    assert body["version_seq"] == 1 and body["self_host"] is True
    spec_d = body["spec_digest"]

    live = ps.path_for(cid)
    # Deterministically prune v1: cut a generous count of distinct UNPINNED versions
    # (each adds a NEW file so the tree digest changes and a fresh version is cut),
    # well past _MAX_UNLABELED=20, so v1 is dropped from the store entirely.
    for i in range(40):
        (live / f"churn_{i}.txt").write_bytes(f"iteration {i}\n".encode())
        assert ps.cut_version(cid, trigger="prune") is not None
    assert 1 not in {v.seq for v in ps.list_versions(cid)}, (
        "v1 was not pruned; the deterministic prune precondition did not hold"
    )

    res = client.get(_bound_url(cid, 1, spec_d))
    assert res.status_code in (409, 410), (
        f"a bound download for a pruned version returned {res.status_code}; a source-bound "
        "download may only succeed as its version or fail atomically with a typed 409/410."
    )
    assert res.status_code not in (200, 500), (
        f"a pruned bound download returned {res.status_code}; it must be neither a (torn) "
        "200 nor an untyped 500."
    )
    assert res.headers.get("content-type") != "application/zip", (
        "a rejected bound download emitted zip bytes; it must emit none."
    )
    reason = res.json()["detail"]["reason"]
    assert reason in {"version_not_found", "source_integrity_failed"}, (
        f"a pruned bound download must carry a typed reason, got {reason!r}"
    )
    assert b"PK\x03\x04" not in res.content, "a rejected bound download body contains zip magic"
