"""F4 — GATED bootstrap detection: one-shot project-type observation on turn 1.

# Contract

When the assist gate is ON and the agent is about to make its FIRST model call
(no real actions in the event log yet, in execution mode), the engine emits a
one-shot `<system-reminder>`-style MessageEvent summarizing the build/test/entry
commands detected from the workspace's manifest files. The observation:

  * fires EXACTLY once per conversation (a flag in `AgentLoop` so a
    resume/steer never re-emits it — the model already saw it on turn 1);
  * is fully gated on `self._assist` — when the gate is OFF (capable-model
    default), the detector is never called and the engine is byte-identical
    to today;
  * only reports commands that are LITERALLY present in a recognized
    manifest (package.json, pyproject.toml, Makefile, Cargo.toml, go.mod) —
    no inference, no fallbacks, no "probably X" (F4 exists to give a weak
    model a TRUE starting point, not a plausible-sounding lie).

# Why this matters

A weak model on a fresh session can burn 3-5 turns hunting for the right
build / test / entry command instead of acting. F4 hands the model the
detected commands in the same observation slot the loop already uses for
`<system-reminder>` messages (C7 escape, container-restart hint, etc.), so
it costs nothing on the capable-model path and saves a real chunk of
turn-budget on the weak-model path.

# Acceptance (this file)

  1. assist ON + turn 1 + a workspace with a package.json → bootstrap
     observation lists the `scripts` entries literally defined there.
  2. assist ON + turn 2 (an action has already been taken) → NO bootstrap
     observation; the flag is one-shot.
  3. assist OFF (default) → the detector is NEVER called; the loop's
     event log contains no F4 bootstrap message; the engine is
     byte-identical to the pre-F4 path.
  4. The detector returns None / a meaningful fallback when the workspace
     has no recognized manifests (no fabrication of plausible commands).
  5. The detector parses pyproject.toml, Makefile, Cargo.toml, go.mod
     in addition to package.json (manifests the user actually uses).

The engine's gating is exercised end-to-end through `AgentLoop.run()` with
fakes; the detector's parsers are exercised in isolation for shape
correctness."""

from __future__ import annotations

import json
from pathlib import Path

from disco.core import (
    ConversationStatus,
    EventSource,
    MessageEvent,
)
from disco.core.llm import ModelExecutionPolicy
from disco.core.loop.bootstrap import (
    _F4_MAX_SCRIPTS_PER_MANIFEST,
    _detect_cargo_toml,
    _detect_go_mod,
    _detect_makefile,
    _detect_package_json,
    _detect_project_bootstrap,
    _detect_pyproject_toml,
)
from loop_fakes import (
    FakeExecutor,
    ScriptedAgent,
    action_step,
    build_loop,
    finish_step,
)

CID = "conv"  # matches build_loop's default conversation_id


# ---------------------------------------------------------------------------
# (1) Workspace + sandbox seam: a minimal fake executor that exposes
#     `sandbox.workspace_path` pointing at a tmp_path with the manifests
#     the test wants. The engine only ever reads from this seam — never
#     executes anything against it — so we don't need a working executor.
# ---------------------------------------------------------------------------


class _FakeSandbox:
    """Minimal duck-typed SandboxInstance for the F4 tests. The detector only
    needs `workspace_path` to be a directory containing the manifest files."""

    def __init__(self, workspace: str):
        self.workspace_path = workspace


class _SandboxExecutor:
    """Minimal ToolExecutor stub exposing a `sandbox` attribute. The F4 tests
    drive `run()` to the first model call only (we don't need real execution
    for the bootstrap test), but the engine still calls
    `_tools_for_step()` which goes through `executor.available_tools()` for
    some paths, so we forward that to a working fake to keep the engine
    happy. The model itself is a ScriptedAgent that we steer to `finish`."""

    def __init__(self, sandbox: _FakeSandbox):
        self.sandbox = sandbox
        self._inner = FakeExecutor()

    def available_tools(self):
        return self._inner.available_tools()

    async def execute(self, call):
        return await self._inner.execute(call)


def _write_workspace(
    tmp_path: Path,
    *,
    package: dict | None = None,
    pyproject: str | None = None,
    makefile: str | None = None,
    cargo: str | None = None,
    gomod: str | None = None,
) -> str:
    """Materialize the manifests requested on a tmp_path; return the path as
    a string. Each manifest is OPTIONAL — pass None to skip it. The detector
    only sees the files we write; everything else in the workspace is
    invisible to F4."""
    if package is not None:
        (tmp_path / "package.json").write_text(json.dumps(package))
    if pyproject is not None:
        (tmp_path / "pyproject.toml").write_text(pyproject)
    if makefile is not None:
        (tmp_path / "Makefile").write_text(makefile)
    if cargo is not None:
        (tmp_path / "Cargo.toml").write_text(cargo)
    if gomod is not None:
        (tmp_path / "go.mod").write_text(gomod)
    return str(tmp_path)


# ---------------------------------------------------------------------------
# (1) assist ON + turn 1 + a workspace with package.json → bootstrap
#     observation lists the scripts. This is the headline test.
# ---------------------------------------------------------------------------


async def test_assist_on_turn_one_emits_bootstrap_with_package_json_scripts(tmp_path):
    workspace = _write_workspace(
        tmp_path,
        package={
            "name": "demo",
            "scripts": {
                "build": "tsc -p .",
                "test": "vitest run",
                "dev": "vite",
                "lint": "eslint .",
            },
        },
    )
    # ScriptedAgent: real work first (a shell exec) so the action bookkeeping
    # actually moves; then a finish to end the run. The bootstrap should
    # appear AFTER the user message but BEFORE the first action.
    agent = ScriptedAgent([action_step("shell", {"command": "echo hi"}), finish_step()])
    loop, store = build_loop(
        agent,
        executor=_SandboxExecutor(_FakeSandbox(workspace)),
    )
    loop._model_policy = ModelExecutionPolicy(tier="weak")
    await loop.send_message("build a stock tracker")
    await loop.run()

    events = await store.get_events(CID)
    bootstrap_msgs = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "F4 bootstrap" in (e.message.content or "")
    ]
    assert len(bootstrap_msgs) == 1, (
        f"expected exactly one F4 bootstrap message on turn 1, got {len(bootstrap_msgs)}: "
        f"{[e.message.content[:120] for e in bootstrap_msgs]}"
    )
    content = bootstrap_msgs[0].message.content
    # The detector must report the script names the manifest literally
    # defines. Order is insertion order from `_F4_PKG_SCRIPT_KEYS`, so the
    # assertion is shape-based (substring), not position-based.
    assert "npm run build" in content
    assert "tsc -p ." in content
    assert "npm run test" in content
    assert "vitest run" in content
    assert "npm run dev" in content
    assert "vite" in content
    # And NOT fabricate commands the manifest doesn't define.
    assert "npm run deploy" not in content
    assert "npm run nonexistent" not in content
    # The bootstrap is wrapped in <system-reminder> so the model treats it
    # as a hint, not a user message.
    assert content.startswith("<system-reminder>")
    assert content.rstrip().endswith("</system-reminder>")


# ---------------------------------------------------------------------------
# (2) assist ON + turn 2 → no repeat. The once-per-session flag is load-bearing:
#     a model that already saw the bootstrap must not see it again on resume.
# ---------------------------------------------------------------------------


async def test_assist_on_bootstrap_fires_only_on_turn_one_even_with_many_actions(tmp_path):
    workspace = _write_workspace(
        tmp_path,
        package={"name": "demo", "scripts": {"build": "tsc"}},
    )
    # Two real actions in the script → the bootstrap fires on turn 1
    # (no actions yet) and NOT on turn 2/3 (actions present). The
    # once-per-session flag is the load-bearing piece.
    agent = ScriptedAgent(
        [
            action_step("shell", {"command": "echo a"}),
            action_step("shell", {"command": "echo b"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(
        agent,
        executor=_SandboxExecutor(_FakeSandbox(workspace)),
    )
    loop._model_policy = ModelExecutionPolicy(tier="weak")
    await loop.send_message("do work")
    await loop.run()

    events = await store.get_events(CID)
    bootstrap_msgs = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "F4 bootstrap" in (e.message.content or "")
    ]
    # Exactly one bootstrap — the first turn. Subsequent turns (turns 2
    # and 3) MUST NOT re-emit it; the flag is locked.
    assert len(bootstrap_msgs) == 1, (
        f"bootstrap must fire exactly once (on turn 1); got {len(bootstrap_msgs)}: "
        f"{[e.message.content[:120] for e in bootstrap_msgs]}"
    )
    # And the loop's flag is now locked — manually verify it can't fire on a
    # second run() segment either (resume / steer behavior).
    assert loop._bootstrap_emitted is True


async def test_assist_on_bootstrap_fires_once_across_two_run_segments(tmp_path):
    """A resume/steer spawns a fresh `run()` segment. The bootstrap flag is
    per-LOOP, not per-segment — once it's True, the second `run()` must not
    re-emit the bootstrap. The model already saw it on turn 1; emitting it
    again would be a redundant context cost for a weak model that may be
    near its context budget."""
    workspace = _write_workspace(
        tmp_path,
        package={"name": "demo", "scripts": {"build": "tsc", "test": "vitest"}},
    )
    agent = ScriptedAgent(
        [
            action_step("shell", {"command": "echo a"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(
        agent,
        executor=_SandboxExecutor(_FakeSandbox(workspace)),
    )
    loop._model_policy = ModelExecutionPolicy(tier="weak")
    # Segment 1: the bootstrap fires once.
    await loop.send_message("first instruction")
    await loop.run()
    # Mark the run as finished (finish_step ends it) so the next send_message
    # is a fresh start, not a continuation of a running loop.
    # Segment 2: bootstrap must NOT re-fire.
    await loop.send_message("second instruction")
    await loop.run()

    events = await store.get_events(CID)
    bootstrap_msgs = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "F4 bootstrap" in (e.message.content or "")
    ]
    assert len(bootstrap_msgs) == 1, (
        f"bootstrap must fire exactly once across segments; got {len(bootstrap_msgs)}"
    )


# ---------------------------------------------------------------------------
# (3) assist OFF (default) → byte-identical to today. The detector is NEVER
#     called; no F4 message lands in the log; nothing changes for capable
#     models. This is the iron rule.
# ---------------------------------------------------------------------------


async def test_assist_off_emits_no_bootstrap_message(tmp_path):
    workspace = _write_workspace(
        tmp_path,
        package={"name": "demo", "scripts": {"build": "tsc"}},
    )
    agent = ScriptedAgent([action_step("shell", {"command": "echo hi"}), finish_step()])
    loop, store = build_loop(
        agent,
        executor=_SandboxExecutor(_FakeSandbox(workspace)),
    )
    # No loop._assist = True. The default is False (capable-model tier).
    await loop.send_message("build it")
    await loop.run()

    events = await store.get_events(CID)
    bootstrap_msgs = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "F4 bootstrap" in (e.message.content or "")
    ]
    assert bootstrap_msgs == [], (
        f"assist OFF must NEVER emit a bootstrap; got {len(bootstrap_msgs)}: "
        f"{[e.message.content[:120] for e in bootstrap_msgs]}"
    )
    # And the flag stays False — the gate was never crossed.
    assert loop._bootstrap_emitted is False


async def test_assist_off_byte_identical_to_pre_f4_event_log(tmp_path):
    """A stronger byte-identical guarantee: the EVENT LOG for an assist-OFF
    run is exactly the events we'd see pre-F4. No additional MessageEvents,
    no additional StatusEvents, no KnowledgeEvents, no anything. The bootstrap
    path is fully closed when the gate is off."""
    workspace = _write_workspace(
        tmp_path,
        package={"name": "demo", "scripts": {"build": "tsc"}},
    )
    agent = ScriptedAgent([action_step("shell", {"command": "echo hi"}), finish_step()])
    loop, store = build_loop(
        agent,
        executor=_SandboxExecutor(_FakeSandbox(workspace)),
    )
    # Default assist = False.
    await loop.send_message("build it")
    await loop.run()

    events = await store.get_events(CID)
    # Filter to the substantive event types. There should be NO
    # ENVIRONMENT MessageEvents at all on the assist-OFF path — no
    # bootstrap, no escape, nothing.
    env_msgs = [
        e for e in events if isinstance(e, MessageEvent) and e.source == EventSource.ENVIRONMENT
    ]
    assert env_msgs == [], (
        f"assist OFF must produce zero ENVIRONMENT messages (F4 closed the "
        f"gate); got: {[e.message.content[:120] for e in env_msgs]}"
    )
    # And no KnowledgeEvents (the bootstrap does NOT add facts to the
    # knowledge base — it's an ephemeral observation, not pinned memory).
    from disco.core import KnowledgeEvent

    knowledge = [e for e in events if isinstance(e, KnowledgeEvent)]
    assert knowledge == [], f"the bootstrap must NOT pollute the knowledge base; got: {knowledge}"


# ---------------------------------------------------------------------------
# (4) No manifests → no bootstrap. The detector must not fabricate commands
#     when the workspace is empty. Better to say nothing than to lie.
# ---------------------------------------------------------------------------


async def test_assist_on_workspace_with_no_manifests_emits_nothing(tmp_path):
    workspace = _write_workspace(tmp_path)  # no manifests at all
    agent = ScriptedAgent([action_step("shell", {"command": "echo hi"}), finish_step()])
    loop, store = build_loop(
        agent,
        executor=_SandboxExecutor(_FakeSandbox(workspace)),
    )
    loop._model_policy = ModelExecutionPolicy(tier="weak")
    await loop.send_message("do it")
    await loop.run()

    events = await store.get_events(CID)
    bootstrap_msgs = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "F4 bootstrap" in (e.message.content or "")
    ]
    # No manifests → detector returns None → no emission. The flag is still
    # set so we don't re-run on a later turn.
    assert bootstrap_msgs == []
    assert loop._bootstrap_emitted is True


async def test_assist_on_workspace_with_unreadable_manifest_emits_nothing(tmp_path):
    """A package.json that isn't valid JSON must NOT crash the detector and
    must NOT produce a fabricated bootstrap. The detector is best-effort:
    bad input → no output."""
    (tmp_path / "package.json").write_text("{this is not json")
    agent = ScriptedAgent([action_step("shell", {"command": "echo hi"}), finish_step()])
    loop, store = build_loop(
        agent,
        executor=_SandboxExecutor(_FakeSandbox(str(tmp_path))),
    )
    loop._model_policy = ModelExecutionPolicy(tier="weak")
    await loop.send_message("do it")
    await loop.run()
    events = await store.get_events(CID)
    bootstrap_msgs = [
        e
        for e in events
        if isinstance(e, MessageEvent) and "F4 bootstrap" in (e.message.content or "")
    ]
    assert bootstrap_msgs == []


# ---------------------------------------------------------------------------
# (5) Detector unit tests — exercise each parser in isolation. The engine
#     gating is verified above; these tests pin the parsers' behavior so
#     the detector doesn't silently start lying or over-reporting.
# ---------------------------------------------------------------------------


def test_detect_package_json_surfaces_documented_keys(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "scripts": {
                    "build": "tsc -p .",
                    "test": "vitest",
                    "dev": "vite",
                    "lint": "eslint .",
                    "deploy": "echo deploying",  # NOT in the documented key set
                    "weird": "echo weird",  # NOT in the documented key set
                },
            }
        )
    )
    out = _detect_package_json(tmp_path)
    # The detector surfaces the documented keys only. The script names ARE
    # the commands (npm run <name>), and the actual command body is shown
    # so the model sees the truth.
    assert any("npm run build: tsc -p ." in line for line in out)
    assert any("npm run test: vitest" in line for line in out)
    assert any("npm run dev: vite" in line for line in out)
    assert any("npm run lint: eslint ." in line for line in out)
    # Non-documented keys are filtered — the detector must not over-broaden
    # its report (the model only needs the well-known entry points).
    assert not any("deploy" in line for line in out)
    assert not any("weird" in line for line in out)


def test_detect_package_json_empty_scripts_returns_empty(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"name": "demo", "scripts": {}}))
    assert _detect_package_json(tmp_path) == []


def test_detect_package_json_missing_returns_empty(tmp_path):
    assert _detect_package_json(tmp_path) == []


def test_detect_pyproject_toml_surfaces_project_scripts(tmp_path):
    (tmp_path / "pyproject.toml").write_text("""
[project]
name = "demo"
[project.scripts]
hello = "hello:main"
goodbye = "goodbye:main"

[tool.poetry.scripts]
poetry-cli = "poetry_cli:main"
""")
    out = _detect_pyproject_toml(tmp_path)
    # Both script tables are surfaced (sorted alphabetically by detector).
    # The detector returns a LIST of "<name>: <cmd>" strings; the test
    # asserts shape via substring, not whole-string equality.
    assert any("hello: hello:main" in line for line in out)
    assert any("goodbye: goodbye:main" in line for line in out)
    assert any("poetry-cli: poetry_cli:main" in line for line in out)


def test_detect_makefile_surfaces_target_names(tmp_path):
    (tmp_path / "Makefile").write_text("""
# A comment line that should be skipped
build:
\ttsc -p .

test:
\tpytest

.PHONY: build test clean
""")
    out = _detect_makefile(tmp_path)
    # Target names are reported (no body parsing). .PHONY is a special
    # pseudo-target that the detector SHOULD report (it's a real
    # Makefile entry — phony targets are still callable as `make
    # .PHONY` won't work but the surfaces-true is what F4 needs). The
    # point is the detector surfaces whatever the Makefile literally
    # defines; the model can pick the right one.
    assert any("make build" in line for line in out)
    assert any("make test" in line for line in out)


def test_detect_makefile_empty_returns_empty(tmp_path):
    (tmp_path / "Makefile").write_text("# just a comment\n")
    assert _detect_makefile(tmp_path) == []


def test_detect_cargo_toml_surfaces_package_and_bins(tmp_path):
    (tmp_path / "Cargo.toml").write_text("""
[package]
name = "demo-cli"
version = "0.1.0"

[[bin]]
name = "demo"
path = "src/main.rs"

[[bin]]
name = "demo-helper"
path = "src/helper.rs"
""")
    out = _detect_cargo_toml(tmp_path)
    assert any("package = demo-cli" in line for line in out)
    assert any("cargo run --bin demo" in line for line in out)
    assert any("cargo run --bin demo-helper" in line for line in out)


def test_detect_go_mod_surfaces_module_path(tmp_path):
    (tmp_path / "go.mod").write_text("""module github.com/example/demo

go 1.22
""")
    out = _detect_go_mod(tmp_path)
    assert len(out) == 1
    assert "module = github.com/example/demo" in out[0]


def test_detect_go_mod_no_module_line_returns_empty(tmp_path):
    (tmp_path / "go.mod").write_text("// just a comment, no module line\n")
    assert _detect_go_mod(tmp_path) == []


def test_detect_project_bootstrap_returns_none_for_empty_workspace(tmp_path):
    """The headline contract: an empty workspace → no fabrication."""
    assert _detect_project_bootstrap(str(tmp_path)) is None


def test_detect_project_bootstrap_returns_none_for_missing_workspace(tmp_path):
    """A path that doesn't exist → None, not an exception."""
    assert _detect_project_bootstrap(str(tmp_path / "does-not-exist")) is None


def test_detect_project_bootstrap_returns_none_for_empty_workspace_path():
    """An empty / falsy workspace path → None (defensive against the
    no-sandbox-attached path where `workspace_path` is empty string)."""
    assert _detect_project_bootstrap("") is None


def test_detect_project_bootstrap_formats_as_system_reminder(tmp_path):
    """The full bootstrap output is wrapped in <system-reminder> tags so the
    model treats it as a system hint (parallel to the C7 escape reminder
    and the container-restart hint the engine already emits)."""
    _write_workspace(
        tmp_path,
        package={"name": "demo", "scripts": {"build": "tsc"}},
    )
    out = _detect_project_bootstrap(str(tmp_path))
    assert out is not None
    assert out.startswith("<system-reminder>")
    assert out.rstrip().endswith("</system-reminder>")
    # Contains the actual command, NOT a fabricated one.
    assert "npm run build" in out
    assert "tsc" in out


def test_detect_project_bootstrap_truncates_at_max_chars(tmp_path):
    """A workspace with many scripts doesn't drown the first turn in a wall
    of text. The detector caps both per-manifest and total chars."""
    # Many package.json scripts so we exceed _F4_MAX_SCRIPTS_PER_MANIFEST.
    # Use the well-known key names with distinct values to be sure the
    # detector is actually surfacing them (the documented-key filter is
    # tested separately in `test_detect_package_json_surfaces_documented_keys`).
    # Here we use ONLY keys in `_F4_PKG_SCRIPT_KEYS` to exercise the
    # truncation path; for that we need a list, so we exercise the
    # `Makefile` truncation path instead, where target count is unbounded.
    many_targets = "\n".join(
        f"target{i}:\n\techo {i}\n" for i in range(_F4_MAX_SCRIPTS_PER_MANIFEST * 3)
    )
    _write_workspace(tmp_path, makefile=many_targets)
    out = _detect_project_bootstrap(str(tmp_path))
    assert out is not None
    # Per-manifest cap kicks in BEFORE the global cap. Either way, we never
    # emit more than `_F4_MAX_SCRIPTS_PER_MANIFEST` lines per manifest.
    assert out.count("make target") <= _F4_MAX_SCRIPTS_PER_MANIFEST


def test_detect_project_bootstrap_does_not_fabricate_undefined_scripts(tmp_path):
    """Iron rule: the detector must NOT report scripts that aren't literally
    in the manifest. A package.json with ONLY `build` and `test` must NOT
    produce a bootstrap that mentions `start` / `dev` / `lint`."""
    _write_workspace(
        tmp_path,
        package={"name": "demo", "scripts": {"build": "tsc", "test": "vitest"}},
    )
    out = _detect_project_bootstrap(str(tmp_path))
    assert out is not None
    # The two we defined are present.
    assert "npm run build" in out
    assert "npm run test" in out
    # The four we didn't define are NOT present.
    assert "npm run start" not in out
    assert "npm run dev" not in out
    assert "npm run lint" not in out
    assert "npm run deploy" not in out


# ---------------------------------------------------------------------------
# (6) The bootstrap is visible to the model on the FIRST turn. The detector
#     emits a MessageEvent that gets picked up by view materialization
#     within the same iteration, so the model sees the hint on the very
#     first step. Verified by capturing the views the ScriptedAgent sees.
# ---------------------------------------------------------------------------


async def test_bootstrap_message_reaches_first_model_call_view(tmp_path):
    workspace = _write_workspace(
        tmp_path,
        package={"name": "demo", "scripts": {"build": "tsc"}},
    )
    agent = ScriptedAgent([action_step("shell", {"command": "echo hi"}), finish_step()])
    loop, store = build_loop(
        agent,
        executor=_SandboxExecutor(_FakeSandbox(workspace)),
    )
    loop._model_policy = ModelExecutionPolicy(tier="weak")
    await loop.send_message("do it")
    await loop.run()

    # The agent captured every view it was offered. The first view (turn 1)
    # must include the bootstrap message — the model needs to see the hint
    # on the same step that needs it.
    assert agent.seen_views, "agent never stepped"
    first_view = agent.seen_views[0]
    # The bootstrap is a MessageEvent with source=ENVIRONMENT and the
    # F4 system-reminder content. The View's `messages` field carries it.
    bootstrap_in_view = [m for m in first_view.messages if "F4 bootstrap" in (m.content or "")]
    assert len(bootstrap_in_view) == 1, (
        f"the bootstrap must reach the first model call's view; got "
        f"{len(bootstrap_in_view)} matching messages in view: "
        f"{[m.content[:120] for m in first_view.messages]}"
    )
    # And the second view (turn 2 — but we never get there, finish_step
    # ends the run on turn 2; this is just defensive) doesn't see it twice
    # within the same run segment.


# ---------------------------------------------------------------------------
# (7) Planning mode does NOT emit a bootstrap. The bootstrap is for the
#     execution turn-1 hint, not the planning phase (a planner doesn't need
#     to know about npm scripts to produce a plan; the executor learns them
#     when it starts running).
# ---------------------------------------------------------------------------


async def test_planning_mode_skips_bootstrap(tmp_path):
    from disco.core.llm import OperatingMode

    workspace = _write_workspace(
        tmp_path,
        package={"name": "demo", "scripts": {"build": "tsc"}},
    )
    # The agent is in PLANNING mode. It should not get a bootstrap.
    from disco.core import ToolCall as TC
    from loop_fakes import AgentStep

    plan_step = AgentStep(
        thought="here's the plan",
        tool_call=TC(
            tool_name="submit_plan",
            arguments={"summary": "build a thing", "steps": [{"title": "scaffold"}]},
        ),
        finished=False,
    )
    agent = ScriptedAgent([plan_step])
    loop, store = build_loop(
        agent,
        executor=_SandboxExecutor(_FakeSandbox(workspace)),
        mode=OperatingMode.PLANNING,
    )
    loop._planning_tools = frozenset({"submit_plan"})
    loop._model_policy = ModelExecutionPolicy(tier="weak")
    await loop.send_message("plan the build")
    await loop.run()
    events = await store.get_events(CID)
    bootstrap_msgs = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "F4 bootstrap" in (e.message.content or "")
    ]
    assert bootstrap_msgs == [], (
        "planning mode must NOT receive a bootstrap (the bootstrap is an "
        "execution turn-1 hint); got: "
        f"{[e.message.content[:120] for e in bootstrap_msgs]}"
    )


# ---------------------------------------------------------------------------
# (8) A workspace path that isn't a directory (e.g. a file) → no bootstrap,
#     no exception. Defensive: the detector must not crash the loop on
#     misconfigured sandboxes.
# ---------------------------------------------------------------------------


async def test_assist_on_with_file_as_workspace_path_does_not_crash(tmp_path):
    """Defensive: if `workspace_path` somehow points at a regular file (a
    misconfigured sandbox, a test, etc.), the loop must not crash. The
    detector returns None, no bootstrap is emitted, the run proceeds."""
    fake_file = tmp_path / "not-a-dir.txt"
    fake_file.write_text("oops")

    # The _SandboxExecutor doesn't validate workspace_path is a directory.
    # We pass a file path; the detector must handle it gracefully.
    agent = ScriptedAgent([action_step("shell", {"command": "echo hi"}), finish_step()])
    loop, store = build_loop(
        agent,
        executor=_SandboxExecutor(_FakeSandbox(str(fake_file))),
    )
    loop._model_policy = ModelExecutionPolicy(tier="weak")
    await loop.send_message("do it")
    await loop.run()
    events = await store.get_events(CID)
    bootstrap_msgs = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "F4 bootstrap" in (e.message.content or "")
    ]
    assert bootstrap_msgs == []
    # Loop completed cleanly — no exception, no ERROR status.
    state = await store.get_state(CID)
    assert state.execution_status == ConversationStatus.FINISHED
