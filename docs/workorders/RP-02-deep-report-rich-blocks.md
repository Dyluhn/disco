# RP-02 — Deep-report rich blocks: tables stop rendering as raw pipes

Parent plan: docs/next-fix-set-plan.md §2 RP-02 (locked). Recon verified
2026-06-10 (agent-projects/gemini/rp-wave1-recon-report.md items 5-8).

## Context — verified anchors

- `frontend/src/components/blocks.tsx:168-210` — a complete `table` renderer
  already exists for AnswerBlocks; the backend simply never emits the kind.
- `packages/retrieval/src/perpleximanus/retrieval/streaming.py:109-171` —
  `_to_blocks()` only emits prose, code, heading.
- `frontend/src/components/research/DeepReportView.tsx:150-155` — deep reports
  split markdown on `/\n\s*\n/` and render plain paragraphs, so GFM tables
  appear as raw `| a | b |` pipes.
- **Deps already present — add NOTHING to package.json:** `react-markdown`
  ^10.1.0 and `remark-gfm` ^4.0.1 are installed, and
  `frontend/src/components/Markdown.tsx` already wraps them. REUSE that
  component.

## The decided design (locked)

1. **Backend table detector (~40 lines):** in `_to_blocks()`, detect a
   contiguous GFM table (header row of `|`-separated cells + `|---|` divider +
   ≥1 data row) and emit the EXISTING `table` AnswerBlock kind (match the
   shape blocks.tsx:168-210 consumes — read the frontend type first:
   frontend/src/types/grounded.ts). Non-table content is untouched. Malformed
   tables (no divider, ragged rows) stay prose — never crash, never emit a
   broken table block.
2. **Deep view:** replace the blank-line splitter + paragraph rendering in
   `DeepReportView.tsx` with the existing `Markdown` component (which already
   has remark-gfm). Preserve the surrounding layout/styling (headings,
   citations UI, anything else the view does around the body). Smallest diff
   that makes GFM tables render.

## Acceptance ladder

1. Unit (retrieval): NEW `packages/retrieval/tests/test_table_blocks.py` —
   markdown with one table → one table block with correct header/rows; table
   sandwiched in prose → prose, table, prose; divider-less pipes → prose;
   ragged rows → prose; two adjacent tables → two blocks. Run ONLY
   `uv run pytest packages/retrieval -q`; tee to
   `test-record/rp-02/units-retrieval.log`.
2. Frontend unit: NEW colocated `DeepReportView.table.test.tsx` — markdown
   body containing a GFM table renders `<table>` with the right cells, not
   raw pipes. `cd frontend && npx vitest run src/components/research/DeepReportView.table.test.tsx`,
   tee to `test-record/rp-02/units-frontend.log`.
3. Full frontend suite still green: `npx vitest run` — tee tail to the same
   log. (Known pre-existing flake: ResearchSurface.test.tsx — if THAT one
   fails alone, note it and move on.)

Live behavioral run (a research run whose report contains a real table +
screenshots in both surfaces) is the REVIEWER's rung — do not start servers;
a harness may be live on :8000.

## Anti-scope

- NO new dependencies (both libs are already installed).
- No chart kind, no new AnswerBlock members (that is RP-03).
- No changes to blocks.tsx (the renderer is done) or grounded.ts.
- Do not restyle the deep report; only the body rendering path changes.

## Manifest (the ONLY files you may touch)

- packages/retrieval/src/perpleximanus/retrieval/streaming.py
- packages/retrieval/tests/test_table_blocks.py
- frontend/src/components/research/DeepReportView.tsx
- frontend/src/components/research/DeepReportView.table.test.tsx
- test-record/rp-02/units-retrieval.log
- test-record/rp-02/units-frontend.log
- agent-projects/gemini/rp-02-report.md

## Report

Write `agent-projects/gemini/rp-02-report.md`: what changed per file, test
counts, deviations flagged at top with justification.
