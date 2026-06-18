# Disco — consolidated roadmap (2026-06-18)

One ordered view of everything still open, folding together: today's driver plan,
the `disco-direction-and-decisions-6-17-26.md` master plan (Track C remainder),
the 6-17 walkthrough/runthru issues, the product-ideas directions, and the
launch-gate / backlog residue. Supersedes the scattered per-doc lists for
planning; the detailed surgical specs still live in the source docs (referenced).

Status legend: ✅ done · ◐ partial · ○ open · ⬚ blocked/decision-gated.

---

## §0 — Already shipped (don't re-litigate)
The recent campaigns closed the bulk of the 6-15/6-17 backlog. Verified done:
- **Build-harness campaign** (K1 + Phase 0 + W1–W6 + seams): read-thrash breaker,
  stale-aware snapshot, syntax-gated writes, capability-gated edits, C18 fix,
  vision wiring. Plus the **god-file decomposition** (engine.py 6446→1543 LOC).
- **Deep-Research polish** (DR-1/2/3 + WALK-01..21): live markdown, planning
  loaders, citation-leak fixes, TTS hygiene + acronyms, recency toggle, branded
  export template, custom audio player, audio mode dialog, schedule UX, agent
  slides/sheets steering, artifacts-tab harvest, pause/steer/resume (WALK-18),
  no-progress breaker (WALK-19).
- **Tailscale runthru B1–B7** (today): export 404, re-plan enforcement (B2+B6),
  container file-exists (B4), completed-build finish (B5), New-button surface (B3),
  verbose browser errors (B7). All live-verified.
- **PDF export polish** (today): SVG charts, numbered citations, clean cover,
  light/dark toggle + full-bleed dark fix.

---

## §1 — Active: driver / provider reliability  ○  (plan written, awaiting go)
Full spec: **`docs/disco-driver-reliability-plan-6-18-26.md`**.
- **P1** OpenRouter provider-preference injection (`require_parameters`,
  `allow_fallbacks`) so tool-calling stops hitting the cookie-only Chutes backend.
- **P2** classify provider rejections + retry with a *routing* change (not the
  model-blaming "fix your JSON" hint).
- **P3** no hardcoded default driver — **persist the last-picked model** and seed new
  conversations from it (Dylan's call).
- **P4** warn on the `_router_now` `config=` footgun.
Effort: P1/P2 small-med (core llm), P3 small-med (settings + frontend pill), P4 trivial.

---

## §2 — Track C: Slides + Artifacts + Editor  ⬚  (gated on the slides decision)
The largest unbuilt track. Detailed surgical specs already exist in
`disco-direction-and-decisions-6-17-26.md` §3 (C1–C8) and §4 (4.1–4.6); §1 Brand
engine is **✅ done** (today's PDF export consumes `disco.core.brand`).

**DECISION GATE — slides architecture (A/B/C).** Still open; Dylan flagged it a
research question and warned *"we've previously solved issues by making them LESS
deterministic; not sure more-deterministic (constrained schema) is viable here."*
Today's slides are Marp (image-per-slide). The plan's **C4 experiment** (15–20
prompts × emit-strategies × models, blind-scored) must run to **freeze the deck
schema** before C1/C2 (and the §4 deck-patch schema) can land. → produce
`docs/slides-experiment-verdict.md`.

- **C5 render/preview fixes** ○ — independent of the schema; can land first.
- **C6 `artifact_mode` flag** ◐ — defined in `routes/_common.py` but **not wired**
  into `_compose_build_loop` (NeverConfirm + INTERACTIVE + `artifact_scope()`).
  Small, schema-independent; Dylan confirmed "flag, not a separate surface."
- **C7 image-gen backends** ○ — see §3 (this is the same work as the image-gen
  product feature; build the ImageBackend seam once).
- **C1/C2/C3/C8** ⬚ — deck schema, generator, native **editable** `.pptx`
  (python-pptx, real text boxes), chart/table layouts. Gated on C4 verdict.
- **§4 Editor / element-to-agent** ⬚ — selection overlay, postMessage bridge,
  Deck/Source resolvers, agent edit-loop (`deck_patch`), React deck editor, reuse
  for the app/site builder. Gated on K1 (done) + C4 verdict + C1–C3.
Effort: large (~the biggest remaining track). Acceptance is visual + opened files.

---

## §3 — Product directions (from `product-ideas-2026-06-17.md`)
**Prerequisite cross-cut — FILE-DELIVERY  ○ (blocks 3 of these).** The agent can't
reliably hand a user a file through the conversation feed (the in-block download +
DeliverablePanel exist but `cid` isn't threaded on the research surface; no
first-class "agent emits file → user downloads" on Build/Agent or the DR closing
card). Wire this first — it unblocks image-gen delivery, the DR→agent handoff, and
the podcast. Effort: medium (glue over existing primitives).

- **Iterative mode toggle ○ (highest novelty, NO prereq).** A toggle beside
  scope/think: use the per-claim **NLI verdicts** as a signal to re-research only
  the weak-sourced claims (not a full DR) — fresh searches for weak items, rewrite
  weak sections + conclusion/intro, preserve strong sections verbatim. ≤5 iters.
  **Open design problem (needs Dylan):** keeping the report coherent (not "choppy")
  when only some sections are rewritten. Effort: high; the coherence approach is
  the unsolved bit.
- **Image generation as a real tool ○ (blocked by FILE-DELIVERY).** Replace the
  procedural-PIL placeholder. Design lever (Dylan): the OpenAI
  `/v1/images/generations` shape is the best compatible boundary (covers paid
  OpenAI + LocalAI + mimics); add a **ComfyUI** adapter for the dominant local
  path. So `bundled | openai-images-compatible | comfyui`, each via Settings
  provider CRUD (encrypted key — infra already shipped). Same seam as C7. Effort: high.
- **DR closing-card → agent handoff ○ (blocked by FILE-DELIVERY).** On report
  finish, offer agent actions ("Make slides") that seed a Build/Agent conversation
  with the report as context + a **pre-approved plan**, skip the approval gate, and
  deliver the artifact. Generalizes to any closing-card action. Effort: medium.
- **Real two-host podcast ○ (blocked by FILE-DELIVERY + RP-09 acceptance).**
  Agent-authored dialogue script → multi-voice TTS → mix → optional video → deliver.
  Bigger than today's two-voice `report_audio`. First verify RP-09 (below). Effort: high.

---

## §4 — Launch gates (from `north-star.md` / release pack)
- **Benchmarks / eval suite ○ (Dylan mandate: "before we launch").** A real set:
  research grounding accuracy, agent task success, latency, cost. Rides the shipped
  `disco verify` surface. Effort: medium-large.
- **8 GB keyless path + bundled model ○ (release blocker).** Three sub-items:
  (a) clean-box gauntlet pass (<8 GB RSS, all surfaces/tiers); (b) lite ONNX encoder
  tier (e5-small + reranker-tiny; make `DISCO_EMBED_MODEL`/`DISCO_RERANK_MODEL` real
  knobs; default ctx 32K→8K; optional KV quant → ~4 GB peak); (c) replace the stale
  Qwen3-4B bundled model with a current-gen tool-call-strong small GGUF, framed
  honestly as a smoke test. Effort: large.
- **License decision ○ (Dylan's call: AGPL vs Apache).** Trivial once decided.
- **Release-eng pack ◐.** Done: `self-host.md`, `provider-matrix.md`, `disco verify`.
  Open: minimal honest CI, demo gallery, `git tag v0.1.0` + changelog, mobile
  responsive pass, SECURITY.md §8 (disclosure process + threat model). Effort: medium.

---

## §5 — Open polish / search / attach / live
- **F1 keep-searching on no-answer ○** — bounded reformulate-retry (≤2) when basic
  research returns 0 grounded claims. (Confirmed absent.) Small.
- **G1 / DR-4 attach files ○** — UploadComposer in the *initial* box (pre-create cid);
  uploads → Passages → cited. Steer-attach already works; just absent from the empty
  state. Medium.
- **DR mid-run steer / inject sources ○** — stop/resume at section boundary exists;
  steer + inject don't. Medium.
- **RP-09 live audio acceptance ○ (#16)** — Kokoro TTS + toggle shipped; the live
  mixer-robustness acceptance pass is open. Gates the podcast. Small-med.
- **noVNC live browser ⬚ (#53–57, 5 phases)** — Xvfb/x11vnc/noVNC → lazy desktop
  service → agent-server route → frontend toggle + Settings gate → live/jail
  acceptance. Prereq: D7 gVisor egress allowlist. Large; design in
  `docs/design-novnc-live-browser.md`.

---

## §6 — Engine / correctness / debt backlog (not launch-blocking; appendix)
Lower priority; many are P1/P2 correctness or infra-/design-blocked.
- Single stable system prompt (stop mutating the tools array per mode — KV-prefix
  stability) ○ · auto-spill large observations to disk ○ · edit-tool hardening
  residue (demote `file_replace_lines`/`insert_lines`, fix steering descriptions) ○ ·
  deploy_preview detached long-running serve (300 s ceiling) ○ · one-feature-per-
  iteration scope enforcement ○.
- **Design-blocked (need a Dylan call):** epochal observation masking vs KV stability
  (HS-06). **Infra-blocked:** grammar-constrained tool calls (llama.cpp `--jinja`
  relaunch, B9).
- Test debt: rebuild the DR-lifecycle harness from **real captures** (E7, violates
  the real-sample-harness rule) · real-sample backfill for streaming/provider
  harnesses. Lint debt: shipping `tsc`→0, ruff F821/B904, eslint→0.
- UX micro-polish: main-feed auto-scroll ("highest-leverage polish"), LiveSignalBar
  pinned to footer, Build session-stash persistence, adjustable-plan/visual design-
  edit mode. Layering nits: a couple of circular/transitive imports + root hygiene
  (move `run_manual*.py`, root `test_*.py`, stray CSVs, live `disco.db` out of root).
- Already DONE here: split-monoliths (engine.py 6446→1543), import-linter gate,
  arch-budget gate.

---

## §7 — Decisions needed from Dylan
1. **Slides architecture A/B/C** — run the C4 experiment to decide, or pick now?
   (Blocks all of Track C C1/C2 + §4 deck-patch.)
2. **Iterative-mode coherence approach** — how to keep a partially-rewritten report
   from feeling choppy (the unsolved design bit).
3. **License** — AGPL vs Apache.
4. **Image providers scope** — confirm `bundled | openai-images-compatible | comfyui`
   as the v1 adapter set.
5. **Benchmarks scope** — which metrics/datasets count as the launch gate.
6. Default driver = sticky last-pick — **DECIDED** (§1 P3).

## §8 — Recommended sequence
1. **Now:** §1 driver reliability (P1/P2/P4) + P3 sticky pick — small, high daily-use value.
2. **Unblock cheaply:** §2 C6 `artifact_mode` wiring · §5 F1 keep-searching · §5 RP-09
   acceptance · §3 FILE-DELIVERY (it gates three product features).
3. **Decide then build:** §7 slides A/B/C → C4 experiment → Track C C1–C3/C8 → §4 editor.
4. **Product features (post-FILE-DELIVERY):** iterative mode (start the coherence
   design) → image-gen tool (= C7) → DR→agent handoff → podcast.
5. **Launch gates in parallel:** benchmarks, 8 GB keyless + bundled-model swap,
   license, release-eng pack.
6. **Backlog (§6):** fold in opportunistically; address design/infra-blocked items
   when their blocker clears.
