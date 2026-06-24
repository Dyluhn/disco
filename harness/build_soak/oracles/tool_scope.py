"""ToolScopeOracle (guidelines §11.7, §16).

Two distinct invariants, with two distinct evidence requirements:

  * WRITE_TOOL_ATTEMPTED_IN_PLANNING — PROVABLE FROM THE EVENT LOG ALONE. A
    mutating action (anything not on the planning-safe read/ask allowlist) appears
    before the plan was approved → the planner mutated state before approval. The
    event log carries the action + the approval status, so this needs nothing more
    than `events`.

  * WRITE_TOOL_ALLOWED_IN_PLANNING — NOT provable from events alone. §11.7 is about
    a disallowed tool remaining *in ToolScope.allowed_tools* even if hidden from the
    advertised set ("a hidden tool is still unsafe if it remains callable"). The
    durable event log does NOT persist the offered/allowed tool schemas, so proving
    a write tool was ALLOWED (callable) needs the runner to capture the per-turn
    tool scope. That capture is the LATER live-runner slice (S3). Until then this
    oracle reports SKIP for the ALLOWED check when no `tool_scope` evidence is
    supplied, and validates it when it is.

Note: the *product* today does let a scripted write call FALL THROUGH and execute
in PLANNING (engine.py:841 `_gate_planning_mode` only intercepts submit_plan + a
no-tool prose turn). The ATTEMPTED check below is exactly what catches that on a
real event log; the §20 contract tests assert the rejection the product still owes.
"""

from __future__ import annotations

from typing import Any

from .. import failure_codes as fc
from ..events import (
    KIND_ACTION,
    KIND_PLAN,
    kind_of,
    plan_approved_seqs,
    seq_of,
    tool_name_of,
)
from .schema import OracleResult, failing, passing, skipping

_ORACLE = "ToolScopeOracle"

# Tools a PLANNING-mode agent may use without mutating state (Claude-Code plan
# parity): read/explore + the ask/plan gates. Anything NOT here is treated as a
# mutating tool for the planning invariant. Mirrors guidelines §15.2's
# planning_disallows / first_agent_tool_in allowlist.
PLANNING_SAFE_TOOLS = frozenset(
    {
        "submit_plan",
        "file_read",
        "file_list",
        "search",
        "extract",
        "ask_user",
        "clarify",
        "think",
    }
)


def _is_plan_gated(events: list[dict[str, Any]], scenario: dict[str, Any] | None) -> bool:
    if any(kind_of(e) == KIND_PLAN for e in events):
        return True
    if scenario:
        ec = (scenario.get("assertions") or {}).get("event_chain") or {}
        if ec.get("require_plan_before_execution"):
            return True
        if (scenario.get("assertions") or {}).get("tool_scope"):
            return True
    return False


class ToolScopeOracle:
    def check(
        self,
        events: list[dict[str, Any]],
        *,
        scenario: dict[str, Any] | None = None,
        tool_scope: list[dict[str, Any]] | None = None,
    ) -> list[OracleResult]:
        results: list[OracleResult] = []

        # --- WRITE_TOOL_ATTEMPTED_IN_PLANNING (event-only) ---
        if _is_plan_gated(events, scenario):
            approvals = plan_approved_seqs(events)
            # Everything before the FIRST approval is "in planning". With no
            # approval at all, every action so far is pre-approval.
            cutoff = approvals[0] if approvals else None
            for e in events:
                if kind_of(e) != KIND_ACTION:
                    continue
                if cutoff is not None and seq_of(e) >= cutoff:
                    continue
                name = tool_name_of(e)
                if name is not None and name not in PLANNING_SAFE_TOOLS:
                    results.append(
                        failing(
                            _ORACLE,
                            fc.WRITE_TOOL_ATTEMPTED_IN_PLANNING,
                            first_broken_link="planning_mode -> mutating_action_before_approval",
                            facts={
                                "tool_name": name,
                                "action_seq": seq_of(e),
                                "first_plan_approved_seq": cutoff,
                            },
                        )
                    )
                    break  # first broken link wins

        # --- WRITE_TOOL_ALLOWED_IN_PLANNING (needs captured tool scope) ---
        if tool_scope is None:
            results.append(
                skipping(
                    _ORACLE,
                    reason=(
                        "WRITE_TOOL_ALLOWED_IN_PLANNING needs captured per-turn tool "
                        "scope (offered/allowed_tools); not in the durable event log — "
                        "TODO live-runner slice (S3)"
                    ),
                )
            )
        else:
            allowed_violation = self._check_allowed(tool_scope)
            if allowed_violation is not None:
                results.append(allowed_violation)

        if not results or all(r.skipped for r in results):
            results.append(passing(_ORACLE, facts={"checked": "attempted_in_planning"}))
        return results

    def _check_allowed(self, tool_scope: list[dict[str, Any]]) -> OracleResult | None:
        """`tool_scope` is a list of captured per-turn scopes:
        {mode, allowed_tools: [name,...]}. While mode == "planning" no mutating
        tool may appear in allowed_tools (§11.7 — hidden-but-callable is unsafe)."""
        for turn in tool_scope:
            if str(turn.get("mode")) != "planning":
                continue
            allowed = turn.get("allowed_tools") or []
            leaked = [t for t in allowed if t not in PLANNING_SAFE_TOOLS]
            if leaked:
                return failing(
                    _ORACLE,
                    fc.WRITE_TOOL_ALLOWED_IN_PLANNING,
                    first_broken_link="planning_tool_scope -> allowed_tools",
                    facts={"leaked_tools": leaked},
                )
        return None
