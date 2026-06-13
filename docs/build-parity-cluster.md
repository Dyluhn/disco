# Build Parity Cluster — the first major cluster of work orders

> **Execution-grade expansion: `docs/workorders/`** — one self-contained, zero-ambiguity
> work order per BP item (decided mechanisms, code skeletons, UI-surface acceptance,
> anti-cheat rules). Hand exactly one `BP-*.md` file per task to the executing model;
> `workorders/README.md` carries the global rules and the dependency order.

**Goal (Dylan, 2026-06-09):** the Build surface at the usability and reliability level of
Manus. "It needs to work just like it does with Manus. None of the pain." Complex,
multi-step, dependency-installing builds must complete routinely — not just small static sites.

Every gap below is **verified against the codebase or live probes this session** (file:line
receipts inline). The Manus mechanism referenced throughout is the triple-sourced
process-as-durable-resource model (leaked tool schemas + container source + official blog;
corroborated by OpenHands-tmux and the E2B process API) — see `release-roadmap.md` §Phase 0.5.

---

## The precise gap list (what Manus has that we don't)

| # | Gap | Our current state (receipt) | Manus's state |
|---|-----|------------------------------|---------------|
| G1 | **Persistent, inspectable shell sessions** | Every shell call is fire-and-forget; long-running processes only via `setsid nohup` inside `run_server` (preview.py:177-183). No way to look at a running process's output. | `shell_exec(id, dir, cmd)` / `shell_view(id)` live scrollback anytime / `shell_wait` / `shell_write_to_process` (stdin) / `shell_kill_process` — processes are named resources that outlive tool calls. |
| G2 | **Single-owner serving** | Port 8000 has TWO owners: container PID-1 keepalive `while true; do python3 -m http.server 8000; sleep 1; done` (gvisor.py:50) races `run_server`'s `pkill -f 'http.server'` (preview.py:178). On app crash, keepalive silently recaptures the port. | No auto-serve, no supervisor. One process per named session; "Previous command not finished in this terminal… use another terminal." |
| G3 | **Truthful server status** | `preview_status` probes *bind only* (`_LISTEN_PROBE`, preview.py:31-34) → reports "serving" when the keepalive stub holds the port, not the agent's app. Agent visibility = 2 booleans + file count + a 20-line tail only at failed startup. | `shell_view` shows the actual process's actual output, anytime. Status is never inferred — it's observed. |
| G4 | **Sanctioned kill / ownership model** | Prompt prohibitions ("NEVER kill or pkill the server on port 8000", prompts.py:209-222) + security hard-deny on pkill patterns (analyzers.py:105-110). The agent is *forbidden* from managing processes it can't see → confusion spiral. | Kill is a first-class sanctioned verb. The agent owns its processes; nothing is hidden, so nothing needs prohibiting. |
| G5 | **Agent-driven browser verification** | No browser tool with feedback. `verify_app` exists but is server-side; the agent cannot *see* its app. | Browser tools return annotated screenshot + indexed elements (`index[:]<tag>text</tag>`) + extracted markdown + `browser_console_view`. Prompt rule: "For web services, must first test access locally via browser" before handoff. |
| G6 | **Vision feedback for the driver** | Verified live 2026-06-09: workstation 27B `/props` → `"modalities":{"vision":false}` — no mmproj loaded. No model in the loop can look at a screenshot. | Multimodal model sees every screenshot. |
| G7 | **Flat-latency code execution** | Pickle-runner CodeAct re-serializes the whole namespace every cell — O(N·K). Measured: 41KB→346KB over 43 calls; each call crept toward the 295s timeout (the v2 "pseudo-hang"). Unpicklables (sockets, handles, threads) silently dropped. `dill`, `jupyter_client`, `ipykernel` all absent from the venv. | Persistent kernel; state lives in RAM; per-cell latency flat over hundreds of cells. |
| G8 | **Loop survival on long builds** | Destructive 8000-char observation snips; no observation masking; KV-prefix instability; read-counter bandaids still in tree (slated rollback). | Stable append-only context, observation masking, files-as-memory. |
| G9 | **Multi-service builds** | `expose_port` rejects everything except PREVIEW_PORT 8000 (_container.py:250,256). A backend+frontend build can't expose both. | `deploy_expose_port(port)` — any port, explicit user-exposure step distinct from local serving. |
| G10 | **Dependency installs — verified, with posture** | Better than feared: Build grants `Capability.NETWORK` (runtime.py:510-512) → `egress_mode()` = **"open"** (_container.py:55-58), and the image ships node 22+npm+pip+git (deploy/sandbox/Dockerfile). But never live-verified inside gVisor (DNS gotchas documented in the egress-proxy investigation), and "open" raw egress is the weakest posture — the filtered allowlist proxy already exists. | Installs just work; egress curated. |
| G11 | **Resumable builds** | PAUSED-build resume is rough: `resume()` is Deep-Research-specific; Build resume is implicit via send-message; orphan-RUNNING reconciliation exists but full auto-suspend/resume (lifecycle G) incomplete. | Tasks survive disconnects/restarts transparently. |
| G12 | **Cockpit UX** | No Terminal tab; no screenshots in the feed; isolation tier not shown (BuildSurface.tsx:44 `[GAP]`). | Live terminal panes per session; screenshots inline in the task feed. |

---

## Work orders — 5 tracks

### Track 1 — See and control the environment (the S-series, Manus mechanism)
- **BP-0 — Vision decision** *(precedes BP-4's screenshot channel; BP-4's text channels don't wait on it)*
  Driver has no vision (verified). Three paths:
  (a) mmproj for the workstation 27B — only if a Qwen3.6-27B VL projector exists; must bench
  against MTP serving stability (history: even a -150mV undervolt crashed MTP) and KV headroom at 128K;
  (b) **recommended:** a `VISION` role in the router's capability enum → mini-PC vision service
  (already deployed) judges screenshots out-of-band; driver KV/MTP untouched;
  (c) OpenRouter fallback (key currently locked — needs PMX_SECRET_KEY).
- **BP-1 — S1: persistent named shell sessions.** `shell_exec(id, exec_dir, command)`,
  `shell_view(id)` (truncated live scrollback), `shell_wait(id, seconds)`,
  `shell_write_to_process(id, input, press_enter)`, `shell_kill_process(id)` (sanctioned).
  One foreground process per session; busy-session error mirrors Manus's wording. Deletes `run_server`.
- **BP-2 — S2: preview becomes a visible session.** Auto-serve runs as session `"preview"` the
  agent can view/restart/kill — NOT container PID-1. Kills the bind race and the false
  "serving" in one move. Keepalive becomes `sleep infinity` (podman.py already does this).
  `server_status` reads the session table: pid, owner command, uptime, last output lines.
- **BP-3 — S3: environment-contract prompt rewrite.** Replace prohibition-text (prompts.py:209-222)
  with an ownership contract: "these sessions are yours; view before acting; kill is fine."
  Retire the pkill hard-deny (analyzers.py:105-110) — it guards a race that no longer exists.
- **BP-4 — S4: real browser tool, Manus return shape.** Screenshot (annotated + clean) +
  indexed elements + extracted markdown + console errors. Text channels (console/elements/markdown)
  ship first and carry most QA value; the screenshot→vision channel lands when BP-0 resolves.
- **BP-5 — S5: the verify rule.** Web deliverable must be locally browser-verified (BP-4)
  before `finish`/serve — Manus's verbatim production rule, enforced as a gate not a suggestion.

### Track 2 — Loop survival (B-series substrate)
- **BP-6 — B1 observation masking + B5 KV discipline.** Drop old observation bodies outside a
  rolling window behind restorable path/hash refs; stable byte-prefix serialization.
- **BP-7 — Read-counter rollback.** Remove streak/nudge/jitter/force-commit (905ae60…e48c5bc);
  keep c430275 + 56526be. Only after BP-6 lands (cause before bandaid removal).
- **BP-8 — B8 persistent IPython kernel.** Delete the pickle runner. Kernel in-sandbox
  (process mode: `jupyter_client`; gvisor: Kernel Gateway HTTP/WS). Interrupt-before-kill fixes
  the timeout deadlock; file-rehydrate on restart. Adds `jupyter_client`+`ipykernel` deps.
  Acceptance: flat per-cell latency over 50+ cells on the real EE-Quest harness.

### Track 3 — Complex-build capability
- **BP-9 — Dependency installs: verify live, then curate.** (1) Live-verify `npm install` /
  `pip install` inside the gVisor box on VM-201 (open-egress DNS path never tested; the egress-proxy
  investigation found gVisor/Docker DNS gotchas). (2) Pre-warm the image with common toolchains
  (vite, next, uv) to cut install latency. (3) Move Build from "open" to **filtered** registry
  allowlist (registry.npmjs.org, pypi.org, files.pythonhosted.org, github.com, …) — the
  egress-proxy sidecar already exists and is live-verified; this is configuration, not construction.
- **BP-10 — Arbitrary port exposure.** Lift the `port != PREVIEW_PORT` rejection
  (_container.py:256) → `deploy_expose_port(port)` semantics; preview proxy routes by port.
  Unblocks backend+frontend builds.
- **BP-11 — File upload into the workspace.** User drops files (data, assets, briefs) that land
  in the sandbox workspace. Already a Phase-1 roadmap item; pulled forward because complex real
  builds almost always start from user-supplied material.

### Track 4 — Lifecycle
- **BP-12 — First-class Build resume.** Explicit Resume on a PAUSED/interrupted build; `resume()`
  generalized off Deep Research; resumed view shows real state, not placeholders.
- **BP-13 — Auto-suspend/auto-resume + orphan reconciliation (lifecycle G complete).** Tab-close/
  idle-TTL suspend with snapshot, transparent resume on reconnect, RUNNING-orphan sweep at startup.

### Track 5 — Cockpit UX
- **BP-14 — Terminal tab = live session views.** The UI renders `shell_view` streams per named
  session — the user watches exactly what the agent watches. (Direct consequence of BP-1/BP-2:
  one source of truth for both.)
- **BP-15 — Screenshots in the feed + isolation tier over the wire.** BP-4 screenshots render
  inline in the task feed; BuildSurface.tsx:44 `[GAP]` isolation badge wired to the real spec.

---

## Sequencing

```
BP-1 → BP-2 → BP-3        (S1–S3: the cockpit substrate — everything else stands on it)
  → BP-6 → BP-7            (mask + KV, then pull the bandaids)
  → BP-8                   (kernel)
  → BP-0 / BP-4 → BP-5     (vision decision; browser; verify gate)
  → BP-9 / BP-10 / BP-11   (capability: installs, ports, uploads)
  → BP-12 / BP-13          (lifecycle)
  → BP-14 / BP-15          (cockpit UX — BP-14 can start as soon as BP-1 lands)
```

## Acceptance gate — the marathon harness

One end-to-end run, on the local 27B, that today is impossible:

> A complex multi-service build — Python backend + Vite frontend + npm dependency installs +
> a user-uploaded data file — where mid-run the dev server is **deliberately crashed**. The
> agent must notice via `shell_view`/`server_status` (not guess), diagnose from real output,
> recover (including a legitimate `shell_kill_process` + restart), browser-verify the running
> app per BP-5, and finish with screenshot evidence. A second run is interrupted by an
> agent-server restart and **resumes** to completion.

Per project rules: real-sample harness (verbatim captures), live-UI Playwright/Firefox
screenshots as evidence, no synthetic fakes.

---

*Companion docs: `release-roadmap.md` (full 3-phase roadmap; this cluster = Phase 0.5 + Phase 0
plus pulled-forward capability items), `agent-architecture-rebuild-plan.md` (B-series evidence
grades), `architecture-rebuild-writeup.md` (how we got here).*
