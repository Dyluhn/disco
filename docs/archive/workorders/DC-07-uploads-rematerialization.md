# DC-07 — Uploads rematerialization

Parent plan: test-record/marathon/DEFECTS.md "## DEFECT-7" (locked).
Ensure uploaded files survive sandbox recreation by storing them server-side and re-materializing them on resume.

## Work items

1. **Server-side Storage: `packages/agent-server/src/disco/agent_server/runtime.py`**
   - Implement a mechanism to store uploaded file bytes on the server's local disk in a `{PMX_DB}.uploads/<cid>/` sidecar directory (the established `{PMX_DB}.surfaces.json` B0 sidecar pattern — NEVER under test-record/, that is evidence space).
   - Update `Runtime` to manage these server-held uploads.

2. **Re-materialization: `packages/agent-server/src/disco/agent_server/runtime.py` (~line 1430)**
   - Update `_rehydrate_after_recreate(conversation_id)` to copy server-held uploads back into the fresh sandbox's `uploads/` directory.
   - This ensures the agent is not "data-orphaned" after a sandbox recreation.

3. **Upload Logic: `packages/agent-server/src/disco/agent_server/app.py` (~line 206)**
   - Update `upload_files` to write bytes to the server-side storage in addition to (or instead of, with write-through to) the sandbox.
   - Ensure quota checks (~line 58, 100MB) accounting for server-side storage.

4. **Resume Reality Block: `packages/core/src/disco/core/llm/prompts.py`**
   - Update the resume reality block (injected at session start) to list uploads as "lost-and-recoverable" when they are missing from the sandbox but present on the server.
   - (Or ideally, the re-materialization makes this transparent, but the reality block should still reflect the truth of the workspace).

## Standing rules (Wave-2)

- Never modify an existing test to make new code pass; if a test contradicts your change, STOP and flag.
- DC-05 meta-tool withholding and valve semantics are ratified design — changes to `_tools_for_step`/valve behavior are out of scope for all four.
- Run the FULL suite for evidence; a green run of only your own test files hid 17 broken tests in wave 1.
- Flag every manifest deviation at the top of your report with justification.
- Do NOT commit, do NOT git add, do NOT start dev servers on :5173/:5174.

## Manifest

- packages/agent-server/src/disco/agent_server/runtime.py
- packages/agent-server/src/disco/agent_server/app.py
- packages/core/src/disco/core/llm/prompts.py
- packages/agent-server/tests/test_upload.py
- test-record/dc-07/units-server.log
- test-record/dc-07/units-core.log
- agent-projects/gemini/dc-07-report.md

## Sequencing Constraint

- Overlaps with RP-06 in `app.py` and `runtime.py`. DC-07 should ideally be implemented after or in coordination with RP-06 if they touch the same regions.

## Anti-scope

- No changes to `SandboxSession.write_file` (keep it focused on the container).
- No changes to `_tools_for_step` or valve behavior (DC-05 ratified).

## Evidence

- `timeout 300 uv run pytest packages/agent-server -v` > test-record/dc-07/units-server.log
- `timeout 300 uv run pytest packages/core -v` > test-record/dc-07/units-core.log
- Unit: upload -> simulate sandbox recreation -> re-materialized bytes identical (in test_upload.py).
- Live reproduction (upload -> SIGTERM -> restart -> resume -> file present): REVIEWER-run rung.
- Report: `agent-projects/gemini/dc-07-report.md`
