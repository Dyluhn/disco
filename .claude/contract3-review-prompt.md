# Codex CODE Review — P2 / CONTRACT-3 (Contract→ToolScope compiler) — IMPLEMENTED

Inspect packages/core/src/disco/core/contract/scopes.py + packages/core/tests/test_contract_scopes.py
(+ __init__ export). Built on CONTRACT-1/2 (merged).

compile_tool_scopes(contract) → ContractToolScopes (frozen): HARD per-phase tool allowlists —
bootstrap=bootstrap.tools, edit=edit.edit_tools, repair=edit.repair_tools, verify={finalizer}, export=∅
(export TOOLS are P10; ExportContract today declares pipeline STAGE names, not tools). Phase enum +
for_phase + allowed(phase, tool) hard-membership check. Pure projection; no tool-runtime import (the
executor-side deny-out-of-scope enforcement is the later integration that consumes these, same deferral
discipline as the context runtime wiring).

Tests: 27 passed (full contract suite). Key invariants proven: a tool absent from a phase pack is
hard-excluded (bootstrap of only app_create cannot file_write); repair bounded to repair_tools; edit can't
rewrite via file_write; verify scope == the finalizer; static.site cannot file_write during EDIT; custom
permits file_write in REPAIR (declared) but not EDIT; roundtrip; every built-in compiles. basedpyright
strict 0 errors.

Judge: (a) is the projection correct + does allowed() give a genuine HARD allowlist (no leak path)? (b) is
export=∅-until-P10 + verify={finalizer} the right modeling, or a gap? (c) is deferring the executor-side
enforcement (deny out-of-scope calls) acceptable for this PR, consistent with the campaign's seam? (d) test
sufficiency for the "model cannot use generic file/shell during bootstrap unless permitted" done-when.
Return APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE + REASONS + REQUIRED_REVISIONS.
