"""Cluster 3 — security: hard-deny tier (catastrophic commands refused outright)
+ the egress allowlist predicate a proxy consults."""

from __future__ import annotations

from loop_fakes import ScriptedAgent, action_step, build_loop
from perpleximanus.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    ObservationEvent,
)
from perpleximanus.core.security.analyzers import hard_deny_reason

CID = "conv"


# ---- the hard-deny analyzer floor -------------------------------------------


def test_hard_deny_catches_catastrophic_commands():
    assert hard_deny_reason("mkfs.ext4 /dev/sda1") is not None
    assert hard_deny_reason("dd if=/dev/zero of=/dev/sda") is not None
    assert hard_deny_reason("echo boom > /dev/sda") is not None
    assert hard_deny_reason(":(){ :|:& };:") is not None
    assert hard_deny_reason("rm -rf /") is not None
    assert hard_deny_reason("rm -rf /*") is not None


def test_hard_deny_allows_ordinary_commands():
    assert hard_deny_reason("ls -la") is None
    assert hard_deny_reason("npm install") is None
    assert hard_deny_reason("rm -rf ./build") is None  # a local dir, not root
    assert hard_deny_reason("dd if=in.txt of=out.txt") is None  # files, not a device


def test_hard_deny_protects_the_preview_server():
    # E6: the agent must not kill the :8000 preview server (the cause of the stuck
    # build) — it's refused, with a pointer to the controlled restart_preview path.
    assert hard_deny_reason("pkill -f http.server") is not None
    assert hard_deny_reason("pkill -9 -f 'python3.*http.server'") is not None
    assert hard_deny_reason("killall http.server") is not None
    assert "restart_preview" in (hard_deny_reason("pkill -f http.server") or "")
    # but killing the agent's OWN unrelated process is fine
    assert hard_deny_reason("pkill -f my_worker.py") is None


# ---- the loop refuses denied actions before the confirm gate ----------------


async def test_loop_refuses_hard_denied_action_without_executing():
    # The agent proposes `rm -rf /`, then a safe finish. The denied command must
    # NOT execute (no observation) and must produce a refusal error the agent sees.
    agent = ScriptedAgent(
        [
            action_step("shell", args={"command": "rm -rf /"}),
            action_step("finish", args={"summary": "ok"}),
        ]
    )
    loop, store = build_loop(agent)
    await loop.send_message("clean up")
    await loop.run()
    events = await store.get_events(CID)
    # The denied shell produced NO observation (it never ran).
    shell_obs = [
        e
        for e in events
        if isinstance(e, ObservationEvent) and e.tool_result.tool_name == "shell"
    ]
    assert shell_obs == []
    # A refusal error was emitted, naming the deny.
    refusals = [
        e for e in events if isinstance(e, AgentErrorEvent) and "REFUSED" in e.error
    ]
    assert len(refusals) == 1
    assert "hard-denied" in refusals[0].error
    # The proposed action is still recorded for audit.
    assert any(
        isinstance(e, ActionEvent)
        and e.tool_call is not None
        and e.tool_call.arguments.get("command") == "rm -rf /"
        for e in events
    )
    # Unused import guard.
    assert ConversationStatus


# ---- the egress allowlist predicate (what a proxy consults) -----------------


def test_egress_deny_by_default():
    from perpleximanus.tools.sandbox.base import SandboxSpec

    spec = SandboxSpec()  # empty allowlist
    assert spec.egress_allowed("example.com") is False
    assert spec.egress_allowed("anything.io") is False


def test_egress_exact_and_subdomain_matching():
    from perpleximanus.tools.sandbox.base import SandboxSpec

    spec = SandboxSpec(egress_allow=frozenset({"api.openai.com", ".github.com"}))
    # exact
    assert spec.egress_allowed("api.openai.com") is True
    assert spec.egress_allowed("openai.com") is False  # not listed
    # subdomain suffix: apex + any subdomain
    assert spec.egress_allowed("github.com") is True
    assert spec.egress_allowed("raw.github.com") is True
    assert spec.egress_allowed("evilgithub.com") is False  # not a real suffix match
