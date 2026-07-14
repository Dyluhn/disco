"""GAP G02 [critical security] — a bare POSITIONAL literal credential ships.

Closeout remediation finding G02. The frozen WO-C5 injection matrix rejects every
secret-bearing CLI *flag* form — ``--token VALUE`` / ``--token=VALUE`` /
``--password`` / ``--secret`` / ``--api-key`` / ``--credential`` / URL userinfo
(``test_c5_injection_reject.py``) — and every inline ``NAME=value`` smuggle. It does
NOT reject a bare POSITIONAL literal credential: a plain command operand (no leading
flag, no ``=``, no shell metacharacter) that passes every existing value guard.

Concretely, ``npm config set //registry.example/:_authToken <LITERAL_TOKEN>`` sets an
npm registry auth token where ``<LITERAL_TOKEN>`` is a positional operand. The token
is shell-inert (it matches the argv literal charset), is not a ``${NAME}`` reference,
carries no ``=``/flag/metacharacter, and is not a runtime env NAME — so it passes
``check_token_hygiene`` (the per-token guard the ``ReleaseIntent`` validators run) AND
sails through the positional-operand branch of ``check_declaration_argv`` (the runtime
grammar the ``release_declare`` tool + the detect-path ``_command_grammar_blocker``
apply). The declaration is ACCEPTED, the literal PERSISTS in ``release-intent.json``,
and it SHIPS verbatim in the emitted ``Dockerfile`` CMD + ``release.json`` inside the
bound ``/download`` zip.

The tip has consciously documented this in ``detect._command_grammar_blocker`` as an
"INHERENT ACCEPTED LIMIT … declaring a secret as a positional value is a caller error
the representation cannot detect". G02 [critical security] OVERRIDES that self-declared
carve-out: a literal credential baked into an exported start command is a shipped
secret regardless of whether it rode a flag or a positional slot, and the export must
fail closed the same way it does for the flag forms.

Boundary (plan §1.2 / §4 crit 3+4): every case drives the REAL public boundary — a
real ``ConversationRuntime`` + real ``ProjectStore`` over a real on-disk root, the real
``release_declare`` ToolExecutor (``execute_pi_tool``), the real
``GET /api/projects/{cid}/release`` route, and the real bound
``GET /download?version_seq=&spec_digest=`` zip bytes. Nothing under test is
mocked/patched/faked. The ONLY seam is ``monkeypatch.setattr(cfg_store, "load", …)``
(the config seam the settings PUT performs); the ``_client``/``_seed`` harness is copied
verbatim from ``test_c4_env_build_toolchain_matrix.py`` (real ConversationRuntime /
ProjectStore, only ``ConfigStore.load`` monkeypatched). The RED half plants the literal
as a RAW ``release-intent.json`` sidecar (bypassing the tool) so the route / detector /
emitter decide the outcome and the test is robust to the fix hardening the tool.

The credential covers the LEADING, MIDDLE, and TRAILING positional positions (including
the canonical ``npm config set <key> <LITERAL>`` form). A unique, npm-token-shaped
sentinel makes ABSENCE checkable in the emitted bytes.

RED vs GREEN on candidate ``581dfbe`` (empirically probed):
  * RED — ``release_declare`` ACCEPTS the positional-credential command (``success`` is
    True) and persists a ``release-intent.json`` carrying the literal; ``/release``
    assesses a self-hostable ``candidate`` (``self_host:true``, a bound ``spec_digest``)
    and the bound ``/download`` zip ships the literal verbatim in the ``Dockerfile`` CMD
    and ``release.json``.
  * GREEN (the asserted OPPOSITE / correct behavior) — the tool REJECTS it (fail closed,
    typed error, value never echoed, ZERO sidecar bytes persisted) and a RAW sidecar
    fails closed at ``/release`` (``needs_review``, ``self_host:false``, no bound digest),
    so no self-host overlay is emitted and the literal is ABSENT from the ``/release``
    JSON and every ``/download`` zip entry.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.core import SqliteEventStore, ToolCall, ToolResult
from disco.core.llm import ConfigStore, ProjectStorageSettings, RouterConfig
from disco.core.release.local_compose import COMPOSE_PATH, DOCKERFILE_PATH, RELEASE_JSON_PATH
from disco.core.release.spec import ReleaseIntent
from disco.tools.projects import ProjectStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.export_track1_closeout

# An npm-token-shaped LITERAL credential (npm access tokens are `npm_<base62>`). It is
# shell-inert (only the argv literal charset), is NOT a `${NAME}` reference, and carries
# no `=`/flag/metacharacter, so it passes every existing value guard — yet by its
# `npm_`+high-entropy format it is unmistakably credential material, not a benign
# operand like `server.js` / `config` / `set` / `//registry.example/:_authToken`.
_CRED_SENTINEL = "npm_G02POSCRED0aK7bQ2xR9mL4wZ8vT1nH6pJ3cF5dS"
# The unique substring used for ABSENCE checks: it survives the 120-char provenance
# evidence clamp in release.json, so a check for it catches the literal in EVERY surface
# it can reach (the Dockerfile CMD, the release.json start_cmd array, and the clamped
# provenance evidence line) — "absence is checkable".
_CRED_MARKER = "G02POSCRED"


def _redact(value: object) -> str:
    """``repr(value)`` with the planted G02 credential MASKED, so a failure message that
    interpolates a command/token can never write the sentinel into pytest's JUnit XML or
    console. The full sentinel is replaced FIRST (it CONTAINS the marker), then any
    standalone marker is masked too, covering partial / provenance-clamped echoes."""
    text = repr(value)
    text = text.replace(_CRED_SENTINEL, "«G02-CRED-REDACTED»")
    return text.replace(_CRED_MARKER, "«G02-CRED-REDACTED»")


# A minimal node service (binds `$PORT`) whose ONLY release-relevant declaration is the
# host-owned intent sidecar — so the positional credential in `start_cmd` is the single
# thing under test.
_NODE_FILES: dict[str, bytes] = {
    "server.js": b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n",
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
}

# The bare positional literal credential in the LEADING, MIDDLE, and TRAILING operand
# slot of an otherwise-valid runtime command. Every command heads with a supported
# runtime and every token is a shell-inert literal, so each is ACCEPTED by the runtime
# grammar on the tip (the credential rides a positional slot, never a flag). The trailing
# case is the canonical `npm config set <key> <LITERAL>` auth-token form.
_POSITIONS: list[tuple[str, list[str]]] = [
    ("leading", ["node", _CRED_SENTINEL, "server.js"]),
    ("middle", ["node", "server.js", _CRED_SENTINEL, "worker.js"]),
    ("trailing", ["npm", "config", "set", "//registry.example/:_authToken", _CRED_SENTINEL]),
]
_POSITION_IDS = [case[0] for case in _POSITIONS]
# Position id -> start_cmd. The tests parametrize on the POSITION id, NOT the raw
# credential-bearing command list: pytest reprs every test PARAMETER in each failure's
# funcargs header, so a `start_cmd` parameter would write the sentinel into the JUnit XML
# + console for the proving-red failures regardless of assert-message redaction. Resolved
# as a LOCAL inside each test (locals are shown only under `--showlocals`, which the
# acceptance lanes never pass), the sentinel reaches evidence through no surface. Node IDs
# stay `[leading]`/`[middle]`/`[trailing]`; the driven command values are identical.
_START_CMD_BY_POSITION: dict[str, list[str]] = dict(_POSITIONS)


# ---- harness (real ASGI app + real ProjectStore; only ConfigStore.load is seamed) ----
# `_app_and_store` / `_client` / `_seed_project` / `_cid` are copied VERBATIM from the
# frozen `test_c4_env_build_toolchain_matrix.py`. `_runtime_and_client` is the SAME
# construction, additionally exposing the real ConversationRuntime so a real
# `release_declare` ToolExecutor can be driven — the only seam remains `ConfigStore.load`.


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


def _runtime_and_client(
    store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ConversationRuntime, TestClient, ProjectStore]:
    """The C4 ``_app_and_store`` construction, additionally returning the real
    ``ConversationRuntime`` so a real ``release_declare`` ToolExecutor can be driven
    through ``execute_pi_tool``. Identical build (real ConversationRuntime + real
    ProjectStore over a real on-disk root); the ONLY seam is ``ConfigStore.load`` — the
    runtime resolves the ACTIVE configured projects root through it, so the tool's
    host-owned intent writer lands (or, on a correct rejection, does NOT land) its
    sidecar under ``tmp_path`` where ``_sidecars`` proves it present/absent."""
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
    return runtime, TestClient(create_app(store, runtime=runtime)), ProjectStore(str(tmp_path))


def _cid(make_name: object, prefix: str) -> str:
    """A seeded id in the canonical ``conv_`` namespace the release route requires
    (``^conv_[A-Za-z0-9_-]+$``); its tail varies from the recorded seed."""
    assert callable(make_name)
    return str(make_name(prefix))


def _seed_project(
    ps: ProjectStore,
    store: SqliteEventStore,
    cid: str,
    title: str,
    files: dict[str, bytes],
    *,
    intent: ReleaseIntent | None = None,
    owner_id: str = "local",
) -> None:
    """Seed a real on-disk workspace, manifest, optional host-owned intent sidecar,
    conversation record — then cut a real version so the live tree matches a committed
    ``VersionRecord`` (the C2 source binding is satisfied and env/build lowering is the
    only thing under test)."""
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
    if intent is not None:
        ps.write_release_intent(cid, intent)
    store.create_conversation(cid, owner_id=owner_id, title=title, surface="build")
    cut = ps.cut_version(cid, trigger="closeout")
    assert cut is not None and cut.seq == 1, "precondition: a real version 1 was committed"


def _output_blob(result: ToolResult) -> str:
    """Everything the tool surfaces to a caller/model, concatenated: the human content,
    the typed error code, and the JSON of the structured payload — the corpus a
    no-echo assertion scans."""
    structured_json = json.dumps(result.structured or {}, ensure_ascii=False)
    return "\n".join([result.content, result.error or "", structured_json])


def _sidecars(root: Path) -> list[Path]:
    return sorted(root.rglob("release-intent.json")) if root.exists() else []


async def _declare(
    runtime: ConversationRuntime, store: SqliteEventStore, cid: str, start_cmd: list[str]
) -> ToolResult:
    """Run ``release_declare`` through the REAL runtime tool path: create the build
    conversation, then ``execute_pi_tool`` builds the conversation's real
    ``DefaultToolExecutor`` and runs the real ``ReleaseDeclareTool`` over the real
    ``ReleaseIntent`` / ``check_declaration_argv`` validators. Returns the
    ``ToolResult``."""
    store.create_conversation(cid, owner_id="local")
    runtime.set_surface(cid, "build")
    return await runtime.execute_pi_tool(
        cid,
        ToolCall(
            tool_name="release_declare",
            arguments={"start_cmd": list(start_cmd)},
            call_id="closeout-g02",
        ),
    )


def _bound_overlay_texts(client: TestClient, cid: str) -> dict[str, str]:
    """The full ``path -> text`` map of the project's BOUND
    ``/download?version_seq=&spec_digest=`` zip — but ONLY when ``/release`` assessed a
    self-hostable ``candidate`` (so an overlay is emitted AND a ``spec_digest`` exists to
    bind the download to). A fail-closed ``needs_review`` (the correct post-fix outcome
    for a credential-bearing command) emits no overlay and carries no ``spec_digest``, so
    this returns ``{}`` and the literal is definitionally absent from every entry."""
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
# GAP G02 (declaration boundary) — a bare positional literal credential must be
# REJECTED by `release_declare`, exactly like every secret CLI FLAG form is. RED on
# 581dfbe: the tool ACCEPTS it and persists a sidecar carrying the literal.
# ===========================================================================


@pytest.mark.parametrize("position", _POSITION_IDS, ids=_POSITION_IDS)
@pytest.mark.asyncio
async def test_positional_credential_rejected_at_release_declare(
    position: str,
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """GAP G02 (tool boundary) — RED on 581dfbe.

    A start command carrying a bare POSITIONAL literal credential (leading / middle /
    trailing operand) must be rejected by ``release_declare`` — fail closed with a typed
    error, the credential VALUE never echoed, and ZERO sidecar bytes persisted — exactly
    as a ``--token VALUE`` flag form is (§9.2). On 581dfbe the runtime grammar accepts any
    positional operand, so the declare is ACCEPTED (``success`` is True) and a
    ``release-intent.json`` carrying the literal is persisted — the RED this test pins."""
    start_cmd = _START_CMD_BY_POSITION[position]
    cid = _cid(closeout_name, "conv_g02tool")
    runtime, _client_unused, _ps = _runtime_and_client(_store, tmp_path, monkeypatch)
    result = await _declare(runtime, _store, cid, start_cmd)

    # The credential VALUE is never echoed back (green on baseline: the success text is
    # NAMES-only; the fix must keep any rejection value-free too).
    assert _CRED_MARKER not in _output_blob(result), (
        f"[{position}] the positional literal credential was ECHOED in the tool output "
        "(content/error/structured); a declaration must never surface the offending value."
    )
    # The correct behavior: the declaration is REJECTED (fail closed, typed error). RED on
    # 581dfbe — the positional-operand branch of check_declaration_argv accepts it.
    assert not result.success and result.error, (
        f"[{position}] release_declare ACCEPTED a start command carrying a bare positional "
        f"literal credential ({_redact(start_cmd)}); GAP G02 requires it to fail closed with "
        "a typed error, exactly as a `--token VALUE` flag form is rejected. 581dfbe accepts "
        "any positional operand (its runtime grammar scrutinizes only flags), so the "
        f"credential is over-accepted (success={result.success!r}, error={_redact(result.error)})."
    )
    # A rejected declaration persists NOTHING — no release-intent sidecar carrying the
    # literal anywhere under the configured projects root. RED on 581dfbe (one is written).
    assert not _sidecars(tmp_path), (
        f"[{position}] an accepted positional-credential declaration persisted a "
        "release-intent.json sidecar carrying the literal; a rejection must persist ZERO "
        "bytes (the literal must be ABSENT from the sidecar bytes)."
    )


# ===========================================================================
# GAP G02 (rendering boundary) — even a RAW intent sidecar (bypassing the tool) that
# carries a bare positional literal credential must NOT ship it: the route/detector must
# fail closed so the literal is ABSENT from the /release JSON and every /download zip
# entry. RED on 581dfbe: /release is a self-hostable candidate and the bound /download
# zip ships the literal verbatim in the Dockerfile CMD + release.json.
# ===========================================================================


@pytest.mark.parametrize("position", _POSITION_IDS, ids=_POSITION_IDS)
def test_positional_credential_does_not_ship_through_release_download(
    position: str,
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """GAP G02 (route + bound download) — RED on 581dfbe.

    A host-owned RAW ``release-intent.json`` sidecar carrying a bare positional literal
    credential (planted directly, bypassing the ``release_declare`` gate) must not result
    in that credential shipping. The correct outcome is a fail-closed ``/release``
    (``needs_review``, ``self_host:false``, no bound ``spec_digest``) so no self-host
    overlay is emitted — leaving the literal ABSENT from the ``/release`` JSON and every
    entry of the bound ``/download`` zip. On 581dfbe the detect-path grammar accepts the
    positional operand, ``/release`` is a self-hostable ``candidate``, and the bound
    ``/download`` zip ships the literal verbatim in the emitted ``Dockerfile`` CMD +
    ``release.json`` — the RED this test pins."""
    start_cmd = _START_CMD_BY_POSITION[position]
    cid = _cid(closeout_name, "conv_g02route")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), _NODE_FILES)
    # drive (b): plant the literal via a RAW sidecar, bypassing the tool's declaration
    # gate — host state OUTSIDE the workspace tree, never a mocked reader.
    ps.release_intent_for(cid).write_text(
        json.dumps({"start_cmd": list(start_cmd)}), encoding="utf-8"
    )

    # drive (c): the /release JSON must never surface the literal credential.
    rel = client.get(f"/api/projects/{cid}/release")
    assert rel.status_code == 200, rel.text
    body = rel.json()
    assert isinstance(body, dict)
    assert _CRED_MARKER not in rel.text, (
        f"[{position}] the /release JSON surfaced the positional literal credential; the "
        "release verdict must never echo command values."
    )

    # drive (d): the bound /download zip must not ship the literal in ANY emitted overlay
    # entry (the Dockerfile CMD / compose.yaml / release.json). RED on 581dfbe — the
    # candidate overlay bakes it into the Dockerfile CMD and the release.json start_cmd.
    texts = _bound_overlay_texts(client, cid)
    shipped = sorted(name for name, text in texts.items() if _CRED_MARKER in text)
    assert not shipped, (
        f"[{position}] the bare positional literal credential SHIPS in the emitted "
        f"self-host overlay entries {shipped} of the bound /download zip (581dfbe bakes it "
        f"into the {DOCKERFILE_PATH} CMD and {RELEASE_JSON_PATH} — never redacted, never "
        f"failed closed); GAP G02 requires it ABSENT from {DOCKERFILE_PATH} / "
        f"{COMPOSE_PATH} / {RELEASE_JSON_PATH} and every other zip entry."
    )
    # Reinforcement: the correct verdict for a credential-bearing command is fail-closed —
    # no self-hostable candidate, no bound digest, so no overlay is offered at all. RED on
    # 581dfbe (a self-hostable candidate with a bound spec_digest).
    assert (
        body["assessment"] == "needs_review"
        and body["self_host"] is False
        and body["spec_digest"] is None
    ), (
        f"[{position}] a raw sidecar carrying a positional literal credential assessed as "
        f"assessment={body['assessment']!r}, self_host={body['self_host']!r}, "
        f"spec_digest={body['spec_digest']!r}; GAP G02 requires it to fail closed to "
        "needs_review with no self-host overlay (as the secret CLI flag forms do)."
    )
