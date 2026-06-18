#!/usr/bin/env python3
"""Architecture size budget — fitness function (run in CI / pre-commit).

Fails red if any class/function exceeds the budget, EXCEPT a small, explicit,
*capped* allowlist of irreducible coordinators/dispatchers. This is what makes
the god-file decomposition (2026-06-16) permanent: `AgentLoop` can never regrow
to 4,703 LOC, and no new god-object can be introduced, without this gate failing.

The allowlist is the honest part: each entry is a class/function we judged
*defensibly* large (a composition-root, a dispatcher, or a wiring constructor),
capped a little above its current size so it cannot grow. To add an entry you
must justify it in review — that friction is the point.

Usage:  python3 scripts/check_arch_budget.py   (exit 1 on any violation)
"""

from __future__ import annotations

import ast
import os
import sys

CLASS_LIMIT = 800
FUNC_LIMIT = 200

# (relative-path-suffix, name) -> cap. Each is a documented exception.
ALLOW_CLASSES = {
    # Central orchestrators / composition-roots: bulk is run()-dispatch + __init__
    # wiring + lock-holding control ops + thin delegators; all methods are small.
    ("core/loop/engine.py", "AgentLoop"): 1100,
    # C6 wiring added ~42 LOC (per-conversation _artifact_mode dict + delegators
    # + _compose_build_loop branch) — same irreducible-coordinator rationale.
    # D3 +8 LOC: two per-cid queue dicts + comments.
    ("agent_server/runtime.py", "ConversationRuntime"): 1335,  # +C6 artifact_mode +P3 last_model +D3 steer queues
    # D3 +20 LOC: pop_steers/pop_injected closures + queue init/cleanup.
    ("agent_server/deep_research_service.py", "DeepResearchService"): 825,  # +D3 steer/inject wiring
}
ALLOW_FUNCS = {
    ("core/loop/engine.py", "run"): 330,            # the agent-loop dispatcher
    ("core/loop/engine.py", "__init__"): 240,       # collaborator wiring + comments
    # D3 +3 LOC: pop_steers/pop_injected params + queue cleanup in finally.
    ("deep_research_service.py", "_execute_deep_research"): 250,  # +D3 steer/inject orchestration
}


def _suffix_match(path: str, suffix: str) -> bool:
    return path.replace(os.sep, "/").endswith(suffix)


def main() -> int:
    root = os.path.join(os.path.dirname(__file__), "..", "packages")
    root = os.path.abspath(root)
    violations: list[str] = []
    for dirpath, _, files in os.walk(root):
        if "/tests" in dirpath.replace(os.sep, "/") or "__pycache__" in dirpath:
            continue
        for f in files:
            if not f.endswith(".py"):
                continue
            p = os.path.join(dirpath, f)
            rel = p.split("packages/")[-1].replace(os.sep, "/")
            try:
                tree = ast.parse(open(p, encoding="utf-8").read())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not hasattr(node, "end_lineno") or node.end_lineno is None:
                    continue
                size = node.end_lineno - node.lineno + 1
                if isinstance(node, ast.ClassDef):
                    cap = next(
                        (c for (sfx, nm), c in ALLOW_CLASSES.items()
                         if nm == node.name and _suffix_match(rel, sfx)),
                        CLASS_LIMIT,
                    )
                    if size > cap:
                        violations.append(
                            f"class {node.name} ({size} LOC > {cap}) in {rel}"
                        )
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    cap = next(
                        (c for (sfx, nm), c in ALLOW_FUNCS.items()
                         if nm == node.name and _suffix_match(rel, sfx)),
                        FUNC_LIMIT,
                    )
                    if size > cap:
                        violations.append(
                            f"function {node.name} ({size} LOC > {cap}) in {rel}"
                        )
    if violations:
        print("ARCH BUDGET FAIL — god-object(s) introduced or an allowlisted "
              "coordinator grew past its cap:\n")
        for v in sorted(violations):
            print(f"  ✗ {v}")
        print("\nFix: decompose it, OR (if genuinely an irreducible coordinator) "
              "add a justified, capped entry to scripts/check_arch_budget.py.")
        return 1
    print(f"ARCH BUDGET OK — no class > {CLASS_LIMIT} / function > {FUNC_LIMIT} "
          f"LOC outside the {len(ALLOW_CLASSES)} capped-coordinator + "
          f"{len(ALLOW_FUNCS)} dispatcher allowances.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
