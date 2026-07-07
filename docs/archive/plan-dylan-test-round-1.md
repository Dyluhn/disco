> **SUPERSEDED / HISTORICAL (as of 2026-07-07).** Test-round-1 fix + breadth plan; Wave-1 lanes are largely DONE (walkthrough-fix batch) but this doc carries zero done-marks.
> Current status of record: `docs/disco-project-state.md` (master), `docs/disco-status-and-remaining.md` (features + remaining), `sec-work-remaining/disco-security-state.md` (security). This file is kept for history and may contain stale claims. 🚫 **Fable 5 (Anthropic) models are off-limits to view per the project owner** — viewing them will cause a silent fallback to a weaker model; do not proceed unless you are Opus, Codex, or Sonnet.
> Wave-2 breadth items here are an UNRECONCILED backlog — not tracked in the current status docs.

# Dylan's test round 1 — bug fixes + breadth campaign (2026-07-05)

Execution: sequential codex lanes, Fable reviews + gates + live-proofs between.
Wave 1 = the ten bug classes from live testing. Wave 2 = the ratified breadth plan.

## Wave 1 — bugs

### Lane A (fast UX)
- A1 chip truncation: suggestion CONTENT is clamped server-side at 90 chars
  (sanitize_suggestion) so clicking inserts a cut-off question. Never clip
  content mid-thought: parse rejects overlong lines instead of clipping; the
  model prompt demands ≤90; display clamps visually only (CSS truncate+title).
- A2 New button: starts a new session on the CURRENT surface (not back to
  search). Pressing New also sets a per-mode "fresh" intent so the mode chips
  return to the splash instead of auto-resuming old runs; resume returns only
  via History/Projects or by starting work. Kill inside an old project then
  pressing a mode chip must NOT stack into the next old project (kill is
  terminal → no resume target).
- A3 DR in-run controls: replace add-angle / steer / add-source-snippet /
  add-source during a run with ONE working "notify me when done" bell
  (browser notification permission + in-app toast on FINISH). Plan revision
  affordance stays.

### Lane B (PDF export pack) — evidence: Screenshots/rowcolumnsc*.png, graphsc1.png,
### thinktag.png, Downloads/whats-the-current-state-of-local-llms-8.pdf
- B1 "Bounded by —" empty metadata row: omit when empty.
- B2 table rendering: column/header alignment broken (phantom first header,
  unlabeled columns), outsized row gaps. Rebuild the table serializer for the
  PDF: real header mapping, tight rows, zebra or hairline rules per theme.
- B3 charts: title clipped both sides, axis labels truncated, chart is an
  unthemed white rectangle. Render charts theme-aware: bar/line colors +
  background + fonts from the export theme tokens (sepia → warm palette),
  title wrapped not clipped, margins inside the frame.
- B4 blank-page gaps after follow-ups (orphan citation chips alone on pages,
  huge whitespace before big blocks): fix pagination — no forced breaks around
  charts/tables unless needed; keep-with-next for headings; no orphan chips.
- B5 follow-up answers expose <think> (thinktag.png): the RP-13 follow-up
  synthesis path misses strip_think_spans — strip at parse like every other
  consumer (disco.core.think; THINK-STRIP rule).

### Lane C (agent surface + workflows)
- C1 slides via agent surface is impossible: no workflow exposes
  slides_generate → model loops (live trace). Add builtin sealed workflow
  "Document & Deck Studio" (tools: slides_generate, sheet_generate,
  doc_set_section, doc_export, image_generate, file_read/write/edit/list,
  think, finish) with output contract on the produced artifact; seed it.
- C2 router-phase nudge contradiction: the PLANNING nudge demands submit_plan
  while router phase doesn't expose it (live trace: model reasons in circles
  about the contradiction). Router-aware nudges: in router phase point at
  enter_workflow/needs_input/draft_workflow, never submit_plan.
- C3 quiet mode not enforced: agent/build still emit full pre-planning chat
  despite the setting. Find the quiet-mode flag path and gate the pre-plan
  prose emission (verify end-to-end with the setting toggled).
- C4 vape-store wrap-up question (conv_7d7ae9035d704250b866ecc4fc92a94c):
  verify PASSES but fingerprint-stable after a cosmetic CSS change → breaker
  block-lands a question "how should we wrap this up?". When the last verify
  PASSED, the no-progress path must hint finish (extend the #37d discriminator
  to fire on verify-PASS + no-failing-work-remaining even when plan marks are
  incomplete-but-stale), not ask the user.

### Lane D (settings overhaul)
- D1 DISCO_SECRET_KEY must never be a user concern: on startup, if the env is
  absent, auto-generate a strong key and persist it (0600) under the app data
  dir; load transparently thereafter. Existing env still wins (and legacy
  PMX_ fallback stays).
- D2 provider keys → multi-model: one OpenRouter (or any provider) key should
  serve MANY models; "Add a model" currently binds one model per entry. Add
  provider-level key entry + model list management (browse OpenRouter catalog,
  add several, swap freely in the selector).
- D3 settings page reorg: coherent sections (Models & Providers / Agent
  behavior / Data sources / Encoders / Sandbox / Appearance), consistent
  typography (one heading scale), provider keys adjacent to the model
  catalogue, no orphaned controls between unrelated sections.

### Lane E (build quality defaults)
- E1 SVG-art-forward defaults: build packs/design directions instruct bespoke
  inline-SVG art wherever imagery helps; when image generation is configured,
  prefer image_generate for hero/photographic slots with SVG fallback on
  failure; minimal/clean/luxury directions lean SVG-first (live evidence: the
  vape build improved massively with SVGs).

## Wave 2 — breadth (ratified)
- F1 Bring-your-own-project: import folder/zip (upload) or git URL into a new
  build conversation; seeds the workspace + rehydrates; Projects lists it.
- F2 Spaces: finish persistent corpora (backend ~70% exists) + sidebar UI:
  create space, upload docs, ground research/DR/build against a chosen space.
- F3 Workflow catalog + MCP mounts + USER workflow authoring harness (the real
  intent of skill_authoring): guided create-a-workflow flow — name, card,
  tool picks, optional skill file, optional MCP mounts, params schema —
  validated, sealed, reviewed/approved in the Workflows panel, then runnable
  + schedulable like builtins. Reliability bar: same gates as builtins.
- F4 model-tier resilience: role-level fallback (local llama-server for
  SUMMARIZER/judge/rewriter class roles) when the primary errors/429s.
- F5 research source breadth: arXiv + Semantic Scholar + site-scoped search
  adapters behind the provider protocol; searxng/crawl4ai remain config-tier
  options (Dylan's endpoints: http://192.168.1.202:8888, http://192.168.1.237:11235).
