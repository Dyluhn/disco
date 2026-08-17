# disco — What Fable Built Since the Handoff

**Span:** `e48c5bc` → `cb86a6e` (HEAD), 2026-06-09 → 2026-06-11. **101 commits, 3 days.**
Picks up exactly where `project-history-since-deep-research-fix.md` (anchor `d2696ae`) ends.
Organized by campaign/workstream, not commit order. Every item maps to a real commit (hash in
brackets). This is the Fable-driven work: three structured order-packs (BP → DC → RP) run through
a deterministic dispatch/verify/commit harness, with model-routing infrastructure built underneath.

---

## 0. The machine that built the machine — orchestration & routing

Before any feature, the **delivery pipeline itself** was built, because a weak local model + cheap
remote workers can't be trusted to self-report. This is why throughput is slow but nothing broken
ships.

- **`orchestrate.sh` + `orders.yaml`** [1c5f657] — deterministic dispatch/verify/commit plumbing.
  An order declares a file manifest; dispatch launches a worker in tmux; a **file-level collision
  gate** forbids two live orders touching the same file; commit stages ONLY the manifest files and
  auto-flips state. `staged` became a first-class state (declared, no work on disk) [612ca8a].
- **Anti-corruption invariants**, each added the day a real incident exposed the gap:
  - **post-commit invariant** — every manifest file must be clean after commit [eaa265d] (caught
    BP-06/07/10 commits that had silently dropped `view.py`, the port-pills frontend, a test removal).
  - **stall detector counts manifest writes as progress** [1f68b33] (a working worker was being killed).
  - **verify-time evidence gate** — `units-*.log` must exist AND be failure-free or the order is
    flagged EVIDENCE MISSING/RED [6f364b1] (catches the worker-skipped-the-full-suite class).
- **Worker fleet & routing** — presets for Gemini Pro [9416715], streaming Sonnet [9b68e14],
  **`pro-direct`** (DeepSeek V4 via direct API, context caching ≈4× cheaper) [45debb0], **`pro-dsk`**
  (DeepSeek V4 via OpenRouter, tools enabled) [13810c3]. Codified in **`ROUTING.md`** [27ac9de]: a
  full table with the **discipline guard** — implementation → DeepSeek/Gemini; vision triage →
  Gemini Flash → Sonnet (never Fable) [47dcef0]; diff pre-filter → DeepSeek Flash, *leads never
  verdicts* [8645af9]; **verification verdicts → pinned-Fable subagents ONLY; Opus never renders a
  verdict** [27ac9de]. Cheap-model claim-vs-diff pre-filter [0f79deb] and screenshot pass/fail
  triage [0c974e4] were prototyped here.

---

## 1. BP pack (17 orders) — turning the sandbox into a real computer the agent can use

The BP pack closed the gap between "the agent emits tool calls" and "the agent operates a live
machine." Each order made a previously-faked or one-shot capability persistent and real.

- **BP-01 — persistent tmux shell sessions** [9fe020b]. 5 tools (open/run/read/list/close),
  self-healing, gVisor-verified. The agent now has *durable* shells, not one-shot `exec`.
- **BP-02 — preview as a visible shell session** [a4b6000]. Port-owner truth + `server_status` +
  auto-serve: the running dev server is a thing the agent (and user) can see and own, not a guess.
- **BP-03 — environment contract** [8b7a3d0]. kill-preview → start-in-session → verify; `pkill`
  denials retired. A deterministic "is the app actually up" handshake.
- **BP-04 — persistent Playwright daemon** [8df4311]. Replaced the one-shot browser fetch with a
  long-lived browser the agent drives across steps (real navigation, not per-call cold starts).
- **BP-05 — finish-time browser verification gate** [d2f3d1a]. The agent must *see its app work in
  a browser* before it's allowed to claim FINISHED.
- **BP-06 — observation masking** [3faf74a]. Old, large tool observations are replaced with
  LLM-view-only stubs so the context window doesn't drown — without touching the real event log
  (KV-stable; the user still sees everything).
- **BP-07 — read-counter family rollback** [406e926]. Surgically removed the read-streak nudge
  machinery that turned out to fight the model more than help (a deliberate un-build).
- **BP-08 — persistent IPython kernel** [2b3321a]. Replaced the pickle-based code runner with a
  real stateful kernel (cross-cell namespace, 4 GiB rlimit). CodeAct that actually persists state.
- **BP-09 — dependency installs are first-class** [37a6007]. A registry egress profile + warm
  toolchain so `pip install`/`npm i` work inside the locked-egress sandbox.
- **BP-00 — vision driver wired end-to-end** [2c30a5e]. Screenshots actually reach the model
  (two root-cause fixes); the agent can *look* at what it built.
- **BP-10 — multi-port preview** [e4d68cb]. Curated USER ports published, owned, proxied, and
  pill-switchable in the UI (a multi-service app is reachable).
- **BP-11 — file upload** [ca5d8dc]. Endpoint + composer + pending-session adoption + feed note —
  upload-then-build as a primary flow.
- **BP-12 — first-class Resume** [0fe6d12]. HTTP route + event-log legality checks + no-replan
  restart survival: a conversation survives a server restart and continues, not restarts.
- **BP-13 — idle-TTL suspend + orphan reconciler + suspended badge** [a13b61c]. Sandboxes suspend
  when idle and reconcile on wake, instead of leaking.
- **BP-14 — Terminal tab = live tmux views** [2d6aba1]. Read-only live terminal in the inspector.
- **BP-15 — screenshots inline in the feed + isolation tier over the wire** [09318e8].
- **BP-16 — marathon gate** [1d9a364]. The endurance test: Phase A PASS, Phase B **SYSTEM FAIL →
  surfaced DEFECT-4** (the stuck-loop), Phase C PASS. This failure is what triggered the DC pack.

---

## 2. DC pack (8 orders) — lifecycle, recovery, and the stuck-loop fight

BP-16 proved the agent could run but not *survive*. The DC pack hardened the full lifecycle and
won the context-survival fight that the marathon exposed.

- **DC-01 — hostname-based preview proxy** [035e33a]. Origin-true sandbox previews (no random ports
  leaking; everything through the agent-server origin).
- **DC-02 — idle-suspend/resume replaces reap-at-FINISHED** [a6553fa, ebda91c]. A finished build is
  suspended, not destroyed; its snapshot survives caches, bad files, and dying pipes.
- **DC-03 — confirmation gates scoped to the real blast radius** [08f1a0a, c2ba0b0]. The risky-action
  gate now fires on actual blast radius (BlastRadiusConfirm), not a blanket rule.
- **DC-04a — actionable session diagnostics** [169d80a] (DEFECT-2 relay fix) and **DC-04b — /sessions
  retry + degrade-to-stale, never 500** [c44005a] (DEFECT-1). The session APIs stopped throwing.
- **DC-05 (a/b/c) — the loop breakers** — this was the multi-day, 8-re-run battle against the stuck
  loop:
  - **DC-05a** [f129a27]: actionless cap, finish valves, knowledge dedup, driver retry (DEFECT-4/5).
    Proven live absorbing a **38.7 s llama-server outage** via driver retry [2b84c62].
  - **DC-05b** [18b8c5e]: resume-path context reconstruction — the DEFECT-4 root cause (a resumed
    loop wasn't rebuilding enough context to make progress).
  - **DC-05c** [1513070]: resume-time condensation of degenerate trailing segments (a wall of
    repeated junk gets compressed on resume instead of re-fed).
  - Plus the fixes the re-runs surfaced: withhold meta-tools until first real action [c3d4fd2],
    post-resume serve gate [5472f82], verify probe must not unlock withheld tools [2887063],
    **persist conversation surface across restarts** [52f85df]. Phase B finally PASSED [7a1c302].
- **DC-07 — uploads re-materialization** [8398573, 0c5dbd6] (DEFECT-7). Uploaded files are copied
  back into a *recreated* sandbox after a restart, via a sidecar store + a single chokepoint in
  `_run_with_persistence`. Live-verified: upload → SIGTERM → resume → files intact [f3d2966].

---

## 3. RP pack (14 orders) — the feature release pack (10 of 14 done)

With the agent stable, the RP pack added the user-facing features. **Done: RP-01,02,03,04,05,06,08,
11,12,14. Remaining: RP-07 export, RP-09 audio, RP-10 slides, RP-13 clarifying-Qs.**

- **RP-01 — dashboard** [30f09c2]. The conversation `status` column existed but was NEVER written;
  now there's atomic write-through + a schema migration + batched read-repair + neutral
  unknown-status chips in History.
- **RP-02 — deep-report rich blocks** [1e4ca4d]. `[[id]]` citation chips render as real chips in
  *both* prose and table cells (shared CitationMarker); table blocks carry `cited_passage_ids` for NLI.
- **RP-03 — chart blocks** [8217e8e]. Typed bar/line/pie/scatter via Chart.js 4 (not Vega), with
  jsonschema validation + one retry-with-error-trace, and **degrade-to-table rather than render a
  broken chart**. (Also root-caused 2 deep_research failures: literal braces in the chart prompt
  broke `str.format`.)
- **RP-04 — wide research, phase 1** [545f7b7]. The engine was strictly sequential; now a
  smart-client pipeline runs **concurrent retrieval + serial MTP synthesis**, budget partitioned
  upfront, hash-stable section ids, per-subquery namespaces, cancel-all on stop/timeout. (+ E3
  stray-XML-tag strip in `_repair_json`.)
- **RP-05 (a/b/c) — MCP client** (the big lever) — connect external Model-Context-Protocol servers:
  - **RP-05a** [247b81b]: stdio transport, connection pool, approval gate, qualified-name invocation.
  - **RP-05b** [9dce032, e021f2f]: rung-B security core — streamable-HTTP transport, **egress
    allowlist**, output fence, retrieval-tier; live UI wired to real CRUD + approval store.
    5-drill live acceptance, 5/5 green, Fable APPROVE [f50fa41].
  - **RP-05c** [7dc2bf4]: active-schema cap — `tool_search` advertise/callable split + an engine
    requery-gate fix so a hidden-but-callable MCP tool isn't needlessly re-queried.
- **RP-06 — replay + share** [85cf645]. A `useReplay` pure-slice hook + ReplayScrubber (hidden while
  live); `share_export` produces a **versioned, scrubbed** bundle (redaction.py, specific-before-
  generic regex as contract); base62 revocable share tokens; a static zero-WS ShareView.
- **RP-08 — scheduled tasks** [f40c949, cb86a6e] *(this session)*. cron-style recurring runs
  (cronsim), run-once-coalesced missed-run policy, one asyncio loop, schedules/schedule_runs tables,
  the confirm card with next-3 runs, wired into the Build surface. **3 review defects fixed** —
  see §4.
- **RP-11 — sheets** [56c35a0] *(this session)*. A `sheet_generate` tool writes real `.xlsx`
  workbooks with **live formulas** (whitelisted, written through the sandbox jail) as deliverable
  artifacts; the frontend renders an honest preview card. **3 review defects fixed** — see §4.
- **RP-12 — B-series residue + weak-model FC kit** [be5b4ff] (DEFECT-6 root cause). The fix that
  let a weak local model survive 13 function-calling fumbles non-terminally: AgentErrorEvent
  `tool_call_id` + orphan-tool-message downgrade, registry-aware bounded requery, name sanitization
  both ways, planning-mode actionless valve.
- **RP-14 — usability sweep** [17b5ce0]. Theme persistence (pre-paint, no flash), Ctrl+K command
  palette (radix, no new deps), history search + empty state, and a cost meter **gated off on paid
  models until real usage events exist** (deliberately no false affordance).

---

## 4. The defects this discipline caught (and the six caught this session)

The verify-before-commit gate exists because workers ship green-but-wrong code. The running tally:

- **DEFECT-1** — `/sessions` 500 → degrade-to-stale (DC-04b).
- **DEFECT-2** — session-diagnostics relay (DC-04a).
- **DEFECT-4** — stuck-loop / context-survival on resume (the DC-05 battle, 8 re-runs).
- **DEFECT-5** — 38.7 s llama-server outage absorbed by driver retry (DC-05a, live-proven).
- **DEFECT-6** — weak-model FC fumbles cascading to terminal failure (RP-12).
- **DEFECT-7** — uploads not re-materialized into a recreated sandbox (DC-07).
- **DEFECT-8** — driver-vision escalation to Gemini Flash [272f59e].
- **This session (6, all shipped "green" by the worker):**
  - rp-11: a **host-side arbitrary file write** (the sheets tool bypassed the sandbox and wrote to
    the agent-server's host cwd with a model-controlled filename); a formula-whitelist **whitespace
    bypass** (`INDIRECT ("x")` slipped the regex); a frontend **false-affordance download button**
    (404s always — no cid on the render path).
  - rp-08: the scheduled fire **re-ran nothing** (it appended a system marker the loop's work-gate
    ignores — now re-injects the original query as a user turn); persisted `depth`/`model_override`
    were **dead data** (never applied before the kick); the schedule UI was **mounted nowhere**
    (unreachable — now wired into the Build surface).
  Each came with a *vacuous* passing test (host-write tests ran with `sandbox=None`; the scheduler
  e2e asserted `kick.call_count` against a mocked runtime). The fixes added the missing load-bearing
  tests: a real jailed sandbox, and the real engine work-gate flipping closed→open.

---

## 5. Where it stands

- **35 of 38 declared orders committed**; the BP and DC packs are 100% complete; the RP pack is
  10/14 feature orders.
- **Remaining:** RP-07 (export PDF/DOCX), RP-09 (audio overviews), RP-10 (slides), RP-13
  (clarifying questions + report follow-ups), plus the standing RP-00 process-debt items
  (real-sample harness rebuild, E3 param-leak root-cause, egress-allowlist rollout).
- The two hardest-to-parallelize orders (colliding backend seams) are now done, so RP-07 ∥ RP-09
  is a clean two-lane next wave.
