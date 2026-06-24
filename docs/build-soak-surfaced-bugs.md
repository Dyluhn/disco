# Build Soak — surfaced product bugs (repair-loop backlog)

The §20 bare-Build contract tests assert the SPEC contract (`docs/build-soak-guidelines.md`).
Where the product violates the spec, the test is marked `@pytest.mark.xfail(strict=True)`
with the failure code below, so the suite stays green AND the bug flips to a HARD failure
the moment the product is fixed (a strict xfail that starts passing fails the run). This is
the repair-loop backlog: **do not remove an xfail without a real product fix** (no
gate-weakening, no assertion-weakening — guidelines §18/§19).

These were surfaced by the FOUNDATION slice (deterministic harness + contract tests). The
product fixes happen in a SEPARATE later repair step — engine.py / messages.py / recitation.py
were NOT touched here.

> **Harness fail-closed fixes (NOT product bugs).** Successive codex reviews of the foundation
> found P0 **false-negatives in the adjudicator itself** — cases where it would classify a REAL
> failure as PASS. A false-green oracle is the worst outcome for this campaign, so they were
> fixed inside the harness (no product change) and pinned by
> `harness/build_soak/tests/test_fail_closed.py` (+ the per-oracle unit tests):
> 1. **DB-row payload drop** (`events.py` normalize_event): a real SQLite row
>    `{seq,kind,source,payload(JSON)}` had its `payload` dropped, hiding
>    `detail`/`tool_call`/`revision` from every predicate →
>    `WRITE_TOOL_ATTEMPTED_IN_PLANNING` / `APPROVE_PLAN_NO_EXECUTION` /
>    `NO_REPLAN_AFTER_REVISION` all read PASS in row shape. Fixed by MERGING the parsed
>    payload with the row columns; the canonical event is identical for both shapes.
> 2. **Missing-approval false finish + forged approval gate** (`oracles/event_chain.py`): the
>    approval→execution chain was only checked when `plan_approved` already existed (circular),
>    so a `PlanEvent → FINISHED` with no approval PASSED. A follow-up review found a second
>    hole — an approval with NO preceding `AWAITING_PLAN_APPROVAL` (a skipped/forged human
>    gate) also PASSED. Now the oracle enforces the FULL ordered chain for a plan-gated FINISHED
>    run: `PlanEvent (A) < AWAITING_PLAN_APPROVAL (B) < RUNNING/plan_approved (C) < execution
>    action (D)`, first-broken-link wins — B missing/out-of-order →
>    `PLAN_APPROVED_STATUS_MISSING (plan_event -> awaiting_plan_approval)`; C missing →
>    `PLAN_APPROVED_STATUS_MISSING`; D missing → `APPROVE_PLAN_NO_EXECUTION`. An AUTONOMOUS run
>    (scenario/manifest `autonomous: true`) legitimately auto-approves inline with no awaiting
>    gate (engine.py ~L855), so the B link is required for interactive runs only — this avoids
>    a NEW false-positive. Re-audit also tightened action↔observation pairing to be
>    ORDER-AWARE: a response (Observation/AgentError) must appear AFTER the action it answers,
>    and an Observation must reference an action that PRECEDES it (an out-of-order pairing is no
>    longer accepted).
> 3. **Required-evidence skipped into PASS** (`oracles/contract.py`): a scenario asserting
>    `tool_scope` with no captured tool-scope evidence used to SKIP into PASS; now it
>    FAIL-CLOSES to INVALID_RUN (insufficient evidence, §8) — the event-only
>    `WRITE_TOOL_ATTEMPTED_IN_PLANNING` check is unchanged.
> 4. **Out-of-order REVISED approval** (`oracles/revision.py`): `_first_approval_after(fseq)`
>    accepted ANY `plan_approved` after the follow-up — even one occurring BEFORE the revised
>    `PlanEvent` (a stale approval, not the revised one), so a revision flow with the approval
>    out of order PASSED. Now the oracle enforces the full ORDERED revised chain, mirroring the
>    initial chain: `followup (F) < revised PlanEvent (P, rev=prev+1) < [interactive:
>    AWAITING (W)] < RUNNING/plan_approved (C) < execution action (D)`, first-broken-link wins —
>    no revised plan → `NO_REPLAN_AFTER_REVISION`; rev ≠ prev+1 → `PLAN_REVISION_NOT_INCREMENTED`;
>    a `plan_approved` in (F,P) or a FINISHED run with no post-P approval →
>    `STALE_PLAN_USED_AFTER_FOLLOWUP`; a write before the (post-P) approval →
>    `WRITE_BEFORE_REVISION_APPROVAL`; missing AWAITING between P and C (interactive) →
>    `PLAN_APPROVED_STATUS_MISSING`; approved-no-action on FINISH → `APPROVE_PLAN_NO_EXECUTION`.
>    Same autonomous exemption as the initial chain.
>
> General principle now enforced: missing/ambiguous evidence → INVALID_RUN (never PASS); a
> required invariant whose evidence is present but violated → FAIL; only a genuinely-complete,
> genuinely-conforming run → PASS.
>
> **Final ordered-invariant audit** — every "X exists after Y" the oracles rely on is now
> enforced by SEQ ORDER relative to the other chain links, not mere existence:
>
> | # | Ordered pair (must hold by seq) | Oracle | Enforcement |
> |---|---|---|---|
> | 1 | PlanEvent(A) < AWAITING(B) [interactive] | EventChain | `any(s>first_plan)` + `any(A<s<C)` |
> | 2 | AWAITING(B) < plan_approved(C) [interactive] | EventChain | `any(first_plan<s<first_approval)` |
> | 3 | plan_approved(C) < execution action(D) | EventChain | `has_action(after_seq=last_approval)` |
> | 4 | action(X) < observation/agent_error(X) | EventChain | `_has_later_response` seq>action; obs→action origin<obs |
> | 5 | mutating action < first plan_approved (planning) | ToolScope | `seq < cutoff` (cutoff=approvals[0]) |
> | 6 | followup(F) < revised PlanEvent(P) | Revision | `_first_plan_after(fseq)` |
> | 7 | no plan_approved in (F,P) [stale] | Revision | `next(s for s if F<s<P)` |
> | 8 | mutating action ≥ revised approval | Revision | `mutated_seq < revised_approval` (C strictly after P) |
> | 9 | revised PlanEvent(P) < revised approval(C) | Revision | `_first_approval_after(p_seq)` |
> | 10 | P < AWAITING(W) < C [interactive] | Revision | `any(p_seq<s<revised_approval)` |
> | 11 | revised approval(C) < action(D) [finished] | Revision | `has_action(after_seq=revised_approval)` |
>
> `OutputTruthOracle` has no ordered-pair invariants (final-state + content checks); it uses
> `terminal_status`, which is itself order-derived (last terminal, cleared on a re-entered
> RUNNING). `HarnessValidity`/`Contract` have no ordered pairs. All oracles read content through
> the normalizer, so they inherit fix #1, and every fixture now carries the AWAITING gate so it
> represents a legitimate (ordered) approval.

| # | Failure code | Severity | Status | Test | Site | Spec |
|---|---|---|---|---|---|---|
| 1 | `WRITE_TOOL_ALLOWED_IN_PLANNING` | P0 | **FIXED** (fix-planning-gate) | `test_build_plan_contract.py::test_write_tool_in_planning_produces_recoverable_rejection` (now passing) | `engine.py:841` `_gate_planning_mode` | §11.1, §11.7, §20.1 |
| 2 | `WRITE_TOOL_ALLOWED_IN_PLANNING` | P0 | **FIXED** (fix-planning-gate) | `test_tool_rejection_recovery.py::test_disallowed_tool_call_visible_as_rejection` (now passing) | `engine.py:841` `_gate_planning_mode` | §11.3, §20.3 |
| 3 | `WRITE_BEFORE_REVISION_APPROVAL` | P1 | **FIXED** (fix-planning-gate) | `test_build_replan_contract.py::test_agent_cannot_write_before_revised_plan_approval` (now passing) | `engine.py:841` `_gate_planning_mode` (revision re-entry) | §11.4, §20.2 |
| 4 | `APPROVE_PLAN_NO_EXECUTION` | P0 | open (xfail, strict) | `test_plan_approval_execution.py::test_kick_after_approval_produces_action_or_terminal_failure` | `finish.py` `gate_execution_nudge` (~L1008, `_EXECUTION_NUDGE_CAP` release) | §11.2, §20.4 |
| 5 | `THINK_NOT_EXPOSED_IN_PLANNING` | P2 (gap) | **FIXED** (fix-think-planning) | `test_build_plan_contract.py::test_first_turn_planning_exposes_think` (xfail removed, now passing) + `test_planning_write_rejection.py::test_think_allowed_in_planning_executes_then_plans` | `runtime.py:1466` planning allowlist + `engine.py` `_gate_planning_mode` | §11.1, §15.2, §20.1 |

> **Bugs 1–3 FIXED** on branch `fix-planning-gate` (two commits): the PLANNING phase gate
> (`_gate_planning_mode`, engine.py) rejects any tool call that is not in the planning allowlist
> with a recoverable, model-visible `AgentErrorEvent` paired by `tool_call_id` (`Disp.CONTINUE`)
> — it NEVER falls through to execution, and it NEVER reaches a per-tool handler.
>
> Two hardening passes after the codex review of the first commit:
> 1. **Gate runs BEFORE the per-tool meta/finish handlers.** The first commit's check ran
>    *after* the `notify_user`/`remember`/`serve`/`delegate_explore`/`finish` dispatch, so those
>    bypassed it and could run in PLANNING. The `_gate_planning_mode` call was moved ahead of all
>    those handlers in the run loop, so the invariant is total: in PLANNING the ONLY tools that
>    ever reach a handler/execution are the planning allowlist; everything else
>    (finish/serve/remember/notify_user/delegate_explore/file_write/shell/browser/…) gets the
>    recoverable rejection. In EXECUTION mode the gate is an immediate no-op fall-through, so the
>    meta/finish handlers are unchanged.
> 2. **Allowlist derived from the read-only capability set, not `_planning_tools` alone.** The
>    gate now sources its allowlist from `Driver.planning_allowed_tool_names()`, which reuses the
>    EXACT read-only-capability ∩ name-allowlist intersection that `tools_for_step()` uses for
>    tool VISIBILITY — plus `submit_plan` (always) and the virtual `ask_user`/`clarify`. Gate and
>    advertised tools can no longer drift (a misconfigured `_planning_tools` naming a write tool
>    is still excluded by the capability backstop).
>
> The three strict-xfail markers were removed (now normal passing tests) and a dedicated
> real-loop regression suite (`test_planning_write_rejection.py`) covers every non-allowlist tool
> family (file_write/finish/serve/remember/notify_user/shell) plus the positive paths (read tools
> still execute, ask_user still halts). Bug 4 is a separate fix and remains strict-xfail.
>
> **Bug 5 FIXED** on branch `fix-think-planning` (Build Soak repair #3). `think` — a pure NO-OP,
> read-only reasoning scratchpad (`tools/builtin/think.py`, `read_only=True`) — was omitted from
> the Build planning allowlist (`runtime.py:1466`), so the just-merged planning gate (which
> intersects the allowlist with the read-only capability set) rejected it. Fix: (a) add `think`
> to the `planning_tools` frozenset (it was already `read_only=True`, so the capability backstop
> already kept it — both halves of `planning_allowed_tool_names()` now include it); (b) in
> `_gate_planning_mode`, treat `think` as a 'free' planning step (`planning_noncounting`) so it
> EXECUTES (harmless no-op) but is NOT tallied toward the explore-read cap — thinking is reasoning,
> not context-gathering, so it must not force a premature plan. The `_buildsoak_fakes` planning
> allowlist + advertised toolset mirror production. The strict-xfail was removed (now passing) and
> a real-loop test (`test_think_allowed_in_planning_executes_then_plans`) proves think is not
> rejected, executes, leaves the explore-read cap at 0, and the model then submits its plan.

## Root causes

### Bugs 1–3 — the planning gate lets non-plan tool calls fall through (one root cause)

`engine.py:841 _gate_planning_mode` intercepts only two cases while in `OperatingMode.PLANNING`:

1. a `submit_plan` call → intercepted into a `PlanEvent` (halts for approval);
2. a no-tool prose turn → acknowledged + nudged back toward `submit_plan`.

**Any other tool call returns `Disp.FALLTHROUGH`** and is then EXECUTED via the normal
`_execute_and_observe` path. The planner's tool set is filtered by
`driver.tools_for_step` (a write tool is not *advertised*), but §11.7 is explicit: *a hidden
tool is still unsafe if it remains callable*. A scripted / adversarial / confused model that
emits `file_write` during PLANNING has it run — a real workspace mutation before plan
approval — instead of getting a recoverable rejection observation.

This single fall-through produces all three of:
- **Bug 1/2** `WRITE_TOOL_ALLOWED_IN_PLANNING` — a write executes in first-turn PLANNING
  instead of being rejected (no `AgentErrorEvent`, the file is written).
- **Bug 3** `WRITE_BEFORE_REVISION_APPROVAL` — after a follow-up re-enters PLANNING
  (`request_plan` → `enter_planning`), the same fall-through lets a write run before the
  revised plan is approved.

Repair shape (later step, do NOT weaken the gate): in `_gate_planning_mode`, when the tool
call is neither `submit_plan` nor a planning-allowed read tool, REJECT it with an
`AgentErrorEvent` (recoverable, visible to the model) and `Disp.CONTINUE` — never fall
through to execution. The `ToolScopeOracle` already codes the attempt
(`WRITE_TOOL_ATTEMPTED_IN_PLANNING`) from the event log; the fix makes the product reject it.

The classifier's event-only `ToolScopeOracle` can prove `WRITE_TOOL_ATTEMPTED_IN_PLANNING`
(a mutating action before approval). It CANNOT prove `WRITE_TOOL_ALLOWED_IN_PLANNING` from
events alone (the offered/allowed tool schemas are not persisted) — that needs the live
runner to capture per-turn tool scope (the S3 slice). The §20 contract test proves the
ALLOWED/rejection contract directly against the real loop.

### Bug 4 — approval can finish with zero execution

After `approve_plan`, an agent that immediately tries to finish without doing any work is
nudged up to `_EXECUTION_NUDGE_CAP` (3) times by `finish.py gate_execution_nudge`, then the
gate RELEASES to `FINISHED` with a loud warning (proven by `test_w5_execution_nudge_cap.py`).
Per §11.2 / §20.4 the post-approval contract is "at least one action OR a terminal explicit
failure" — a `FINISHED` with no action and no `ERROR` violates it. Repair shape: the nudge-cap
release should land a terminal FAILURE (or surface a clear unexecuted-plan error), not a
silent `FINISHED`. (Do NOT just raise the cap — that hides the problem.)

### Bug 5 — `think` not offered in PLANNING (minor gap)

§20.1 / §15.2 list `think` among the allowed first moves in PLANNING, but the production
planning allowlist (`runtime.py:1467`) is `{submit_plan, file_list, file_read, search,
extract}` — `think` is omitted, so the planner can't use the no-op reasoning scratchpad
before proposing a plan. Quality gap, not a safety hole. Repair shape: add `think` to the
planning allowlist (it is `read_only`-safe — a no-op scratchpad).
