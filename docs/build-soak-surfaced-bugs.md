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

> **Harness fail-closed fixes (NOT product bugs).** A codex review of the foundation found
> THREE P0 **false-negatives in the adjudicator itself** — cases where it would classify a
> REAL failure as PASS. A false-green oracle is the worst outcome for this campaign, so they
> were fixed inside the harness (no product change) and pinned by
> `harness/build_soak/tests/test_fail_closed.py`:
> 1. **DB-row payload drop** (`events.py` normalize_event): a real SQLite row
>    `{seq,kind,source,payload(JSON)}` had its `payload` dropped, hiding
>    `detail`/`tool_call`/`revision` from every predicate →
>    `WRITE_TOOL_ATTEMPTED_IN_PLANNING` / `APPROVE_PLAN_NO_EXECUTION` /
>    `NO_REPLAN_AFTER_REVISION` all read PASS in row shape. Fixed by MERGING the parsed
>    payload with the row columns; the canonical event is identical for both shapes.
> 2. **Missing-approval false finish** (`oracles/event_chain.py`): the approval→execution
>    chain was only checked when `plan_approved` already existed (circular), so a
>    `PlanEvent → FINISHED` with no approval PASSED. Now: a PlanEvent that reaches FINISHED
>    MUST show the approval chain — no approval → `PLAN_APPROVED_STATUS_MISSING`; approval but
>    no execution action → `APPROVE_PLAN_NO_EXECUTION`.
> 3. **Required-evidence skipped into PASS** (`oracles/contract.py`): a scenario asserting
>    `tool_scope` with no captured tool-scope evidence used to SKIP into PASS; now it
>    FAIL-CLOSES to INVALID_RUN (insufficient evidence, §8) — the event-only
>    `WRITE_TOOL_ATTEMPTED_IN_PLANNING` check is unchanged.
>
> General principle now enforced: missing/ambiguous evidence → INVALID_RUN (never PASS); a
> required invariant whose evidence is present but violated → FAIL; only a genuinely-complete,
> genuinely-conforming run → PASS. The other oracles (revision, output_truth, harness_validity)
> were audited for the same "checks only if precondition already true → silent pass" pattern
> and the same row-vs-dict blindness; their remaining SKIPs are genuine not-applicable cases
> (no approval/follow-up to judge; a non-finished terminal that already surfaced its own
> error/cancel), and all read content through the normalizer so they inherit fix #1.

| # | Failure code | Severity | Status | Test | Site | Spec |
|---|---|---|---|---|---|---|
| 1 | `WRITE_TOOL_ALLOWED_IN_PLANNING` | P0 | **FIXED** (fix-planning-gate) | `test_build_plan_contract.py::test_write_tool_in_planning_produces_recoverable_rejection` (now passing) | `engine.py:841` `_gate_planning_mode` | §11.1, §11.7, §20.1 |
| 2 | `WRITE_TOOL_ALLOWED_IN_PLANNING` | P0 | **FIXED** (fix-planning-gate) | `test_tool_rejection_recovery.py::test_disallowed_tool_call_visible_as_rejection` (now passing) | `engine.py:841` `_gate_planning_mode` | §11.3, §20.3 |
| 3 | `WRITE_BEFORE_REVISION_APPROVAL` | P1 | **FIXED** (fix-planning-gate) | `test_build_replan_contract.py::test_agent_cannot_write_before_revised_plan_approval` (now passing) | `engine.py:841` `_gate_planning_mode` (revision re-entry) | §11.4, §20.2 |
| 4 | `APPROVE_PLAN_NO_EXECUTION` | P0 | open (xfail, strict) | `test_plan_approval_execution.py::test_kick_after_approval_produces_action_or_terminal_failure` | `finish.py` `gate_execution_nudge` (~L1008, `_EXECUTION_NUDGE_CAP` release) | §11.2, §20.4 |
| 5 | `THINK_NOT_EXPOSED_IN_PLANNING` | P2 (gap) | open (xfail, strict) | `test_build_plan_contract.py::test_first_turn_planning_exposes_think` | `runtime.py:1467` planning allowlist | §11.1, §15.2, §20.1 |

> **Bugs 1–3 FIXED** on branch `fix-planning-gate`: `_gate_planning_mode` (engine.py) now
> rejects any tool call that is not in the planning allowlist (`submit_plan` + `_plan_tool`
> defensively, the read/explore tools `file_read`/`file_list`/`search`/`extract`, and the
> virtual `ask_user`/`clarify`) with a recoverable, model-visible `AgentErrorEvent` paired by
> `tool_call_id` (`Disp.CONTINUE`) — it NEVER falls through to execution. The three strict-xfail
> markers were removed (the tests are now normal passing tests) and a dedicated real-loop
> regression suite was added (`test_planning_write_rejection.py`). Bugs 4 + 5 are separate
> fixes and remain strict-xfail.

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
