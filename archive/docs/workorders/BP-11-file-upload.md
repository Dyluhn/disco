# BP-11 — File upload into the Build workspace

**Read `README.md` first. Independent (no S-series dependency).**

## Why

Complex real builds start from user material (data files, assets, briefs) — Manus and
every production peer accept uploads. We have download/export
(`GET /api/projects/{cid}/download`, `/manifest`) but **zero upload path** (verified:
no multipart/FormData anywhere in `frontend/src` beyond a fixture description).

## The decided design

Multipart upload to the agent-server (it owns the sandbox handle), files land in
`<workspace>/uploads/`, and the agent learns about them through the event log — an
environment `MessageEvent`, the same channel every other fact reaches it by.

### 1. Endpoint (`agent_server/app.py`)

```
POST /conversations/{cid}/files        (multipart/form-data, field name "files")
→ 200 {"saved": [{"name": "data.csv", "bytes": 18234}], "rejected": [{"name": "…", "reason": "…"}]}
```

Rules (exact):
- Limits: ≤ 25 MB per file, ≤ 20 files per request, ≤ 100 MB total per conversation
  (sum of `uploads/` — check before writing). Over-limit → that file goes in `rejected`,
  HTTP stays 200 unless ALL rejected (then 413).
- Filename sanitization: take `Path(raw_name).name` (kills traversal), NFC-normalize,
  strip leading dots, replace whitespace runs with `-`, reject empty results. Collision →
  suffix `-2`, `-3`.
- Write via `SandboxSession.write_file(f"uploads/{name}", data)` — through the sandbox
  boundary, never direct host fs (the workspace path differs per backend).
- After saving, append ONE environment MessageEvent (source=environment, the same
  mechanism the circuit breaker uses — find it in engine.py/runtime.py):
  `"User uploaded: uploads/data.csv (18,234 bytes), uploads/logo.png (4,120 bytes)"`.
  If the conversation is mid-run, that's fine — it enters context on the next view; if
  idle, it's there when the next message kicks the loop.
- Allowed in every conversation state except a terminal ERROR; uploading to a FINISHED
  build is allowed (user may want a follow-up iteration).

### 2. Frontend

- `frontend/src/api/agent.ts`: `uploadFiles(cid, files: File[])` using `FormData`.
- Composer (the message input area in `BuildSurface.tsx`): a paperclip button (file
  picker, `multiple`) + drag-and-drop onto the composer with a visible drop highlight.
  On success: toast `Uploaded N file(s) to uploads/` and invalidate the Files-tab query
  so `uploads/` appears. On rejection: red toast with the reason — no silent drops.
- Files tab (`ExecutionCanvas.tsx` FilesPane): no special casing — `uploads/` shows like
  any directory (verify the file listing includes it; fix if the lister filters dirs).

### 3. Prompt bullet (prompts.py, BP-03 section — append)

```
"  • Files the user uploads appear under uploads/ in your workspace and are "
"announced in the conversation. Read them with file_read before guessing at their "
"contents.\n"
```

## Acceptance

1. **Unit (python)**: sanitization table-test (traversal `../../etc/passwd` → `passwd`;
   dotfile; unicode; collision suffixing); limit enforcement; event text format.
   **Unit (frontend)**: `uploadFiles` posts FormData; composer renders drop state.
2. **Integration (process backend)**: POST two real files (a 1 KB csv + a 30 MB blob) →
   csv saved + announced, blob rejected with reason; `list_dir("uploads")` shows the csv.
3. **Behavioral (live driver)**: upload a small real CSV (take one from `test-record/`
   or generate from real data — not lorem ipsum), then prompt: "Build a page that
   renders the uploaded CSV as a table." Event log shows `file_read` of
   `uploads/….csv` and the finished page contains real cell values. Save →
   `test-record/bp-11/`.
4. **UI surface (live, Firefox)** — `bp-11-upload.spec.ts`: drive step 3 through the UI
   using Playwright's `setInputFiles`; assert the toast, the feed announcement line, the
   Files tab entry, and the Preview rendering the table. Screenshots: `upload-toast.png`,
   `preview-table.png` → `test-record/screenshots/bp-11/`, sent to user.

## Prohibitions

- No base64-in-JSON uploads; multipart only. No writing outside `uploads/`. No silent
  rejection (every rejected file surfaces in UI).
- Do not add upload to the Research surface here (separate, later work).
