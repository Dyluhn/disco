"""WO-5 / WO-C1 tests — `release_declare` host tool + host-owned typed release-intent record.

Proves each acceptance criterion:

1. Valid args (start argv, port_env NAME, health path, required env NAMES) persist a
   `release-intent.json` sidecar OUTSIDE the workspace tree; `read_release_intent`
   round-trips to an EQUAL `ReleaseIntent`.
2. Args carrying an env VALUE (a `NAME=value` smuggle or a value OBJECT) OR a shell
   STRING command (not an argv list) are rejected with a typed error; NOTHING persists.
3. The success text confirms NAMES-only recording (candidate, not a verdict); a bytes
   scan of the sidecar shows no planted value from a rejected attempt, and the rejection
   text never echoes the value.
4. The tool is in the free-form agent/build scope and NOT in the strict AppKit allowlist.
5. Re-declaring overwrites ATOMICALLY via the store's tmp-file + replace helper.

WO-C1: `release_declare` is now an `in_process` HOST tool. It NEVER derives a host
path itself (no `ProjectStore("")` fallback and no sandbox scratch guess); it persists
ONLY through the narrow host-owned intent-writer capability the runtime injects into
`ToolContext`. Absent that capability it fails closed with a typed error and persists
nothing, and its success output carries env NAMES + structural metadata only (no argv,
no sidecar path). The end-to-end resolution of the ACTIVE configured store (custom
root, root-changed, fail-closed matrix) is proven by the frozen closeout matrix
`export_track1_closeout/test_c1_intent_store_custom_root.py`; these unit tests cover
the tool's own contract at the `Tool.run` boundary.
"""

from __future__ import annotations

from pathlib import Path

import disco.tools.projects.store as store_mod
import pytest
from disco.core.llm import ModelExecutionPolicy, OperatingMode
from disco.core.release.spec import (
    LocalResourceProfile,
    ReleaseIntent,
    ResourceDecl,
    ResourceKind,
    ResourceProfiles,
)
from disco.tools.anatomy import ToolContext
from disco.tools.appkit_scope import (
    APPKIT_LIFECYCLE,
    APPKIT_MUTATORS,
    APPKIT_PROBES,
    APPKIT_READ_TOOLS,
    AppKitPhase,
    appkit_allowed_tools,
)
from disco.tools.builtin import build_default_registry
from disco.tools.builtin.release_declare import ReleaseDeclareArgs, ReleaseDeclareTool
from disco.tools.projects import ProjectStore
from disco.tools.registry import AGENT_TOOLS, ARTIFACT_TOOLS, RESEARCH_TOOLS, agent_scope
from disco.tools.release_intent import ReleaseIntentWriter
from disco.tools.secrets import CapabilityBroker
from pydantic import ValidationError


class _BackendSandbox:
    """A minimal SandboxInstance stand-in exposing ONLY ``workspace_path``, in the
    two REAL backend shapes:

    * process (dev) backend — a private ``tempfile.mkdtemp`` scratch path
      ``<mkdtemp>/sbx_XXXX`` (the ONLY backend that returns non-None); it is NOT
      under the host projects root.
    * container backends (gvisor [config default] / local / podman) — ``None`` (the
      host FS is hidden).

    NEITHER shape's ancestry is the projects root, so the tool MUST resolve the
    store independently of it. (The fictitious ``<root>/<cid>/workspace`` layout the
    old test simulated exists in NO backend — that is exactly what masked the bug.)"""

    def __init__(self, workspace_path: str | None) -> None:
        self.workspace_path = workspace_path


def _process_backend_sandbox(tmp_root: Path) -> tuple[_BackendSandbox, Path]:
    """Build a PROCESS-backend-shaped sandbox whose ``workspace_path`` is a real
    ``<mkdtemp>/sbx_XXXX`` scratch path under ``tmp_root`` (matching
    ``ProcessSandboxService``: ``tempfile.mkdtemp(prefix="disco-sbx-") / sbx_<hex>``).
    Returns the sandbox and the mkdtemp PARENT dir — the location the OLD
    ``workspace_path.parent.parent`` guess wrongly treated as the projects root, so a
    test can assert the sidecar NEVER lands under it."""
    mkdtemp_parent = tmp_root / "sbxtmp"
    workspace = mkdtemp_parent / "disco-sbx-ab12cd" / ("sbx_" + "0" * 32)
    workspace.mkdir(parents=True, exist_ok=True)
    return _BackendSandbox(str(workspace)), mkdtemp_parent


def _redirect_store_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectStore:
    """Point ``ProjectStore("")``'s default-root resolution at a tmp dir (never the
    real user home) via ``DISCO_DATA_DIR`` — exactly what the container entrypoint
    sets in production. Returns the store the tool resolves to, so a test can assert
    the sidecar lands where the runtime persists ``manifest.json`` for this cid."""
    monkeypatch.setenv("DISCO_DATA_DIR", str(tmp_path / "data"))
    return ProjectStore("")


def _host_writer(store: ProjectStore) -> ReleaseIntentWriter:
    """A minimal host-owned intent writer that persists under a FIXED ProjectStore —
    the runtime-side capability release_declare now writes through (WO-C1). The real
    runtime closure additionally resolves the ACTIVE configured store and fail-closes
    on invalid root / owner mismatch (proven by the frozen closeout matrix); here we
    only need a store-bound writer to exercise the tool's own persist path."""

    async def _write(conversation_id: str, owner_id: str, intent: ReleaseIntent) -> None:
        store.write_release_intent(conversation_id, intent)

    return _write


def _ctx(sbx: object, cid: str, *, writer: ReleaseIntentWriter | None = None) -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=30,
        capabilities=CapabilityBroker().grant(frozenset()),
        owner_id="owner",
        conversation_id=cid,
        release_intent_writer=writer,
    )


def _sqlite_resource() -> ResourceDecl:
    return ResourceDecl(
        id="db",
        kind=ResourceKind.sqlite,
        persistent_path="data/app.db",
        profiles=ResourceProfiles(
            local=LocalResourceProfile(url="file:/data/app.db", volume="app-data")
        ),
    )


# --- criterion 1: valid persist + equal round-trip, outside the workspace tree -----


def test_store_round_trips_release_intent_equal(tmp_path: Path) -> None:
    store = ProjectStore(str(tmp_path))
    cid = "conv-1"
    intent = ReleaseIntent(
        start_cmd=("node", "server.js"),
        build_cmd=("npm", "ci"),
        port_env="PORT",
        health_path="/healthz",
        required_env=("DATABASE_URL", "STRIPE_API_KEY"),
        resources=(_sqlite_resource(),),
    )
    path = store.write_release_intent(cid, intent)
    assert path.is_file()
    got = store.read_release_intent(cid)
    assert got == intent  # frozen pydantic model → value equality


def test_sidecar_is_outside_workspace_tree(tmp_path: Path) -> None:
    store = ProjectStore(str(tmp_path))
    cid = "conv-outside"
    path = store.write_release_intent(cid, ReleaseIntent(start_cmd=("node", "server.js")))
    # sits directly in <root>/<cid>/, NEXT TO where the manifest lives
    assert path == tmp_path / cid / "release-intent.json"
    assert path.parent == store.manifest_for(cid).parent
    # and is NOT inside the workspace/ subtree
    workspace = store.path_for(cid)
    assert workspace not in path.parents
    assert "workspace" not in path.relative_to(tmp_path).parts


def test_read_release_intent_returns_none_when_absent(tmp_path: Path) -> None:
    assert ProjectStore(str(tmp_path)).read_release_intent("never-declared") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["process", "container"])
async def test_tool_persists_through_injected_writer_regardless_of_sandbox(
    backend: str, tmp_path: Path
) -> None:
    """WO-C1 — the in_process host tool persists ONLY through the INJECTED host-owned
    intent writer, so the sidecar lands in the WRITER's store and reads back equal.

    It never reads ``workspace_path``, so the sandbox backend shape is irrelevant:
    the PROCESS backend (a private ``<mkdtemp>/sbx_XXXX`` scratch path) and the
    CONTAINER backend (``workspace_path is None``) both produce the identical result.
    (The old code guessed the root from ``workspace_path.parent.parent`` on the
    process backend — the WO-C1 bug this removes.)"""
    store = ProjectStore(str(tmp_path / "root"))
    cid = f"conv-{backend}"
    if backend == "process":
        sbx: object = _process_backend_sandbox(tmp_path)[0]
    else:
        sbx = _BackendSandbox(None)  # container backend: no host FS handle
    out = await ReleaseDeclareTool().run(
        ReleaseDeclareArgs(
            start_cmd=["node", "server.js"],
            build_cmd=["npm", "ci"],
            port_env="PORT",
            health_path="/healthz",
            required_env=["DATABASE_URL", "STRIPE_API_KEY"],
        ),
        _ctx(sbx, cid, writer=_host_writer(store)),
    )
    assert out.success, out.content
    # criterion 3: names-only + candidate-not-verdict framing surfaced to the model
    assert "NAMES only" in out.content
    assert "candidate input to release detection" in out.content.lower()
    assert out.structured is not None
    assert out.structured["is_verification_claim"] is False
    # WO-C1 crit 8: env NAMES + structural metadata only — no argv, no sidecar path
    assert "sidecar_path" not in out.structured
    assert "start_cmd" not in out.structured and "build_cmd" not in out.structured
    assert out.structured["required_env"] == ["DATABASE_URL", "STRIPE_API_KEY"]
    assert out.structured["port_env"] == "PORT"
    # persisted through the WRITER's store and readable back through it
    got = store.read_release_intent(cid)
    assert got is not None
    assert got.start_cmd == ("node", "server.js")
    assert got.required_env == ("DATABASE_URL", "STRIPE_API_KEY")
    assert got.port_env == "PORT"
    assert got.health_path == "/healthz"


@pytest.mark.asyncio
async def test_tool_fails_closed_without_host_writer() -> None:
    """WO-C1 — with NO host-owned intent writer in context (a standalone executor, or
    any run the runtime did not wire), the tool fails closed with a typed error and
    persists nothing: it has no fallback store and never derives a host path itself."""
    out = await ReleaseDeclareTool().run(
        ReleaseDeclareArgs(start_cmd=["node", "server.js"], required_env=["DATABASE_URL"]),
        _ctx(_BackendSandbox(None), "conv-nocap"),  # writer defaults to None
    )
    assert not out.success
    assert out.error == "intent_writer_unavailable"
    assert out.structured is None


# --- criterion 2 + 3: rejections persist nothing and never leak a value ------------


@pytest.mark.asyncio
async def test_tool_rejects_env_value_and_persists_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _redirect_store_root(tmp_path, monkeypatch)
    cid = "conv-reject-env"
    sbx, _ = _process_backend_sandbox(tmp_path)
    sentinel = "plantedsecretvalue987"
    out = await ReleaseDeclareTool().run(
        ReleaseDeclareArgs(start_cmd=["node", "server.js"], required_env=[f"TOKEN={sentinel}"]),
        _ctx(sbx, cid),
    )
    assert not out.success
    assert out.error == "invalid_release_intent"
    # the rejection names the offending field but NEVER echoes the smuggled value
    assert sentinel not in out.content
    assert "required_env" in out.content
    # NOTHING persisted — at the STORE's real resolved location
    assert store.read_release_intent(cid) is None
    assert not store.release_intent_for(cid).exists()


@pytest.mark.asyncio
async def test_rejected_value_never_reaches_the_sidecar_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _redirect_store_root(tmp_path, monkeypatch)
    cid = "conv-bytes"
    sbx, _ = _process_backend_sandbox(tmp_path)
    tool = ReleaseDeclareTool()
    ctx = _ctx(sbx, cid, writer=_host_writer(store))
    # a valid declaration writes the sidecar (at the store's resolved location)
    ok = await tool.run(
        ReleaseDeclareArgs(start_cmd=["node", "server.js"], required_env=["DATABASE_URL"]), ctx
    )
    assert ok.success
    sidecar = store.release_intent_for(cid)
    assert sidecar.is_file()
    # a value-bearing attempt is rejected...
    sentinel = "leakedvalue_ABC123"
    bad = await tool.run(
        ReleaseDeclareArgs(start_cmd=["node", "server.js"], required_env=[f"SECRET={sentinel}"]),
        ctx,
    )
    assert not bad.success
    # ...and no planted value (or its smuggled name) ever landed on disk
    raw = sidecar.read_bytes()
    assert sentinel.encode() not in raw
    assert b"SECRET" not in raw


@pytest.mark.asyncio
async def test_tool_rejects_env_assignment_argv_and_persists_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AUDIT #4a: an argv element shaped like `API_TOKEN=hunter2` smuggles a secret
    # VALUE through a start-command token. It must be rejected with a typed error and
    # NOTHING may persist; the rejection must not echo the smuggled value.
    store = _redirect_store_root(tmp_path, monkeypatch)
    cid = "conv-argv-smuggle"
    sbx, _ = _process_backend_sandbox(tmp_path)
    out = await ReleaseDeclareTool().run(
        ReleaseDeclareArgs(start_cmd=["API_TOKEN=hunter2", "node", "server.js"]),
        _ctx(sbx, cid),
    )
    assert not out.success
    assert out.error == "invalid_release_intent"
    assert "hunter2" not in out.content
    assert store.read_release_intent(cid) is None
    assert not store.release_intent_for(cid).exists()


@pytest.mark.asyncio
async def test_tool_rejects_env_assignment_in_build_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the same rejection applies to the build argv, not just start.
    store = _redirect_store_root(tmp_path, monkeypatch)
    cid = "conv-argv-build"
    sbx, _ = _process_backend_sandbox(tmp_path)
    out = await ReleaseDeclareTool().run(
        ReleaseDeclareArgs(
            start_cmd=["node", "server.js"], build_cmd=["SECRET_KEY=xyz", "npm", "run", "build"]
        ),
        _ctx(sbx, cid),
    )
    assert not out.success
    assert out.error == "invalid_release_intent"
    assert "xyz" not in out.content
    assert not store.release_intent_for(cid).exists()


def test_shell_string_command_is_structurally_rejected() -> None:
    # a shell STRING (not an argv list) cannot even be expressed in the schema.
    # model_validate takes Any, so this probe stays type-clean (no suppression comment).
    with pytest.raises(ValidationError):
        ReleaseDeclareArgs.model_validate({"start_cmd": "npm run start"})


def test_env_value_object_and_unknown_field_are_structurally_rejected() -> None:
    # the literal criterion example: an `env` blob carrying a VALUE — unknown field
    with pytest.raises(ValidationError):
        ReleaseDeclareArgs.model_validate({"env": [{"name": "TOKEN", "value": "x"}]})
    # a value OBJECT smuggled into required_env — items must be bare NAME strings
    with pytest.raises(ValidationError):
        ReleaseDeclareArgs.model_validate({"required_env": [{"name": "TOKEN", "value": "x"}]})


def test_required_env_items_are_typed_name_strings() -> None:
    schema = ReleaseDeclareArgs.model_json_schema()
    assert schema["properties"]["required_env"]["items"] == {"type": "string"}


@pytest.mark.asyncio
async def test_malformed_env_name_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _redirect_store_root(tmp_path, monkeypatch)
    cid = "conv-lower"
    sbx, _ = _process_backend_sandbox(tmp_path)
    out = await ReleaseDeclareTool().run(
        ReleaseDeclareArgs(start_cmd=["node", "s.js"], required_env=["database_url"]),
        _ctx(sbx, cid),
    )
    assert not out.success and out.error == "invalid_release_intent"
    assert not store.release_intent_for(cid).exists()


# --- criterion 5: atomic overwrite (tmp-file + replace) ----------------------------


def test_redeclare_overwrites_and_leaves_no_temp_litter(tmp_path: Path) -> None:
    store = ProjectStore(str(tmp_path))
    cid = "conv-overwrite"
    store.write_release_intent(
        cid, ReleaseIntent(start_cmd=("node", "a.js"), required_env=("FOO",))
    )
    second = ReleaseIntent(start_cmd=("python", "b.py"), required_env=("BAR",))
    store.write_release_intent(cid, second)
    assert store.read_release_intent(cid) == second
    assert not (tmp_path / cid / "release-intent.json.tmp").exists()
    assert {p.name for p in (tmp_path / cid).iterdir()} == {"release-intent.json"}


def test_write_release_intent_delegates_to_atomic_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Path] = []
    real = store_mod._write_json_atomic

    def spy(path: Path, payload: object) -> None:
        seen.append(path)
        real(path, payload)

    monkeypatch.setattr(store_mod, "_write_json_atomic", spy)
    store = ProjectStore(str(tmp_path))
    out_path = store.write_release_intent("cidx", ReleaseIntent(start_cmd=("node", "s.js")))
    # persisted through the SAME tmp-file + os.replace helper the manifest/versions use
    assert seen == [out_path]
    assert out_path.name == "release-intent.json"


# --- criterion 4: scope membership -------------------------------------------------


def test_registered_in_agent_build_scope_not_appkit() -> None:
    # in the catalogue (so the tool-schema gate scans it) ...
    assert "release_declare" in build_default_registry().names()
    # ... and in the free-form Build/Agent security allowlist
    assert "release_declare" in AGENT_TOOLS
    assert (
        "release_declare" in agent_scope(model_policy=ModelExecutionPolicy.standard()).allowed_tools
    )
    # NOT in the strict AppKit allowlist — for ANY loop_mode / phase / autonomous combo
    for loop_mode in [None, *list(OperatingMode)]:
        for phase in AppKitPhase:
            for autonomous in (False, True):
                allowed = appkit_allowed_tools(
                    loop_mode=loop_mode, phase=phase, autonomous=autonomous
                )
                assert "release_declare" not in allowed
    # nor in the AppKit component frozensets
    assert "release_declare" not in (
        APPKIT_READ_TOOLS | APPKIT_MUTATORS | APPKIT_PROBES | APPKIT_LIFECYCLE
    )
    # nor in the narrower artifact / research scopes
    assert "release_declare" not in ARTIFACT_TOOLS
    assert "release_declare" not in RESEARCH_TOOLS


def test_no_env_value_field_exists_on_the_schema() -> None:
    # Defense in depth: the R2/G03 intent contract EXPANDS the declarable fields (an
    # explicit runtime strategy, an install argv, the package manager / lockfile, the
    # static output_dir, and scoped `env` declarations) — but every one stays
    # NAMES-ONLY / argv-LIST / a typed shape, so there is still NO place to put a secret
    # VALUE. `env` is a list of typed `EnvVarDecl` (names + scope/requiredness/secret
    # class), which is value-free BY CONSTRUCTION (no `value` field), NOT a
    # `{"NAME": "value"}` blob.
    schema = ReleaseDeclareArgs.model_json_schema()
    props = schema["properties"]
    assert set(props) == {
        "runtime",
        "start_cmd",
        "build_cmd",
        "install_cmd",
        "package_manager",
        "lockfile",
        "output_dir",
        "port_env",
        "health_path",
        "required_env",
        "env",
        "resources",
    }
    # `env` items are the typed, value-free EnvVarDecl — never a free-form value map.
    assert props["env"].get("items") == {"$ref": "#/$defs/EnvVarDecl"}
    env_decl_props = set(schema["$defs"]["EnvVarDecl"]["properties"])
    assert "value" not in env_decl_props, "an EnvVarDecl must record a NAME, never a value"
    assert env_decl_props == {"name", "scope", "required", "secret", "binding", "consumers"}
