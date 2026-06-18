# Autonomous execution log — surgical master plan (started 2026-06-18)

Dylan authorized full autonomous execution of `docs/disco-surgical-master-plan-6-18-26.md`
with rigorous standards, AFK, "do not stop to ask questions… execute it." App not in
use (free to change/restart). DeepSeek OK as the running-app model. Parallelize
(Sonnet subagents ≤4, MiniMax-in-Pi, Gemini).

## Decisions made autonomously (were §E of the master plan)
1. **Slides A/B/C** → run the **C4 experiment** to decide empirically (answers the
   "is a constrained schema viable" concern directly).
2. **Image providers (C7)** → `bundled | openai-images-compatible | comfyui`.
3. **FILE-DELIVERY UX** → inline `FileDownload` feed card reusing the `SheetDownload`
   anchor idiom (sub-plan A).
4. **DR steer/inject** → build both; **steer first**, inject second (heavier ENGINE).
5. **noVNC** → build P1–P4; **P5 live-jail acceptance is hardware-blocked** (VM 202
   destroyed) → implement + unit/structure-verify, mark the live jail proof deferred.
6. **Live-app model for acceptances** → `or-deepseek-deepseek-v4-flash` (reliable,
   avoids the Chutes free-pool issue). Driver P1/P2 acceptance still runs on `:free`
   to PROVE the hardening (not a workaround).
7. **Driver default** → sticky last-pick (P3).

## Standards held (non-negotiable)
4 fitness gates green from MAIN checkout (worktree pyright is unreliable — empty venv);
`.venv/bin/python3 -m pytest -m "not integration"` exit-code-as-truth; UI changes need
a real Firefox screenshot + SendUserFile; engine changes need a live real-model
acceptance; real-sample harnesses; OFF-paths byte-identical; no false affordances;
preserve originals. Worktree-kit isolation for parallel lanes; integrator merges +
re-gates from main.

## Progress (append-only)
- 2026-06-18: plan committed `2fb4800`. Starting Wave 0 (4 parallel lanes + C4 kick + RP-09).

### Wave 0 — DONE + verified (2026-06-18)
- **Driver P1/P2/P3** (`14fe27b`): provider-pref injection + routing-retry + sticky last-pick.
  **LIVE PROOF**: build acceptance on `or-gpt-oss-120b-free` (the model that Chutes-errored
  3× earlier today) now completes — B2/B4/B5/B6 all PASS, zero provider ERRORs. P1/P2 works.
- **F1 keep-searching** (`fcb117c`): bounded ≤2 reformulate on no-answer; OFF-path byte-identical.
- **C5 render/preview** (`5ca117a`): deriveSrcDoc null, "0 B" suppressed, `?inline=true` sandboxed route.
- **C6 artifact_mode** (`74019e7`): NeverConfirm + INTERACTIVE + artifact_scope wiring.
- Integration `42f6ada`: merged all 4, resolved runtime.py/files.py conflicts (extracted
  `_read_artifact_bytes`/`_artifact_response`, hoisted `_INLINE_CSP`, cap 1320), synced
  `provider_prefs` into 2 stray agent-step fakes (the recurring fake-sync class).
- Gates: pyright 0, lint 0, arch OK, diagram fresh, full pytest EXIT 0, frontend tsc 0 + vitest 444.
- NOTE: Serena MCP unavailable this session (launched from home dir, repo .mcp.json not loaded);
  held symbol-nav discipline manually. Relaunch from repo dir to restore it.

### Wave 1 — dispatched (Sonnet×4 + Gemini + Pi/MiniMax)

### Wave 1 status (in progress)
- chart_svg relocation + P4: DONE (`e875249`, wave1-pi-misc) — MiniMax drafted imperatively, I fixed _LOG bug + committed.
- MiniMax delegation finding: works on IMPERATIVE step-lists, NOT spec docs; drafts ~partial then exits before test/commit. Workflow = MiniMax drafts → I complete+gate+commit.
- Drafts in worktrees (MiniMax, uncommitted): editor (9 files: selectionBridge/Overlay/agent + iframe wiring), c7img (10 files: image backends + config), attach (2 files). filedelivery: redraft needed.
- Next: complete each draft lane-by-lane with full gates, then integrate.
