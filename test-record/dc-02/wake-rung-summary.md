# DC-02 + DC-03 combined live rung — 2026-06-10

Conversation: `conv_9b54038195ab4db68224cfa44c557a73` ("cleanup demo" build:
scratch dir + index.html + forced `rm -rf scratch-demo` + browser verify).
Driven through the real UI (Firefox/Playwright on vite :5174), real local 27B
model, gVisor sandbox on VM-201.

## DC-03 — sandboxed HIGH-risk auto-approve (PASS)

- `rm -rf scratch-demo` scored HIGH ("recursive force delete") and ran
  **without any confirmation gate**: zero `WAITING_FOR_CONFIRMATION` status
  events in the log; run went straight to FINISHED in 18 iterations.
- The action event carries `meta.auto_approved = "sandboxed"` (exactly 1
  stamped action — the rm -rf; see `../dc-03/events-conv_9b540381….json`).
- The ActivityFeed renders the **"AUTO · SANDBOXED"** badge on that feed row,
  HIGH-risk red left border intact — `../dc-03/badge-auto-sandboxed-crop.png`
  (zoom), `../dc-03/badge-auto-sandboxed.png` (full page, preview pane shows
  the live app at the same time).

## DC-02 — suspend → wake (PASS, with one finding)

Previous rung FAILED here (503: `.pnpm-store` walk broke the snapshot, no
manifest ever written — see `preview-2-after-wake.png` for that failure).
After the archive.py fix (`ebda91c`):

1. FINISHED did **not** reap the sandbox; preview kept serving (200, container
   `pmx-sbx-sbx_95a092cd…` up).
2. Snapshot wrote `manifest.json` (4 files, 18,138 bytes — junk caches
   excluded by design; zero persistence-reminder events).
3. TTL-10 sweep suspended it (`suspended idle sandbox … idle_s=33`); container
   gone from VM-201.
4. A hostname preview hit (`http://9b540381-8000.localhost:8000/`) **woke the
   conversation**: fresh container `pmx-sbx-sbx_d853b070…` rehydrated from the
   snapshot, page served 200 — `rung2-preview-2-after-wake.png` shows the
   rehydrated app ("cleanup demo complete").
5. `idle_ttl_s` restored to 1800 in perpleximanus-config.json.

### Finding (filed, not blocking): first-hit wake race

The *first* preview hit immediately after suspend returned 503
("upstream connect error: All connection attempts failed") —
`rung2-preview-1-before-suspend.png` caught that page. The wake had created
the sandbox but the proxy connected before the rehydrated preview server
bound :8000. The second hit (seconds later) served 200. Wake works; the
first request after a suspend can lose a race. Candidate fix: short
connect-retry in host_proxy when the upstream was just woken (fold into a
later order — runtime.py/host_proxy is contested).
