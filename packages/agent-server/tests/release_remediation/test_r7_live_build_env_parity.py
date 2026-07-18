"""R7 live-lane defect D4 — typed-intent candidates need source build-env parity.

Reproduced live (2026-07-17 R7 diagnostic at c0c4f728): the frozen public-build-env
Vite case declares its build via a typed intent with NO env declarations while the
source reads ``import.meta.env.VITE_PUBLIC_BANNER`` (public) and
``import.meta.env.VITE_ADMIN_SECRET`` (secret-shaped). The intent path lowered only
DECLARED env, so the bundle was accepted as a candidate with NO ``ARG``/build-arg
lowering — violating the frozen §12.10 disjunction on both sides at once (the public
marker never reached the built asset AND the secret-shaped var never triggered the
rejection branch). The source-detection path already applies both rules
(conserve public decls for build-requiring services; fail closed on secret-shaped
discovered build vars).

These tests live OUTSIDE the frozen closeout dirs and drive the real
``/api/projects/{cid}/release`` + ``/download`` routes; only ``ConfigStore.load`` is
seamed. Assertions are on the actual downloaded overlay bytes.
"""

from __future__ import annotations

import io
import itertools
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, ProjectStorageSettings, RouterConfig
from disco.core.release.local_compose import COMPOSE_PATH, DOCKERFILE_PATH
from disco.core.release.spec import EnvScope, EnvVarDecl, ReleaseIntent
from disco.tools.projects import ProjectStore
from fastapi.testclient import TestClient

_ID_COUNTER = itertools.count()


def _uid(prefix: str) -> str:
    return f"{prefix}_{next(_ID_COUNTER)}"


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
    files: dict[str, bytes],
    *,
    intent: ReleaseIntent | None = None,
) -> None:
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
        title=cid,
        owner_id="local",
        created_at="2026-07-17T00:00:00Z",
        file_count=len(files),
        total_bytes=total,
        imported=False,
    )
    if intent is not None:
        ps.write_release_intent(cid, intent)
    store.create_conversation(cid, owner_id="local", title=cid, surface="build")
    cut = ps.cut_version(cid, trigger="r7d4")
    assert cut is not None and cut.seq == 1


def _release(client: TestClient, cid: str) -> dict[str, object]:
    res = client.get(f"/api/projects/{cid}/release")
    assert res.status_code == 200, res.text
    body = res.json()
    assert isinstance(body, dict)
    return body


def _overlay_texts(client: TestClient, cid: str) -> dict[str, str]:
    dl = client.get(f"/api/projects/{cid}/download")
    assert dl.status_code == 200, dl.text
    out: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        for name in zf.namelist():
            out[name] = zf.read(name).decode("utf-8", errors="replace")
    return out


def _vite_files(main_js: bytes) -> dict[str, bytes]:
    """A Vite tree DISTINCT from the frozen live fixture (different names/body), so the
    fix cannot be a fixture special-case."""
    return {
        "index.html": (
            b"<!doctype html><html><head><title>r7d4</title></head><body>"
            b"<div id='root'>r7d4</div>"
            b"<script type='module' src='/src/entry.js'></script>"
            b"</body></html>\n"
        ),
        "package.json": (
            b'{"name":"r7d4-spa","private":true,"version":"1.0.0",'
            b'"scripts":{"build":"vite build"},"devDependencies":{"vite":"^5.4.0"}}'
        ),
        "vite.config.js": b"export default { build: { outDir: 'dist' } };\n",
        "src/entry.js": main_js,
    }


_PUBLIC_ONLY = _vite_files(
    b"const banner = import.meta.env.VITE_SITE_BANNER;\n"
    b"document.getElementById('root').textContent = 'r7d4 ' + banner;\n"
)

_WITH_SECRET_SHAPED = _vite_files(
    b"const banner = import.meta.env.VITE_SITE_BANNER;\n"
    b"const tok = import.meta.env.VITE_SERVICE_TOKEN;\n"
    b"document.getElementById('root').textContent = 'r7d4 ' + banner + (tok ? '' : '');\n"
)

_STATIC_INTENT = ReleaseIntent(
    build_cmd=("npm", "run", "build"), output_dir="dist", health_path="/"
)


def test_undeclared_public_build_var_lowers_to_arg_on_the_intent_path(
    _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reproduced live defect (§12.10 clause 1): a source-read public build var
    must lower to a Dockerfile ARG + compose build arg even when the intent declares
    no env — otherwise the built asset silently ships without it."""
    cid = _uid("conv_r7d4pub")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, _PUBLIC_ONLY, intent=_STATIC_INTENT)

    body = _release(client, cid)
    assert body["assessment"] == "candidate" and body["self_host"] is True, body
    texts = _overlay_texts(client, cid)
    assert "ARG VITE_SITE_BANNER" in texts[DOCKERFILE_PATH], texts[DOCKERFILE_PATH]
    assert "VITE_SITE_BANNER" in texts[COMPOSE_PATH], texts[COMPOSE_PATH]


def test_undeclared_secret_shaped_build_var_fails_closed_on_the_intent_path(
    _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reproduced live defect (§12.10 rejection branch): a secret-shaped
    source-discovered build var rejects the bundle exactly as the source path does —
    never silently accepted with the secret dropped."""
    cid = _uid("conv_r7d4sec")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, _WITH_SECRET_SHAPED, intent=_STATIC_INTENT)

    body = _release(client, cid)
    assert body["assessment"] == "needs_review" and body["self_host"] is False, body
    blockers = body["blockers"]
    assert isinstance(blockers, list)
    codes = {str(b["code"]) for b in blockers}
    assert "secret_build_env_unsupported" in codes, codes


def test_source_path_parity_is_preserved(
    _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSITIVE control: the SAME public-var tree WITHOUT an intent (source path)
    already lowers the ARG — the intent path now matches it, not the other way
    around."""
    cid = _uid("conv_r7d4src")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, _PUBLIC_ONLY)

    body = _release(client, cid)
    assert body["assessment"] == "candidate" and body["self_host"] is True, body
    texts = _overlay_texts(client, cid)
    assert "ARG VITE_SITE_BANNER" in texts[DOCKERFILE_PATH], texts[DOCKERFILE_PATH]


def test_prebuilt_static_intent_without_build_is_untouched(
    _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRESERVATION: a prebuilt static serve (no build_cmd) has no build step to
    inject into — no ARG appears and the candidate is unchanged, even when a source
    file mentions a Vite build var."""
    cid = _uid("conv_r7d4pre")
    client, ps = _client(_store, tmp_path, monkeypatch)
    files = {
        "index.html": b"<!doctype html><h1>prebuilt r7d4</h1>\n",
        "notes/reference.js": b"// import.meta.env.VITE_SITE_BANNER mentioned in a comment\n",
    }
    _seed(ps, _store, cid, files, intent=ReleaseIntent(runtime="static", health_path="/"))

    body = _release(client, cid)
    assert body["assessment"] == "candidate" and body["self_host"] is True, body
    texts = _overlay_texts(client, cid)
    assert "ARG " not in texts[DOCKERFILE_PATH], texts[DOCKERFILE_PATH]


# ---- C9-05: build/runtime scope conflict must not silently drop a build value ----


def _blocker_codes(body: dict[str, object]) -> set[str]:
    blockers = body["blockers"]
    assert isinstance(blockers, list)
    return {str(b["code"]) for b in blockers}


def _assert_scope_conflict(client: TestClient, cid: str) -> None:
    body = _release(client, cid)
    assert body["assessment"] == "needs_review", body
    assert body["self_host"] is False and body["spec_digest"] is None, body
    assert "build_env_scope_conflict" in _blocker_codes(body), sorted(_blocker_codes(body))
    # No self-host overlay is offered for a bundle whose built asset would lack the value.
    dl = client.get(f"/api/projects/{cid}/download")
    assert dl.status_code == 200, dl.text
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        assert DOCKERFILE_PATH not in set(zf.namelist()), zf.namelist()


def test_required_env_runtime_shorthand_conflict_needs_review(
    _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C9-05: source reads VITE_SITE_BANNER at build time, but the intent declares the
    SAME name via the runtime-scoped ``required_env`` shorthand — trusting that would
    ship a built asset silently lacking the value. Fail closed."""
    cid = _uid("conv_r7d5req")
    client, ps = _client(_store, tmp_path, monkeypatch)
    intent = ReleaseIntent(
        build_cmd=("npm", "run", "build"),
        output_dir="dist",
        health_path="/",
        required_env=("VITE_SITE_BANNER",),
    )
    _seed(ps, _store, cid, _PUBLIC_ONLY, intent=intent)
    _assert_scope_conflict(client, cid)


def test_explicit_runtime_scope_conflict_needs_review(
    _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C9-05: an EXPLICIT runtime-scope declaration of a build-read var is the same
    conflict — an incompatible runtime declaration is never silently trusted."""
    cid = _uid("conv_r7d5rt")
    client, ps = _client(_store, tmp_path, monkeypatch)
    intent = ReleaseIntent(
        build_cmd=("npm", "run", "build"),
        output_dir="dist",
        health_path="/",
        env=(EnvVarDecl(name="VITE_SITE_BANNER", scope=EnvScope.runtime, required=True),),
    )
    _seed(ps, _store, cid, _PUBLIC_ONLY, intent=intent)
    _assert_scope_conflict(client, cid)


def test_explicit_build_scope_declaration_is_candidate_and_lowered(
    _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C9-05 positive: an EXPLICIT build-scope declaration of the build-read var is a
    candidate whose value lowers to a Dockerfile ARG + compose build arg."""
    cid = _uid("conv_r7d5build")
    client, ps = _client(_store, tmp_path, monkeypatch)
    intent = ReleaseIntent(
        build_cmd=("npm", "run", "build"),
        output_dir="dist",
        health_path="/",
        env=(EnvVarDecl(name="VITE_SITE_BANNER", scope=EnvScope.build, required=True),),
    )
    _seed(ps, _store, cid, _PUBLIC_ONLY, intent=intent)
    body = _release(client, cid)
    assert body["assessment"] == "candidate" and body["self_host"] is True, body
    texts = _overlay_texts(client, cid)
    assert "ARG VITE_SITE_BANNER" in texts[DOCKERFILE_PATH], texts[DOCKERFILE_PATH]
    assert "VITE_SITE_BANNER" in texts[COMPOSE_PATH], texts[COMPOSE_PATH]


def test_unrepresentable_build_var_name_fails_closed_not_500(
    _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C9-05 P3-1: a mixed-case ``import.meta.env.VITE_*`` name matches the scanner but
    cannot form an env-var decl. The route must return 200 needs_review with the typed
    ``build_env_unsupported_name`` blocker — NEVER an uncaught 500."""
    cid = _uid("conv_r7d5mixed")
    client, ps = _client(_store, tmp_path, monkeypatch)
    files = _vite_files(
        b"const banner = import.meta.env.VITE_Site_Banner;\n"
        b"document.getElementById('root').textContent = 'x ' + banner;\n"
    )
    _seed(ps, _store, cid, files, intent=_STATIC_INTENT)

    res = client.get(f"/api/projects/{cid}/release")
    assert res.status_code == 200, res.text  # not a 500
    body = res.json()
    assert body["assessment"] == "needs_review" and body["self_host"] is False, body
    assert "build_env_unsupported_name" in _blocker_codes(body), sorted(_blocker_codes(body))
