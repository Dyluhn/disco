> **SUPERSEDED / HISTORICAL (as of 2026-07-07).** June-era finish-all-discrepancies plan, frozen mid-execution.
> Current status of record: `docs/disco-project-state.md` (master), `docs/disco-status-and-remaining.md` (features + remaining), `sec-work-remaining/disco-security-state.md` (security). This file is kept for history and may contain stale claims.

# Finish-all-discrepancies plan (2026-06-22)

Closes every gap between "marker present" (commit 97ffbed) and "done + proven", across 4 tiers.
Tier 3 = the PROOF tier, driven by **MiniMax via Dylan's MiniMax API key** as Disco's live driver
model — this is what turns the 98 regression-tier fixes into *proven-works* ([[feedback-live-model-proves-works]]).

Git discipline throughout: stage only the touched files; NEVER stage/commit the B1-B7 WIP
(`packages/core/.../loop/engine.py`, `messages.py`, `recitation.py`, `uv.lock`); never stash-pop.
Each order: change → scoped test → gate (tsc/eslint/vitest + pytest/ruff/basedpyright) → commit.

---

## TIER 1 — finish the campaign's half-wired items (bounded, no live stack) → 1 commit

**T1.1 — #98 fire-now backend route.** Add `POST /api/conversations/{cid}/schedules/{sid}/fire-now`
to `routes/schedules.py`; implement `fire_schedule_now(sid)` on the schedule service
(`schedule.py`/`schedule_service`) to run the job immediately via the existing APScheduler/runner and
emit a `schedule_run` event into the conversation event store. Acceptance: the existing disco-verify
`fire_schedule_id` scenario path resolves a real 2xx + a `schedule_run` event (no longer a dangling
client seam); new pytest for the route + service.

**T1.2 — #63 confirmContext callers.** `frontend/src/views/HistoryView.tsx` + `ProjectsView.tsx`: pass
`confirmContext="history-delete"` / `"project-delete"` to the delete `ConfirmDialog`. Acceptance: the
two delete dialogs render distinct `data-confirm-context`; vitest for both.

**T1.3 — #20 revise→send handle.** `frontend/src/components/build/PlanPanel.tsx`: add
`data-disco-control="revise-plan-send"` to the revise-textarea submit (the 2nd step after "Revise…").
Acceptance: handle present + unique; PlanPanel vitest.

**T1.4 — deck-element-edit dup.** `editor/ElementBox.tsx`: split the shared `build.deck-element-edit`
into `build.deck-element-edit-input` (input) + `build.deck-element-edit-textarea` (textarea). Acceptance:
no `uniq -d` collision; DeckEditor vitest.

Gate: full frontend tsc/eslint/vitest + agent-server pytest green (modulo the 4 Tier-2 reds). Commit
"finish Tier-1 half-wired UI-control items (#98 route, #63/#20 handles, deck dup)".

---

## TIER 2 — green the 4 pre-existing reds (investigate → fix) → 1 commit

For each: `git log -S`/blame to establish whether the behavior change was intentional (then update the
stale test) or a regression (then restore the source). Decision recorded inline.

**T2.1 — NeedMoreCard "Build a deck".** Confirmed stale: source does `createBuildConversation(null,
"agent",false)` + `/agent/` (runthru-v2, intentional); test asserts `(null,"build",true)`. → update the
test expectation + route to `agent/false` + `/agent/`.

**T2.2/T2.3 — UploadComposer attach ×2** (`ResearchSurface.attach`, `DeepResearchSurface.attach`):
test asserts the composer is ABSENT when `preCid` null / `agentLive` false, but it renders. Investigate
the attach feature (commit 99c2058): if the guard regressed → restore `preCid`/`agentLive` gating on the
UploadComposer render; if the empty-state attach is intended → update the tests. Decision after reading
the source.

**T2.4 — PreviewPane "Static preview"** + the orphaned `isBundlerEntryHtml`: the bundler-vs-static
preview-defaulting was dropped in a refactor (function defined, unused; test still expects the static
srcdoc default). Decide: re-wire `isBundlerEntryHtml` into the `showEdit`/preview branch to restore E2
(preferred — it's a real correctness behavior), else delete the function + update the test. Removes the
last eslint error too.

Acceptance: `npx vitest run` = 0 failed; `npx eslint .` = 0 errors. Commit "green the 4 pre-existing
branch-debt reds (stale test + attach guard + preview bundler-default)".

---

## TIER 3 — PROOF via MiniMax (the live-model scenario matrix) → evidence, not a code commit

Prereq P0 — **MiniMax key: FOUND + LIVE-VERIFIED (2026-06-22).** Key at `~/.config/minimax/api-key`
(`export MINIMAX_API_KEY=sk-c…`). Smoke-tested against `https://api.minimax.io/v1/chat/completions`:
**MiniMax-M2 → HTTP 200**, **MiniMax-M3 → HTTP 200** (both valid; `abab6.5s-chat` rejected). NOTE: both
emit `<think>…</think>` reasoning blocks in `content` — the driver must strip them (Disco already does
this for Qwen3.6; confirm in T3.0). The harness reaches the same key via the Anthropic-compat gateway
(`api.minimax.io/anthropic`); Disco uses the OpenAI-compat `/v1`. No Dylan action needed.

**T3.0 — Provision MiniMax as the live driver.** Add `driver-minimax` to
`~/.config/disco/disco-config.json`: OpenAI-compatible, `base_url="https://api.minimax.io/v1"`,
`model_id="MiniMax-M2"` (the agent/tool-calling-tuned model; A/B vs `MiniMax-M3` on a real build-loop
tool-call and pick the stronger tool-caller). Store the key in Disco's encrypted `secrets.json` under a
`minimax` provider (read it from `~/.config/minimax/api-key`). Verify: (1) tool-calling round-trips,
(2) streaming works (driver MUST stream — [[perpleximanus-streaming-driver]]), (3) `<think>` blocks are
stripped from the surfaced answer. Set as the build/agent + DR driver (default, or
`model_override="driver-minimax"` in the verify scenarios).

**T3.1 — Stack up + tunnel.** `~/disco-dev-up.sh` (app-server + agent-server + vite, DISCO_SECRET_KEY
set, kill the :8000 squatter). Reverse-SSH tunnel workstation→VM-201 (`-R 5173/8000/8800`) so VM-201
Playwright hits the live MiniMax-driven app ([[disco-novnc-live-browser]], [[gvisor-sandbox-host]]).

**T3.2 — disco-verify scenarios LIVE (backend boundary proof).** Run all scenarios with MiniMax driving:
`python -m disco.agent_server.verify.scenarios_run` (slides_from_research_report, missing_file_sandbox_
error, app_from_build, steer_then_stop_build, research_report_export, + the new fire-now). Each must
reach a terminal status, produce a REAL delivered artifact, pass the W14 validators, write a redacted
dossier under `test-record/disco-verify/`. PROVES: real model → real tool calls → real deliverable.

**T3.3 — Per-surface Playwright on VM-201 (UI-control proof).** Run `e2e-full/scenarios/per-surface.full
.spec.ts` against the live MiniMax stack; `HitMap.assertCoverage()` must go green — i.e. the 98 handles
are actually clicked + their effects observed in the live app (this is the line between "handle exists"
and "handle works"). Capture evidence dossiers (timeline + screenshots) per surface.

**T3.4 — Per-surface flow proofs + evidence (the four-truths).** With MiniMax driving, prove each surface
DELIVERS, not just renders — capture screenshot + dossier + SendUserFile for:
- Search: a grounded answer with citations.
- Deep Research: a report → export (md/pdf/docx) → audio overview → build-a-deck handoff.
- Build/Agent: "build a landing page" → live app deliverable reachable; a gate (confirm/clarify) fires
  and is answered; steer mid-run; stop/resume.
- Settings: a provider key saved → round-trips → used by a real call.
- Schedule: create → fire-now (T1.1) → a run appears in Activity.

**T3.5 — Triage.** A live model surfaces real bugs (the point). Each failure → BIAS-FREE root-cause →
fix (own commit) or log. Loop T3.2–T3.4 until the matrix is green. Acceptance: every surface has a
saved live-MiniMax evidence dossier proving a real delivered output + the handles exercised.

---

## TIER 4 — real Settings test-probes (NEW features; flip honest-disabled → functional) → per-probe commits

Each currently honest-disabled "test" affordance becomes a real, lightweight provider probe (backend
endpoint + wire the existing button + flip `data-disco-flag` → live status + test + live-verify):

- **T4.1 ProviderKeys "Test key"** — `POST /api/secrets/{provider}/test`: a cheap authenticated ping
  (e.g. models-list) → ok/unauthorized/unreachable. Wire ProviderKeysSection.
- **T4.2 DataSources "Test connection"** — probe the configured search/extraction endpoint.
- **T4.3 Audio "Test TTS"** — synth a 1-word clip via the configured TTS tier; play/ंconfirm non-empty.
- **T4.4 ImageGen "Test image"** — 1 tiny generation via the configured tier; confirm non-procedural
  when a real backend is set (ties to the #76 fallback warning).
- **T4.5 MCP "Test connection"** — handshake the configured MCP server; surface tool count.

Each probe is real (no fake green); failures shown honestly. Acceptance: button drives a real probe,
status reflects truth, live-verified with a real provider where Dylan has one.

---

## Execution order, parallelism, gates
1. **Tier 1** (serial, fast) → commit.
2. **Tier 2** (investigate then fix) → commit → suite fully green.
3. **Tier 3** (sequential: P0 key → T3.0 driver → T3.1 stack → T3.2 → T3.3 → T3.4 → T3.5 loop). The
   proof tier; needs the live stack + MiniMax key. Evidence-only (+ bug-fix commits as found).
4. **Tier 4** (parallelizable across the 5 probes — fan out subagents if desired) → per-probe commits.

Verification discipline (all tiers): real-only for proof (no cassettes), evidence dossiers + screenshots
for every UI claim, Playwright ONLY on VM-201, two-gate (real screenshot + adversarial review) on the
risky cores. "Regression-clean" vs "proven-works" kept distinct in every report.

---
## TIER 5 — Disco Operator (Claude drives Disco non-deterministically) [Dylan request 2026-06-22]
Built `packages/agent-server/.../verify/operator.py` — the control plane that lets CLAUDE sit in the
human-in-the-loop seat over the real HTTP/WS boundary:
- `operator state <cid>` → status + pending-gate context (proposed plan summary+steps / question / alternatives).
- `operator wait [--any] [--timeout]` → blocks until a watched conversation reaches a gate or changes
  state, prints the event (the NOTIFIER — run as a backgrounded cmd that pings me on completion, or in a loop).
- `operator respond <cid> <action> [text]` → approve | revise <text> | reject | confirm | answer <text> |
  steer <text> | pick <id> | resume | stop | pause | inject — thin wrappers over the verified UI WS frames;
  keeps the socket open until the status moves off the gate (the close-race fix from a99e1a2).
LIVE-PROVEN: as the operator I read a stuck conversation (AWAITING_PLAN_APPROVAL) and `respond approve`
→ status_before AWAITING_PLAN_APPROVAL → status_after RUNNING. This is the foundation of the MCP Dylan
asked about (these three verbs become the MCP tools: state/wait/respond).

### TIER 5 OPERATOR — FULL LOOP LIVE-PROVEN (2026-06-22)
The complete notify→read→respond loop, end-to-end, as the operator (me):
1. NOTIFY: `operator wait --any` detected conv_8ed9 at AWAITING_PLAN_APPROVAL.
2. READ: `operator state` surfaced MiniMax's actual proposed plan — "Create and serve a modern
   one-page landing page…" + 4 steps (SPEC.md → index.html → verify → serve file server).
3. RESPOND: `operator respond approve` → status AWAITING_PLAN_APPROVAL → RUNNING; the build proceeded.
This is exactly Dylan's 4 asks: ping on plan/question/state-change + enter text into the response
boxes + make them actionable. The disco-verify runner's canned auto_answer is the dumb fallback; this
is a real intelligence in the loop. → Becomes the MCP (state/wait/respond = the tools).

### OPERATOR `view` — "see in code what I see with my eyes" (Dylan 2026-06-22) + FULL DELIVERABLE PROOF
Added `operator view <cid>` — the complete code-level visual of a surface, derived from the event log:
status, plan + per-step progress, the activity feed, workspace files (path→bytes), terminal/server
output, the BROWSER observation (rendered url+title), deliverables, agent messages, and (for a served
app) the live /preview-app/ body (with a directory-listing guard). No more ad-hoc curl/parse — one tool
gives the whole picture. This removes Dylan from manual verification: I see the surface as he does.

FULL DELIVERABLE PROOF (operator-driven, live MiniMax): I approved conv_8ed9's plan as operator → MiniMax
built SPEC.md (3826 B) + index.html (15969 B, title "Velocity - Build Something Amazing"), served it
(preview 200, real HTML, NOT a directory listing), opened it in the browser tool to verify, marked plan
progress 4/4 done, emitted an `app` deliverable, and FINISHED. operator `view` shows ALL of it.
The operator console now = state + view (SEE) + wait (NOTIFY) + respond (ACT) → the Disco Operator MCP.
