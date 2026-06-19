# Runthru v2 — Research Digest (input for the plan-builders)

Distilled conclusions from 5 research lanes + the 2-reviewer build-harness consensus.
Authoritative for the plan. Full sources in each lane's notes; build consensus in
`docs/build-harness-review-6-17-26.md §7`.

---

## R1 — Constrained schema vs free-form generation (settles #34)
**Verdict: LOOSE-schema + DEFERRED (deterministic) render, model-tier-aware.**
- "Format tax" is real: grammar-constraining open-weight models during generation costs
  3–30 pts quality (an 8B model hit 0% under rigid JSON); frontier/strong models barely feel it.
- Pure free-form (raw Marp) → unguarded layout overflow (today's failure). Rigid per-field
  schema → bland, can't adapt layout to content.
- Sweet spot (formally: "generate data + render separately" dominates "generate formatted
  output directly", 100% vs 0–60%): LLM writes CONTENT freely incl. **layout-intent hints** →
  parse into a LOOSE typed slide schema (`{type, title, body[], layout_hint, image_prompt?}`)
  via *instructed* JSON (NOT grammar-constrained) → deterministic template render owns
  overflow/fonts/16:9.
- Model tiers: **gpt-oss-120b** → one pass, loose schema, layout freedom. **Weak local** →
  free prose → extraction → render (more structure). LAYOUT is always deterministic regardless.
- Settle-it experiment: 15–20 prompts × {free-form / rigid / loose-hybrid} × {gpt-oss-120b,
  weakest local}; score overflow rate + LLM-judge content + blind human visual.

## R2 — Recency toggle (settles #1/#2)
**Build a `recency_window` toggle: "month" | "week" | None(default, zero change).** Two layers:
1. Time-filtered search — pass `timelimit` to ddgs (`m`/`w`) + `time_range` to SearXNG. ~60–80%
   honored; net-positive. Avoid `"day"` (near-zero results).
2. **Date injection into prompts** (highest ROI, provider-independent): inject current date +
   recency directive into `decompose` (biggest win), `synthesize_section`, `coherence_pass`,
   (`_gap_reason` = v2).
- last30days entity-resolver/engagement-scrape: NOT v1 (needs platform API keys; we're keyless).
  v2 stretch: `site:reddit.com`/`site:news.ycombinator.com` extra gather legs.
- v1 file hooks: `recency_window` on CreateConversationBody + DeepResearchService.set_recency;
  `time_filter` kwarg on SearchProvider protocol + DdgsSearchProvider + SearxngSearchProvider;
  `recency_window` on RetrievalRequest + thread DeepResearchRun → gather_for_subquestion →
  RetrievalRequest; recency prefix in decompose_query; recency line in synthesize_section +
  coherence_pass. UI toggle next to depth-tier. Off = byte-identical to today.

## R3 — OSS agent prior art (refines the BUILD-LOOP fix)
**Our "scale the snapshot" instinct is directionally right but the better architecture is
"structural map + on-demand full content + diff", not a bigger snapshot.** Confirmed by 4 agents:
- **Aider:** tree-sitter **repo-map** (signatures + PageRank, disk-cached, ~1k-token budget) shows
  what exists w/o full content; full file added only on demand, stays in context.
- **SWE-agent:** tiny **state blob** (`open_file`+`cwd`) every turn + **last-N observations**
  (N=5; older → "(N lines omitted)" but KEEP the model's own reasoning) + 100-line file window +
  $-budget kill. No semantic loop detector (abandoned — false positives) → relies on history elision.
- **OpenHands:** append-only event stream + 3-tier condensation; **StuckDetector** (5 patterns w/
  thresholds: identical action-obs ×4, action-error ×3, monologue ×3, ping-pong ×6, ctx-window) —
  EXTEND with a read-navigation pattern; **architectural finish-gate** (only `finish` after
  `pytest` exit 0) = the gold standard, and it IS "test once at the end".
- **Cline:** `[DUPLICATE FILE READ]` (read-aware dedup at presentation layer — exactly our F9,
  but ON for everyone) + "CRITICAL FILE STATE ALERT" on external file change.
- **Disco fixes vs prior art:** scale-snapshot → PARTIAL (do structural-map + on-demand instead);
  read-aware breaker → STRONGLY aligns (extend OpenHands patterns, file-specific to avoid the
  #5355 wait-loop false-positive); dedup-for-all → aligns (Cline); finish-gate → adopt OpenHands.
- **Novel for us:** a **diff-based workspace view** ("files changed since last step") — compact,
  fits a build agent's write-heavy actions; not seen in the researched frameworks.

## R4 — Branded export template (settles #11)
**One report MODEL → 3 renderers (md / PDF-weasyprint / DOCX-pandoc-reference-doc).**
- Replace `_markdown_to_html(serialize_markdown(...))` with `_build_pdf_html(report, follow_ups)`
  that builds HTML from the structured model (not the flattened string). **DROP `nl2br`** (the #1
  formatting defect).
- Brand = Disco's own design system on paper: **Fraunces** (display/cover), **Schibsted Grotesk**
  (UI/headings), **Newsreader** (reading) — all OFL; bundle TTFs under
  `agent-server/.../fonts/`; weasyprint FontConfiguration. Design-system color tokens; one
  restrained accent (cover rule, section numbers, source IDs). Cover block + running header/footer +
  numbered sections + follow-up Q&A on its own page + sources appendix. Full print-CSS skeleton +
  HTML structure provided in the R4 notes.
- DOCX: build a styled `reference.docx`, pass `--reference-doc` (+ `--toc`). MD path unchanged
  (byte-parity).
- Big reports: TOC when >10 sections; 2-col sources when >30; page-break hygiene.
- Files: `report_export.py` (`_build_pdf_html`, `serialize_pdf`, `serialize_docx`), new `fonts/`,
  new `reference.docx`.

## R5 — Presenton harvest (settles #27/slides architecture, pairs with R1)
**Recommendation: (C) build our own lean Python slides tool harvesting Presenton's PATTERN; (A)
optional 1–2 day sidecar demo first. NOT (B) extract — renderer is React+Chromium+closed binary.**
- Presenton pipeline = R1's verdict exactly: outline (structured JSON, slide count locked) → pick
  layout per slide → fill the chosen layout's JSON Schema (typed dict, NOT markdown) → assets →
  export. Its export is the weak part: Next.js→Chromium screenshot→**image-per-slide PPTX**
  ("editable" = boxes over images); the PPTX assembler is a **closed external Node binary
  (`presentation-export` v0.3.4), unknown license**.
- Option C (~1–2 wk, ~600 LOC): port 6–8 layout schemas (from Presenton's Zod →
  `servers/nextjs/app/presentation-templates/general/`) to Pydantic; implement the 5-step pipeline
  in Python; render NATIVE editable PPTX via **python-pptx (MIT, already in Presenton's deps)** +
  pdf via LibreOffice + html. Harvest **`services/image_generation_service.py`** directly (one
  class, provider-pluggable Pexels/Pixabay/DALL-E/Gemini/ComfyUI/OpenAI-compat, no DB coupling) →
  also delivers product-idea-#1 image-gen-as-tool. Data-reports = chart/metric/table layouts in the
  same pipeline (Chart.js) — comes for free.
- License: Apache-2.0 + all-permissive deps; the only flag is the closed `presentation-export`
  binary (irrelevant once we own the renderer via C). Footprint of A-sidecar ~2–3 GB (Chromium+Node).
- Files to study for C: `utils/llm_calls/generate_slide_content.py`,
  `generate_presentation_outlines.py`, the `presentation-templates/general/` schemas,
  `services/image_generation_service.py`, `models/presentation_*_model.py`,
  `api/v1/ppt/endpoints/presentation.py`.

---

## Build-loop consensus (from the 2 unbiased Opus reviews — `build-harness-review-6-17-26.md §7`)
Dominant: snapshot can't stably represent a 13-file project (8-file cap; per-file 6000 cap +
file_read 7000 budget both < windows.js 8047 → never "fully seen"; obs masking commands re-reads)
→ harness *instructs* re-reads → 82 reads. Every brake blind (stuck-equality includes `thought`;
WALK-19 only edits; F9 gated to assist). Contributing: browser can't click the dock
(`[data-pmx-index]` never set) → 30s timeouts; no vision yet prompt demands "see it render"; C18
`file_exists` resolves None `workspace_path` → false "missing". Separate: preview serves workspace
root but app in `macos-clone/` subdir → "text, no design" + post-teardown 503.
