"""Secrets/capabilities (§11.4), egress (§11.5), kill switch (§11.6) — the
security-critical tests. The headline is `test_no_secret_in_the_box`."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from disco.tools import (
    CapabilityBroker,
    CapabilityDenied,
    DefaultToolExecutor,
    InMemorySecretsStore,
    ProcessSandboxService,
    SandboxSpec,
    ToolScope,
    build_default_registry,
    research_scope,
)
from tool_fakes import call

SENTINEL = "SUPER-SECRET-SENTINEL-9F3A2"


def _assert_secret_absent_from_workspace(workspace: Path, secret: str) -> None:
    """Inspect every persisted byte, including host-owned bounded-output spills."""
    needle = secret.encode()
    for path in workspace.rglob("*"):
        if path.is_file():
            relative = path.relative_to(workspace)
            assert needle not in path.read_bytes(), f"secret present in workspace file {relative}"


# ---- §11.4 the headline: no secret is ever in the box -----------------------


# The bounded-exec helper's designed spill names: `<token_hex(8)>.<stream>.log`.
_SPILL_NAME_RE = re.compile(r"[0-9a-f]{16}\.(stdout|stderr)\.log")


async def _assert_box_secret_free(inst, *, allowed: tuple[str, ...] = ()) -> None:
    """The box invariant, stated on the SECRET rather than on emptiness: the only
    content the sandbox machinery may itself create is the designed bounded-exec
    spill artifact (`.disco/spills/` — written when a command's output exceeds the
    128 KiB capture cap, e.g. `ps` on a process-heavy host). Every spill entry must
    carry the DESIGNED name shape (independent verification demonstrated a
    sentinel-bearing FILENAME passed a contents-only scan) and no spill byte may
    carry the sentinel. Anything else in a fresh box — beyond the caller's
    explicitly `allowed` test-authored files — is still a failure."""
    files = await inst.list_dir(".")
    unexpected = [name for name in files if name != ".disco" and name not in allowed]
    assert unexpected == [], f"unexpected content in the box: {unexpected}"
    if ".disco" in files:
        assert await inst.list_dir(".disco") == ["spills"]
        for name in await inst.list_dir(".disco/spills"):
            assert _SPILL_NAME_RE.fullmatch(name), f"non-designed spill entry: {name}"
            data = await inst.read_file(f".disco/spills/{name}")
            assert SENTINEL.encode("utf-8") not in data


async def test_no_secret_in_the_box(tmp_path: Path):
    """A secret injected into the orchestrator's store is absent from the
    sandbox's env, filesystem (including any designed spill artifact), and
    process list."""
    secrets = InMemorySecretsStore({"PROVIDER_KEY": SENTINEL})
    assert secrets.get("PROVIDER_KEY") == SENTINEL  # the orchestrator can read it

    svc = ProcessSandboxService(root=str(tmp_path / "sandbox-root"))
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")
    try:
        # env (clean — never os.environ), process list, and every persisted byte:
        # all secret-free. Large process listings may legitimately create a bounded
        # host-owned .disco/spills file, so inspect it rather than assuming no files.
        env = await inst.exec_shell("env", timeout_s=10)
        procs = await inst.exec_shell("ps -e -o args 2>/dev/null || ps", timeout_s=10)
        assert SENTINEL not in env.stdout
        assert SENTINEL not in procs.stdout  # the bounded stdout projection
        # ALSO scan the FULL process list at FULL FIDELITY, not just the bounded
        # stdout projection: bounded exec returns only a 64 KiB head + tail and
        # spills at most 1 MiB, so a sentinel in the dropped middle of an oversized
        # `ps` would be invisible to a stdout scan (independent verification
        # demonstrated exactly that gap). The scan runs INSIDE the box — `grep`
        # over the captured listing — so only a tiny bounded match count crosses
        # the API. (Reading the whole listing back via read_file is not viable: on
        # a busy host `ps -e -o args` can exceed read_file's 32 MiB transfer cap;
        # grep-in-box has no such limit and closes the same projection gap.)
        ps_capture = await inst.exec_shell(
            "{ ps -e -o args 2>/dev/null || ps; } > .ps-scan.txt; wc -c < .ps-scan.txt",
            timeout_s=10,
        )
        assert ps_capture.exit_code == 0
        assert int(ps_capture.stdout.strip()) > 0  # the listing was actually captured
        # grep exits 0 (found), 1 (clean), or 2 (error). Only 1 is acceptable here.
        ps_scan = await inst.exec_shell(f"grep -F -c -- {SENTINEL!r} .ps-scan.txt", timeout_s=10)
        assert ps_scan.exit_code == 1, (
            f"process-list sentinel scan did not run clean (exit {ps_scan.exit_code}): "
            f"{ps_scan.stdout.strip()} {ps_scan.stderr.strip()}"
        )
        assert ps_scan.stdout.strip() == "0"
        # Every persisted byte — including spills and the test-authored scan file —
        # is secret-free, AND the box structure carries only designed content.
        _assert_secret_absent_from_workspace(inst._workspace, SENTINEL)
        await _assert_box_secret_free(inst, allowed=(".ps-scan.txt",))
    finally:
        await inst.destroy()


def test_secret_absence_oracle_scans_platform_spills(tmp_path: Path) -> None:
    """A platform-managed path is allowed structurally but never exempt from scanning."""
    spill = tmp_path / ".disco" / "spills" / "captured.stdout.log"
    spill.parent.mkdir(parents=True)
    spill.write_text(f"ordinary output\n{SENTINEL}\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="secret present in workspace file"):
        _assert_secret_absent_from_workspace(tmp_path, SENTINEL)


async def test_no_secret_in_the_box_even_when_output_spills(tmp_path: Path):
    """DETERMINISTIC spill branch: force a >128 KiB output so the bounded-exec
    helper writes its designed `.disco/spills/` artifact, then prove the box
    STILL carries no secret byte anywhere — including inside the spill files.
    (The headline test only hits this branch when the host is process-heavy;
    this pins the behavior regardless of load.)"""
    secrets = InMemorySecretsStore({"PROVIDER_KEY": SENTINEL})
    assert secrets.get("PROVIDER_KEY") == SENTINEL

    svc = ProcessSandboxService(root=str(tmp_path / "sandbox-root"))
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")
    try:
        big = await inst.exec_shell("seq 1 400000", timeout_s=30)
        assert big.exit_code == 0
        assert SENTINEL not in big.stdout
        files = await inst.list_dir(".")
        assert files == [".disco"], f"expected the designed spill artifact, got {files}"
        _assert_secret_absent_from_workspace(inst._workspace, SENTINEL)
        await _assert_box_secret_free(inst)
    finally:
        await inst.destroy()


async def test_capability_mediates_without_exposing_the_credential():
    """The search tool reaches a provider via an orchestrator-side handler that
    uses the secret; the tool gets the RESULT, never the key."""
    secrets = InMemorySecretsStore({"SEARCH_KEY": SENTINEL})
    seen_key = {}

    async def search_handler(*, query, limit):
        key = secrets.get("SEARCH_KEY")  # used orchestrator-side, in the closure
        seen_key["used"] = key
        return [f"result for {query!r} (limit {limit})"]

    broker = CapabilityBroker()
    broker.register("search", search_handler)
    ex = DefaultToolExecutor(build_default_registry(), research_scope(), broker=broker)
    res = await ex.execute(call("search", query="quantum", limit=3))
    assert res.success
    assert "quantum" in res.content
    assert SENTINEL not in res.content  # the key never reaches the result/tool
    assert seen_key["used"] == SENTINEL  # but the handler used it orchestrator-side


async def test_denied_capability_is_not_silently_granted():
    """A tool invoking a capability it wasn't granted gets `denied`."""
    # search declares uses_capabilities={"search"} but the broker registers none.
    ex = DefaultToolExecutor(build_default_registry(), research_scope(), broker=CapabilityBroker())
    res = await ex.execute(call("search", query="x"))
    assert res.success is False and res.structured["kind"] == "denied"


async def test_scope_is_the_first_boundary():
    """A research-scope executor refuses a world-affecting tool even though it
    exists in the registry."""
    ex = DefaultToolExecutor(build_default_registry(), research_scope())
    res = await ex.execute(call("shell", command="rm -rf /"))
    assert res.success is False and res.structured["kind"] == "unknown_tool"


# ---- §11.5 egress (deny-by-default policy) ----------------------------------


def test_empty_allowlist_denies_all_network():
    spec = SandboxSpec()  # empty egress_allow
    assert spec.egress_allowed("example.com") is False
    assert spec.egress_allowed("localhost") is False


def test_allowlist_scopes_to_named_hosts():
    spec = SandboxSpec(egress_allow=frozenset({"api.allowed.test"}))
    assert spec.egress_allowed("api.allowed.test") is True
    assert spec.egress_allowed("evil.test") is False


# ---- §11.6 kill switch ------------------------------------------------------


async def test_kill_switch_revokes_caps_egress_and_destroys_instance():
    secrets = InMemorySecretsStore({"K": SENTINEL})

    async def handler(*, query, limit=5):
        return [secrets.get("K")]  # would expose if it ran post-kill

    broker = CapabilityBroker()
    broker.register("search", handler)
    svc = ProcessSandboxService()
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")
    ex = DefaultToolExecutor(
        build_default_registry(),
        ToolScope(allowed_tools=frozenset({"shell", "search"})),
        sandbox=inst,
        broker=broker,
    )
    # works before kill
    assert (await ex.execute(call("shell", command="echo ok"))).success

    await ex.kill()

    # sandbox tool → sandbox_error; capability → denied (revoked).
    after_shell = await ex.execute(call("shell", command="echo ok"))
    assert after_shell.success is False and after_shell.structured["kind"] == "sandbox_error"
    after_search = await ex.execute(call("search", query="x"))
    assert after_search.success is False  # capabilities revoked


async def test_kill_revokes_a_capability_set_issued_before_kill():
    """A tool context already in flight cannot retain a stale handler map."""

    async def handler():
        return "should not run"

    broker = CapabilityBroker()
    broker.register("visual_inspection", handler)
    issued = broker.grant(frozenset({"visual_inspection"}))
    broker.revoke_all()

    assert issued.has("visual_inspection") is False
    with pytest.raises(CapabilityDenied):
        await issued.call("visual_inspection")
    with pytest.raises(RuntimeError, match="revoked"):
        broker.register("visual_inspection", handler)
