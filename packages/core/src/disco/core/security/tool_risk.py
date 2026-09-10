"""Tool-risk scoring — split from ``security/analyzers.py``.

Factors the injected ``base_risk`` (the tool/sandbox layer's static hint, passed
in because ``core`` cannot import ``tools``) and the argument shape for non-shell
tools.  Also includes the shell-command risk-level scoring (HIGH/MEDIUM/LOW
patterns).
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from ..events import SecurityRisk
from .risk import max_risk

_H = SecurityRisk.HIGH
_M = SecurityRisk.MEDIUM
_L = SecurityRisk.LOW

# ---- shell-command rules [INTERIOR] -----------------------------------------
# Start conservative (over-flag rather than under-flag); relax with experience.

SHELL_HIGH: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(sudo|su)\b"), "privilege escalation"),
    (re.compile(r"\bmkfs\b"), "filesystem creation (destructive)"),
    (re.compile(r"\bdd\b[^\n;|&]*\b(if|of)="), "raw disk write/copy (dd)"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|disk)"), "redirect to a raw device"),
    (re.compile(r">\s*/(etc|boot|sys|proc)/"), "write to a system path"),
    (re.compile(r":\(\)\s*\{"), "fork bomb"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b|\binit\s+0\b"), "host power/state change"),
    (re.compile(r"\b(iptables|nft|ufw)\b"), "firewall/network rule change"),
    (re.compile(r"\bn(et)?cat?\b[^\n;|&]*-e\b|\bnc\b[^\n;|&]*-e\b"), "reverse-shell pattern"),
    (re.compile(r"\bchmod\b[^\n;|&]*\b777\b"), "world-writable permissions"),
    (re.compile(r"\bchown\b[^\n;|&]*\broot\b"), "ownership change to root"),
    # fetch-and-execute (curl/wget/base64 piped into a shell) — exfil/RCE shape
    (
        re.compile(r"\b(curl|wget|fetch|base64)\b[^\n]*\|\s*(sudo\s+)?(ba|z)?sh\b"),
        "fetch-and-execute (pipe to shell)",
    ),
]

SHELL_MEDIUM: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b(apt|apt-get|yum|dnf|pip3?|npm|pnpm|yarn|gem|cargo|brew|go|go install)\b"
            r"[^\n;|&]*\b(install|add|get)\b"
        ),
        "package installation",
    ),
    (re.compile(r"\bgit\s+push\b"), "pushing to a remote"),
    (re.compile(r"\b(chmod|chown)\b"), "permission/ownership change"),
    (re.compile(r"\b(curl|wget|scp|rsync|ssh|ftp)\b"), "outbound network access"),
    (re.compile(r"\b(docker|systemctl|service|kubectl|podman)\b"), "service/container control"),
    (re.compile(r"\b(kill|pkill|killall)\b"), "process termination"),
]

# Clearly read-only commands → LOW. Anchored at the start (the leading command).
SHELL_READ = re.compile(
    r"^\s*(ls|cat|less|more|head|tail|grep|rg|ag|pwd|echo|printf|wc|stat|file|which|type|"
    r"whoami|id|date|env|printenv|du|df|ps|top|htop|tree|find|sort|uniq|cut|awk|sed|jq|"
    r"git\s+(status|log|diff|show|branch|remote|config\s+--get))\b"
)


def destructive_rm(low: str) -> bool:
    """`rm` invoked with BOTH recursive and force flags (any ordering/clustering)."""
    m = re.search(r"\brm\s+([^\n;|&]*)", low)
    if not m:
        return False
    flagchars = "".join(re.findall(r"(?:^|\s)-(\w+)", m.group(1)))
    return "r" in flagchars and "f" in flagchars


def score_shell(command: str) -> tuple[SecurityRisk, str]:
    low = command.lower()
    if destructive_rm(low):
        return _H, "recursive force delete (rm -rf)"
    for pat, why in SHELL_HIGH:
        if pat.search(low):
            return _H, why
    for pat, why in SHELL_MEDIUM:
        if pat.search(low):
            return _M, why
    if SHELL_READ.match(low):
        return _L, "read-only command"
    # Shell is inherently non-trivial: an unrecognized command is MEDIUM, not LOW.
    return _M, "unrecognized shell command (cautious default for shell)"


# Plan/control meta tools: they update plan-tracker state but have NO workspace side
# effects. `submit_plan` is intercepted by the loop before the gate; `plan_step` /
# `update_plan_progress` only report capstone progress. Pin them LOW so a progress
# marker never interrupts an approved build with a confirmation gate.
META_TOOLS = frozenset({"submit_plan", "plan_step", "update_plan_progress"})


def _classify_read_only(name: str, tool_name: str) -> tuple[SecurityRisk, str] | None:
    """Return (risk, rationale) if the tool name looks read-only, else None."""
    if any(
        k in name for k in ("read", "search", "fetch", "list", "view", "get", "status", "wait")
    ):
        return _L, f"read-only tool '{tool_name}'"
    return None


def _classify_state_changing(
    name: str, tool_name: str, args: Mapping[str, object]
) -> tuple[SecurityRisk, str] | None:
    """Return (risk, rationale) if the tool name looks state-changing, else None."""
    if not any(k in name for k in ("write", "create", "edit", "save", "append", "kill")):
        return None
    path = str(args.get("path") or args.get("file") or args.get("filename") or "")
    if path.startswith("/") or ".." in path:
        return _H, f"'{tool_name}' writing outside the workspace ({path})"
    return _M, f"state-changing tool '{tool_name}'"


def _classify_deploy(name: str, tool_name: str) -> tuple[SecurityRisk, str] | None:
    """Return (risk, rationale) if the tool name looks like a deployment tool."""
    if any(k in name for k in ("deploy", "publish", "release")):
        return _H, f"deployment tool '{tool_name}'"
    return None


def _classify_destructive(name: str, tool_name: str) -> tuple[SecurityRisk, str] | None:
    """Return (risk, rationale) if the tool name looks destructive."""
    if any(k in name for k in ("delete", "remove", "destroy", "drop")):
        return _H, f"destructive tool '{tool_name}'"
    return None


def _classify_browser(
    name: str,
    tool_name: str,
    args: Mapping[str, object],
) -> tuple[SecurityRisk, str] | None:
    """Return (risk, rationale) for browser tool interactions, else None."""
    if "browse" not in name and "browser" not in name:
        return None
    act = str(args.get("action") or args.get("op") or "").lower()
    if act in ("submit", "post") or args.get("submit"):
        # Sending form data outward (exfiltration / state-changing) — HIGH, so it
        # hits the gate even when a page tries to induce it.
        return _H, f"'{tool_name}' submitting form data"
    if act in ("click", "type", "fill"):
        return _M, f"'{tool_name}' interacting with the page"
    return None


def score_other(tool_name: str, args: Mapping[str, object]) -> tuple[SecurityRisk, str]:
    """Score a non-shell tool by its name keywords and argument shape."""
    name = tool_name.lower()
    if name in META_TOOLS:
        return _L, f"plan-mode control signal '{tool_name}' (no side effects)"
    inferred = _L
    why = f"tool '{tool_name}'"

    for result in (
        _classify_read_only(name, tool_name),
        _classify_state_changing(name, tool_name, args),
        _classify_deploy(name, tool_name),
        _classify_destructive(name, tool_name),
    ):
        if result is not None:
            risk, rationale = result
            inferred = max_risk(inferred, risk)
            why = rationale

    browser_result = _classify_browser(name, tool_name, args)
    if browser_result is not None:
        risk, rationale = browser_result
        inferred = max_risk(inferred, risk)
        why = rationale

    return inferred, why


def score_other_with_base(
    tool_name: str,
    args: Mapping[str, object],
    base_risk_by_tool: Mapping[str, SecurityRisk],
) -> tuple[SecurityRisk, str]:
    """Score a non-shell tool and factor in the injected base_risk."""
    if tool_name.lower() in META_TOOLS:
        return score_other(tool_name, args)
    inferred, why = score_other(tool_name, args)
    base = base_risk_by_tool.get(tool_name)
    if base is not None:
        return max_risk(base, inferred), f"{why}; base_risk={base.value}"
    return inferred, why
