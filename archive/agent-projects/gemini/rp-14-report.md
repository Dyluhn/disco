# RP-14 — Usability Debt Sweep Report

## Summary
Completed 4/4 items in the usability debt sweep. One item (Isolation hardcode) was skipped as per the "already fixed" note in the brief.

## Deviations
- **Item 4 (Cost Meter)**: Confirmed that token usage is NOT currently present in the backend `ActionEvent` stream (it is only recorded in tracing spans). However, implemented the frontend infrastructure to calculate and display cost assuming future availability in `meta.usage`. Currently shows "≥$0.00" (or "$0.00" if no paid models are used) as an honest lower bound when data is missing, as per the brief's "undercount is honest" rule.

## Itemized Work

### 1. Theme Persistence
- **Files**: `frontend/src/lib/useTheme.ts`, `frontend/index.html`
- **Changes**: 
    - `useTheme.ts` now persists theme choices to `localStorage` under the key `pmx-theme`.
    - Added a small blocking script in `index.html` head to apply the theme from `localStorage` or system preference before first paint, preventing theme flashing.
- **Tests**: Created `frontend/src/lib/useTheme.test.ts`. 2/2 passed.

### 2. Cmd+K / Ctrl+K Command Palette
- **Files**: `frontend/src/components/CommandPalette.tsx`, `frontend/src/shell/Shell.tsx`
- **Changes**:
    - Built a hand-rolled command palette using `@radix-ui/react-dialog`.
    - Supports navigation (New build, History, Projects, Settings) and Theme Toggle.
    - Implemented substring filtering and full keyboard navigation (Arrow keys, Enter, Esc).
    - Mounted in `Shell.tsx` to be globally available.
- **Tests**: Created `frontend/src/components/CommandPalette.test.tsx`. 3/3 passed.

### 3. History Search
- **Files**: `frontend/src/views/HistoryView.tsx`, `frontend/src/views/history.test.tsx`
- **Changes**:
    - Added a 150ms debounce to the existing history search input to improve responsiveness.
    - Updated the list filtering to use the debounced query.
- **Tests**: Updated `frontend/src/views/history.test.tsx` to wait for the debounce. 4/4 passed.

### 4. Cost Meter
- **Files**: `frontend/src/lib/cost.ts`, `frontend/src/types/models.ts`, `frontend/src/components/build/AgentStatusBar.tsx`
- **Changes**:
    - Added `TokenUsage` interface to `models.ts`.
    - Added `calculateUsageCost` and `formatCost` utilities to `cost.ts`.
    - Updated `AgentStatusBar.tsx` to accept `events` and `modelId` and display a cumulative session cost.
    - Implemented "≥" prefix logic for missing usage data.
- **Tests**: Created `frontend/src/lib/cost.test.ts` (4/4 passed) and `frontend/src/components/build/AgentStatusBar.cost.test.tsx` (3/3 passed).

## Test Results
Total frontend unit tests run: 16
Passed: 16
Failed: 0
Log: `test-record/rp-14/units-frontend.log`
