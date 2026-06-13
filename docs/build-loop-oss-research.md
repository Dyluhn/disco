# Disco build-loop: OSS prior-art research (2026-06-13)

Eight build-agent-loop issues surfaced during live testing (Gemma E4B + gpt-oss-120b).
For each, what Aider / OpenHands / SWE-agent / Cline actually do (source-cited), and
the recommendation for Disco. Refs cloned at `~/agent-refs/{aider,openhands-aci,SWE-agent,cline}`.

The through-line: **every failure we hit was a harness/prompt defect, never the model.**
gpt-oss-120b ≈ GPT-4.5 (what Manus ran on). The OSS leaders solved all eight already.

---

## 1. File-state visibility across turns  — ✅ DONE (validated against Aider)
**Proven pattern:** Aider re-reads each in-chat file from disk EVERY turn (`base_coder.py:598-607`)
and places that block AFTER the condensable `done` history (`chat_chunks.py:16-26`) — file
bodies never live in the summarizable transcript, so eliding them from history is harmless.
Trust banner: "*Trust this message as the true contents of these files*" (`base_prompts.py:24-28`).
OpenHands/SWE-agent/Cline reach the same end differently (str_replace on live disk; windowed
re-read; chokidar change-detection + re-inject).
**Disco:** the A8 live-workspace snapshot does exactly this (disk-fresh, post-condensation,
trust banner). Matches Aider. **Keep.**

## 2. File-context formatting (fence contamination)  — ✅ DONE (could upgrade to XML)
**Proven pattern:** NO leader uses a fixed ``` fence for editable content. Aider's `choose_fence()`
dynamically picks a non-colliding delimiter (`base_coder.py:609-635`); its candidate list even
comments `# LLMs ignore and revert to triple-backtick, causing #2879` (`:77-85`) — our exact RUN5
bug is a *known Aider issue*. Cline wraps content in XML `<file_content path="…">` (`responses.ts:208`).
Line numbers: shown only in a separate *view* tool (`cat -n` / `N | line`), NEVER in the editable
block (Aider keeps the numbering code commented out, `:651-653`).
**Disco:** switched snapshot from ``` to plain `----- BEGIN/END FILE -----` (collision-free).
**Upgrade option:** XML `<file path="…">…</file>` (Cline) + strip-echoed-delimiter on ingest
(Aider `strip_quoted_wrapping`, `editblock_coder.py:335-362`).

## 3. Reliable edit primitive  — 🔧 PROPOSED (high priority)
**Proven pattern:** 3 of 4 use CONTENT-ANCHORED edits as the default/only primitive — Aider
SEARCH/REPLACE, OpenHands `str_replace(old_str,new_str)`, Cline `replace_in_file`. NONE exposes
an unguarded "replace lines start..end". Guardrails: **uniqueness refuse** on >1 match
(`editor.py:224-228`), **fail-closed + re-anchor hint** on no-match (Aider `find_similar_lines`),
**no fuzzy edit-distance fallback** (Aider disabled it deliberately, `editblock_coder.py:183`),
**post-edit parse/lint check with revert** (SWE-agent flake8 before/after + undo, `windowed_edit_linting/bin/edit:94-124`).
**Disco:** `file_edit` is already content-anchored (good). **Demote/guard the line-number tools**
(`file_replace_lines`/`file_insert_lines`) — they silently deleted code on gpt-oss-120b. 80/20:
steer to content-anchored + uniqueness-refuse + **empty/deletion guard** + post-edit parse check.

## 4. Plan/TODO bookkeeping loops (`plan_step` spam)  — 🔧 PROPOSED (high priority)
**Proven pattern:** Cline does NOT expose plan-marking as a tool — progress is a `task_progress`
PARAMETER piggybacked on a real action (`assistant-message/index.ts:46`; `focus_chain.ts:5` is a
non-tool placeholder). A bookkeeping-only turn is **structurally impossible**. Counts are derived by
parsing `[x]`/`[ ]` (`utils.ts:13`); reminder injected only every ~6 turns (`FocusChainSettings.ts:10`).
OpenHands stuck-detector fires at **4** identical (not our 15-31).
**Disco:** primary fix — demote `plan_step` to a `plan_update` parameter on edit/run/finish tools.
Fallback — cap consecutive bookkeeping-only actions at 2 + "you marked steps but made no edit — act or finish."

## 5. Completion / self-verify gating (finish-verify loop)  — 🔧 PROPOSED (high priority)
**Proven pattern:** NOT ONE leader lets the MODEL author a shell verify that hard-blocks finish.
Verify is harness-owned + fixed (Aider `test_cmd`/lint, applied then **proceeds** after `max_reflections=3`),
or a separate capped critic (SWE-agent reviewer `max_accepts`), or model self-assert + stuck backstop
(OpenHands `task_completed` bool, no gate), or a one-shot re-verify nudge that doesn't count as a
mistake (Cline `AttemptCompletionHandler.ts:69-91`). Every fix-verify path is capped → surface-and-proceed.
**Disco:** make `verify` advisory (run, surface output, DON'T block on exit code); cap failed-finish
at 3 then accept+attach failure; **auto-strip a malformed verify** (exit 2/127 = broken gate, not failed task).

## 6. Stuck / loop detection & recovery  — 🔧 PROPOSED (high priority)
**Proven pattern:** OpenHands `StuckDetector` — 5 signatures over a 20-event window, reset after the
last user message, compared by SEMANTIC content (tool+args+obs, ignoring ids): identical action→obs ×4,
action→error ×3, monologue ×3, alternating A/B ×6 (`stuck_detector.py`). Cline graduated recovery:
soft@3 = inject "called X 3× identically, not making progress, use a different tool" (`responses.ts:309`);
hard@5 = trip mistake-limit → escalate model / halt-and-ask (`ToolExecutor.ts:585-594`).
**Disco:** replace the single temperature-jitter with content-signature detection (catches the 33×-rewrite
and oscillation cases jitter can't) + a soft-message → escalate-model/strategy → halt ladder.

## 7. Autonomy vs over-asking the user (`ask_user` stalls)  — 🔧 PROPOSED (medium)
**Proven pattern:** in autonomous/headless mode, a real block is made IMPOSSIBLE: Cline STRIPS
`ask_followup_question` from the schema when `yoloModeToggled` (`ask_followup_question.ts:12`) + prompt
flips to "make reasonable assumptions… without asking" (`rules.ts:23`, "don't end with questions"
`objective.ts:14`); SWE-agent has no ask tool at all (stuck → submit/forfeit); OpenHands intercepts
and auto-answers "NEVER ASK FOR HUMAN HELP, continue" + clean `exit`.
**Disco:** an autonomous mode that drops `ask_user` from the schema + "assume-and-proceed, note the
assumption" prompt rule + auto-answer backstop. (This is why the calc runs stalled on a specified task.)

## 8. Context condensation preserving file state  — ✅ PARTLY DONE (refinements proposed)
**Proven pattern:** pin file state out of condensation (Aider panel-rebuild; everyone keeps system +
original task + recent N + per-file latest view). Cheap deterministic pass (dedup repeated file-reads to
latest-verbatim + pointer, Cline `ContextManager.ts:809-942`) BEFORE LLM summary; trigger on a fraction
of the model's window (Cline 0.75) not a fixed 24k; API context-window-error fallback; recoverable stubs
(re-fetch handle, not bare hash); structured summary schema preserving CODE_STATE/task IDs
(OpenHands `summarizing_prompt.j2`).
**Disco:** file state now survives via the snapshot (root cause closed). Refinements: pin original task
+ last N actions; window-fraction trigger; make the sha256 mask recoverable (path+range); dedup-before-summarize.

---

## Priority for the build loop becoming reliable on capable models
1. **#4 plan_step → parameter** and **#5 advisory verify + cap** — these caused most observed STUCK/incomplete.
2. **#6 stuck detector (content-signature + graduated recovery)** — the catch-all backstop.
3. **#3 demote line-edit tools + deletion guard** — closes the silent-deletion clobber.
4. **#7 autonomous ask_user suppression** — closes the question-stall.
5. **#2 XML delimiter + strip-on-ingest**, **#8 condensation refinements** — robustness.

Done this session: #1 (file-state snapshot), #2 (de-fenced), file_write steer (#3 partial), pin file state (#8 partial).
