"""Cluster 3 — security: hard-deny tier (catastrophic commands refused outright)
+ the egress allowlist predicate a proxy consults."""

from __future__ import annotations

from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    ObservationEvent,
)
from disco.core.security.analyzers import hard_deny_reason
from loop_fakes import ScriptedAgent, action_step, build_loop

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


# ---- W3 C-3: the rm floor must survive the bypass variants a regex missed -----


def test_hard_deny_catches_rm_root_bypass_variants():
    # Each of these previously slipped past `rm … -[rfRF]* (/|/*)` and is the
    # explicit Wave-3 acceptance set: `rm -rf -- /` (+ 5 other variants) DENIED.
    for cmd in [
        "rm -rf -- /",                       # end-of-options `--` separator
        "rm -rf --no-preserve-root /",       # explicit root-wipe opt-in
        "rm --recursive --force /",          # long options
        "rm -fr /",                          # reordered short cluster
        "rm -r /",                           # recursive without force is still fatal
        "rm -rf /*",                         # glob of root
        "X=/; rm -rf \"$X\"",                # variable indirection
        "cd /tmp && rm -rf /",               # rm in a later segment
        'rm -rf "$(echo /)"',                # command substitution target
        "bash -c 'rm -rf /'",                # nested shell interpreter
        "rm -rf /etc",                       # a whole system dir
        "rm -rf ~",                          # home wipe
        "rm -rf /home/../",                  # path traversal collapses to /
        "rm -rf ///",                        # slash tricks collapse to /
        "rm -rf /etc/../etc",                # traversal back to a system dir
        r"\rm -rf /",                        # backslash-escaped rm (alias bypass)
        "'r'm -rf /",                        # split-quote rm (shlex rejoins to rm)
        "command rm -rf /",                  # `command` builtin prefix
        "sudo rm -rf /",                     # sudo prefix
        "env rm -rf /",                      # env prefix
        "/bin/rm -rf /",                     # absolute path to rm
        "rm -rf /etc/*",                     # glob of a system dir's contents
        "bash -lc 'rm -rf /'",               # clustered -lc shell flag
        "sh -ec 'rm -rf /'",                 # clustered -ec shell flag
        "rm -rf --no-preserve-root ///",     # slash-collapse after opt-in
        "rm -rf --no-preserve-root /usr/..", # traversal after opt-in
        "find / -delete",                    # find-based root wipe
        "find / -maxdepth 0 -exec rm -rf {} +",  # find -exec rm on root
        # round-2 adversarial review (gpt-5.5):
        "rm -rf //etc",                      # POSIX keeps //, Linux resolves to /etc
        "sudo -u root rm -rf /",             # prefix with an option-argument
        "nice -n 19 rm -rf /",               # ditto (nice)
        "env -u PATH rm -rf /",              # ditto (env)
        "rm --rec /etc",                     # GNU long-option abbreviation
        "rm --r /etc",                       # shortest unambiguous abbreviation
        "find -H / -delete",                 # find leading global option
        "find / -exec /bin/rm -rf {} +",     # path-qualified rm in -exec
    ]:
        assert hard_deny_reason(cmd) is not None, f"NOT denied but should be: {cmd!r}"


def test_hard_deny_does_not_false_positive_on_real_builds():
    # The floor cannot be overridden, so ordinary cleanup MUST stay allowed.
    for cmd in [
        "rm -rf ./build",
        "rm -rf build",
        "rm -rf node_modules",
        "rm -rf dist/",
        "rm -rf /tmp/scratch-123",
        "rm -rf .next .cache",
        "rm -rf $BUILD_DIR",                 # unassigned var → unknown, not root
        'rm -rf "$PWD/out"',
        "rm file.txt",                       # not recursive
        "git rm -r --cached .",              # git rm of a repo subtree
        "echo rm -rf /",                     # rm is an ARGUMENT to echo, not the command
        "printf 'rm -rf /\\n'",              # literal text, not an invocation
        "find . -name '*.pyc' -delete",      # find-delete under cwd, not a root
        "find . -type f -delete",            # ditto
        "find src -name '*.o' -exec rm {} +", # find -exec rm under a subdir
        "find /tmp/work -delete",            # find-delete under /tmp subpath
        "bash -c 'npm run build'",           # wrapper with a benign command
        # round-2 adversarial review (gpt-5.5): a `;`/`|` INSIDE quotes must not
        # be treated as a command separator (quote-aware tokenization).
        "printf 'rm -rf /;'",                # the ; is inside the quoted arg
        'node -e "console.log(\'rm -rf /;\')"',  # rm text inside a JS string
        "echo 'rm -rf / | cat'",             # the | is inside the quoted arg
        "rm --interactive foo",              # --i… is NOT the --recursive prefix
        "rm --force ./x",                    # --f… is NOT recursive
    ]:
        assert hard_deny_reason(cmd) is None, f"false-positive deny on: {cmd!r}"


def test_hard_deny_allows_preview_termination():
    # BP-03: pkill http.server is no longer hard-denied (the agent owns its sessions)
    assert hard_deny_reason("pkill -f http.server") is None
    assert hard_deny_reason("pkill -9 -f 'python3.*http.server'") is None
    assert hard_deny_reason("killall http.server") is None
    # but killing unrelated processes remains allowed as always
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
    from disco.tools.sandbox.base import SandboxSpec

    spec = SandboxSpec()  # empty allowlist
    assert spec.egress_allowed("example.com") is False
    assert spec.egress_allowed("anything.io") is False


def test_egress_exact_and_subdomain_matching():
    from disco.tools.sandbox.base import SandboxSpec

    spec = SandboxSpec(egress_allow=frozenset({"api.openai.com", ".github.com"}))
    # exact
    assert spec.egress_allowed("api.openai.com") is True
    assert spec.egress_allowed("openai.com") is False  # not listed
    # subdomain suffix: apex + any subdomain
    assert spec.egress_allowed("github.com") is True
    assert spec.egress_allowed("raw.github.com") is True
    assert spec.egress_allowed("evilgithub.com") is False  # not a real suffix match
