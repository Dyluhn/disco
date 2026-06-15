"""Analyzer strategies — security-analyzer-contract.md §4.

- `RuleBasedAnalyzer` (§4.1): fast, deterministic, no model call; the always-on
  floor. Parses the shell tool's raw command for known-dangerous shapes and
  factors other tools' base_risk + argument shape.
- `LLMBasedAnalyzer` (§4.2): contextual judgement via an injected sync `scorer`;
  ADVISORY + ADDITIVE only (it can raise caution via the ensemble, never lower).
- `EnsembleAnalyzer` (§4.3): composes analyzers, returns the MOST cautious verdict
  via `max_risk`; runs the LLM only when the rule-based result warrants it.

The interface (§3) is the contract; the rule list / prompt are [INTERIOR] and
tunable. The strategy roles + ensemble behavior are contractual.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping

from ..events import ActionEvent, SecurityRisk
from .assessment import RiskAssessment
from .risk import at_or_above, max_risk

# A sync risk scorer (the LLM analyzer's injected dependency). The real one
# bridges to the router; tests inject a fake. Kept sync so the loop's assess()
# boundary stays sync (contract §3 [VERIFY]); async router scoring is deferred.
Scorer = Callable[[ActionEvent], RiskAssessment]

_H = SecurityRisk.HIGH
_M = SecurityRisk.MEDIUM
_L = SecurityRisk.LOW
_U = SecurityRisk.UNKNOWN


# ---- shell-command rules [INTERIOR] -----------------------------------------
# Start conservative (over-flag rather than under-flag); relax with experience.

_SHELL_HIGH: list[tuple[re.Pattern[str], str]] = [
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

_SHELL_MEDIUM: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\b(apt|apt-get|yum|dnf|pip3?|npm|pnpm|yarn|gem|cargo|brew|go|go install)\b"
                   r"[^\n;|&]*\b(install|add|get)\b"),
        "package installation",
    ),
    (re.compile(r"\bgit\s+push\b"), "pushing to a remote"),
    (re.compile(r"\b(chmod|chown)\b"), "permission/ownership change"),
    (re.compile(r"\b(curl|wget|scp|rsync|ssh|ftp)\b"), "outbound network access"),
    (re.compile(r"\b(docker|systemctl|service|kubectl|podman)\b"), "service/container control"),
    (re.compile(r"\b(kill|pkill|killall)\b"), "process termination"),
]

# Clearly read-only commands → LOW. Anchored at the start (the leading command).
_SHELL_READ = re.compile(
    r"^\s*(ls|cat|less|more|head|tail|grep|rg|ag|pwd|echo|printf|wc|stat|file|which|type|"
    r"whoami|id|date|env|printenv|du|df|ps|top|htop|tree|find|sort|uniq|cut|awk|sed|jq|"
    r"git\s+(status|log|diff|show|branch|remote|config\s+--get))\b"
)


def _destructive_rm(low: str) -> bool:
    """`rm` invoked with BOTH recursive and force flags (any ordering/clustering)."""
    m = re.search(r"\brm\s+([^\n;|&]*)", low)
    if not m:
        return False
    flagchars = "".join(re.findall(r"(?:^|\s)-(\w+)", m.group(1)))
    return "r" in flagchars and "f" in flagchars


# Cluster 3 — HARD DENY: catastrophic, never-allowed commands. Distinct from
# HIGH (which routes to human confirmation): these are refused outright by the
# loop BEFORE the confirm gate — no approval, no policy, no LLM can run them.
# OS-level enforcement (egress/filesystem isolation) is the deeper layer; this
# is the command-level non-negotiable floor.
_SHELL_DENY: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bmkfs\b"), "format a filesystem (irreversible)"),
    (re.compile(r"\bdd\b[^\n;|&]*\bof=/dev/(sd|nvme|hd|disk|vd)"), "raw write to a disk device"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|disk|vd)"), "redirect to a raw disk device"),
    (re.compile(r":\(\)\s*\{\s*:?\s*\|?\s*:?\s*&?\s*\}\s*;?\s*:"), "fork bomb"),
    (
        re.compile(r"\brm\b[^\n;|&]*\s-[rfRF]*\s*(/|/\*)(\s|$)"),
        "recursive delete of the root filesystem",
    ),
]


def hard_deny_reason(command: str) -> str | None:
    """Return a reason string if a shell command is HARD-DENIED (never runnable),
    else None. Pure + deterministic. The loop refuses denied actions outright."""
    low = command.lower()
    for pat, why in _SHELL_DENY:
        if pat.search(low):
            return why
    return None


def _score_shell(command: str) -> tuple[SecurityRisk, str]:
    low = command.lower()
    if _destructive_rm(low):
        return _H, "recursive force delete (rm -rf)"
    for pat, why in _SHELL_HIGH:
        if pat.search(low):
            return _H, why
    for pat, why in _SHELL_MEDIUM:
        if pat.search(low):
            return _M, why
    if _SHELL_READ.match(low):
        return _L, "read-only command"
    # Shell is inherently non-trivial: an unrecognized command is MEDIUM, not LOW.
    return _M, "unrecognized shell command (cautious default for shell)"


# ---- the rule-based analyzer ------------------------------------------------


class RuleBasedAnalyzer:
    """[CONTRACT role; INTERIOR rules] Fast, deterministic, no model call — the
    always-on floor under every action. For the `shell` tool it parses the raw
    command; for other tools it factors the injected `base_risk` (the tool/sandbox
    layer's static hint, passed in because `core` cannot import `tools`) and the
    argument shape. Sync + total: any internal error returns UNKNOWN (cautious),
    never raises."""

    name = "rule_based"

    def __init__(self, base_risk_by_tool: Mapping[str, SecurityRisk] | None = None) -> None:
        self._base = dict(base_risk_by_tool or {})

    def assess(self, action: ActionEvent) -> SecurityRisk:
        return self.assess_detailed(action).risk

    def assess_detailed(self, action: ActionEvent) -> RiskAssessment:
        try:
            risk, rationale = self._score(action)
        except Exception as exc:  # noqa: BLE001 — fail toward caution, never crash the gate
            return RiskAssessment(
                risk=_U,
                rationale=f"rule analyzer error: {type(exc).__name__}",
                analyzer=self.name,
                self_assessed=action.self_assessed_risk,
            )
        return RiskAssessment(
            risk=risk,
            rationale=rationale,
            analyzer=self.name,
            self_assessed=action.self_assessed_risk,
        )

    def _score(self, action: ActionEvent) -> tuple[SecurityRisk, str]:
        tc = action.tool_call
        if tc.tool_name in {"shell", "shell_exec"}:
            # The tool contract guarantees the shell tools surface their raw command.
            return _score_shell(str(tc.arguments.get("command", "")))
        return self._score_other(tc.tool_name, tc.arguments)

    # Plan-mode meta tools: pure control signals with no side effects. `submit_plan`
    # is intercepted by the loop before the gate; `plan_step` only marks capstone
    # progress. Pin them LOW so a progress marker never interrupts an approved build.
    _META_TOOLS = frozenset({"submit_plan", "plan_step"})

    def _score_other(self, tool_name: str, args: Mapping[str, object]) -> tuple[SecurityRisk, str]:
        name = tool_name.lower()
        if name in self._META_TOOLS:
            return _L, f"plan-mode control signal '{tool_name}' (no side effects)"
        inferred = _L
        why = f"tool '{tool_name}'"

        if any(
            k in name for k in ("read", "search", "fetch", "list", "view", "get", "status", "wait")
        ):
            inferred, why = _L, f"read-only tool '{tool_name}'"
        if any(k in name for k in ("write", "create", "edit", "save", "append", "kill")):
            inferred, why = _M, f"state-changing tool '{tool_name}'"
            path = str(args.get("path") or args.get("file") or args.get("filename") or "")
            if path.startswith("/") or ".." in path:
                inferred, why = _H, f"'{tool_name}' writing outside the workspace ({path})"
        if any(k in name for k in ("deploy", "publish", "release")):
            inferred, why = _H, f"deployment tool '{tool_name}'"
        if any(k in name for k in ("delete", "remove", "destroy", "drop")):
            inferred, why = _H, f"destructive tool '{tool_name}'"
        if "browse" in name or "browser" in name:
            act = str(args.get("action") or args.get("op") or "").lower()
            if act in ("submit", "post") or args.get("submit"):
                # Sending form data outward (exfiltration / state-changing) — HIGH, so it
                # hits the gate even when a page tries to induce it.
                inferred, why = max_risk(inferred, _H), f"'{tool_name}' submitting form data"
            elif act in ("click", "type", "fill"):
                inferred, why = max_risk(inferred, _M), f"'{tool_name}' interacting with the page"

        base = self._base.get(tool_name)
        if base is not None:
            return max_risk(base, inferred), f"{why}; base_risk={base.value}"
        return inferred, why


# ---- the LLM-based analyzer (advisory, additive) ----------------------------


class LLMBasedAnalyzer:
    """[CONTRACT role; INTERIOR prompt] Catches novel/contextual risk a rule list
    misses, via an injected sync `scorer`. ADVISORY + ADDITIVE: it only ever
    contributes to the ensemble's `max_risk`, so it can raise caution but never
    lower what the rule-based analyzer flagged (injection-resistance, §4.2). Sync
    + total: scorer errors contribute UNKNOWN, never raise."""

    name = "llm_based"

    def __init__(self, scorer: Scorer) -> None:
        self._scorer = scorer

    def assess(self, action: ActionEvent) -> SecurityRisk:
        return self.assess_detailed(action).risk

    def assess_detailed(self, action: ActionEvent) -> RiskAssessment:
        try:
            result = self._scorer(action)
        except Exception as exc:  # noqa: BLE001 — fail toward caution
            return RiskAssessment(
                risk=_U,
                rationale=f"llm analyzer error: {type(exc).__name__}",
                analyzer=self.name,
                self_assessed=action.self_assessed_risk,
            )
        # Normalize: ensure the contributor is attributed to this analyzer.
        return result.model_copy(update={"analyzer": result.analyzer or self.name})


# ---- the ensemble -----------------------------------------------------------


class EnsembleAnalyzer:
    """[CONTRACT] Composes analyzers and returns the MOST cautious verdict via
    `max_risk` (§4.3). The rule-based analyzer runs on EVERY action (the floor);
    the LLM analyzer runs only when the rule-based result warrants it (at/above a
    configurable trigger, or UNKNOWN) — so trivial reads aren't taxed with a model
    call. No analyzer can relax another's caution."""

    name = "ensemble"

    def __init__(
        self,
        rule_based: RuleBasedAnalyzer,
        llm: LLMBasedAnalyzer | None = None,
        *,
        llm_trigger: SecurityRisk = SecurityRisk.MEDIUM,
    ) -> None:
        self._rule_based = rule_based
        self._llm = llm
        self._llm_trigger = llm_trigger

    def assess(self, action: ActionEvent) -> SecurityRisk:
        return self.assess_detailed(action).risk

    def _warrants_llm(self, rule_risk: SecurityRisk) -> bool:
        # Run the (expensive) LLM only for non-trivial actions: at/above the
        # trigger, or when the rule-based analyzer was itself unsure.
        return rule_risk == _U or at_or_above(rule_risk, self._llm_trigger)

    def assess_detailed(self, action: ActionEvent) -> RiskAssessment:
        contributors: list[RiskAssessment] = []
        rb = self._rule_based.assess_detailed(action)  # always
        contributors.append(rb)
        combined = rb.risk

        if self._llm is not None and self._warrants_llm(rb.risk):
            llm = self._llm.assess_detailed(action)
            contributors.append(llm)
            combined = max_risk(combined, llm.risk)  # additive caution only

        ran = [c.analyzer for c in contributors]
        return RiskAssessment(
            risk=combined,
            rationale=f"ensemble of {', '.join(ran)} → {combined.value} (most cautious)",
            analyzer=self.name,
            contributors=contributors,
            self_assessed=action.self_assessed_risk,
        )


def build_default_analyzer(
    base_risk_by_tool: Mapping[str, SecurityRisk] | None = None,
    llm_scorer: Scorer | None = None,
    *,
    llm_trigger: SecurityRisk = SecurityRisk.MEDIUM,
) -> EnsembleAnalyzer:
    """The default ensemble: rule-based (always) + optional LLM (when warranted).
    `base_risk_by_tool` is injected by the wiring layer (which can see `tools`)."""
    rule = RuleBasedAnalyzer(base_risk_by_tool)
    llm = LLMBasedAnalyzer(llm_scorer) if llm_scorer is not None else None
    return EnsembleAnalyzer(rule, llm, llm_trigger=llm_trigger)
