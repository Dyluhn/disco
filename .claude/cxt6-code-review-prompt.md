# Codex CODE Review — PR CXT-6 (todo.md as active working memory) — IMPLEMENTED

Review the ACTUAL CODE. CXT-1..5 merged. Design (from a scout): NO new tool — reuse the existing
context_memory(write, kind=todo) for minor agent updates; CXT-6 adds (1) a pure helper
render_plan_as_todo_markdown(plan) and (2) an approval hook that auto-seeds .disco/context/todo.md.
Scope-change detection (signals.is_revision_intent → _enter_revision_planning) is already correct and is
NOT changed; a revised-plan re-approval re-seeds todo.md.

Inspect:
- packages/core/src/disco/core/loop/context_builder.py (render_plan_as_todo_markdown)
- packages/core/src/disco/core/loop/engine.py (approve_plan now calls _seed_todo_from_plan AFTER
  _arm_dod_from_plan; _seed_todo_from_plan is best-effort: skips when executor.sandbox is None, wraps in
  try/except so a seed error NEVER blocks approval; uses ArtifactMemoryStore(sbx).seed_todo). ALSO fixed a
  PRE-EXISTING (confirmed via git-stash) reportAssignmentType in _arm_dod_from_plan's file_preds
  comprehension by narrowing through a local.
- packages/core/tests/test_todo_memory.py

PlanEvent stays the approved contract (unchanged); todo.md is the durable execution memory. update_plan_progress
(event-log/UI progress) is independent and untouched — no conflict.

Test status: 18 passed (CXT-6 + plan_approval + dod_gate regressions); basedpyright strict 0 errors on
engine.py + context_builder.py. Judge: correctness, best-effort safety (approval never blocked), no conflict
with update_plan_progress, the pre-existing-fix is behavior-preserving, no false affordance. Return
APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE + REASONS + REQUIRED_REVISIONS.
