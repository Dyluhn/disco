"""CONTRACT-ACTIVATE harvest (2026-07-10): replay soak dossiers through the REAL
scope compiler + phase tracker and report every would-deny.

For each run dossier (events.jsonl), extract the ordered successful tool calls,
simulate BuildPhaseTracker under a target contract kind, and evaluate
decide_tool_in_scope with the tool's real read_only metadata (from the builtin
registry) — exactly what enforcement would do. Output: per-kind, per-phase deny
counts, so the contract packs get updated from EVIDENCE, not guesses.

    python -m harness.build_soak.harvest_scopes test-record/build-soak-overnight --kind static.site
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from disco.core.contract import BuildContractRegistry, ContractKind
from disco.core.contract.enforce import decide_tool_in_scope
from disco.core.contract.phase import BuildPhaseTracker
from disco.core.contract.scopes import compile_tool_scopes
from disco.tools import build_default_registry


def _read_only_map() -> dict[str, bool]:
    reg = build_default_registry()
    return {name: reg._tools[name].definition.read_only for name in reg.names()}


def _successful_calls(events_path: Path) -> list[str]:
    """Ordered tool names whose paired observation reported success."""
    actions: dict[str, str] = {}  # call_id -> tool_name
    order: list[tuple[str, str]] = []  # (call_id, tool)
    successes: set[str] = set()
    for line in events_path.open():
        e = json.loads(line)
        tc = e.get("tool_call") or {}
        if tc.get("tool_name"):
            actions[tc.get("call_id", "")] = tc["tool_name"]
            order.append((tc.get("call_id", ""), tc["tool_name"]))
        tr = e.get("tool_result") or {}
        if tr.get("tool_name") and tr.get("success"):
            successes.add(tr.get("call_id", ""))
    return [tool for cid, tool in order if cid in successes]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("out_root")
    p.add_argument("--kind", default="static.site")
    args = p.parse_args(argv)

    contract = BuildContractRegistry.default().get(ContractKind(args.kind))
    assert contract is not None
    scopes = compile_tool_scopes(contract)
    ro = _read_only_map()

    denies: dict[str, Counter] = defaultdict(Counter)  # phase -> tool -> count
    deny_runs: dict[tuple[str, str], set[str]] = defaultdict(set)
    runs = 0
    for events in sorted(Path(args.out_root).glob("*/conversations/*/events.jsonl")):
        run_id = events.parts[-4]
        calls = _successful_calls(events)
        if not calls:
            continue
        runs += 1
        tracker = BuildPhaseTracker(contract)
        for tool in calls:
            is_mut = None if tool not in ro else (not ro[tool])
            d = decide_tool_in_scope(scopes, tracker.current(), tool, is_mutating=is_mut)
            if not d.allowed:
                denies[tracker.current().value][tool] += 1
                deny_runs[(tracker.current().value, tool)].add(run_id)
            tracker.note_tool_success(tool)

    print(f"kind={args.kind} runs_replayed={runs}")
    if not denies:
        print("ZERO would-denies — packs already cover real usage.")
        return 0
    for phase in ("bootstrap", "edit", "repair", "verify", "export"):
        if phase not in denies:
            continue
        print(f"\n[{phase}] would-DENY:")
        for tool, n in denies[phase].most_common():
            nr = len(deny_runs[(phase, tool)])
            print(f"  {tool:24s} {n:4d} calls across {nr} runs")
    return 1


if __name__ == "__main__":
    sys.exit(main())
