"""WO-C5 #2 — emission-path hardening (F1 / F4 / F2).

Three gaps the injection matrices (`test_c5_emission_injection.py` /
`test_c5_injection_reject.py`) did not pin, closed by the campaign's OTHER
invariants (secret-free bundles / no silent-broken-bundle / build-robustness):

* F1 (secret-free bundle) — the runtime command GRAMMAR (`check_declaration_argv`)
  ran ONLY in the `release_declare` tool. The `/release → /download` emission path
  (`detect._from_intent → emit_local_compose`) applied only token hygiene + a
  HEAD-only toolchain check, so a raw `release-intent.json` carrying a secret CLI
  form (`--token VALUE` / `--password VALUE` / URL userinfo) was lowered VERBATIM
  into the emitted `Dockerfile` CMD + `release.json` — baking the secret into the
  bundle. The fix applies the SAME grammar on the emission path (→ `needs_review`,
  no bundle), mirroring C4's build-secret gate at the authoritative boundary. RED on
  `2121e5d9`: the secret value survives into the emitted bytes.

* F4 (no accept-but-break) — a `--flag=${NAME}` token passes hygiene (the sanctioned
  `--token=${API_TOKEN}` form the tool ACCEPTS) but is NOT a WHOLE `${NAME}`
  reference, so the baseline `_exec_or_shell` renders it as an inert literal — the
  container would run `node --port=${PORT}` verbatim and crash. The fix makes the
  emitter EXPAND it (shell-concatenation `'--flag='"${NAME}"`), so `${NAME}` resolves
  at runtime. RED on `2121e5d9`: the CMD is the exec-array (no-shell) form, so the
  reference never expands.

* F2 (Dockerfile build-robustness) — `check_workspace_rel_path` admitted a leading
  `-`, so `root="--link"` emitted `COPY --link/ ./` (the shell-form COPY parses
  `--link` as a flag → the build fails). The fix rejects any path SEGMENT beginning
  with `-`. RED on `2121e5d9`: the emitted COPY source is `--link/`.

Boundary (plan §1.2 / §4 crit 3+4): F1 drives the REAL public `/release → /download`
route over a real `ProjectStore` + a real committed version (only `ConfigStore.load`
is seamed — the settings-PUT injection); the malicious intent is planted as RAW
sidecar JSON so the outcome is decided by the real route/detector/emitter. F4 / F2
drive the REAL emitter `emit_local_compose` directly over an adversarial spec built
with pydantic's non-validating `model_copy(update=...)` — the exact bypass the
validation plane's docstring says must be judged honestly. No mock/patch/fake.

DEFERRED (plan §9.9): the live-container canary belongs to the C8 live lane; a
passing byte-inspection is necessary but not sufficient there.
"""

from __future__ import annotations

import io
import json
import shlex
import subprocess
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, ProjectStorageSettings, RouterConfig
from disco.core.release.local_compose import DOCKERFILE_PATH, emit_local_compose
from disco.core.release.spec import (
    DetectorProvenance,
    ReleaseAssessment,
    ReleaseService,
    ReleaseSpec,
    RuntimeStrategy,
    ServiceRole,
)
from disco.tools.projects import ProjectStore
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

pytestmark = pytest.mark.export_track1_closeout

_NODE_FILES: dict[str, bytes] = {
    "server.js": b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n",
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
}

# A node workspace whose package.json ALSO defines a `migrate` script and ships the
# `migrate.js` it runs, so a benign `npm run migrate` / `node migrate.js` migration has a
# PROVABLE target (Batch-3 migrate-target proof). Used by the legit-migrate positive control:
# after Batch-3, an UNPROVABLE migrate target (a bare `alembic` / `wrangler` head the node
# image cannot run, or an `npm run migrate` with no such script) correctly fails closed to
# `needs_review`, so a candidate positive control must name a target the tree/install proves.
_NODE_FILES_WITH_MIGRATE: dict[str, bytes] = {
    "server.js": _NODE_FILES["server.js"],
    "package.json": (
        b'{"name":"svc","scripts":{"start":"node server.js","migrate":"node migrate.js"}}'
    ),
    "migrate.js": b"process.exit(0);\n",
}

# A python workspace whose exact pip plan installs `alembic`, so an `alembic upgrade head`
# migration has a PROVABLE target under the Batch-3 migrate-target proof. Used by the
# declared-credential-ref positive control (whose intent is that a WHOLE ${NAME} credential
# ref in a migrate_cmd stays a candidate — a bare-tool migrate carrying a credential flag is
# only representable on a runtime whose install plan actually provides the tool).
_PY_MIGRATE_FILES: dict[str, bytes] = {
    "main.py": b"from fastapi import FastAPI\napp = FastAPI()\n",
    "requirements.txt": b"fastapi\nuvicorn\nalembic\n",
}


# ---- real ASGI app + real ProjectStore (only ConfigStore.load is seamed) --------


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
    app: FastAPI = create_app(store, runtime=runtime)
    return TestClient(app), ProjectStore(str(tmp_path))


def _cid(make_name: object, prefix: str) -> str:
    assert callable(make_name)
    return str(make_name(prefix))


def _seed_raw(
    ps: ProjectStore,
    store: SqliteEventStore,
    cid: str,
    raw_intent: dict[str, Any],
    files: dict[str, bytes] = _NODE_FILES,
) -> None:
    """A real on-disk workspace + manifest, a RAW release-intent sidecar (written
    directly, not via the tool, so it can plant a form the fix will reject downstream),
    a conversation record, and a committed version 1. ``files`` defaults to the minimal
    node app; a caller passes an alternate tree when a migration needs a provable target."""
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
        title="proj",
        owner_id="local",
        created_at="2026-06-06T00:00:00Z",
        file_count=len(files),
        total_bytes=total,
        imported=False,
    )
    ps.release_intent_for(cid).write_text(json.dumps(raw_intent))
    store.create_conversation(cid, owner_id="local", title="proj", surface="build")
    cut = ps.cut_version(cid, trigger="closeout")
    assert cut is not None and cut.seq == 1, "precondition: a real version 1 was committed"


def _download_bytes_texts(client: TestClient, cid: str) -> dict[str, str]:
    """The FULL emitted bundle TEXT (every zip member) — but ONLY when the project is
    a self-hostable `candidate`. A fail-closed `needs_review` (the SAFE post-fix
    outcome) emits no bundle, so this returns `{}` and any planted value is
    definitionally absent from the shipped bytes."""
    rel = client.get(f"/api/projects/{cid}/release")
    if rel.status_code != 200 or rel.json().get("assessment") != "candidate":
        return {}
    dl = client.get(f"/api/projects/{cid}/download")
    if dl.status_code != 200:
        return {}
    texts: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        for name in zf.namelist():
            texts[name] = zf.read(name).decode("utf-8", "replace")
    return texts


def _assessment(client: TestClient, cid: str) -> str:
    """The `/release` assessment, TOLERANT of a fail-closed non-200. A raw intent that
    a value-guard rejects at the schema boundary (e.g. URL userinfo, caught by token
    hygiene) surfaces as a fail-closed non-200 rather than a 200 `needs_review`; either
    way it is NOT a self-host `candidate`, and the load-bearing invariant (the secret
    reaches no shipped byte) is asserted separately over the download. A non-200 maps to
    a non-candidate sentinel that also names the status for diagnosis."""
    rel = client.get(f"/api/projects/{cid}/release")
    if rel.status_code != 200:
        return f"fail_closed_{rel.status_code}"
    value = rel.json().get("assessment")
    assert isinstance(value, str)
    return value


def _release_body(client: TestClient, cid: str) -> str:
    return client.get(f"/api/projects/{cid}/release").text


# ===========================================================================
# F1 — a secret CLI form in a raw intent is REJECTED on the emission path, so it
# never reaches the shipped bundle. RED on 2121e5d9 (the grammar ran only in the
# tool; the route lowered the secret argv verbatim into Dockerfile CMD + release.json).
# ===========================================================================

_SECRET_CLI_INTENTS: list[tuple[str, list[str]]] = [
    ("token_space", ["node", "server.js", "--token", "C5F1SECRET"]),
    ("token_equals", ["node", "server.js", "--token=C5F1SECRET"]),
    ("password", ["node", "server.js", "--password", "C5F1SECRET"]),
    ("api_key", ["node", "server.js", "--api-key", "C5F1SECRET"]),
    ("credential", ["node", "server.js", "--credential", "C5F1SECRET"]),
    ("url_userinfo", ["node", "server.js", "--upstream", "https://u:C5F1SECRET@api.internal/x"]),
    ("unknown_flag_literal", ["node", "server.js", "--config", "C5F1SECRET"]),
]


@pytest.mark.parametrize(
    "case_id,start_cmd", _SECRET_CLI_INTENTS, ids=[c[0] for c in _SECRET_CLI_INTENTS]
)
def test_secret_cli_intent_never_reaches_emitted_bundle(
    case_id: str,
    start_cmd: list[str],
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C5 #2 F1 — RED on 2121e5d9.

    A raw release intent carrying a secret CLI value must fail closed on the emission
    path (the SAME runtime grammar the tool applies), so no bundle is emitted and the
    planted value `C5F1SECRET` appears in NO shipped byte. Baseline lowered it verbatim
    into the emitted Dockerfile CMD + release.json."""
    cid = _cid(closeout_name, "conv_c5f1")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_raw(ps, _store, cid, {"start_cmd": start_cmd})

    # The safe outcome is fail-closed (needs_review, or a secret-free non-200 for a
    # hygiene-rejected form) — never a self-host candidate.
    assert _assessment(client, cid) != "candidate", (
        f"[{case_id}] a secret-CLI start command was accepted as a self-host candidate; "
        "the emission-path runtime grammar must fail it closed."
    )
    # The load-bearing invariant: the planted secret is in NO shipped byte, and NO
    # /release response byte, regardless of how the route reports the fail-closed outcome.
    assert "C5F1SECRET" not in _release_body(client, cid), (
        f"[{case_id}] the secret value was echoed in the /release response."
    )
    for name, text in _download_bytes_texts(client, cid).items():
        assert "C5F1SECRET" not in text, (
            f"[{case_id}] the secret value was baked into the emitted {name}; the runtime "
            "grammar must reject a secret CLI form on the emission path (WO-C5 #2 F1)."
        )


def test_declared_env_ref_intent_still_a_candidate(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C5 #2 F1 (positive) — the sanctioned form is NOT over-rejected.

    A whole DECLARED `${NAME}` reference on a flag (`--token ${API_TOKEN}`, API_TOKEN in
    required_env) is the legitimate way to pass a secret by name; the emission-path
    grammar must keep such an intent a self-host candidate (no literal is shipped)."""
    cid = _cid(closeout_name, "conv_c5f1ok")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_raw(
        ps,
        _store,
        cid,
        {
            "start_cmd": ["node", "server.js", "--token", "${API_TOKEN}"],
            "required_env": ["API_TOKEN"],
        },
    )
    assert _assessment(client, cid) == "candidate", (
        "a whole declared ${NAME} secret reference must remain a self-host candidate; the "
        "emission-path grammar must reject secret VALUES, not the safe reference form."
    )


# ===========================================================================
# F4 — a `--flag=${NAME}` token EXPANDS in the emitted command (never a literal).
# RED on 2121e5d9 (`_exec_or_shell` renders it as an inert exec-array literal, so
# the container runs `--port=${PORT}` verbatim and crashes).
# ===========================================================================


def _flag_ref_node_spec() -> ReleaseSpec:
    service = ReleaseService(
        id="web",
        role=ServiceRole.ingress,
        runtime=RuntimeStrategy.node,
        # `--port=${PORT}` — the accept-but-break form: passes hygiene (the tool accepts
        # `--token=${API_TOKEN}`) but is not a WHOLE reference.
        start_cmd=("node", "server.js", "--port=${PORT}"),
        port_env="PORT",
    )
    provenance = DetectorProvenance(
        detector="closeout-c5", detector_version="2", assessment=ReleaseAssessment.candidate
    )
    return ReleaseSpec(
        kind="node",
        name="app",
        version_seq=1,
        tree_digest="a" * 64,
        services=(service,),
        provenance=provenance,
    )


def _cmd_array(dockerfile: str) -> list[str]:
    for line in dockerfile.splitlines():
        if line.startswith("CMD "):
            value = json.loads(line[len("CMD ") :])
            assert isinstance(value, list)
            return [str(token) for token in value]
    raise AssertionError(f"no CMD line in emitted Dockerfile:\n{dockerfile}")


def test_flag_equals_env_ref_expands_in_emitted_command() -> None:
    """WO-C5 #2 F4 — RED on 2121e5d9.

    A `--flag=${NAME}` token must be lowered so `${NAME}` is SHELL-EXPANDABLE (never a
    literal exec token). Baseline's `_exec_or_shell` sees no WHOLE reference and emits
    the exec-array form `CMD ["node","server.js","--port=${PORT}"]`, so the container
    runs `--port=${PORT}` verbatim. The fix emits the `sh -c` expand form; running its
    body with `PORT` set must resolve `--port=8080`."""
    dockerfile = emit_local_compose(_flag_ref_node_spec())[DOCKERFILE_PATH]
    cmd = _cmd_array(dockerfile)

    # A reference-bearing command must use the shell EXPAND form — never an exec-array
    # that would pass `--port=${PORT}` to the process as a literal argument.
    assert cmd[:2] == ["sh", "-c"], (
        f"a ${{NAME}}-bearing command must lower to the `sh -c` expand form so the "
        f"reference resolves; got an inert exec array {cmd} (WO-C5 #2 F4)."
    )
    body = cmd[2]
    assert body.startswith("exec "), body
    # Prove the emitted bytes RESOLVE: run the body (exec -> printf) under a real shell
    # with PORT set and confirm `--port=8080` — the reference expanded and concatenated
    # to the flag prefix — and that the literal `${PORT}` did not survive.
    probe = "printf '%s\\n' " + body[len("exec ") :]
    completed = subprocess.run(
        ["sh", "-c", probe],
        capture_output=True,
        text=True,
        env={"PORT": "8080", "PATH": "/usr/bin:/bin"},
        check=True,
    )
    resolved = [line for line in completed.stdout.split("\n") if line]
    assert "--port=8080" in resolved, (
        f"the emitted command did not resolve ${{PORT}} in `--port=${{PORT}}`; "
        f"resolved argv was {resolved} (WO-C5 #2 F4 — accept-but-break)."
    )
    assert "--port=${PORT}" not in resolved, (
        f"the emitted command passed `--port=${{PORT}}` as a LITERAL argument "
        f"(resolved argv {resolved}); the reference must expand (WO-C5 #2 F4)."
    )
    # Sanity: shlex parses the body (it is a well-formed shell word list).
    assert shlex.split(body)  # no exception


# ===========================================================================
# F2 — a service `root` whose segment begins with `-` cannot emit a `COPY -flag/`
# line. RED on 2121e5d9 (`check_workspace_rel_path` admitted a leading `-`, so
# `root="--link"` emitted `COPY --link/ ./` — a malformed COPY the build rejects).
# ===========================================================================

_LEADING_DASH_ROOTS: list[tuple[str, str]] = [
    ("double_dash_flag", "--link"),
    ("single_dash_flag", "-x"),
    ("nested_dash_segment", "app/-x"),
]


@pytest.mark.parametrize(
    "case_id,root", _LEADING_DASH_ROOTS, ids=[c[0] for c in _LEADING_DASH_ROOTS]
)
def test_leading_dash_root_cannot_emit_flag_copy_source(case_id: str, root: str) -> None:
    """WO-C5 #2 F2 — RED on 2121e5d9.

    A service `root` whose path segment begins with `-` must be rejected (a
    Dockerfile `COPY <root>/ ./` would parse the leading-dash segment as a COPY flag
    and the build would fail). Baseline's `check_workspace_rel_path` admitted a leading
    `-`, so `root='--link'` emitted `COPY --link/ ./`."""
    base = _flag_ref_node_spec()
    # Reset the start_cmd to a plain one so ONLY the root is adversarial.
    plain = base.services[0].model_copy(update={"start_cmd": ("node", "server.js"), "root": root})
    bad_spec = base.model_copy(update={"services": (plain,)})
    try:
        overlay = emit_local_compose(bad_spec)
    except (ValueError, ValidationError):
        return  # fail-closed emission — no malformed COPY can appear
    dockerfile = overlay.get(DOCKERFILE_PATH, "")
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.startswith("COPY ") and not stripped.startswith("COPY --from="):
            parts = stripped.split()
            if len(parts) >= 2:
                source = parts[1]
                assert not source.startswith("-"), (
                    f"[{case_id}] a leading-dash root emitted a COPY source {source!r} that "
                    "parses as a COPY flag; WO-C5 #2 F2 rejects a path segment starting with '-'."
                )


# ===========================================================================
# F5 — a resource `migrate_cmd` inline secret-CLI value never reaches the shipped
# bundle. RED on 345ad5dd (the runtime grammar gated only start/build; a resource
# migrate_cmd was lowered VERBATIM into the compose migrate-service `command:` +
# serialized into release.json, so `--token SECRET` shipped in plaintext).
# ===========================================================================


def _sqlite_resource_json(migrate_cmd: list[str]) -> dict[str, Any]:
    """A valid raw sqlite `ResourceDecl` sidecar shape carrying `migrate_cmd`."""
    return {
        "id": "db",
        "kind": "sqlite",
        "persistent_path": "/data/app.db",
        "profiles": {"local": {"url": "file:/data/app.db", "volume": "app-data"}},
        "consumers": ["web"],
        "migrate_cmd": migrate_cmd,
    }


_MIGRATE_SECRET_CMDS: list[tuple[str, list[str]]] = [
    ("token_space", ["alembic", "upgrade", "head", "--token", "C5F5SECRET"]),
    ("password_space", ["alembic", "upgrade", "head", "--password", "C5F5SECRET"]),
    ("api_key_equals", ["alembic", "upgrade", "head", "--api-key=C5F5SECRET"]),
    ("credential_space", ["wrangler", "d1", "execute", "db", "--credential", "C5F5SECRET"]),
    ("access_token_equals", ["alembic", "upgrade", "--access-token=C5F5SECRET"]),
]


@pytest.mark.parametrize(
    "case_id,migrate_cmd", _MIGRATE_SECRET_CMDS, ids=[c[0] for c in _MIGRATE_SECRET_CMDS]
)
def test_migrate_cmd_inline_secret_never_reaches_bundle(
    case_id: str,
    migrate_cmd: list[str],
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C5 #3 F5 — RED on 345ad5dd.

    A resource `migrate_cmd` carrying an inline secret on a credential-bearing flag
    must fail closed on the emission path (head-agnostic inline-secret hygiene), so no
    bundle is emitted and the planted value `C5F5SECRET` appears in NO shipped byte
    (compose.yaml migrate command + release.json). Baseline lowered it verbatim."""
    cid = _cid(closeout_name, "conv_c5f5")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_raw(
        ps,
        _store,
        cid,
        {"start_cmd": ["node", "server.js"], "resources": [_sqlite_resource_json(migrate_cmd)]},
    )

    assert _assessment(client, cid) != "candidate", (
        f"[{case_id}] a migrate_cmd with an inline secret was accepted as a self-host "
        "candidate; the emission-path inline-secret hygiene must fail it closed."
    )
    assert "C5F5SECRET" not in _release_body(client, cid), (
        f"[{case_id}] the secret value was echoed in the /release response."
    )
    for name, text in _download_bytes_texts(client, cid).items():
        assert "C5F5SECRET" not in text, (
            f"[{case_id}] the migrate secret was baked into the emitted {name}; a resource "
            "migrate_cmd inline secret must be rejected on the emission path (WO-C5 #3 F5)."
        )


def test_migrate_cmd_declared_secret_ref_is_candidate(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C5 #3 F5 (positive) — a migration credential passed as a whole DECLARED
    `${NAME}` reference (`--token ${DB_TOKEN}`, DB_TOKEN in required_env) carries no
    literal and must remain a self-host candidate — the fix rejects inline VALUES, not
    the safe reference form.

    Batch-3: an `alembic` migration is only a PROVABLE target on a runtime whose install
    plan installs alembic, so this rides a python app whose `requirements.txt` includes
    alembic (the migrate-target proof passes); the load-bearing assertion — a declared
    ${NAME} credential ref is not treated as an inline secret — is unchanged."""
    cid = _cid(closeout_name, "conv_c5f5ok")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_raw(
        ps,
        _store,
        cid,
        {
            "runtime": "python",
            "start_cmd": ["uvicorn", "main:app"],
            "required_env": ["DB_TOKEN"],
            "resources": [
                _sqlite_resource_json(["alembic", "upgrade", "head", "--token", "${DB_TOKEN}"])
            ],
        },
        files=_PY_MIGRATE_FILES,
    )
    assert _assessment(client, cid) == "candidate", (
        "a migrate_cmd whose credential is a whole declared ${NAME} reference must remain "
        "a self-host candidate; F5 rejects inline secret VALUES, not the reference form."
    )


# Benign, non-secret migrations whose targets are PROVABLE in the seeded node tree
# (`_NODE_FILES_WITH_MIGRATE`): a direct `node <file>` whose file ships, and an `npm run
# <script>` the package.json defines. Batch-3 note: before the migrate-target proof this
# list read `alembic -c … upgrade head` / `wrangler d1 migrations apply` on a NODE app —
# both bare tools the node image cannot run — so they were FALSE candidates the emitted
# one-shot migration service could never exec. The frozen adversarial harness (MIGRATE-01..
# 04) requires exactly such unprovable targets to fail closed, so the positive control now
# names targets the tree/install proves; the credential-hygiene invariant it guards (a
# benign migrate is not rejected by the SECRET rail) is unchanged.
_LEGIT_MIGRATE_CMDS: list[tuple[str, list[str]]] = [
    ("node_file", ["node", "migrate.js"]),
    ("npm_run", ["npm", "run", "migrate"]),
]


@pytest.mark.parametrize(
    "case_id,migrate_cmd", _LEGIT_MIGRATE_CMDS, ids=[c[0] for c in _LEGIT_MIGRATE_CMDS]
)
def test_legit_migrate_commands_stay_candidate(
    case_id: str,
    migrate_cmd: list[str],
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C5 #3 F5 (positive) — head-AGNOSTIC secret hygiene must NOT reject a legitimate
    non-secret migration command. A benign migration with a PROVABLE target (`node
    migrate.js` whose file ships, `npm run migrate` the package.json defines) with no
    credential-bearing flag stays a self-host candidate — this is inline-secret data
    hygiene, not a blanket rejection of the migration step."""
    cid = _cid(closeout_name, "conv_c5f5legit")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_raw(
        ps,
        _store,
        cid,
        {"start_cmd": ["node", "server.js"], "resources": [_sqlite_resource_json(migrate_cmd)]},
        files=_NODE_FILES_WITH_MIGRATE,
    )
    assert _assessment(client, cid) == "candidate", (
        f"[{case_id}] a legitimate non-secret migrate command was rejected; F5 hygiene is "
        "head-agnostic and must only reject an INLINE secret, never a benign migration tool."
    )
