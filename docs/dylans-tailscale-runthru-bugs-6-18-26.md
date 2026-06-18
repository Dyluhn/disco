# Dylan's Tailscale runthru — bug report (2026-06-18)

Live testing of the merged campaign over Tailscale (`http://100.94.219.2:5173`).
Captured verbatim from Dylan, then triaged. This is the source of truth for the
follow-up fix set. Fix order: **export first** (Dylan is verifying it), then dig
into the rest (traces required for #4/#5/#6).

## §1 — Verbatim notes (DO NOT EDIT)

1. On Deep Research, the export doesn't work (already tracking).
2. There's a bug with the revise plan. I sent a revision and it appeared to
   infinitely load instead of actually revising the plan.
3. A gripe: when I'm in a search and I click on the New button, that should take
   me to a new search or build or whatever. Currently it keeps me in the same
   surface. Not a fan of that.
4. In the build surface, the agent had a few issues. It kept saying the system is
   telling it that a file didn't exist anymore. Isn't this what we set out to fix?
   You'll need to look at the traces to determine what's going on.
5. There is also a weird issue where on the last step of a build, the session
   pauses itself. You saw this occur with gpt-oss. I don't know why that is.
6. I tried to iterate on a project and added an idea for a new plan. The agent
   tries to just build immediately instead of re-entering plan mode and producing
   a plan accurate in revision numbers. This makes it loop more.
7. Is there a way we can give the agent better access to errors that surface? More
   verbosity and specificity — specifically the browser console errors. It spent
   quite a few turns trying to trace a bug down that more verbose errors would have
   helped with.

## §2 — Triage (initial; deepen with traces)

- **B1 — DR export 404** *(FIXING FIRST)*. Root cause CONFIRMED: `NeedMoreCard.tsx`
  `_fetchExportBlob` fetches a RELATIVE url `/api/conversations/{cid}/report/export`
  (no base) → resolves against the page origin (vite :5173) → 404. The audio export
  (same file) correctly uses `${agentHttpBase()}/conversations/...`. Fix: prefix the
  export fetch with `agentHttpBase()`. Introduced by the DR-1 "route exports through
  the server endpoint" change; unit tests mocked `fetch`, so the wrong origin passed
  green — the visual-evidence gap. Shared helper → fixes md/pdf/docx at once. NOT a
  Tailscale issue (would 404 on localhost too, no vite /api proxy).

- **B2 — revise-plan infinite load**. A plan revision hangs (spinner, never resolves).
  Surface TBD (DR plan-iterate vs build plan-approval). Trace the revision request +
  the loop's handling of a re-submitted/revised plan. Suspect: the revision endpoint
  or the loop not re-kicking after a plan revision, or a state stuck in a pending
  status with no resolution.

- **B3 — "New" stays in the same surface (UX)**. The New button should let the user
  start a fresh conversation on ANY surface (or surface-pick), not pin them to the
  current surface. Frontend routing/New-action bug.

- **B4 — build "file doesn't exist anymore" (HIGH — core fix area)**. The agent is
  repeatedly told a file is gone/stale. This is the area W2 (FileStateTracker
  stale-notice) + C18 (file_exists done-condition) addressed — so either (a) the
  FileStateTracker is emitting a FALSE "changed on disk / re-read" signal, or (b) C18
  still resolves against the wrong cwd and reports a present file as missing, or (c) a
  sandbox path/identity mismatch. MUST read Dylan's build trace. Risk: a regression or
  an incomplete fix in the very thing we set out to fix.

- **B5 — last-step self-pause**. On the final step the run goes PAUSED (seen on the
  gpt-oss acceptance: the model declared "All set!" via `notify_user` ×3 without
  calling `finish()`, so the W5 actionless valve paused it). The valve behavior is
  CORRECT (no thrash), but the UX is wrong: a completed build shouldn't look paused.
  Fix direction: treat "build complete + notify_user, plan steps done" as a clean
  FINISH (or prompt the model to call finish), not a pause; OR auto-finish when the
  DoD/plan is satisfied and the model goes actionless.

- **B6 — iterate without re-planning**. Adding a new idea to an existing project makes
  the agent build IMMEDIATELY instead of re-entering PLANNING and producing a revised,
  revision-numbered plan → more looping. Fix direction: a new instruction on an
  existing build conversation should re-enter plan mode (or at least produce a plan
  delta) before executing; the plan revision numbers must increment.

- **B7 — verbose/specific errors for the agent (esp. browser console)**. The agent
  should receive richer error detail — particularly browser console errors — so it can
  debug in fewer turns. Today the browser tool likely surfaces a truncated/generic
  error. Fix direction: pipe full console errors (and richer tool errors) into the
  observation the agent sees; consider a structured error block.

## §3 — Fix order
1. **B1 export** (now; Dylan verifying).
2. Trace-driven: **B4** (file-doesn't-exist — core), **B5** (last-step pause), **B2**
   (revise-plan hang), **B6** (iterate re-plan) — these need the live build/DR traces.
3. UX: **B3** (New button), **B7** (verbose errors — agent-effectiveness win).

---

## §4 — Surgical fix plan (trace-confirmed; ready to build)

> Root causes below are CONFIRMED against the live `disco.db` traces from Dylan's
> session (not hypothesised). Key evidence conversations:
> - `conv_f32adcb4` (macOS-clone build → "add a 3d game") — **B2+B6+B4**.
> - `conv_8bfa4677` (static-site build) — **B5** (PAUSED/actionless at seq 59).
> - `conv_7f34e7f3` (slides) — **B5** sibling.
>
> **Headline finding: B2 and B6 are the SAME bug** (see B2/B6 below). The
> subagent's first hypothesis ("post-restart mode-restore flips PLANNING→EXECUTION")
> was checked against the code and **disproved** — the seq-comparison at
> `engine.py:1051-1054` keeps `mode=PLANNING` correctly on a re-plan. The real
> failure is that PLANNING mode is *not enforced* on a re-plan turn.

### B1 — DR export 404  ✅ DONE (Dylan verified "exports work")
`NeedMoreCard.tsx:121` now prefixes `agentHttpBase()`. Committed. No further action.

---

### B2 + B6 — revise-plan hangs / iterate builds without re-planning  *(ONE bug)*
**Severity: HIGH** (breaks the whole iterate-on-a-project loop; is the "infinite load").

**Trace (conv_f32adcb4, verbatim seqs):**
```
203 FINISHED                       ← first build done
204 USER  "please add a simple 3d game to this."
205 STATUS RUNNING/planning        ← enter_planning() DID fire; mode set PLANNING
206 STATUS RUNNING/(none)          ← run() top
207 ACTION file_read js/main.js    thought: "Let me read the key files I need to modify to add a 3D game"
208..240  file_read ×16 more       thoughts: "Let me create the 3D game files and wire everything together"
225 ENV   <reground-anchors> GOAL: Build a simple macOS-inspired desktop clone…  ← OLD goal re-injected
230 USER  "make a plan"            ← user had to ask AGAIN; agent ignores it, keeps reading
241 IDLE/cancelled                 ← user gave up / killed
```
**No `plan` event is ever emitted** between 205 and 243. The frontend's revise
spinner (`useBuildStream`/`PlanPanel`) waits for a new `PlanEvent` that never
comes → **infinite load = B2**. The agent free-builds (reads-to-edit) instead of
re-planning → **B6**. Same root cause, two symptoms.

**Root cause:** the PLANNING gate (`engine.py:823-887`, `_gate_planning_mode`) by
design lets the planner call read tools to "gather context before proposing"
(Phase-1 exploration) and **falls through with no cap** (line 885-887; contrast the
*no-tool* branch which nudges). On a re-plan the model inherits an execution frame
of mind (its thoughts say "create the 3D game files"), so it explores indefinitely
and never calls `submit_plan`. Two aggravators:
1. **Reground re-injects the ORIGINAL build goal** (seq 225) — `signals`/reground
   pulls "Build a macOS clone", reinforcing *execute the old plan* over *plan the
   new request*.
2. **No planning re-assertion**: `enter_planning` emits `RUNNING/planning` but the
   next turn's framing/system-reminder doesn't tell the model "you are RE-PLANNING
   a new instruction; your ONLY terminal move is submit_plan."

**Fix (surgical, core loop):**
- **(a) Cap planning-mode exploration → force a plan.** In `_gate_planning_mode`
  (`engine.py:823`), add a counter `_plan_explore_reads` parallel to `_plan_nudges`.
  After N (propose **5**) consecutive planning-mode read/list tool calls *without a
  `submit_plan`*, inject a system-reminder on the read's observation:
  "You've explored N files in PLANNING mode without proposing a plan. You have
  enough context — call `submit_plan` now with the revision." Reset the counter on
  `submit_plan` or a user message. This is the forcing function the read-path is
  missing (mirror the no-tool nudge at line 875).
- **(b) Re-plan framing.** When `enter_planning` is entered with a non-empty `text`
  AND a prior plan was approved (a *revision*, not the first plan), emit an
  ENVIRONMENT system-reminder immediately after the `RUNNING/planning` status:
  "RE-PLANNING: the user added a new instruction to an existing build. Produce a
  REVISED plan (call `submit_plan`) — do not start editing files. The plan revision
  number will increment." Touch: `engine.py:enter_planning` (1496) — pass a
  `revision: bool` derived from "is there a prior `plan_approved` in the log".
- **(c) Reground must respect planning mode.** When `self.mode == PLANNING`, the
  reground-anchors block must re-ground on the *re-plan task* (the latest user
  instruction), NOT the original build GOAL. Touch: the reground emitter (find via
  `<reground-anchors>` string) — guard on mode and swap the anchor text.
- **(d) Plan revision numbers.** Confirm `_plan_from_args`/`PlanEvent.revision`
  increments across re-plans (the trace shows `rev=1` for both plans — likely not
  incrementing). Touch: `engine.py:_plan_from_args` (~834) — set
  `revision = (count of prior PlanEvents) + 1`.

**Files:** `packages/core/src/disco/core/loop/engine.py` (primary);
possibly `signals.py`/reground helper.
**Tests:** new `test_replan_forces_plan.py` — script: finished build → user msg →
assert (1) capped exploration emits the submit_plan nudge, (2) re-plan reminder is
emitted, (3) a `PlanEvent` with `revision=2` is produced before any execution
action, (4) reground in PLANNING mode references the new instruction not the old
goal. Frontend: a vitest that the revise spinner clears when a `PlanEvent` arrives
(guards the B2 symptom).
**Risk: HIGH** — this is the plan/execute lifecycle. Stay additive (counters +
reminders); do NOT change the FALLTHROUGH semantics for legitimate Phase-1 reads.
Re-run the full core loop suite + the W1/W5 regression tests.

---

### B4 — build "file doesn't exist anymore" (false advisory)
**Severity: HIGH** (the exact thing W2/C18 set out to fix — incomplete for containers).

**Trace (conv_f32adcb4):** `seq 199 ENV [advisory, C18] done-condition for plan
step 1 ("Create inde…")` fired while the files were on disk — the agent's own
thoughts (earlier in the run) called it "a false positive."

**Root cause (confirmed in code):** `plan_conditions.py` `check_file_exists_for_plan_step`
resolves `workspace = getattr(sbx, "workspace_path", None)` (line ~164). For the
**local container backend** (Dylan's default `sandbox_backend: local`),
`_container.py:256 workspace_path` returns **`None`** — and its own docstring
(line 259) literally says *"[callers] check `getattr(sbx,'workspace_path',None)`
and skip when None."* But `plan_conditions.py` does **NOT** skip: when `workspace`
is falsy it falls to line ~195 `p = Path(path_str); p.is_file()` — checking the
literal path against the **agent-server HOST cwd**, where the container's files do
not exist → **false MISSING** → the C18 advisory fires every step. (W5/C18 only
ever worked for the *process* sandbox, whose `workspace_path` is a real host path.)

**Fix:** make the existence check sandbox-aware instead of host-FS-bound. Two
options — recommend **Option 1**:
1. **Ask the sandbox** (correct, matches D's "container files aren't host-FS
   accessible" intent). Add a tiny `async def file_exists(self, path) -> bool` to
   the sandbox `Protocol` (`sandbox/base.py:123` area), implemented per backend:
   container → `exec test -f <path>` (or reuse the existing read/stat path);
   process → the current `Path` check. `check_file_exists_for_plan_step` calls
   `await sbx.file_exists(path)` when a sandbox exists; only the no-sandbox/fake
   path keeps the literal `Path` check.
2. **Skip when workspace is None** (cheap, honours the docstring): if `sbx` exists
   but `workspace_path is None`, treat the done-condition as *unverifiable* →
   **do not emit the false advisory** (return "unknown", not "missing"). Loses the
   real check for containers but removes the false negative.

**Files:** `packages/core/src/disco/core/loop/plan_conditions.py` +
`packages/tools/src/disco/tools/sandbox/{base,process,_container,session}.py`
(Option 1). Note the layering: `core` calling a sandbox method is fine —
`executor.sandbox` is already injected; keep the `Protocol` in core or duck-type
via `getattr`/`hasattr` to avoid an upward import.
**Tests:** `test_plan_conditions.py` — a fake container sandbox with
`workspace_path=None` + `file_exists` returning True must NOT produce the missing
advisory; a genuinely-absent file still does. Live: re-run a container build and
assert zero false C18 advisories in the event log.
**Risk: MEDIUM** — touches the sandbox Protocol (Option 1). Option 2 is core-only,
lower risk, weaker. Recommend Option 1 with Option 2 as the fallback if the Protocol
change ripples.

---

### B5 — last-step self-pause on a completed build
**Severity: MEDIUM** (cosmetic-but-confusing: a finished build looks broken).

**Trace (conv_8bfa4677):**
```
55 AGENT notify_user("All files are in place, the macOS-style site…")
56 AGENT notify_user("The macOS-style static site is fully built…")
57 AGENT "All set! …complete, served on …"
58 ENV   "The agent produced 3 consecutive responses without doing any real work…"
59 STATUS PAUSED/actionless
```
**Root cause:** the model signals completion with `notify_user` (×3) instead of
calling `finish()`. `notify_user` is in `_NON_PRODUCTIVE_TOOLS` (`signals.py:48`),
so after `_ACTIONLESS_BREAK_CAP=3` consecutive non-productive turns the actionless
valve (`turn_control.py` / `engine.py:540`) emits **PAUSED/actionless**. The valve
behaviour is *correct* (it prevents thrash) but the **UX is wrong**: the plan/DoD
was satisfied — this should read as DONE, not PAUSED.

**Fix:** when the actionless valve is about to PAUSE, first check completion: if all
plan steps are satisfied (or a recent `notify_user` clearly signals completion and
no plan steps remain), emit **FINISHED** (clean terminal) instead of PAUSED — OR,
one turn earlier, when the model emits a completion-flavoured `notify_user` with the
plan done, convert it to a `finish()` (auto-finalize). Recommend the **valve-side
check** (smaller blast radius): in the actionless-pause path, branch on
`signals.plan_steps_complete(events)` → FINISHED vs PAUSED.
**Files:** `packages/core/src/disco/core/loop/turn_control.py` (the valve) +
maybe a `signals.plan_steps_complete` helper.
**Tests:** `test_actionless_completion.py` — 3× notify_user with all plan steps
done → FINISHED (not PAUSED); 3× notify_user with steps REMAINING → still PAUSED
(preserve the thrash guard). Live: re-run the gpt-oss macOS build and assert it
lands FINISHED.
**Risk: MEDIUM** — must not weaken the genuine-stall pause. Gate strictly on
plan-completeness; default to PAUSED when unsure.

---

### B3 — "New" button pins you to the current surface  (UX)
**Severity: LOW-MEDIUM** (friction, not breakage).

**Root cause (frontend, confirmed):** the "New" button (`NavRail.tsx:27,104` and
`CommandPalette.tsx:23`) just `navigate("/")`. The `/` route renders
`MainSurface` (`App.tsx:25-30`), which reads the **persistent** mode from
`ModeProvider` (`useMode()`) — and `ModeProvider` (`mode.ts:18`, `ModeProvider.tsx:9`)
initialises once and **never resets**. So `/` re-renders whatever surface you were
last on; submitting then sends that hardcoded `surface` to the backend
(`api/agent.ts:61`, `api/deepResearch.ts:53`).

**Fix (recommend Option A — minimal, matches Dylan's "take me to a new search or
build or whatever"):** reset to the default/neutral surface on New. In the New
handlers, call `setMode("search")` (the landing surface) before/with
`navigate("/")`. Expose `setMode` from `useMode()` where needed.
- `NavRail.tsx:104` — onClick → `setMode("search")`.
- `CommandPalette.tsx:23` — action → `setMode("search"); navigate("/")`.
A stronger Option B (surface picker on `/`) is more work and a product decision —
flag for Dylan, default to A.
**Files:** `frontend/src/shell/NavRail.tsx`, `frontend/src/components/CommandPalette.tsx`
(+ maybe `ModeProvider` to ensure `setMode` is exported).
**Tests:** vitest — clicking New from build mode lands on the research/landing
surface. **Visual evidence: mandatory** — Firefox screenshot of New-from-build
landing on the search surface (per CLAUDE.md UI rule).
**Risk: LOW.**

---

### B7 — give the agent verbose/specific errors (esp. browser console)
**Severity: MEDIUM** (agent-effectiveness; burns turns chasing invisible errors).

**Root cause (confirmed):** the Playwright daemon *captures* console + pageerror
(`_browser_daemon.py:34-45`) but (1) `_add_pageerror` keeps only `err.message`
(drops `err.stack`), (2) console capture drops `msg.location` (file/line), (3) there
is **no `requestfailed`/`response` listener** (failed network requests invisible),
and (4) the render filter `browser.py:303` shows **only `error`/`warning`** to the
agent — `console.log/info/debug` (the app's own diagnostics) are silently dropped.
So when the build agent debugs a broken page, it sees a terse "(1 errors)" with no
stack, no source location, no failed-fetch signal.

**Fix:**
- **Daemon (`_browser_daemon.py`):** capture `err.stack` in `_add_pageerror`;
  capture `msg.location` (url, lineNumber, columnNumber) in `_add_console`; add
  `page.on("requestfailed", …)` and optionally `page.on("response")` for ≥400
  statuses. Keep the existing ring-buffer cap.
- **Render (`browser.py:_render_observation`, line ~294-327):** include a richer,
  structured error block — for each error: `level`, `text`, `source:line`, and the
  stack (truncated to ~N lines); include failed requests as
  `NETWORK FAIL: <method> <url> → <status/err>`. Keep `console.log/info` available
  (at least when there ARE errors, so the diagnostics that precede a crash are
  visible) but bounded so it doesn't blow the context budget.
**Files:** `packages/tools/src/disco/tools/builtin/_browser_daemon.py`,
`packages/tools/src/disco/tools/builtin/browser.py`.
**Tests:** extend `test_browser_daemon.py` — a page that throws + logs + a 404 fetch
→ the rendered observation contains the stack, the `source:line`, and the
`NETWORK FAIL` line. **Visual/live: mandatory** — drive a real broken page through
the browser tool and show the agent-visible observation now carries the detail.
**Risk: LOW** (additive capture + richer rendering). Watch the context-budget cap so
verbose console output doesn't re-bloat the prompt (tie into the existing MAX_CONSOLE
ring buffer).

---

## §5 — Build order & grouping

| # | Bug | Area | Severity | Risk | Depends |
|---|-----|------|----------|------|---------|
| B1 | export 404 | frontend | — | — | ✅ done |
| B2+B6 | re-plan not enforced | core loop | HIGH | HIGH | — |
| B4 | false file-missing | core + sandbox | HIGH | MED | — |
| B5 | completed-build pause | core valve | MED | MED | — |
| B3 | New button surface | frontend | LOW | LOW | — |
| B7 | verbose browser errors | tools | MED | LOW | — |

**Recommended order:** B4 first (highest value, contained, un-breaks the loop the
agent fights), then B5 (small, makes completed builds read correctly), then B2+B6
(the big one — re-plan lifecycle, needs the most care + a live acceptance), then the
two low-risk wins B7 and B3 in parallel.

**Parallelization:** B4 (core+sandbox), B2+B6 (core loop engine), B5 (core valve)
all touch `packages/core` — keep them in **separate worktrees** (the
`disco-parallel-worktree-kit`) to avoid the engine.py clobber we hit last time. B3
(frontend) and B7 (tools) are disjoint and can run fully parallel.

**Acceptance (live, real models — no fakes for the verify):**
- B2+B6: run a build → finish → add a new instruction → assert a `revision=2`
  PlanEvent appears and the agent re-plans (no infinite spinner).
- B4: container build → zero false C18 advisories.
- B5: completed build lands FINISHED, not PAUSED.
- B3/B7: Firefox screenshots (UI) + a real broken-page observation (browser).
