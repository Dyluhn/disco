"""R1 (GAP G02) — the command/credential boundary, at the REAL public boundaries.

Closeout remediation R1. The frozen ``test_g02_positional_credential`` pins three
positional slots through ``release_declare`` + a raw sidecar → ``/release`` +
``/download``. This companion widens the CORPUS (leading / middle / trailing
positional, the ``config set`` shape + role forms, the ``npm set`` shorthand, a
generic high-entropy blob, and the flag / metachar / ``$()`` / backtick / newline /
inline ``NAME=value`` / URL-userinfo forms) and drives each independently through
EACH real boundary, so removing the guard at any one boundary fails that boundary's
test (the committed half of the mutation criterion):

* the real ``release_declare`` tool (``execute_pi_tool``) must fail closed with a
  typed error, echo NO value, and persist ZERO sidecar bytes;
* a directly-written RAW ``release-intent.json`` sidecar (bypassing the tool) must
  make ``GET /api/projects/{cid}/release`` fail closed to ``needs_review`` /
  ``self_host:false`` / no ``spec_digest`` at HTTP 200 (a graceful blocker, never a
  500), leaving the literal ABSENT from the ``/release`` JSON and every ``/download``
  zip entry;
* POSITIVE: a benign command stays ACCEPTED at both boundaries (a self-hostable
  candidate), and a credential passed correctly as a whole declared ``${NAME}``
  reference is accepted — proving the fix rejects secret VALUES, not the safe form.

Boundary + no-echo discipline mirrors the frozen test: real ``ConversationRuntime``
+ real ``ProjectStore`` over a real on-disk root, only ``ConfigStore.load`` seamed;
every credential VALUE is resolved as a LOCAL (never a parametrize id) and a shared
``R1CREDMARK`` marker makes ABSENCE checkable without writing the value to evidence.
"""

from __future__ import annotations

import io
import itertools
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.core import SqliteEventStore, ToolCall, ToolResult
from disco.core.llm import ConfigStore, ProjectStorageSettings, RouterConfig
from disco.tools.projects import ProjectStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Self-contained id maker: these remediation regression tests live OUTSIDE the frozen
# closeout dirs (so they never perturb the acceptance manifest) and therefore cannot use
# the closeout conftest's seeded `closeout_name` fixture. A monotonic counter yields a
# unique, route-valid `conv_`-namespace id per call, which is all these tests need.
_ID_COUNTER = itertools.count()


def _uid(prefix: str) -> str:
    return f"{prefix}_{next(_ID_COUNTER)}"


# Marker-bearing credential VALUES, one per SHAPE FAMILY (so this is a taxonomy, not a
# sentinel). Each embeds the shared marker so ABSENCE is checkable in emitted bytes
# without writing a real-looking secret into the assertion. Matched by structure only.
_MARK = "R1CREDMARK"
_CRED_NPM = "npm_R1CREDMARK0aK7bQ2xR9mL4wZ8vT1nH6pJ3cF5dS"
_CRED_GH = "ghp_R1CREDMARK292c6912E7710c838347Ae178B4a"
_CRED_GENERIC = "wZq3R1CREDMARKXf8Kd0Lp2Rn7Tv1Bh4Mj6Yc9Se5Ag8"
# A base64url / separator-bearing opaque secret (the Tier-B class the generic-rail
# narrowing had regressed on): shell-inert (`-`/`_` are argv-literal chars), so it drives
# both boundaries. The embedded marker keeps ABSENCE checkable.
_CRED_B64URL = "R1CREDMARK_kJ8x-L2mNoPqRs4TuVwXyZ0aB1cD2eF3gH4iJ5"

# A minimal node service (binds $PORT) — its only release-relevant declaration is the
# host-owned intent sidecar, so the command under test is the single thing assessed.
_NODE_FILES: dict[str, bytes] = {
    "server.js": b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n",
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
}

# Shell-INERT credential commands: they pass token hygiene (no metacharacter), so they
# reach BOTH the tool grammar AND the detect-path grammar — the exact G02 hole. Driven
# through the tool boundary AND the raw-sidecar/route boundary.
_INERT_CRED_COMMANDS: dict[str, list[str]] = {
    "pos_leading": ["node", _CRED_NPM, "server.js"],
    "pos_middle": ["node", "server.js", _CRED_GH, "worker.js"],
    "config_set_shape": ["npm", "config", "set", "//registry.example/:_authToken", _CRED_NPM],
    "config_set_role_literal": [
        "npm",
        "config",
        "set",
        "//registry.example/:_authToken",
        "plainliteralvalue1234567",
    ],
    "npm_set_shorthand": ["npm", "set", "//registry.example/:_authToken", _CRED_NPM],
    "generic_blob_positional": ["node", _CRED_GENERIC, "server.js"],
    "base64url_positional": ["node", _CRED_B64URL, "server.js"],
}

# Non-inert credential forms (metachar / substitution / inline assign / secret flag /
# URL userinfo). Already rejected by token hygiene / the secret-flag grammar; asserted
# to STAY rejected (regression) at the tool boundary. A raw sidecar of these would fail
# ReleaseIntent construction, so they are a tool-boundary corpus only.
_NONINERT_CRED_COMMANDS: dict[str, list[str]] = {
    "flag_space_secret": ["node", "srv", "--token", _CRED_NPM],
    "flag_equals_secret": ["node", "srv", f"--token={_CRED_NPM}"],
    "url_userinfo": ["node", "srv", f"https://user:{_CRED_NPM}@api.internal/x"],
    "inline_assign": ["node", f"API_TOKEN={_CRED_NPM}", "server.js"],
    "cmd_subst": ["node", f"$(echo {_CRED_NPM})", "server.js"],
    "backtick": ["node", f"`echo {_CRED_NPM}`", "server.js"],
    "newline": ["node", f"{_CRED_NPM}\nRUN evil", "server.js"],
    "semicolon": ["node", f"{_CRED_NPM};id", "server.js"],
}

_INERT_IDS = sorted(_INERT_CRED_COMMANDS)
_NONINERT_IDS = sorted(_NONINERT_CRED_COMMANDS)
_ALL_TOOL_IDS = sorted({**_INERT_CRED_COMMANDS, **_NONINERT_CRED_COMMANDS})


def _command_for(case_id: str) -> list[str]:
    if case_id in _INERT_CRED_COMMANDS:
        return _INERT_CRED_COMMANDS[case_id]
    return _NONINERT_CRED_COMMANDS[case_id]


# ---- harness (real ASGI app + real ProjectStore; only ConfigStore.load seamed) ----


@pytest.fixture
def _store() -> Iterator[SqliteEventStore]:
    yield SqliteEventStore(":memory:")


def _build(
    store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ConversationRuntime, FastAPI, ProjectStore]:
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
    return runtime, create_app(store, runtime=runtime), ProjectStore(str(tmp_path))


def _seed_project(ps: ProjectStore, store: SqliteEventStore, cid: str, title: str) -> None:
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
    store.create_conversation(cid, owner_id="local", title=title, surface="build")
    cut = ps.cut_version(cid, trigger="closeout")
    assert cut is not None and cut.seq == 1


def _output_blob(result: ToolResult) -> str:
    structured_json = json.dumps(result.structured or {}, ensure_ascii=False)
    return "\n".join([result.content, result.error or "", structured_json])


def _sidecars(root: Path) -> list[Path]:
    return sorted(root.rglob("release-intent.json")) if root.exists() else []


async def _declare(
    runtime: ConversationRuntime, store: SqliteEventStore, cid: str, start_cmd: list[str]
) -> ToolResult:
    store.create_conversation(cid, owner_id="local")
    runtime.set_surface(cid, "build")
    return await runtime.execute_pi_tool(
        cid,
        ToolCall(
            tool_name="release_declare",
            arguments={"start_cmd": list(start_cmd)},
            call_id="closeout-r1",
        ),
    )


def _bound_overlay_texts(client: TestClient, cid: str) -> dict[str, str]:
    rel = client.get(f"/api/projects/{cid}/release")
    if rel.status_code != 200:
        return {}
    body = rel.json()
    if not isinstance(body, dict) or body.get("assessment") != "candidate":
        return {}
    seq, digest = body.get("version_seq"), body.get("spec_digest")
    if not isinstance(seq, int) or not isinstance(digest, str):
        return {}
    res = client.get(f"/api/projects/{cid}/download?version_seq={seq}&spec_digest={digest}")
    if res.status_code != 200:
        return {}
    out: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        for name in zf.namelist():
            out[name] = zf.read(name).decode("utf-8", errors="replace")
    return out


# ===========================================================================
# Boundary 1 — the real release_declare tool must fail closed for the WHOLE corpus.
# ===========================================================================


@pytest.mark.parametrize("case_id", _ALL_TOOL_IDS)
@pytest.mark.asyncio
async def test_release_declare_rejects_credential_command(
    case_id: str,
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real ``release_declare`` rejects every credential-bearing command — the new
    positional / config-set forms AND the pre-existing metachar / secret-flag /
    userinfo forms — value-free, persisting ZERO sidecar bytes. Removing the R1 guard
    from the tool path re-accepts the inert positional/config-set cases → this fails."""
    start_cmd = _command_for(case_id)
    cid = _uid("conv_r1tool")
    runtime, _app, _ps = _build(_store, tmp_path, monkeypatch)
    result = await _declare(runtime, _store, cid, start_cmd)

    assert _MARK not in _output_blob(result), (
        f"[{case_id}] the credential VALUE was echoed in the tool output; a rejection "
        "must never surface the offending value."
    )
    assert not result.success and result.error, (
        f"[{case_id}] release_declare ACCEPTED a credential-bearing command; R1 requires "
        "it to fail closed with a typed error."
    )
    assert not _sidecars(tmp_path), (
        f"[{case_id}] a rejected declare persisted a release-intent sidecar; a rejection "
        "must persist ZERO bytes."
    )


# ===========================================================================
# Boundary 2 — a raw sidecar (bypassing the tool) must fail closed at /release and
# ship nothing through the bound /download, for the shell-inert corpus.
# ===========================================================================


@pytest.mark.parametrize("case_id", _INERT_IDS)
def test_raw_sidecar_credential_fails_closed_at_release_and_download(
    case_id: str,
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directly-written RAW ``release-intent.json`` carrying a shell-inert credential
    command makes ``/release`` fail closed (``needs_review`` / ``self_host:false`` / no
    ``spec_digest``) at HTTP 200, with the literal ABSENT from the ``/release`` JSON and
    every ``/download`` zip entry. Removing the R1 guard from the detect path makes
    ``/release`` a self-hostable candidate that ships the literal → this fails."""
    start_cmd = _command_for(case_id)
    cid = _uid("conv_r1route")
    _runtime, app, ps = _build(_store, tmp_path, monkeypatch)
    client = TestClient(app)
    _seed_project(ps, _store, cid, _uid("proj"))
    ps.release_intent_for(cid).write_text(
        json.dumps({"start_cmd": list(start_cmd)}), encoding="utf-8"
    )

    rel = client.get(f"/api/projects/{cid}/release")
    assert rel.status_code == 200, rel.text
    body = rel.json()
    assert isinstance(body, dict)
    assert _MARK not in rel.text, f"[{case_id}] /release JSON surfaced the credential literal"

    texts = _bound_overlay_texts(client, cid)
    shipped = sorted(name for name, text in texts.items() if _MARK in text)
    assert not shipped, f"[{case_id}] the credential SHIPS in bound /download entries {shipped}"

    assert (
        body["assessment"] == "needs_review"
        and body["self_host"] is False
        and body["spec_digest"] is None
    ), (
        f"[{case_id}] a raw credential sidecar assessed as assessment="
        f"{body['assessment']!r}, self_host={body['self_host']!r}, "
        f"spec_digest={body['spec_digest']!r}; R1 requires it to fail closed to needs_review."
    )


# ===========================================================================
# POSITIVE — benign commands stay accepted at both boundaries; a credential passed
# correctly as a whole declared ${NAME} reference is accepted.
# ===========================================================================

_ACCEPTED_TOOL: dict[str, dict[str, object]] = {
    "node_script": {"start_cmd": ["node", "server.js"]},
    "npm_start": {"start_cmd": ["npm", "start"]},
    "declared_secret_ref": {
        "start_cmd": ["node", "server.js"],
        "build_cmd": ["npm", "run", "build", "--token", "${API_TOKEN}"],
        "required_env": ["API_TOKEN"],
    },
    "config_set_declared_ref": {
        "start_cmd": ["node", "server.js"],
        "install_cmd": [
            "npm",
            "config",
            "set",
            "//registry.example/:_authToken",
            "${NPM_TOKEN}",
        ],
        "required_env": ["NPM_TOKEN"],
    },
}


@pytest.mark.parametrize("case_id", sorted(_ACCEPTED_TOOL))
@pytest.mark.asyncio
async def test_release_declare_accepts_benign_and_declared_reference(
    case_id: str,
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A benign command — and a credential passed correctly as a whole DECLARED
    ``${NAME}`` reference — is ACCEPTED by ``release_declare`` (the fix rejects secret
    VALUES, not the safe reference form)."""
    arguments = dict(_ACCEPTED_TOOL[case_id])
    cid = _uid("conv_r1ok")
    runtime, _app, _ps = _build(_store, tmp_path, monkeypatch)
    _store.create_conversation(cid, owner_id="local")
    runtime.set_surface(cid, "build")
    result = await runtime.execute_pi_tool(
        cid,
        ToolCall(tool_name="release_declare", arguments=arguments, call_id="closeout-r1ok"),
    )
    assert result.success, (
        f"[{case_id}] a benign/declared-reference command was rejected: "
        f"{result.error} / {result.content}"
    )


def test_raw_sidecar_benign_command_is_self_hostable_candidate(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A benign raw sidecar (``node server.js``) over a node project stays a
    self-hostable ``candidate`` — proving the R1 detect-path guard does not
    false-reject a legitimate command."""
    cid = _uid("conv_r1okroute")
    _runtime, app, ps = _build(_store, tmp_path, monkeypatch)
    client = TestClient(app)
    _seed_project(ps, _store, cid, _uid("proj"))
    ps.release_intent_for(cid).write_text(
        json.dumps({"start_cmd": ["node", "server.js"]}), encoding="utf-8"
    )
    rel = client.get(f"/api/projects/{cid}/release")
    assert rel.status_code == 200, rel.text
    body = rel.json()
    assert body["assessment"] == "candidate" and body["self_host"] is True, (
        f"a benign node command failed to assess as a self-hostable candidate: {body}"
    )


# ===========================================================================
# migrate_cmd — the same-class gap: a positional credential in a resource migrate_cmd
# must fail closed at BOTH boundaries and never ship, while benign migrates stay OK.
# ===========================================================================


def _resource_json(migrate_cmd: list[str]) -> dict[str, object]:
    """A minimal valid sqlite ResourceDecl payload carrying ``migrate_cmd`` — the only
    field under test."""
    return {
        "id": "db",
        "kind": "sqlite",
        "persistent_path": "/data/app.db",
        "profiles": {"local": {"url": "file:/data/app.db", "volume": "app-data"}},
        "consumers": ["web"],
        "migrate_cmd": list(migrate_cmd),
    }


# Benign migrations that must stay ACCEPTED (head with a non-runtime migration tool; no
# credential-shaped operand).
_BENIGN_MIGRATE: dict[str, list[str]] = {
    "alembic": ["alembic", "-c", "alembic.ini", "upgrade", "head"],
    "wrangler": ["wrangler", "d1", "migrations", "apply"],
    "manage_py": ["python", "manage.py", "migrate"],
    "npm_run": ["npm", "run", "migrate"],
}


async def _declare_with_resources(
    runtime: ConversationRuntime, store: SqliteEventStore, cid: str, migrate_cmd: list[str]
) -> ToolResult:
    store.create_conversation(cid, owner_id="local")
    runtime.set_surface(cid, "build")
    return await runtime.execute_pi_tool(
        cid,
        ToolCall(
            tool_name="release_declare",
            arguments={
                "start_cmd": ["node", "server.js"],
                "resources": [_resource_json(migrate_cmd)],
            },
            call_id="closeout-r1mig",
        ),
    )


# The migrate credential is carried in the LAST operand of an otherwise-benign migrate;
# both the npm-token shape and the base64url (Tier-B) shape must fail closed.
_MIGRATE_CRED = {"npm_token": _CRED_NPM, "base64url": _CRED_B64URL}


@pytest.mark.parametrize("case_id", sorted(_MIGRATE_CRED))
@pytest.mark.asyncio
async def test_release_declare_rejects_credential_in_migrate_cmd(
    case_id: str,
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``release_declare`` fails closed when a resource ``migrate_cmd`` carries a bare
    positional literal credential (npm-token OR base64url) — value-free, ZERO sidecar
    bytes persisted."""
    cid = _uid("conv_r1migtool")
    runtime, _app, _ps = _build(_store, tmp_path, monkeypatch)
    result = await _declare_with_resources(
        runtime, _store, cid, ["alembic", "upgrade", _MIGRATE_CRED[case_id]]
    )
    assert _MARK not in _output_blob(result), "a rejection must never echo the migrate credential"
    assert not result.success and result.error, (
        f"[{case_id}] release_declare ACCEPTED a positional credential in a resource "
        "migrate_cmd; R1 requires it to fail closed."
    )
    assert not _sidecars(tmp_path), "a rejected declare must persist ZERO sidecar bytes"


@pytest.mark.parametrize("case_id", sorted(_MIGRATE_CRED))
def test_raw_sidecar_credential_in_migrate_cmd_fails_closed(
    case_id: str,
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A RAW sidecar whose resource ``migrate_cmd`` carries a positional literal
    credential (npm-token OR base64url) makes ``/release`` fail closed (``needs_review``
    / ``self_host:false`` / no ``spec_digest``) at HTTP 200, with the credential ABSENT
    from the ``/release`` JSON and every ``/download`` zip entry (compose.yaml /
    release.json included). Before R1 this was a self-hostable candidate that shipped the
    credential verbatim."""
    cid = _uid("conv_r1migroute")
    _runtime, app, ps = _build(_store, tmp_path, monkeypatch)
    client = TestClient(app)
    _seed_project(ps, _store, cid, _uid("proj"))
    ps.release_intent_for(cid).write_text(
        json.dumps(
            {
                "start_cmd": ["node", "server.js"],
                "resources": [_resource_json(["alembic", "upgrade", _MIGRATE_CRED[case_id]])],
            }
        ),
        encoding="utf-8",
    )

    rel = client.get(f"/api/projects/{cid}/release")
    assert rel.status_code == 200, rel.text
    body = rel.json()
    assert _MARK not in rel.text, "/release JSON surfaced the migrate credential literal"

    texts = _bound_overlay_texts(client, cid)
    shipped = sorted(name for name, text in texts.items() if _MARK in text)
    assert not shipped, f"the migrate credential SHIPS in bound /download entries {shipped}"

    assert (
        body["assessment"] == "needs_review"
        and body["self_host"] is False
        and body["spec_digest"] is None
    ), (
        f"a raw sidecar with a credential migrate_cmd assessed as assessment="
        f"{body['assessment']!r}, self_host={body['self_host']!r}, "
        f"spec_digest={body['spec_digest']!r}; R1 requires it to fail closed."
    )


@pytest.mark.parametrize("case_id", sorted(_BENIGN_MIGRATE))
@pytest.mark.asyncio
async def test_release_declare_accepts_benign_migrate_cmd(
    case_id: str,
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Benign migration commands (alembic / wrangler / manage.py / npm run migrate) in a
    resource ``migrate_cmd`` stay ACCEPTED by ``release_declare`` — the migrate rail
    rejects credential VALUES, not legitimate migration operands."""
    cid = _uid("conv_r1migok")
    runtime, _app, _ps = _build(_store, tmp_path, monkeypatch)
    result = await _declare_with_resources(runtime, _store, cid, _BENIGN_MIGRATE[case_id])
    assert result.success, (
        f"[{case_id}] a benign migrate command was rejected: {result.error} / {result.content}"
    )
