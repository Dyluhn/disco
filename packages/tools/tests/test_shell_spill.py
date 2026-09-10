"""Shell output caps for one-shot shell and persistent shell_exec."""

from __future__ import annotations

from typing import Any

from disco.core.events import AgentErrorEvent
from disco.core.loop.observe import _error_detail
from disco.tools import DefaultToolExecutor
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.shell_sessions import ShellExecTool
from disco.tools.builtin.system import CodeExecTool, ShellTool
from disco.tools.registry import ToolRegistry, ToolScope
from disco.tools.sandbox.base import ExecResult
from disco.tools.sandbox.kernel import KernelResult
from disco.tools.sandbox.shell_sessions import ExecOutcome
from tool_fakes import FakeSandboxInstance, call


class BigStdoutInstance(FakeSandboxInstance):
    def __init__(self, payload: str, *, exit_code: int = 0, stderr: str = "") -> None:
        super().__init__()
        self._payload = payload
        self._exit_code = exit_code
        self._stderr = stderr
        self.last_cmd = None

    async def exec_shell(self, cmd, *, timeout_s):
        self._alive()
        self.last_cmd = cmd
        return ExecResult(
            exit_code=self._exit_code,
            stdout=self._payload,
            stderr=self._stderr,
        )


def _ctx(sbx, *, assist=False, sessions=None, kernel=None):
    return ToolContext(
        sandbox=sbx,
        sessions=sessions,
        kernel=kernel,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.SHELL},
        owner_id="local",
        conversation_id="c",
        assist=assist,
    )


def _shell_args(cmd="big"):
    return ShellTool.definition.args_model(command=cmd)


PAYLOAD_ANCHORED = "HEADBLOCK-START" + "A" * 80 + "X" * 6_000 + "B" * 80 + "TAILBLOCK-END"
BINARY_FONT_OUTPUT = "wOF2\x00\x01\x02\x1b\ufffd" + "Z" * 8_000


def _assert_no_binary_output(value: Any) -> None:
    if isinstance(value, str):
        assert "wOF2" not in value
        assert "\ufffd" not in value
        assert not any(
            (ord(char) < 32 or 127 <= ord(char) <= 159) and char not in {"\t", "\n", "\r"}
            for char in value
        )
    elif isinstance(value, dict):
        for key, item in value.items():
            _assert_no_binary_output(key)
            _assert_no_binary_output(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_binary_output(item)
    elif isinstance(value, bytes):
        assert b"wOF2" not in value
        assert not any(
            (byte < 32 or 127 <= byte <= 159) and byte not in {9, 10, 13} for byte in value
        )


async def test_shell_caps_large_output_and_spills_full_evidence():
    """Over-cap output: the observation is head+tail capped, and the FULL
    output is saved to a workspace spill file the marker names — evidence is
    never discarded when a sandbox can hold it."""
    sbx = BigStdoutInstance(PAYLOAD_ANCHORED)
    res = await ShellTool().run(_shell_args(), _ctx(sbx))

    assert res.success is True
    assert "HEADBLOCK-START" in res.content
    assert "TAILBLOCK-END" in res.content
    assert "omitted" in res.content
    assert "FULL output saved to" in res.content
    spill_paths = [pth for pth in sbx._fs if "disco-spill" in pth]
    assert len(spill_paths) == 1
    assert spill_paths[0] in res.content
    assert sbx._fs[spill_paths[0]].decode() == PAYLOAD_ANCHORED  # full evidence
    assert len(res.content) < 4_400

    assert res.structured is not None
    assert res.structured["output_truncated"] is True
    assert res.structured["output_chars"] == len(PAYLOAD_ANCHORED)
    assert res.structured["dropped_chars"] == len(PAYLOAD_ANCHORED) - 4_000
    assert res.structured["spill_path"] == spill_paths[0]
    assert "stdout" not in res.structured
    assert "stderr" not in res.structured


async def test_shell_cap_falls_back_to_rerun_hint_when_spill_fails():
    """No sandbox write possible → the marker falls back to the rerun hint
    (honest: nothing was saved)."""
    sbx = BigStdoutInstance(PAYLOAD_ANCHORED)

    async def _refuse_write(path, data):  # noqa: ANN001
        raise OSError("read-only")

    sbx.write_file = _refuse_write
    res = await ShellTool().run(_shell_args(), _ctx(sbx))
    assert "full output not retained" in res.content
    assert "`... > out.log 2>&1`" in res.content
    assert res.structured is not None and "spill_path" not in res.structured


async def test_shell_cap_applies_when_assist_on_with_spill_file():
    """The cap + spill behave identically for the assist tier (the old
    assist-only gate is gone; evidence preservation is tier-independent)."""
    sbx = BigStdoutInstance(PAYLOAD_ANCHORED)
    res = await ShellTool().run(_shell_args(), _ctx(sbx, assist=True))

    assert res.structured is not None
    assert res.structured["output_truncated"] is True
    assert any("disco-spill" in path for path in sbx._fs)


async def test_no_cap_below_threshold_preserves_stdout_stderr_structured():
    sbx = BigStdoutInstance("short stdout", stderr="small stderr")
    res = await ShellTool().run(_shell_args(), _ctx(sbx))

    assert res.success is True
    assert res.content == "short stdout"
    assert res.structured["stdout"] == "short stdout"
    assert res.structured["stderr"] == "small stderr"
    assert res.structured["output_truncated"] is False


async def test_normal_utf8_execution_output_is_preserved_exactly():
    stdout = "first\tline\nZażółć gęślą jaźń — 🛠️"
    stderr = "ordinary warning\r\n"
    sbx = BigStdoutInstance(stdout, stderr=stderr)

    res = await ShellTool().run(_shell_args(), _ctx(sbx))

    assert res.content == stdout
    assert res.structured is not None
    assert res.structured["stdout"] == stdout
    assert res.structured["stderr"] == stderr
    assert "binary_output_sanitized" not in res.structured


async def test_successful_shell_binary_output_is_replaced_before_spill_or_outcome_fields():
    sbx = BigStdoutInstance(BINARY_FONT_OUTPUT)

    res = await ShellTool().run(_shell_args("cat release/fonts/proof.woff2"), _ctx(sbx))

    assert res.success is True
    assert "binary/control stdout omitted" in res.content
    assert "workspace artifact" in res.content
    assert "output.bin" in res.content
    assert res.structured is not None
    assert res.structured["binary_output_sanitized"] is True
    assert res.structured["sanitized_streams"]["stdout"] == {
        "decoded_chars": len(BINARY_FONT_OUTPUT),
        "control_chars": 4,
        "decode_replacements": 1,
    }
    # Binary-like output is never preserved as a spill; the safe replacement is
    # short enough to return directly.
    assert not any("disco-spill" in path for path in sbx._fs)
    _assert_no_binary_output(res.content)
    _assert_no_binary_output(res.structured)
    _assert_no_binary_output(sbx._fs)


async def test_failed_shell_binary_output_never_reaches_durable_agent_error_detail():
    sbx = BigStdoutInstance(
        BINARY_FONT_OUTPUT,
        exit_code=1,
        stderr="failure diagnostic\n" + "E" * 6_000,
    )
    registry = ToolRegistry(allow_unclassified_for_testing=True)
    registry.register(ShellTool())
    executor = DefaultToolExecutor(
        registry,
        ToolScope(allowed_tools=frozenset({"shell"})),
        sandbox=sbx,
        conversation_id="binary-error-regression",
    )

    result = await executor.execute(call("shell", command="cat release/fonts/proof.woff2"))

    assert result.success is False
    assert result.error == "command exited 1"
    assert "failure diagnostic" in result.content
    assert "binary/control stdout omitted" in result.content
    assert result.structured is not None
    assert result.structured["binary_output_sanitized"] is True
    spill_paths = [path for path in sbx._fs if "disco-spill" in path]
    assert len(spill_paths) == 1
    assert result.structured["spill_path"] == spill_paths[0]
    _assert_no_binary_output(sbx._fs[spill_paths[0]])

    # Exercise the exact loop helper that persists ToolResult.content as the
    # bounded AgentErrorEvent recovery detail.
    event = AgentErrorEvent(
        error=result.error,
        detail=_error_detail(result.error, result.content),
        action_id="action-binary",
        tool_call_id=result.call_id,
    )
    durable = event.model_dump(mode="json")
    assert event.detail is not None and "workspace artifact" in event.detail
    _assert_no_binary_output(result.content)
    _assert_no_binary_output(result.structured)
    _assert_no_binary_output(durable)
    _assert_no_binary_output(event.to_llm_message().content)
    _assert_no_binary_output(sbx._fs)


class _BinaryKernel:
    async def execute(self, code: str, *, timeout_s: int) -> KernelResult:
        return KernelResult(
            ok=True,
            stdout=BINARY_FONT_OUTPUT,
            stderr="",
            result_repr="wOF2\x00\x03",
        )


async def test_python_code_exec_sanitizes_stream_and_result_before_tool_outcome():
    sbx = FakeSandboxInstance()
    res = await CodeExecTool().run(
        CodeExecTool.definition.args_model(language="python", code="print(font_bytes)"),
        _ctx(sbx, kernel=_BinaryKernel()),
    )

    assert res.success is True
    assert "binary/control stdout omitted" in res.content
    assert "binary/control result omitted" in res.content
    assert res.structured is not None
    assert res.structured["binary_output_sanitized"] is True
    _assert_no_binary_output(res.content)
    _assert_no_binary_output(res.structured)


class _FakeSessions:
    async def exec(self, session: str, command: str, exec_dir: str | None):
        return ExecOutcome(
            running=False,
            exit_code=0,
            output=PAYLOAD_ANCHORED,
        )


class _BinarySessions:
    async def exec(self, session: str, command: str, exec_dir: str | None):
        return ExecOutcome(running=False, exit_code=0, output=BINARY_FONT_OUTPUT)


async def test_persistent_shell_binary_output_is_sanitized_before_tool_outcome():
    sbx = FakeSandboxInstance()
    ctx = _ctx(sbx, sessions=_BinarySessions())
    args = ShellExecTool.definition.args_model(session="main", command="cat font", exec_dir="")

    res = await ShellExecTool().run(args, ctx)

    assert res.success is True
    assert "binary/control session output omitted" in res.content
    assert res.structured is not None
    assert res.structured["binary_output_sanitized"] is True
    _assert_no_binary_output(res.content)
    _assert_no_binary_output(res.structured)


async def test_shell_exec_caps_large_session_output_with_same_hint():
    sbx = FakeSandboxInstance()
    ctx = _ctx(sbx, sessions=_FakeSessions())
    args = ShellExecTool.definition.args_model(session="main", command="big", exec_dir="")

    res = await ShellExecTool().run(args, ctx)

    assert res.success is True
    assert res.content.startswith("session 'main' — exit 0")
    assert "HEADBLOCK-START" in res.content
    assert "TAILBLOCK-END" in res.content
    assert "full output not retained" in res.content
    assert "`... > out.log 2>&1`" in res.content
    assert res.structured is not None
    assert res.structured["output_truncated"] is True


async def test_refusal_text_surfaces_as_error_not_bare_exit_126():
    # Bug 16 (3a): a direct `shell` that hits the containment refusal (exit 126,
    # stderr="refused: ...") must render the refusal TEXT as the ToolOutcome.error,
    # NOT a bare "shell exited 126" — the loop emits AgentErrorEvent(error=...) and
    # drops content, so without this the model never sees the actionable guidance.
    sbx = FakeSandboxInstance()
    refusal = (
        "refused: port 8000 is reserved for the platform (reserved control/UI ports: "
        "5173, 8000, 8800) — serve your app on a non-reserved port such as 3000 instead"
    )

    async def _exec(cmd, *, timeout_s):
        return ExecResult(exit_code=126, stdout="", stderr=refusal)

    sbx.exec_shell = _exec
    res = await ShellTool().run(_shell_args("python3 -m http.server 8000"), _ctx(sbx))

    assert res.success is False
    assert res.error == refusal
    assert "3000" in res.error and "exited 126" not in res.error


async def test_non_refusal_nonzero_exit_keeps_concise_summary():
    # Must-not-regress: an ordinary command failure still reports the concise
    # "... exited N" summary (only `refused:` 126 stderr is promoted to the error).
    sbx = FakeSandboxInstance()

    async def _exec(cmd, *, timeout_s):
        return ExecResult(exit_code=2, stdout="", stderr="no such file")

    sbx.exec_shell = _exec
    res = await ShellTool().run(_shell_args("cat missing"), _ctx(sbx))

    assert res.success is False
    assert res.error is not None and "exited 2" in res.error
