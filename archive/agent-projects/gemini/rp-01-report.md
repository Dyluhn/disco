# RP-01 — Dashboard lite: status write-through + History chips

## Summary of Changes

Implemented status write-through and read-repair to surface conversation state in the History library without needing to open each conversation.

### Core (packages/core)
- **`packages/core/src/disco/core/store/base.py`**: Added `status: str | None = None` to `ConversationSummary`. (Flagged: deviation from manifest, necessary as this is where the model is defined).
- **`packages/core/src/disco/core/store/sqlite.py`**:
    - Updated `_store_one` to perform write-through updates to the `conversations` table when a `StatusEvent` is appended.
    - Updated `list_conversation_summaries` to include the `status` column and implement lazy read-repair (backfilling from event reconstruction if status is NULL).
- **`packages/core/src/disco/core/__init__.py`**: Exported `ConversationSummary` for easier access and testing. (Flagged: deviation from manifest).
- **`packages/core/tests/test_store_status.py`**: New unit tests for write-through, read-repair, and "write once" idempotency.

### App Server (packages/app-server)
- **`packages/app-server/src/disco/app_server/app.py`**: Updated `ConversationSummaryDTO` with `status` field and populated it in the `GET /api/conversations` route.
- **`packages/app-server/tests/test_app.py`**: Added `test_conversations_include_status` to verify the API correctly returns the cached status.

### Frontend (frontend)
- **`frontend/src/types/conversation.ts`**: Added `status` field to `ConversationSummary` interface.
- **`frontend/src/views/HistoryView.tsx`**: Added `StatusChip` component and integrated it into the conversation list.
- **`frontend/src/views/HistoryView.status.test.tsx`**: New unit tests for status chip rendering across all categories.

## Test Results

| Suite | Command | Result |
|---|---|---|
| Core | `uv run pytest packages/core/tests/test_store_status.py -q` | 4 passed |
| Server | `uv run pytest packages/app-server/tests/test_app.py -q` | 19 passed |
| Frontend | `npx vitest run src/views/HistoryView.status.test.tsx` | 2 passed |

Detailed logs are available in `test-record/rp-01/`.

## Deviations

1. **`packages/core/src/disco/core/store/base.py`**: Touched to add the `status` field to the `ConversationSummary` Pydantic model. This model is the bridge between the store and the DTOs; without this change, the store cannot return status values to callers.
2. **`packages/core/src/disco/core/__init__.py`**: Touched to export `ConversationSummary`. This followed the project's pattern of exporting storage models in the core package and simplified testing/DTO mapping.

No changes were made to `packages/agent-server/` as its `list_conversations` route returns bare IDs, not summaries, per the "if it returns bare ids, leave it alone" instruction.
