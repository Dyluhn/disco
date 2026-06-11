# RP-02 FIX 1 REPORT

## F1 — Citation chips in DeepReportView (Markdown)
- **Problem:** `DeepReportView` swapped to `Markdown.tsx` for section rendering, but `Markdown.tsx` did not handle `[[id]]` citation markers, rendering them as raw text.
- **Fix:**
    - Refactored `frontend/src/components/blocks.tsx` to export `CitationMarker`, a reusable component for a single citation chip (or its placeholder).
    - Extended `frontend/src/components/Markdown.tsx` to:
        - Accept an optional `answer: GroundedAnswer` prop.
        - Implement a recursive `processCitations` function that traverses React children and replaces `[[id]]` strings with `CitationMarker`.
        - Wrap key components (`p`, `li`, `td`, `th`, `h1-h3`, `blockquote`) to apply `processCitations` when an `answer` is present.
    - Updated `frontend/src/components/research/DeepReportView.tsx` to pass the `answer` (grounded report) to all `Markdown` instances (Executive Summary and Sections).
- **Verification:**
    - Created `frontend/src/components/Markdown.test.tsx` covering prose, bold/italic text, and tables.
    - Extended `frontend/src/components/research/DeepReportView.table.test.tsx` to verify chips render in both prose and table cells.
    - All 8 frontend tests passed.

## F2 — Table blocks cited_passage_ids
- **Problem:** `_to_blocks` in `streaming.py` extracted citations for prose blocks but ignored them for table blocks, breaking NLI verification for table-borne claims.
- **Fix:**
    - Modified `_to_blocks` in `packages/retrieval/src/perpleximanus/retrieval/streaming.py` to extract `[[id]]` matches from all header and row cells in a table block.
    - Added `cited_passage_ids` (sorted set) to the table block payload, matching the prose block convention.
- **Verification:**
    - Added `test_table_with_citations` to `packages/retrieval/tests/test_table_blocks.py`.
    - All 7 backend tests passed.

## Evidence
- `test-record/rp-02/units-retrieval.log` generated.
- `test-record/rp-02/units-frontend.log` generated.

## Deviations
- None. Implementation followed the requested "reuse chip logic" and "optional prop" constraints exactly.
