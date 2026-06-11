# RP-06 — Replay + share links

Parent plan: docs/next-fix-set-plan.md §2 RP-06 (locked).
Implementation of conversation replay in the UI and static-bundle sharing.

## Work items

1. **Replay Hook: `frontend/src/lib/buildTrace.ts` (~line 299)**
   - Implement `useReplay` hook to allow stepping through events.
   - buildTrace selectors are already pure functions of `AgentEvent[]`; leverage this.
   - Add a scrubber UI component to the Build view.

2. **Share Export: `packages/agent-server/src/perpleximanus/agent_server/runtime.py`**
   - Implement `share_export(cid)` to produce a scrubbed JSON bundle.
   - Bundle should include events and necessary metadata for static replay. The bundle IS the future harness cassette format (RP-00 locked synergy) — keep it a pure, versioned projection.

3. **Share API: `packages/agent-server/src/perpleximanus/agent_server/app.py`**
   - Implement `POST /api/conversations/{cid}/share` to trigger export.
   - Implement `GET /share/<id>` to serve the static viewer + bundle.
   - Implement `share_tokens` table (base62 random, revocable) for link management.

4. **Redaction: `packages/agent-server/src/perpleximanus/agent_server/redaction.py` (New File)**
   - Implement export-time redaction using ordered regex.
   - Target secrets like `sk_live_...`, generic `KEY=value`, shell/env outputs.
   - Ensure streamed deltas are also scrubbed if applicable.

5. **Static Viewer: `frontend/src/views/ShareView.tsx` (New File)**
   - Create a read-only, self-contained viewer for shared link bundles.
   - Side-step live WebSocket dependencies; use the static bundle.

## Standing rules (Wave-2)

- Never modify an existing test to make new code pass; if a test contradicts your change, STOP and flag.
- DC-05 meta-tool withholding and valve semantics are ratified design — changes to `_tools_for_step`/valve behavior are out of scope for all four.
- Run the FULL suite for evidence; a green run of only your own test files hid 17 broken tests in wave 1.
- Flag every manifest deviation at the top of your report with justification.
- Do NOT commit, do NOT git add, do NOT start dev servers on :5173/:5174.

## Manifest

- frontend/src/lib/buildTrace.ts
- frontend/src/api/agent.ts
- frontend/src/views/ShareView.tsx
- packages/agent-server/src/perpleximanus/agent_server/app.py
- packages/agent-server/src/perpleximanus/agent_server/runtime.py
- packages/agent-server/src/perpleximanus/agent_server/redaction.py
- packages/agent-server/tests/test_share.py
- packages/agent-server/tests/test_redaction.py
- frontend/src/lib/useReplay.test.ts
- test-record/rp-06/units-server.log
- test-record/rp-06/units-frontend.log
- agent-projects/gemini/rp-06-report.md

## Sequencing Constraint

- Overlaps with DC-07 in `app.py` and `runtime.py`. DC-07 should ideally be implemented after or in coordination with RP-06 if they touch the same regions.

## Anti-scope

- No live WebSocket door for shares (read-only bundle only).
- No replay-into-agent fork (v1 is read-only).

## Evidence

- `cd frontend && timeout 300 npx vitest run` > test-record/rp-06/units-frontend.log
- `timeout 300 uv run pytest packages/agent-server -v` > test-record/rp-06/units-server.log
- Scrubber unit suite built from verbatim captured secrets-bearing events.
- Shared link in a clean browser profile + revocation: REVIEWER-run rungs.
- Report: `agent-projects/gemini/rp-06-report.md`
