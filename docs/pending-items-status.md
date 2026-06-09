# Pending Items — Codebase Mapping & Status

> Code-verified 2026-06-08 against the working tree (not the Jun-7 plan docs).
> Status legend: ✅ Complete · 🟡 Partial · 🔴 Not done · 🟠 Mitigated (root cause open)
> Sources: `manus-gap-analysis.md`, `manus-gap-analysis-addendum.md`,
> `manus-ui-gap-analysis.md`, `universal-readiness-plan.md`, `future-plans.md`,
> and the harness plan `~/.claude/plans/fancy-sauteeing-ocean.md`.

## Roll-up

~25 tracked items: **~10 complete · ~9 partial · ~6 not-done.** Not-done is
concentrated in **session lifecycle** (suspend/resume/reconcile, Build resume,
checkpointed DR resume, persistence) and the **Ask-gate** — a coherent next
milestone. Backend coherence (GAP A–H) is largely landed.

---

## Cluster 1 — Session lifecycle (GPU-leak / stale-RUNNING arc)

- [x] ✅ Teardown sandbox on FINISHED — `runtime.py:711-722` `_teardown_sandbox`
- [ ] 🟡 Auto-suspend on WS-disconnect / idle-TTL — `app.py:378-403` catches
  `WebSocketDisconnect` but finally-block only cancels pump tasks; no teardown, no idle timer
- [ ] 🟡 Auto-resume on reconnect — rehydrate exists (`runtime.py:1004-1035`
  `_maybe_rehydrate`, on first kick); WS reconnect/backoff missing (`agent.ts:94-123` one-shot)
- [ ] 🔴 Reconcile orphaned RUNNING on startup — no lifespan/startup hook in agent-server; no RUNNING sweep
- [ ] 🔴 Build explicit Resume button — Deep Research has it (`useDeepResearchStream.ts:164`,
  `runtime.py:1218`); Build has no `resume` verb (`useBuildStream.ts:252-267`).
  Read-only-on-open IS done (`useBuild.ts:30-39`, "View ≠ start")
- [ ] 🔴 Checkpointed Deep Research resume — `runtime.py:757-768` re-runs the same plan;
  engine stateless (`deep_research/engine.py:119-235`) → redoes completed sub-questions
- [ ] 🔴 Build session persistence (localStorage cid) — `useBuild.ts:15-26` React-state only

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
- [ ] 🔴 Persistence + reconnect + notifications — no localStorage cid, no WS reconnect/backoff,
  no completion `Notification`/title badge
- [ ] 🟡 Plan front-door polish — Revise modal keeps text (`PlanPanel.tsx:142-188`); missing
  "drafting…" skeleton, "re-planning" stale state, per-step skip/reorder

## Doc-integrity items (not features — correct the docs)

- [ ] CodeAct "Manus-faithful" overclaim — it's stateless one-shot
- [ ] Egress deny-by-default contract (`tool-sandbox-contract.md:36,87`) — only true on gVisor;
  podman/local seal instead. Either enforce on all tiers or scope the guarantee

## Process debt

- [ ] Synthetic-fake harnesses (streaming, providers, Deep Research lifecycle) used
  `_ScriptedRouter`/hand-made docs — violate the real-captured-samples rule; rebuild from captures
- [ ] All harness work + UI/server mods are UNCOMMITTED — commit before further change
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
