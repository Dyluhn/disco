# P10b — LIVE export-smoke proof (evidence)

**2026-06-30** · real MiniMax-M3 build (agent-server `:8000` → relay `:8080` → MiniMax-M3 **direct**).
Run `e1`, conversation `conv_7851c99d…`.

The export/download path, proven end-to-end on a real live model (not a fixture):

- **DeliverableEvent** detected in the event log: `artifact_kind="files"`, `path="export.html"`.
- `GET /conversations/{cid}/artifacts/export.html` → **HTTP 200, 6100 real bytes**.
- The downloaded bytes are real HTML (`<!doctype html>…<html lang="en">`) and contain the build
  marker token `P10B_EXPORT_LANDING_OK` (3×, in `<span title="Build marker">…</span>`) —
  **output-truth**, independently re-fetched and confirmed, not an empty 200.
- Provider ledger: **15 calls, all host `api.minimaxi.chat`, 0 OpenRouter, 0 post-terminal**.

The driver (`harness/product_build/export_smoke_run.py`) is **fail-closed**: no DeliverableEvent, a
404, 0 bytes, or bytes that aren't the real file → FAIL. `download_bytes` is the length of the
**actual GET**, not a fixture claim.

**Verdict: PASS.** `serve(kind="files")` → `DeliverableEvent` → `GET /artifacts/{path}` returns the
real served file bytes. Codex (gpt-5.5) evidence-gate: APPROVE.
