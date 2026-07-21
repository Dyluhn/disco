"""WO-C6 red matrix — atomic overlays + collision-honest API/download.

Plan §10 (WO-C6): the generated self-host overlay is produced in memory, validated,
and collision-checked ONCE — there is no state in which only a subset is releasable
(§10.1). A workspace file that collides with ANY generated overlay path forces the
WHOLE assessment to fail closed: ``needs_review`` / ``self_host:false`` /
``spec_digest:null`` with a typed ``overlay_path_conflict`` blocker naming the EXACT
colliding path (§10.2), and a bound download for that project returns a typed 409 with
no zip body bytes while the plain source download still works (§10.3). The ``.env``-
omitting ``.dockerignore`` hazard can never coexist with a self-host-ready partial
bundle (§10.5); an uncollided candidate ships EXACTLY the frozen overlay set (§10.6);
the zip writer never emits colliding/traversal/symlink archive entries (§10.7);
assessment and the bound download agree on the collision (§10.8); a bound release
failure never degrades into a successful plain zip via a broad ``except`` (§10.9); and
existing not-web / needs-review UNBOUND source downloads stay behavior-compatible
(§10.10).

Locked semantics (plan §2): #1 no partial security overlay ships (#7) — the generated
overlay is atomic, all-or-none; #3 ``self_host == true`` means a COMPLETE, internally
consistent overlay — any blocker or overlay collision forces ``self_host == false``
and no partial overlay; #6 runtime/collision uncertainty fails closed to
``needs_review`` with a typed repair instruction.

Boundary (plan §1.2): every criterion drives the REAL FastAPI ``/release`` +
``/download`` routes through the ASGI app, a real ``ProjectStore`` on a real on-disk
workspace, a real committed version cut, and real zip bytes. No release / assessment /
overlay / zip-writer function is mocked or fake-replaced; the ONLY ``monkeypatch`` is
``ConfigStore.load`` (the config seam the settings PUT performs). §10.7's writer-
hardening cases additionally exercise the REAL ``_zip_workspace_with_overlay`` writer
directly with adversarial archive names (a supplemental pure-unit test over the real
code under test, per WO-C0 acceptance crit 3) — the writer is called, never replaced.

RED vs GREEN on baseline ``a8e3e710`` (see each test):
  * RED — a single overlay collision leaves ``assess_release`` on the ``candidate``
    path: it keeps ``self_host:true`` and a non-null ``spec_digest``, DROPS only the
    colliding overlay entry (shipping the OTHER five — a partial bundle), and emits the
    coarse ``overlay_suppressed_by_workspace_file`` blocker instead of the fail-closed
    ``overlay_path_conflict``. ``/download`` has no bound (source-locked) form — it
    ignores the query binding and returns 200, and a broad ``except Exception`` in the
    download handler falls back to a plain zip when assessment fails. The zip writer
    dedupes only EXACT collisions, so case-fold / slash-normalization / traversal
    archive names leak.
  * GREEN (preservation) — a clean uncollided candidate still ships exactly the six
    frozen overlay paths; the plain (unbound) source download of a collided candidate
    still retains the workspace file; the writer still dedupes exact collisions and
    skips symlinks; not-web / needs-review UNBOUND downloads stay plain-zip.

Randomized (plan §4 crit 8): every conversation id / title is drawn from the seeded
``closeout_name`` factory; the collision matrix is keyed on the overlay PATH, never on
a fixture name, so hard-coding an id/path cannot satisfy it.

Scope note — §10.4 (the UI renders every collision blocker and no self-host command /
bound bundle action) is a SEPARATE frontend tranche (``frontend/**``); it is NOT
authored here. This file asserts the API/download side only. The "AppKit entrypoint"
that plan §10.2 lists among the generated paths is, on this baseline, materialized
INSIDE the Dockerfile via a ``COPY <<HEREDOC`` (it is not a discrete workspace-
collidable overlay file); its integrity is therefore covered by the Dockerfile
collision case, and §10.6 pins the exact emitted set so a future discrete entrypoint
file cannot slip in un-collision-checked. See the limitations in the work-order report.
"""

from __future__ import annotations

import io
import os
import posixpath
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.agent_server.routes.projects import _zip_workspace_with_overlay
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, ProjectStorageSettings, RouterConfig
from disco.core.release.local_compose import (
    COMPOSE_PATH,
    DOCKERFILE_PATH,
    DOCKERIGNORE_PATH,
    ENV_EXAMPLE_PATH,
    RELEASE_JSON_PATH,
    SELFHOST_DOC_PATH,
)
from disco.tools.projects import ProjectStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.export_track1_closeout

# The frozen single-service self-host overlay path set (plan §10.2 / §10.6). Every
# candidate download must carry EXACTLY these generated files; a fail-closed collision
# ships NONE of them.
_FROZEN_OVERLAY = frozenset(
    {
        COMPOSE_PATH,
        DOCKERFILE_PATH,
        DOCKERIGNORE_PATH,
        ENV_EXAMPLE_PATH,
        SELFHOST_DOC_PATH,
        RELEASE_JSON_PATH,
    }
)

# Every generated overlay path, in a stable order, for the §10.2 parametrization.
_OVERLAY_PATHS: tuple[str, ...] = (
    COMPOSE_PATH,
    DOCKERFILE_PATH,
    DOCKERIGNORE_PATH,
    ENV_EXAMPLE_PATH,
    SELFHOST_DOC_PATH,
    RELEASE_JSON_PATH,
)

# ---- positive-candidate base workspaces (proven candidates on baseline) --------
#
# Both are exactly the shapes WO-C3's positive matrix proves are self-hostable
# candidates with a complete overlay on this baseline (Express/Node single-service,
# AppKit dev_server). A collision fixture = one of these PLUS a workspace file at a
# generated overlay path.

_NODE_BASE: dict[str, bytes] = {
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
    "package-lock.json": b'{"lockfileVersion":3,"name":"svc"}',
    "server.js": (
        b"const port=process.env.PORT;\n"
        b"require('http').createServer((_q,r)=>r.end('ok')).listen(port,'0.0.0.0');\n"
    ),
}
_APPKIT_BASE: dict[str, bytes] = {
    ".disco/appspec.json": b'{"name":"appkit-app","version":1}',
    "wrangler.toml": (
        b'name = "appkit-app"\n\n[[d1_databases]]\nbinding = "DB"\ndatabase_name = "appkit_prod"\n'
    ),
    "worker/index.ts": (
        b"export default {\n"
        b"  async fetch(_req: Request): Promise<Response> {\n"
        b"    return new Response('ok');\n"
        b"  },\n"
        b"};\n"
    ),
    "schema.sql": b"CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT);\n",
}

# A neutral marker for a workspace-owned file that shadows a generated overlay path —
# innocuous to the detector (no db url, no web-framework import, no manifest content).
_COLLISION_MARKER = b"# workspace-owned file that collides with a generated overlay path\n"


def _base_for(kind: str) -> dict[str, bytes]:
    return dict(_NODE_BASE if kind == "node" else _APPKIT_BASE)


# ---- harness (real ASGI app + real ProjectStore; only ConfigStore.load seamed) --


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


def _cid(make_name: object, prefix: str) -> str:
    """A seeded id in the canonical ``conv_`` namespace the release route requires;
    its tail varies from the recorded seed."""
    assert callable(make_name)
    return str(make_name(prefix))


def _seed(
    ps: ProjectStore,
    store: SqliteEventStore,
    cid: str,
    title: str,
    files: dict[str, bytes],
    *,
    owner_id: str = "local",
) -> None:
    """Seed a real on-disk workspace + manifest + conversation, then cut a real
    version so the live tree matches a committed ``VersionRecord`` (the C2 source
    binding is satisfied and collision handling is the only thing under test)."""
    workspace = ps.path_for(cid)
    workspace.mkdir(parents=True, exist_ok=True)
    total = 0
    for rel, data in files.items():
        dest = workspace / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        total += len(data)
    ps.write_manifest(
        cid,
        title=title,
        owner_id=owner_id,
        created_at="2026-06-06T00:00:00Z",
        file_count=len(files),
        total_bytes=total,
        imported=False,
    )
    store.create_conversation(cid, owner_id=owner_id, title=title, surface="build")
    cut = ps.cut_version(cid, trigger="closeout")
    assert cut is not None and cut.seq == 1, "precondition: a real version 1 was committed"


def _release(client: TestClient, cid: str) -> object:
    res = client.get(f"/api/projects/{cid}/release")
    assert res.status_code == 200, res.text
    return res.json()


def _bound_url(cid: str, version_seq: int | str, spec_digest_value: str) -> str:
    return f"/api/projects/{cid}/download?version_seq={version_seq}&spec_digest={spec_digest_value}"


def _blocker_codes(body: object) -> set[str]:
    assert isinstance(body, dict)
    blockers = body["blockers"]
    assert isinstance(blockers, list)
    return {str(b["code"]) for b in blockers}


def _has_conflict_for(body: object, path: str) -> bool:
    """Whether the response carries a typed ``overlay_path_conflict`` blocker naming
    the EXACT colliding overlay path (plan §10.2)."""
    assert isinstance(body, dict)
    blockers = body["blockers"]
    assert isinstance(blockers, list)
    return any(
        str(b.get("code")) == "overlay_path_conflict" and str(b.get("path")) == path
        for b in blockers
    )


def _zip_names(content: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return set(zf.namelist())


def _generated_overlay(content: bytes, source_paths: set[str]) -> set[str]:
    """The GENERATED overlay files in a download zip: every archive entry that is not
    one of the seeded workspace source files. A candidate ships the full frozen set; a
    collided (fail-closed) project must ship NONE (all-or-none, plan §10.1)."""
    return _zip_names(content) - source_paths


def _zip_read(content: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return zf.read(name)


# ===========================================================================
# §10.1 — atomic overlay: a single collision anywhere forces the WHOLE
# assessment to fail closed; there is never a partial ready bundle.
# ===========================================================================


def test_c6_single_collision_forces_whole_assessment_needs_review(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C6 §10.1 — RED on baseline. THE load-bearing atomicity test.

    A node candidate whose workspace already holds ONE file at a generated overlay
    path (``SELFHOST.md``) must fail closed as a WHOLE: ``needs_review`` /
    ``self_host:false`` / ``spec_digest:null``, and its ``/download`` must ship ZERO
    generated overlay files (all-or-none). Baseline stays on the ``candidate`` path:
    it keeps ``self_host:true`` + a non-null ``spec_digest`` and DROPS only the
    colliding entry, so the download still carries the other five generated overlay
    files — a partial, self-host-labelled bundle §2 #7 forbids."""
    cid = _cid(closeout_name, "conv_c6atomic")
    client, ps = _client(_store, tmp_path, monkeypatch)
    files = {**_NODE_BASE, SELFHOST_DOC_PATH: _COLLISION_MARKER}
    _seed(ps, _store, cid, _cid(closeout_name, "proj"), files)

    body = _release(client, cid)
    assert isinstance(body, dict)
    assert body["assessment"] == "needs_review", (
        f"a single overlay collision must fail the WHOLE assessment closed; baseline "
        f"stays {body['assessment']!r} (the candidate path)."
    )
    assert body["self_host"] is False, "a collided overlay must force self_host:false (§2 #3)."
    assert body["spec_digest"] is None, "a fail-closed collision must not bind a spec_digest."

    dl = client.get(f"/api/projects/{cid}/download")
    assert dl.status_code == 200, dl.text
    generated = _generated_overlay(dl.content, set(files))
    assert generated == set(), (
        "a collided project shipped a PARTIAL generated overlay "
        f"{sorted(generated)}; §10.1 requires all-or-none (zero generated files)."
    )


# ===========================================================================
# §10.2 — for a collision with EACH generated overlay path, the response is
# needs_review / self_host:false / spec_digest:null with a typed
# ``overlay_path_conflict`` blocker naming the EXACT colliding path.
# ===========================================================================


@pytest.mark.parametrize(
    "kind,overlay_path",
    [(kind, path) for kind in ("node", "appkit") for path in _OVERLAY_PATHS],
    ids=[f"{kind}-{path}" for kind in ("node", "appkit") for path in _OVERLAY_PATHS],
)
def test_c6_overlay_path_collision_matrix(
    kind: str,
    overlay_path: str,
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C6 §10.2 — RED on baseline, over EVERY generated overlay path.

    A positive candidate (node single-service OR AppKit dev_server) whose workspace
    holds a file at each generated overlay path (``compose.yaml``, the ``Dockerfile``,
    ``.dockerignore``, ``.env.example``, ``SELFHOST.md``, ``release.json``) must return
    ``needs_review`` / ``self_host:false`` / ``spec_digest:null`` with the typed
    ``overlay_path_conflict`` blocker naming the EXACT path.

    Baseline never emits ``overlay_path_conflict``: the AppKit collisions stay on the
    ``candidate`` path (``self_host:true`` + non-null ``spec_digest`` + the coarse
    ``overlay_suppressed_by_workspace_file`` code); the node ``compose.yaml`` /
    ``Dockerfile`` collisions instead trip the container-manifest detector and fail
    closed with the unrelated ``release_field_unresolved`` code and no exact path.
    Either way the ``overlay_path_conflict`` + exact-path contract is absent."""
    # A constant, pattern-valid id prefix (the route requires ``^conv_[A-Za-z0-9_-]+$``,
    # so a dotted overlay path like ``.env.example`` cannot seed the id); each case runs
    # in its own in-memory store + tmp_path, so the id never collides across parameters.
    cid = _cid(closeout_name, "conv_c6mtx")
    client, ps = _client(_store, tmp_path, monkeypatch)
    files = {**_base_for(kind), overlay_path: _COLLISION_MARKER}
    _seed(ps, _store, cid, _cid(closeout_name, "proj"), files)

    body = _release(client, cid)
    assert isinstance(body, dict)
    assert body["assessment"] == "needs_review", (
        f"[{kind}:{overlay_path}] an overlay collision must fail closed to needs_review; "
        f"baseline returns {body['assessment']!r}."
    )
    assert body["self_host"] is False, (
        f"[{kind}:{overlay_path}] a collision must force self_host:false."
    )
    assert body["spec_digest"] is None, (
        f"[{kind}:{overlay_path}] a collision must null the spec_digest."
    )
    assert _has_conflict_for(body, overlay_path), (
        f"[{kind}:{overlay_path}] expected a typed overlay_path_conflict blocker naming "
        f"the exact path {overlay_path!r}; saw codes {sorted(_blocker_codes(body))}."
    )


# ===========================================================================
# §10.3 — a collided BOUND download is a typed 409 with no zip body bytes; the
# plain source download still works and retains the original workspace file.
# ===========================================================================


def test_c6_collided_bound_download_returns_409_no_zip_bytes(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C6 §10.3 (bound) — RED on baseline.

    For a collided candidate, a BOUND self-host download
    (``?version_seq=&spec_digest=``) must return a typed 409 and emit NO zip body
    bytes. Baseline has no bound-download form: it ignores the query binding and
    streams a 200 ``application/zip`` (the workspace plus the partial overlay)."""
    cid = _cid(closeout_name, "conv_c6b409")
    client, ps = _client(_store, tmp_path, monkeypatch)
    files = {**_NODE_BASE, SELFHOST_DOC_PATH: _COLLISION_MARKER}
    _seed(ps, _store, cid, _cid(closeout_name, "proj"), files)

    body = _release(client, cid)
    assert isinstance(body, dict)
    seq = body["version_seq"]
    # Baseline reports a (dishonest) non-null spec_digest for the collided candidate;
    # a fixed server nulls it. Bind with whatever /release names so the URL is always
    # well-formed — the server must reject the bound download on the collision itself.
    digest = body["spec_digest"] if isinstance(body["spec_digest"], str) else "sha256:" + "0" * 64

    res = client.get(_bound_url(cid, seq, digest))
    assert res.status_code in (409, 410), (
        f"a bound download of a collided version returned {res.status_code}; it must be a "
        "typed 409. Baseline ignores the binding and returns 200 with the partial-overlay zip."
    )
    assert res.headers.get("content-type") != "application/zip", (
        "a rejected bound download emitted zip content-type; it must emit no bundle bytes."
    )
    assert not res.content.startswith(b"PK"), (
        "a rejected bound download emitted zip (PK) body bytes; §10.3 requires none."
    )


def test_c6_collided_plain_download_retains_workspace_file(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C6 §10.3 (plain) — GREEN preservation.

    The PLAIN (unbound) source download of a collided project still works (200) and
    retains the ORIGINAL workspace file byte-for-byte (the workspace always wins a
    path collision). Already correct on baseline; the fix must keep it green."""
    cid = _cid(closeout_name, "conv_c6plain")
    client, ps = _client(_store, tmp_path, monkeypatch)
    own = b"# MY OWN self-host notes -- keep me verbatim\n"
    files = {**_NODE_BASE, SELFHOST_DOC_PATH: own}
    _seed(ps, _store, cid, _cid(closeout_name, "proj"), files)

    res = client.get(f"/api/projects/{cid}/download")
    assert res.status_code == 200, res.text
    assert SELFHOST_DOC_PATH in _zip_names(res.content), "the workspace file must be present"
    assert _zip_read(res.content, SELFHOST_DOC_PATH) == own, (
        "the plain download did not retain the original workspace file bytes; the "
        "workspace file must win the collision, never be overwritten by the overlay."
    )


# ===========================================================================
# §10.5 — a workspace .dockerignore that omits .env can never coexist with a
# self-host-ready partial bundle (fail closed).
# ===========================================================================


def test_c6_dockerignore_omitting_env_never_ships_selfhost_ready(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C6 §10.5 — RED on baseline.

    A candidate whose workspace ships its OWN ``.dockerignore`` that does NOT exclude
    ``.env`` must fail closed — it can never be self-host-ready with a partial bundle
    whose only ``.dockerignore`` (the workspace one) would let a later ``.env`` leak
    into the image build context. Baseline keeps ``self_host:true`` and drops only the
    generated ``.dockerignore`` (keeping the workspace one that omits ``.env``),
    shipping the other five generated files — exactly the self-host-ready partial
    bundle §10.5 forbids."""
    cid = _cid(closeout_name, "conv_c6dockign")
    client, ps = _client(_store, tmp_path, monkeypatch)
    # A workspace .dockerignore with NO `.env` / `.env.*` entry (secrets would leak).
    files = {**_NODE_BASE, DOCKERIGNORE_PATH: b"node_modules\n*.log\n"}
    _seed(ps, _store, cid, _cid(closeout_name, "proj"), files)

    body = _release(client, cid)
    assert isinstance(body, dict)
    assert body["self_host"] is False, (
        "a workspace .dockerignore that omits .env must fail closed (self_host:false); "
        "baseline keeps self_host:true and ships a partial bundle with the .env-leaking "
        "workspace .dockerignore."
    )

    dl = client.get(f"/api/projects/{cid}/download")
    assert dl.status_code == 200, dl.text
    generated = _generated_overlay(dl.content, set(files))
    assert generated == set(), (
        "a .dockerignore collision shipped a self-host-ready partial bundle "
        f"{sorted(generated)}; §10.5 requires it to fail closed with no partial overlay."
    )


# ===========================================================================
# §10.6 — an uncollided candidate zip has EXACTLY the frozen overlay path set:
# no missing and no extra generated files.
# ===========================================================================


@pytest.mark.parametrize("kind", ["node", "appkit"])
def test_c6_uncollided_overlay_is_exactly_frozen_set(
    kind: str,
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C6 §10.6 — GREEN preservation.

    A CLEAN (uncollided) candidate's ``/download`` carries EXACTLY the six frozen
    generated overlay files — none missing, none extra. Already correct on baseline;
    the fix must keep it exact. This test also PINS the emitted set: were the fix to
    add a discrete generated file (e.g. a promoted AppKit entrypoint) it would trip
    here and flag that the §10.2 collision matrix needs the new path.

    (Plan §10.6 also requires the overlay to parse under Compose; that is the live
    Docker lane — WO-C8 — and is out of scope for this API-side red matrix.)"""
    cid = _cid(closeout_name, f"conv_c6ex{kind[:3]}")
    client, ps = _client(_store, tmp_path, monkeypatch)
    files = _base_for(kind)
    _seed(ps, _store, cid, _cid(closeout_name, "proj"), files)

    body = _release(client, cid)
    assert isinstance(body, dict)
    assert body["assessment"] == "candidate" and body["self_host"] is True, (
        f"[{kind}] precondition: a clean fixture must be a self-hostable candidate."
    )

    dl = client.get(f"/api/projects/{cid}/download")
    assert dl.status_code == 200, dl.text
    generated = _generated_overlay(dl.content, set(files))
    assert generated == set(_FROZEN_OVERLAY), (
        f"[{kind}] the uncollided overlay was not exactly the frozen set: missing "
        f"{sorted(_FROZEN_OVERLAY - generated)}, extra {sorted(generated - _FROZEN_OVERLAY)}."
    )


# ===========================================================================
# §10.7 — the zip writer rejects duplicate / case-fold / slash-normalization /
# symlink / traversal archive entries. A public-boundary case-fold test plus a
# supplemental unit test over the REAL writer with adversarial archive names.
# ===========================================================================


def test_c6_download_has_no_case_fold_duplicate_names(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C6 §10.7 (case-fold, public boundary) — RED on baseline.

    A node candidate whose workspace holds a lowercase ``selfhost.md`` must not yield a
    download that ALSO carries the generated ``SELFHOST.md`` — on Windows/macOS those
    two entries collide case-insensitively. Baseline's collision check is exact-match
    only, so it does not treat ``selfhost.md`` as shadowing ``SELFHOST.md``: the
    download carries BOTH, a case-fold duplicate. A fixed, case-fold-aware collision
    policy fails closed (no generated ``SELFHOST.md`` overlay), leaving no duplicate."""
    cid = _cid(closeout_name, "conv_c6case")
    client, ps = _client(_store, tmp_path, monkeypatch)
    files = {**_NODE_BASE, "selfhost.md": _COLLISION_MARKER}
    _seed(ps, _store, cid, _cid(closeout_name, "proj"), files)

    dl = client.get(f"/api/projects/{cid}/download")
    assert dl.status_code == 200, dl.text
    names = sorted(_zip_names(dl.content))
    lowered = [n.lower() for n in names]
    assert len(lowered) == len(set(lowered)), (
        "the download carries case-fold-duplicate archive names "
        f"{sorted(n for n in names if lowered.count(n.lower()) > 1)} — they collide on a "
        "case-insensitive filesystem; the zip writer / collision policy must reject them."
    )


def _writer_output_or_reject(src: Path, overlay: dict[str, str]) -> tuple[bool, bytes]:
    """Run the REAL ``_zip_workspace_with_overlay`` writer over adversarial inputs and
    report ``(rejected, content)`` — ``rejected`` True if the writer raised (its
    intended hardening), else the produced zip bytes to inspect for unsafe entries."""
    try:
        content = b"".join(_zip_workspace_with_overlay(src, overlay))
    except (ValueError, RuntimeError, OSError):
        return True, b""
    return False, content


def _archive_is_safe(content: bytes) -> tuple[bool, str]:
    """Whether a produced zip carries no colliding or escaping archive entry: no exact,
    case-fold, or slash-normalization duplicate, and no traversal/absolute name."""
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = zf.namelist()
    if len(names) != len(set(names)):
        return False, "exact duplicate archive name"
    lowered = [n.lower() for n in names]
    if len(lowered) != len(set(lowered)):
        return False, "case-fold duplicate archive name"
    normed = [posixpath.normpath(n) for n in names]
    if len(normed) != len(set(normed)):
        return False, "slash-normalization duplicate archive name"
    for n in names:
        norm = posixpath.normpath(n)
        if norm == ".." or norm.startswith("../") or posixpath.isabs(norm):
            return False, f"traversal/absolute archive name {n!r}"
    return True, ""


@pytest.mark.parametrize("case", ["exact_dup", "case_fold", "slash_norm", "traversal", "symlink"])
def test_c6_zip_writer_rejects_unsafe_archive_names(case: str, tmp_path: Path) -> None:
    """WO-C6 §10.7 (writer hardening) — RED for ``case_fold`` / ``slash_norm`` /
    ``traversal``; GREEN preservation for ``exact_dup`` / ``symlink``.

    Drives the REAL ``_zip_workspace_with_overlay`` writer (never a fake) with an
    adversarial (workspace tree, overlay) pair. The writer must EITHER reject (raise)
    OR produce an archive with no colliding/escaping entry. Baseline dedupes only EXACT
    collisions and skips symlinks (so ``exact_dup`` / ``symlink`` are already safe), but
    emits both members of a case-fold / slash-normalization pair and writes a ``../``
    traversal name verbatim — producing an unsafe archive without raising."""
    src = tmp_path / "ws"
    src.mkdir()
    overlay: dict[str, str] = {}

    if case == "exact_dup":
        (src / "foo.txt").write_bytes(b"workspace wins\n")
        overlay = {"foo.txt": "overlay loses"}
    elif case == "case_fold":
        (src / "readme.md").write_bytes(b"workspace\n")
        overlay = {"README.md": "overlay"}
    elif case == "slash_norm":
        (src / "dir").mkdir()
        (src / "dir" / "f.txt").write_bytes(b"workspace\n")
        overlay = {"dir//f.txt": "overlay"}  # normalizes to dir/f.txt on extraction
    elif case == "traversal":
        (src / "a.txt").write_bytes(b"workspace\n")
        overlay = {"../evil.txt": "escapes the extraction root"}
    else:  # symlink
        (src / "real.txt").write_bytes(b"workspace\n")
        os.symlink(src / "real.txt", src / "link.txt")
        overlay = {"note.txt": "overlay"}

    rejected, content = _writer_output_or_reject(src, overlay)
    if not rejected:
        safe, reason = _archive_is_safe(content)
        assert safe, (
            f"[{case}] the zip writer neither rejected the input nor produced a safe "
            f"archive: {reason}. It must reject or exclude colliding/escaping entries."
        )


# ===========================================================================
# §10.8 — assessment and the bound download use the SAME collision result; the
# download does not silently reassess into a different (servable) action set.
# ===========================================================================


def test_c6_assessment_and_bound_download_agree_on_collision(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C6 §10.8 — RED on baseline.

    ONE collided candidate must yield ONE consistent collision verdict across both
    endpoints: ``/release`` reports ``overlay_path_conflict`` (the self-host action is
    withdrawn) AND the bound ``/download`` returns 409 with no servable bundle. The
    download must not silently reassess into a different action set (e.g. serve a
    plain/partial zip as though it were the bound bundle). Baseline diverges the other
    way: ``/release`` reports no ``overlay_path_conflict`` (candidate, self_host:true)
    and the bound download returns a 200 ``application/zip`` — a servable bundle for a
    project whose overlay conflicts."""
    cid = _cid(closeout_name, "conv_c6agree")
    client, ps = _client(_store, tmp_path, monkeypatch)
    files = {**_NODE_BASE, SELFHOST_DOC_PATH: _COLLISION_MARKER}
    _seed(ps, _store, cid, _cid(closeout_name, "proj"), files)

    body = _release(client, cid)
    assert isinstance(body, dict)
    seq = body["version_seq"]
    digest = body["spec_digest"] if isinstance(body["spec_digest"], str) else "sha256:" + "0" * 64
    res = client.get(_bound_url(cid, seq, digest))

    assessment_flags_conflict = _has_conflict_for(body, SELFHOST_DOC_PATH)
    download_refuses = res.status_code in (409, 410)
    assert assessment_flags_conflict and download_refuses, (
        "assessment and the bound download disagree on the collision: "
        f"/release overlay_path_conflict={assessment_flags_conflict}, bound download "
        f"status={res.status_code}. Both must reflect the same withdrawn-self-host verdict."
    )
    assert res.headers.get("content-type") != "application/zip", (
        "the bound download served a bundle for a collided project — a silent "
        "reassessment into a servable action set the assessment had withdrawn."
    )


# ===========================================================================
# §10.9 — no broad ``except Exception`` converts a bound release failure into a
# successful plain zip.
# ===========================================================================


def test_c6_bound_download_failure_is_typed_not_plain_zip_fallback(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C6 §10.9 — RED on baseline.

    When a BOUND self-host download's release assessment fails (here: a corrupt
    host-owned ``release-intent.json`` sidecar makes ``assess_project`` raise
    ``StorageError``), the response must be a typed error — NEVER a broad-``except``
    fallback to a successful plain zip (plan §1.3). Baseline's download handler wraps
    the assessment in ``except Exception: overlay_files = {}`` and then streams a 200
    ``application/zip`` plain workspace zip. The corrupt sidecar is proven to fault by
    ``/release`` returning HTTP 500 first."""
    cid = _cid(closeout_name, "conv_c6fail")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, _cid(closeout_name, "proj"), dict(_NODE_BASE))
    # Corrupt the host-owned intent sidecar (outside the workspace) with invalid JSON.
    ps.release_intent_for(cid).write_bytes(b"{ this is not valid json ")

    probe = client.get(f"/api/projects/{cid}/release")
    assert probe.status_code == 500, (
        f"precondition: a corrupt intent sidecar must fault /release; got {probe.status_code}."
    )

    res = client.get(_bound_url(cid, 1, "sha256:" + "0" * 64))
    assert not (res.status_code == 200 and res.headers.get("content-type") == "application/zip"), (
        "a bound self-host download whose assessment FAILED fell back to a successful "
        f"plain zip (status {res.status_code}); §10.9 forbids the broad-except plain-zip "
        "fallback — it must surface a typed error."
    )
    assert not res.content.startswith(b"PK"), (
        "a failed bound self-host download emitted zip (PK) body bytes instead of a typed error."
    )


# ===========================================================================
# §10.10 — existing not-web / needs-review UNBOUND source downloads remain
# behavior-compatible (plain workspace zip, no overlay).
# ===========================================================================

_NOT_WEB_FILES: dict[str, bytes] = {
    "notes.txt": b"just some project notes, no web entrypoint here\n",
    "data.csv": b"a,b\n1,2\n",
}
# A container manifest with no typed intent fails closed to needs_review on THIS
# baseline (rung 2), independent of the WO-C3 detection changes — a stable
# needs_review fixture for the unbound-download regression.
_NEEDS_REVIEW_FILES: dict[str, bytes] = {
    "Dockerfile": b'FROM alpine:3\nCMD ["true"]\n',
    "readme.md": b"opaque container project\n",
}


@pytest.mark.parametrize(
    "expected_assessment,files",
    [("not_web", _NOT_WEB_FILES), ("needs_review", _NEEDS_REVIEW_FILES)],
    ids=["not_web", "needs_review"],
)
def test_c6_unbound_source_download_behavior_compat(
    expected_assessment: str,
    files: dict[str, bytes],
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C6 §10.10 — GREEN preservation.

    A not-web workspace and a (container-manifest) needs-review workspace both serve
    their UNBOUND ``/download`` as a plain workspace zip: 200, every source file
    present, and NO generated overlay file (no false self-host affordance). Already
    correct on baseline; the collision-honesty fix must not regress it."""
    cid = _cid(closeout_name, f"conv_c6uc{expected_assessment[:3]}")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, _cid(closeout_name, "proj"), files)

    body = _release(client, cid)
    assert isinstance(body, dict)
    assert body["assessment"] == expected_assessment, (
        f"precondition: expected {expected_assessment!r}, got {body['assessment']!r}."
    )
    assert body["self_host"] is False

    dl = client.get(f"/api/projects/{cid}/download")
    assert dl.status_code == 200, dl.text
    names = _zip_names(dl.content)
    assert set(files).issubset(names), "every workspace source file must be present"
    assert _generated_overlay(dl.content, set(files)) == set(), (
        "a non-candidate unbound download must ship no generated overlay file."
    )
