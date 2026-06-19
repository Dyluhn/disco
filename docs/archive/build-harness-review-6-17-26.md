# Build-Harness Review Packet — macOS-clone build (2026-06-17)

This packet is the input for an **independent review**. It contains: the reported
symptoms, the exact task, the approved plan, observable facts from the run, the full
event trace, and a map of the build harness. **No hypothesis or root cause is offered
here on purpose** — the reviewer is asked to reach their own conclusions from the
evidence.

---

## 1. Reported symptoms (user, paraphrased to the observable behavior)

During a live build (`build a simple macosx clone`), the user observed:
- The agent **loops**: it checks the files on disk, "verifies the build," checks the
  files again, verifies again, says it needs to verify the same file again, goes back
  to testing the site, then reads files again — a constant bouncing between
  reading/verifying/testing rather than converging.
- It repeatedly states that **all the files are present on disk**, then immediately says
  something like *"now let me read X file"* or *"let me read the truncated … file"* and
  reads it again.
- **Plan steps are slow to get "checked off."**
- The **preview** shows some text from the app but **virtually none of the visual design**.
- The **live preview / "open" reports "preview not available."**
- The user also asked (a request, not a defect) that the agent be steered to **avoid
  monolithic files**.

The user's overall read: "we still have some serious issues with our build harness that
makes this so painful." They want an independent diagnosis of *why this run behaved this
way*, from the trace + harness, without being led to a pre-formed answer.

---

## 2. The task and the approved plan

**Task (initial user message):** `build a simple macosx clone`
**Mid-plan user steer:** `do not create a single monolithic html file please.`

**Approved plan (7 steps):**
1. Scaffold project directory structure + base `index.html` (folders `css/`, `js/apps/`, `assets/icons/`; thin index.html that loads all CSS/JS).
2. Build the macOS-style CSS across separate files (`base/style.css`, `menu-bar.css`, `dock.css`, `windows.css`).
3. Implement the menu bar with a live clock (`js/menu-bar.js`).
4. (Dock, windows, apps — finder/calculator/about — per subsequent steps.)
…the plan deliberately spreads the app across **~13 files** (per the user's no-monolith steer).

---

## 3. Observable facts from this run (counts only — interpret as you see fit)

- Total events: **374**. Outcome: status **FINISHED**.
- Action tool histogram: **file_read 82**, browser 46, file_write 13, plan_step 8,
  shell 7, file_list 6, file_edit 2, shell_kill_process 2, shell_exec 2, code_exec 2,
  search 1, extract 1.
- Thoughts containing "remaining / haven't seen / truncated / full": **91**.
- Occurrences of the string "chars elided" anywhere in the trace: **0**.
- The project on disk ended at **13 files** (index.html + 5 CSS + 7 JS).
- A `browser` click to launch the Calculator **timed out** (`Page.click: Timeout 30000ms …
  waiting for locator [data-pmx-index='0']`), after which the agent returned to reading files.
- Several `file_read` observations are paginated, e.g. `[lines 1-266 of 449; read more with
  offset=267]` and `[lines 200-278 of 278]`.

(These are raw counts pulled from the event log; the reviewer should verify and extend
them, not take them as conclusions.)

---

## 4. The full trace

`docs/evidence/macos-build-trace-6-17-26.txt` — the complete, ordered event log for
conversation `conv_6483d49d30f045699016b2946ca34523`, rendered as
`[seq] ACTION tool(args…) / thought / → OBS result`. The raw payloads are in `disco.db`
(`SELECT seq,kind,payload FROM events WHERE conversation_id='conv_6483d49d30f045699016b2946ca34523' ORDER BY seq`).
Other build/agent conversations for cross-reference: `conv_303536044bf246fd912e11e33e9f005f`
(earlier build), `conv_06eb76f34d9c4a159fdc0905f6be8a0a` (an agent/slides run).

---

## 5. The build harness (where the code lives — read it directly)

The build/agent surfaces run the same machinery. Relevant code, to read and judge:

- **Loop engine + turn flow:** `packages/core/src/disco/core/loop/engine.py`
  (the agent loop), `driver.py` (model call), `agent.py` (BuildAgent), `control.py`,
  `finish.py`.
- **What the model sees each turn (context rendering):** `loop/view_render.py` (the
  workspace snapshot + how the running file-state and prior actions/observations are
  rendered into the prompt), and `core/events.py` (how each ActionEvent/ObservationEvent
  becomes an LLM message, including any size/shaping transforms applied at render time).
- **Loop-control valves:** `loop/turn_control.py` (the gates/valves run each turn),
  `loop/stuck.py` (stuck / no-progress detection), `loop/dedup.py` (de-duplication of
  repeated content), `loop/signals.py`, `loop/recitation.py`.
- **Prompts:** `packages/core/src/disco/core/llm/prompts.py` — the planning and execution
  driver prompts (incl. small-model variants) and the file/verification rules they contain.
- **Tools the agent used:** `packages/tools/src/disco/tools/builtin/files.py`
  (`file_read`/`file_write`/`file_edit`/`file_list`), `shell.py`, `browser.py`, `slides.py`;
  and the tool registry/scope in `packages/tools/src/disco/tools/registry.py`.
- **Sandbox + preview:** `packages/tools/src/disco/tools/sandbox/`, and the preview
  routes `packages/agent-server/src/disco/agent_server/routes/preview.py` +
  `preview_service.py` (relevant to the "preview not available" / blank-design symptom).

There are known tunables in the harness (snapshot size caps, per-arg/observation size
shaping, dedup/stuck thresholds, assist-tier behavior). The reviewer should locate these
themselves and decide which, if any, bear on the observed behavior — and whether the
cause is in the harness, the prompts, the tools, the model's behavior, or some
interaction.

---

## 6. What the review should produce

An independent root-cause judgment of **why this specific run loops / bounces / under-
delivers the preview**, grounded in the trace and the code (file:line), with each claim
marked CONFIRMED vs HYPOTHESIS, and concrete fix directions. Do not assume any prior
diagnosis is correct; derive your own.

---

## 7. Consensus of the two independent reviews (2026-06-17)

Two Opus reviewers worked this packet independently (no shared hypothesis, no steer).
They converged. **This supersedes the earlier `_snip_args`/F8 "keystone" framing for the
BUILD loop** — that elision bug is real and corrupts the *slides* path, but this macOS
build trace has ZERO elision markers; the build loop is a context-management +
breaker-blindness problem.

**Dominant (both CONFIRMED) — the harness manufactures "re-read this file" signals.**
The per-turn workspace snapshot can't stably represent 13 files: 8-file cap
(`_WS_MAX_FILES=8`, view_render.py:41) → 5 always omitted with "file_read them"
(view_render.py:229-233); a read rotates recency and evicts another (messages.py:64-66);
`windows.js` (8,047 B) exceeds BOTH the snapshot per-file cap (6000, view_render.py:42)
AND the file_read page budget (7000, files.py:23) → never "fully seen" → re-read 15×; old
read-observations mask to a stub that commands re-reading (view.py:386-388). The 91
"haven't seen/truncated" thoughts are the model obeying. The user's no-monolith steer
(→13 files) tipped it past the 8-file cap.

**Dominant (both CONFIRMED) — every brake is blind to a read loop.** Stuck-equality
includes `thought` (equality.py:36-40) so varied narration defeats patterns 1/2/4; WALK-19
only counts edit→probe (stuck.py:334-386); F9 read-dedup is gated to assist tier
(dedup.py:9-15) so capable models get none; noop counts only tool-less turns. The loop ran
~350 events until `noop_limit`.

**Contributing (both CONFIRMED) — the model can't actually verify.** Browser element
walker matches only a/button/input/[role] (_browser_daemon.py:179-181); the dock is
`<div>`+addEventListener → no `[data-pmx-index]` → every `click(index)` times out 30 s
(seq 171/226/262/286). No vision (`DRIVER_VISION` off, browser.py:194) → model sees only
innerText, can't see the design, yet the prompt mandates "unseen build is not finished"
(prompts.py:208-214) → unbounded verify loop.

**Contributing (both CONFIRMED) — false plan done-conditions.** C18 `file_exists` resolves
against a None `workspace_path`/`executor.sandbox` (plan_conditions.py:163-197; base.py:98-118
exposes no workspace_path) → falls back to agent-server CWD, not /workspace → every
`plan_step(done)` answered "missing" even for served files. (Plus a real name mismatch:
`js/menu-bar.js` vs `js/menubar.js`.) This whiplash is the real "steps slow to check off".

**Separate bug (both) — preview.** App in `macos-clone/` subdir but preview serves the
workspace root (session.py:420-422) → directory listing = "text, no design"; model killed
the managed session + ran its own subdir server (seq 151/209); post-teardown
`wake_for_preview`→None → "preview not available" (preview.py:46-51). (root-serving CONFIRMED;
503 path HYPOTHESIS.)

**Consensus fix order (all additive; assist kit preserved):**
1. Scale/redesign the snapshot + masking (budget by chars not 8-file count; don't evict/mask
   the active plan's files; stop "re-read me" for files already shown; align 6000/7000).
2. Thought-independent semantic-repeat breaker covering reads/navigations (hash tool+args,
   exclude `thought`).
3. Enable F9 read-dedup for all tiers, not just assist.
4. Fix the browser tool (text/CSS locators, broader element detection, short click timeout) +
   route screenshotting build/agent runs to a vision driver (or drop the visual-verify mandate).
5. Fix C18 to resolve against the real sandbox FS; never "missing" for a file in the live snapshot.
6. Fix the preview to serve a subdirectory app + keep it alive after finish.

`_snip_args` execution-guard remains valid for the SLIDES/artifact corruption, but is NOT the
build-loop driver and is demoted accordingly.
