# Build Parity Cluster — Work Order Pack

This directory is the execution-grade expansion of `docs/build-parity-cluster.md`. Each
`BP-*.md` file is a **self-contained work order**: hand exactly one file to the executing
model per task. The orders encode *decisions*, not options — if an order says tmux, you
use tmux; you do not substitute a mechanism you prefer.

## Execution order (dependencies are real — do not reorder)

```
BP-01 shell sessions (tmux)          ← everything stands on this
BP-02 preview-as-session             ← needs BP-01
BP-03 environment contract           ← needs BP-02 (prompt describes the new world)
BP-06 observation masking + KV       ← independent of S-series; do after BP-03
BP-07 read-counter rollback          ← ONLY after BP-06 is merged and verified
BP-10 multi-port exposure            ← independent; before BP-08 (kernel port)
BP-08 persistent IPython kernel      ← needs BP-10 (container kernel port)
BP-04 real browser (in-sandbox)      ← needs BP-01 (daemon session); BP-10 helps
BP-05 browser-verify finish gate     ← needs BP-04
BP-00 vision judge role              ← needs BP-04 (screenshots exist)
BP-09 egress verification + prewarm  ← any time after BP-02
BP-11 file upload                    ← independent
BP-12 first-class build resume       ← independent
BP-13 idle suspend / reconnect resume← after BP-12
BP-14 terminal = live sessions       ← needs BP-01/BP-02
BP-15 screenshots in feed + tier wire← needs BP-04
BP-16 MARATHON GATE                  ← last; the cluster is DONE only when this passes
```

## Environment facts (do not rediscover, do not contradict)

- Repo root: `/var/home/dylan/projects/perpleximanus build` (the directory name contains
  a space — quote every path).
- uv workspace, Python ≥3.12. Packages: `packages/{core,tools,retrieval,agent-server,app-server}`.
- Frontend: `frontend/` — React 19, Vite 6, TS 5.6, Tailwind 4. Dev server `:5173`.
- agent-server binds `127.0.0.1:8000` (loopback ONLY — never expose to tailnet/0.0.0.0).
  app-server `:8800`.
- Driver model: Qwen3.6-27B at `http://192.168.1.231:18080/v1` (config.py `_QWEN`). It has
  NO vision (`/props` → `"modalities":{"vision":false}` — verified 2026-06-09).
- Sandbox backends selected by `PMX_SANDBOX`: `process` (host, dev default), `local`,
  `podman`, `gvisor` (remote Docker on VM-201, Tailscale SSH keyless, LAN 192.168.1.77 /
  tailnet 100.81.82.115). Image: `deploy/sandbox/Dockerfile` (`pmx-sandbox:base`).
- Test commands: `make unit`, `make harness`, `make e2e` (Playwright Firefox),
  `make replay`, `make canary`. Frontend unit: `cd frontend && npm run test`.
- NEVER auto-start vite. End every run by printing the dev URL `http://localhost:5173/`.

## Global rules — these override your instincts

1. **No workarounds, no hardcoded state, no synthetic green.** If you cannot complete a
   step as written, STOP and report exactly what blocked you. Do not improvise an
   alternative and present it as done. If asked "did you do a workaround?" answer
   truthfully. (Standing project rule; violations have been caught before.)
2. **UI-surface acceptance is mandatory and live.** Every order's acceptance section ends
   at the real UI: Playwright **Firefox** against the **live** backend (`VITE_AGENT_BASE`
   set — NOT fixture mode), screenshots saved to `test-record/screenshots/bp-XX/`, and the
   screenshots delivered to the user. A green vitest/pytest count is NOT evidence of
   anything user-visible.
3. **Real samples only in acceptance harnesses.** Unit tests may use fakes. Acceptance
   tests must exercise the real sandbox backend named in the order (usually `process`
   first, then `gvisor` on VM-201). No fixture-mode Playwright for acceptance.
4. **One work order = one commit** (plus a follow-up fix commit if review finds issues).
   Conventional title `feat(bp-01): …` / `fix(bp-03): …`. Trailer:
   `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
5. **Anchors are symbols, not line numbers.** Line numbers in orders are hints from
   2026-06-09; locate code by the quoted symbol/string. If a quoted anchor does not exist,
   STOP and report — do not guess at intent.
6. **No false affordances.** Any UI element you add that is not wired end-to-end must be
   visibly flagged (red) and called out in your report.
7. **Deletions are part of the spec.** When an order says delete a tool/file/pattern,
   delete it and fix every reference (grep for the name). Leaving dead code is a failure.
8. **Do not touch what the order doesn't name** unless a named change forces it; list any
   forced collateral edits in your report.

## Definition-of-done template (every order ends with this filled in)

```
[ ] All implementation steps completed as written (or STOP-reported)
[ ] All named deletions done; grep for old names returns no hits outside docs/
[ ] Unit tests added/updated and passing: <command + count>
[ ] Acceptance run on backend(s) named in the order, output pasted
[ ] Playwright Firefox spec(s) added and passing against LIVE backend
[ ] Screenshots in test-record/screenshots/bp-XX/ and sent to user
[ ] No workarounds; collateral edits listed; dev URL printed
```
