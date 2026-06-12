"""Acceptance tests for BP-03: environment contract + retired preview denies."""

from __future__ import annotations

from disco.core.events import ActionEvent, SecurityRisk, ToolCall
from disco.core.llm.prompts import _EXECUTION_DRIVER_PROMPT, _PLANNING_DRIVER_PROMPT
from disco.core.loop.engine import AgentLoop
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
        return AgentLoop._hard_deny_reason(action) is not None

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
    # Execution prompt MUST contain the new session/port 8000 contract
    assert "YOUR ENVIRONMENT — processes, ports, serving" in _EXECUTION_DRIVER_PROMPT
    assert "shell_kill_process('preview')" in _EXECUTION_DRIVER_PROMPT
    assert "server_status" in _EXECUTION_DRIVER_PROMPT
    
    # Execution prompt MUST NOT contain the old "NEVER kill" or stale tools
    assert "NEVER kill" not in _EXECUTION_DRIVER_PROMPT
    assert "pkill http.server" not in _EXECUTION_DRIVER_PROMPT
    assert "run_server" not in _EXECUTION_DRIVER_PROMPT
    assert "preview_status" not in _EXECUTION_DRIVER_PROMPT
    assert "restart_preview" not in _EXECUTION_DRIVER_PROMPT

def test_planning_prompt_environment():
    # Planning prompt MUST contain the new environment paragraph
    assert "EXECUTION ENVIRONMENT" in _PLANNING_DRIVER_PROMPT
    assert "port 8000" in _PLANNING_DRIVER_PROMPT
    assert "persistent shell SESSIONS" in _PLANNING_DRIVER_PROMPT
