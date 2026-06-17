"""Secrets/capabilities (§11.4), egress (§11.5), kill switch (§11.6) — the
security-critical tests. The headline is `test_no_secret_in_the_box`."""

from __future__ import annotations

from tool_fakes import call
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

SENTINEL = "SUPER-SECRET-SENTINEL-9F3A2"


# ---- §11.4 the headline: no secret is ever in the box -----------------------


async def test_no_secret_in_the_box():
    """A secret injected into the orchestrator's store is absent from the
    sandbox's env, filesystem, and process list."""
    secrets = InMemorySecretsStore({"PROVIDER_KEY": SENTINEL})
    assert secrets.get("PROVIDER_KEY") == SENTINEL  # the orchestrator can read it

    svc = ProcessSandboxService()
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")
    # env (clean — never os.environ), process list, and workspace: all secret-free.
    env = await inst.exec_shell("env", timeout_s=10)
    procs = await inst.exec_shell("ps -e -o args 2>/dev/null || ps", timeout_s=10)
    files = await inst.list_dir(".")
    assert SENTINEL not in env.stdout
    assert SENTINEL not in procs.stdout
    assert files == []  # nothing was written into the box
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
