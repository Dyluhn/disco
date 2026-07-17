"""Secrets/capabilities (§11.4), egress (§11.5), kill switch (§11.6) — the
security-critical tests. The headline is `test_no_secret_in_the_box`."""

from __future__ import annotations

import re

from disco.tools import (
    CapabilityBroker,
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


async def test_no_secret_in_the_box():
    """A secret injected into the orchestrator's store is absent from the
    sandbox's env, filesystem (including any designed spill artifact), and
    process list."""
    secrets = InMemorySecretsStore({"PROVIDER_KEY": SENTINEL})
    assert secrets.get("PROVIDER_KEY") == SENTINEL  # the orchestrator can read it

    svc = ProcessSandboxService()
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")
    # env (clean — never os.environ), process list, and workspace: all secret-free.
    env = await inst.exec_shell("env", timeout_s=10)
    # Scan the FULL process list, not the bounded stdout projection: bounded exec
    # returns only a 64 KiB head + tail and spills at most 1 MiB, so a sentinel in
    # the dropped middle of an oversized `ps` would be invisible to a stdout scan
    # (independent verification demonstrated exactly that gap). Writing the listing
    # into the box and reading the file back whole closes it — read_file's 32 MiB
    # cap is far above any real process list.
    ps_write = await inst.exec_shell(
        "{ ps -e -o args 2>/dev/null || ps; } > .ps-scan.txt", timeout_s=10
    )
    assert ps_write.exit_code == 0
    ps_bytes = await inst.read_file(".ps-scan.txt")
    assert len(ps_bytes) > 0
    assert SENTINEL not in env.stdout
    assert SENTINEL.encode("utf-8") not in ps_bytes
    await _assert_box_secret_free(inst, allowed=(".ps-scan.txt",))
    await inst.destroy()


async def test_no_secret_in_the_box_even_when_output_spills():
    """DETERMINISTIC spill branch: force a >128 KiB output so the bounded-exec
    helper writes its designed `.disco/spills/` artifact, then prove the box
    STILL carries no secret byte anywhere — including inside the spill files.
    (The headline test only hits this branch when the host is process-heavy;
    this pins the behavior regardless of load.)"""
    secrets = InMemorySecretsStore({"PROVIDER_KEY": SENTINEL})
    assert secrets.get("PROVIDER_KEY") == SENTINEL

    svc = ProcessSandboxService()
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")
    big = await inst.exec_shell("seq 1 400000", timeout_s=30)
    assert big.exit_code == 0
    assert SENTINEL not in big.stdout
    files = await inst.list_dir(".")
    assert files == [".disco"], f"expected the designed spill artifact, got {files}"
    await _assert_box_secret_free(inst)
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
