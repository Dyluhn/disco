"""Acceptance tests for BP-03: environment contract + retired preview denies."""

from __future__ import annotations

from disco.core.events import ActionEvent, SecurityRisk, ToolCall
from disco.core.llm.prompts import (
    _EXECUTION_DRIVER_PROMPT,
    _EXECUTION_DRIVER_PROMPT_SMALL,
    _HOST_VERIFY_MANDATE_CAPABLE,
    _HOST_VERIFY_MANDATE_SMALL,
    _PLANNING_DRIVER_PROMPT,
    _SELF_VERIFY_MANDATE_CAPABLE,
    _SELF_VERIFY_MANDATE_SMALL,
)
from disco.core.loop import signals
from disco.core.security.analyzers import RuleBasedAnalyzer, hard_deny_reason


def test_analyzer_allows_pkill_but_denies_mkfs():
    # pkill is no longer in _SHELL_DENY
    assert hard_deny_reason("pkill -f http.server") is None
    assert hard_deny_reason("killall preview") is None

    # mkfs is still denied
    assert hard_deny_reason("mkfs.ext4 /dev/sda1") is not None


def test_shell_exec_is_guarded_like_shell():
    def is_denied(tool_name: str, command: str) -> bool:
        call = ToolCall(tool_name=tool_name, arguments={"command": command})
        action = ActionEvent(thought="test", tool_call=call)
        return signals.hard_deny_reason(action) is not None

    # BOTH are denied for destructive root commands
    assert is_denied("shell", "rm -rf /") is True
    assert is_denied("shell_exec", "rm -rf /") is True

    # NEITHER is denied for pkill
    assert is_denied("shell", "pkill -f http.server") is False
    assert is_denied("shell_exec", "pkill -f http.server") is False


def test_new_tool_risk_levels():
    analyzer = RuleBasedAnalyzer()

    def get_risk(tool_name: str, args: dict | None = None) -> SecurityRisk:
        call = ToolCall(tool_name=tool_name, arguments=args or {})
        action = ActionEvent(thought="test", tool_call=call)
        return analyzer.assess(action)

    # LOW risk tools
    assert get_risk("shell_view") == SecurityRisk.LOW
    assert get_risk("shell_wait") == SecurityRisk.LOW
    assert get_risk("server_status") == SecurityRisk.LOW

    # MEDIUM risk tools
    assert get_risk("shell_kill_process") == SecurityRisk.MEDIUM
    assert get_risk("shell_write_to_process") == SecurityRisk.MEDIUM

    # shell_exec follows command risk (unrecognized command = MEDIUM default)
    assert get_risk("shell_exec", {"command": "ls"}) == SecurityRisk.LOW
    assert get_risk("shell_exec", {"command": "npm install"}) == SecurityRisk.MEDIUM
    assert get_risk("shell_exec", {"command": "sudo rm -rf /"}) == SecurityRisk.HIGH


def test_prompt_contract_text():
    # Execution prompt MUST teach the PLATFORM-owned preview contract: the model declares
    # intent via `preview_start` and uses the URL/port it RETURNS — there is no fixed :8000
    # auto-served inside the sandbox (the platform chooses the port).
    assert "YOUR ENVIRONMENT — processes, ports, serving" in _EXECUTION_DRIVER_PROMPT
    assert "preview_start" in _EXECUTION_DRIVER_PROMPT
    assert "server_status" in _EXECUTION_DRIVER_PROMPT
    # The dead ":8000 auto-served" model is gone — no instruction to free/bind port 8000.
    assert "shell_kill_process('preview')" not in _EXECUTION_DRIVER_PROMPT
    assert "auto-served on port 8000" not in _EXECUTION_DRIVER_PROMPT

    # Execution prompt MUST NOT contain the old "NEVER kill" or stale/nonexistent tools
    assert "NEVER kill" not in _EXECUTION_DRIVER_PROMPT
    assert "pkill http.server" not in _EXECUTION_DRIVER_PROMPT
    assert "run_server" not in _EXECUTION_DRIVER_PROMPT
    assert "restart_preview" not in _EXECUTION_DRIVER_PROMPT


def test_scanfix_prompt_surgery_contract():
    assert "TALKING vs FINISHING — know what each tool is for" in _EXECUTION_DRIVER_PROMPT
    assert "three distinct tools" not in _EXECUTION_DRIVER_PROMPT

    assert "step BOUNDARIES" in _EXECUTION_DRIVER_PROMPT
    assert "not per action" in _EXECUTION_DRIVER_PROMPT
    assert "ARRAY OF OBJECTS" not in _EXECUTION_DRIVER_PROMPT

    assert "A successful edit's observation shows the updated region" in _EXECUTION_DRIVER_PROMPT
    assert "do not re-read after your own successful edit" in _EXECUTION_DRIVER_PROMPT
    assert "Read a file immediately BEFORE editing it" not in _EXECUTION_DRIVER_PROMPT

    assert "user or target requires a particular layout" in _EXECUTION_DRIVER_PROMPT
    assert "large files are supported" in _EXECUTION_DRIVER_PROMPT.lower()
    assert "800 lines" not in _EXECUTION_DRIVER_PROMPT
    assert "48KB" not in _EXECUTION_DRIVER_PROMPT
    assert "single monolithic file" not in _EXECUTION_DRIVER_PROMPT_SMALL

    assert "verify_web_app" in _EXECUTION_DRIVER_PROMPT
    assert "browser tool once" not in _EXECUTION_DRIVER_PROMPT


def test_execution_prompts_bound_large_file_tool_calls():
    for prompt in (_EXECUTION_DRIVER_PROMPT, _EXECUTION_DRIVER_PROMPT_SMALL):
        assert "under 12,000 characters" in prompt
        assert "bounded `file_write`" in prompt
        assert "bounded `file_append`" in prompt


def test_verify_mandates_lead_with_structured_verifier():
    mandates = [
        _SELF_VERIFY_MANDATE_CAPABLE,
        _HOST_VERIFY_MANDATE_CAPABLE,
        _SELF_VERIFY_MANDATE_SMALL,
        _HOST_VERIFY_MANDATE_SMALL,
    ]
    for mandate in mandates:
        assert "verify_web_app" in mandate
        assert "single pass/fail verdict" in mandate
        assert "never a guessed :8000" in mandate
        assert "Verify once and stop" in mandate
        assert "browser tool once" not in mandate


def test_planning_prompt_environment():
    # Planning prompt MUST contain the new environment paragraph + the preview_start
    # contract (preview is a PLATFORM concern; no fixed-port assumption).
    assert "EXECUTION ENVIRONMENT" in _PLANNING_DRIVER_PROMPT
    assert "preview_start" in _PLANNING_DRIVER_PROMPT
    assert "auto-served on port 8000" not in _PLANNING_DRIVER_PROMPT
    assert "persistent shell SESSIONS" in _PLANNING_DRIVER_PROMPT
    assert (
        "`shell`, `code_exec`, and all write tools are LOCKED until the plan is approved"
        in _PLANNING_DRIVER_PROMPT
    )
