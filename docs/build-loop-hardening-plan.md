# Disco build-loop hardening — vetted plan (2026-06-13)

Synthesis of 10 parallel deep-dives (6 Pi/MiniMax-M3 gap analyses + 4 Claude-Sonnet
architecture designs incl. an adversarial reviewer), cross-checked against my own
reading of the engine. OpenHands is the reference (Disco is event-sourced like it).

## Headline finding — Disco is FAR further along than the issue list implied
Disco already implements most of the OpenHands/Cline patterns. The honest answer to
"should we implement all of these?" is **NO — 2 of 6 are already done; 4 are real.**

| # | Candidate | Verdict | Evidence |
|---|---|---|---|
| 4 | Graduated stuck recovery | **DROP — already exists** | `stuck.py` = OpenHands' 4-pattern detector; engine.py:1986-2089 = escape(temp 0.9)→STUCK→circuit-breaker handoff |
| 6 | Condensation refinements | **DROP — already exists** | `LLMSummarizingCondenser` (view.py:472-501): soft_frac 0.65/hard 0.80 capped 24k/32k; masked stubs already recoverable (view.py:286-331); plan pinned + recitation tail (view.py:137-222). Residual = param tuning only |
| 5 | **Autonomous mode** | **KEEP — #1, highest leverage** | No autonomous/interactive distinction exists; recovery assumes a human (ask_user / AWAITING_USER_DECISION). The `__continue__` self-resume mechanism ALREADY EXISTS (engine.py:3257-3278) — autonomous mode just self-invokes it |
| 3 | **finish-verify cap** | **KEEP — tiny, proven pattern** | `_finish_verify_passed` (engine.py:1795-1883) is the ONLY uncapped gate; mirror the existing `_browser_verify_refusals` cap-3 release valve (engine.py:869, 2647-2684) |
| 2 | **plan_step spam cap** | **KEEP — real gap** | plan_step cycling through indices evades BOTH StuckDetector (args differ) AND `_consecutive_noops` (engine.py:1294-1300 `continue`s past bookkeeping) |
| 1 | Edit safety guards | **KEEP (scoped) — low priority** | Real silent-deletion tool is `file_replace_lines` (no forgiveness), NOT `file_edit` (already forgiving + A8 warns). Cheap targeted guards only |

## The unifying insight (priority #1)
**Disco's loop is built INTERACTIVE; OpenHands is built HEADLESS.** Every degeneracy
recovery in Disco ends in "ask the human" (ask_user → AWAITING_USER_DECISION/QUESTION).
In an unattended build run that becomes a STALL (observed: calc RUN2/RUN5). Aligning
Disco with OpenHands' "never ask for human help, continue" — an **autonomous mode** —
is the priority-#1, OpenHands-shaped change, and it SUBSUMES the headless-stall behind
#5+#7+the circuit-breaker handoff. It is also *small* because the resume machinery
(`_CONTINUE_OPTION_ID`) already exists.

---

## The vetted plan — 4 fixes, ordered by leverage

### A. Autonomous mode  (engine.py + prompts.py + agent-server runtime.py)
**Gap:** no autonomous flag; recovery halts for a human. **Injection (all GO, layering-safe):**
- `AgentLoop.__init__(autonomous: bool = False)`; threaded from `ConversationRuntime` via a persisted per-conversation sidecar (`_autonomous: dict[str,bool]`, B0 pattern, set at create time), exactly like `mode`/`_surface`/`_model_override`. Dependency stays agent-server→core.
- `_tools_for_step`: when autonomous, OMIT `ask_user`/`clarify` from the advertised tool list (planning + execution branches).
- Defensive intercept: if the model still calls ask_user/clarify, convert the halt to a non-blocking ENVIRONMENT note + `continue` (backstopped by the existing `_actionless_valve`).
- Plan gate: auto-approve inline (emit the SAME `PlanEvent` + `StatusEvent(RUNNING, detail="plan_approved")` the external `approve_plan()` emits — event log identical).
- Circuit breaker Phase 2: instead of `AWAITING_USER_DECISION`, self-invoke the existing `__continue__` path (reset streak, resume) OR clean-forfeit to STUCK after a bounded retry. (Sonnet-A and Sonnet-D converge here; D's "self-invoke the existing `__continue__`" is the minimal core.)
- `DriverPrompts(autonomous=True)`: prepend OpenHands/Cline rule — "no human is available; make reasonable assumptions, record them with notify_user/remember, never end with a question."
**Fit:** GO. Pure additive flag, default False = today's behavior unchanged; event-sourcing + layering preserved. **Risk:** plan self-revision loop (add a revision cap); STUCK-vs-FINISHED forfeit semantics (document); weak-model ask_user hallucination (covered by the actionless valve).

### B. finish-verify cap + malformed-strip  (engine.py only)
**Gap:** the model-authored `verify` is the only uncapped gate; a broken oneliner loops to max_iterations. **Injection:** add `self._finish_verify_refusals = 0` next to `_browser_verify_refusals`; in the finish handler (engine.py:2510) increment on fail; at 3 → warn-loudly-and-proceed (mirror engine.py:2673-2684 verbatim); reset on pass + at run-segment entry. Distinguish *malformed* (exit 127 / SyntaxError substring) → auto-strip the verify for that attempt + warn (no counter bump), bounded per session. **Fit:** GO — applies Disco's own existing cap-3 idiom to the one gate that lacks it; stays in core; event log already carries the verify probe as `ActionEvent(meta={"verify_probe":True})`. **Risk:** weakens a deliberate "test before done" forcing function → the release MUST be loud (system-reminder + a StatusEvent detail the UI renders as a warning); update `_FINISH_DESCRIPTION` in the same change; preserve the `verify_probe` meta tag (Phase-B leak guard).

### C. plan_step spam cap  (engine.py only)
**Gap:** plan_step cycling evades StuckDetector + the noop valve. **Injection:** a log-derived static `_bookkeeping_streak_signal(events)` (count trailing `_BOOKKEEPING_TOOLS`-minus-finish actions since last user msg / resume; reset on any real action), called in the pre-step diagnostics next to `_plan_step_lag_signal`. At N=3 → one-time nudge "you marked steps but did no real work — act or finish" (guard the re-fire with a `StatusEvent(detail=...)` marker, not a string match); at 2N → route through the existing `_actionless_valve`/PAUSED landing. **Fit:** GO — same shape as every other Disco spam guard (prose/serve/remember → noop valve); symmetric-and-non-conflicting with `_plan_step_lag_signal` (that one needs `productive>=total`; this one needs `productive==0`). Design B chosen over Cline's "progress-as-parameter" because the latter fights Disco's one-tool-per-step principle and needs ~6-8 files + every tool schema changed (Sonnet-B). **Risk:** end-of-task burst check-off false positive → optionally co-condition on `_actions_since_last_resume==0`.

### D. Edit safety (scoped)  (tools/builtin/files.py + registry.py)
**Gap:** `file_replace_lines` with empty `new_text` silently deletes a range; descriptions actively steer the model toward the fragile line tools. **Injection (cheap, low-risk only):**
- Deletion guard: `FileReplaceLinesTool.run` refuses empty `new_text` (the unambiguous data-loss case) with a helpful message. (Do NOT add a >K-line guard — legitimate large deletions exist.)
- No-op guard: `FileEditTool.run` refuses `old==new` (after ws-norm).
- Demote: drop `file_replace_lines`/`file_insert_lines` from `agent_scope().advertised_tools` (still callable by name; just not advertised) and FIX the FileRead/FileEdit descriptions that currently push the model toward them.
- (Optional, advisory) post-edit `python3 -m py_compile` note on `.py` writes — surface, never block.
**Fit:** GO on the deletion/no-op guard + demote (directly kills the 120b silent-deletion; A8 already steers via the snapshot preamble, so this is belt-and-suspenders). The uniqueness-refuse on `file_edit` is a CONDITIONAL go (exact-match only; the ws-normalized fallback over-refuses short anchors). **Risk:** check `test_executor_scope.py` for advertised-list assertions; the deletion guard's ~0% false-positive (whitespace-only replacement still possible via file_write).

---

## What we are deliberately NOT doing (and why)
- **Graduated stuck recovery (#4):** already implemented end-to-end (engine.py:1986-2089). Adding more would duplicate/conflict.
- **Condensation refinements (#6):** `LLMSummarizingCondenser` already does window-fraction triggers, recoverable masked stubs, plan pinning, and a recitation tail. Residual is parameter tuning, answerable only from live long-run traces — not an architecture change.
- **Cline "progress-as-tool-parameter" (plan_step option A):** fights Disco's one-tool-per-step principle; massive schema churn; weak models won't fill an optional field. Rejected in favor of the streak cap.

## Architecture verification (end-to-end)
Every kept fix (a) is **derivable from the event log** (preserves `View.of` purity — no fix needs hidden state), (b) **reuses an existing Disco idiom** (cap-3 release valve, the noop/actionless valve, the `__continue__` resume, the advertised-vs-allowed tool split), (c) **stays in `core`** except the autonomous flag which threads agent-server→core exactly like `mode`, and (d) emits only existing event kinds (StatusEvent details, ENVIRONMENT MessageEvents) — **no new event types, no store migration, no protocol change.** This is why the set is low-risk: it is Disco extending its own patterns, not importing foreign architecture.

## Recommended order + verification (per the protocol)
1. **A. Autonomous mode** — highest leverage; directly fixes the headless stalls (calc RUN2/RUN5). Verify: re-run the 120b 5×5 in autonomous mode → expect the ask_user/handoff stalls to vanish.
2. **B. finish-verify cap** — tiny; verify with a forced-bad-verify build test → finishes after ≤3 with a loud warning.
3. **C. plan_step cap** — verify with a build that induces bookkeeping spam → nudge fires, run completes.
4. **D. edit guards** — verify line-tool deletion refused + line tools no longer advertised; full edit unit suite green.
Each lands behind the existing real-app harness + unit suites; restart-from-1 on any failure (Rule 1).
