These are the remaining work orders. As you complete each item, mark them off. For deferred items, evaluate the recommended action to complete the work order fully. execute it. do not pause to ask the user questions. do not pause the python timer.

---

## EXECUTION PROTOCOL (apply to every batch)
1. For each batch of orders: if not already extensively planned with Codex, **build a plan**, have **codex evaluate** (a) your code, (b) the work orders it targets, and (c) the application code. If codex recommends changes, evaluate + incorporate as necessary. Iterate **however many turns it takes** until codex approves the plan.
2. Then send **agents in parallel where possible** to complete them (disjoint files; each stages only its own paths; backend+frontend split safe).
3. When finished, **submit the changes to codex**. Align feedback; iterate until **codex passes**. Incorporate.
4. **After the entire walkthrough campaign is complete:** assign **2 codex agents to develop UNBIASED tests**. They know only the project code + what each work order was meant to fix — **NOT the actual implemented fixes**. They deliver test criteria; **run them as written**. When fully finished, submit (your test + your test results + the original codex test criteria) to **2 MORE codex agents** to judge whether you cheated/gamed the system. If they say you did → **follow their recommendations no matter the outcome**. Only after all tests pass → move to the AppKit phase.
5. **AppKit phase (only after the whole walkthrough campaign passes):** copy the project to **`disco-nightly`** (all AppKit work happens there). Fan out opus subagents to write surgical prompts for **one epic at a time** → submit the whole epic to codex → iterate until codex passes the plan → implement in parallel where possible → route completed code to codex → iterate → codex pass → implement into the nightly folder. Repeat for **every epic A→O**.
6. **Do not defer.** Drive the application live with **MiniMax** (existing API key). If needed, access the VM/gVisor + proxmox mini-pc via `ssh blackbox`; only alter the VMs needed for testing, no more.
7. **Mark off each step as you complete it.** Heartbeat: every 2nd tick (~10 min) re-read this file.

Source of full surgical detail: `docs/dylans-walkthrough-v3-6-22-26.md` §5 (cited per item). Constraints: WIP-safe commits (never stage `engine.py`/`messages.py`/`recitation.py`/`uv.lock` unless the fix legitimately requires it and the tree is clean); trailers `Co-Authored-By: Claude Opus 4.8` + `Claude-Session`; **no push**.


> **Completed history archived** → [`docs/WORK-ORDERS-archive.md`](WORK-ORDERS-archive.md)
> (Section 1 walkthrough W-*, Phase 1.5, AppKit A→O, and the BW live-walkthrough campaign — all landed).
> A few carryovers remain tracked there but are out of the active path: W-53 (OPS image-gen, cost-guarded), EPIC J (AppKit visual editing).
> The live-testing fix wave (2026-06-24: clarify/TTS/DR/preflight/plan-display/write-loop/recovery/noVNC/thumbnails/model-switch/slides-spiral/sandbox-persistence) is merging to `build-surface-recovery-ux`.

---

## NEXT ON THE AGENDA — Build Soak hardening (after the live-testing fix wave lands)

**Pointer:** the full normative spec is at [`docs/build-soak-guidelines.md`](build-soak-guidelines.md). The python timer points HERE next once the current live-testing fix wave is merged + bounced.

**Gate before starting:** finish the live-testing fixes Dylan surfaced this session (clarify, slides-wedge, TTS, DR, preflight, plan-display, LibreOffice, write-loop, error-recovery, sandbox-config outage, noVNC redesign, thumbnails, model-switch terminal-state, slides post-generation spiral, sandbox connection-persistence + unreachable banner). All merged to `build-surface-recovery-ux` + live-bounced before Build Soak begins.

**What it is:** make the bare Build loop (`submit → plan → approve → execute → produce output → revise → re-plan → execute → verify output`) boringly reliable BEFORE any higher-level Build primitive (AppKit, Cloudflare export, visual editing) is promoted to the main product path. A deterministic evidence-oracle harness — agents may explain/patch/RCA but NEVER adjudicate; only deterministic checks over frozen evidence decide PASS/FAIL/INVALID_RUN/INFRA_FAILURE.

**Promotion gate (§3/§28):** API bare build 100/100 · UI bare build 50/50 · revision scenarios 25/25 · complex revisions 10/10 (≥3 follow-ups) · tool-rejection sim 100% · no P0/P1/UNKNOWN/INVALID in the final soak · every historical P0/P1 has frozen evidence + RCA + patch + exact-seed replay + regression test. Ordering: bare Build → multi-turn revision → AppKit primitive → UI AppKit → main promotion.

**Implementation order (§27):** S1 evidence-lock + oracle schema → S2 classifier + core oracles → S3 headless API runner → S4 UI Playwright runner → S5 multi-turn revision scenarios → S6 fake-model/fake-tool simulator → S7 Claude/Codex repair adapters → S8 exact-seed replay + no-fluke policy → S9 patch-acceptance gate → S10 regression dashboard. Start with the bare Build scenarios + the §20 contract tests (no live model spend) BEFORE the live soak; do NOT start with AppKit.

**Operating rules:** Claude Code = patch agent; Codex = read-only RCA/review; deterministic oracle = sole adjudicator. No "fluke" — a failed run that later passes on replay is `FAIL-INTERMITTENT`, not PASS. Every patch adds a regression test; no patch weakens a planning/tool/verifier gate without an approved migration (§19). The harness lives under `harness/build_soak/` (experimental branches OK) but this guideline belongs in main as the baseline Build reliability contract.

— appended 2026-06-24 per Dylan; see `docs/build-soak-guidelines.md` for the complete spec.
