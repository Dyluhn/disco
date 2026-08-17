# Backlog — CODE-VERIFIED register (2026-06-16)

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
