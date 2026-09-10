# Build Soak — surfaced product bugs (repair-loop backlog)

The §20 bare-Build contract tests assert the SPEC contract (`current/docs/build-soak-guidelines.md`).
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
> `development/harness/build_soak/tests/test_fail_closed.py` (+ the per-oracle unit tests):
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
| 4 | `APPROVE_PLAN_NO_EXECUTION` | P0 | **FIXED** (fix-approve-noexec) | `test_plan_approval_execution.py::test_kick_after_approval_produces_action_or_terminal_failure` (xfail removed, now passing) | `finish.py` `gate_execution_nudge` (`_EXECUTION_NUDGE_CAP` → STUCK terminal) | §11.2, §20.4 |
| 5 | `THINK_NOT_EXPOSED_IN_PLANNING` | P2 (gap) | **FIXED** (fix-think-planning) | `test_build_plan_contract.py::test_first_turn_planning_exposes_think` (xfail removed, now passing) + `test_planning_write_rejection.py::test_think_allowed_in_planning_executes_then_plans` | `runtime.py:1466` planning allowlist + `engine.py` `_gate_planning_mode` | §11.1, §15.2, §20.1 |
| 12 | `NO_REPLAN_AFTER_REVISION` | P0 | **FIXED** (fix-bug12-replan-followup) | `test_bug12_followup_replan.py` (6 real-loop tests: finished→followup, ordered chain, in-flight write race, between-steps steer, read-then-write window, Q&A negative) | `engine.py` `_maybe_reenter_planning_for_followup` (run() intake + `_run_drive`) + `_gate_midstep_steer_replan` (apply-time, in-flight race; re-enters on ANY tool) + `signals.is_revision_intent` | §11.4 |

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
> still execute, ask_user still halts).
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
>
> **Bug 4 FIXED** on branch `fix-approve-noexec` (Build Soak repair #2): `gate_execution_nudge`
> (`finish.py`) no longer RELEASES to a false `FINISHED:execution_nudge_cap` when the
> `_EXECUTION_NUDGE_CAP` (3) is reached without a productive action since approval. It now
> terminalizes the run **`STUCK`** with detail `approve_plan_no_execution` (a plan approved but
> never executed is a terminal explicit failure per §11.2, not a success) — bounded at 3 nudges,
> HALT, no infinite loop. STUCK (not ERROR) matches the existing bounded no-progress terminals;
> ERROR is reserved for thrown exceptions. `productive_action_since_approval()` is unchanged, so a
> run with ≥1 real post-approval action still FINISHES normally (the gate falls through/resets).
> The strict-xfail on `test_kick_after_approval_produces_action_or_terminal_failure` was removed
> (now a normal passing test); `test_w5_execution_nudge_cap.py` and the `test_loop_step.py` W-5
> livelock test were flipped from FINISHED→STUCK; the C18 plan_step tests gained a real productive
> action so they finish legitimately (they previously relied on the buggy cap-release).

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

### Bug 4 — approval can finish with zero execution — FIXED

After `approve_plan`, an agent that immediately tries to finish without doing any work was
nudged up to `_EXECUTION_NUDGE_CAP` (3) times by `finish.py gate_execution_nudge`, then the
gate RELEASED to `FINISHED` with a loud warning. Per §11.2 / §20.4 the post-approval contract
is "at least one action OR a terminal explicit failure" — a `FINISHED` with no action violates
it. **Fix:** at the cap, instead of releasing to `FINISHED:execution_nudge_cap`, the gate now
emits a "plan was not executed" system-reminder + `StatusEvent(STUCK, detail=
"approve_plan_no_execution")` and HALTs — bounded at 3, no infinite loop. STUCK (not ERROR)
matches the existing bounded no-progress terminals. `productive_action_since_approval()` was NOT
weakened, so a run with ≥1 real post-approval action still FINISHES (the gate falls through and
resets the nudge streak). The cap was NOT raised (that would hide the problem).

### Bug 5 — `think` not offered in PLANNING (minor gap)

§20.1 / §15.2 list `think` among the allowed first moves in PLANNING, but the production
planning allowlist (`runtime.py:1467`) is `{submit_plan, file_list, file_read, search,
extract}` — `think` is omitted, so the planner can't use the no-op reasoning scratchpad
before proposing a plan. Quality gap, not a safety hole. Repair shape: add `think` to the
planning allowlist (it is `read_only`-safe — a no-op scratchpad).

---

## S3 live-runner findings (2026-06-24, headless API runner vs the running :8000)

These were surfaced by the FIRST live runs of the S3 headless runner against a real
agent-server (`process` sandbox backend, homelab Qwen3.6-27B, no browser daemon). They are
recorded per §17 — the product fixes are a LATER repair iteration; engine/messages/recitation
were NOT touched. (The runner's own `pre-kick IDLE race` bug is NOT here — that was a harness
bug, fixed + pinned in `test_pre_kick_idle_does_not_abort_the_drive`.)

### Bug 6 — bare-Build PAUSES "actionless" instead of FINISHING a completed deliverable — PRODUCT — FIXED

`must_plan_before_tool` (conv_c0ff86840809442ca4bb08801156f053). The agent planned, was
approved, and WROTE the full deliverable (`index.html` + `styles.css` + `script.js` exist in
the sandbox). It then could not VERIFY: `verify_web_app` returns `UNVERIFIABLE` ("no browser
daemon could be started on this sandbox backend"). The agent retried verification a few times,
made no further file progress, and the loop's no-progress breaker fired:
`StatusEvent(PAUSED, detail="actionless")` (seq27-28) — "produced 3 consecutive responses
without doing any real work while plan steps remain undone — pausing instead of burning tokens."
Note the C18 done-condition advisories: the file-write/served steps' done-conditions WERE MET
(`file_exists(index.html): found`, served HTTP 200 — seq19-23); the only "undone" step was the
browser VERIFY, which is structurally impossible on this backend. So a SUBSTANTIVELY-COMPLETE
static build (deliverable written, done-conditions met) PAUSES rather than FINISHING. Likely §12
angle: a false/early **terminalization-as-PAUSE on a complete build** — relates to the
verify-gate + finish recognition; closest codes `NO_CLEAR_FAILURE_TO_USER` (P1, halts with no
clean terminal the user can act on) / `MISSING_DONE_CONDITIONS`. Repair direction (NEXT
repair-loop iteration, NOT fixed here): the loop should recognize plan-complete + FINISH, OR the
verify-unavailable path should let a substantively-complete build finish, instead of
actionless-pausing it. (The runner now RESUMES such a pause ≤3× as the user — see Bug 8 — which
exposes whether the product can make progress; here it cannot, because the verify step is
unsatisfiable on the no-browser backend.) Evidence:
`trace_conversation.py conv_c0ff86840809442ca4bb08801156f053 25` (seq19-28).

> **Bug 6 FIXED** on branch `fix-verify-backend` (Build Soak repair #4), shared root with Bug 7.
> The honest-unverifiable finish path now recognizes a delivered-but-unverifiable build:
> `FinishGate._gate_verify_web_app` (`finish.py`) gained `_maybe_honest_unverifiable_static_finish`
> — when the ONLY verify failure is "not serving" (server unreachable; the browser never ran, so
> no console/network errors and no blank-render judgement) AND the backend cannot run a headless
> browser (no `browser` tool) AND `index.html` exists on disk, the gate emits
> `StatusEvent(detail="unverifiable_static_finish")` + a visible UNVERIFIED note and FALLTHROUGH →
> FINISHED, instead of refusing → `verify_no_progress` → STUCK. A REAL fail (console/network/blank/
> missing deliverable) never reaches this branch (W-45 preserved), and it runs only AFTER
> `gate_execution_nudge` (so `APPROVE_PLAN_NO_EXECUTION` still STUCKs and a zero-action run cannot
> finish). With Bug 7 fixed too, the reachable-server + browser-unavailable case already finishes
> via the existing `verdict="unverifiable", passed=True` path. Regression: `test_verify_web_app_gate.py::`
> `test_process_browser_unavailable_static_build_finishes_not_stuck` (+ two negatives).

> **Bug 6 FIXED (repair #5)** on branch `fix-bug6-actionless-finish` (Build Soak repair #5). The
> live soak REPRODUCED Bug 6 ×2 after repair #4 because the repair-#4 honest finish lives at the
> FINISH GATE — and the model never reaches it here. It churns on the unsatisfiable browser-verify
> plan step and the **actionless valve PAUSES it first** (`Valve.actionless_valve`,
> `turn_control.py`), so the finish-gate honest path is unreachable. Fix = RCA option **3a**:
> EXTEND the same honest-unverifiable finish to the actionless valve.
> `FinishGate.maybe_honest_unverifiable_static_actionless_finish(events)` (`finish.py`) is called
> from `Valve.actionless_valve` immediately before the incomplete-plan "actionless → PAUSE" branch;
> when it returns True the valve emits the honest `StatusEvent(detail="unverifiable_static_finish")`
> marker + a terminal **FINISHED** instead of PAUSED. Conservative guards (ALL required): a plan
> exists, is incomplete, and EVERY not-done step is verify-only (title/detail matches a
> verify/check/render/test lexicon); ≥1 productive action since approval; `index.html` on disk; a
> NON-browser validation (shell HTMLParser/static parse referencing `index.html`) PASSED after the
> last write/edit; browser verification is GENUINELY unavailable (no `browser` tool OR a browser
> observation/error carrying `browser_unavailable` / the `BROWSER_UNAVAILABLE_MSG` text); and NO
> real web-failure evidence (console/network errors or a served-but-blank render). NOT option 3b
> (it does NOT auto-mark the verify step done — that would mutate advisory plan bookkeeping and hide
> that browser verify never ran). Must-not-regress preserved: a real `verify_web_app` failure still
> STUCKs (W-45); a missing deliverable or a FAILED static validation still PAUSE/STUCKs; a
> zero-productive-work run still STUCKs `approve_plan_no_execution`; the pending-re-plan (BW-01)
> suppression is honored. Proof = loop-fake reproduction (the running dev server is at the
> pre-fix code, so a true server bounce was not done): `test_bug6_actionless_honest_finish.py` —
> 1 positive (exact repro → FINISHED + honest marker, not PAUSED/STUCK) + 3 negatives (no
> deliverable, failed validation, zero-work).
>
> **Hardening (codex review).** Three anti-false-finish holes tightened so the honest finish fires
> ONLY for a genuinely-complete, content-validated, browser-unavailable static build: (1)
> `_VERIFY_STEP_RE` is now phrase-anchored — bare `test`/`render` dropped so "Add a **test**imonials
> section" / "**render** the gallery" are NOT misread as verify-only (verify/validate/check/confirm/
> qa/smoke/lint + "renders correctly" / "displays correctly" / "test that…/it" / "tests pass" only);
> (2) `_nonbrowser_static_validation_passed` now requires a REAL content/structure check
> (`_VALIDATION_CMD_RE`: HTML/XML parser, structure check, or content grep) — a bare `ls`/`test -f`/
> `stat`/`cat` existence check no longer counts; (3) `_real_web_failure_evidence` now also blocks on
> browser NETWORK failures (the daemon's `network` ring) and BLANK renders (no meaningful
> text/elements), not just console errors. Three added negatives:
> `test_negative_content_step_not_verify…`, `test_negative_existence_check_is_not_a_validation`,
> `test_negative_browser_failure_evidence_blocks_honest_finish[network|blank]`.
>
> **Hardening (codex re-review).** Two residual GAMEABLE false-finish paths closed: (1) `_VERIFY_STEP_RE`
> no longer matches a standalone `renders` — "renders"/"displays" count ONLY as a verification PHRASE
> (a qualifier "renders correctly/properly", or an explicit subject "the page renders"/"it displays"),
> so a CONTENT step "Create product renders" / "Add hero renders" (renders = images/output) is NON-verify;
> (2) validation is now STRUCTURAL via `_is_real_validation_command` (+ `_shell_command_text`) — the
> first token (invoked executable) must be a markup parser/validator (`xmllint`/`tidy`/`html5validator`/
> grep) or a `python -c/-m` that actually imports a parser module (`html.parser`/`lxml`/`html5lib`/etc.);
> `echo validate index.html`, `printf "markup" index.html`, no-ops, and `python -c "print('validate')"`
> are rejected (substring presence of "validate"/"markup" no longer counts). Two added negatives:
> `test_negative_renders_noun_step_not_verify[Create product renders|Add product renders]`,
> `test_negative_echoed_validation_word_is_not_a_validation[echo|printf]`. Suite: 12 Bug-6 tests green.
>
> **Hardening (codex re-review 2 — STRUCTURAL, kills the whole content-noun class).** Whack-a-moling
> individual verification words (test→testimonials, render→product-renders, validation→input-validation,
> lint→lint-config, check→checkout) never converges, so the verify-only classifier is now structural:
> a not-done step is verify-only iff it has a verification-ACTION framing (`_VERIFY_ACTION_RE`: a verify
> VERB — verify/validate/check/confirm/ensure/test/review, precise stems so "checkout"/"testimonials"
> don't match — followed by a target "that/the/it/…", or a standalone "qa"/"smoke test"/"run the
> linter", or an outcome phrase "renders correctly"/"the page renders"/"tests pass"/"no console errors")
> AND has NO creation/content verb (`_CONTENT_VERB_RE` hard negative override: add/create/build/
> implement/write/design/style/make/set up/configure/install/include/insert/append/generate/scaffold/
> integrate/update/fix/refactor/…). So "Add input validation", "Set up linting", "Create product
> renders", "Add a testimonials section", "Build the contact form" are ALL non-verify, and a bare noun
> ("validation"/"lint") never matches (no action framing). When in doubt → NON-verify (stay paused, the
> safe direction). The positive repro step "Verify the page renders correctly" still qualifies (verb +
> "the", no creation verb). Added negative: `test_negative_verification_word_as_content_noun_not_verify`
> over [Create/Add product renders, Add input/form validation, Set up linting, Configure ESLint, Add a
> testimonials section]. Suite: 17 Bug-6 tests green.

### Bug 7 — build preview/serve port == agent-server port (8000) collides on the `process` backend — PRODUCT/CONFIG — FIXED

`static_html_minimal` (conv_b59521ba06054d5189722e1f319b63f7). The plan's serve/verify steps are
pinned to **port 8000**, which is ALSO the agent-server's own HTTP port. On the local `process`
sandbox backend the sandbox shares the host network namespace, so the build's
`python3 -m http.server 8000` squats the agent-server port and KILLED the live server mid-run
(the second smoke aborted with `httpx.ConnectError`; on restart the lifecycle reconciler set the
orphaned RUNNING conv → PAUSED — the PAUSED status is that restart artifact, not a no-progress
pause). The agent itself DETECTED the conflict ("the agent server on 8000 isn't serving static
files") and worked around it by serving on 8080, but the plan done-conditions + `verify_web_app`
remain hardwired to 8000 → an UNSATISFIABLE verify loop, and any literal "serve on 8000" attempt
is self-destructive on this backend. Likely §12 code: **`FALSE_FINISH_PREVIEW_BROKEN`** /
preview-truth class (the required preview can never come up on the agent-server's port). Repair
direction (later): the build preview port must not equal the agent-server port on a
network-shared backend, or the `process` backend must isolate the network. Evidence:
`trace_conversation.py conv_b59521ba06054d5189722e1f319b63f7 25`.

**Live-smoke reproduction (clean, server survived) — conv_6d4dafa9c400484e860c1e74932770c0:** a
bounded `static_html_minimal` live run reproduced this WITHOUT crashing the server (the agent
served on 8080, not binding 8000). It wrote + served the page, but `verify_web_app({})` with no
url DEFAULTS to `http://127.0.0.1:8000/` → "App not serving" (8000 is the agent-server, nothing
the build can serve), the env keeps telling it to `python3 -m http.server 8000`, and after the
`verify_no_progress` breaker the run terminalized **STUCK** (seq33-40). The S3 runner classified
it deterministically as **FAIL / `BUILD_DID_NOT_FINISH` (P1)** — a TRUE outcome (the build never
finished), and a live validation of the Bug 8 fix (pre-fix this STUCK-with-actions run would have
SKIP→PASS). So the concrete product hook is `verify_web_app`'s default preview port (8000) ==
the agent-server port; on an isolated sandbox the two 8000s don't collide, but the default still
points the verifier at a port the build cannot own on the shared-net `process` backend.

> **Bug 7 FIXED** on branch `fix-verify-backend` (Build Soak repair #4). Three fixes keyed off one
> new shared resolver `disco.core.loop.preview_target` (single source of truth, consumed by BOTH the
> finish gate in core AND `verify_web_app` in tools). **The PRIMARY, essential fix is #1
> (verify-retargeting); #2 is best-effort defense-in-depth, NOT a containment guarantee.**
> 1. **Backend-aware verify target (PRIMARY)** — `resolve_preview_port(host_shared, owned,
>    conversation_id)`. On a shared-host backend (`process`/`local` — `sandbox.workspace_path` is set)
>    it returns ONLY a CONVERSATION-OWNED non-reserved port (its tmux session matches `disco-{cid8}-…`),
>    else **None = UNDETECTABLE**. It does NOT blind-guess a port (P1, codex): a guess could verify the
>    agent-server (8000), Disco's own Vite UI (**5173** — now reserved alongside 8000/8800), or a
>    SIBLING conversation's server → a FALSE PASS against the wrong app. None routes to the honest-
>    unverifiable path (no false verify). The driven `verify_web_app` is passed `{"url": target}` (or
>    `{}`→tool auto-detect→same resolver→`""`/not-serving when undetectable). The SAME rule applies to an
>    AGENT-SUPPLIED **explicit** `url` (`verify_web_app({"url": "http://127.0.0.1:5173/"})` could
>    otherwise bypass the resolver): on the shared host an explicit url is honored ONLY if its port is
>    conversation-owned + non-reserved (`explicit_target_allowed` — probed live); a reserved/foreign port
>    → not-this-build's-app (not-serving + clear reason), never a false PASS. The finish-gate None-path is
>    sound: with the tool unable to emit a passing verdict for a foreign port, `target_url=None` (binding
>    disabled) drives a fresh `{}` verify → not-serving → honest path (no spurious pass, no crash).
>    Isolated (gVisor/Podman) backends keep 8000 + honor explicit urls as-is (the box's app).
> 2. **Process control-port containment (BEST-EFFORT / defense-in-depth — NOT a guarantee)** —
>    `ProcessSandboxInstance.exec_shell` refuses commands that bind/kill a reserved port via
>    `reserved_port_command_violation`; `expose_port` refuses to advertise them; `ensure_preview` remaps
>    the 8000 default to a process-safe port. This is a SHELL-STRING scan: it catches the common
>    `python -m http.server 8000` shape but is **trivially bypassable** (a raw Python `socket.bind`, a
>    renamed binary). Per the RCA, shell scanning cannot guarantee "never bind/collide" — the robust
>    containment is a **network namespace** (or not using the `process` backend for hosted/multi-tenant
>    soak), **tracked as a follow-up**. Containment is not the essential fix; #1 is.
> 3. **Honest unverifiable-finish** — see Bug 6 above (shared root). Regression:
>    `test_preview_target.py` (resolver returns None on no-conversation-owned port + reserves 5173 +
>    `explicit_target_allowed`/`target_url_port` + containment units), `test_verify_app.py::`
>    `test_process_autodetect_*` (undetectable "", never 5173/8000) + `test_explicit_*` (explicit
>    reserved/foreign url rejected, conversation-owned honored, isolated unchanged),
>    `test_verify_web_app_gate.py::test_process_gate_drives_verify_against_conversation_port_not_8000`
>    + the Bug-6 honest-finish loop test.

### Bug 8 — adjudicator GAP: a PAUSED-incomplete run classified PASS (HARNESS) — FIXED

NOT a product bug — a harness fail-closed gap surfaced by Bug 6. A bare-Build run that ended
PAUSED (or any non-`FINISHED`/non-work terminal) used to classify **PASS**: `terminal_status()`
returns None for PAUSED (correctly — it is resumable), so the EventChain approval chain is not
judged and `OutputTruthOracle` SKIPPED (it only checked output truth on a finished terminal). Net:
a run that PAUSED without finishing AND whose required workspace/preview output was therefore never
verified scored PASS (observed live: `must_plan_before_tool` → PASS over conv_c0ff86, which actually
PAUSED actionless). Same fail-closed class as the foundation-slice false-negatives
(`test_fail_closed.py`) and the STUCK-terminal fix. **Fix (this commit,** with §19 migration
`migrations/2026_06_24_paused_incomplete_not_pass.md`**):** `OutputTruthOracle` now FAILS-CLOSED with
the new **`BUILD_DID_NOT_FINISH`** (P1) when the scenario requires output (workspace/preview) and/or
declares `terminal_status_in` but the run did NOT reach a finished/required terminal — never
SKIP→PASS. A scenario asserting no output still SKIPs (unchanged). The RUNNER also now RESUMES a
PAUSED run a bounded ≤3 times (acting as the user) before classifying, so a transient actionless
pause that the user would clear is not mis-failed, while a build that just keeps pausing lands
`BUILD_DID_NOT_FINISH`. Pinned: `test_classifier.test_paused_incomplete_required_output_is_build_did_not_finish`,
`test_output_truth_oracle.test_not_finished_with_required_output_fails_closed`,
`test_api_runner.test_paused_{then_finished_resumes_to_terminal,forever_is_bounded_then_build_did_not_finish}`.

### Bug 9 — runner workspace-collection returned an EMPTY manifest for a SUCCEEDED build (HARNESS) — FIXED

NOT a product bug — a RUNNER collection defect surfaced by the first live soak smoke. A build
that genuinely SUCCEEDED (`file_write index.html` → served → `DELIVERABLE index.html` "Build Smoke
OK" → `FINISHED`) produced an empty `workspace-manifest.json` (`{}`), so `OutputTruthOracle`
false-FAILed it with **`FALSE_FINISH_NO_OUTPUT`**. A runner that false-fails every successful run
is useless.

**Root cause.** `collect_workspace` fetched each declared path via the single-origin preview proxy
(`GET /conversations/{cid}/preview-app/<path>`), which proxies the agent's ephemeral DEV SERVER.
Post-FINISH that route 404s (the served preview isn't up/registered for that exact path), so the
manifest came back empty even though `index.html` was really written. Live-verified that **no** HTTP
route serves arbitrary workspace source for a finished static build: `…/preview-app/index.html`
(proxy down), `…/workspace/index.html` (image-only allowlist `.pmx/screenshots|plots`), and
`…/artifacts/index.html` (declared-`files` only — an app deliverable is `artifact_kind="app"`) ALL
404.

**Fix (this commit, `fix-soak-workspace-collect`, HARNESS-only `adapters/disco_api.py`).**
`collect_workspace` now reads the AUTHORITATIVE host ProjectStore SNAPSHOT directly
(`<projects_root>/<cid>/workspace/…` — the SAME durable source the product's own
preview-edit/artifact-download routes fall back to once a run is terminal), independent of
dev-server state. It walks the snapshot (symlink-jailed: `resolve()` + `is_relative_to`) into a
manifest `{path: {present, size, sha256, content}}` reflecting the WHOLE workspace, keying each
declared path by its exact scenario string so the unchanged oracle's `path in files` check matches.
A genuinely-missing declared file is OMITTED (never a `present:false` key — that would mask
`FALSE_FINISH_NO_OUTPUT` into `ARTIFACT_TRUTH_MISMATCH`), so a real missing deliverable still FAILs.
A bounded re-read (`--snapshot-wait`, default 15s) absorbs the snapshot-vs-`FINISHED` flush race
(the build appends `FINISHED` inside `loop.run()`, then `_maybe_snapshot` writes the workspace).
**Snapshot is AUTHORITATIVE — no proxy mask (anti-false-PASS hardening).** The preview proxy is
NEVER a workspace-source fallback. Generated preview bytes are not arbitrary source truth, and the
authenticated legacy preview-app route is forbidden by the isolated capability boundary. With no
ProjectStore snapshot the honest manifest is empty, so the evidence contract fails closed. A declared
file absent from an existing snapshot is likewise omitted; no served/stale copy can mask
`FALSE_FINISH_NO_OUTPUT`. **LIVE-PROVEN:** re-running `static_html_minimal` populates the manifest from
the snapshot (`index.html` present, content "Build Smoke OK", real sha256/size) → workspace truth
checks PASS. Pinned:
`test_api_runner.test_collect_workspace_{reads_snapshot_when_preview_proxy_404s,
genuinely_missing_file_is_omitted,without_projects_root_fails_closed_without_preview_fallback}`,
`test_snapshot_authoritative_does_not_proxy_mask_missing_required_file`.

### Bug 10 / H175-H177 — canonical capability preview truth + provenance (HARNESS) — FIXED

Surfaced by the Bug 9 live re-run: with the workspace manifest now correct, the SAME
`static_html_minimal` smoke fails one step later with **`FALSE_FINISH_PREVIEW_BROKEN`**
(`preview_health_status: 404`). `collect_preview` read the preview via the same fragile
`GET …/preview-app/` proxy, which 404s post-FINISH because the model's served preview
(`python3 -m http.server 8080 -d /workspace`, a backgrounded shell process) is torn down when the
run ends. This is NOT a bad build and NOT Bug 9: the event log PROVES the preview served correctly
DURING the run (`verify_web_app` on :8080 → passed; `shell curl …8080 | grep 'Build Smoke OK'` →
exit 0); `…/preview-edit/index.html` (snapshot-backed) returns 200, so the deliverable is durable.

**Current fix.** Preview hardening made the old authenticated `GET …/preview-app/` path correctly
return 403 `preview capability required`, surfacing H175. The transport now mints
`POST /conversations/{cid}/preview/capability`, strictly validates the returned p3s bootstrap origin,
redeems the one-use intent in a clean cookie jar, rejects any application-session crossover, and
fetches `/__disco/isolated-preview/{cid}/`. The final status/body are authoritative. A mint,
redemption, or product-preview error remains a real failure even when a host snapshot exists; the
harness never converts it into a local snapshot PASS. This also eliminates H176's false verifier
veto coupling: preview truth comes from the product boundary, while an `unverifiable` browser verdict
remains independently visible in events and terminal-warning enforcement.

H177 provenance is persisted as `preview/metadata.json` beside health/body and included in the
evidence hash lock. Frozen-folder classification reloads workspace, preview health/body, and metadata,
so retained output truth can be independently replayed. Pinned by capability clean-cookie/origin and
fail-closed tests, exact H175 `browser_unavailable` coverage, canonical failure/wrong-body coverage,
metadata tamper detection, and PASS/403/wrong-body frozen-dossier replay.

### Bug 11 — runner crashes the whole drive on a normal PAUSE→resume (HARNESS) — FIXED

Surfaced live during the Bug 10 re-run: a build that PAUSED (the cooperative valve), then was resumed
by the runner (acting as the user, ≤3×, the Bug 8 design), crashed the entire drive with
`ValueError: invalid literal for int() with base 10: 'RUNNING'` → the run degraded to `INVALID_RUN`.
Root cause: `DiscoApiClient.resume` returned `{"status": <http_int>, **body}`, but the resume body is
`{"ok": true, "status": "RUNNING"}` — the body's STATE STRING clobbered the HTTP int under the shared
`status` key, so the caller's `int(resp["status"])` blew up the moment any build paused. The
deterministic tests missed it because the fake transport returned no `status` in the resume body.
**Fix (this commit):** `resume` returns the HTTP code under the non-colliding `http_status` key (body
fields, including the state `status`, preserved); the caller reads `http_status`. The fake transport
now returns the REALISTIC resume body (`{"ok": true, "status": "RUNNING"}`) so the existing
`test_paused_*` resume tests are a permanent regression guard.

### Bug 12 — a change follow-up after an approved/finished build does NOT re-plan — PRODUCT — FIXED

Surfaced by the live soak in TWO scenarios (`revise_after_finish` + `steer_while_running`), both
adjudicated `NO_REPLAN_AFTER_REVISION` (P0): after a change follow-up on an approved/finished build
the loop resumed executing the OLD plan and wrote files WITHOUT a revised plan
(`write_before_revised_plan_approval=true`, `latest_plan_revision_after_followup` unchanged).

**Root cause:** the Build follow-up paths (WS `send_message`/`steer`, REST `/messages`/`followup`)
append a USER message and call `runtime.run_controller.kick()` — they do NOT call `request_plan()`, the only op
wired to `enter_planning()`. So on a follow-up the loop never set the `planning` mode marker; the
just-merged planning gate (`engine.py` `_gate_planning_mode`) is a no-op unless `self.mode ==
PLANNING`, so the model wrote freely against the stale approved plan. `signals.in_planning_for_revision`
and the actionless valve also key off the durable `StatusEvent(detail="planning")` marker, which was
never emitted — so the existing safeguards had nothing to engage on.

**Fix** (`fix-bug12-replan-followup`, engine.py + signals.py only — disjoint from the Bug-6 finish
work; `finish.py`/`turn_control.py` untouched): a new `AgentLoop._maybe_reenter_planning_for_followup`
supplies the MISSING `planning` marker on a CHANGE/REVISION follow-up. It fires only for a plan-gated
conversation already in execution mode (a prior `plan_approved` exists) with a fresh unprocessed user
turn whose intent is a change (`signals.is_revision_intent` — a conservative predicate: change verbs
revise/change/add/update/remove/fix/edit/replace/make/build/… → re-plan; a clear pure question
("what font did you use?") → exempt; ambiguous → bias to planning, the safe contract). It sets
`mode = PLANNING`, resets the explore-read counter, emits `StatusEvent(RUNNING, detail="planning")`,
and emits the existing replan framing. The existing revision machinery (`Planner.plan_from_args` →
revision = prev+1) then produces rev 2. Wired at THREE points: the `run()` intake (the FINISHED→followup
case); `_run_drive()` before `drive_step()` (the mid-run RUNNING→steer case — `kick()` returns early
when a task is active, so the intake never sees a mid-run steer); AND — closing the in-flight race
below — `_gate_midstep_steer_replan` at the tool-apply boundary. A re-entered run produces the
revision oracle's exact chain: `followup < revised PlanEvent(rev=prev+1) < AWAITING_PLAN_APPROVAL <
RUNNING/plan_approved < write`. Q&A is exempt (answered in execution mode, no forced re-plan).

**In-flight steer race (codex-found follow-ups, same branch):** the top-of-loop `_run_drive` re-plan
check runs BEFORE `drive_step()`, so a change steer that lands WHILE the model is mid-turn is missed —
the in-flight step can be a WRITE that lands once on the OLD plan before the next iteration re-enters
planning (exactly what the revision oracle flags). Closed by a second, apply-time gate
`AgentLoop._gate_midstep_steer_replan` (called just before the ActionEvent is built): it RE-POLLS the
log for a fresh pending change follow-up and, on detection, RE-ENTERS PLANNING immediately (sets
`mode=PLANNING` + the `planning` marker + replan framing), then REJECTS the current call recoverably IF
it is mutating (record the ActionEvent paired with an `AgentErrorEvent`, same shape as
`_gate_planning_mode`).

Crucially the re-poll fires for ANY in-flight tool, **read/think/explore included** (a second
codex-found residual): if it had only fired for mutating tools, a READ in flight when the steer lands
would proceed and emit its ActionEvent AFTER the steer, BURYING the steer's unprocessed marker — so
neither the next top-of-loop check nor a later apply-gate would see it, and the model's subsequent write
would slip through on the stale plan. So on a read-in-flight steer the read may proceed harmlessly but
PLANNING is already set, and `_gate_planning_mode` then rejects EVERY subsequent tool (a single
`drive_step` emits one tool call, so the next write is a fresh iteration the per-iteration planning gate
catches). The invariant: the moment a change steer is detected at apply-time, the conversation is in
PLANNING and no mutating write can land until a revised plan is approved — regardless of the in-flight
tool type. Q&A still exempt (`signals.is_revision_intent`).

Proof: `current/packages/core/tests/test_bug12_followup_replan.py` — 6 real-loop (`loop_fakes`) tests
reproducing the exact revise + steer conditions via the PLAIN product follow-up path (not
`request_plan`): (1) finished→followup re-enters PLANNING + defers the stale-plan write + submits
rev 2; (2) approving rev 2 then writes, asserting the full ordered chain; (3) the IN-FLIGHT steer race
— the steer lands DURING the write step, the apply-time gate defers it, only an approved revised plan
lets the write land (proven: with the apply-gate disabled this test fails — the write goes through);
(3b) a between-steps steer re-enters PLANNING via the top-of-loop check; (3c) the READ-then-WRITE window
— the steer lands during an in-flight READ; the read may proceed but PLANNING is re-entered on detection
and the model's next write is rejected on the old plan (proven: with the read-re-entry reverted this
test fails — the write slips through); (4) negative — a pure Q&A follow-up is answered without a forced
re-plan (no dead-end). The §20 contract suite + the planning-gate regression suite + the full core suite
stay green.

### Bug 13 — revision oracle FALSE-FAILs a REJECTED pre-approval write (HARNESS) — FIXED

Surfaced re-classifying the `revise_after_finish` live run (`build_soak_revise_after_finish_20260624_155854_000`,
conv `23f99798`) against the merged Bug-12 product fix. The run was adjudicated
`WRITE_BEFORE_REVISION_APPROVAL` (P0) — but that was a HARNESS false-FAIL, masking proof that Bug 12
works. After the follow-up (seq 56) the model ATTEMPTED `file_replace_lines` at seq 59 — which the
planning gate correctly REJECTED (seq 60 = `agent_error`, the file was NOT mutated; the model recovered
via `file_read` at seq 61, re-planned to rev 3, got approval at seq 65, then the post-approval write
executed). A REJECTED write attempt is the §11.3 tool-rejection-recovery contract WORKING — exactly the
Bug-12 gate firing — NOT a §11.4 write-before-revised-approval violation.

**Root cause:** `oracles/revision.py` `_first_mutating_action_after` returned the seq of a mutating
ACTION regardless of whether it actually EXECUTED. It counted the rejected seq-59 attempt as
`mutated_seq` → `write_before_revised_plan_approval=true`. The §11.4 contract is "no write EXECUTES /
mutates before revised approval", not "no write is ATTEMPTED". `oracles/tool_scope.py`'s
`WRITE_TOOL_ATTEMPTED_IN_PLANNING` had the same latent flaw (it flags the product letting a write FALL
THROUGH and EXECUTE in planning — a rejected attempt is the gate working, the §11.3 PASS, not the
violation).

**Fix** (`fix-bug13-revision-oracle`, harness only — `events.py` + `oracles/revision.py` +
`oracles/tool_scope.py`; no product/engine/messages/recitation/uv.lock): a mutating action is correlated
with its result by `action_id` (the ActionEvent's `id` ↔ the Observation/AgentError `action_id`; verified
on frozen evidence) and counts unless it was **rejected by a RECOGNIZED pre-execution gate/guard**. Two
codex anti-false-PASS corrections shaped the discriminator:

  1. It is **NOT the `success` flag.** A write tool can mutate disk and THEN report `success=False` (a
     partial / failed-after-mutation write), so treating any `success=False` as "not executed" would
     wrongly EXCLUDE a real mutation → false-PASS. **Any ObservationEvent (a `tool_result` exists, success
     True OR False) → the call reached the executor and COUNTS.**
  2. It is **NOT the mere presence of an AgentErrorEvent.** The product emits AgentErrorEvent for MANY
     cases (its own docstring: "tool failed, action invalid, execution raised, or the human declined") —
     critically a tool that STARTED, mutated disk, then RAISED yields an AgentError with no observation but
     DID mutate. So a bare AgentError is not proof of non-mutation. Exclusion is narrowed to a **closed
     allowlist of recognized pre-execution gate/guard refusals** (`events._GATE_REJECTION_MARKERS`):
       * `_gate_planning_mode` — `"… is not available in PLANNING mode. No workspace mutation …"`
       * `_gate_midstep_steer_replan` (`_MIDSTEP_STEER_REFUSAL`) — `"… was not applied. A change request
         arrived while you were mid-step …"` (the Bug-13 seq-60 text)
       * K1 elision-marker execution guard — `"… contain an internal elision placeholder …"`

`events.action_executed(events, action_id)` therefore returns:

  * paired with an **ObservationEvent** → True (reached the executor; may have mutated) → COUNTS;
  * paired ONLY with an **AgentErrorEvent matching a recognized gate marker** and no observation → False
    (refused before execution, nothing mutated, the §11.3 contract working) → does NOT count;
  * any **other AgentErrorEvent** (tool raised after possibly mutating / invalid / declined), or **neither**
    (dangling) → True, conservatively (lean anti-false-PASS).

`_first_mutating_action_after` and the tool_scope `WRITE_TOOL_ATTEMPTED_IN_PLANNING` loop both apply it.
**Anti-false-PASS preserved:** any write that reached the executor before revised approval — a
`success=False` observation OR a non-gate AgentError (tool raised) — STILL counts → STILL FAILs
`WRITE_BEFORE_REVISION_APPROVAL` / `WRITE_TOOL_ATTEMPTED_IN_PLANNING`. The ONLY thing excluded is a write
carrying a recognized gate-rejection marker — exactly the Bug-13 case (seq 59 write / seq 60 agent_error
carrying the `_MIDSTEP_STEER_REFUSAL` text).

**Marker fragility + deferred product hardening:** the `AgentErrorEvent` carries only a free-text `error`
string (no structured field; `meta` is empty), so the harness keys on the gate's fixed message strings.
That couples the harness to the product's refusal WORDING — if a gate's message text changes, the allowlist
must be updated. The robust fix is a small, **gate-only** product change: a structured rejection marker on
the AgentErrorEvent the gates emit (e.g. `error_type="gate_rejected"` or a rejection code) that the oracle
could key on instead of strings. The strings are stable today, so this is DEFERRED, not done here (kept
strictly harness-only; flagged for a decision).

Proof: harness unit tests use the REAL gate refusal strings (mirrored from the product constants).
`development/tests/test_revision_oracle.py::test_rejected_preapproval_write_then_clean_replan_passes` (gate-rejected
attempt carrying the real `_MIDSTEP_STEER_REFUSAL` marker, no observation → no
`WRITE_BEFORE_REVISION_APPROVAL`, revised chain holds → PASS); `::test_executed_preapproval_write_still_fails`
(success-observed pre-approval write → STILL fails); `::test_mutate_then_fail_preapproval_write_still_fails`
(codex #1 — a `success=False` observation reached the executor → STILL fails);
`::test_nongate_agent_error_preapproval_write_still_fails` (codex #2 — a pre-approval write whose tool
RAISED, recorded as a bare non-gate AgentError with no observation, may have mutated → STILL fails).
`development/tests/test_classifier.py::test_rejected_write_in_planning_is_not_a_violation` (real `_gate_planning_mode`
marker → no `WRITE_TOOL_ATTEMPTED_IN_PLANNING`), `::test_failed_write_in_planning_still_a_violation`
(`success=False` planning write that ran → STILL fails), and
`::test_nongate_agent_error_write_in_planning_still_a_violation` (a non-gate AgentError planning write →
STILL fails). All existing oracle/classifier tests (the legitimate-violation cases) stay green; full
harness suite green.

**Confirms Bug 12 works live.** Re-classifying the frozen `revise_after_finish` run (read-only, into a
copy — the original frozen evidence is untouched) now shows **RevisionOracle PASS** and **ToolScopeOracle
PASS** — the seq-59 pre-approval write WAS rejected by the gate and the re-plan (rev 3) went through, so
the harness no longer false-FAILs the Bug-12 behaviour.

**NOTE — not a false-PASS:** the run as a whole still does NOT classify PASS, and that is CORRECT. With
the Bug-13 false-FAIL removed, adjudication proceeds to `OutputTruthOracle`, which honestly reports
`BUILD_DID_NOT_FINISH`: the run genuinely ended `STUCK` (`repeated_action_error`, seq 84) on the SECOND
follow-up — it never reached `FINISHED`/`VERIFIED`. (The `index.html` deliverable does contain both
required substrings, but a build that does not complete is a failure regardless — the same fail-closed
policy as Bug 8.) That STUCK outcome is a separate, genuine run result, NOT a harness artifact; forcing
the run to PASS would be a false-PASS. Bug 13 is strictly the WRITE_BEFORE_REVISION_APPROVAL false-FAIL,
and it is fixed.
### Bug 14 — executor-boundary elision-marker guard coverage gap (the copied placeholder could reach `file_replace_lines` + any executor-direct path) — PRODUCT — FIXED

Surfaced by the live soak (`revise_after_finish` + `steer_while_running`), both adjudicated STUCK on
`repeated_action_error`. **Root cause is two-layered, and only ONE layer is a loop bug:**

1. **Model-quality (NOT a bug — the STUCK is CORRECT):** the weak local driver saw its OWN prior large
   tool-call args rendered in history as the context-saving elision placeholder
   (`<N chars elided — re-issue the call or file_read the path …; do not copy this placeholder into a
   tool argument>`, `events._arg_snip_marker_neutral`) and COPIED that placeholder verbatim into
   `file_replace_lines.new_text`. The K1 guard rejected the call recoverably, the model re-issued the
   SAME bad action, and the `StuckDetector` — correctly comparing the RAW (un-elided)
   `ActionEvent.tool_call.arguments`, NOT the rendered/snipped view (`equality.event_content_eq`) —
   saw a genuine repeat and fired a LEGITIMATE STUCK. The detector is RIGHT; it was not changed.

2. **The loop hardening gap (the actual fix):** the existing K1 elision guard lived only in the
   **Observer** (`loop/observe.py`, before `executor.execute`). Not every execution path funnels through
   the Observer, so a `file_replace_lines` (or any future mutator, or a direct `executor.execute` call)
   reaching the executor on a non-Observer path was UNGUARDED — the copied placeholder could have
   overwritten real content (DATA LOSS), exactly the K1 failure class for a non-`file_write` mutator.

**Fix** (`fix-bug14-executor-elision`, executor + tests only — `messages.py`/`recitation.py`/`engine.py`
untouched): a GENERIC tool-boundary guard in `DefaultToolExecutor.execute()`
(`current/packages/tools/src/disco/tools/executor.py`, step 1.5 — after tool resolution, BEFORE pydantic
validation / `tool.run`). It reuses the existing K1 detector `events.find_elided_arg_markers(call.arguments)`
(which anchors on the marker's STRUCTURE — count anchor OR its signature tail prose — so a legitimate arg
that merely mentions "elided" is NOT rejected) and, on any hit, returns a RECOVERABLE failed `ToolResult`
(`invalid_arguments`: "argument(s) … contain the elision placeholder text … this call was NOT executed.
Re-issue with the FULL content, or file_read the path first"). The executor is the UNIVERSAL chokepoint
every tool call funnels through, so the placeholder can now NEVER mutate disk on ANY path — file_write,
file_replace_lines, any future mutator, or a direct executor call. It returns a recoverable tool failure
only — NOT a success, finish, or planning-state change — so Bug 6 / Bug 12 / W-45 are unaffected.

**Must-not-regress (verified):** core storage keeps FULL args — `SqliteEventStore` persists
`event.model_dump(mode="json")` and `ActionEvent.to_llm_message()` applies `_snip_args` only at
LLM-context RENDER time, never at storage; the durable event log is intact. The LLM context still snips
assistant tool-call args. The `StuckDetector` still catches truly-identical repeats.

**Proof** (unit — forcing the exact live model behavior is impractical):
`current/packages/tools/tests/test_executor_elision_guard.py` — the executor-DIRECT path: `file_replace_lines`
with the marker in `new_text` → recoverable `invalid_arguments`, tool NEVER runs, file on disk UNCHANGED;
a paraphrased (count-less) marker in `file_write.content` → also rejected, disk unchanged; a clean edit
runs normally; a benign marker-shaped arg is NOT false-rejected. `current/packages/core/tests/test_loop_stuck.py`
— two `file_replace_lines` with DISTINCT `new_text` of identical length (both snip to the SAME marker)
are NOT stuck (raw-args comparison pinned), while truly-identical repeats ARE still stuck.
`current/packages/core/tests/test_k1_elision_guard.py` — the Observer path now also covers `file_replace_lines`
(rejected, executor not called, one `AgentErrorEvent`, names `new_text`). Ruff + basedpyright clean,
import-linter KEPT, full tools + core suites green.

**NOTE:** the revise/steer soak STUCK is PARTLY model-quality — a capable driver does not copy the
placeholder back. This fix guarantees the placeholder can never mutate disk and the model always gets a
clean recoverable error on ANY path, but a CAPABLE model is still needed for the gate-level revise/steer
scenarios to reliably PASS.

### Bug 15 — the runner's terminal wait was a PROGRESS-BLIND wall-clock → a still-progressing build was cut off mid-flight + mislabeled `BUILD_DID_NOT_FINISH` (HARNESS) — FIXED

This was the §17 **no-fluke intermittency source**: the SAME `must_plan_before_tool` scenario PASSED in
one re-soak and FAILED (`BUILD_DID_NOT_FINISH`) in the next, with no product change between them.

**Root cause** (`development/harness/build_soak/adapters/disco_api.py` `poll_until_terminal` /
`poll_until_terminal_or_gate`, used by `run.py` `_drive_to_terminal`): the terminal wait gave up at a
FIXED wall-clock `timeout_s` *regardless of whether the conversation was still actively producing events*.
In the confirmed re-soak, `must_plan_before_tool` was emitting an event every ~10–13s and reached FINISHED
(`completed_via_notify`) at seq 42 — but the runner's 240s deadline hit at ~seq 31 **while the build was
still progressing**. It froze a non-terminal 32-event snapshot (last status RUNNING) and the classifier
read the missing terminal as a PRODUCT `BUILD_DID_NOT_FINISH`. The build finished 51s later. This
conflated "model slow / deadline short" with "the loop failed to finish."

**Fix** (`fix-bug15-progress-timeout`, **harness only** — no product/engine/messages/recitation touched).
The terminal wait is now **progress-aware** (`DiscoApiClient._poll_progress_aware`,
`adapters/disco_api.py`). It tracks a cheap progress fingerprint — `(event_count, max_seq)` from the
durable event log (`_progress_marker`) plus status transitions — and KEEPS WAITING while either advances.
The wait ends only on:
- a **genuine terminal / gate / pause** status → returned as before (a slowly-but-genuinely FINISHED run
  now collects the FULL events incl. the terminal and classifies normally — the must_plan seq-42 case
  PASSES);
- **genuine inactivity** — no new events and no status change for the `inactivity_s` window →
  `INACTIVE_TIMEOUT`, which the drive falls through to classify normally (a real wedge IS a finding:
  `BUILD_DID_NOT_FINISH` / `STUCK`); or
- a generous **hard cap** (`hard_cap_s`, default 1200s, well above a normal ~5min build) that bounds a
  truly-non-terminating run.

**Honest timeout classification:** a hard-cap cutoff reached **while the build was still progressing**
(events advanced within the inactivity window) returns the new `PROGRESSING_TIMEOUT` sentinel →
`_drive_to_terminal` raises `InconclusiveRunError` → `run_once` records **`INVALID_RUN`** with code
`RUN_TIMEOUT_WHILE_PROGRESSING` (`failure_codes.py`, a harness-validity code), NOT a product
`BUILD_DID_NOT_FINISH`. INVALID_RUN means "the runner could not obtain a terminal verdict," so the §17
no-fluke policy **re-runs** it instead of recording a false product failure. Genuine inactivity stays a
real terminal/stuck finding — the two are distinguished (actively-progressing-then-cut-off = inconclusive;
stopped-progressing/wedged = real).

**The knobs** (`run.py` CLI): `--timeout` is now the **progress-aware inactivity window** (no-progress
silence budget, default 180s) — NOT a blind wall-clock; the separate **`--hard-cap`** (default 1200s) is
the safety ceiling. A still-progressing build is never cut off by `--timeout` elapsing.

**Proof** (`development/tests/test_api_runner.py`, fake transport): (a) a TINY inactivity window does NOT cut off a
build that keeps emitting events — the wait holds to the real FINISHED; (b) a frozen (no-new-events) build
returns `INACTIVE_TIMEOUT` → `BUILD_DID_NOT_FINISH` (a real finding, NOT inconclusive); (c) the hard cap
bounds an always-progressing never-terminating run → `PROGRESSING_TIMEOUT` → `INVALID_RUN` /
`RUN_TIMEOUT_WHILE_PROGRESSING`, pinned to be **NEVER** `BUILD_DID_NOT_FINISH` / `FAIL`. Ruff +
basedpyright clean, import-linter KEPT, full build-soak suite green. Live re-run of `must_plan_before_tool`
against the shared :8000 now WAITS for the real terminal and classifies on it.

---

### Bug 17 — the runner did NOT answer a mid-build clarifying question → an interactive build that asks one false-stalled into `NO_PLAN` (HARNESS) — FIXED

Another §17 **no-fluke intermittency source**, on the SAME `must_plan_before_tool` scenario: the 27B
sometimes asked a clarifying question BEFORE planning, intermittently driving the conversation to
`AWAITING_USER_QUESTION`. The runner left it unanswered → the build sat idle until the inactivity window
and was classified `NO_PLAN_AFTER_USER_TURN` — a **false stall**, not a product failure.

**Root cause** (`development/harness/build_soak/run.py` `_drive_to_terminal`): the drive loop acted ONLY on
`AWAITING_PLAN_APPROVAL`. `AWAITING_USER_QUESTION` and `WAITING_FOR_CONFIRMATION` are in the adapter's
`GATE_STATES` (`adapters/disco_api.py`) so the progress-aware poll RETURNS on them — but the loop had **no
branch to ACT on them**, so the build waited for an answer that never came. The runner is supposed to ACT
AS THE USER (§14 runner directive); it approved plans but never answered a clarifying question.

**Fix** (`fix-bug17-runner-clarify`, **harness only** — no product/engine/messages/recitation touched).
`_drive_to_terminal` now answers the two non-approval gates, keeping the runner "acting as the user":
- `AWAITING_USER_QUESTION` → SEND a clarification answer over the SAME `send_message` WS path a real user
  follow-up uses (appends the user turn + kicks the loop). The answer is the scenario-provided
  `clarification_answer` (a new OPTIONAL scenario field, default `None`) if set, ELSE a generic safe
  default that **instructs the model not to ask further questions** (`_GENERIC_CLARIFY_ANSWER`) so a model
  can't trap the build in a question-loop.
- `WAITING_FOR_CONFIRMATION` → confirm the pending risky action via the product's **REAL confirmation
  mechanism** — a new `DiscoApiClient.confirm(cid)` that sends the dedicated `{"type":"confirm"}` WS control
  frame (`routes/ws.py:89` → `runtime.confirm` → `control_ops.py:89` `ControlOps.confirm` →
  `loop.confirm()` + kick), the exact analogue of `approve_plan`. **A plain user `send_message` does NOT
  clear this gate** (codex follow-up caught the first cut answering it with a free-text "Yes, proceed"
  message, which the product ignores → the gate would never clear and the build would stall); only the
  `confirm` frame executes the pending action and resumes past the gate.
- **Bounded** by `_MAX_CLARIFY = 3` (mirrors `_MAX_GATES` / `_MAX_RESUMES`): a model that keeps asking past
  the cap is **let go** to a real terminal/inactivity and classified HONESTLY — never an infinite
  answer-loop, never a masked failure.

This does **not** weaken the oracle: answering a clarifying question is what a real user does; the resulting
build is still adjudicated end-to-end (plan→approve→execute→deliver). A build that STILL fails after a
reasonable clarification is a genuine finding — the fix only stops a clarify-question from being a **false**
stall. The existing approval-gate handling, the Bug-15 progress-aware wait, and the runner-hygiene teardown
are all intact.

**Proof** (`development/tests/test_api_runner.py`, fake transport): (a) a build that hits `AWAITING_USER_QUESTION` →
the runner sends the generic answer and proceeds to a clean terminal (PASS); (b) a scenario with a
`clarification_answer` → that EXACT answer is sent; (c) `WAITING_FOR_CONFIRMATION` → the runner clears it
with the REAL `{"type":"confirm"}` control frame (asserted — NOT a no-op message) and proceeds; (d) a model
that asks endlessly → answered up to `_MAX_CLARIFY` (=3) then let go → the
non-finished run classifies `BUILD_DID_NOT_FINISH` (NOT an infinite loop, NOT a silent pass). The prior
fail-closed test now pins an UNHANDLED gate (`AWAITING_USER_DECISION`). Ruff + basedpyright clean,
import-linter KEPT, full build-soak suite green. **Live note:** the local 27B was busy with a running soak,
so the unit tests (fake transport) are the proof for this fix.
### Bug 16 — a reserved-port preview SERVE STUCKs the build (Bug-7 containment refused the bind but gave NO recovery) — PRODUCT — FIXED

Surfaced by a §17 **no-fluke replay** (intermittent on the local 27B's port choice). The model served its
static deliverable via the preview shell: `tmux send-keys -t disco-{cid8}-preview -l 'python3 -m http.server
8000'`, then retried `5173`. Both are in the reserved control/UI set, so the Bug-7 process-backend
containment (`reserved_port_command_violation`, `ProcessSandboxInstance.exec_shell`) correctly REJECTED the
binds (they'd crash the agent-server / collide with vite) — but gave the model **no recovery path**: it never
established a preview, so the resolver found no conversation-owned port, `verify_web_app` reported
not-serving, and the build STUCKed `verify_no_progress`. (The existing `ensure_preview` remap covers only the
default auto-serve, NOT a raw model-launched `http.server <reserved>` via shell/tmux.)

**Fix** (`fix-bug16-reserved-port-serve`, remap at the preview-serve handling point + actionable refusal).
Two review rounds converged on **where** the remap may safely live. Review #2 proved a regex over the
ALREADY-WRAPPED/arbitrary `exec_shell` string can never be quote/heredoc-aware (`echo '; python3 -m
http.server 8000'`, a heredoc body, a `python -c` literal all contain a "separator" + serve-shape inside
quotes), so the remap was **relocated to the model's CLEAN command, before it is tmux-wrapped**:
- **Remap (essential) — on the clean `shell_exec` command** — `ShellSessionManager.exec`
  (`sandbox/shell_sessions.py`) now calls `remap_reserved_preview_serve` (`preview_target.py`) on the model's
  raw `command` BEFORE wrapping it into `tmux send-keys -l '<command>'`. The matcher is **anchored at the
  START** of that clean command (`^\s*(python -m http.server <reserved>)`) — which is what makes it SAFE
  without a shell parser: a leading `python` cannot be inside a quote/heredoc/echo-argument (nothing precedes
  it), so serve-shaped TEXT is NEVER rewritten. A reserved-port serve is rewritten to the process-safe port
  (`process_safe_preview_port()` → 3000); the remapped server runs in the conversation's tmux session → it is
  conversation-owned → `resolve_preview_port(host_shared=True)` (the same resolver `verify_web_app({})` and
  the finish gate use) targets it → the build can FINISH. Gated to SHARED-host backends via the SAME signal
  the verify resolver uses (`workspace_path is not None` ⇒ process/local); an ISOLATED container keeps 8000
  as its canonical app port and is never remapped. **The unsafe regex over arbitrary `exec_shell` strings was
  REMOVED** — `ProcessSandboxInstance.exec_shell` no longer rewrites anything; it keeps ONLY the best-effort
  containment REFUSAL (which never rewrites, just rejects). A serve buried after a `&&`/`;`/`|` separator or
  in any non-leading position is intentionally left to that refusal — refuse-and-guide is safer than risking
  a quoted-text rewrite.
- **Actionable refusal** — `reserved_port_command_violation`'s message now names the rejected port, the whole
  reserved set, AND a concrete safe replacement ("serve … on a non-reserved port such as 3000 instead"). And
  `_exec_outcome` (`system.py`) promotes an `ExecResult(126, stderr="refused: …")` to the ToolOutcome `error`
  text (not a bare "exited 126") so the model — whose loop drops tool `content` and emits only `error` —
  actually SEES the recovery guidance. This is the standalone safety net: even where the remap does not fire,
  the model is told a safe port and retries.

**Must-not-regress (verified):** kills + arbitrary reserved binds still refused (exit 126, never launched);
`exec_shell` rewrites NOTHING (an already-wrapped string is launched verbatim); `expose_port()` still None for
reserved process ports; explicit verify URLs to 8000/8800/5173 on shared-host still rejected; isolated
containers keep 8000 canonical; W-45 broken-app still fails verify.

**Proof — LIVE soak was running (local 27B busy), so per policy the UNIT tests are the required proof** (no
live soak run, port 8000 never bound by this work): `test_preview_target.py` (actionable message; remap of a
leading serve incl. absolute python path + leading whitespace; **review-#2 bypass negatives — a serve-shaped
string inside echo/printf/`python -c`/quotes/comment/heredoc/after-a-separator is NEVER rewritten**; the
remap operates on the clean command, not the tmux wrapper; remap→conversation-owned→resolver-targets-safe-port
composition), `test_shell_sessions.py` (`ShellSessionManager.exec` remaps a clean reserved serve on a
shared-host fake → the `send-keys` literal carries the safe port; keeps 8000 canonical on an isolated fake;
never rewrites serve-shaped text), `test_sandbox.py` (`exec_shell` launches an already-wrapped string verbatim
— no rewrite; kills + arbitrary binds still refused 126 with the actionable message), `test_shell_spill.py` (a
`refused:` 126 surfaces as the `error` text; ordinary nonzero exits keep the concise summary). Ruff +
basedpyright clean, import-linter KEPT, preview/verify/shell suite green. (One pre-existing, untouched
env-dependent assertion in `test_process_backend_expose_port_defense` fails only because the LIVE soak
currently holds port 3000 — not a regression of this change.)

---

### Bug 18 — a malformed tool-arg call STUCKs the build because the schema-validation error is not ACTIONABLE — PRODUCT — FIXED

Live **MiniMax-M3** finding on a `revise_after_finish` soak. The model called
`update_plan_progress({"steps": ["", "", ""]})` — empty strings where each `steps` item must be an OBJECT —
and kept resending the identical malformed call until the stuck breaker fired (`actionless_loop` → STUCK). The
validation error it saw, `argument 'steps.0': Input should be a valid dictionary or instance of
PlanProgressItem` (×3), was technically correct but **not actionable**: it never showed the EXPECTED nested
shape, the `state` enum, or a concrete example, so a model that mis-formats an array-of-objects could not
self-correct. The build failed not because the task was impossible but because a recoverable FORMATTING error
was never made recoverable.

**Fix** (`fix-bug18-actionable-validation`, in the single generic validation-error formatter). The tool
executor already routes every arg-schema failure through `describe_validation_failure`
(`current/packages/tools/src/disco/tools/executor.py`) — the one place whose string is the ONLY thing the model sees
(the structured `expected_schema` is not rendered into the next turn). It now appends, for ANY tool whose
failing arg is a pydantic model (or a list of models), a concise **nested-shape hint**: the expected shape
with the enum spelled out + ONE concrete example, e.g. for `update_plan_progress`:

> The 'steps' argument must be a list of objects, each shaped `{"index": <integer>, "state":
> "pending"|"active"|"done"}`. Example: `steps=[{"index": 1, "state": "pending"}, {"index": 2, "state":
> "active"}]`.

Generic by construction (derived from the model's fields/annotations), so a future nested-arg tool benefits
with no extra code; concise by design (the bad arg path + expected nested shape + enum + one example, NOT the
full JSON schema, so context isn't bloated).

**Must-not-regress (verified):** the schema is UNCHANGED — `["", "", ""]` is still REJECTED
(`invalid_arguments`), never coerced/normalized into objects (the tool body never runs on bad input); the
message is DETERMINISTIC for identical bad calls, so the loop's stuck detector still trips byte-for-byte; the
stuck breaker is untouched — if the model ignores the now-actionable error and keeps looping, it still attempts
the escape once then halts STUCK (no infinite loop).

**Proof — LIVE stack in use, so per policy the UNIT + loop-fake tests are the required proof** (no live soak):
`test_validation_executor.py` (the malformed `steps` call yields `invalid_arguments` whose model-visible
message contains `steps.0`, "list of objects", `index`/`state`, the `pending`/`active`/`done` enum, and a
concrete example; the corrected `[{"index": 1, "state": "done"}]` call SUCCEEDS; a generic non-`update_plan_progress`
tool with a malformed nested arg gets the same treatment — proving genericity; the rich message is
deterministic), and `test_bug18_actionable_validation.py` (loop-level via the REAL executor: the first
`AgentErrorEvent.to_llm_message()` the model receives carries the shape + example, the corrected call produces
a success `ObservationEvent`, and the SAME malformed call repeated past the stuck threshold still drops a
`stuck_escape` marker then halts terminal STUCK). Ruff + basedpyright clean, import-linter KEPT.
### Bug 19 — a process-backend build could KILL the agent-server (or any host process) via `kill <pid>` — PRODUCT — FIXED

**P0, LIVE finding** (MiniMax-M3, `conv_68eeed23`, `multifile_static_site`, process backend). The model wanted
to serve its preview on port 8000 (the default the build prompt teaches), ran `ss -lntp | grep ':8000'` + `curl
http://127.0.0.1:8000/`, saw 8000 **occupied by the agent-server itself** (uvicorn pid 931479), and ran
`kill 931479 2>&1 || true ; …` — which **killed the agent-server** and took down the whole dev stack (the next
soak scenario got `INFRA_FAILURE/agent_server_unreachable`). The process backend shares the host PID namespace,
so a `kill <pid>` of a discovered PID hits arbitrary host processes.

**The gap.** The Bug-7/16 containment refused reserved-PORT bind/kill shapes **by port number**
(`fuser -k 8000`, `lsof …:8000 | xargs kill`, `--port 8000`), but a **raw `kill <pid>`** of a PID the model
discovered was NOT matched — nor `pkill`/`killall`/`kill -9`/`kill -TERM`. The security analyzer also rates
`kill|pkill|killall` only *medium* (not hard-denied), so nothing stopped the trivial stack-takedown.

**Fix** (`fix-bug19-process-kill-containment`). An ADDITIONAL refusal in the SAME containment boundary —
`ProcessSandboxInstance.exec_shell` (`sandbox/process.py`), immediately after the reserved-port check, before
`asyncio.create_subprocess_shell`. New module-level `process_backend_signal_command_violation(command)`
**blanket-refuses host-process-signal command shapes** on the process backend — `kill`/`kill -9`/`kill -TERM`/
`kill -s …`, `pkill`/`pkill -f`, `killall`, `fuser -k`, and the `lsof -ti:PORT | xargs [-r] kill` pipeline —
returning `ExecResult(126, stderr="refused: …")` **without invoking the launcher**. The refusal is actionable
and starts with `refused:` so `system.py` (`_exec_outcome`, Bug 16) surfaces it as the model-visible error:
*"killing host processes is not permitted on this (process) backend … serve your preview on a non-reserved port
such as 8080 (it runs in your named preview/dev session) … never `kill`/`pkill`/`killall`/`fuser -k` a host PID
or free a port."* Each signal verb is matched only as a **command head** (string start or after a shell
separator) so `kill`/`pkill`/`killall` buried in an `echo`/quoted arg, and `pytest -k kill_switch` (`-k` flag,
no word boundary), are NOT falsely refused. A build has no legitimate need to signal host PIDs — its OWN
foreground server is managed via the named preview/dev session (`shell_kill_process` →
`ShellSessionManager.kill_foreground`), which is unaffected.

**Scope / isolation (honest).** PROCESS backend ONLY — the dev-only weak-isolation backend that shares the
host. This is **best-effort command-pattern matching** (like the reserved-port scan): trivially bypassable (a
renamed binary, a raw `os.kill` inside `python -c`, env-indirection). The robust long-term answer is a
**PID-namespaced/isolated backend** — the container/gVisor backend already has its own PID namespace, so a
`kill` there only hits sandbox processes; its `exec_shell` (`_container.py`) is **NOT routed through this check
and is left UNCHANGED**. The containment is the must-have safety net that stops the trivial stack-takedown
until/unless any untrusted build is moved off the process backend.

**Complementary build guidance — deferred (follow-up).** The build prompt still teaches `8000` as the
user-visible preview port and says `shell_kill_process('preview')` to free it (`prompts.py:285/385`), which is
**correct for the isolated/container backend** (8000 is genuinely the sandbox's own port there). A
process-backend-only "8000 is platform-owned here; use 8080+; never kill processes or free ports" note would
require threading backend/isolation-awareness into the (currently backend-agnostic) `PromptLibrary` and
conditionalizing the port-8000 guidance — engine/prompt-internal sprawl that risks the container story. Left as
a follow-up; the containment refusal is the essential fix and stands alone.

**Must-not-regress (verified).** Normal build commands still reach the launcher (`pytest --version`,
`npm run build`, a SAFE-port serve `python3 -m http.server 8080`, file ops, installs, `pytest -k kill_switch`,
`echo "kill …"`, `… | xargs rm`); reserved-port kill shapes (`fuser -k 8000`, `lsof …:8000 | xargs kill`) keep
their existing port-specific refusal; the container backend's `kill` path is unchanged (own PID namespace);
Bug 6/7/12/16 + W-45 intact.

**Proof — LIVE stack in use, so per policy UNIT tests are the required proof** (no live soak, port 8000 never
bound by this work): `test_sandbox.py` — `test_process_exec_shell_refuses_host_signal_commands` (every signal
shape → 126 + actionable `refused:` text, the monkeypatched launcher NEVER invoked, incl. the exact live
`ss … ; kill 931479` shape), `test_process_exec_shell_allows_normal_build_commands` (controls reach the
launcher exactly once; no false-refuse of `pytest -k kill_switch` / quoted `kill` / `xargs rm`),
`test_container_backend_kill_path_unchanged` (`ContainerInstance.exec_shell` runs `kill 12345` inside the
container — NOT refused). Ruff + basedpyright clean, import-linter KEPT, preview/verify/shell suite green. (The
same pre-existing `test_process_backend_expose_port_defense` env flake — LIVE soak holds port 3000 — is
untouched by this change.)

---

## Runner hygiene — kill abandoned conversations (harness only)

**Symptom.** The runner left a build conversation **RUNNING** whenever it stopped watching —
an inconclusive `PROGRESSING_TIMEOUT` / hard-cap cutoff, an error path (mid-run transport loss →
`INVALID_RUN`), or simply releasing the conversation after evidence collection. Leaked RUNNING convs
accumulated and loaded the shared server (observed: 10+ leaked, manually `/kill`'d).

**Fix** (`run.py` + `adapters/disco_api.py`, **harness only**). `run_once` now wraps the
drive → assemble → classify body in a **try/finally**; the `finally` calls `_release_conversation`,
which **kills the conversation the run created** via the existing kill route
(`POST /conversations/{cid}/kill`, conversations.py:275) — a new `DiscoApiClient.kill()` adapter method
mirroring `resume()`. Properties:

- The cid is tracked on the client (`last_conversation_id`, set in `create_build_conversation`
  immediately after create, before the kick) so the teardown can reach it **even when drive_scenario
  raised before returning** a `CollectedRun`. It is reset to `None` at the start of each `run_once` so a
  reused client only ever kills the conversation **this** run created.
- The kill happens **AFTER** evidence is collected + frozen (the §6 terminal-events read already
  happened inside `drive_scenario`; `assemble_dossier` ran before the `finally` on the happy path), so it
  never races the dossier.
- It kills **only a still-non-terminal** conversation (RUNNING / PAUSED / AWAITING_*). A conversation that
  reached a genuine terminal (FINISHED / ERROR / STUCK / IDLE) is left alone — no wasted teardown, no
  double-kill.
- Best-effort + **idempotent**: any error (server gone, already terminal) is swallowed so teardown never
  turns a recorded verdict into a crash. A pre-create `INFRA_FAILURE` (no conversation created) returns
  before the try/finally — nothing to kill.

**Proof** (`development/tests/test_api_runner.py`, fake transport): an inconclusive/abandoned run (never-terminal,
progressing cutoff → `INVALID_RUN`) issues **exactly one** `/conversations/{cid}/kill`; a cleanly-terminal
run (smoke PASS) issues **zero** kills; `kill()` is idempotent on an already-terminal conv; and
`_release_conversation` swallows an unreachable server (no exception escapes teardown).

## New bare-Build scenarios (§3 / §28 diversity)

Three scenarios added to `scenarios.yaml`, mirroring the existing schema; each has a deterministically
checkable oracle expectation (specific files + `must_contain` markers and/or a required preview), all
satisfiable by a competent model and verifiable by the existing oracles. None assert `tool_scope` (no
per-turn tool-scope evidence yet — file header). **Not yet soaked live** (model busy); validated by unit
tests + schema checks only.

1. **`multifile_static_site`** — a 3-file static site (`index.html` + `about.html` + shared `style.css`
   with a `site-nav` nav linking the pages). Asserts all three files exist with per-page markers
   (`'Welcome Home'` / `'About Us'`), the shared class (`site-nav` referenced in the pages, `.site-nav`
   defined in the css), cross-page nav links, preview required (root serves `'Welcome Home'`), terminal
   FINISHED/VERIFIED. *Exercises multi-file delivery.*
2. **`revise_twice_complex`** — initial SaaS landing build, then **two** sequential change follow-ups
   (add a 3-tier pricing section → change every CTA to `'Get Started'`), each requiring a fresh revised
   plan (rev 1→2→3) before any write (the §11.4 RevisionOracle chain). Final `index.html` must carry the
   **cumulative** result (`Acme Cloud` + `Starter`/`Pro`/`Enterprise` + `Get Started`), terminal FINISHED.
   *Stepping stone toward §3's "complex revisions ≥3 follow-ups" — this is 2; a 3-followup variant can
   extend it later.*
3. **`verify_catches_broken_then_fixed`** — a richer build → serve → **verify** → finish on a contact page
   with a `<form>` (name + email inputs + a `'Send'` submit). Kept **achievable** (clean single pass,
   quote-style-agnostic markers — not a forced failure). Asserts the deliverable content + preview +
   terminal FINISHED. **NOTE:** a TRUE broken-then-fixed fault-injection scenario (force a verify failure,
   assert the loop repairs + re-verifies before finishing) needs **runner support to inject a verify
   failure** into the live build — no such hook yet, so that variant is **deferred**.

**Schema guard** (`development/tests/test_api_runner.py::test_every_scenario_loads_with_a_valid_schema` +
`::test_new_scenarios_assert_deterministic_oracle_checkable_output`): every scenario in `scenarios.yaml`
loads, has the required keys, uses only known assertion/event-chain keys + the terminal vocabulary, never
asserts `tool_scope`, and any `requires_plan_revision` followups declare
`revisions.expected_final_plan_revision` (the ContractOracle well-formedness rule).

**Flakiness note.** All markers are dictated verbatim by the prompts so a competent model emits them; the
verify scenario's markers are quote-agnostic substrings to avoid single/double-quote drift. The mildest
risk is exact-class-name fidelity in `multifile_static_site` (`site-nav`), but the prompt names it
explicitly. None are expected to be flaky for a capable model.
