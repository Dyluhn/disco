"""WO-C8 live clean-room bundle-lifecycle matrix (plan §12) + the fail-not-skip
Docker/Compose availability assert it is built around (plan §1.3/§4.5).

FROZEN acceptance path (plan §1.1). Selected ONLY by
``-m "export_track1_closeout and integration"`` (the module markers below); excluded
from the default/non-live lanes by the ``integration`` marker.

What this proves (on the self-hosted Docker host that runs the lane): for each of the
six §12 fixtures — Express/Node, FastAPI, Imported Node, Vite/static, AppKit, and
public-build-env Vite — a bundle is obtained through the REAL authenticated
``/release`` -> bound ``/download`` flow, extracted to a fresh dir, and driven through
a real Docker clean-room lifecycle: ``docker compose config --quiet`` -> a NO-CACHE
build with no host coupling -> ``up -d`` to healthy -> a 200 with a fixture-specific
meaningful body on loopback -> ``restart`` still healthy. AppKit additionally writes a
unique record through its public API, restarts, reads the EXACT record back through its
AUTHENTICATED API, and survives a ``down`` (keeping the volume) + ``up`` with migration
initialization succeeding twice. Express and AppKit additionally prove a missing
required runtime env fails before healthy and that the runtime secret is absent from
every bundle byte / build context / image layer-history-filesystem / log while being
present (sanitized) only in the container env. The public-build-env Vite fixture proves
the public build marker reaches the built asset with the secret build sentinel absent —
or that the bundle was correctly rejected as unsupported. Every test owns a UNIQUE
compose project name and a finalizer that tears its resources down (even on assertion
failure) and proves zero labelled resources remain.

Why it is RED on the authoring host: there is no local Docker here (podman is
supplemental and cannot substitute), so ``require_live_runtime`` (the first line of
every fixture test) and the availability test below FAIL — never skip. That is the
CORRECT red for this frozen lane: it cannot be silently green without a real engine.
On a real Docker host at baseline ``2ec1ceba`` these fail for the RIGHT reason (the
C1–C7 gaps — unbound download, unestablished health contract, missing build-env
lowering, non-idempotent migration, secret leakage) and turn green only once C1–C7
land. CI wiring (a required, non-advisory job with ``timeout-minutes`` that uploads
evidence, plan §12.12) is a separate step in the frozen workflow file; these tests are
structured so it can run them with no skip/continue-on-error path.

The seed-derived name fixtures below are mirrored from the closeout ``conftest`` (the
same ``CLOSEOUT_SEED`` source + ``record_property`` evidence hook), because that
conftest is not visible from this ``integration`` directory; per its own documented
policy the seeded-name generator is intentionally mirrored rather than shared through
an importable package.
"""

from __future__ import annotations

import json
import os
import random
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from disco.core import SqliteEventStore

from ._closeout_live_support import (
    HOST_PORT_VAR,
    PUBLIC_BUILD_MARKER,
    SECRET_BUILD_SENTINEL,
    SECRET_ENV_SENTINEL,
    ComposeBundle,
    FixtureWorkspace,
    appkit_fixture,
    appkit_record_plan,
    assert_no_host_coupling,
    assert_sentinel_absent,
    assign_loopback_port,
    await_http_ok,
    becomes_healthy,
    bound_download_to_dir,
    bundle_zip_bytes,
    docker_compose_available,
    express_fixture,
    fastapi_fixture,
    http_response,
    http_status_body,
    imported_node_fixture,
    ingress_service_id,
    public_build_env_vite_fixture,
    release_body,
    release_client,
    require_live_runtime,
    scan_dir_bytes,
    scan_zip_bytes,
    seed_and_cut,
    session_cookie_from,
    vite_fixture,
    write_env_file,
)

pytestmark = [pytest.mark.export_track1_closeout, pytest.mark.integration]


# ---------------------------------------------------------------------------
# Seed-derived names (mirrored from the closeout conftest, plan §4 crit 8).
# ---------------------------------------------------------------------------

CLOSEOUT_SEED_ENV = "CLOSEOUT_SEED"
DEFAULT_CLOSEOUT_SEED = "export-track1-closeout-v1"
_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


@pytest.fixture
def closeout_seed(record_property: Callable[[str, object], None]) -> str:
    """The active randomized seed (``CLOSEOUT_SEED`` or the fixed default), recorded
    into the JUnit ``testcase`` so evidence can prove which seed ran."""
    seed = os.environ.get(CLOSEOUT_SEED_ENV, DEFAULT_CLOSEOUT_SEED)
    record_property("closeout_seed", seed)
    return seed


@pytest.fixture
def closeout_rng(closeout_seed: str) -> random.Random:
    """A deterministic RNG seeded from ``closeout_seed`` (byte-stable reruns)."""
    return random.Random(closeout_seed)


@pytest.fixture
def closeout_name(closeout_rng: random.Random) -> Callable[[str], str]:
    """Factory ``prefix -> "<prefix>-<8 seeded chars>"`` for conversation ids,
    titles, and UNIQUE compose project names (isolation + cleanup keying)."""

    def _make(prefix: str) -> str:
        token = "".join(closeout_rng.choice(_ALPHABET) for _ in range(8))
        return f"{prefix}-{token}"

    return _make


def _cid(make_name: object, prefix: str) -> str:
    assert callable(make_name)
    return str(make_name(prefix))


_MakeProject = Callable[[str, Path], ComposeBundle]


@pytest.fixture
def live_project(request: pytest.FixtureRequest, closeout_name: object) -> _MakeProject:
    """A factory ``(slug, bundle_dir) -> ComposeBundle`` under a UNIQUE seeded compose
    project name. It registers a finalizer that tears the project's resources down and
    proves ZERO labelled resources remain (plan §12.11) — guaranteed to run even after
    an in-body assertion failure."""

    def _make(slug: str, bundle_dir: Path) -> ComposeBundle:
        project = _cid(closeout_name, slug)
        bundle = ComposeBundle(project=project, bundle_dir=bundle_dir)
        request.addfinalizer(bundle.teardown_and_assert_clean)
        return bundle

    return _make


def _run_stateless_core(
    bundle: ComposeBundle,
    fx: FixtureWorkspace,
    port: int,
    *,
    extra_env: dict[str, str] | None = None,
) -> dict[str, object]:
    """The lifecycle shared by every fixture (plan §12.2–§12.5): supply env outside
    the bundle, ``config --quiet`` (exit 0) with no host coupling, a NO-CACHE build,
    ``up -d`` to a 200 with the meaningful body, and a ``restart`` that stays healthy
    with the body still correct. Returns the parsed compose model."""
    env = {HOST_PORT_VAR: str(port)}
    if extra_env:
        env.update(extra_env)
    write_env_file(bundle.bundle_dir, env)

    doc = bundle.config_json()  # §12.2 `docker compose config --quiet` exits 0
    assert_no_host_coupling(doc)  # §12.2 no host node_modules/env/source/image coupling
    bundle.build_no_cache()  # §12.2 no-cache build

    up = bundle.up()  # §12.3 up -d
    assert up.returncode == 0, f"`docker compose up -d` failed:\n{up.stderr}"
    body = await_http_ok(port, fx.health_path)  # §12.3/§12.4 healthy + 200 on loopback
    assert fx.body_marker in body, (  # §12.4 fixture-specific meaningful body
        f"health body at {fx.health_path} lacked the fixture marker {fx.body_marker!r}"
    )

    bundle.restart()  # §12.5 restart
    body2 = await_http_ok(port, fx.health_path)
    assert fx.body_marker in body2, "meaningful body was not correct after restart"
    return doc


# ---------------------------------------------------------------------------
# The fail-not-skip availability assert the whole lane is built around.
# ---------------------------------------------------------------------------


def test_docker_and_compose_are_available_for_the_live_lane() -> None:
    """FAIL (assert), never skip, when the live runtime is absent (plan §1.3/§4.5)."""
    ok, detail = docker_compose_available()
    assert ok, (
        "Docker Engine + Compose v2 are required for the export-track1-closeout live "
        f"lane: {detail}. This lane must FAIL — not skip — without a real engine "
        "(plan §1.3/§4.5). Run it on the self-hosted Docker host, never a hosted "
        "runner without Docker."
    )


# ---------------------------------------------------------------------------
# §12 fixture 1 — Express/Node: full lifecycle + missing-env fail + secret sweep.
# ---------------------------------------------------------------------------


def test_live_express_node_bundle_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
    live_project: _MakeProject,
) -> None:
    """WO-C8 §12 (Express/Node) — §12.1–§12.5, §12.8, §12.9, §12.11.

    Full clean-room lifecycle plus: a missing required runtime env (``APP_SECRET``)
    keeps the service from becoming healthy and supplying it succeeds (§12.8); the
    runtime secret is absent from the zip, extracted build context, image
    history/filesystem, logs, and captured output while present (sanitized) only in
    the container env (§12.9)."""
    require_live_runtime()
    store = SqliteEventStore(":memory:")
    client, ps = release_client(store, tmp_path / "root", monkeypatch)
    cid = _cid(closeout_name, "conv_c8express")
    fx = express_fixture()
    seed_and_cut(ps, store, cid, _cid(closeout_name, "proj"), fx.files, intent=fx.intent)

    body = release_body(client, cid)  # §12.1 real authenticated /release
    assert body["assessment"] == "candidate" and body["self_host"] is True, body
    zip_bytes = bundle_zip_bytes(client, cid, body)
    extract = bound_download_to_dir(client, cid, body, tmp_path / "bundle")  # §12.1 bound /download

    # §12.9 (part): the secret is absent from the zip and the build context BEFORE .env.
    assert_sentinel_absent(
        SECRET_ENV_SENTINEL,
        [
            ("bound download zip", scan_zip_bytes(zip_bytes)),
            ("build context", scan_dir_bytes(extract)),
        ],
    )

    bundle = live_project("c8express", extract)
    port = assign_loopback_port()

    # §12.8: with the required secret omitted, the `${APP_SECRET:?}` guard makes start
    # fail before the service is ever healthy (no config-success assertion here — the
    # DESIRED behavior is that Compose fails closed).
    write_env_file(extract, {HOST_PORT_VAR: str(port)})
    missing = bundle.up()
    assert missing.returncode != 0 or not becomes_healthy(port, fx.health_path, timeout_s=30), (
        "a missing required runtime env (APP_SECRET) must prevent the service from "
        "ever becoming healthy (§12.8); it came up anyway"
    )
    assert_sentinel_absent(
        SECRET_ENV_SENTINEL,
        [("compose output (missing-env)", "".join(bundle.captured_output).encode("utf-8"))],
    )
    bundle.compose("down")

    # §12.8: supplying it succeeds; §12.2–§12.5 healthy lifecycle with the secret set.
    doc = _run_stateless_core(bundle, fx, port, extra_env={"APP_SECRET": SECRET_ENV_SENTINEL})
    service = ingress_service_id(doc)

    # §12.9: sanitized runtime inspection PROVES the secret reached the container env…
    assert bundle.exec_env(service, "APP_SECRET") == SECRET_ENV_SENTINEL, (
        "the required runtime secret never reached the container environment"
    )
    # …yet it is absent from every prohibited surface.
    assert_sentinel_absent(
        SECRET_ENV_SENTINEL,
        [
            ("image history", bundle.image_history_text(service).encode("utf-8")),
            ("final image filesystem", bundle.image_filesystem_bytes(service)),
            ("application logs", bundle.logs().encode("utf-8")),
            ("captured compose output", "".join(bundle.captured_output).encode("utf-8")),
        ],
    )


# ---------------------------------------------------------------------------
# §12 fixture 2 — FastAPI.
# ---------------------------------------------------------------------------


def test_live_fastapi_bundle_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
    live_project: _MakeProject,
) -> None:
    """WO-C8 §12 (FastAPI) — §12.1–§12.5, §12.11. A python candidate boots, serves a
    meaningful GET / body on loopback, and survives restart."""
    require_live_runtime()
    store = SqliteEventStore(":memory:")
    client, ps = release_client(store, tmp_path / "root", monkeypatch)
    cid = _cid(closeout_name, "conv_c8fastapi")
    fx = fastapi_fixture()
    seed_and_cut(ps, store, cid, _cid(closeout_name, "proj"), fx.files, intent=fx.intent)

    body = release_body(client, cid)
    assert body["assessment"] == "candidate" and body["self_host"] is True, body
    extract = bound_download_to_dir(client, cid, body, tmp_path / "bundle")
    bundle = live_project("c8fastapi", extract)
    _run_stateless_core(bundle, fx, assign_loopback_port())


# ---------------------------------------------------------------------------
# §12 fixture 3 — Imported Node.
# ---------------------------------------------------------------------------


def test_live_imported_node_bundle_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
    live_project: _MakeProject,
) -> None:
    """WO-C8 §12 (Imported Node) — §12.1–§12.5, §12.11. An imported plain-http server
    (npm-ci lockfile path, ``imported=True``) boots, serves its meaningful body, and
    survives restart."""
    require_live_runtime()
    store = SqliteEventStore(":memory:")
    client, ps = release_client(store, tmp_path / "root", monkeypatch)
    cid = _cid(closeout_name, "conv_c8imported")
    fx = imported_node_fixture()
    seed_and_cut(
        ps,
        store,
        cid,
        _cid(closeout_name, "proj"),
        fx.files,
        intent=fx.intent,
        imported=fx.imported,
    )

    body = release_body(client, cid)
    assert body["assessment"] == "candidate" and body["self_host"] is True, body
    extract = bound_download_to_dir(client, cid, body, tmp_path / "bundle")
    bundle = live_project("c8imported", extract)
    _run_stateless_core(bundle, fx, assign_loopback_port())


# ---------------------------------------------------------------------------
# §12 fixture 4 — Vite/static.
# ---------------------------------------------------------------------------


def test_live_vite_static_bundle_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
    live_project: _MakeProject,
) -> None:
    """WO-C8 §12 (Vite/static) — §12.1–§12.5, §12.11. An npm Vite build with a
    statically-resolved ``dist`` output dir builds, is served as a static site, and
    survives restart with the meaningful body intact."""
    require_live_runtime()
    store = SqliteEventStore(":memory:")
    client, ps = release_client(store, tmp_path / "root", monkeypatch)
    cid = _cid(closeout_name, "conv_c8vite")
    fx = vite_fixture()
    seed_and_cut(ps, store, cid, _cid(closeout_name, "proj"), fx.files, intent=fx.intent)

    body = release_body(client, cid)
    assert body["assessment"] == "candidate" and body["self_host"] is True, body
    extract = bound_download_to_dir(client, cid, body, tmp_path / "bundle")
    bundle = live_project("c8vite", extract)
    _run_stateless_core(bundle, fx, assign_loopback_port())


# ---------------------------------------------------------------------------
# §12 fixture 5 — AppKit: persistence + migration idempotence.
# ---------------------------------------------------------------------------


def test_live_appkit_persistence_and_migration_idempotence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
    live_project: _MakeProject,
) -> None:
    """WO-C8 §12 (AppKit) — §12.1–§12.9, §12.11.

    The stateful fixture: full lifecycle, plus a missing ``ADMIN_TOKEN`` fails before
    healthy (§12.8); §12.6 drives the generated app's DOCUMENTED auth model
    (acceptance-v5, owner-adjudicated 2026-07-17): a unique user is registered via
    ``/api/register`` under Bearer ``ADMIN_TOKEN``, logged in via ``/api/login``, and
    the returned session cookie writes a unique record; an UNauthenticated read is
    rejected (401); after a ``restart`` the SAME session cookie reads the EXACT
    record back, so an in-memory substitute cannot pass; ``down`` WITHOUT ``-v``
    then ``up`` stays healthy with the session AND record still present and
    migration initialization succeeds a second time over existing state (§12.7);
    and the admin secret is absent from every prohibited surface while present
    (sanitized) only in the container env (§12.9)."""
    require_live_runtime()
    store = SqliteEventStore(":memory:")
    client, ps = release_client(store, tmp_path / "root", monkeypatch)
    cid = _cid(closeout_name, "conv_c8appkit")
    fx, app, worker_src = appkit_fixture()
    seed_and_cut(ps, store, cid, _cid(closeout_name, "proj"), fx.files)

    body = release_body(client, cid)
    assert body["assessment"] == "candidate" and body["self_host"] is True, body
    zip_bytes = bundle_zip_bytes(client, cid, body)
    extract = bound_download_to_dir(client, cid, body, tmp_path / "bundle")
    assert_sentinel_absent(
        SECRET_ENV_SENTINEL,
        [
            ("bound download zip", scan_zip_bytes(zip_bytes)),
            ("build context", scan_dir_bytes(extract)),
        ],
    )

    bundle = live_project("c8appkit", extract)
    port = assign_loopback_port()

    # §12.2 config + no host coupling + no-cache build (npm ci from the AppKit lock),
    # with the required secret supplied so config/build resolve.
    write_env_file(extract, {HOST_PORT_VAR: str(port), "ADMIN_TOKEN": SECRET_ENV_SENTINEL})
    doc = bundle.config_json()
    assert_no_host_coupling(doc)
    service = ingress_service_id(doc)
    bundle.build_no_cache()

    # §12.8: with ADMIN_TOKEN removed, the required-secret guard keeps AppKit from
    # becoming healthy; supplying it (below) succeeds.
    write_env_file(extract, {HOST_PORT_VAR: str(port)})
    missing = bundle.up()
    assert missing.returncode != 0 or not becomes_healthy(port, fx.health_path, timeout_s=45), (
        "a missing required ADMIN_TOKEN must prevent AppKit from becoming healthy (§12.8)"
    )
    bundle.compose("down")

    # §12.3/§12.4: restore the secret; up to healthy with a meaningful body.
    write_env_file(extract, {HOST_PORT_VAR: str(port), "ADMIN_TOKEN": SECRET_ENV_SENTINEL})
    up = bundle.up()
    assert up.returncode == 0, f"AppKit `up -d` failed:\n{up.stderr}"
    assert fx.body_marker in await_http_ok(port, fx.health_path)

    # §12.6 (acceptance-v5): drive the generated app's documented session-auth model.
    route, payload, marker, role = appkit_record_plan(app, worker_src)
    json_headers = {"Content-Type": "application/json"}
    email = f"{_cid(closeout_name, 'user')}@example.test"
    password = _cid(closeout_name, "pw-session")  # seeded, >=8 chars, throwaway

    # 1. register a unique user through /api/register under Bearer ADMIN_TOKEN.
    reg_status, _, reg_body = http_response(
        port,
        "/api/register",
        method="POST",
        headers={**json_headers, "Authorization": f"Bearer {SECRET_ENV_SENTINEL}"},
        data=json.dumps({"email": email, "password": password, "role": role}).encode("utf-8"),
    )
    assert reg_status < 400, f"admin register failed: {reg_status} {reg_body!r}"

    # 2./3. login and capture the returned session cookie.
    login_status, login_headers, login_body = http_response(
        port,
        "/api/login",
        method="POST",
        headers=json_headers,
        data=json.dumps({"email": email, "password": password}).encode("utf-8"),
    )
    assert login_status == 200, f"login failed: {login_status} {login_body!r}"
    cookie = {"Cookie": session_cookie_from(login_headers)}

    # 4. create the unique record with that session cookie.
    post_status, _, post_body = http_response(
        port,
        route,
        method="POST",
        headers={**json_headers, **cookie},
        data=json.dumps(payload).encode("utf-8"),
    )
    assert post_status < 400, f"session record POST to {route} failed: {post_status} {post_body!r}"

    # 5. an unauthenticated read is rejected.
    unauth_status, _ = http_status_body(port, route)
    assert unauth_status == 401, (
        f"an unauthenticated read of {route} must be rejected (401); got {unauth_status} — "
        "entity reads require a valid session"
    )

    # 6. restart; the SAME session cookie reads the EXACT record back.
    bundle.restart()  # §12.5
    assert fx.body_marker in await_http_ok(port, fx.health_path)
    read_status, read_body = http_status_body(port, route, headers=cookie)
    assert read_status == 200 and marker.encode("utf-8") in read_body, (
        "the exact record (or the session that reads it) did not survive restart on the "
        "persistent volume (§12.6); an in-memory substitute would have lost them"
    )

    # 7. §12.7: down WITHOUT -v, up again — healthy, session + record persist,
    # migration initialization succeeds a SECOND time over existing state.
    bundle.down_keep_volume()
    up2 = bundle.up()
    assert up2.returncode == 0, f"second AppKit `up -d` failed:\n{up2.stderr}"
    assert fx.body_marker in await_http_ok(port, fx.health_path), (
        "migration initialization must succeed a SECOND time over existing state (§12.7)"
    )
    read2_status, read2_body = http_status_body(port, route, headers=cookie)
    assert read2_status == 200 and marker.encode("utf-8") in read2_body, (
        "the session and record must remain after a down (keeping the volume) + up (§12.7)"
    )

    # §12.9: sanitized runtime env carries the secret; no prohibited surface does.
    assert bundle.exec_env(service, "ADMIN_TOKEN") == SECRET_ENV_SENTINEL
    assert_sentinel_absent(
        SECRET_ENV_SENTINEL,
        [
            ("image history", bundle.image_history_text(service).encode("utf-8")),
            ("final image filesystem", bundle.image_filesystem_bytes(service)),
            ("application logs", bundle.logs().encode("utf-8")),
            ("captured compose output", "".join(bundle.captured_output).encode("utf-8")),
        ],
    )


# ---------------------------------------------------------------------------
# §12 fixture 6 — public-build-env Vite (§12.10 disjunction).
# ---------------------------------------------------------------------------


def test_live_public_build_env_vite_bundle_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
    live_project: _MakeProject,
) -> None:
    """WO-C8 §12 (public-build-env Vite) — §12.1–§12.5, §12.10, §12.11.

    Either the bundle is a candidate — then it boots, and the PUBLIC build marker
    reaches the built asset while the SECRET build sentinel is absent from image
    layers/history/files/logs — OR the bundle is correctly rejected as unsupported
    (``needs_review`` with ``secret_build_env_unsupported`` and no self-host bundle).
    Baseline satisfies NEITHER branch (the build-env contract is not lowered), so this
    is RED until C4 lands the build-env lowering + secret-build handling."""
    require_live_runtime()
    store = SqliteEventStore(":memory:")
    client, ps = release_client(store, tmp_path / "root", monkeypatch)
    cid = _cid(closeout_name, "conv_c8pubvite")
    fx = public_build_env_vite_fixture()
    seed_and_cut(ps, store, cid, _cid(closeout_name, "proj"), fx.files, intent=fx.intent)

    body = release_body(client, cid)
    codes = {str(b.get("code")) for b in (body.get("blockers") or []) if isinstance(b, dict)}

    if not (body.get("assessment") == "candidate" and body.get("self_host") is True):
        # §12.10 (rejection branch): correctly rejected as unsupported, no bundle offered.
        assert body.get("assessment") == "needs_review" and body.get("self_host") is False, body
        assert "secret_build_env_unsupported" in codes, (
            "a secret-classed build env must be lowered with a real secret mount OR "
            f"rejected with `secret_build_env_unsupported`; saw blockers {sorted(codes)}"
        )
        return

    # §12.10 (accepted branch): the secret-shaped build env used a real secret mount.
    extract = bound_download_to_dir(client, cid, body, tmp_path / "bundle")
    bundle = live_project("c8pubvite", extract)
    port = assign_loopback_port()
    _run_stateless_core(
        bundle,
        fx,
        port,
        extra_env={
            "VITE_PUBLIC_BANNER": PUBLIC_BUILD_MARKER,
            "VITE_ADMIN_SECRET": SECRET_BUILD_SENTINEL,
        },
    )
    service = ingress_service_id(bundle.config_json())

    # §12.10 clause 1: the PUBLIC build marker reached the built (hashed) Vite asset.
    index = await_http_ok(port, fx.health_path).decode("utf-8", "ignore")
    asset_paths = [p for p in _iter_module_srcs(index) if p.endswith(".js")]
    assert asset_paths, f"no built module asset referenced from index.html: {index!r}"
    marker = PUBLIC_BUILD_MARKER.encode("utf-8")
    found = any(marker in http_status_body(port, path)[1] for path in asset_paths)
    assert found, (
        "the public build marker VITE_PUBLIC_BANNER did not reach the built Vite asset "
        "(§12.10 clause 1); the build-env contract was not lowered to a build arg"
    )

    # §12.10 clause 2: the SECRET build sentinel is absent from layers/history/files/logs.
    assert_sentinel_absent(
        SECRET_BUILD_SENTINEL,
        [
            ("image history", bundle.image_history_text(service).encode("utf-8")),
            ("final image filesystem", bundle.image_filesystem_bytes(service)),
            ("built assets", b"".join(http_status_body(port, p)[1] for p in asset_paths)),
            ("application logs", bundle.logs().encode("utf-8")),
        ],
    )


def _iter_module_srcs(html: str) -> list[str]:
    """The ``src="/..."`` module asset paths referenced by a built ``index.html``."""
    return re.findall(r'src=["\']([^"\']+)["\']', html)
