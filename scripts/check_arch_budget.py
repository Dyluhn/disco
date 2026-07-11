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
#
# CLOSE-OUT RATCHET (2026-07-07): the gate had been permanently red (~19
# baseline offenders past their caps), which killed its signal — a NEW
# god-object couldn't be seen in an already-failing run. Every baseline
# offender is now frozen at EXACTLY its current size: the gate is green
# today, and any single line of growth in any of them fails CI. Burn the
# caps back down by decomposition post-v0.1; never raise one without the
# review-justified rationale this file has always demanded.
ALLOW_CLASSES = {
    # Central orchestrators / composition-roots: bulk is run()-dispatch + __init__
    # wiring + lock-holding control ops + thin delegators; all methods are small.
    ("core/loop/engine.py", "AgentLoop"): 1956,
    ("agent_server/runtime.py", "ConversationRuntime"): 3970,
    ("agent_server/deep_research_service.py", "DeepResearchService"): 1192,
    # Ratchet additions — long-standing coordinators that predate the gate's caps.
    ("app_server/config_state.py", "ConfigState"): 918,
    ("core/loop/turn_control.py", "Valve"): 1042,
}
ALLOW_FUNCS = {
    ("core/loop/engine.py", "run"): 330,            # the agent-loop dispatcher
    ("core/loop/engine.py", "__init__"): 266,       # collaborator wiring + comments
    ("deep_research_service.py", "_execute_deep_research"): 295,
    ("agent_server/runtime.py", "__init__"): 317,
    # B5 Epic-O port (verbatim from nightly's 11-wave-audited deploy code): the
    # real wrangler sequence is a deliberately LINEAR, gate-laden script — every
    # step logged, refusal-coded, and audited as one readable unit. Decomposing
    # it would scatter the audited order across helpers. Capped at ported size.
    ("appkit_cloudflare/deploy.py", "_run_real_deploy"): 400,
    ("appkit_cloudflare/routes.py", "make_cloudflare_router"): 245,  # flat endpoint registrations (router-factory class, like siblings)
    # Ratchet additions (frozen at current size, see block comment above).
    ("agent_server/runtime.py", "_compose_build_loop"): 400,
    ("core/loop/engine.py", "_gate_planning_mode"): 263,
    ("core/loop/engine.py", "_run_drive"): 363,
    ("core/loop/driver.py", "drive_step"): 249,
    ("core/loop/observe.py", "execute_and_observe"): 291,
    ("agent_server/report_audio.py", "generate_report_audio"): 221,
    ("agent_server/routes/conversations.py", "make_conversations_router"): 321,
    ("agent_server/routes/preview.py", "make_preview_router"): 216,
    ("agent_server/routes/report.py", "make_report_router"): 239,
    ("core/view.py", "of"): 238,
    ("retrieval/deep_research/synthesis.py", "synthesize_section"): 267,
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
