"""HS-01: Shell spill-to-file when assist is ON and stdout is huge.

Gated by `ToolContext.assist` (added by T1) so capable-model behavior is
byte-identical when the gate is OFF. Threshold defaults to 50KB and is
overridable via `DISCO_SHELL_SPILL_KB`. stdout-only (file side-effects are
not spilled). The marker is PRESCRIPTIVE: it tells the model exactly which
file to file_read / grep.
"""

from __future__ import annotations

import os
import re
import uuid

from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.system import ShellTool
from disco.tools.sandbox.base import ExecResult
from tool_fakes import FakeSandboxInstance

# ---- fakes -----------------------------------------------------------------


class BigStdoutInstance(FakeSandboxInstance):
    """FakeSandboxInstance that returns a deterministic huge stdout from
    exec_shell. The payload is a single repeated string so we can verify the
    head and tail slices are present in the rewritten content."""

    def __init__(self, payload: bytes, *, exit_code: int = 0) -> None:
        super().__init__()
        self._payload = payload
        self._exit_code = exit_code
        self.last_cmd = None

    async def exec_shell(self, cmd, *, timeout_s):
        self._alive()
        self.last_cmd = cmd
        return ExecResult(exit_code=self._exit_code, stdout=self._payload, stderr="")


def _ctx(sbx, *, assist):
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.SHELL},
        owner_id="local",
        conversation_id="c",
        assist=assist,
    )


def _args(cmd="big"):
    return ShellTool.definition.args_model(command=cmd)

def _spill_keys(sbx):
    """Return the in-memory keys for any .disco-spill-*.log file written
    during this test, robust to the workspace_path prefix (so "." -> "./." ."""
    return [
        p
        for p in sbx._fs
        if os.path.basename(p).startswith(".disco-spill-") and p.endswith(".log")
    ]




# Sizes chosen to be unambiguous relative to the 50KB default and the 2KB
# head/tail slices.
PAYLOAD_60K = b"X" * 60_000
PAYLOAD_10K = b"X" * 10_000
# Anchor bytes: unique markers placed in the FIRST 100 bytes and the
# LAST 100 bytes of the payload, with 50KB of filler in between. With the 2KB
# head/tail slices these markers must land in their respective slices, proving
# the head and tail come from the ORIGINAL stdout and no bytes are fabricated
# between them.
PAYLOAD_ANCHORED = (
    b"HEADBLOCK-START" + b"A" * 80
    + b"X" * 60_000
    + b"B" * 80 + b"TAILBLOCK-END"
)


# ---- the three cases the plan calls out ------------------------------------


async def test_spill_huge_stdout_when_assist_on():
    """Case 1: >50KB stdout + ctx.assist=True -> spill file + head+tail marker."""
    sbx = BigStdoutInstance(PAYLOAD_ANCHORED)
    ctx = _ctx(sbx, assist=True)
    res = await ShellTool().run(_args(), ctx)

    assert res.success is True
    # Marker is present and PRESCRIPTIVE
    assert "[truncated" in res.content
    assert "file_read" in res.content or "grep" in res.content

    # The spill file was written and holds the FULL stdout
    spill_paths = _spill_keys(sbx)
    assert len(spill_paths) == 1, f"expected exactly 1 spill file, got {spill_paths}"
    spill_path = spill_paths[0]
    spill_bytes = sbx._fs[spill_path]
    assert spill_bytes == PAYLOAD_ANCHORED, "spill file must hold the FULL stdout"

    # The marker points AT the spill file
    assert spill_path in res.content

    # Head and tail of the ORIGINAL stdout appear in the content
    assert "HEADBLOCK-START" in res.content
    assert "TAILBLOCK-END" in res.content
    # And the middle was DROPPED (no fabricated bytes between them)
    assert (
        "HEADBLOCK-START" + "A" * 80 + "X" * 60_000 + "B" * 80 + "TAILBLOCK-END"
        not in res.content
    )

    # Structured payload records the spill path so callers can find it
    assert res.structured is not None
    assert res.structured.get("spill_path") == spill_path


async def test_no_spill_below_threshold():
    """Case 2: <50KB -> behavior unchanged. No spill file, content = raw stdout."""
    sbx = BigStdoutInstance(PAYLOAD_10K)
    ctx = _ctx(sbx, assist=True)
    res = await ShellTool().run(_args(), ctx)

    assert res.success is True
    # No marker, no spill file
    assert "[truncated" not in res.content
    spill_paths = _spill_keys(sbx)
    assert spill_paths == []
    # Raw stdout preserved as-is
    assert res.content == PAYLOAD_10K.decode()
    assert res.structured is None or "spill_path" not in res.structured


async def test_no_spill_when_assist_off():
    """Case 3: ctx.assist=False + >50KB -> behavior unchanged.

    Capable-model path is byte-identical.
    """
    sbx = BigStdoutInstance(PAYLOAD_ANCHORED)
    ctx = _ctx(sbx, assist=False)
    res = await ShellTool().run(_args(), ctx)

    assert res.success is True
    assert "[truncated" not in res.content
    spill_paths = _spill_keys(sbx)
    assert spill_paths == []
    # Raw stdout preserved as-is
    assert res.content == PAYLOAD_ANCHORED.decode()
    assert res.structured is None or "spill_path" not in res.structured


# ---- extra binary-observable checks ---------------------------------------


async def test_spill_filename_is_uuid_hex_under_workspace():
    """Spill filename matches .disco-spill-<32-hex>.log, written under the workspace."""
    sbx = BigStdoutInstance(PAYLOAD_60K)
    ctx = _ctx(sbx, assist=True)
    await ShellTool().run(_args(), ctx)

    spill_paths = _spill_keys(sbx)
    assert len(spill_paths) == 1
    spill_path = spill_paths[0]
    # <uuid4().hex> = 32 lowercase hex chars
    m = re.match(r"^\.?/?\.disco-spill-([0-9a-f]{32})\.log$", spill_path)
    assert m, f"spill path {spill_path!r} does not match expected pattern"
    # valid uuid
    uuid.UUID(m.group(1))


async def test_spill_threshold_override_via_env(monkeypatch):
    """DISCO_SHELL_SPILL_KB changes the threshold; here we set it very low so a
    'small' payload still spills - proving the env var is honored."""
    monkeypatch.setenv("DISCO_SHELL_SPILL_KB", "5")  # 5KB
    sbx = BigStdoutInstance(PAYLOAD_10K)
    ctx = _ctx(sbx, assist=True)
    res = await ShellTool().run(_args(), ctx)

    assert "[truncated" in res.content
    spill_paths = _spill_keys(sbx)
    assert len(spill_paths) == 1


async def test_spill_does_not_touch_stderr_or_exit_code():
    """stdout-only spill: stderr + exit_code are preserved verbatim."""
    payload = b"Y" * 60_000
    sbx = BigStdoutInstance.__new__(BigStdoutInstance)
    FakeSandboxInstance.__init__(sbx)

    async def _exec(cmd, *, timeout_s):
        sbx.last_cmd = cmd
        return ExecResult(exit_code=7, stdout=payload, stderr="boom\n", timed_out=False)

    sbx.exec_shell = _exec
    ctx = _ctx(sbx, assist=True)
    res = await ShellTool().run(_args(), ctx)

    # exit code and stderr preserved
    assert res.structured["exit_code"] == 7
    assert res.structured["stderr"] == "boom\n"
    # Spill file holds the FULL stdout (no stderr mixed in)
    spill_paths = _spill_keys(sbx)
    assert len(spill_paths) == 1
    assert sbx._fs[spill_paths[0]] == payload
