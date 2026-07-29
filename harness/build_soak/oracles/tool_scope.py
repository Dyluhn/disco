"""ToolScopeOracle (guidelines §11.7, §16).

Two distinct invariants, with two distinct evidence requirements:

  * WRITE_TOOL_ATTEMPTED_IN_PLANNING — PROVABLE FROM THE EVENT LOG ALONE. A
    mutating action (anything not on the planning-safe read/ask allowlist) appears
    before the plan was approved → the planner mutated state before approval. The
    event log carries the action + the approval status, so this needs nothing more
    than `events`.

  * WRITE_TOOL_ALLOWED_IN_PLANNING — NOT provable from events alone. §11.7 is about
    a disallowed tool remaining *in ToolScope.allowed_tools* even if hidden from the
    advertised set ("a hidden tool is still unsafe if it remains callable"). Build
    campaigns derive this evidence from the frozen per-request DISCO_INSPECT tool-
    scope snapshots. Legacy/non-inspect callers still receive a SKIP when their
    scenario does not select the assertion; selected assertions fail closed through
    ContractOracle when the capture is absent.

The product gate independently rejects a scripted mutating call in PLANNING.  The
ATTEMPTED check below proves from the event log that no mutating action reached the
executor, while the frozen scope check proves that the same tool was not callable.
"""

from __future__ import annotations

from typing import Any, TypeGuard, cast

from .. import failure_codes as fc
from ..events import (
    KIND_ACTION,
    KIND_PLAN,
    action_executed,
    action_id_of,
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
        "questions_v2",
        "clarify",
        "think",
    }
)

# ---- the ONE definition of what a captured turn's `mode` means ---------------
#
# A DISCO_INSPECT tool-scope snapshot is written once per MODEL TURN, so counting
# them by mode is the canonical turn count. Both this oracle (which adjudicates
# scope leakage per mode) and the efficiency reporter (which only counts) key on
# the constants below, so an added or renamed operating mode can never mean one
# thing to the gate and another to the report.
#
# `interactive` and `long_horizon` are the two EXECUTION modes the loop drives
# after plan approval; `planning` is the pre-approval read-only mode.
PLANNING_MODE = "planning"
EXECUTION_MODES = frozenset({"interactive", "long_horizon"})
VALID_TURN_MODES = frozenset({PLANNING_MODE}) | EXECUTION_MODES


def turn_mode_counts(tool_scopes: Any) -> dict[str, int]:
    """Planning/execution turn counts over captured tool-scope snapshots.

    Counting only — it makes NO judgement and returns no verdict, so it is safe
    to call on a failing run whose oracle result stopped early. Entries whose
    `mode` is not a recognized turn mode (malformed or a future mode) are counted
    under ``unrecognized`` rather than silently folded into either bucket: an
    unreadable capture must never read as "zero turns".
    """
    counts = {"planning": 0, "execution": 0, "unrecognized": 0}
    if not isinstance(tool_scopes, list):
        return counts
    for turn in tool_scopes:
        mode = turn.get("mode") if isinstance(turn, dict) else None
        if mode == PLANNING_MODE:
            counts["planning"] += 1
        elif mode in EXECUTION_MODES:
            counts["execution"] += 1
        else:
            counts["unrecognized"] += 1
    return counts


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


def _check_attempted_in_planning(
    events: list[dict[str, Any]],
    scenario: dict[str, Any] | None,
) -> OracleResult | None:
    """Check WRITE_TOOL_ATTEMPTED_IN_PLANNING from the event log alone."""
    if not _is_plan_gated(events, scenario):
        return None
    approvals = plan_approved_seqs(events)
    cutoff = approvals[0] if approvals else None
    for e in events:
        if kind_of(e) != KIND_ACTION:
            continue
        if cutoff is not None and seq_of(e) >= cutoff:
            continue
        name = tool_name_of(e)
        if name is not None and name not in PLANNING_SAFE_TOOLS:
            aid = action_id_of(e)
            if aid is not None and not action_executed(events, aid):
                continue
            return failing(
                _ORACLE,
                fc.WRITE_TOOL_ATTEMPTED_IN_PLANNING,
                first_broken_link="planning_mode -> mutating_action_before_approval",
                facts={
                    "tool_name": name,
                    "action_seq": seq_of(e),
                    "first_plan_approved_seq": cutoff,
                },
            )
    return passing(_ORACLE, facts={"checked": "attempted_in_planning"})


def _validate_str_list(value: Any) -> TypeGuard[list[str]]:
    """Validate a list of non-empty unique strings."""
    return (
        isinstance(value, list)
        and all(isinstance(name, str) and name for name in value)
        and len(set(value)) == len(value)
    )


def _validate_turn_shape(
    turn: dict[str, Any],
    *,
    required: bool,
    index: int,
) -> bool:
    """Validate one turn's shape. Returns True if valid, False if malformed."""
    malformed = turn.get("__malformed__")
    if malformed:
        return False
    mode = turn.get("mode")
    if mode not in VALID_TURN_MODES:
        return False
    offered = cast(list[str], turn.get("offered_tools"))
    if not _validate_str_list(offered):
        return False
    allowed = turn.get("allowed_tools")
    if not _validate_str_list(allowed):
        return False
    attempt = turn.get("attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        return False
    if turn.get("complete") is not True:
        return False
    offered_count = turn.get("offered_count")
    if not isinstance(offered_count, int) or isinstance(offered_count, bool):
        return False
    if offered_count != len(offered):
        return False
    allowed_count = turn.get("allowed_count")
    if not isinstance(allowed_count, int) or isinstance(allowed_count, bool):
        return False
    return allowed_count == len(allowed)


def _check_turn_leakage(
    turn: dict[str, Any],
    *,
    mode: str,
    allowed: list[Any],
    required: bool,
    disallowed: set[str],
    execution_denied: set[str],
) -> OracleResult | None:
    """Check one valid turn for tool-scope leakage. Returns a failure or None."""
    offered = cast(list[str], turn.get("offered_tools"))
    if not set(offered).issubset(allowed):
        return failing(
            _ORACLE,
            fc.TOOL_SCOPE_MISMATCH,
            first_broken_link="offered_tools -> allowed_tools",
            facts={"offered_but_not_callable": sorted(set(offered) - set(allowed))},
        )
    if mode != PLANNING_MODE:
        leaked_execution = [name for name in allowed if name in execution_denied]
        if leaked_execution:
            return failing(
                _ORACLE,
                fc.TOOL_SCOPE_MISMATCH,
                first_broken_link="execution_tool_scope -> allowed_tools",
                facts={"leaked_tools": leaked_execution, "mode": mode},
            )
        return None
    leaked = (
        [t for t in allowed if t in disallowed]
        if required
        else [t for t in allowed if t not in PLANNING_SAFE_TOOLS]
    )
    if leaked:
        return failing(
            _ORACLE,
            fc.WRITE_TOOL_ALLOWED_IN_PLANNING,
            first_broken_link="planning_tool_scope -> allowed_tools",
            facts={"leaked_tools": leaked},
        )
    return None


class ToolScopeOracle:
    def check(
        self,
        events: list[dict[str, Any]],
        *,
        scenario: dict[str, Any] | None = None,
        tool_scope: list[dict[str, Any]] | None = None,
    ) -> list[OracleResult]:
        results: list[OracleResult] = []

        attempted_result = _check_attempted_in_planning(events, scenario)
        if attempted_result is not None:
            results.append(attempted_result)

        if tool_scope is None:
            results.append(
                skipping(
                    _ORACLE,
                    reason=(
                        "WRITE_TOOL_ALLOWED_IN_PLANNING needs captured per-turn tool "
                        "scope (offered/allowed_tools); no selected frozen inspect "
                        "capture was supplied"
                    ),
                )
            )
        else:
            required = bool(((scenario or {}).get("assertions") or {}).get("tool_scope"))
            assertion = ((scenario or {}).get("assertions") or {}).get("tool_scope") or {}
            disallowed = assertion.get("planning_disallows") if required else None
            execution_disallowed = assertion.get("execution_disallows") if required else None
            scope_result = self._check_allowed(
                tool_scope,
                required=required,
                planning_disallows=disallowed,
                execution_disallows=execution_disallowed,
            )
            if scope_result is not None:
                results.append(scope_result)

        if not results or all(r.skipped for r in results):
            results.append(passing(_ORACLE, facts={"checked": "attempted_in_planning"}))
        return results

    def _check_allowed(
        self,
        tool_scope: list[dict[str, Any]],
        *,
        required: bool,
        planning_disallows: Any,
        execution_disallows: Any,
    ) -> OracleResult | None:
        """`tool_scope` is a list of captured per-turn scopes:
        {mode, allowed_tools: [name,...]}. While mode == "planning" no mutating
        tool may appear in allowed_tools (§11.7 — hidden-but-callable is unsafe)."""
        planning_turns = 0
        execution_turns = 0
        disallowed = set(planning_disallows) if isinstance(planning_disallows, list) else set()
        execution_denied = (
            set(execution_disallows) if isinstance(execution_disallows, list) else set()
        )
        for index, turn in enumerate(tool_scope):
            if not _validate_turn_shape(turn, required=required, index=index):
                if required:
                    return failing(
                        _ORACLE,
                        fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                        first_broken_link="inspect_trace -> tool_scope_capture",
                        facts={
                            "reason": str(turn.get("__malformed__") or "malformed tool scope"),
                            "index": index,
                        },
                    )
                continue
            mode = cast(str, turn.get("mode"))
            allowed = cast(list[Any], turn.get("allowed_tools"))
            leakage = _check_turn_leakage(
                turn,
                mode=mode,
                allowed=allowed,
                required=required,
                disallowed=disallowed,
                execution_denied=execution_denied,
            )
            if leakage is not None:
                return leakage
            if mode != PLANNING_MODE:
                execution_turns += 1
            else:
                planning_turns += 1
        if required and planning_turns == 0 and isinstance(planning_disallows, list):
            return failing(
                _ORACLE,
                fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                first_broken_link="inspect_trace -> planning_tool_scope_capture",
                facts={"reason": "no planning turn captured"},
            )
        if required and isinstance(execution_disallows, list) and execution_turns == 0:
            return failing(
                _ORACLE,
                fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                first_broken_link="inspect_trace -> execution_tool_scope_capture",
                facts={"reason": "no execution turn captured"},
            )
        return passing(
            _ORACLE,
            facts={
                "checked": "allowed_in_planning",
                "planning_turn_count": planning_turns,
                "planning_disallows": sorted(disallowed),
                "execution_turn_count": execution_turns,
                "execution_disallows": sorted(execution_denied),
            },
        )
