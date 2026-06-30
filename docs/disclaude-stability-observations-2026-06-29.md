# disclaude P1B-LIVE-STABILITY — what the stability matrix has observed (2026-06-29)

Goal of this work (your directive): **prove the P1B-LIVE browser product harness is stable,
fail-closed, and not hiding product failures** — via repeated MiniMax-M3 *direct-API* browser
product-harness builds — *before* building export capture (P10b) on top of it. Direct MiniMax
only (host `api.minimaxi.chat`, model `MiniMax-M3`, OpenRouter count 0, provider ledger every
run). This document reports what the matrix has surfaced so far.

---

## 1. Headline

The harness has **succeeded at its core purpose**: it is **fail-closed** and is **surfacing
real failures rather than green-washing them**. In the process it caught **2 harness bugs**
(now fixed + gated) and **2 genuine build-reliability failure modes** in MiniMax-M3. The
"10/10 consecutive PASS" target is currently blocked **not by the harness** but by MiniMax-M3's
**~50% finish rate** — a real model+engine limitation, not something the harness is hiding.

**Two different things are being conflated by a single number:**
- **Harness stability** — is the harness itself correct, deterministic, fail-closed? → **YES**
  (proven; see §2, §3).
- **Build reliability** — does a MiniMax-M3 autonomous build reliably reach a clean FINISH? →
  **~50%**, blocked by genuine thrash (see §4).

---

## 2. What was built + proven (STAB-1)

- **STAB-1a — negative fixtures (deterministic, 18 tests, gated).** Proves the classification
  fails closed on every required negative, each a minimal sole-change vs a passing control,
  asserting the *exact* failure code: missing slice/ledger/scenario_id → INVALID_RUN; OpenRouter
  host → FAIL (`PROVIDER_FORBIDDEN`); provider-call-after-terminal → FAIL (`SIDECAR_NOT_STOPPED`);
  nothing-shown → FAIL (`ARTIFACT_NOT_SHOWN_TO_USER`); orphan/not-released → FAIL
  (`WORKSPACE_NOT_CLEANED`); finished-without-verification → FAIL (`VERIFICATION_GATE_BYPASSED`).
- **STAB-1b — the live stability runner (gated).** `STATIC_SMOKE_STRICT` scenario *enforces* the
  `sidecar` slice; a single-build spec (no retry-to-pass) captures all 7 slices incl. the
  **sidecar / "no provider calls after terminal"** proof via a **ledger-native, clock-free,
  run-scoped** relay line-count offset (sound under append-only relay + strict sequencing); an
  orchestrator runs it N× sequentially, **records every run**, and **stops on the first non-PASS**.

These hold the line: **oracles untouched, no SKIP-as-PASS, every run recorded.**

---

## 3. Harness bugs the matrix caught (fixed + gated — STAB-2b)

The very act of running real builds exposed two harness defects that would have let bad data
through downstream. Both are now fixed with regression tests:

1. **Verification capture was too narrow.** It recognized only the `verify_web_app` tool. But
   disco's finish gate also verifies via a **`browser` inspection** of the served app
   (navigate + console check), and a build reaches FINISHED only once that gate is satisfied.
   Run-2's build genuinely verified *via browser* and finished cleanly — yet the harness
   false-flagged `VERIFICATION_GATE_BYPASSED`. Fixed with `decideVerification()` (8 vitest cases)
   that mirrors disco's *actual* gate: `verify_web_app` structured-pass **OR** a real browser
   inspection **plus** a clean terminal that is **not** an `unverified_release` /
   `unverifiable_static_finish` escape. Fail-closed: a called-but-failed verify can't back-door.
2. **Real FAIL verdicts were mislabeled `CLASSIFIER_ERROR`.** The Python classifier exits
   non-zero on a non-PASS (by design); the spec's `execFileSync` threw and the catch discarded
   the real verdict. Fixed to parse the verdict JSON from stdout in *both* exit branches —
   real FAIL/INVALID_RUN verdicts are now surfaced, not hidden.

**These are exactly the "harness could pass while the product is broken / could swallow a real
verdict" holes the stability work exists to find** — and they were found before export was
built on top.

---

## 4. The two genuine build-reliability failure modes (MiniMax-M3)

Both modes share a shape: MiniMax-M3 produces an *initial* deliverable, then **thrashes on
refinement/bookkeeping** until disco's stuck machinery halts it. The harness correctly records
these as `NO_CLEAN_TERMINAL` (it does not pretend they finished).

### Mode A — bookkeeping-malform stuck (`bookkeeping_only`)
- MiniMax-M3 intermittently malforms `update_plan_progress` (`steps:['']` instead of
  `[{index,state}]`); repeated malformed bookkeeping trips `gate_bookkeeping_streak` → STUCK,
  **after the deliverable was already produced and verified**.
- Evidence: an earlier run reached a deliverable + `verify_web_app` pass, then forfeited on
  bookkeeping spam.
- **Disposition:** the deliverable here is *verified* → a **finalize-on-verified-deliverable**
  engine fix (STAB-2a, which you pre-authorized) would convert this to a clean finish *safely*
  (only finalize genuinely delivered+verified work; never weaken the halt).

### Mode B — edit-elision thrash (`recovery_requested → STUCK`)
- Root cause: the **`_snip_args` elision interaction** (your `disco-runthru-v2-snipargs-keystone`
  note). disco elides large tool-arg bodies (e.g. a `file_write`'s content) in the model's
  *context view* for context management. When MiniMax then tries to `file_edit` that file, it
  cannot reconstruct the exact text — it either **echoes the elision marker** back (executor
  rejects: *"Argument(s) ['new'] contain an internal elision placeholder"* — correct) or
  **guesses** (`old_text_not_found`). It looped 12 failed edits (seq 44–116) → `recovery_requested`
  → STUCK at iter 55.
- Evidence (run v2-1, `conv_59b967ce…`): 120 events, 55 actions, **15 agent_errors**
  (`update_plan_progress` malform + `old_text_not_found` + `read_before_write` +
  elision-placeholder), browser-navigate at seq 26, **no `verify_web_app`**, final state
  mid-broken-edit.
- **Disposition:** here the final state is an **unverified, mid-edit (possibly broken)**
  deliverable → finalize-on-stuck would be **unsafe**. The honest fix is **elision-recovery
  robustness** (e.g. auto-`file_read` an elided file before an edit, or don't elide a file the
  model is actively editing) — a **deeper, riskier** engine change than STAB-2a.

### Provider-ledger proof (the P17 constraint) — clean where captured
- Run-1 (a clean PASS) ledger: **31 calls, all `api.minimaxi.chat`, 0 OpenRouter**, sidecar
  `stopped_at_terminal=True, provider_calls_after_terminal=0`. The direct-MiniMax / no-OpenRouter
  / no-post-terminal-call guarantees hold on the runs that complete.

---

## 5. Run ledger so far

| Run | Verdict | Mode / note |
|---|---|---|
| (attempt-1) run 1 | **PASS** | clean; sidecar 0-after-terminal; 31 calls / 0 OpenRouter |
| (attempt-1) run 2 | FAIL→fixed | **harness bug** (browser-verify unrecognized + CLASSIFIER_ERROR mislabel) → STAB-2b |
| (attempt-2) run 1 | FAIL | **Mode B** edit-elision thrash → STUCK (genuine build non-finish) |

Earlier campaign builds (pre-stability) showed Mode A (bookkeeping stuck) and several clean
finishes — overall ~50–70% finish, consistent with the `disco-m3-real-build-capability` note.

---

## 6. The decision (why I paused)

Reaching **10/10 consecutive** requires real **build-reliability** work, because the blockers are
genuine model+engine thrash, not harness bugs:
- **Mode A** → STAB-2a finalize-on-verified-deliverable (clear, safe, pre-authorized).
- **Mode B** → elision-recovery robustness (deeper, riskier).

The options I see (no oracle weakening in any of them):
1. **Build both fixes** — STAB-2a + elision-recovery, each gated, then re-run. Principled path to
   a real 10/10; substantial engine work + risk on the elision fix.
2. **Survey first** — run ~12 builds *without* stopping to quantify the exact finish rate + mode
   distribution (~1hr of builds), then scope the fixes from hard data.
3. **STAB-2a only, then reassess** — ship the clear/safe bookkeeping fix, measure the improvement,
   then decide if the riskier elision fix is worth it.
4. **Harness proven; change the build bar** — accept the harness is proven fail-closed (its core
   goal), record the ~50% finish rate as a known model limitation, and have the stability gate
   *measure + report* the real finish rate rather than require 10/10.

**My recommendation:** **Option 3 → then 2.** Ship STAB-2a first (it's the clear, safe,
pre-authorized fix and directly addresses the verified-deliverable forfeit), then run a no-stop
survey to measure how much it moved the rate and how often Mode B actually occurs — and only then
decide whether to take on the riskier elision-recovery change. This keeps every step gated and
data-driven, and never weakens an oracle.

P10b remains parked until P1B-LIVE-STABILITY is resolved.
