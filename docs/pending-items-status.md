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

**Lifecycle cluster (in progress, 2026-06-09):** **auto-suspend on tab-close** landed
(`a98d224`) — idle build sandboxes free after a 60s disconnect grace, RUNNING runs left
alone, no-op without durable storage. Cluster 1 is now all-✅ except **checkpointed Deep
Research resume** (the one remaining 🔴 — engine is stateless and redoes completed
sub-questions). NEXT.

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
- [ ] 🔴 Checkpointed Deep Research resume — `runtime.py:757-768` re-runs the same plan;
  engine stateless (`deep_research/engine.py:119-235`) → redoes completed sub-questions
- [x] ✅ Build session persistence — NON-ISSUE (by design): builds persist as server resources
  reached via History + the `/build/:cid` route (read-only-on-open, `useBuild.ts:30-39`). A
  localStorage stash was the "trap" Deep Research deliberately REMOVED — don't re-introduce it.

## Cluster 2 — Backend agent-coherence (GAP A–H + addendum)

- [x] ✅ GAP A S1 — model-aware threshold — `view.py:266-285` (`soft=0.65×ctx`, `hard=0.80×ctx`, `min(…,BUDGET)`)
- [x] ✅ GAP A S2 — Snip at ingestion — `events.py:243-262` `snip_content` (head 5k + tail 2k, reversible)
- [ ] 🔴 GAP A S3 — Microcompact (drop no-op turns with tombstone) — not present
- [x] ✅ Structured summarizer — `summarizer.py:24-34` (FILES/DECISIONS/PROGRESS/FAILED/OPEN).
  Caveat: `keep_recent` counts raw events not tool turns (`view.py:272`)
- [x] ✅ GAP D — plan pinning — `view.py:34-53` `_latest_plan` + `_pinned_seqs`
- [x] ✅ GAP C — `file_read` offset/limit — `builtin/files.py:21-58`
- [x] ✅ GAP G — Knowledge/Datasource events — `events.py` (both classes)
- [x] ✅ GAP B — notify/finish turn-taking — `test_cluster2_turntaking.py`, serve/finish tests
- [ ] 🔴 Persistent CodeAct kernel — `builtin/system.py:92-99` stateless one-shot
  (writes `_codeact.{ext}` + fresh subprocess). Doc "Manus-faithful CodeAct" is an OVERCLAIM
- [ ] 🔴 GAP H — cache markers — `openai_provider.py` emits no `cache_control`/`prompt_cache_key`
  (but `sort_keys=True` at `:134` ✅ and `cached_tokens` parsed at `:197-206` ✅)
- [ ] 🟡 GAP H — mode-boundary de-mutation — two prompt strings (`prompts.py:99` vs `:134`);
  tools filtered not masked (`engine.py:724-726`) → breaks cache prefix
- [ ] 🔴 Escape-instead-of-halt on stuck — `stuck.py` bool; `engine.py:1308-1310` emits STUCK
  immediately; no temp-bump/reframe escape
- [ ] 🟡 3-strike circuit breaker — threshold is 4 (`engine.py:559`); 1st hit asks the MODEL to
  call `ask_user` (`:1334-1350`); 2nd → `AWAITING_USER_DECISION` (`:1367-1379`); harness does NOT
  synthesize the AlternativesEvent (depends on the model volunteering)
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
- [ ] 🔴 `verify_app` / visual self-verification — only the static MVP (`engine.py:458-472`
  `_static_verify_command` = files-exist + HTML-parse; no curl/chromium/screenshot; `test_verify_on_finish.py:158`)

## Cluster 4 — Build-surface UX

- [x] ✅ Client-side `srcdoc` live preview — `buildTrace.ts:216-247` `deriveSrcDoc` →
  `ExecutionCanvas.tsx:235-261` iframe, updates per `file_write`
- [ ] 🟡 DeliverablePanel handoff — `DeliverablePanel.tsx:16-56` hero exists; missing
  `deployment_url` on `Project` (`project.ts:17-28`), manifest export, auto-switch-to-preview
- [ ] 🔴 Ask-gate (two-way) — no `AWAITING_USER_QUESTION` status (`types/agent.ts:8-17`);
  `ask_user` is muted-grey `agent_message`, `attention:false` (`buildTrace.ts:156-169`); no `AskPanel`
- [ ] 🟡 Graceful Stop / Kill confirm / Pause — `cancel()` wired to Stop (`useBuildStream.ts:208`,
  `AgentStatusBar.tsx:88-97`); missing "Stopping…" pending, Kill confirm; PAUSED phantom
- [ ] 🟡 Aggregate progress + sticky plan — done/total + bar (`PlanPanel.tsx:80-91`) but PlanPanel
  still inside the scroll container (`BuildSurface.tsx:202-206`)
- [ ] 🟡 Liveness — auto-scroll on key transitions + active-state spinner landed; no continuous
  auto-scroll, no `pmx-rise` entrance animations
- [ ] 🟡 Persistence + reconnect + notifications — reconnect/backoff DONE (`b3694a0`); persistence
  is by-design (History + route); STILL MISSING: completion `Notification` / `document.title` badge
- [ ] 🟡 Plan front-door polish — Revise modal keeps text (`PlanPanel.tsx:142-188`); missing
  "drafting…" skeleton, "re-planning" stale state, per-step skip/reorder

## Doc-integrity items (not features — correct the docs)

- [ ] CodeAct "Manus-faithful" overclaim — it's stateless one-shot
- [ ] Egress deny-by-default contract (`tool-sandbox-contract.md:36,87`) — only true on gVisor;
  podman/local seal instead. Either enforce on all tiers or scope the guarantee

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
