# Backlog — AUDITED + UNAUDITED (compiled 2026-06-16)

> Two sections: the **AUDITED** block (the 57 canonical items, code-verified against the
> tree by the verify-* harness lanes + my spot-checks) and the **UNAUDITED** block (the raw
> archive sweep of all 21 plans + 40 workorder briefs, doc-extracted, not yet code-checked).
> A deeper Opus re-audit of the AUDITED claims is recorded separately in `docs/truth.md`.

═══════════════════════════════════════════════════════════════════════════════
# ✅ AUDITED SECTION — code-verified register (all 57 canonical items)
═══════════════════════════════════════════════════════════════════════════════


The 57 canonical items in `.omo/plans/full-backlog-plan.md`, each audited **against the
live tree** (file:line + test runs) by 7 harness lanes (MiniMax + DeepSeek), with the
**consequential verdicts spot-checked by me** — because the workers over-claim (proven:
C20 below). This **replaces** the from-memory `❓` guesses in my earlier "what's remaining"
answers, which were wrong in *both* directions.

**Verdict key:** ✅ DONE (file:line) · 🟡 PARTIAL (gap named) · 🔲 OPEN · ⚪ EXCLUDED/MOOT · ⏳ verification in flight

---

## HEADLINE — all 57 canonical items now code-verified:
- **✅ 45 DONE · 🟡 6 PARTIAL · 🔲 3 OPEN · ⚪ 2 MOOT · 🚧 1 BLOCKED-external** → **~79% actually shipped**, and only **3 items truly open**.
- **Genuinely OPEN (3):** A5b (13 shipping `eslint no-explicit-any`), D7 (egress allowlist live VM test — VM-blocked), E7 (rebuild synthetic harnesses from real captures).
- **PARTIAL (6):** A1 (2 tsc `effective_root` errors), A3 (CI gate has typecheck in *required* not advisory), A6 (compose volume renamed against MUST-NOT + 3 verify scripts read PMX_ literally), **C20 (`delegate_explore` is a production stub — false affordance)**, D8 (browser gate wired, no pixel/vision verify), F6 (patch-spiral detector shipped, force-rewrite action half).
- **MOOT (2):** B2 + B4 — the bundled model was **removed entirely** (compose:131), so "ship a smaller GGUF" has no target. *Decision for Dylan: is keyless-bundled-model still a goal, or is BYO-endpoint the only path?*
- **BLOCKED-external (1):** D3 Univer (upstream 0.25.x has no read-only embed).
- **The big correction: most of what I'd called "remaining" from memory is DONE.** Specifically, items I wrongly reported as OPEN that are verified DONE with tests:
  - **C1a/b/c — fresh-context DoD evaluator** (I called this "the highest-leverage ZERO-code open item"). It's `dod.py` + `dod_evaluator.py` + finish-gate wiring, **33 tests**. Done.
  - **C2 first-hit wake race, C3 dev-server re-materialize, C5 MEMORY.md persistence, C7 serialization jitter, C11 reversible compaction, C13 full-lifecycle prefill, C14 DR isolated sub-contexts, C21 exec-prompt** — all OPEN in my register, all **DONE**.
  - **F3 read-before-write, F7 context-aware read trim** — I called OPEN; **DONE** (gated).
  - **"No CI"** — wrong; `.github/workflows/ci.yml` **exists** (A3).
  - **"Bundled-model swap (B4) = the release blocker"** — wrong; the **bundled model was removed entirely** (`compose.yaml:131` "NO bundled driver model"), so **B2 + B4 are MOOT**, not blockers.
- **One worker over-claim I caught:** verify-c2 marked **C20 `delegate_explore` "DONE, not a stub"** — it is **a stub**: `_run_fanout` (engine.py:3769+) returns `{"stub": True}` "(override for a real subagent)", zero production override. **Real false-affordance** → corrected to 🟡 PARTIAL.

---

## Track A — Release honesty & CI
- 🟡 **A1** tsconfig.build / shipping tsc→0 — build-config + Dockerfile + fixture-keeping exclusion in place, but `tsc -p tsconfig.build.json` still has **2 real shipping errors** (`effective_root` missing on `api/projects.ts:36` + `ProjectStorageSection.tsx:157`). Gate red.
- ✅ **A2** demo-data badge — `DemoDataBadge.tsx` + `client.ts:41 isDemoMode()` + `Shell.tsx:74`, 2 tests.
- 🟡 **A3** minimal CI — `.github/workflows/ci.yml` exists (required + advisory split), but `typecheck:build` is in the **required** job (red while A1 open) instead of advisory; gate contents diverge from brief.
- ✅ **A4** de-flake make test (TOCTOU #25) — `session.py:114,284,525` task tracked+cancelled in destroy(), test passes.
- ✅ **A5a** ruff F821/B904 — live `ruff check --select F821,B904` → **All checks passed** (fixed in `bd4db27`).
- 🔲 **A5b** eslint real errors — live `eslint src` → **28 problems (13 in shipping files**: Markdown.tsx, blocks.tsx, AgentStatusBar.tsx, grounded.ts — all `no-explicit-any`). Unmet.
- 🟡 **A6** PMX_→DISCO_ rename — reader (`env.py disco_env`), secret-key fallback, `.env.example`, compose defaults, 8 tests all in place; **gaps:** compose volume renamed to `disco-data` **against the MUST-NOT** (orphans old `pmx-data`); 3 `packages/agent-server/scripts/verify_*.py` read `PMX_LOCAL_*` literally (bypass disco_env); ~86 PMX_ docstring stragglers.

## Track B — 8 GB keyless
- ✅ **B1a** EMBED/RERANK_MODEL env knobs — `local_encoders.py:91,97`, 9 tests.
- ✅ **B1b** lite ONNX tier — `local_encoders.py:103-124` (bge-small + ms-marco-MiniLM), 11 tests.
- ⚪ **B2** ctx 8K + KV quant — **MOOT**: bundled llm service removed from compose (`compose.yaml:131` "NO bundled driver model"); no target.
- ✅ **B3** encoder OOM guard (#26) — `local_encoders.py:34-82` EncoderUnavailable + _require_ram, 3 tests.
- ⚪ **B4** replace bundled Qwen3-4B — **MOOT**: no bundled model exists to replace (removed). *Open question for Dylan: is keyless-bundled-model a goal at all anymore, or is BYO-endpoint the only path?*
- ✅ **B5** sweep leaked /tmp roots — `process.py:202-270` dual-prefix sweep, wired `runtime.py:1642`, 4 tests.

## Track C — Agent-loop correctness (the big surprise: nearly all DONE)
- ✅ **C1a** external DoD spec (write-once) — `dod.py` + `sqlite.py:144-373`, 13 tests.
- ✅ **C1b** fresh-context evaluator — `dod_evaluator.py:1-610`, 16 tests.
- ✅ **C1c** finish-gate wiring — `engine.py:4159-4250,5451`, 4 tests.
- ✅ **C9** live-window condense — `view.py:645-689`. ✅ **C10** keep_recent tool-turns — `view.py:732-774`.
- ✅ **C6** recitation on cadence/drift — `engine.py:3060-3125`, 12 tests. ✅ **C16** hard_reset pointer-flush — `view.py:783-802`, 9 tests. ✅ **C18** plan-step verify predicates — `plan.py:42` + `engine.py:3791-3860`, 3 tests.
- ✅ **C2** wake connect-retry — `host_proxy.py:107-144`. ✅ **C3** dev-server re-materialize — `shell_sessions.py:327`. ✅ **C5** .disco/MEMORY.md persistence — `engine.py:3482` + `session.py:85`, 24 tests. ✅ **C7** serialization jitter + nudge-pool — `engine.py:1056-1098`. ✅ **C11** reversible compaction recover — `view.py:136-189`. ✅ **C13** full-lifecycle prefill — `agent.py:95-113` + `types.py:97`, 24 tests. ✅ **C14** DR isolated sub-contexts — `gather.py:52-84,152` fresh per-leg messages+CallContext (**I spot-checked: real isolation**). ✅ **C15** idle-kernel cull — `kernel.py:545-648`. ✅ **C21** exec-prompt tuning — `prompts.py:261-308`.
- 🟡 **C20** subagent fan-out (`delegate_explore`) — tool spec + cap + intercept + tests are real (`engine.py:1638-1723`), **but `_run_fanout` is a production STUB** (returns `{"stub":True}`, no override) → **false-affordance**. *(I spot-checked; the verify-c2 "DONE" was an over-claim.)*

## Track D — Feature completion (D2–D12) — verified (DeepSeek lane)
- ✅ **D2** slides deck viewer — `blocks.tsx` SlidesBlockComponent, 15 tests.
- ⚪ **D3** Univer read-only grid — **BLOCKED (external)**: `@univerjs` 0.25.x collab-only, no read-only embed (`blocks.tsx:335-338`).
- ✅ **D4** live audio + mixer robustness — `_audio_mixer.py`, 15 tests (**also proven live this session: real 7.1 MB mp3**).
- ✅ **D5** Kokoro Settings toggle — `AudioSection.tsx` 4-mode toggle + off→unload.
- ✅ **D6** DatasourceEvent emission — `app.py:467-486` `await store.append(DatasourceEvent(...))` at attach site (**I spot-checked: real emission; earlier "no emission path" scan was stale**).
- 🔲 **D7** egress allowlist live VM test — OPEN (no repeatable script / VM evidence; VM-blocked).
- 🟡 **D8** headless-browser/screenshot verify gate — gate wired but no pixel/vision-driver verification (the screenshot-correctness half).
- ✅ **D9** image gen + binary-safe write — `image_gen.py` registered, tests pass.
- ✅ **D10** share-bundle ↔ cassette unify — `bundle["cassette"]` projection, tests pass.
- ✅ **D11** running-tasks dashboard — `views/ActivityView.tsx` dashboard + global indicator, 7 tests (**I spot-checked: real; earlier "the open Phase-2 item" was stale**).
- ✅ **D12** in-block artifact download (cid threading) — SheetBlock + SlidesBlock, 26 tests.

## Track E — Polish / debt
- ✅ **E5** #25 hot-apply + docker-wedge guard — `_container.py:160-241` `_safe_reload` bounded thread + `config.py` mutable hot-apply.
- ✅ **E6** #10 ApprovalDiff real new-hash — DONE.
- 🔲 **E7** rebuild synthetic harnesses from real captures — OPEN (still `_ScriptedRouter`).
- ✅ **E8** egress podman/local proxied allowlist — DONE.

## Track F — Weak-model assist (all gated default-off) + HS
- ✅ **F3** read-before-write (21 tests) · ✅ **F4** bootstrap detect (21) · ✅ **F5** thinking-budget (12) · 🟡 **F6** patch-spiral — detector shipped+gated+tested, the "force full rewrite" action half is the gap · ✅ **F7** context-aware read trim (7) · ✅ **F8** mid-turn arg trunc (11) · ✅ **F9** read-dedup (16).
- ✅ **HS-02** anchored compaction (7) · ✅ **HS-03** re-grounding cadence (10) · ✅ **HS-07** ThinkTool (3) · ✅ **HS-08** invalid-tool reroute invariant (4).
- *(F1 multi-format recovery + F2 "did you mean" confirmed DONE earlier this session — `toolcall_recovery.py` + `engine.py:82,4908-4925`.)*

---

## So what's *actually* left (verified), the short list:
1. **Release polish (Track A):** A1 fix 2 tsc errors → A3 move typecheck to advisory (1 coupled fix); A5b 13 shipping `eslint any`; A6 volume-name revert + 3 verify scripts.
2. **C20 `delegate_explore`** — wire a real read-only subagent round-trip, or stop advertising the tool (false-affordance).
3. **F6** patch-spiral force-rewrite half; **E7** real-capture harnesses.
4. **Track D:** only **D7** (egress live VM test, VM-blocked) and **D8** (browser pixel-verify) remain; D2/D4/D5/D6/D9/D10/D11/D12 all DONE, D3 external-blocked.
5. **MOOT/decision:** B2/B4 (bundled model removed — confirm BYO-endpoint is the intended path).
6. **(Separate from these 57)** the release-*engineering* set from the archive (compose deploy, LICENSE, demo gallery, consolidated SECURITY.md) — though note verify-a already found CI exists + demo badge done.

**Caveat:** DONE verdicts are worker-audited (tests + file:line) with my spot-checks on the consequential ones; I caught 1 over-claim (C20) in 2 spot-checks, so treat ✅ as high-but-not-perfect confidence. Driver still on `120b-free` (`disco-config.json`, backup `.bak-*`). Branch `build-surface-recovery-ux` unpushed.

═══════════════════════════════════════════════════════════════════════════════
# 🗂 UNAUDITED SECTION — raw archive sweep (doc-extracted leads, NOT code-checked)
═══════════════════════════════════════════════════════════════════════════════


Heavy de-duplication is expected across sources (these 21 docs are 30 overlapping views of
the same backlog). The verbatim agent outputs are preserved below, grouped by source slice.

---

# SLICE 1 — release-execution-plan.md + release-roadmap.md

## SOURCE: `docs/archive/release-execution-plan.md`

### §1 Reconciliation — Phase 2 PARTIAL items
- Artifact engine — sheets: **Univer read-only viewer DEFERRED** (packages locked, not wired; rp-11 `56c35a0` only landed live-formula .xlsx + honest preview card)
- Artifact engine — sheets: **in-block download DEFERRED** (needs `cid` threaded through `BlockView`)
- **Background-task dashboard — only "dashboard lite" shipped** (rp-01 `30f09c2` = status write-through + History chips + read-repair only; still missing: dedicated running-tasks view, global "N running" indicator, schedule-run history surface, jump-to-task)
- Session replay **fork/take-over** — v2, post-release (rp-06 shipped read-only ShareView)
- **Credibility-weighted source scoring** (beyond cs-01) — post-v0.1
- **Wide-research N-item horizontal mode** ("compare 20 X") — NOT-STARTED, post-v0.1

### §1 — Phase 3 NOT-STARTED / PARTIAL
- **One-command deploy** — NOT-STARTED; `deploy/` = ONLY `sandbox/Dockerfile`; no compose, no installer; runbook is tmux + env tribal knowledge
- **Eval-as-a-feature** — PARTIAL substrate only (`harness/` exists); nothing user-facing; nothing scores an arbitrary user model
- **Security posture doc** — PARTIAL; primitives shipped but only `tool-sandbox-contract.md` §7 exists; no consolidated threat model
- **README.md truth pass** — still says "Status: Phase 0 — the Event & State spine"
- **project-status.md truth pass** — still says "the brain isn't plugged in"
- **Install guide, provider/VRAM matrix, demo gallery** — NOT-STARTED
- **Versioning** — all packages 0.1.0, zero git tags, no LICENSE, no CONTRIBUTING, no `.github/`/CI
- **Mobile pass** — NOT-STARTED
- **Windows packaging** — NOT-STARTED (hard POSIX deps: tmux, gVisor/podman, ssh-socket)

### §1 — Standing debt
- **E3 `</parameter>` leak root cause** — mitigated (`openai_provider` _raw fallback + rp-04 strip), root cause never bisected
- **Real-sample harness backfill** — streaming/provider/DR-lifecycle harnesses still `_ScriptedRouter`/synthetic
- **HS-01 shell spill-to-file** — NOT-STARTED
- **HS-02/03/05 reality-block cluster** — NOT-STARTED
- **HS-04 stuck-detector upgrade** — NOT-STARTED
- **HS-06** — design-blocked; **HS-07** — NOT-STARTED; **HS-08 hidden-invalid-tool** — NOT-STARTED (verify-overlap w/ rp-12)
- **First-hit wake race** — OPEN
- **ruff/flake lint debt** — OPEN (blocks CI green)

### §2 Wave A/B/C/D (release engineering) — all the above re-stated as ordered work, plus:
- A1 doc truth pass; A2 E3 isolation + wake race + lint; A3 HS-01/04/08 harvest
- B1 Tasks view + "N running" + schedule-run history + jump-to-task; B2 .xlsx grid preview + cid-threaded downloads; B3 **bundle import** (untrusted-input security: version-guard + schema-validate + fence)
- C1 cassette→rp-06-bundle unification + retire `_ScriptedRouter`; C2 **`disco verify` CLI** graded battery + reference scorecards
- D1 docker compose (app+agent+frontend+sandbox, keyless, healthchecks, footprint); D2 GHCR publish + digest pin + `-lite` variant + v0.1.0 tags + changelog; D3 SECURITY.md threat model (every claim file:line-verified); D4 LICENSE (AGPL-vs-Apache = Dylan) + GitHub Actions CI + CONTRIBUTING; D5 install guide + provider/VRAM matrix + recommended-model table + demo gallery; D6 WSL2/Windows verify + mobile responsive pass

### §3 Explicitly post-v0.1 (cut)
Wide-research N-item; replay fork/take-over; internet-public share links; credibility-weighted scoring; design-reference pack (tier-1 style-preset skills = suggested first post-release order); HS-02/03/05; HS-06; HS-07; messaging triggers (Telegram/mail); GitHub two-way sync; chat-vs-agent routing; cross-session memory (gated on 27B A/B); native apps.

## SOURCE: `docs/archive/release-roadmap.md`
### §2.1 Research scorecard — absent / partial still-open
- #1 clarifying questions (now done rp-13); #5 parallel fan-out (now rp-04); #10 chart blocks in reports; #12 audio (now rp-09); #15 file upload (now done); #16 MCP as research source (now rp-05); **#17 vertical depth (arxiv/SEC EDGAR/yfinance keyless)** — never implemented; **#18 wide/horizontal N-item** — post-v0.1
- #4 mid-run interrupt/steer/inject (stop/resume at boundary exists; no steer/inject); #6 credibility scoring surfaced; #8 contradiction surfacing (cs-01 partial; credibility list open); #13 follow-up on finished deep report (mechanism exists, was unexposed); #20 answer-UX micro-polish (cards-slide-in citation sync, favicons, snippet hovers)
### §2.2 Build scorecard still-open
- documents/reports as files (PDF/DOCX now rp-07; sheets gap persists); spreadsheets+charts (charts rp-03; sheets partial); **deploy to hosted URL** (no publish flow); GitHub two-way sync (post-v0.1); **Projects = instruction/knowledge container** (workspaces exist, no instruction/knowledge container); cross-session memory (post-v0.1); chat-vs-agent routing (post-v0.1)
### §2.3 Usability
- #3 no parallel-task awareness (backend exists, UI doesn't show); #5 no onboarding; #6 mobile (build surface desktop-only)
### §3 Phase 3
- one-command self-host; eval harness as "verify your setup"; security posture doc; docs+gallery; mobile pass + license + naming + CONTRIBUTING; messaging trigger (Telegram/email)

---

# SLICE 2 — next-fix-set-plan.md (RP pack + §5–§9)

## §1 Gap inventory → RP assignments (most RP orders since shipped; residue noted in SLICE 11)
gap-15 (Phase 3) DEFERRED; gap-16 RP-00 process debt.
## RP-00 process debt
- Real-sample harness rebuild (blocked on rp-06 bundle = cassette format); E3 param-leak root cause before rp-04; egress allowlist proxy = rp-05 rung 0
## §5 Post-RP Design-reference pack (NOT ordered)
- **tier-1 style-preset skill pack** (named design specs as SkillStore entries for UI builds); **tier-2 license-clean template scaffolds** (vite+tailwind starter trees, copy-then-customize); **tier-3 reference-image matching** (drop screenshot → vision driver "match this aesthetic")
## §6 OpenHands mining (NOT ordered)
- stuck-detector scenarios 1/2/4 feeding the graduated valve; critic finish-gate frame (score finish → followup not finish, bounded max_iter); weak-model FC kit (fn_call_converter prompt-mocked calling, FunctionCallValidationError→USER, corrective nudge on empty turns); ThinkTool; condenser framework (HARD/SOFT, hard_context_reset ladder, minimum_progress ≥10%)
## §7 OSS harvest ledger (HS- floaters, NOT in any RP order)
- HS-#3 shell spill-to-file; HS-#5 structured checkpoint + anchored-summary UPDATE; HS-#8 scheduled facts-survey re-grounding (+ post-restart); HS-#9 duplicate-content stuck detector (n-gram); HS-#2 epochal observation masking **DESIGN-BLOCKED**
## §8 OpenCode steals (NOT ordered)
- hidden-`invalid`-tool reroute; anchored-summary UPDATE compaction; interrupted-tool `[Tool execution was interrupted]` rendering + orphan exclusion; max-steps cap = forced TEXT-ONLY wrap-up; doom-loop detector (3× identical → permission ask); **finish-reason distrust (exit requires finish AND no pending tool-call parts — AUDIT our driver)**; capability-adaptive truncation hints; Qwen sampling pin (temp 0.55/topP 1 — cross-check llama.cpp); prune-in-place caution (hostile to llama.cpp prefix caching)
## §9 Handoff-document design (ratified, NO order)
- write-through checkpoint at every plan-step transition; harness-rendered sections (plan/deliverables/pinned facts), model fills only narrative slots; no "ask first" pattern (ask_user unlocks only after one real action); handoff rendered to human on resume; candidate: fold into reality-block/re-grounding line

---

# SLICE 3 — remaining-work-plan.md + pending-items-status.md
(Track A–H; verified-open as of 2026-06-13. Many overlap canonical .omo plan.)
### Track A: A1 tsconfig.build (28/39 tsc errors in SHIPPING files); A2 demo-data badge on env.js fail; A3 minimal CI; A4 de-flake make test (TOCTOU #25); A5 ruff F821/B904 + eslint 67; A6 PMX_→DISCO_ rename
### Track B: B1 lite ONNX tier + real PMX_EMBED/RERANK_MODEL env knobs (~4GB→0.8GB); B2 ctx 32K→8K + KV q8_0 (~3.7GB); B3 encoder OOM guard (#26); B4 replace bundled Qwen3-4B; B5 ~3088 leaked /tmp/pmx-sbx-*
### Track C (loop): C1 B7 fresh-context evaluator (ZERO code, highest leverage); C2 first-hit wake race (connect-retry); C3 re-materialize agent dev servers on wake (DC-02 only restarts static http.server); C5 auto-spill + .pmx/MEMORY.md (PARTIAL); C6 recitation on cadence; C7 serialization-seed jitter + nudge-pool (PARTIAL); C8 propose_plan_update loop bound; C9 condenser honor live window not 24k/32k (PARTIAL); C10 keep_recent count tool-turns not events; C11 reversible-compaction on-demand recovery (PARTIAL); C12 CodeAct-as-default **NEEDS DYLAN DECISION**; C13 B9 prefill full lifecycle (PARTIAL, DISCO_PLAN_PREFILL=1 OFF); C14 DR isolated sub-contexts (shared router today); C15 idle-kernel cull + RLIMIT_AS-not-cgroup; C16 hard_reset pointer-only flush; C18 plan-step verify predicates (PARTIAL); C20 subagent fan-out; C21 exec-prompt tuning. (DROP: C4, C19. DONE: C17.)
### Track D: D2 slides deck viewer; D3 Univer grid (blocked @univerjs 0.25.x); D4 live audio acceptance + mixer (#16); D5 Kokoro Settings toggle (residual only, #21); D6 DatasourceEvent emission path (no producer); D7 egress allowlist LIVE VM test; D8 headless-browser/screenshot verify gate; D9 image gen + binary-safe write; D10 share-bundle↔cassette unify; D11 running-tasks dashboard (PARTIAL); D12 in-block download (cid threading). (DROP: D1 deploy_preview intentionally absent.)
### Track E: E1 _save_autonomous atomic write; E2 probe cache TTL/invalidation; E3 bookkeeping cap vs ≥7 batched plan_step; E4 snapshot binary-file skip + deleted-file note; E5 #25 hot-apply + docker-client wedge guard; E6 #10 ApprovalDiff real new-hash; E7 rebuild synthetic harnesses from REAL captures; E8 egress podman/local proxied (PARTIAL, deny-all today)
### Track F (SmallCode harvest, behind gate): F1 multi-format tool-call recovery (VERIFIED GAP openai_provider.py:385); F2 quality monitor "did you mean" + cross-turn repeat; F3 read-before-write guard; F4 bootstrap detection; F5 thinking-budget mgmt (head+tail truncate, disable on retry≥2); F6 patch-spiral detector (no-op half shipped); F7 context-aware read trim; F8 mid-turn arg truncation; F9 general read-dedup (PARTIAL); F10 Contract/DoD guard (→ C1)
### Track G (HS-01..08): G1/HS-01 shell spill; G2/HS-02 anchored compaction (verify-overlap); G3/HS-03 scheduled re-grounding (verify-overlap); G6/HS-06 epochal masking DESIGN-BLOCKED; G7/HS-07 ThinkTool (XS); G8/HS-08 invalid-tool reroute (verify-overlap). (DONE: G4/HS-04, G5/HS-05→C1.)
### Track H (BP-G): BP-G9 multi-service builds; BP-G10 Build egress open→filtered + D7 live test (PARTIAL); BP-G12 cockpit UX surfacing (PARTIAL).
### pending-items-status.md residue: GAP-H mode-boundary de-mutation (deferred); 3-strike breaker AS-IS by design; E3 mitigated-not-root-caused; egress podman/local PARTIAL; deployment_url/manifest backend field; liveness animations; plan front-door polish; synthetic-harness rebuild; future-plans 2A/2B/2C/3A/3B **STALE/DROPPED per RWP C19 (no-automatic-nudge invariant)**.

---

# SLICE 4 — universal-readiness-plan.md + fable-plan-reconciliation.md
### universal §A: A2 paid-model transient toast
### §B (providers): B0-persist per-conversation model_override (ephemeral, lost on restart); B0-defaults ddgs/local/bundled as fresh-install defaults; B1 pluggable extraction (Firecrawl + LocalExtraction httpx+trafilatura) + Settings UI; B2 pluggable search (ddgs default + Tavily/Brave/Serper BYO) + Settings UI; B3 build-agent search/extract return clean markdown not `str(results)` (`builtin/retrieval.py:38,60`)
### §C: C1 reveal remote-encoder endpoint fields + persist URLs in ConfigStore
### §D: D1 opening a project must be read-only + explicit Resume (today auto-spins GPU on open); D2 circuit-breaker recovery-proposal LLM call + AlternativesEvent + __continue__/__steer__ + frontend AlternativesGate
### §E: E1 background-process persistence (detached serve tool); E2 `<deploy_rules>` prompt; E3 `</parameter>` leak; E4 verify-on-finish static-site path; E6 preview_status + restart_preview + hard-deny pkill :8000; E7 "Refresh preview" button
### §G: auto-suspend/resume state machine (live⇄suspended); one-time orphan container cleanup
### §H: H1 cap condense trigger at fixed working budget ≈16-32k (decouple from 1M window); H2 elide large tool-call args in View; H3 bound recitation/pin set; measure: per-action input-token logging
### fable-reconciliation OPEN/PARTIAL: GAP-A keep_recent counts events not tool-turns; GAP-C auto-spill to disk; GAP-E deploy_preview absent; GAP-G DatasourceEvent emission; 2.4 headless-browser verify + verify_app tool; 2.5 image gen + binary write; 2.6/3.5 serialization jitter + nudge-pool; 3.1-S5 hard_reset pointer-only; 3.3 `<deploy_rules>` + detached serve + pre-expose self-test; 3.4B .disco/MEMORY.md persistence; 3.6-1 rolling transcript cache markers; 3.6-2 mode-boundary KV break (P1); 3.6-4 cache-write cost tracking; §4 plan-step verify predicates; 2.3B egress live VM test (P1 security); B2 kernel-spill/CodeAct-default; B6 recitation every-step; B7 fresh-context evaluator (P1); B8 idle-cull + cgroup; B9 full-lifecycle prefill; §4 DR isolated sub-contexts; shell/file_write tool descriptions de-steer.

---

# SLICE 5 — agent-architecture-rebuild-plan.md + architecture-rebuild-writeup.md
### §0 hardware constraints (gate B9): tool_choice:required NOT enforced on the llama.cpp build; grammar/json_schema return empty content; re-launch llama.cpp with --jinja + Hermes/Qwen tool template (server config not taken)
### §2 Part A bandaid rollback — BLOCKED on B1+B2 landing (surgical removal of read-streak/force-commit family; drop 4 force-commit tests) — DONE in tree per BP-07 but plan-doc-stale
### §3 B-series (per the plan; tree status varies — VERIFY): B1 rolling-window masking + restorable refs + tune M; B2 CodeAct-default; B3 deterministic-by-seq tail variation; B4 keep failed actions/errors; B5 byte-stable prefix + sort_keys + prefix-caching-on; B6 recitation on-demand; B7 external DoD spec + fresh-context evaluator (one feature/iter); B8 delete pickle runner + KernelSession protocol + process/gvisor gateways + two-flag readiness + interrupt-before-kill + cgroup + idle-cull + jupyter_client/ipykernel deps; B9 prefill masking (Auto/Required/Specified-group) + tool-name prefixes + validate --jinja seam
### §4 single-vs-multi: DR isolated sub-contexts (last in sequence); build read-only sub-agents
### §5 caveats (gates): memory-tool A/B on 27B before any agentic-memory; masking-M validation on own harness
### §6/§7 sequencing + verification gates: EE-Quest read-rut gone; flat kernel latency 50+ cells; masking ≥ summary; fresh-context evaluator catches deliberately-broken feature; all tests green + new tests per item

---

# SLICE 6 — manus-gap-analysis.md + manus-ui-gap-analysis.md
(GAP A–H + UI §2.1–§3; many shipped — VERIFY. Verbatim highlights:)
### GAP A: model-aware condense threshold; token estimate counts tool-call JSON/schemas/system; structured summary schema; keep_recent in tool-turns; reversible-compaction tier (compaction pass in View.of)
### GAP B: prose≠finished (`finish` virtual tool); rephrase prompts.py:142-147
### GAP C: file_read offset/limit; extract/search head-cap; auto-spill to .pmx/observations/; prompt teaches filesystem-as-memory
### GAP D: pin PlanEvent + head user msg against condensation; recency recitation block at View tail
### GAP E: deploy_preview impl; long-running serve/process tool; document Level-2 shell contract
### GAP F: `<error_handling>` ladder; AGENT_DRIVER temp 0.3-0.5; summarizer preserves failed approaches
### GAP G: KnowledgeEvent (scoped, pinned) + move Skills off prompt; DatasourceEvent (durable API docs)
### GAP H: stop mutating tools array on mode boundary; sort_keys=True; cache_control before hosted model
### UI §2.1 live preview: hide dead Preview tab; client-side srcdoc from first write; default to Preview; visual design-edit mode. §2.2 deliverable: deployment_url field; keep preview active at FINISHED; export manifest card; DeliverablePanel hero. §2.3 ask-gate: AWAITING_USER_QUESTION status; ask_user attention:true; AskPanel. §2.4 control: wire cancel() to Stop button; Kill confirm + Stopping…; resolve phantom Pause. §2.5 progress: sticky plan; done/total counter + bar; pin LiveSignalBar footer. §2.6 steer: agent-side ack; pin SteerInput; relabel. §2.7 plan: adjustable per-step skip/reorder; re-planning dimmed-stale state; drafting-plan skeleton. §2.8 async: port DR localStorage session stash to useBuild; completion Notification + title badge; WS reconnect with backoff. §3 polish: liveness all active states; **auto-scroll feed (single highest-leverage polish)**; entrance animations; currently-executing highlight; running-computer header signal.

---

# SLICE 7 — manus-gap-analysis-addendum.md
### §1: `propose_alternatives` tool documented in events.py:393-399 but DOES NOT EXIST (no-false-affordance violation — ship it or fix docstring)
### §2.1 file-rules: `<file_rules>` block; shell description steer; file_write "preferred" note; **file_append tool missing**; optional ShellTool hard-guard regex (`<<EOF`,`>`,`>>`,`sed -i`,`tee`)
### §2.2: notify_user non-blocking tool missing; GAP-B finished=True bug
### §2.3: **hard-DENY tier above HIGH** (raw-device writes, fork bombs, mkfs); **egress allowlist is DEAD CODE at network layer** (egress_allowed() modeled, never translated to iptables/proxy rule; sealed() only true with no allowlist+no NETWORK; real exfil surface)
### §2.4: NO headless browser/screenshot/console capability; verify_app tool missing; deploy_preview stub; FINISHED gate verifies process-completeness not output-correctness
### §2.5: NO image gen; file tools UTF-8-only (no binary write); generate_image tool + 5th router role + critic loop
### §2.6: temp 0.0; no serialization jitter; StuckDetector only HALTs (no escape)
### §3.1 (5-shaper): S1 dynamic budget; S2 snip at ingestion; S3 microcompact no-op turns; S4 structured summary schema; S5 hard_reset = pointer-only flush
### §3.2–3.8: notify/ask partition + affirmative finish; 0.0.0.0 bind rule + serve tool + pre-expose self-test; lazy/path-scoped skills + agent-writable MEMORY.md (file_read offset/limit prereq); noise injection + escape-instead-of-halt graded StuckDetector; explicit prompt caching (4 breakpoints + mode-boundary fix + sort_keys + cached-token measurement); harness-enforced 3-strike circuit breaker (consecutive_distinct_failures, Tier1 reminder/Tier2 AlternativesEvent); persistent CodeAct kernel (Node REPL still one-shot)
### §4: verify predicates on plan gate; PlanPanel done/total; FINISHED "what's next?" hand-back; prompt 3-5 capstones

---

# SLICE 8 — build-loop-hardening + build-loop-oss-research + build-parity-cluster + decomplexity-wave + harvest-backlog
### hardening-plan (4 fixes; A/B/C done this session, D partial): D edit-safety residue — demote file_replace_lines/file_insert_lines from advertised + fix FileRead/FileEdit descriptions + FileEdit old==new refuse + optional py_compile note
### oss-research upgrades: XML `<file>` delimiter + strip-on-ingest; edit-primitive guards (uniqueness-refuse, fail-closed re-anchor hint, no fuzzy fallback, post-edit lint+undo); plan_step→parameter (Cline; REJECTED for Disco); advisory verify + cap (done); stuck-detector content-signature + soft@3 inject; autonomous ask_user suppression (done); condensation refinements (pin task+last N; window-fraction trigger; recoverable sha256 mask; dedup-before-summarize)
### build-parity G1–G12 + BP-0..15 — DONE per BP campaign (VERIFY); marathon harness
### decomplexity DC-01..05 — DONE per memory (VERIFY); DC-05c knowledge-dedup + DC-05d valve taxonomy status UNCERTAIN; Phase B re-run
### harvest-backlog HS-01..08 — HS-01 spill (S); HS-02 anchored compaction (verify-overlap); HS-03 re-grounding (verify-overlap); HS-04 n-gram stuck upgrade (UPGRADE to stuck.py); HS-05 critic finish-gate; HS-06 epochal masking DESIGN-BLOCKED; HS-07 ThinkTool (XS); HS-08 invalid-tool reroute (verify-overlap)

---

# SLICE 9 — project-history×2 + HANDOFF + 05-snapshot
### since-fable-handoff (MOST CURRENT residue at the time): RP-07/09/10/13 "not started" (all since shipped — VERIFY); RP-00 process debt (real-sample harness rebuild; E3 root cause; egress-allowlist rollout to all egress paths not just MCP)
### HANDOFF open decision: **BP-06 masking knob (keep=8/min=600 vs keep=1/min=100) — Dylan never ruled**; shipped at keep=8/min=600; "revisit if context pressure reappears"
### 05-snapshot: **No auth — every conversation is owner "local"** — deferred-by-design, NOT addressed by any RP/DC/BP order; genuinely open

---

# SLICE 10 — BP-00..16 workorder residue (most fully shipped; residue/conditionals)
- **BP-00 vision**: entire order conditional on V2 bench gate; if no Qwen3.6-27B mmproj found or soak failed → only judge-role fallback (in git history, UNIMPLEMENTED) exists; OpenRouter path needs DISCO_SECRET_KEY
- **BP-08**: process-backend kernel memcap explicitly authorized to remain a reported gap if wrapper not simple (no RLIMIT_AS)
- **BP-11**: **Research-surface file upload = explicit "separate, later work"** — no order covers it
- **BP-12**: DR checkpoint resume defects (re-runs completed sub-questions; "(resumed)" placeholder) explicitly LEFT OPEN; workspace-rehydration-into-new-sandbox a conditional STOP-and-report gap
- **BP-14**: interactive terminal user-input (write-to-session) intentionally absent — needs separate order
- **BP-16 marathon**: any failed assertion = open defect in `test-record/marathon/DEFECTS.md` (the primary BP residue); "mostly passed = failed"
- BP-01/03/04/05/06/07/09/10/13/15: fully shipped (a few conditional STOP-gates on VM-201 reachability)

---

# SLICE 11 — DC + RP workorder residue (ratification-pending decisions + reviewer rungs)
- **DC-01**: deprecated path-prefix proxy routes kept one release (removal deferred); live e2e rung was orchestrator-run
- **DC-02**: `wake_for_preview` does NOT restart agent dev servers after wake (502 by design — = Track-C C3 gap)
- **DC-03**: BlastRadiusConfirm swap into runtime.py:583 was orchestrator-deferred (completion unconfirmed)
- **DC-04b**: `/sessions` returns `stale` but frontend intentionally not wired (stale-indicator UI deferred)
- **DC-07**: status UNCONFIRMED (not in STATUS-2026-06-11); whole order (upload sidecar storage + re-materialization + resume reality-block) may be open; sequencing dep on RP-06
- **RP-02**: known pre-existing flake `ResearchSurface.test.tsx` acknowledged, not fixed
- **RP-04**: RP-04b profiling SKIPPED; E3 root-cause (stray `</parameter>` in stream parsing) — unclear if fixed vs mitigation left
- **RP-05b-orchestrator-proxy-decision**: **RATIFICATION-PENDING** — new env `PMX_MCP_EGRESS_PROXY_HOST`; stale anchor correction; anti-gaming proxy test (real stub, not config-shape)
- **RP-05b-reapproval-diff-decision**: **RATIFICATION-PENDING** — `mcp_approvals` gains `tool_descriptions TEXT` (rung-A schema touch); tool-lists-for-diff over hashes-only
- **RP-12**: Rung-3 B9 prefill + Rung-4 grammar-constrained calls shipped FLAGGED-OFF, live llama.cpp feasibility probe is reviewer's
- **RP-14**: Suna session-status enum (harvest #10) DEFERRED (collides RP-01)
- **rp-11**: Univer read-only viewer deferred (@univerjs 0.25.x no lightweight embed); in-block download deferred (cid threading)
- Many RP live-acceptance rungs (RP-01/02/05/06) were "REVIEWER's rung" — execution unconfirmed in the briefs

---

*End unaudited sweep. ~11 source slices, heavy cross-slice dedup expected. Code-verify before acting on any line.*


═══════════════════════════════════════════════════════════════════════════════
# 🧹 HYGIENE — god-files · inspectable logging · security (3× Opus-4.8, 2026-06-16)
═══════════════════════════════════════════════════════════════════════════════

## H1 — God-file decomposition

**Sizes (verified):** `engine.py` 6,452 LOC (run() = lines 4307–6185, ~1,878), `runtime.py` 3,857, `app.py` 1,811 (whole route surface nested in one `create_app`), `view.py` 824, `config_state.py` 954, `blocks.tsx` 650, `ExecutionCanvas.tsx` 978. Dependency direction is already clean + downward (app_server→agent_server→{retrieval,tools}→core); no extraction below adds an upward edge.

### Can we split BY SURFACE (deep research / build / agent)? — Mostly **NO**, the premise is partly false:
- **Agent IS Build.** `runtime.py:653 _BUILD_LIKE_SURFACES = {"build","agent"}`; `_loop_for` builds the same `BuildAgent`+`_compose_build_loop` for both. The ONLY divergences: prompt flavor string (`runtime.py:632-634`) + one egress branch (`runtime.py:1140 if surface=="agent"`). A `surfaces/agent/` dir would be ~5 lines — **don't create it.**
- **Deep Research's engine is ALREADY separate** — it lives in `packages/retrieval/.../deep_research/` and does NOT run through `AgentLoop` (`_compose_deep_research_loop` short-circuits `loop.run()`). What's in runtime.py is orchestration GLUE, cleanly liftable.
- **The shared core (`AgentLoop`) is surface-agnostic by construction** — `engine.py` has **no `if surface ==` anywhere**; surface differences are *injected* (executor/policy/condenser), not branched.
- **Verdict:** decompose `engine.py` BY CONCERN (state machine stays one class), lift the DR glue out of `runtime.py` (the only real surface seam, at the composition layer), split `app.py` by HTTP domain.

### Module extraction (per file, function-level):
**engine.py →** `loop/fc_kit.py` (`_levenshtein` :82, `_nearest_tool_name` :109 — pure), `loop/bootstrap.py` (F4 detectors :191-328 — pure), `loop/tool_specs.py` (all `_*_tool_spec`/singletons — pure), `loop/dedup.py` (F8/F9 funcs :425-620 — pure), `loop/valves.py` (the static/pure `events`-predicates :2400-2805), `loop/messages.py` (`_stuck_escape_reminder` :1090, `_describe_llm_error` :995), `loop/recitation.py` (:3018-3091 — value-object seam), `loop/reground.py` (:779-3175 — emit seam), `loop/plan_predicates.py` (:3791-4004 — needs a `PlanStepProbe` over the sandbox), `loop/dod_gate.py` (:4039-4262 + browser-verify :1727-1803 — a `FinishGate` collaborator), `loop/snapshot.py` (:2841,3450,3488,3541 — workspace/memory mixin). **Stays:** the `AgentLoop` shell, plumbing, control ops, and `run()`.
**runtime.py →** `runtime/deep_research.py` (~700 LOC DR glue: `_research`,`research_stream`,`_maybe_run_deep_research`,`_propose/_execute/_follow_up_deep_research`,`export_report` — a `DeepResearchService`), `runtime/mcp_lifecycle.py` (~500: pool/http/retrieval-wiring + `_MCPToolWrapper`), `runtime/share.py` (~250), `runtime/schedule.py` (fold into existing schedule modules), `runtime/lifecycle.py` (~600: suspend/idle/orphan/rehydrate — most coupled, last), `runtime/resume.py` (degeneracy-condense + reconstruct).
**app.py →** per-domain `APIRouter` factories `make_router(runtime,store)`: `routes/{conversations,files_preview,projects,share,schedules,report,mcp,models}.py` + `routes/_common.py`; `create_app` keeps lifespan/CORS/`include_router`. (Watch the two `path:path` catch-alls register after literal routes.)
**view.py →** optional `view/condense.py` + `view/microcompact.py` (low priority). **config_state.py →** `config/dtos.py` + `config/mappers.py` (pure-move). **Frontend:** `blocks.tsx →` `blocks/{ChartBlock,SheetBlock,SlidesBlock,TableView,CitedText}.tsx`; `ExecutionCanvas.tsx →` `build/canvas/{FilesPane,TerminalPane,PreviewPane,Cockpit}.tsx`.

### Safest extraction ORDER (each independently shippable, no behavior change):
- **Wave 1 (pure moves, zero interface — unit tests suffice):** fc_kit, bootstrap, tool_specs, dedup, valves, messages; config dtos/mappers; view microcompact; ALL frontend splits (independent, can run in parallel).
- **Wave 2 (collaborator seam, state passed explicitly — needs a real run + event-log baseline diff):** runtime share/schedule/resume; app.py routers; engine recitation/reground.
- **Wave 3 (sandbox/executor interface, one at a time, full live regression):** dod_gate (`FinishGate`), plan_predicates (`PlanStepProbe`), snapshot mixin, mcp_lifecycle (`McpManager`), deep_research (`DeepResearchService`), lifecycle (last, most coupled).
- **Wave 4 (SEPARATE initiative, NOT hygiene):** `run()` → `loop/dispatch.py` table of per-meta-tool handlers. This is the only **interface change with real behavior risk** (shared `events`/`noops`/`step` locals, `continue` flow, 6+ streak counters per arm) — gate behind the full live Build+Agent suite. Until then run() stays ~1,800 lines even after every helper is extracted (file drops 6,452 → ~1,800-2,000).

**Honest limit:** no three-way surface split exists to make; `run()` is the one irreducible monolith in pass 1; everything else is a pure-move or a narrow collaborator seam.

---

## H2 — Inspectable logging / observability layer

**Feasible — and ~70% of the substrate already exists, just UNWIRED.** The accurate move is to *activate dormant seams*, not build an OTel stack.

### What's built-but-inert (ground truth):
- `core/obs.py` — `log_span()`/`log_event()`/`JsonFormatter`/`install_json_logging()` exist; JSON logging only on `DISCO_LOG_JSON=1` (`agent_server/__main__.py:55`). The ONLY real span is `agent.step` (`loop/agent.py:156`) — tool-exec/retrieval/condensation/routing have **none**.
- `routing.py:99 RoutingSink` protocol + `:127 CostTracker` — but `runtime.py:635` builds `DefaultLLMRouter(... )` with **no `sink=` and no `cost_tracker=`** → every decision → `NullRoutingSink`, and the router is rebuilt PER REQUEST so `CostTracker` never accrues across turns. `TokenUsage.cached_tokens` captured but never surfaced (KV hit-rate invisible).
- `store/base.py:92 publish_ephemeral`/`subscribe_ephemeral` — the natural inspector transport (already wired to the WS). `events.py:89 meta` — VOLATILE, reserved for "tracing ids", unused. `view.py:446 View.fingerprint()` — per-message hashes that *prove* byte-prefix stability per step. `CondensationEvent` — already a first-class inspectable event.

### Recommended design (one primary move):
Promote `obs.py` into a per-conversation **trace sink** (in-memory ring buffer keyed by `request_id`), wire the already-defined `RoutingSink`+`CostTracker` into it, expose over the existing ephemeral channel behind `DISCO_INSPECT=1`. **Reject putting trace data on the persisted event log** (replay/redaction/KV-prefix hazards + it's redundant — routing/condensation/action-obs are already durable). One **VOLATILE breadcrumb** (`request_id`) on `event.meta` re-correlates a finished run to its trace; the fat (raw prompt bytes, raw model output, token deltas) lives only in the volatile buffer.

### Per-surface, step-by-step — what a dev sees for one run (all 3 surfaces, since they share the engine):
Per loop `seq`: **the prompt sent** (`View.messages`, redacted) + `fingerprint()` diff vs prior step (proves the KV prefix stayed byte-stable); **the raw model output** (`resp.text`+`tool_calls` before mapping); **routing+cost** (`RoutingDecision` model/path/reason/attempt + `cost_usd` + `cached_tokens`); **tool call+result** (Action/Observation already on the log, with the span's ms + pre-snip byte count); **condensation** (`CondensationEvent` inline, `recover_span` for "what was forgotten"); **gated-assist decisions** (F4/F9 fired? — promote the existing F9 `_LOG.info` to a structured `assist.gate` point event so weak-model assists are trace-assertable).

### Boundary safety:
Trace data **never becomes an Event / never enters `View.messages`** (model never sees it, `View.of` purity intact). It rides a separate frame `type=="trace"` that the user WS whitelist does NOT forward → can't leak to the product UI. Pure-read over View/CompletionResponse → never mutates `req.messages`, never breaks B3/B5 prefix rotation. Flag-off = no-op shim (byte-identical prod).

### Concrete plan:
1. **`obs.py`** — add `TraceSink` + `InMemoryTraceSink` (bounded) + a `TraceContext` contextvar (cid, request_id, surface, seq) so spans auto-correlate; `log_span` pushes to the active sink; a `redact()` seam reusing `agent_server/redaction.py`.
2. **`routing.py`** — add `TraceRoutingSink` forwarding `RoutingDecision`+usage; no router-logic change.
3. **`runtime.py:635`** (the highest-value 2-liner) — when `DISCO_INSPECT=1`, pass `sink=TraceRoutingSink(...)` AND a process-lifetime `CostTracker` (so cost finally accrues); fan trace frames onto `publish_ephemeral(cid,{"type":"trace",...})`.
4. **`app.py`** — `@app.websocket("/ws/debug/{cid}")` (forwards only `type=="trace"`, 404 unless `DISCO_INSPECT=1`) + `GET /conversations/{cid}/trace` (REST snapshot for tests).
5. **Span insert points (file:line):** `loop.step` `engine.py:~4399`; `view.materialize` `:~3246`; `agent.step` exists `agent.py:156` (add raw I/O capture); `routing` via the sink; `tool.exec` `engine.py:~3645` in `_execute_and_observe`; `condense` `:~3257`; `assist.gate` at the F9/F4 sites.
**Storage:** nothing new on the event log (one VOLATILE `meta` breadcrumb at `_emit` `:2094`); volatile ring buffer; optional `.disco/trace/{cid}.jsonl` only on `DISCO_INSPECT_PERSIST=1`, **excluded from the share-bundle export path** (`app.py:1468`).
**Effort:** Med-low (~1-2 days, mostly mechanical — the expensive parts exist). **Risk:** Low by construction (pure-read, meta-only durable write, fingerprint diff = its own regression guard); the one review must-get-right: raw prompts pass through redaction before buffering, and the sidecar JSONL is excluded from download/manifest/share paths.

---

## H3 — Security audit (critical findings first)

### 🔴 SEC-1 — CRITICAL — Hostile MCP stdio server inherits the full host env (master key + all provider keys), unsandboxed on the host
**Vuln:** `tools/mcp/stdio.py:62-69` — `resolved_env = dict(_os.environ)` then `.update(self._env)`, passed to `StdioServerParameters` and spawned as a plain host subprocess (the docstring's "the subprocess IS the boundary" is false). Host env contains `DISCO_SECRET_KEY` (Fernet master key decrypting `~/.config/disco/secrets.json`, `secrets.py:25,64`) + every provider key read via `os.environ.get(api_key_env)` (`runtime.py:765,786,964,1374,1375`, `wiring.py:37`).
**Exploit:** user adds ANY third-party stdio MCP server (the normal `npx some-mcp-server` flow); on connect/first call it reads `os.environ["DISCO_SECRET_KEY"]` + the secrets file → decrypts the stored OpenRouter key, harvests all provider keys, reads/writes the host FS, exfiltrates over its own network. Supply-chain-compromised package = full host credential theft, zero extra privilege.
**Fix:** never hand `os.environ` to the child — mirror the process backend's `_clean_env` (`process.py:55-61`): pass only `_SAFE_PASSTHROUGH = (PATH,HOME,LANG,LC_ALL,TZ,TMPDIR,SystemRoot)` + the explicitly-attached `SecretsStore` refs (`self._env`). Update the misleading docstring.
**Guidance:** ideally run stdio MCP servers INSIDE a sandbox (the backend seam exists); at minimum the clean-env fix is mandatory + cheap. Add a regression test: spawn a stub MCP server, dump its env, assert `DISCO_SECRET_KEY`/provider keys absent. (`runtime.py:619 dict(os.environ)` is fine — stays in-process, never crosses a process boundary.)

### 🟠 SEC-2 — HIGH — Unauthenticated Jupyter Kernel Gateway (port 8899) published to host/tailnet on open-egress sandboxes → network-reachable RCE into the sandbox
**Vuln:** `sandbox/kernel.py:326-329` launches `jupyter kernelgateway --ip 0.0.0.0 --port {port}` with **no `--auth_token`**; client connects with no `Authorization` (`:361-368`). 8899 is INTERNAL (`_container.py:43`) but folded into `PUBLISHED_PORTS` (`:44`), and non-sealed boxes publish that set to the daemon host (`gvisor.py:267,281-282`, `local.py:90-92`). Agent surface defaults to **open** egress (`runtime.py:1141`), so 8899 is reachable at the daemon's tailnet IP.
**Exploit:** any other tailnet peer (or LAN host for the local backend) connects token-less, creates a kernel, runs arbitrary Python in that conversation's sandbox — reads `uploads/`+workspace, exfiltrates via the open egress, reaches a second sandbox on the same host (breaks conversation isolation). Unauthenticated externally-reachable RCE, third-party-driven.
**Fix:** per-instance `secrets.token_urlsafe(32)`, pass `--KernelGatewayApp.auth_token={token}`, send `Authorization: token {token}` on every REST/WS call; generate before the readiness poll, thread through start/execute/interrupt/restart/shutdown. Defense-in-depth: stop publishing INTERNAL_PORTS to `0.0.0.0` — bind 8899 to `127.0.0.1` on the daemon host; only USER_PORTS need wide publication.
**Guidance:** live-verify on VM 201 — from a different tailnet host, `curl http://<vm201>:<published-8899>/api` must be 403 not 200 post-fix.

### 🟠 SEC-3 — HIGH — No auth on the agent-server + `CORS allow_origins=["*"]` → drive-by cross-origin read+write/exfiltration
**Vuln:** `app.py:151-157` wildcard CORS (origins/methods/headers `*`); no auth on any route — ownership is a request param defaulting to "local" (`app.py:75-76,816-823`). Loopback bind blocks remote *network* callers but not a malicious page in the user's own browser, and `*` CORS lets that page READ responses.
**Exploit:** user visits any malicious site while the app runs; its JS hits `http://127.0.0.1:8000` cross-origin: `GET /conversations` + `/events` (read every conversation's full log/workspace), `/api/projects/{id}/download` (pull zips), `POST .../messages` (inject into a running agent), `POST .../files` (plant files), `.../kill`, `DELETE /api/projects/{id}`, `POST .../schedules` (persistent cron). With `allow_credentials=False` + `*`, the browser lets the attacker page read every response → full cross-origin read+write, not blind CSRF.
**Fix:** (1) replace `*` CORS with an explicit origin allowlist (the packaged UI origin); (2) require a per-install bearer token on every mutating/data route via a FastAPI dependency (`require_local_auth`), minted at first run + handed to the UI (also closes DNS-rebinding); (3) validate `Host`/`Origin`. Keep the loopback default; document that binding beyond loopback without the token layer is unsafe.
**Guidance:** verify via a `file://` page doing `fetch('http://127.0.0.1:8000/conversations')` — pre-fix returns data, post-fix blocked + 401.

### 🟡 SEC-4 — MEDIUM — `/api/storage/browse` has no path jail → arbitrary host-directory enumeration
**Vuln:** `app.py:955-1001` — `Path(path).expanduser().resolve()` with NO containment; `?path=/`, `/etc`, `/root` all list children (names + is_dir, not contents). **Exploit:** with SEC-3, a page walks the whole FS tree cross-origin (recon for SEC-1/2). **Fix:** jail to an allowlist of bases (home + configured `projects_root`); reject anything not `is_relative_to` one after `resolve()`; apply the SEC-3 auth dep.

### 🟡 SEC-5 — MEDIUM (defense-in-depth) — untrusted-content fences don't escape their own closing marker (fence breakout)
**Vuln:** `builtin/browser.py:131-148` interpolates raw page text before `_FENCE_CLOSE="[END UNTRUSTED WEB CONTENT]"`; `mcp/fence.py:23-57` wraps the body without escaping `</untrusted_mcp_result>`. **Exploit:** a hostile page/MCP result embeds the literal close marker + forged "SYSTEM:" directives → premature close, steers a model relying on the fence. MEDIUM not CRITICAL because channel separation holds (output is a `role="tool"` DATA message, any induced action still hits the SecurityAnalyzer+ConfirmRisky gate). **Fix:** neutralize the close marker in the untrusted body before interpolation (`.replace(_FENCE_CLOSE, "[…escaped]")`), or use a randomized nonce-tagged fence. Add a test feeding a body containing the verbatim marker.

### ✅ Defenses verified SOLID (no action):
- **Egress IS network-enforced** (not the addendum's "dead code" — a non-empty allowlist → `filtered` mode → internal no-NAT net whose only route is the allowlisting proxy sidecar; `_container.py:57-77`, `gvisor.py:173-245`). process/local backend honestly dev-only + clean-env.
- **Sandbox interiors never get host secrets** (container env = proxy vars only; OR key overlaid into a LOCAL env copy for `build_providers`, never `os.environ`/the sandbox — `runtime.py:619-624`).
- **Secrets at rest** Fernet-encrypted, key from `DISCO_SECRET_KEY`, out-of-tree.
- **Export redaction wired** into share/export + streaming frames (`redaction.py:222-230`).
- **Workspace/artifact routes jailed** (normalize + reject `..`/absolute, prefix allowlist, `is_relative_to`, extension allowlist, nosniff). **Preview proxy not SSRF** (upstream from the conversation registry, USER_PORTS-gated, INTERNAL refused). **MCP rug-pull defense exists** (SHA-256 description hash + `ApprovalRequired` on drift) — though it gates *description* changes, not call-time behavior (SEC-1 is the live hole there).

**Two must-fix:** SEC-1 (CRITICAL) and SEC-2 (HIGH) — both concrete code-level violations of the "hostile MCP server / hostile peer must not reach the host or escape the sandbox" boundary, both small surgical fixes.
