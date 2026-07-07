> **SUPERSEDED / HISTORICAL (as of 2026-07-07).** June-era assist-pipeline → ModelExecutionPolicy refactor plan, frozen mid-execution.
> Current status of record: `docs/disco-project-state.md` (master), `docs/disco-status-and-remaining.md` (features + remaining), `sec-work-remaining/disco-security-state.md` (security). This file is kept for history and may contain stale claims.

# Assist-pipeline refactor → ModelExecutionPolicy (work-order plan)

**Decision (Dylan, 2026-06-20):** FULL refactor in ONE pass. Codex architecture verdict =
SALVAGEABLE-WITH-REFACTOR: rebuild the assist *control plane* as a single resolved policy
object; keep the F-behaviors. Execute via the harness flow: plan → codex approval → fan out
parallel **Sonnet** subagents (worktree-isolated; >3 orders so not the ≤3 MiniMax cap).

## Hard constraints
- **BEHAVIOR-PRESERVING.** Restructure the control plane; do NOT flip any model's tier in this
  pass. For the current config (local Qwen driver, `tier=None`) the resolver must still yield
  `assist=True` exactly as today. Un-sandbagging Qwen = a later one-line `tier="standard"` flip
  Dylan opts into separately.
- **`standard` tier is a provable no-op.** Every weak-model gate stays `if policy.assist`, so a
  standard-tier run is byte-identical to the pre-assist path.
- Disco's gates are the completion gate: full `pytest` (core+tools+agent-server) + `ruff` +
  frontend `tsc`/`eslint`/`vitest` all green; then codex review; then live both-tiers proof.

## The single source of truth (already drafted — Order 0)
`core/llm/exec_policy.py`:
```python
@dataclass(frozen=True)
class ModelExecutionPolicy:
    tier: Literal["standard","weak"] = "standard"
    anchored_edit: bool = True
    @property
    def assist(self) -> bool: return self.tier == "weak"
    @property
    def withheld_tools(self) -> frozenset[str]:   # reconciles the TWO former classifiers
        out=set()
        if not self.anchored_edit: out.add("file_str_replace")
        if self.tier=="weak": out.update({"plan_step","update_plan_progress"})
        return frozenset(out)
def resolve_policy(*, assist_override, entry_tier, hosting_weak_default, anchored_edit) -> ModelExecutionPolicy
```
Precedence: conv assist-toggle > `ModelEntry.tier` > hosting heuristic (local&&!openrouter).

## FIXED INTERFACE CONTRACTS (every order codes against these — enables parallelism)
*(revised per codex plan review — naming collision, single-source, exact interfaces, defaults)*
1. `runtime_settings._effective_policy(cid) -> ModelExecutionPolicy` is the **ONLY** place that
   reads `ModelEntry.tier` AND `Requirement.ANCHORED_EDIT`. `_effective_assist(cid)` becomes a
   back-compat shim `= self._effective_policy(cid).assist`. The current separate anchored-edit
   path in `runtime.py` (~:1198) is **DELETED**, not paralleled.
2. **NAME COLLISION FIX:** `AgentLoop` ALREADY has `policy: ConfirmationPolicy` (engine.py:448,
   `self.policy`). The new arg is **`model_policy: ModelExecutionPolicy = ModelExecutionPolicy
   .standard()`** (defaulted → covers Deep Research + test/script call sites with no edit),
   stored `self._model_policy`. `self._assist` becomes a read-only property `= self._model_policy
   .assist` (every existing `if self._loop._assist:` collaborator UNCHANGED). `StuckDetector` →
   `StuckDetector(stuck_thresholds, assist=model_policy.assist)` (fixes dead F6).
3. `DefaultToolExecutor(..., model_policy: ModelExecutionPolicy = ModelExecutionPolicy.standard())`
   replaces `assist: bool` (defaulted → covers test/script call sites). `ctx.assist =
   model_policy.assist`. `available_tools()` additionally drops `model_policy.withheld_tools`.
   The default `conversation_id` literal `"conv"` is replaced with a real per-instance id.
4. `agent_scope(*, model_policy: ModelExecutionPolicy)` — ONE exact signature (NOT "policy or
   withheld_tools"). It computes `advertised_tools` and narrows by `model_policy.withheld_tools`.
   No runtime `agent_scope(model_caps=...)` path remains.
5. `CompletionRequest.assist` stays as a wire field, ALWAYS `= model_policy.assist`; not retired
   this pass (bounds blast radius).
6. **Defaults everywhere** = `ModelExecutionPolicy.standard()` (the no-op), so any unswept
   call site is safe + behavior-identical to assist-OFF.

## Work orders (Wave 1 runs in PARALLEL after Order 0 lands on the branch)

### Order 0 — Foundation [SEQUENTIAL, lands first] — owner: orchestrator
Files: `core/llm/exec_policy.py` (done), `core/llm/config.py` ModelEntry.tier (done),
`core/llm/__init__.py` exports (done), `agent_server/runtime_settings.py` `_effective_policy`
+ `_effective_assist` shim, new `packages/core/tests/test_exec_policy.py` (resolve_policy
precedence + withheld_tools + standard-no-op). Accept: new unit test green; `_effective_assist`
returns identical values to pre-change for current config (behavior-preserving).

### Order A — Loop threading + F6 — owner: Sonnet #1
Files: `core/loop/engine.py` ONLY. Add `model_policy: ModelExecutionPolicy =
ModelExecutionPolicy.standard()` to `AgentLoop.__init__` (do NOT touch the existing
`policy: ConfirmationPolicy`); `self._model_policy = model_policy`; make `self._assist` a
`@property` returning `self._model_policy.assist` (remove the old `self._assist = assist` line);
`StuckDetector(stuck_thresholds, assist=model_policy.assist)`. Verify `agent.py`/`driver.py`
still read `self._loop._assist` (now the property) unchanged — do not edit them. Update AgentLoop
constructions in `packages/core/tests/**` that passed `assist=` → `model_policy=` (or rely on the
default). Accept: core loop tests green; new test asserts a weak `model_policy` makes
`StuckDetector` emit F6 `rewrite_directive` (F6 no longer broken-closed).

### Order B — Tools threading + tool-surface reconcile + F3 scope + weak-prompt — owner: Sonnet #2
Files: `tools/executor.py` (replace `assist: bool` with `model_policy=...standard()`;
`ctx.assist = model_policy.assist`; `available_tools()` drops `model_policy.withheld_tools`;
replace the `"conv"` default conversation_id), `tools/registry.py` (`agent_scope(*, model_policy)`
exact interface — compute advertised set then subtract `model_policy.withheld_tools`),
`tools/builtin/files.py` (`_read_state` → executor-scoped state, cleared on executor/conversation
teardown; never the shared `"conv"` bucket), `core/llm/prompts.py` (the small-model execution
prompt AND `_AGENT_PLANNING_CAPABILITY_BLOCK` ~:418 must NOT mention withheld tools — when weak,
omit `plan_step`/`update_plan_progress`; when `!anchored_edit`, omit `file_str_replace`). Update
`DefaultToolExecutor(assist=)`/`agent_scope(model_caps=)` call sites in `tools` + `core` tests.
Accept: tools tests green; weak policy `available_tools` excludes plan_step+update_plan_progress;
standard advertises them; non-anchored standard withholds ONLY file_str_replace; `_read_state`
no longer module-global; prompt-contamination test passes (weak prompt names no withheld tool).

### Order C — Runtime threading + ATOMIC staleness kill — owner: Sonnet #3
Files: `agent_server/runtime.py`, `agent_server/runtime_settings.py`,
`agent_server/routes/conversations.py`, `agent_server/routes/_common.py`. (a) Build
`model_policy = self._effective_policy(cid)` ONCE in `_compose_build_loop`/executor construction;
pass to `AgentLoop(model_policy=)` + `DefaultToolExecutor(model_policy=)`; DELETE the separate
anchored path (~runtime.py:1198) so `_effective_policy` is the only tier/caps reader.
(b) **Atomic staleness contract (revised per codex r2 — NO re-entrant deadlock):** add a
per-conversation `asyncio.Lock` held at ONE level only. A single async helper
`async def apply_settings_change(cid, *, model_override?, assist?) -> bool` acquires the lock
ONCE, then calls the INNER, NON-locking primitives (rename current setters to
`_set_model_override_unlocked`/`_set_assist_unlocked`; the public `set_*` keep working for
non-route callers but the route uses the atomic helper). **The compose+register step runs UNDER the same per-cid lock** so PATCH and kick can't interleave
mid-compose: `kick()` acquires the lock, composes the loop AND registers `_loops[cid]`/`_tasks[cid]`,
then releases — so by the time it returns the loop is visible. (NB: `kick()`/`_loop_for()` are sync
at runtime.py:1369 + schedule `_run_with_persistence` via `create_task` at runtime.py:1382, and the
durable `RUNNING` event is emitted LATER inside `AgentLoop.run()` at engine.py:1062 — so the lock,
not the RUNNING event, is the serialization point.) Setters must NOT re-acquire the lock.
**Pristine check (revised r3 — events AND in-memory composition):** `_conversation_is_pristine(cid)`
is non-pristine (→ PATCH 409, settings UNMUTATED) iff EITHER (i) the EVENT STORE has a WORK/RUN
event — any `PlanEvent`, non-bookkeeping `ActionEvent`, AGENT `MessageEvent`, or run-start
`StatusEvent` — OR (ii) a loop is already composed (`cid in _loops`) or a run task is live
(`cid in _tasks`). Clause (ii) closes the compose→RUNNING gap codex flagged. SETUP events do NOT
block: pre-kick uploads append ENVIRONMENT `MessageEvent` + `DatasourceEvent` (files.py:191) and
must stay patchable — only AGENT messages / work / run events count. (c) `UpdateSettingsBody` gains
`assist: bool | None`; the route applies it via `apply_settings_change` pre-kick. Accept:
agent-server tests green; stale-policy test + no-deadlock test + the compose-gap concurrent test
(loop composed + run task scheduled but RUNNING not yet emitted → PATCH must 409) below.

### Order D — Frontend badge + consistency tests + cleanup — owner: Sonnet #4
Files: `core/loop/turn_control.py` (DELETE `gate_plan_step_lag` + its caller). Frontend is
**DISPLAY-ONLY this pass** (no user-facing assist toggle yet — tier is server-derived): add
`assist?: boolean` to the state extras type in `frontend/src/types/agent.ts`; `hooks/
useBuildStream.ts` stores `extras.assist` like it already does `autonomous`;
`components/build/AgentStatusBar.tsx` renders an "Assist"/"Standard" badge from it. Do NOT wire a
PATCH-assist control in `useBuild.ts` (the backend `UpdateSettingsBody.assist` from Order C
exists for the pre-kick/programmatic path; no UI sender this pass). NEW
`packages/agent-server/tests/test_policy_consistency.py`. Accept: badge renders the live
server-derived tier (screenshot); consistency + no-contamination + sweep tests green; FE
tsc/lint/vitest green.

## ACCEPTANCE GATES (codex-required — all must pass before "done")
- **Consistency (both tiers):** for a weak AND a standard conversation, assert badge `is_assist`,
  loop `_model_policy.assist`, executor `ctx.assist`, `available_tools` membership, prompt variant,
  and `CompletionRequest.assist` ALL agree.
- **Stale-policy:** PATCH on a PRISTINE conversation (no work/run events; pre-kick UPLOADS — env
  MessageEvent + DatasourceEvent — stay patchable) → recomposes correctly; PATCH after ANY
  PlanEvent / real ActionEvent / AGENT MessageEvent / run-start status → 409 + unmutated; PATCH
  when a loop is already composed (`_loops`) or a run task is live (`_tasks`) → 409 even before
  the RUNNING event exists (the compose-gap test); **concurrent PATCH+kick** under the per-cid
  lock cannot leave badge ≠ loop/executor policy AND cannot deadlock (non-locking inner setters).
- **No-contamination:** weak policy neither advertises NOR prompts withheld tools (plan_step,
  update_plan_progress, file_str_replace-when-withheld); standard advertises the progress tools;
  non-anchored standard withholds ONLY file_str_replace.
- **Constructor sweep (grep gates):** no remaining `AgentLoop(... assist=...)`, no
  `DefaultToolExecutor(... assist=...)`, no runtime `agent_scope(model_caps=...)`. PLUS a
  POSITIVE gate: every PRODUCTION `AgentLoop(` / `DefaultToolExecutor(` construction in the
  agent-server runtime compose paths EXPLICITLY passes `model_policy=` (defaults are for
  tests/scripts/Deep-Research legacy only — a missing production thread-through must fail this
  gate, not silently resolve to standard()).
- **Behavior-preserving:** current local-Qwen config still resolves `assist=True`.

## Merge + verify (orchestrator)
Pull each order (disjoint files → low conflict), run FULL pytest (core+tools+agent-server) + ruff
+ FE tsc/eslint/vitest, then codex re-review of the merged diff, then live both-tiers proof
(standard = capable prompt + progress tools + no compensations; weak = small prompt + tools
withheld + compensations on + F6 live). Update memory [[disco-assist-pipeline-audit]] → DONE.
