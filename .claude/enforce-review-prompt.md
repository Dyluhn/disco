# Codex CODE Review — CONTRACT-ENFORCE (executor-side per-phase contract scope) — IMPLEMENTED

This retires the long-tracked CONTRACT-3 / TOOL-1#4 enforcement follow-up: it actually DENIES an
out-of-phase tool at the dispatch boundary.

Inspect:
NEW core/contract/enforce.py: ScopeDecision + DANGEROUS_TOOLS + decide_tool_in_scope(scopes, phase, tool) +
ContractScopeGuard(scopes, phase_provider) / .for_contract(contract, phase_provider) / .check(tool).
Governed = a dangerous escape-hatch mutator (file_write/file_edit/file_replace_lines/file_delete/shell/
bash/exec) OR a tool the contract scopes to bootstrap/edit/repair/export. The VERIFY finalizer is excluded
(a control signal — must stay callable). Ungoverned utilities (think/preview/read-only) always pass.
CHANGED tools/executor.py: optional scope_guard param; in execute(), AFTER tool resolution (unknown tools
still report unknown_tool) and before arg-validation/run, a denied tool returns _fail(kind 'denied',
message 'out of contract scope: …names the phase + permitted tools'). guard=None ⇒ no enforcement (a plain
agent run, unchanged). Exported from contract/__init__.

Tests: 12 (7 core decisions + 5 executor integration). Proven: file_write DENIED in appkit EDIT (and the
write never lands on the fake FS), allowed in REPAIR; app_create (bootstrap tool) denied mid-EDIT; shell
denied even though appkit never names it; think/preview/file_read/finalizer always pass; the guard reflects
the LIVE phase; no-guard executor is unchanged. Existing executor suites (elision/scope/assist/validation)
+ appkit/agent tools all still green (74 in this run). basedpyright strict 0 errors.

Judge: (a) is the dispatch-boundary placement + the 'denied' outcome correct + safe (write truly blocked
pre-execution, recoverable, model told what's permitted)? (b) is the governed-set definition right — does
excluding verify + passing utilities while gating dangerous/scoped tools correctly enforce the contract
without stranding the run? (c) backward-compat: guard=None changes nothing? (d) any bypass (a mutator not in
DANGEROUS_TOOLS, an alias)? (e) test sufficiency. Return APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE + REASONS
+ REQUIRED_REVISIONS.
