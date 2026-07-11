# Disco Session Writeup — 2026-07-08

Status of the mega-campaign work as of this session, what shipped, the live
frontier (glm driver stalls), how the flat-rate subscription plans drive the
work, and a concrete finish-out runbook.

---

## 1. Where we are — one paragraph

The provider-keys feature is **shipped and live**: you add a plan's key in
Settings, browse the models that key exposes, and toggle any of them into the
catalogue where they become first-class drivers. Four engine/loop reliability
fixes landed alongside it (monolith prevention, finish-detection oracle, a
mode-desync race, and an empty-turn repair). Pillar B (the UI build gauntlet)
is **3 sites fully clean + 1 nearly clean, 5 remaining**. The one open
investigation is glm-5.2 as a build driver: it interleaves real tool calls with
dead turns and stalls; the root cause is now captured at the wire level and the
fix is scoped but not yet written. Everything runs on flat-rate plans, so
per-run cost is zero.

---

## 2. Shipped this session (merged to `disclaude/mega-campaign`, live)

Commit chain (newest first):

| Commit | What |
|--------|------|
| `37657d1` | Test-fixture fix (models the STUCK watermark in a fake) |
| `f350bcc` | Merge: empty-turn repair |
| `7b8d3cf` | Session fix set (monolith gate, oracle v3, provider-key polish, compatibility gate) |
| `f1eb643` | Empty-reasoning-turn repair |
| `2bc8321` | Root-cause writeup for the glm stalls |
| `4a50878` | Post-approval mode-desync fix |

### 2a. Provider keys — the headline feature
- **Settings → Providers**: pick a preset (11 shipped) or a custom base URL,
  paste the plan's key, and the live model list for that key appears inline.
- Toggle a model on → it becomes an ordinary catalogue entry, visible in the
  Build model picker and every role assignment, **with zero engine changes**.
- Honest gaps by design: when a plan doesn't publish a model list, you get a
  clear notice plus a manual model-ID field (no fake list, no dead buttons).
- Follow-up polish (from your live testing): labels drop the internal
  `prov-...` scaffolding; unreported pricing shows **"pricing unknown"** rather
  than a fake "Free"; and **context window is required at enable time** — no
  silent small default that would starve the engine's context budgeting. Newly
  enabled models get sensible driver-grade capability defaults so they actually
  appear in the Build picker (which gates on tool-calling capability).

### 2b. MONO-1 — monolith prevention (your "linting is too late" ask)
- `file_write` / `file_append` refuse to create or grow a web-source file past
  **800 lines / 48 KB**. The refusal is a **diagnosis + recipe**: it names the
  exact limit tripped, states nothing was written, and gives a per-extension
  split recipe for the content in hand (HTML → extract CSS/JS + link them;
  CSS → split by concern; JS → ES modules).
- The build planner prompt now requires a **modular file layout from the first
  write**, and its scaffold example was changed from single-file to multi-file
  (examples teach harder than instructions).
- Shrinking rewrites, appends to legacy oversize files, and targeted edits are
  all exempt — the gate stops *new* monoliths forming, never blocks repairs.

### 2c. Gauntlet oracle v3 — dead-stream immunity
- The build status chip now exposes a monotonic **state-version** attribute.
  The gauntlet spec accepts a finished build by *version advance* rather than by
  witnessing the transition on a live stream, plus a watchdog reload if the
  version stops moving. This killed the "budget exhausted on a build that
  actually finished" false failures.

### 2d. Mode-desync fix
- A real race: after plan approval, a re-kick could compose a driver request
  with an execution objective but a *planning* prompt and planning-only tools.
  The loop now reconciles its in-memory mode from the durable event log at every
  run/drive boundary, so the prompt, tool surface, and objective always agree.

### 2e. Empty-reasoning-turn repair
- Some models return a turn with **no visible text and no tool call** (the work
  intent lands in a reasoning channel the loop can't act on). These used to
  count silently toward the stall breaker and **persist nothing**, making the
  failure invisible after the fact. Now: the provider classifies that shape, the
  loop retries the step once with a pointed reminder, and **every such turn
  persists a diagnostic event** so the log is self-explaining.

### 2f. Smaller landed fixes
- Completion self-check widened so reasoning-heavy models aren't mis-flagged as
  dead endpoints.
- STUCK detection reset on genuine forward progress (a watermark) instead of
  firing on slow-but-progressing runs.
- Compatibility gate: a serving-engine-specific request field is now sent only
  to self-hosted endpoints, since strict hosted plans reject it outright. This
  is what unblocked glm-5.2 from failing instantly.

---

## 3. The live frontier — glm-5.2 as a build driver

### What happens
glm-5.2 (via the OpenCode plan) **can** build — in isolated replays it writes
clean multi-file sites. But in the full loop it **interleaves real tool calls
with dead turns** and trips the actionless breaker before finishing.

### Definitive wire-level evidence (probe 2, captured this session)
Nine streamed turns, in order:

| # | finish | content | reasoning | tool call | class |
|---|--------|---------|-----------|-----------|-------|
| 1 | stop | 145 | 403 | `file_list` | real work |
| 2 | stop | 112 | 3026 | `submit_plan` | real work |
| 3 | stop | 187 | 209 | — | **prose-only no-op** |
| 4 | stop | 0 | 0 | — | **truly blank** |
| 5 | stop | 0 | 0 | — | **truly blank** |
| 6 | stop | 69 | 328 | — | **prose-only no-op** |
| 7 | stop | 0 | 0 | — | **truly blank** |
| 8 | stop | 0 | 0 | — | **truly blank** |
| 9 | stop | 835 | 3976 | `ask_user` | gave up, asked |

### Root cause (two dead-turn shapes)
1. **Prose-only no-op** (#3, #6): glm narrates its next move ("let me grab the
   tokens file and build index.html") with **no tool call in the same turn**.
   The engine correctly counts this as a no-op — it *is* one — but glm's habit of
   announcing-then-acting-next-turn burns the budget.
2. **Truly blank** (#4/5/7/8): `finish=stop`, **both** content and reasoning
   empty, a tiny output length, no call. The empty-turn repair we shipped
   targets `content=0 + reasoning>0`; these are `content=0 + reasoning=0`, so
   they slip the shape gate.

### The scoped fix (not yet written)
- **Widen the empty-turn repair's shape gate** to also match the truly-blank
  case (`finish=stop`, no content, no reasoning, no call, near-zero output) —
  same one-shot retry-with-reminder + diagnostic-persist path already built.
- **Add a prose-only nudge**: on the first prose-only no-op after approval,
  inject a single "call the tool in this turn, don't just describe it" reminder
  before counting it toward the breaker. One nudge, not a ladder.
- Both are shape-gated and driver-agnostic — protocol repair, not a
  model-specific crutch. glm keeps its place in the precedence once this lands.

### Instrumentation note
A temporary, clearly-marked wire-capture block is currently in the provider
(`openai_provider.py`, flagged `REMOVE-ME`, gated on a scratchpad flag file).
**Remove it once the glm fix is verified.** Separately, an *unidentified* writer
produced a `.wire-dump/` directory during an earlier live build — it is now
git-ignored, but the writer was never found and should be tracked down (it dumps
full model traffic into the repo root if it re-arms).

---

## 4. How the flat-rate plans drive — precedence

Everything runs on **flat-rate subscription plans**, so build runs cost nothing
per request. Two plans provide drivers; a third agent does implementation.

### Driver precedence (what runs a Disco build)
1. **OpenCode plan — deepseek-v4-flash** *(preferred for throughput)*
   Proven this session: clean multi-file builds in ~10 minutes, reliable
   autonomous auto-resume. This is the default driver for gauntlet runs.
2. **MiniMax plan — via the local relay** *(proven fallback)*
   The most-exercised driver of the campaign (Pillar B's first clean wave ran on
   it). Slower per build but rock-solid. Use when flash is rate-limited or when
   you want the known-good baseline.
3. **OpenCode plan — glm-5.2** *(pending the dead-turn fix in §3)*
   Fast and capable, but parked as a build driver until the two dead-turn shapes
   are handled. Fine for non-build roles now.

**Rule of thumb:** flash to move fast, MiniMax to be sure, glm once §3 lands.

### How to switch the driver (dev)
The driver is chosen by **two** places that must agree — this bit us once:
- `disco.db.last_model.json` — the per-owner **last-selected model** sidecar.
  It **outranks** the configured default at build-create time.
- the role **assignments** (what `dev_switch_driver.py` in the scratchpad sets).

To switch cleanly: set the assignments **and** write the sidecar to the same
model, then restart the servers (`~/disco-dev-up.sh`). Always verify **both**
after — the assignments view does not show the sidecar, so a stale sidecar
silently wins. Current sidecar: `prov-opencode-go-glm-5-2` (switch to
`prov-opencode-go-deepseek-v4-flash` before the Pillar B finish-out).

### Implementation agent (writes the code, not a Disco driver)
Codex (gpt-5.5) is the coding agent for spec-driven work. Pattern used all
session and recommended going forward:
- One git **worktree per task** (`git worktree add ../disclaude-wt-<name> -b
  wt-<name>`) so parallel work never collides and the live dev checkout is
  untouched.
- Hand it a `SPEC-*.md` with hard invariants + a "reproduce first" step + a
  "record deviations in FINDINGS.md" instruction.
- **Review the diff yourself and re-run its tests** before merging — codex is
  reliable but the merge decision is not delegable.

---

## 5. Finish-out runbook (priority order)

### P0 — close the glm driver (unblocks glm in the precedence)
1. Write `SPEC-glm-deadturn-widen.md`: widen the empty-turn repair to the
   truly-blank shape + add the one-shot prose-only nudge (§3). Reproduce-first
   with a scripted fake driver; 4–6 loop-level tests.
2. Codex implements in `wt-glmdeadturn`; you review + re-run suites; merge.
3. Restart, re-probe glm-5.2 on a small build. Success = files written, no
   stall; a residual stall now shows **persisted diagnostics**, not silence.
4. **Remove** the temp wire-capture block and its flag file.

### P1 — finish Pillar B (5 sites)
- Done clean: **records, leadgen, directory** (build + 5 iterations each).
- **form**: build + 4 iterations — one iteration short; re-run to close.
- Remaining: **seo, collection, flags, blog, analytics**.
- Switch the driver to **deepseek-v4-flash** (fastest), verify sidecar +
  assignments agree, then run the wave script (in scratchpad) at the doubled
  budgets. The oracle-v3 and monolith fixes are already live, so the earlier
  false-failure and monolith-thrash modes are closed.

### P2 — hygiene
- Track down the unidentified `.wire-dump/` writer (§3).
- The two pre-existing baseline test failures
  (`test_f5_thinking_budget` pair, one preview-session process-backend test)
  predate this session's work — triage and fix or quarantine.
- Fold the standalone OpenCode section in Settings into the generic provider
  presets (left intact this pass deliberately).

### P3 — remote-deploy reachability
- Work to make a packaged deploy usable from another machine's browser is
  **parked on its own branch for a separate review pass** — out of scope for
  this writeup, not on the critical path for the above.

---

## 6. Quick reference

- **Branch:** `disclaude/mega-campaign` (all §2 work merged).
- **Dev URL:** http://localhost:5173/
- **Restart:** `~/disco-dev-up.sh` (re-reads config, applies driver switches).
- **Driver switch helper:** `dev_switch_driver.py` (scratchpad) — sets
  assignments; you must also write `disco.db.last_model.json` to match.
- **Gauntlet evidence:** `test-record/gauntlet/<site>/RESULT.txt` + screenshots.
- **glm root-cause detail:** `ROOTCAUSE.md` (repo root, from the blind
  investigation) + this file's §3.
