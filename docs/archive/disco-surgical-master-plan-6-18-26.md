# Disco — surgical master plan (2026-06-18)

The single execution driver for the four folded-in areas. The line-level surgical
specs live in the four sub-plan docs (each verified against current code, with
corrected file:line); THIS doc is the orchestration layer — cross-cutting facts,
the dependency graph, the unified build sequence, parallelization, decisions, and
the acceptance philosophy.

**Sub-plans (the surgical detail):**
- **A** — driver reliability + FILE-DELIVERY → `docs/surgical-A-driver-and-file-delivery.md`
- **B** — Track C slides/artifacts pipeline → `docs/surgical-B-trackc-slides.md`
- **C** — Track C in-browser editor / element-to-agent → `docs/surgical-C-trackc-editor.md`
- **D** — polish / search / attach / live → `docs/surgical-D-polish-search-attach-live.md`

Source roadmap: `docs/disco-consolidated-roadmap-6-18-26.md`. Conventions: 4 fitness
gates + `.venv/bin/python3 -m pytest -m "not integration"`; visual evidence mandatory
for UI; real-sample harnesses; no class>800/func>200 LOC; worktree-kit for parallel
agents touching the same package.

---

## §A — Cross-cutting facts (reconciled — read before sequencing)

1. **K1 is DONE, not a gate.** The elision-marker execution-guard is wired at
   `packages/core/src/disco/core/loop/observe.py:220-245` (covers run/confirm/verify/
   finish — every path funnels through `execute_and_observe`), with
   `test_k1_elision_guard.py`. Sub-plan B's "K1 PARTIAL" note is SUPERSEDED — verified
   wired. **Track C is NOT blocked by K1.**

2. **The one real gate for schema-bound slides work is the C4 verdict.** The deck
   schema must be frozen by the C4 experiment (`docs/slides-experiment-verdict.md`,
   not yet run) before: C1 authoring schema, C2 generator prompt, and §4's
   `deck_patch` tool / `DeckResolver` / `DeckEditor`. Everything else is
   decision-independent and can proceed now.

3. **`chart_svg.py` must relocate before the deck path can use it (layering).** It
   lives at `agent-server/chart_svg.py` (top layer); the deck renderer is in `tools`
   (lower) — `tools → agent_server` is an illegal import. **Move `chart_svg.py` →
   `packages/core/src/disco/core/brand/` (or `core/`)** so BOTH the PDF export and the
   deck/chart path import it downward. Do this as the first step of C8 (and update the
   existing `report_export.py` import). Re-run `lint-imports` + diagram gate.

4. **FILE-DELIVERY is a genuine prerequisite.** Sub-plan A confirmed Dylan's
   instinct: the per-file artifact route exists (`files.py:164`) but the Build
   deliverable card downloads a whole-project ZIP, the card is FINISHED-only/bottom-
   pinned (not inline in the feed), Research/DR render reports WITHOUT threading
   `cid` (so block downloads are dead), and DR has no file-handoff at all. FILE-
   DELIVERY (A/F1–F4) unblocks the DR→agent handoff, image-gen delivery, and the
   podcast. Build it early.

5. **`artifact_mode` (C6) is declared but consumed nowhere** (`routes/_common.py:100`;
   no `set_artifact_mode`/`artifact_scope`). Small, schema-independent wiring into
   `_compose_build_loop` (`runtime.py:1091`).

6. **Stale line refs corrected** throughout the sub-docs (e.g. `_compose_build_loop`
   :1091 not :1070; image-gen registration `builtin/__init__.py:102`; `set_model_override`
   → `runtime_settings.py:61`). Trust the sub-docs' verified lines over the older
   direction doc.

---

## §B — Unified work breakdown

| Area | Item | Size | Gate / dep | Sub-doc |
|------|------|------|-----------|---------|
| **A** | P4 router config-footgun warning | XS | — | A |
| **A** | P1 OpenRouter provider-pref injection | S-M | — | A |
| **A** | P2 classify + routing-retry (`LLMProviderUnavailable`) | M | P1 | A |
| **A** | P3 sticky last-picked model | S-M | — | A |
| **A** | FILE-DELIVERY F1–F4 (per-file card, inline feed, thread `cid`, DR handoff event) | M | — | A |
| **D** | F1 keep-searching (≤2 reformulate on no-answer) | S-M | — | D |
| **D** | RP-09 live audio acceptance (real Kokoro→mixer) | S | — | D |
| **D** | Attach files in initial box (Passages corpus, WS needs `cid`) | M | — | D |
| **D** | DR mid-run steer / inject sources (ENGINE) | M-H | — | D |
| **B** | C5 render/preview fixes (`deriveSrcDoc`→null, "0 B", `?inline=true` sandboxed) | S | — | B |
| **B** | C6 `artifact_mode` wiring | S | — | B |
| **B** | C7 image backends (ImageBackend seam: bundled/openai-compat/comfyui) | S-M | — | B |
| **B** | C4 slides experiment → freeze schema | (sched) | — | B |
| **B** | C8 relocate `chart_svg`→core + chart/table deck layouts | S-M | §A.3 | B |
| **B** | C3 native editable `.pptx` (python-pptx) + LibreOffice PDF + 16:9 HTML | M | python-pptx+soffice deps | B |
| **B** | C1 deck schema · C2 generator | M | **C4 verdict** | B |
| **C** | §4.1 selection overlay · 4.2 postMessage bridge | M | — (schema-indep) | C |
| **C** | §4.3 SourceResolver + tag pass · 4.4 source edit path · 4.6 app-builder reuse | M-H | W3/W4 tools (DONE) | C |
| **C** | §4.3 DeckResolver · 4.4 `deck_patch` · 4.5 React deck editor | M-H | **C4 verdict** + C3 + C5 | C |

---

## §C — Dependency graph (what blocks what)

```
K1 .......................... DONE (not a gate)
C4 experiment ──► freezes deck schema ──► C1, C2, deck_patch(4.4), DeckResolver(4.3), DeckEditor(4.5)
chart_svg → core (§A.3) ───► C8 deck charts
C5 (srcdoc/inline) ────────► §4.1 overlay (srcdoc deck must render)
C3 (pptx render, data-element-id) ─► DeckResolver / DeckEditor visual acceptance
FILE-DELIVERY (A F1–F4) ───► DR→agent handoff, image-gen delivery, podcast
attach-corpus (D) ─────────► DR inject-sources (D) reuses the upload corpus
W3/W4 source-edit tools .... DONE ──► §4.4 SourceResolver path (no new tool needed)
python-pptx + soffice deps ─► C3 (add to tools/pyproject.toml + sandbox Dockerfile)
```

Everything NOT downstream of "C4 verdict" is buildable immediately.

---

## §D — Unified build sequence (waves; parallelizable lanes)

Use the worktree-kit when two lanes touch the same package. Each item ships with its
gates green + (UI) Firefox visual evidence + (engine) a live acceptance.

**Wave 0 — cheap, high-daily-value (parallel, ~days):**
- A: P4 → P1 → P2 → live acceptance on `:free` (zero provider ERRORs) → P3 sticky pick.
- D: F1 keep-searching; RP-09 live audio acceptance.
- B: C5 render/preview fixes; C6 `artifact_mode` wiring.
These are disjoint packages (core-llm / retrieval / frontend+agent-server) → 3–4
lanes. Lane discipline: A is `core/llm` + settings + pill; F1 is `retrieval`; C5 is
frontend; C6 is agent-server.

**Wave 1 — FILE-DELIVERY + slides foundations (parallel):**
- A: FILE-DELIVERY F1–F4 (unblocks the product features; do before image-gen).
- B: C7 image backends; C8 part-1 = relocate `chart_svg`→core (§A.3); add python-pptx
  + soffice deps for C3.
- B: kick the **C4 experiment** (runs in the background; produces the verdict).
- C: §4.1 overlay + §4.2 bridge (schema-independent; needs C5 from Wave 0).

**Wave 2 — native decks + editor substrate (parallel):**
- B: C3 native editable `.pptx` + LibreOffice PDF + 16:9 HTML; C8 part-2 deck charts.
- C: §4.3 SourceResolver + §4.4 source-edit path + §4.6 app-builder reuse (uses the
  DONE W3/W4 tools).
- D: attach-files-in-initial-box (Passages corpus).

**Wave 3 — schema-bound (AFTER C4 verdict):**
- B: C1 deck schema + C2 generator (finalize field set per verdict).
- C: §4.3 DeckResolver + §4.4 `deck_patch` + §4.5 React deck editor.
- D: DR mid-run steer/inject (reuses attach corpus).

**Wave 4 — large separate effort:**
- D: noVNC live browser (P1–P5; D7 gVisor egress prereq).

---

## §E — Decisions needed (consolidated, blocking where noted)

1. **Slides architecture A/B/C** — *blocks Wave 3 (C1/C2/deck editor).* Recommend:
   run the C4 experiment (Wave 1) and let it decide empirically — it directly answers
   Dylan's "is a constrained schema even viable" concern. Approve running C4.
2. **Image-provider set** — confirm `bundled | openai-images-compatible | comfyui`
   as the C7 v1 adapters.
3. **FILE-DELIVERY UX** — inline feed download-card design (a small product/UX call;
   sub-plan A proposes a `FileDownload` feed card reusing the `SheetDownload` idiom).
4. **DR steer/inject scope** — steer-only first, or steer + inject-source together
   (the inject path is the heavier ENGINE change).
5. Driver default = sticky last-pick — **DECIDED**.
6. **noVNC** — confirm it's in-scope now or deferred (it's the one LARGE item; Dylan
   included "live" in the fold, so it's planned, but it's a separate Wave-4 effort).

---

## §F — Acceptance philosophy (per the standing rules)
- **Engine/loop changes** (A P1/P2, D F1/DR-steer): live acceptance with a real model
  + the regression-metric harness; exit-code-as-truth; the four gates.
- **UI changes** (A P3 pill, A FILE-DELIVERY, B C5/decks, C editor): mandatory Firefox
  screenshot of the feature working in the running app + SendUserFile; vitest +
  `typecheck:build`. For decks: the editable `.pptx` MUST open with real text boxes in
  PowerPoint/LibreOffice (NOT image-per-slide) — open it and screenshot.
- **Audio (D RP-09):** real Kokoro→mixer→playable file, both modes, varying section
  counts; deliver the file.
- **No false affordances; preserve byte-identical OFF-paths** (F1 default round=1, P1
  local-payload unchanged, recency/attach default-off).

---

## §G — Suggested first move
Wave 0 lane A (driver) + lane D-F1 + lane B-C5/C6 in parallel worktrees — all small,
all high-value, no decision needed, no cross-package collision. Approve **running the
C4 experiment** in parallel so Wave 3 isn't blocked when we get there.
