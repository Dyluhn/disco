# Pending Items — Codebase Mapping & Status

> Code-verified 2026-06-08 against the working tree (not the Jun-7 plan docs).
> Status legend: ✅ Complete · 🟡 Partial · 🔴 Not done · 🟠 Mitigated (root cause open)
> Sources: `manus-gap-analysis.md`, `manus-gap-analysis-addendum.md`,
> `manus-ui-gap-analysis.md`, `universal-readiness-plan.md`, `future-plans.md`,
> and the harness plan `~/.claude/plans/fancy-sauteeing-ocean.md`.

## Roll-up

Not-done is concentrated in **session lifecycle** (suspend/resume/reconcile, Build
resume, checkpointed DR resume, persistence) and the **Ask-gate** — a coherent next
milestone. Backend coherence (GAP A–H) is largely landed.

**Closed this session (2026-06-08, second pass):** all harness/feature work COMMITTED
(7 themed commits + fixes); test suite complete (Phase 2 `--replay`, Phase 3
ReplaySandbox, Phase 7 E2E/visual); the **rerank 16 GB→2.3 GB memory fix** (the
machine-freeze root cause, `4669a76`); **UI now surfaces failures** instead of silent
spinners (`74a8661`); the **build-continuation bug** — second iteration ran in an empty
sandbox because the rehydrate flag wasn't cleared on teardown (`06f0dbe`, VERIFIED live
through the real UI: 3 iterations, workspace accumulated); and the **research-grounding
empty-prose** bug — reasoning model burned its budget thinking + the canary checked the
wrong frame shape (`2188d0f`, canary now PASS).

**Lifecycle cluster (2026-06-09):** Cluster 1 (session lifecycle) is now **all-✅**.
**Auto-suspend on tab-close** landed (`a98d224`) — idle build sandboxes free after a 60s
disconnect grace, RUNNING runs left alone, no-op without durable storage. **Checkpointed
Deep Research resume** landed (`e6b31a8`) — the engine interleaves gather→synthesize so
completed sections survive a Stop, and resume carries them forward instead of redoing
finished sub-questions. NEXT milestone: the **Ask-gate** (Cluster 4, two-way) or remaining
backend-coherence 🔴s (GAP A S3 microcompact, persistent CodeAct kernel, GAP H cache markers).

---

## Cluster 1 — Session lifecycle (GPU-leak / stale-RUNNING arc)

- [x] ✅ Teardown sandbox on FINISHED — `runtime.py:711-722` `_teardown_sandbox`
- [x] ✅ Continue a build after FINISHED restores the workspace — `06f0dbe`: `_teardown_sandbox`
  now clears the `_rehydrated` flag so the next iteration rehydrates the snapshot (was: empty
  sandbox, lost prior work). VERIFIED live (3 iterations accumulated). Regression test added.
  (NOTE: only active when `projects_root` is configured — the snapshot/teardown/rehydrate path.)
- [x] ✅ Auto-suspend on WS-disconnect / idle-TTL — `a98d224`: `runtime.on_connect`/`on_disconnect`
  track live UI sockets; when the last closes, a 60s-grace timer (longer than the WS reconnect
  backoff, so a blip cancels it) runs `_suspend` → snapshot + `_teardown_sandbox`. Guards: skips
  if no live executor, if `projects_root` unset (no durable snapshot → keep), or if status is
  RUNNING (let in-flight work finish). Wired in `app.py` (accept → on_connect; finally → on_disconnect).
  Regression tests: idle-frees-vs-running-kept, no-op-without-storage, grace+reconnect-cancel.
- [x] ✅ Auto-resume on reconnect — `b3694a0`: `subscribeLive` reconnects with exponential
  backoff (was one-shot → a blip sent a fatal error). Server replays history-then-live + reducers
  dedup by id. Mock-WebSocket test. (Workspace rehydrate on kick already existed, `runtime.py:1004`.)
- [x] ✅ Reconcile orphaned RUNNING on startup — `8bcdef9`: `runtime.reconcile_orphaned_runs()`
  marks stale-RUNNING conversations PAUSED + an interrupted note, wired to a FastAPI startup
  lifespan in `create_app`. Fixes the stale-'RUNNING'-forever-after-crash. Regression test added.
  (Container-leak cleanup for podman/gvisor backends is a separate follow-up; process backend doesn't leak.)
- [x] ✅ Build explicit Resume button — `e0bd746`: `useBuildStream.resume()` (sends the existing
  surface-agnostic `resume` frame) + a Resume button on `AgentStatusBar` when PAUSED (replaces Stop).
  Pairs with orphan-reconciliation (reconciled→PAUSED runs get one-click Resume). Regression test.
- [x] ✅ Checkpointed Deep Research resume — `e6b31a8`: the engine now interleaves
  gather→synthesize PER sub-question (each completed section is a durable checkpoint that
  survives Stop, vs. the old gather-all-then-synth-all that left zero sections on Stop).
  `run()` takes `resume_sections`/`resume_passages`/`resume_all_hits`; matching plan steps
  are skipped, only un-done sub-questions run, coherence re-runs over the full set. The
  runtime PAUSED branch rebuilds the partial ReportEvent into retrieval types + passes it as
  `resume_from`. Tests: engine (done subq not re-searched) + runtime wiring + stronger Stop test.
- [x] ✅ Build session persistence — NON-ISSUE (by design): builds persist as server resources
  reached via History + the `/build/:cid` route (read-only-on-open, `useBuild.ts:30-39`). A
  localStorage stash was the "trap" Deep Research deliberately REMOVED — don't re-introduce it.

## Cluster 2 — Backend agent-coherence (GAP A–H + addendum)

- [x] ✅ GAP A S1 — model-aware threshold — `view.py:266-285` (`soft=0.65×ctx`, `hard=0.80×ctx`, `min(…,BUDGET)`)
- [x] ✅ GAP A S2 — Snip at ingestion — `events.py:243-262` `snip_content` (head 5k + tail 2k, reversible)
- [x] ✅ GAP A S3 — Microcompact — `5d4e15b`: `view.microcompact()` tombstones no-op turns (a
  failed call an identical later call superseded), no-model + reversible, run before the summarizer.
- [x] ✅ Structured summarizer — `summarizer.py:24-34` (FILES/DECISIONS/PROGRESS/FAILED/OPEN).
  Caveat: `keep_recent` counts raw events not tool turns (`view.py:272`)
- [x] ✅ GAP D — plan pinning — `view.py:34-53` `_latest_plan` + `_pinned_seqs`
- [x] ✅ GAP C — `file_read` offset/limit — `builtin/files.py:21-58`
- [x] ✅ GAP G — Knowledge/Datasource events — `events.py` (both classes)
- [x] ✅ GAP B — notify/finish turn-taking — `test_cluster2_turntaking.py`, serve/finish tests
- [x] ✅ Persistent CodeAct kernel — `edf0185`: Python `code_exec` shares state across cells via a
  namespace-serialization runner (dill→functions/imports, pickle fallback→data). Verified live vs the
  process sandbox. Node stays one-shot (documented). Overclaim corrected in the gap docs.
- [x] ✅ GAP H — cache markers — `eb302cc`: `prompt_cache_key` (stable prefix hash) on every request +
  Anthropic `cache_control` breakpoints for claude models (gated; local stays plain-string).
- [ ] 🟡 GAP H — mode-boundary de-mutation — DEFERRED (low value): the plan→exec tool/prompt change
  is a ONE-TIME-per-conversation cache break (planning happens once), not per-turn. Noted in `c97c1b3`.
- [x] ✅ Escape-instead-of-halt on stuck — `c97c1b3`: first stuck → a `stuck_escape` marker + a
  single high-temp (0.9) retry to break the self-imitation chain BEFORE STUCK; halts only if still
  stuck after acting. NO reframe reminder (respects the no-automatic-nudge invariant).
- [ ] 🟡 3-strike circuit breaker — AS-IS (intentional): the existing breaker (diagnose→`ask_user`→
  hand off) is model-driven by design; harness-synthesizing the AlternativesEvent would violate the
  same no-nudge rule that kept the stuck-escape reminder-free (`c97c1b3` rationale).
- [x] ✅ `propose_alternatives` phantom — RESOLVED; `events.py:195` docstring corrected
- [ ] 🟠 E3 `</parameter>` leak — mitigated (`openai_provider.py:236,335` `{"_raw":…}` fallback);
  root cause not isolated

## Cluster 3 — Sandbox security

- [ ] 🟡 Egress allowlist enforced — REAL on gVisor (`egress_proxy.py` 403s denied; wired
  `gvisor.py:246-290` with `HTTP_PROXY` + `internal=True`; `test_egress_proxy.py`).
  Podman/local: sealed deny-all instead of proxied (`podman.py:245-250`, `local.py:58-62`)
- [x] ✅ Hard-deny verdict tier — `analyzers.py:92-122` `_SHELL_DENY` + `hard_deny_reason`;
  refused at `engine.py:1837-1853` (mkfs/dd/fork-bomb/`rm -rf /`/`pkill http.server`)
- [x] ✅ Preview tooling — `builtin/preview.py:41-196` `preview_status` + `restart_preview` + `run_server`;
  raw `pkill http.server` hard-denied → steered to `restart_preview`
- [x] ✅ `verify_app` / visual self-verification — `391390e`: `verify="app[:url]"` GETs the RUNNING
  deliverable (HTTP 200 + non-trivial body) on finish, beyond static file checks. Portable
  (python3/urllib). Tested live vs a real http.server (passes serving, fails not). (Pixel screenshot
  still needs a browser in the sandbox image — absent on the process backend.)

## Cluster 4 — Build-surface UX

- [x] ✅ Client-side `srcdoc` live preview — `buildTrace.ts:216-247` `deriveSrcDoc` →
  `ExecutionCanvas.tsx:235-261` iframe, updates per `file_write`
- [x] 🟡 DeliverablePanel handoff — `54380a7`: auto-switch-to-Preview on finish (renderable artifact →
  the result is shown, not the file tree). VERIFIED live (real Qwen index.html → Preview auto-activated).
  Still open: `deployment_url`/manifest export need a backend field — deferred (no unwired UI per the
  no-false-affordances rule).
- [x] ✅ Ask-gate (two-way) — `b1e0772`: dedicated `AWAITING_USER_QUESTION` status (core enum +
  `state.py` `pending_question_id`); the engine's free-form `ask_user` branch emits it with the
  question message id as detail; the loop re-kicks on the user's reply (same as the decision gate).
  Frontend: `useBuildStream.pendingQuestion`/`awaitingQuestion`/`answer()` + a new `AskPanel`
  (Markdown question + focused answer box, Enter sends) wired into `BuildSurface`; `deriveLiveSignal`
  narrates it; feed auto-scrolls. Tests: core loop-step, AskPanel, useBuildStream wiring, live-signal.
  Offline demo (`6e0e8f1`) + real-browser e2e w/ screenshots (`31f81d9`). **Planner extension
  (`101e1b6`):** `ask_user` is now available in PLANNING mode too (was execution-only) + the planning
  prompt's Phase 1.5 "ASK IF BLOCKED" — found by a LIVE run where the real Qwen3.6-27B wanted to ask
  for a user-required detail (brand color) but was forced to guess a placeholder. VERIFIED live
  end-to-end: model asks → AskPanel → answer → plan uses the real value (screenshots captured).
- [x] ✅ Graceful Stop / Kill confirm — `1ee4220`: Stop shows a "Stopping…" pending state until the
  loop leaves RUNNING (cancel is cooperative); Kill (destructive) gated behind an inline confirm
  ("Kill this run? Confirm/Cancel", Stop hidden during it). Added the missing AWAITING_USER_QUESTION
  status label. Tests + real-browser e2e screenshot. (PAUSED phantom: Resume button already landed `e0bd746`.)
- [x] ✅ Aggregate progress + sticky plan — `f9d4dc5`: the read-only plan tracker is now `sticky
  top-0` (opaque bg) so the plan + done/total progress bar stay pinned while the feed scrolls.
  Real-browser e2e asserts <40px drift on a 4000px scroll + screenshot. (done/total + bar already in `PlanPanel`.)
- [x] 🟡 Liveness — auto-scroll on key transitions + active-state spinner landed; plan-drafting
  skeleton added (`54380a7`). Still open (lowest value): continuous auto-scroll, `pmx-rise` animations.
- [x] ✅ Persistence + reconnect + notifications — reconnect/backoff DONE (`b3694a0`); persistence
  is by-design (History + route); completion `Notification` + `document.title` badge DONE (`1ee4220`,
  `useBuildNotifications`): badges the title + best-effort OS-notify when a build finishes or a gate
  opens while the tab is hidden; restores on refocus; no noise when visible. Tests cover all branches.
- [x] 🟡 Plan front-door polish — `54380a7`: "Drafting a plan…" skeleton between submit and the gate
  (real-browser e2e + screenshot). Still open (lowest value): "re-planning" stale state, per-step skip/reorder.

## Doc-integrity items (not features — correct the docs)

- [x] ✅ CodeAct "Manus-faithful" overclaim — RESOLVED by making it true (`edf0185`, stateful CodeAct)
  + the gap docs now describe the real namespace-serialization mechanism.
- [x] ✅ Egress deny-by-default contract — `edf0185`: `tool-sandbox-contract.md` §7 now scopes the
  guarantee by tier (gVisor = real selective allowlist; podman/local = sealed deny-all; process = models only).

## Process debt

- [ ] Synthetic-fake harnesses (streaming, providers, Deep Research lifecycle) used
  `_ScriptedRouter`/hand-made docs — violate the real-captured-samples rule; rebuild from captures
- [x] ✅ All work COMMITTED — was uncommitted; now 7 themed commits + the session's fixes
- [x] ✅ Phase 2 `--replay` e2e — DONE. The OOM blocker was a real bug: the cross-encoder rerank
  ballooned to 16 GB on full-page passages (fixed in commit 4669a76 — truncate+batch → 2.3 GB).
  Captured `research_demo.jsonl`; `make eval` passes (faithfulness 1.0, no baseline regression, 3.5 GB peak)
- [x] ✅ Phase 7 Playwright E2E + visual regression — BUILT (commit 3449cbb): `frontend/e2e/*.spec.ts`
  (4 flows) + Firefox visual baselines (both themes), fixture mode via `vite --mode test`, `make e2e`.
  Meta-verified (break heading → functional + visual go red)
- [x] ✅ Phase 3 build-surface `ReplaySandbox` — BUILT (`harness/sandbox.py`): Recording/Replay sandbox
  on the `sandbox_service=` seam, keyed by `(method, args, occurrence-nonce)` for the stateful backend;
  wired into `build_replay_runtime`; round-trip tested (`test_sandbox_replay.py`). Full e2e BUILD replay
  still needs a captured build cassette (`record_sandbox=True` → needs the model server up)

## future-plans.md follow-ups (system-reminder pattern, not yet built)

- [ ] 2A consecutive-tool-error reminder · 2B iteration-ceiling warning · 2C stuck precursor
- [ ] 3A re-plan entry reminder · 3B browser-injection reinforcement
- [ ] Subagent fan-out (Explore/Plan) · execution-prompt tuning for small open models
