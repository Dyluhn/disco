# Disco — Consolidated Remaining-Items Register (2026-06-15)

**Method.** Every planning / status / gap / backlog / TODO surface in the repo was
scanned in parallel by 13 agents (8 Sonnet slices + 4 MiniMax-M3 harness workers +
1 Gemini harness worker), each cross-checking doc claims against the live tree at
HEAD `3c1a1b2`. This document de-duplicates across all of them, marks each item
**OPEN** / **PARTIAL** / **DONE** / **POST-v0.1**, and is reconciled so that
**code-verified status wins over doc claims.**

> **This is now the single remaining-work doc.** The ~22 superseded
> planning/status/gap docs and the 40 per-order BP/DC/RP briefs they came from
> were moved to `docs/archive/` (history preserved, nothing deleted). Living
> reference docs stay in `docs/`: `north-star`, `self-host`, `provider-matrix`,
> `audit-2026-06-15`, `design-novnc-live-browser`, `surfaced-issues-2026-06-15`,
> plus the root contracts + `basis-of-design`.
>
> **Verification status:** items here are de-duplicated from doc scans; only a
> handful were spot-checked against code this session (and 1 doc-sourced "bug"
> was disproved — see §2-C). Treat unverified lines as *leads*, not gospel,
> until a code pass confirms each. A full file:line verification pass is the
> recommended next step.

> ## ⚠ The staleness gradient — read this first
>
> The big older roadmaps (`release-execution-plan.md`, `release-roadmap.md`,
> `next-fix-set-plan.md`, the manus-gap trio) were written days-to-weeks ago and
> list **hundreds of items as open that have since shipped.** The code-verified
> scanners agreed:
> - manus-gap trio (scan-m1): of 11 backend gaps A–H, **9 LIKELY-DONE**; of 13 UI items, **7 done**, rest partial.
> - architecture-rebuild + BP/DC clusters (scan-m3): **105 LIKELY-DONE / 13 ambiguous / only 4 truly OPEN.**
> - the current-state reconciliation (`.omo/evidence/live/remaining-work-reconciliation.md`, dated today) found **~88% of `remaining-work-plan.md` was stale/done.**
>
> **Do not work from the old roadmaps directly** — they will send you to rebuild
> things that exist. The genuinely-open set is below. The "what looks open but
> isn't" reconciliation is in §10.

---

## 1. North Star tracking

`docs/north-star.md` (compiled 2026-06-13) is the single vision/release-gate doc.
It is tracked here per the request.

**The one goal.** A stranger downloads Disco, runs one command on a normal
machine, and uses *every* surface — keyless — without troubleshooting. "Done"
means a human (or the gauntlet) drove the *real assembled app* and watched it
work. Green mocks never earn that word.

**Five standing policies (the release gate):**
1. **The gauntlet is the gate** — `docker compose up` on a never-seen machine + driving every surface = release-ready (`make gauntlet`).
2. **"Verified" requires the real app** — no feature is done on the strength of mocks. (See [[feedback-verification-protocol]], [[feedback-real-sample-harnesses]].)
3. **Show the work (heartbeat)** — surface what the system is doing at every wait point.
4. **Whitelist, don't blacklist, at every render/throughput boundary** — internal event names must never leak to the UI. (See [[feedback-no-false-affordances]].)
5. **Honest framing of demo-grade things** — bundled model, fixture mode, no-isolation backend each must loudly say what they are.

**North-Star Definition-of-Done — live status** (all must be green on a clean
8 GB box, keyless):

| DoD gate | Status | Note |
|---|---|---|
| `docker compose up` → all services healthy; model cached + survives recreate | **PARTIAL** | compose + cache-volume fix + frontend healthcheck shipped; final clean-box gauntlet pass still owed (§2-A) |
| Grounded answer with citations; no OOM, no silent vanish | **PARTIAL** | OOM spike+floor fixed & live-proven; 8 GB keyless proof = lite-encoder swap pending (§2-B) |
| Deep Research → charted report, live heartbeat, correctly-marked steps | **DONE** | heartbeat + whitelist renderer + chart fences + model pill all shipped |
| Build/Agent spins a real sandbox, lists/edits files | **DONE** | BP/DC clusters closed |
| `disco verify` passes | **DONE** | eval-as-a-feature shipped (task #23) |
| Nothing requires reading a log / editing a config to recover | **PARTIAL** | sandbox-settings hot-apply + docker-client-wedge fixed; encoder OOM-guard honest-frame shipped; final recover-without-logs sweep rides the gauntlet pass |

**North-Star open work** rolls into §2 (release/RAM), §3 (rename stragglers),
and §2-B (bundled-model swap).

---

## 2. GENUINELY OPEN — release-blocking / near-term

### A. Release engineering (the remaining road — Phase 3)
- **OPEN** — LICENSE file (Dylan decision: AGPL vs Apache tradeoff). *Sources: north-star §2#7-adjacent, release-exec D4, README, audit §2.*
- **OPEN** — Minimal honest CI: GitHub Actions running `make test` + frontend vitest/build; `tsc`/`eslint` labeled non-required, not suppressed. *north-star §2#5, release-exec D4.*
- **OPEN** — Demo gallery of exported share bundles (build replay, charted report, slides, sheet, audio). *release-exec D5.*
- **OPEN** — `git tag v0.1.0` across the five packages + changelog from order history. *release-exec D2.*
- **OPEN** — Mobile responsive pass (research/history/tasks); Build/Agent inspector stays desktop-first, *labeled.* *release-exec D6, README, roadmap §2.3.*
- **PARTIAL** — SECURITY.md exists, but §8 (vulnerability-disclosure process / contact / SLA) is a placeholder; consolidated threat model + per-tier guarantee table still owed. *g1, completeness-critic, release-exec D3.*
- **DONE (reconciled)** — one-command compose deploy (`self-host.md` describes the live 4-service stack; task #22 committed). Install guide (`self-host.md`) and provider matrix (`provider-matrix.md`) **exist** — the old "Phase 3 not started" framing is stale.

### B. The 8 GB keyless path (north-star #26) + bundled model
- **OPEN** — Final clean-box **gauntlet pass**: boot bundled compose on a fresh 8 GB box, `disco verify` passes, peak RSS < 8 GB, all surfaces + all DR tiers. *finish-everything §3.1/§5.3, task #52.*
- **OPEN** — Replace bundled `Qwen3-4B-Instruct-2507` (11-mo-old, hallucinated turn 2) with a current-gen small GGUF that is tool-call-strong; verify keyless pull + `disco verify` before blessing. Frame honestly as a smoke test, not a capable default. *north-star §6, finish-everything §3.4.*
- **PARTIAL** — lite ONNX encoder tier (≈4 GB→0.8 GB) + ctx 8K + KV-quant: env knobs + config shipped (tasks #41/#42); the **VM-201 8 GB RSS proof** that it actually fits keyless is the open rung. *north-star §5, finish-everything §3.2/§3.3, task #51/#52.*
- **OPEN (tuning)** — OOM semaphore `K=9` is aggressive on the co-tenanted workstation (drives system RAM to ~1 GB); candidate: raise `_RESERVE_GB` 2→~4. *oom-fix-live-results §Finding 1.*

### C. PMX_ → DISCO_ rename stragglers (cosmetic only)
- **DONE (verified 2026-06-15)** — the entrypoints already read `DISCO_*` primary with `PMX_*` fallback: `frontend/docker-entrypoint.d/40-disco-env.sh:11-14` and `deploy/compose/entrypoint.sh:8,16`. The reconciliation doc's §A6 "custom ports silently ignored" claim was **itself stale** — no bug here. (Lesson: doc-sourced "bugs" must be code-checked.)
- **OPEN (cosmetic)** — `OpenRouterSection.tsx` help text + `local_encoders.py` `PMX_RERANK_MODEL` constant + ~25 stale `PMX_` docstrings/comments (deliberately deferred per Dylan's 2026-06-15 call; `.pmx` data dir intentionally kept). *[[disco-rename]].*

### D. False affordances (violates [[feedback-no-false-affordances]] — treat as P0-correctness)
- **OPEN** — `delegate_explore` fan-out is a **production stub**: `_run_fanout` (engine.py:3712-3783 / :5206) returns a canned echo `{"stub": True}`; only tests override it. The tool is advertised to the model but does nothing real. *inline-scanner, reconciliation §C20.*
- **PARTIAL** — `deploy_preview` is declared in the tool-name registry but **never built** (`builtin/__init__.py` marks it "intentionally absent"); delivery actually works via `serve` + preview-proxy. Either build it or stop advertising the name. *scan-m1 GAP E, tool-sandbox-contract §0.*

---

## 3. OPEN — engine / agent-loop residue

Most of the manus/architecture-rebuild backlog **shipped** (see §10). The
genuine residue:

- **OPEN** — DR **orchestrator-worker isolated-context fan-out**: only the gather-leg concurrency landed (`_gather_leg`, RAM-bounded); sub-questions still run in the *same* loop context, not isolated contexts returning distilled findings. *scan-m3 §4, scan-m4 §4, fable-recon §4.*
- **OPEN** — **Single stable system prompt** / stop mutating the tools array per mode: `_tools_for_step` still varies the visible tool set (planning vs execution vs meta-withhold), breaking the llama.cpp KV prefix. *scan-m1 GAP H, fable-recon 3.6-2 (flagged P1).*
- **OPEN** — **Auto-spill large observations to disk** + `.disco/MEMORY.md` durability: snip-at-ingest is done, but the per-observation spill-file + "re-read with file_read" pattern is not enforced. *scan-m1 GAP C, fable-recon P1#4.*
- **OPEN** — Edit-tool hardening residue (Plan D): demote `file_replace_lines`/`file_insert_lines` from `advertised_tools` (still callable) **and** fix the `FileRead`/`FileEdit` descriptions that still steer the model toward the fragile line tools. Deletion-guard (empty `new_text`) already shipped. *scan-m2 §3/§3.5.*
- **OPEN** — `deploy_preview` / detached long-running `serve` process tool: 300 s per-tool timeout is still the ceiling; no first-class detached-server primitive. *scan-m1 GAP E(2), fable-recon P1#3.*
- **OPEN** — B7 "**one feature per iteration**" scope enforcement (the per-predicate DoD spec is the closest analogue; the scoping rule itself is unenforced). *scan-m3 §3.B7.*
- **OPEN (design-blocked)** — HS-06 epochal observation masking (per-step masking vs KV-cache stability conflict needs a **Dylan strategy call**). *harvest-backlog, scan-m2.*
- **OPEN (infra-blocked)** — B9 grammar-constrained tool calls via llama.cpp `--jinja` template relaunch (prefill is the only masking seam in use; grammar/`tool_choice:required` proven inert on the current server). *scan-m3 §0/§3.B9.*
- **OPEN (small)** — soft@3 "you've called X 3× — try a different tool" inject in the stuck detector; XML file delimiter + strip-on-ingest; skills `active_paths` wiring at the `runtime.py` call site; serialization jitter / nudge-pool (escape-pool half done). *scan-m1 2.7/2.11, scan-m2 §2/§6.*
- **AMBIGUOUS (likely DONE)** — recitation-on-cadence: fable-recon says it fires every step; scan-m1/m3 found `_recitation_cadence` + `_recitation_signature` shipped. Confirm cadence default, then close.

---

## 4. OPEN — Deep Research / report & artifact surface
- **OPEN** — #16/#21 **RP-09 live audio acceptance**: in-process Kokoro TTS + Settings toggle + frontend audio-overview wiring all shipped (C2 this session); the live mixer-robustness acceptance against a real run is the open rung. *tasks #16/#21, reconciliation §4.3.*
- **OPEN** — In-block artifact download: thread `cid` through `BlockView` so chart/sheet/slides blocks offer real downloads (currently `.xlsx`-card-only). *release-exec B2, RP-11 deferral.*
- **OPEN** — Mid-run DR steer / inject sources (stop/resume at section boundary exists; steer+inject don't). *roadmap §2.1#4.*
- **DEFERRED (external)** — Univer read-only `.xlsx` grid embed: `@univerjs` 0.25.x ships no standalone read-only component. Card stays preview-only. *reconciliation §D3.*
- **DEFERRED (external)** — Real generative-image backend (SDXL/Diffusers needs torch + weights); procedural PIL backend is the keyless default. *reconciliation §D9, scan-m1 2.6.*

## 5. OPEN — Build / Agent UX residue
- **OPEN** — Main-feed auto-scroll (only TerminalPane autoscrolls; the doc's "single highest-leverage polish fix"). *scan-m1 UI 3.1/§6.*
- **OPEN** — Pin `LiveSignalBar` to the footer (currently inside the scroll container). *scan-m1 UI 3.6.*
- **OPEN** — Port Deep Research's `localStorage` session stash to `useBuild` (in-memory only today). *scan-m1 UI 3.9.*
- **OPEN** — Adjustable plan (per-step skip/reorder batched into `request_plan`) + visual design-edit mode. *scan-m1 UI 3.8/§6#13.*
- **PLANNED (#53–57)** — E6 v2 **noVNC live browser**: P1 image (Xvfb/x11vnc/noVNC) → P2 lazy desktop service → P3 agent-server route → P4 frontend Live toggle + Settings gate → P5 live acceptance. Defaults locked (lazy-headed, view-only, no-WM, local/podman-first). gVisor path additionally needs the egress allowlist updated for the noVNC port (rides D7). *design-novnc-live-browser.md.*

## 6. OPEN — test-harness & process debt
- **OPEN** — E7: rebuild the DR-lifecycle harness from **real captures** (14 `_ScriptedRouter` synthetic sites in `test_deep_research.py`; cassette infra exists but no DR test loads from it). Violates [[feedback-real-sample-harnesses]]. *reconciliation §E7, finish-everything §3.5.*
- **OPEN** — Real-sample backfill for the streaming / provider harnesses (still synthetic). *release-exec standing-debt.*
- **OPEN** — E3 `</parameter>` leak: mitigated in `openai_provider` + rp-04 strip, **root cause never isolated** (reproduce under the rp-12 requery harness). *release-exec A2, perpleximanus-open-gaps.*
- **OPEN** — First-hit wake race after idle-suspend (host_proxy connects before rehydrated preview binds :8000; candidate fix = connect-retry). *perpleximanus-open-gaps, release-exec A2.*
- **OPEN** — `ACKNOWLEDGEMENTS.md` SmallCode MIT notice: each ported file needs the inline MIT notice at port time; design harvested, ports not yet attributed. *oss-harvest-survey, completeness-critic.*
- **OPEN** — Lint debt: drive shipping-file `tsc` to 0 (28 of 39 errors are in shipping files, not tests — the Dockerfile "test-only" comment is false); fix real ruff `F821`/`B904`; eslint 67→0. (Partly addressed task #47.) *north-star §2.*
- **OPEN** — `df-08 "done # committed"` hand-annotation is gate-invisible to `orchestrate.sh` — latent shared-file trap for any future order touching df-08's files. *[[orchestrate-gate-committed-token]].*

## 7. OPEN — code organization (audit 2026-06-15, all P0–P2)
- **OPEN (P0)** — Split monoliths: `core/loop/engine.py` (6,446 LOC, 1,878-line `run()`), `agent-server/runtime.py` (3,836), `agent-server/app.py` (1,678), `app-server/config_state.py`, `frontend ExecutionCanvas.tsx`, `core/view.py`, `frontend blocks.tsx`. *audit §1.1.*
- **OPEN (P0)** — Layering defects: circular import `tools/builtin/audio_overview.py → agent_server` (hidden `agent-server→tools→agent-server` cycle); undeclared transitive deps (`tools/mcp/retrieval_tier.py→disco.retrieval`, `app-server config_state→disco.tools`); private-internal reach-ins. *audit §1.2.*
- **OPEN** — Root hygiene: move/delete `run_manual*.py`×5, root `test_*.py`×5, `*.diff`×3, stray CSVs, live `disco.db`+sidecars out of repo root to `./data/` or XDG; make `harness/` + `integrations/messaging/` real uv workspace members (currently silently excluded from pytest). *audit §1.3, perpleximanus-bp-campaign-status.*
- **OPEN** — Adopt `import-linter` layers contract + file-size CI gate (ESLint `max-lines`, ruff line-count). *audit §1.4.*

## 8. OPEN — security / contract decisions (mostly tuning, not blockers)
- **OPEN** — Per-domain human approval for non-allowlisted egress hosts (proxy returns 403; loop doesn't yet intercept it as a confirmation event). *scan-m1 2.4(3).*
- **OPEN (live-blocked)** — D7 egress allowlist live VM test + podman/local egress re-verify (blocked on destroyed VM 202; gVisor path tagged HARDWARE-UNVERIFIED). *finish-everything §3.5, fable-recon 2.3B, gvisor.py:176.*
- **OPEN (decisions)** — contract-appendix open calls: confirmation threshold HIGH-vs-MEDIUM for Agent; browser driver (browser-use vs Stagehand vs Playwright); vector-store engine (pgvector vs local); warm-pool sizing; sync-vs-async security analyzer; score-cache for identical actions; revive dormant intelligent overflow routing. *g1 (agent-loop / security-analyzer / llm-router / retrieval-grounding / tool-sandbox contracts).*
- **OPEN** — #10 ApprovalDiff: real new-hash from an agent-server probe (re-approval diff shows old-vs-new tool lists). *reconciliation §4.2, RP-05b-reapproval decision (ratification-pending).*

## 9. POST-v0.1 / future backlog (explicitly deferred — not release work)
- **future-plans.md** captured ideas: Tier-2A/2B/2C + 3A/3B system-reminder patterns (consecutive-error, iteration-ceiling, stuck-precursor, re-plan-entry, browser-injection); subagent infra (Explore/Plan); execution-prompt A/B tuning for small models (gate-first). *future-plans.md.*
- **HS ledger residue**: HS-01 (gated-done, ungated is residual), HS-04 (n-gram similarity upgrade; A-B-A-B + action-error already in tree), HS-05 (rule-based DoD vs score-based critic). HS-07/HS-08 done. *harvest-backlog, scan-m2.*
- **Weak-model assist tier**: build the gate first (toggle, auto-probed from live model label, default-OFF for capable models) then F3/F5/F6/F7 behind it. F-flags (F4/F6/F8/F9) already gated-and-shipped. *[[feedback-gate-weak-model-assists]], oss-harvest-survey.*
- **Style retrieval** (StyleDistance encoder on LXC 100 + 2-stage recall→precision pipeline). *[[reranker-for-style-verdict]].*
- **Release-exec cut list**: N-item horizontal research, replay fork/take-over, internet-public share links, credibility-weighted source scoring, messaging triggers (Telegram/mail), GitHub two-way sync, chat-vs-agent cheap routing, cross-session memory (gated on 27B A/B evidence), native mobile/desktop apps. *release-exec §3, roadmap §2.2.*
- **OSS harvest borrows** not yet pulled: OpenCode hidden-invalid-tool reroute (HS-08 done; rest open), anchored-summary UPDATE compaction, interrupted-tool rendering, max-steps forced text-only wrap-up, finish-reason distrust, Qwen sampling pin; Aider failure-feedback kit; SWE-agent epochal masking (=HS-06). *oss-harvest-survey, next-fix-set §8.*

---

## 10. What LOOKS open in the old docs but is DONE (reconciliation)

So no one re-builds these. **All cross-checked against code/commits:**

- **Entire BP cluster (BP-00…16)** — shipped (shell sessions, preview-as-session, env contract, Playwright daemon, finish-verify gate, observation masking, read-counter rollback, IPython kernel, dep installs, vision driver, multi-port, file upload, resume, idle-suspend, terminal tab, screenshots, marathon gate). *scan-m3 §4.*
- **Entire DC wave (DC-01…07)** — shipped + Wave 0 CLOSED (`7a1c302`); restart survival, idle-suspend/resume, blast-radius gates, session diagnostics, loop breakers, upload re-materialization. *scan-m3 §3.*
- **RP pack (RP-01…14)** — shipped: status write-through, rich blocks, Chart.js charts, wide-research pipeline, full MCP client stack (a/b/c), replay+share, PDF/DOCX export, scheduled tasks, **Kokoro audio**, Marp slides, openpyxl sheets, FC kit, clarify+followups, usability debt (theme/Cmd+K/history-search/cost-meter/isolation-tier). *scan-m4, STATUS-2026-06-11.*
- **manus-gap A–H**: model-aware condensation, plan/knowledge/datasource pinning + recitation tail, `finish` tool + prose≠done (Build/Research agent split), notify/ask partition, `file_append`/offset/limit, egress proxy live, hard-deny tier, browser-verify gate, image-gen (procedural), prompt-cache markers. *scan-m1.*
- **OOM** (spike: RAM-aware semaphore; floor: onnxruntime arena disable) — fixed **and live-proven** (exhaustive DR finishes bounded; floor recedes to ~2.9 GB). *[[disco-oom-rootcause-fix]], oom-fix-live-results.*
- **Surfaced-issues C1/D1/D2/E1–E7/F0** — closed this session (export origin, Need-More card, resumed title, silent snapshot, preview live, planning tool-awareness, artifact allowlist, honest browser copy, Build/Agent isolation guard). License: default reranker swapped CC-BY-NC → **MIT bge-reranker-base**. *17 session commits.*
- **Compose deploy / install guide / provider matrix / `disco verify`** — all exist (`self-host.md`, `provider-matrix.md`, eval-as-a-feature). The "Phase 3 not started" framing in `release-execution-plan.md`/`release-roadmap.md` is **stale.**

---

## 11. Source coverage map

| Scanner | Slice | Verdict |
|---|---|---|
| S1 | `.omo/evidence/live/BACKLOG.md` | A–I extraction |
| S2 | remaining-work-plan + pending-items-status | Track A–H |
| S3 (Sonnet) | next-fix-set / release-exec / release-roadmap / universal-readiness | huge OPEN/DONE table |
| S4 (Sonnet) | **north-star** + basis-of-design | NS summary + goals |
| S5 (Sonnet) | inline TODO/FIXME/stub in packages+frontend+orders.yaml | stub inventory |
| S6 (Sonnet) | memory files | open-item extraction |
| S7 (Sonnet) | audit / surfaced-issues / design-novnc / archive / workorders | extraction |
| S8 (Sonnet, critic) | full docs/ tree + root *.md + `.omo/` | inventory + gap hunt + commit cross-check |
| scan-m1 (M3) | manus-gap-analysis (+addendum) + manus-ui-gap | 9/11 backend done |
| scan-m2 (M3) | harvest-backlog + build-loop-oss-research + hardening-plan | 71 done / 12 open |
| scan-m3 (M3) | arch-rebuild-plan + writeup + decomplexity-wave + build-parity | 105 done / 4 open |
| scan-m4 (M3) | fable-recon + project-history×2 + provider-matrix + self-host | RP/fable residue |
| scan-g1 (Gemini Pro) | root contracts (future-plans, HANDOFF, USAGE, ui-impl, api-endpoints, 6×*-contract) | appendix decisions |

**Authoritative current-state doc:** `.omo/evidence/live/remaining-work-reconciliation.md`
(2026-06-15) supersedes the older roadmaps. **Active completion plan:**
`.omo/plans/finish-everything-plan.md` (Phase 0–5). When in doubt, those two +
this register over anything in `docs/archive/` (where the old `*-plan.md` now live).

---

## ADDENDUM — Dylan's live walkthrough, 2026-06-17 (WALK-01..21)

A full live-driving session on the booted dev stack (all surfaces) surfaced 24
issues, each traced to code with file:line evidence by 7 parallel read-only
investigators + live event-log/config corroboration. Verbatim notes + full
root-cause breakdown live in **`docs/dylans-walkthru-6-17-26.md`** (the source of
truth for these). Summary of the actionable items folded here:

**P0 (breaks/confuses a core flow):**
- WALK-03 — `[[id]]` citation id leaks into the "Sources disagree" callout (`DeepReportView.tsx:174`).
- WALK-08 — stale "Follow-up: <query>" shows before the report exists (`useDeepResearch.ts:178-186`, `-1` seq fallback).
- WALK-09 — build Deliverable/manifest card surfaced at serve-time, not FINISHED (`BuildSurface.tsx:367-379`).
- WALK-10 — build "Open" button uses the deprecated non-waking `preview-app` route → 503 after suspend (`routes/preview.py:33-43`).
- WALK-18 — **(ENGINE)** pause/steer/resume not really wired (`engine.pause()` dead code; idempotent `kick` drops resume).
- WALK-19 — **(ENGINE)** build loop has no failure-independent no-progress breaker → grinds toward `max_iterations=500` on capable models (trace: 160 obs / 7 errors in one run).

**P1 (quality / UX):** WALK-01 stream markdown live (basic search); WALK-02 follow-up markdown render; WALK-04 DR "Planning…" loader; WALK-05 rename "DuckDuckGo"→"ddgs"; WALK-06 tighten disputed-notes extraction (platform-name + vacuous hedges); WALK-07 null persisted base_url on bundled flip; WALK-11 decouple follow-up indicator from plan loader; WALK-12 follow-up progress events + "what the model is doing" section; WALK-13 first-run TTS download progress; WALK-14 custom AudioPlayer on the design system; WALK-15 steer agent to `slides_generate`/`sheet_generate` at execution time; WALK-16 enumerate observation/deliverable artifacts in the Artifacts tab; WALK-17 schedule UX (presets + cron validation + rename).

**P2 (feature):** WALK-20 include-follow-up in exports/audio + modular "how many?" popup; WALK-21 podcast-vs-single-speaker popup + single-voice honest-walkthrough mode.

**Closed by investigation (no work):** surgical line-edit tools are present & wired
(not removed); the deepseek-v4 driver and encoders were NOT the mini-PC spin cause —
Settings was truthful, the cause was persisted crawl4ai/searxng (already fixed by
switching to bundled; null the stale base_urls per WALK-07).
