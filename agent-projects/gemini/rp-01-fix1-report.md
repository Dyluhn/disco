# RP-01 FIX 1 REPORT

## F1 — BLOCKER: missing schema migration
- **Change**: Added `ALTER TABLE conversations ADD COLUMN status TEXT` migration within a try/except block in `SqliteEventStore.__init__` in `packages/core/src/perpleximanus/core/store/sqlite.py`.
- **Verification**: Added `test_schema_migration_adds_column` to `packages/core/tests/test_store_status.py`. This test creates a legacy database without the `status` column, reopens it via the store, and verifies that both read-repair (list) and write-through work correctly.
- **Status**: Fixed.

## F2 — write-through must not half-apply
- **Change**: Modified `append` and `append_many` in `packages/core/src/perpleximanus/core/store/sqlite.py` to wrap the call(s) to `_store_one` in a `with self._conn:` context manager. This ensures that the event insertion and the status update are part of the same transaction and will be rolled back together if any part of the operation fails.
- **Verification**: Core and server tests pass, ensuring basic functionality is preserved with the new transaction management.
- **Status**: Fixed.

## F3 — read-repair N+1
- **Change**: Refactored `list_conversation_summaries` in `packages/core/src/perpleximanus/core/store/sqlite.py` to collect all necessary read-repairs in a list and execute them using a single `executemany` call and a single `commit()` at the end of the method.
- **Verification**: `test_read_repair_on_list` and `test_repair_writes_once` in `packages/core/tests/test_store_status.py` pass.
- **Status**: Fixed.

## F4 — unknown-status chip
- **Change**: Updated `StatusChip` in `frontend/src/views/HistoryView.tsx` to return a neutral (gray) chip using `text-text-muted` and `border-hairline` classes for any non-null status that doesn't match the known categories.
- **Verification**: Updated `frontend/src/views/HistoryView.status.test.tsx` to expect a rendered chip for the "IDLE" status while still expecting no chip for missing status.
- **Status**: Fixed.

## Evidence
All tests passed successfully:
- `test-record/rp-01/units-core.log`: 5 passed.
- `test-record/rp-01/units-server.log`: 18 passed.
- `test-record/rp-01/units-frontend.log`: 2 passed.
