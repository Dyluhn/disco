# RP-02 Report: Deep-report rich blocks

Tables now render correctly in both streaming answers and deep research reports.

## Changes

### Backend (retrieval)
- **File:** `packages/retrieval/src/perpleximanus/retrieval/streaming.py`
- **Change:** Implemented GFM table detection in `_to_blocks()`. It now identifies contiguous tables (header + divider + ≥1 data row) and emits them as `table` blocks.
- **Robustness:** Handles ragged rows by padding/truncating to header length. Malformed tables (missing divider or data rows) are left as prose to avoid breaking the layout.

### Frontend (web)
- **File:** `frontend/src/components/research/DeepReportView.tsx`
- **Change:** Replaced manual paragraph splitting and `CitedText` rendering with the shared `Markdown` component. This enables GFM table support in report sections and the executive summary.
- **Note on Citations:** As `Markdown.tsx` does not currently support `[[id]]` chip rendering, citations in deep reports now appear as raw `[[id]]` text. This was chosen to adhere to the "REUSE that component" and "Smallest diff" instructions while enabling table rendering.

## Verification Results

### Unit Tests (Retrieval)
- **File:** `packages/retrieval/tests/test_table_blocks.py`
- **Count:** 6 new tests (all passed).
- **Log:** `test-record/rp-02/units-retrieval.log`

### Unit Tests (Frontend)
- **File:** `frontend/src/components/research/DeepReportView.table.test.tsx`
- **Count:** 2 new tests (all passed).
- **Log:** `test-record/rp-02/units-frontend.log`

### Full Suite
- **Result:** All tests passed EXCEPT the known flake `ResearchSurface.test.tsx`.
- **Flake Details:** `ResearchSurface (integration + streaming reconcile) > goes empty → query → a finished, grounded, structured answer` failed with `Unable to find role="button" and name /How is k chosen/i`. This matches the pre-existing flake warning in the brief.

## Deviations
- **Citation Rendering:** As noted above, citations in `DeepReportView` now render as raw text because the mandated `Markdown` component does not yet support the citation chip UI. A future update to `Markdown.tsx` (outside the scope of this manifest) would restore the chips globally.
