"""WO-C6 closeout — directory-named-as-overlay-path collision honesty.

A LOW-severity but genuine breach of the §10.1 all-or-none overlay-collision promise:
a workspace that contains a *directory* named exactly like one of the six generated
overlay paths (e.g. a workspace file at ``compose.yaml/inner.txt`` makes ``compose.yaml``
a DIRECTORY, not a file) was NOT treated as a collision. On the pre-fix tree the real
endpoints returned a FALSE ``self_host:true`` candidate with a bound ``spec_digest`` and
ZERO blockers, and BOTH the plain and bound ``/download`` zips emitted an ambiguous
file-vs-directory pair — a generated ``compose.yaml`` FILE alongside the workspace
``compose.yaml/inner.txt`` entry — which no extractor can materialize intact.

Root cause: ``release._overlay_collisions`` compared each generated overlay path only
against the set of full workspace FILE paths (exact / case-fold / slash-normalized); it
never considered a workspace path for which the overlay path is a DIRECTORY PREFIX. The
zip writer's collision tracking likewise reserved only full written names, not ancestor
directory prefixes, so it wrote both members of the pair.

The fix makes the collision check general across ALL six overlay constants: an overlay
path ``p`` collides when any workspace file equals ``p`` OR lives under ``p + "/"``,
forcing the existing atomic gate (``needs_review`` / ``self_host:false`` /
``spec_digest:null`` + typed ``overlay_path_conflict`` naming ``p``). The zip writer
additionally reserves the directory prefixes of every written name.

RED-first on baseline ``31a017fb`` (before the fix):
  * the two dir-named-overlay cases FAIL — ``/release`` returns ``candidate`` /
    ``self_host:true`` / non-null ``spec_digest`` / no ``overlay_path_conflict``;
  * the plain (and bound) download carries the file-vs-directory duplicate.
GREEN after the fix, and the clean-candidate preservation guard stays green throughout.

Boundary + anti-bypass: every criterion drives the REAL FastAPI ``/release`` +
``/download`` routes through the ASGI app over a real ``ProjectStore`` on a real on-disk
workspace with a real committed version cut; the writer-hardening case drives the REAL
``_zip_workspace_with_overlay``. No release / assessment / overlay / zip-writer function
is mocked; the ONLY ``monkeypatch`` is ``ConfigStore.load`` (the settings-PUT config
seam). Ids / titles are drawn from the seeded ``closeout_name`` factory; the assertions
key on the overlay PATH, never on a fixture name, so hard-coding cannot satisfy them.
"""

from __future__ import annotations

import io
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

# The frozen single-service self-host overlay path set (plan §10.2 / §10.6). A
# fail-closed collision ships NONE of these generated files.
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

# The proven Express/Node single-service candidate shape from WO-C3's positive matrix
# (self-hostable with a complete overlay on this baseline). A dir-prefix collision
# fixture = this PLUS a workspace file UNDER a generated overlay path.
_NODE_BASE: dict[str, bytes] = {
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
    "package-lock.json": b'{"lockfileVersion":3,"name":"svc"}',
    "server.js": (
        b"const port=process.env.PORT;\n"
        b"require('http').createServer((_q,r)=>r.end('ok')).listen(port,'0.0.0.0');\n"
    ),
}

# A neutral inner file whose only role is to make its parent segment a DIRECTORY named
# exactly like a generated overlay path — innocuous to the detector.
_INNER_MARKER = b"# inner file that makes its parent dir shadow a generated overlay path\n"


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
    """A seeded id in the canonical ``conv_`` namespace the release route requires."""
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
    """Seed a real on-disk workspace + manifest + conversation, then cut a real version
    so the live tree matches a committed ``VersionRecord`` (the C2 source binding is
    satisfied and collision handling is the only thing under test)."""
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


def _release(client: TestClient, cid: str) -> dict[str, object]:
    res = client.get(f"/api/projects/{cid}/release")
    assert res.status_code == 200, res.text
    body = res.json()
    assert isinstance(body, dict)
    return body


def _bound_url(cid: str, version_seq: object, spec_digest_value: str) -> str:
    return f"/api/projects/{cid}/download?version_seq={version_seq}&spec_digest={spec_digest_value}"


def _has_conflict_for(body: dict[str, object], path: str) -> bool:
    """Whether the response carries a typed ``overlay_path_conflict`` blocker naming the
    EXACT colliding overlay path (plan §10.2)."""
    blockers = body["blockers"]
    assert isinstance(blockers, list)
    return any(
        str(b.get("code")) == "overlay_path_conflict" and str(b.get("path")) == path
        for b in blockers
    )


def _blocker_codes(body: dict[str, object]) -> set[str]:
    blockers = body["blockers"]
    assert isinstance(blockers, list)
    return {str(b["code"]) for b in blockers}


def _zip_names(content: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return set(zf.namelist())


def _generated_overlay(content: bytes, source_paths: set[str]) -> set[str]:
    """The GENERATED overlay files in a download zip: every archive entry that is not a
    seeded workspace source file. A collided (fail-closed) project must ship NONE."""
    return _zip_names(content) - source_paths


def _file_vs_dir_conflicts(names: set[str]) -> set[str]:
    """Archive names that are BOTH a file entry and a directory prefix of another entry —
    the un-extractable file-vs-directory pair (a ``compose.yaml`` FILE alongside a
    ``compose.yaml/inner.txt`` entry)."""
    conflicts: set[str] = set()
    for name in names:
        needle = name + "/"
        if any(other.startswith(needle) for other in names):
            conflicts.add(name)
    return conflicts


# ===========================================================================
# A directory named exactly like a generated overlay path is an overlay
# collision: /release fails closed with a typed overlay_path_conflict.
# ===========================================================================


@pytest.mark.parametrize(
    "overlay_path",
    [COMPOSE_PATH, DOCKERFILE_PATH],
    ids=["compose_yaml_dir", "dockerfile_dir"],
)
def test_c6_dir_named_as_overlay_path_is_collision(
    overlay_path: str,
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C6 (dir-prefix) — RED on baseline ``31a017fb``.

    A node candidate whose workspace contains a DIRECTORY named exactly like a generated
    overlay path (a file at ``<overlay_path>/inner.txt``, so ``<overlay_path>`` is a
    directory, not a file) must fail closed: ``needs_review`` / ``self_host:false`` /
    ``spec_digest:null`` with a typed ``overlay_path_conflict`` blocker naming the EXACT
    path. Baseline never treats a directory-prefix as a collision: the basename of
    ``compose.yaml/inner.txt`` is ``inner.txt`` (so the detector's container-manifest rung
    is not tripped and the node project stays a candidate), and ``_overlay_collisions``
    compares only full FILE paths — so it returns ``candidate`` / ``self_host:true`` /
    non-null ``spec_digest`` / ZERO blockers, a FALSE self-host affordance."""
    cid = _cid(closeout_name, "conv_c6dirp")
    client, ps = _client(_store, tmp_path, monkeypatch)
    files = {**_NODE_BASE, f"{overlay_path}/inner.txt": _INNER_MARKER}
    _seed(ps, _store, cid, _cid(closeout_name, "proj"), files)

    body = _release(client, cid)
    assert body["assessment"] == "needs_review", (
        f"a directory named {overlay_path!r} must fail the assessment closed to "
        f"needs_review; baseline returns {body['assessment']!r} (the candidate path)."
    )
    assert body["self_host"] is False, (
        f"a dir named {overlay_path!r} must force self_host:false; baseline keeps it true."
    )
    assert body["spec_digest"] is None, (
        f"a dir named {overlay_path!r} must null the spec_digest; baseline binds a false one."
    )
    assert _has_conflict_for(body, overlay_path), (
        f"expected a typed overlay_path_conflict blocker naming {overlay_path!r}; saw codes "
        f"{sorted(_blocker_codes(body))}."
    )


# ===========================================================================
# The collided project's plain AND bound downloads are atomic: zero generated
# overlay files and no file-vs-directory duplicate archive entry.
# ===========================================================================


def test_c6_dir_collision_downloads_are_atomic(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C6 (dir-prefix) — RED on baseline for BOTH download forms.

    For a project whose workspace holds ``compose.yaml/inner.txt`` (``compose.yaml`` is a
    DIRECTORY): the PLAIN ``/download`` must ship ZERO generated overlay files and carry
    NO file-vs-directory duplicate (no generated ``compose.yaml`` FILE alongside the
    ``compose.yaml/inner.txt`` entry), and the BOUND ``/download`` must reject with a typed
    409/410 and emit NO zip bytes. Baseline plain download writes the generated
    ``compose.yaml`` FILE next to ``compose.yaml/inner.txt`` (the un-extractable pair), and
    the bound download streams a 200 ``application/zip`` carrying the same pair."""
    cid = _cid(closeout_name, "conv_c6dirdl")
    client, ps = _client(_store, tmp_path, monkeypatch)
    inner = f"{COMPOSE_PATH}/inner.txt"
    files = {**_NODE_BASE, inner: _INNER_MARKER}
    _seed(ps, _store, cid, _cid(closeout_name, "proj"), files)

    body = _release(client, cid)
    seq = body["version_seq"]
    # A fixed server nulls the digest for a collided project; bind with whatever /release
    # names so the URL is well-formed — the server must reject on the collision itself.
    digest = body["spec_digest"] if isinstance(body["spec_digest"], str) else "sha256:" + "0" * 64

    plain = client.get(f"/api/projects/{cid}/download")
    assert plain.status_code == 200, plain.text
    names = _zip_names(plain.content)
    assert inner in names, "the workspace directory entry must remain in the plain download"
    assert COMPOSE_PATH not in names, (
        f"the plain download wrote a generated {COMPOSE_PATH!r} FILE alongside {inner!r} — "
        "an un-extractable file-vs-directory pair; the collided overlay must be withheld."
    )
    assert _generated_overlay(plain.content, set(files)) == set(), (
        "a dir-prefix collision shipped a partial generated overlay; §10.1 requires none."
    )
    assert _file_vs_dir_conflicts(names) == set(), (
        "the plain download carries a file-vs-directory duplicate archive pair "
        f"{sorted(_file_vs_dir_conflicts(names))}; the writer must never emit one."
    )

    bound = client.get(_bound_url(cid, seq, digest))
    assert bound.status_code in (409, 410), (
        f"a bound download of a dir-collided version returned {bound.status_code}; it must "
        "be a typed 409/410. Baseline ignores the binding and returns 200 with the bad pair."
    )
    assert bound.headers.get("content-type") != "application/zip", (
        "a rejected bound download emitted zip content-type; it must emit no bundle bytes."
    )
    assert not bound.content.startswith(b"PK"), (
        "a rejected bound download emitted zip (PK) body bytes; a dir collision ships none."
    )


# ===========================================================================
# GREEN preservation: a clean candidate (no dir-prefix collision) still ships a
# self-hostable bundle whose bound download carries exactly the six generated files.
# ===========================================================================


def test_c6_clean_candidate_bound_download_ships_frozen_six(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C6 (dir-prefix) — GREEN preservation.

    A clean node candidate with NO dir-prefix collision still assesses ``candidate`` /
    ``self_host:true`` with a bound ``spec_digest``, and its BOUND ``/download`` ships
    EXACTLY the six frozen generated overlay files (none missing, none extra) with no
    file-vs-directory duplicate. The dir-prefix fix must not over-trigger on an ordinary
    candidate (e.g. it must not mistake a sibling file like ``server.js`` for a directory
    under a ``.js`` overlay path)."""
    cid = _cid(closeout_name, "conv_c6clean")
    client, ps = _client(_store, tmp_path, monkeypatch)
    files = dict(_NODE_BASE)
    _seed(ps, _store, cid, _cid(closeout_name, "proj"), files)

    body = _release(client, cid)
    assert body["assessment"] == "candidate" and body["self_host"] is True, (
        f"a clean candidate must stay self-hostable; got {body['assessment']!r} / "
        f"self_host={body['self_host']!r}."
    )
    seq = body["version_seq"]
    digest = body["spec_digest"]
    assert isinstance(digest, str) and digest, "a clean candidate must bind a real spec_digest"

    dl = client.get(_bound_url(cid, seq, digest))
    assert dl.status_code == 200, dl.text
    names = _zip_names(dl.content)
    generated = _generated_overlay(dl.content, set(files))
    assert generated == set(_FROZEN_OVERLAY), (
        "the clean bound download was not exactly the frozen six: missing "
        f"{sorted(_FROZEN_OVERLAY - generated)}, extra {sorted(generated - _FROZEN_OVERLAY)}."
    )
    assert _file_vs_dir_conflicts(names) == set(), (
        "a clean candidate download must have no file-vs-directory duplicate archive pair."
    )


# ===========================================================================
# Writer hardening (belt-and-suspenders, §10.7): the REAL _zip_workspace_with_overlay
# never emits a generated FILE where a workspace directory of the same name lives.
# ===========================================================================


def test_c6_zip_writer_reserves_directory_prefixes(tmp_path: Path) -> None:
    """WO-C6 (dir-prefix) — RED on baseline over the REAL writer.

    Drive ``_zip_workspace_with_overlay`` directly with a workspace tree that has a
    DIRECTORY named ``compose.yaml`` (a file at ``compose.yaml/inner.txt``) and an overlay
    that wants to write a ``compose.yaml`` FILE. The produced archive must NOT contain both
    (a file-vs-directory pair). Baseline reserves only full written names, not their
    ancestor directory prefixes, so it writes the generated ``compose.yaml`` FILE next to
    ``compose.yaml/inner.txt``; the hardened writer reserves the directory prefix and drops
    the colliding overlay file."""
    src = tmp_path / "ws"
    (src / COMPOSE_PATH).mkdir(parents=True)
    (src / COMPOSE_PATH / "inner.txt").write_bytes(_INNER_MARKER)
    (src / "keep.txt").write_bytes(b"workspace\n")

    content = b"".join(_zip_workspace_with_overlay(src, {COMPOSE_PATH: "generated compose file"}))
    names = _zip_names(content)
    assert f"{COMPOSE_PATH}/inner.txt" in names, "the workspace directory entry must survive"
    assert COMPOSE_PATH not in names, (
        f"the writer wrote a generated {COMPOSE_PATH!r} FILE where a workspace directory of "
        "the same name lives — an un-extractable file-vs-directory pair."
    )
    assert _file_vs_dir_conflicts(names) == set(), (
        "the writer produced a file-vs-directory duplicate archive pair "
        f"{sorted(_file_vs_dir_conflicts(names))}."
    )
