# God-FUNCTION decomposition plan (2026-06-16)

**Goal / definition of done:** every function ≲ ~80 LOC, every class ≲ ~500 LOC,
full regression green + live smokes pass. Until then we are NOT within good
engineering practice. The metric is **max function/class size**, not file LOC.

Current offenders: `AgentLoop.run()` 1,875 · `ConversationRuntime` 3,405 ·
`create_app` 1,454 · `AgentLoop` class 4,613.

## Execution strategy — why a tight pipeline, not naive concurrency
Everything imports `core` (engine); `agent-server` imports `core` + `runtime`.
So concurrently *editing* engine.py / runtime.py / app.py contaminates each
other's test runs (a mid-edit broken intermediate in engine.py fails the
agent-server suite a parallel lane is gating on) — which would make regression
non-deterministic, the opposite of what we need. Worktree isolation is rejected
too: the uv editable-install resolves to the *main* tree, so a worktree's tests
would silently run against unmodified code (a verification-integrity hazard).
Therefore: **pipeline by file**, each god-function fully landed + regression-green
before the next. Parallelism is used WHERE it's safe: (a) the create_app router
modules each write their own new file (one concurrent executor lane), (b) within
a lane the independent gate/arm extractions.

Order: **create_app → ConversationRuntime → run()** (most-mechanical first to
prove the live-smoke workflow; the riskiest, run(), last and hand-driven).

Gate every step: `.venv/bin/python3 -m pytest packages/<pkg>/tests/ -q -p no:warnings`
(use EXIT CODE as truth — summary line buffers away). Plus a live smoke per lane.

---
## TARGET A — `create_app` (app.py, 1,454) → 15 domain routers  [LANE RUNNING]
44 routes → `routes/{files,conversations,ws,share,projects,report,mcp,schedules,
preview,storage,activity,sessions,health,export,models}.py`, each
`make_<domain>_router(store, runtime) -> APIRouter` (handlers close over the
factory params, identical paths). Shared closures `_reject_if_imported`,
`_declared_artifacts`, `_preview_upstream_resolver` + constants
`_MAX_SESSION_TAIL_CHARS`,`_WORKSPACE_PREFIXES`,`_ARTIFACT_TYPES`,`_AUDIO_MEDIA_TYPES`
→ `routes/_common.py` (module level). `pump_events`/`pump_ephemeral` stay with ws.
`create_app` → setup + lifespan + 15 include_router (~150 LOC). Gate: agent-server
suite + live endpoint smoke.

---
## TARGET B — `ConversationRuntime` (runtime.py, 3,405) → 5 collaborators
Seam: each collaborator constructed once in `__init__`, methods become one-line
delegators (public API preserved). The composition root STAYS on the runtime
(`__init__`, `_router_now`, `_loop_for`, `_compose_build_loop`, `_build_broker`,
`_retrieval_handlers`, `_build_sandbox_spec`, `kick`, `_run_with_persistence`).
Extraction order (isolated→entangled):
1. **McpManager** → `mcp_manager.py` (9 `_mcp_*` methods; owns 6 `_mcp_*` attrs; `_cap_handlers` via getter, not owned). Gate: test_mcp_*.
2. **ShareService** → `share_service.py` (share_export/import, create/lookup/list/revoke_share_link; stateless over store + getters). Gate: test_share*, test_share_import.
3. **ResumeService** → `resume_service.py` (_condense_trailing_degeneracy, _reconstruct_resume_context, resume_conversation; back-ref). Gate: test_resume*.
4. **LifecycleManager** → `lifecycle.py` (on_connect/disconnect, suspend, idle-sweep, orphan-reconcile, rehydrate, snapshot; owns `_connections`,`_suspend_tasks`). Gate: test_idle_suspend, test_lifecycle, test_sessions_degrade.
5. **DeepResearchService** → `deep_research_service.py` (LAST, most entangled — 12 DR methods + export_report/_render_docx; owns `_depth`,`_research_providers`,`_cancel_flags`; depends on McpManager being stable). Gate: test_research*, test_report*.
Gate after each: full agent-server suite + a live DR/agent smoke.

---
## TARGET C — `AgentLoop.run()` (engine.py:3185-5060, 1,875; ~1,784-line while-body)  [HAND-DRIVEN]
**HARD CONSTRAINTS:** (1) entire body under `async with self._lock:` — NO extracted
method may touch `self._lock`; `action_to_execute` stays a skeleton local across
the lock boundary. (2) `_normalize_finish_step` mutates `step` and FALLS THROUGH
(returns the mutated step) — not a terminator. (3) `_drive_step`'s
`LLMContextWindowExceeded` two-level try → must map to OUTER-loop `continue` (don't
merge try levels). (4) prologue 3185-3305 + (a)/(b) early-returns STAY (the `return
state` local-vs-`get_state()` difference is subtle).

**Step 0 — `loop/control.py`:** `class Disp(Enum): CONTINUE; HALT; FALLTHROUGH`.
Gates that re-poll return `(Disp, events)`; others return `Disp`.
**Simplification:** `escape_seq`/`acted_since_escape`/`fresh_session`/`in_escape`/
`escape_temp` are pure functions of `events` — recompute inside `_drive_step`, don't thread.

Extractions (each a verbatim block-move, `continue`→`return Disp.CONTINUE`,
`return await self.get_state()`→`return Disp.HALT`; read/write sets per the
architect's table in the run): `_post_noop_valve` (the 7×-repeated tail) ·
`_gate_f4_bootstrap` · `_gate_stuck` · `_gate_circuit_breaker` ·
`_gate_plan_step_lag` · `_gate_bookkeeping_streak` · `_drive_step` (+leaves
`_known_tool_names_for_requery`, `_unknown_tool_requery_hint`) ·
`_handle_{notify_user,remember,serve,delegate_explore}` (+ `_refuse_fresh_session`) ·
`_normalize_finish_step` (+`_resolve_verify_command`) · `_gate_planning_mode` ·
`_handle_finish_path` (=`_gate_execution_nudge`+`_gate_browser_verify`+`_finalize_finish`) ·
`_handle_noop_step` · `_gate_ask_fresh_session` · `_gate_autonomous_ask_stall` ·
`_handle_propose_plan_update` · `_handle_clarify`/`_handle_ask_user` ·
`_gate_hard_deny` · `_gate_risk_confirm`. Skeleton `run()` → ~110-130 LOC.
NOTE: `plan_step` is NOT a dispatch arm (regular tool, falls to action path).
Gate: full core suite (33 loop tests drive run() E2E) + live agent smoke.

---
## Progress
- [x] Plan built (this doc) — architect (Opus) + create_app AST analysis.
- [ ] A create_app — executor lane running.
- [ ] B ConversationRuntime — pending A.
- [ ] C run() — pending B; hand-driven.
- [ ] Final: re-measure max fn/class size → loop until "good practice = YES".
