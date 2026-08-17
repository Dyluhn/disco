# RP-05b re-approval diff — cross-half design decision (orchestrator, 2026-06-11)

**Status:** RATIFICATION-PENDING (decided under the AFK autonomy grant; flagged for
Dylan's review). Drives the rp-05b rework after Fable rejected the UI half.

## Why this note exists
Fable's rp-05b-ui review (REJECT) found the re-approval diff is a **false
affordance**: the banner renders `oldHash == newHash` (same value), Confirm
re-submits the OLD hash (a no-op), and the Re-approve button is shown whenever a
hash exists rather than on an actual mismatch. The poisoning defense's human
checkpoint — review what changed, then accept the new tool set — cannot function.

Root cause (traced through the code, not guessable from the UI):
- app-server ↔ agent-server are decoupled via a **shared ConfigStore** (sqlite/
  JSON) + a frontend→agent-server **WebSocket**. The app-server is a thin config
  surface; it does not run agent loops. (`app_server/__init__.py`.)
- The **live/new** description hash is computed by the agent-server pool at
  `McpPool.start()` (`runtime.py:1315`) and held **in-memory** in
  `_mcp_approval_pending` (`runtime.py:351`), exposed via py's REST
  `GET /api/mcp/servers` (`agent_server/app.py:174`) and WS `mcp_approval_required`
  frames. py's brief told it to "just expose" this — it did.
- The UI worker never wired the **app-server** to consume py's route; it mocked
  the endpoints with `vi.mock("@/api/config")`, so the integration seam — exactly
  where the missing data lives — was hidden by the worker's own mock.
- The **old** tool list is unrecoverable: rung A's `mcp_approvals` schema persists
  only `description_hash` (migrations.py:13-17), and a SHA-256 is one-way. The
  poisoning drill is **between sessions** (workorder line 455), so at detection
  the old descriptions are already gone.

## Decision
Implement the **§5-complete, security-correct** design — a real old-vs-new tool
**list** diff, mismatch-gated, Confirm advancing state. A hash-only / fingerprint-
only gate is rejected: it tells the operator *that* something changed but not
*what*, defeating the review checkpoint (and Fable already rejected the
hashes-not-tool-lists shape). This requires a small, surgical rung-A schema
completion, justified because the workorder's own "snapshot" concept (line 161,
"the pool replaces the snapshot only on explicit re-approval") implies the
approved tool set — not just its hash — is the unit of approval.

### The contract (what each layer owns)
1. **Schema (rung-A `tools/mcp/migrations.py`, orchestrator-owned completion):**
   add `tool_descriptions TEXT` (JSON) to `mcp_approvals`. `create_mcp_approval`
   persists the approved descriptions at approval time (they ARE the thing being
   approved, and are live-available then). `get_mcp_approval`/`list_mcp_approvals`
   return them. Pre-release: no data migration needed; widen the CREATE + helpers.
2. **py (agent-server `runtime.py` + `app.py`):** on mismatch, retain the **new**
   live descriptions; the `GET /api/mcp/servers` projection + the
   `mcp_approval_required` WS frame carry `approval_required`, `description_hash`
   (new), `old_description_hash` (old), `new_tools` (live descriptions), and
   `old_tools` (the stored snapshot via `get_mcp_approval`).
3. **app-server (`app.py` + `config_state.py`):** `GET /api/mcp` performs a
   **server-side** fetch of py's `GET /api/mcp/servers` (both ports internal; the
   frontend stays app-server-only; :8901 is never exposed), merges the live
   approval-pending fields into `McpConnectionDTO`. The approve endpoint records
   the new approved snapshot (descriptions + hash) via `create_mcp_approval`. A
   real diff is available (old_tools vs new_tools), replacing the dead
   `mcp_approval_diff`.
4. **frontend (`McpSection.tsx` + api/hooks/types):** the Re-approve banner is
   gated on real `approval_required` (mismatch), the diff view shows **old vs new
   tool lists** (not identical hashes), Confirm submits the **new** hash → approve
   persists the new snapshot. Keep the SHA-256 fingerprint for paranoid mode.
   Tests assert **distinct** old-vs-new content end-to-end; use MSW against the
   real endpoint contract (or explicitly flag any module-mock seam).

### Deviation flagged for ratification
- **Rung-A schema touch.** `mcp_approvals` gains `tool_descriptions`. This reaches
  outside both rung-B half-manifests into rung-A-committed code. Justified as the
  completion the §5 diff requires; called out here so it is not silent.
- The workorder's internal tension (event carries hashes per line 262; §5 wants
  tool lists per line 408) is resolved in favor of **§5** (tool lists), because the
  review-what-changed checkpoint is the load-bearing security property.

## Sequencing
py-side (schema + projection, defines the contract) lands and is Fable-verified
first; the UI consumption (app-server proxy + frontend diff) reworks against the
now-real contract. Both then feed the §6 5-drill live acceptance (the orchestrator's
gate), where drill 4 (poisoning → banner → refuse) finally exercises the whole path.
