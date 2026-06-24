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
| 4 | `APPROVE_PLAN_NO_EXECUTION` | P0 | **FIXED** (fix-approve-noexec) | `test_plan_approval_execution.py::test_kick_after_approval_produces_action_or_terminal_failure` (xfail removed, now passing) | `finish.py` `gate_execution_nudge` (`_EXECUTION_NUDGE_CAP` → STUCK terminal) | §11.2, §20.4 |
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
> finish gate in core AND `verify_web_app` in tools). **The PRIMARY, load-bearing fix is #1
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
>    soak), **tracked as a follow-up**. Containment is not the load-bearing fix; #1 is.
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
**Snapshot is AUTHORITATIVE — no proxy mask (anti-false-PASS hardening).** The preview-proxy
fallback fires ONLY when there is NO snapshot at all (no `projects_root`, or the conversation's
snapshot workspace dir never materialized within `--snapshot-wait`). When the snapshot workspace dir
exists, the proxy is NEVER consulted: a declared file absent from the snapshot is genuinely missing
and is OMITTED — the proxy can't substitute a served/stale copy to mask a missing required deliverable
(`FALSE_FINISH_NO_OUTPUT` preserved). **LIVE-PROVEN:** re-running `static_html_minimal` populates the
manifest from the snapshot (`index.html` present, content "Build Smoke OK", real sha256/size,
`source≠preview_proxy`) → workspace truth checks PASS. Pinned:
`test_api_runner.test_collect_workspace_{reads_snapshot_when_preview_proxy_404s,
genuinely_missing_file_is_omitted,falls_back_to_proxy_without_projects_root}`,
`test_snapshot_authoritative_does_not_proxy_mask_missing_required_file`.

### Bug 10 — runner PREVIEW-collection has the SAME ephemeral-proxy fragility (HARNESS) — FIXED

Surfaced by the Bug 9 live re-run: with the workspace manifest now correct, the SAME
`static_html_minimal` smoke fails one step later with **`FALSE_FINISH_PREVIEW_BROKEN`**
(`preview_health_status: 404`). `collect_preview` read the preview via the same fragile
`GET …/preview-app/` proxy, which 404s post-FINISH because the model's served preview
(`python3 -m http.server 8080 -d /workspace`, a backgrounded shell process) is torn down when the
run ends. This is NOT a bad build and NOT Bug 9: the event log PROVES the preview served correctly
DURING the run (`verify_web_app` on :8080 → passed; `shell curl …8080 | grep 'Build Smoke OK'` →
exit 0); `…/preview-edit/index.html` (snapshot-backed) returns 200, so the deliverable is durable.

**Fix (HARNESS-only `adapters/disco_api.py` `collect_preview`).** When the LIVE proxy serves
(status < 400 AND non-empty), that IS the truth and is used unchanged. When it is down, the runner
produces a preview health/content ONLY from GENUINE POSITIVE EVIDENCE — never a forged 200:

- **Serve + probe (Option A — positive evidence, the residual-gap fix).** When a STATIC served-root
  (`index.html`, root-preferred then shallowest) is durable in the snapshot — `_snapshot_served_index`,
  which MIRRORS the product's `lifecycle._find_snapshot_index` skip set (`.pmx` / `.disco` /
  `node_modules`) so an internal tool `index.html` is never the served root (hole #3) — the runner
  SERVES that snapshot dir itself on an OS-assigned FREE loopback port (port 0 → ephemeral high port;
  NEVER a reserved control port 8000/8800/5173; always torn down in `finally`) and HTTP-PROBES `GET /`.
  The REAL probe (status + served body) is the evidence — INDEPENDENT of whether the agent ran an
  in-run verify, which closes the residual hole: a build that NEVER verified no longer gets a 200 from
  mere absence-of-failure; it gets a 200 only if the deliverable ACTUALLY serves the required content.
  A non-serving / unreadable / wrong-content snapshot → the probe genuinely fails / the body lacks the
  needle → preview FAILs (never masked; the body is the REAL served bytes, so a wrong-content snapshot
  can't forge the needle).
- **Verifier-failure veto (hole #2).** If the build's own LAST in-run `verify_web_app` FAILED —
  `_in_run_verify_failed`, where FAILURE = `structured.passed is False`, the verifier FAILED TO EXECUTE
  (`tool_result.success is False`, no verdict), or a verdict/error signalling failure — the runner
  believes that broken-verdict and does NOT claim OK even if the static shell would serve.
- **No static served-root** (dynamic-only app, or no deliverable) → honest 404 →
  `FALSE_FINISH_PREVIEW_BROKEN`. Live dynamic-app preview verification is the documented follow-up —
  never a forged pass.

**INVARIANT:** the runner NEVER reports preview-health-200 without genuine positive evidence the
deliverable serves the required content. **LIVE-PROVEN PASS** (`static_html_minimal`, `conv_75881694…`
→ `conv_e29cc4a2…` → `conv_c33248cc…`): proxy down post-FINISH, the runner served+probed the snapshot
(health 200, `served.html` = the real probed body), OutputTruthOracle PASS, overall **PASS**.
Anti-false-PASS pins: `test_api_runner.test_collect_preview_{no_verify_serves_and_probes_for_genuine_evidence,
no_verify_probe_carries_real_wrong_body,does_not_substitute_on_verifier_execution_failure}`,
`test_snapshot_served_root_skips_internal_dirs`, `test_durable_preview_does_not_mask_wrong_content`.
Pinned: `test_api_runner.test_collect_preview_{uses_durable_snapshot_when_proxy_404s,
does_not_mask_failing_in_run_verify,no_durable_deliverable_stays_broken,live_proxy_wins_over_snapshot}`,
`test_static_build_classifies_pass_with_dead_proxy_via_durable_sources`,
`test_durable_preview_does_not_mask_wrong_content`.

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
