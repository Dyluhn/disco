# Universal-Readiness Plan

**Goal (the north star):** anyone can download the app, paste an OpenRouter key,
and do *everything* — research, deep research, and building — paying their own way.
Dylan's local LLMs/services are a **supported option, never a requirement**. The
sandbox is a garden, not a walled garden.

This plan addresses every item raised, grounded in the actual code (file:line),
with the fix split across **backend / frontend / prompt** and a recommended order.

---

## 0. Dependency map (where we stand)

| Dependency | Job | Default today | Bundled/local? | UI swap? | Universal-ready? |
|---|---|---|---|---|---|
| LLM (all generative roles) | brain + answer/rewrite/summarize | Qwen (LAN) | — | ✅ matrix + pick | ✅ **yes** (pick drives all generative) |
| Embeddings | retrieval vectors | ✅ fastembed bundled | ✅ | ✅ encoder toggle | ✅ yes |
| Rerank | passage ordering | ✅ bundled | ✅ | ✅ | ✅ yes |
| NLI | citation verify | ✅ bundled | ✅ | ✅ | ✅ yes |
| Sandbox | build execution | process/gVisor/local/podman | ✅ local container | ✅ sandbox section | ✅ yes |
| **Search** | web discovery | SearXNG, hardcoded `192.168.1.202` (`live.py:384`) | ❌ | ❌ | ❌ **NO** |
| **Extraction** | URL → clean content | Crawl4AI, hardcoded `192.168.1.237` (`live.py:385`) | ❌ | ❌ | ❌ **NO** |

**Two dependencies stand between us and "download + key works": Search and
Extraction.** Both are web-data services (can't be fully offline) but each needs a
BYO-key paid option, a degraded local fallback, and a Settings surface.

---

## A. Model routing (backend done — UI notification TODO)

- **A1.** Model pick drives all generative work in **regular search AND deep
  research, every depth.** Verified: `/ws/research` → `research_stream(model_override)`
  → `_router_now(pick=…)` (`runtime.py:590`); deep research `leaderId` → same `pick`
  path (`runtime.py:797`). `pick` reassigns *every* generative role (driver,
  answerer, rewriter, summarizer). ✅ Backend done.
- **A2 (NEW — UI honesty).** When a user picks a **paid** model on the chat-bubble
  override pill, show a **transient toast**: e.g. *"Now using {model} for all
  generative work (answering, rewriting, summarizing) — this is a paid model."* It
  appears briefly and dismisses itself. Only fires for paid models (`free:false` in
  the catalogue); free/local picks stay silent. So nobody runs up a bill without
  knowing the pick is global, not just "the answer." Frontend-only (the pill already
  knows `free`); add/reuse a toast primitive.

---

## B. Universal data providers (Search + Extraction)

### B0 — First-run defaults + PERSISTENCE (non-ephemeral, you asked)
- **First-run rule:** anything that needs no credential (bundled search `ddgs`,
  bundled local extraction, bundled encoders) is the **automatic default on a fresh
  install** — the app works the instant it's downloaded, zero config. A user only
  touches Settings to *upgrade* to a keyed/self-hosted provider.
- **Persisted, NOT ephemeral.** All provider choices + keys go in the **shared
  ConfigStore** (the same persisted `RouterConfig` the encoder/sandbox settings use),
  so a choice survives restarts. **Flag of current state:** today the persisted
  configs (sandbox, encoders, assignments, projects) ARE durable; the one **ephemeral**
  thing is the per-conversation **`model_override`** (`runtime.py:191`, an in-memory
  dict — lost on restart). That should also persist (per-conversation, in the event
  log or a small table) so a resumed conversation keeps its picked model. I will NOT
  build the new provider settings as ephemeral — they go straight into ConfigStore.

### B1 — Extraction: pluggable (Crawl4AI ↔ Firecrawl ↔ local fallback)
- **Now:** only `Crawl4aiExtractionProvider` (`live.py:128`), hardwired URL, no UI.
- **Fix (backend):** an `extraction` config (provider = `crawl4ai | firecrawl |
  local`, + base_url/key). Add:
  - `FirecrawlExtractionProvider` (paid `/v1/scrape`, BYO key in secrets).
  - `LocalExtractionProvider` — stripped-down, **in-process**: `httpx` GET →
    readability-style main-content extraction → **markdown** (a small,
    dependency-light HTML→markdown; candidates: `trafilatura` or `readability-lxml`
    + `markdownify`). No external service. This is the "garden works offline" piece.
- **Fix (frontend):** an Extraction section in Settings mirroring the Encoders one,
  with provider choice + contextual fields (key/URL) that appear per provider.

### B2 — Search: pluggable, with a BUNDLED free option (a ≠ b ≠ c)
- **Same gap as B1.** `SearxngSearchProvider` (`live.py:41`) is the only one, hardwired.
- **THREE options, not two** (the correction): (a) self-host a container [SearXNG],
  (b) pay for a service [Tavily/Brave/Serper, BYO key], **(c) a lesser-but-usable
  BUNDLED engine that ships with the app** — no key, no container. This (c) is the
  whole point: download → it just works.
- **Bundled candidate:** `ddgs` (the maintained DuckDuckGo-scraping lib, formerly
  `duckduckgo_search`) — no key, in-process, moderate rate limits that are fine for
  personal use. Wrap it as a `SearchProvider`. Degrades on rate-limit rather than
  crashing. **This is the default for a fresh user**; (a)/(b) are upgrades.
- Same Settings surface as extraction (provider choice + contextual key/URL fields).

### B3 — Markdown conversion + context hygiene (you flagged this)
- **Root cause found:** the **Build agent's** `search`/`extract` tools dump
  `content=str(results)` / `str(content)` (`builtin/retrieval.py:38,60`) — a raw
  Python repr, not markdown. That pollutes the agent's context. (The *research*
  path already gets `fit_markdown` via Crawl4AI — fine there.)
- **Fix (backend):** the tools return **clean markdown** (search → a compact ranked
  list of `title · url · 1-line snippet`; extract → the doc's markdown), with a
  length budget + the existing 8 000-char Observation snip so a big page can't blow
  context. The local extractor (B1) emits markdown natively, so this composes.

---

## C. Encoder Settings UX (fix the bad UI I just shipped)

- **C1.** The bundled/remote toggle has no way to configure the remote endpoints,
  and you can't tell what's active. **Fix (frontend only — backend already has the
  fields via `_DEFAULTS`/env):** when **Remote** is selected, reveal the three
  endpoint fields (reranker / embedder / NLI URLs) inline under the tab; hide them
  on **Bundled**. Show the active mode unambiguously. (Backend: persist those URLs
  in config like the encoder mode, so the fields are real, not env-only.)

---

## D. Build surface UX

### D1 — Projects: Resume, don't auto-spin
- **Now:** opening a build re-subscribes and the loop resumes (spins the GPU) with
  no explicit consent. (`/ws/conversations/{id}` replays history; the run resumes
  via `_run_with_persistence` — need to gate the auto-resume on open.)
- **Fix:** opening a project is **read-only** (history view); an explicit **Resume**
  button sends the frame that starts `loop.run()`. Clicking in never spins the GPU.
  Backend: don't auto-run on connect; only on an explicit resume frame.

### D2 — Circuit breaker: recommend + bypass (FULL SPEC)
- **Root cause:** at 4 failures (`engine.py:1184`) it emits only a plain MessageEvent
  + `AWAITING_USER_DECISION` — no `AlternativesEvent`, so the UI has no buttons and no
  recommendations. Dead end.
- **The mechanism (exact):**
  1. **Trigger** unchanged: `_count_recent_failures >= threshold` (keep 4; make it
     config). Streak resets on a USER message or a successful Observation (existing).
  2. **Recovery-proposal call (NEW):** before halting, the loop makes ONE structured
     LLM call to the driver: *"You've hit N failures: [last N errors, full]. (1)
     Diagnose in 1–2 sentences what's actually blocking you. (2) Propose 2–3 concrete
     next steps, each as a specific tool action."* Forced to a JSON schema →
     `{diagnosis: str, options: [{title, description, tool_name, arguments}]}`. Reuses
     the structured-output path; it's the agent's OWN context, so the recommendations
     are real, not generic.
  3. **Emit `AlternativesEvent`** built from that: `summary = diagnosis`; `options` =
     the proposed actions (each already `{id,title,description,tool_name,arguments}` —
     the existing shape, so `pick_alternative` runs them verbatim) **plus two
     synthetic options appended:**
     - **`__continue__`** — "Continue anyway (let the agent keep trying)" — picking it
       **resets the failure streak** and resumes the loop with the agent choosing its
       own next step (no tool runs). New special-case in `pick_alternative`/the loop:
       emit a streak-reset marker (an ENVIRONMENT message the agent sees: "User chose
       continue; keep going") and resume.
     - **`__steer__`** — "I'll guide it myself" — drops focus into the steer input
       (the existing escape; no backend action).
  4. **No silent loop:** if the recovery-proposal call itself fails, fall back to the
     current plain-message behavior (never wedge).
- **Frontend (`AlternativesGate`):** render `summary` as the **diagnosis banner**
  (plain language, what's wrong); the proposed actions as the existing option cards;
  the **Continue anyway** option styled DISTINCTLY (it's a bypass, not a recommended
  fix — secondary/outline button); keep the steer escape. So the user sees: *what
  broke → here are 2–3 fixes → or let it keep going → or I'll steer.*
- **Why specific:** the failure mode you're guarding against is "generic, useless
  recommendations." The fix is that the proposals come from a **forced structured
  call over the agent's real failure context**, not a canned list — and each is a
  runnable tool action, not advice.

---

## E. The loop / the "wall" (the stuck build) — CONFIRMED LIVE

**Root cause confirmed against the running session (not theory):** the build runs on
the `local` (podman) backend; the container auto-serves the workspace on port 8000
via its keepalive. The transcript shows the agent ran `pkill -f http.server` and
`pkill -9 -f 'python3.*http.server'` — **it killed the container's own preview
server** — then started its own with `&`, which the sandbox reaped (each `shell`
call is a separate exec). Live check: a build container (`3584ed…`) has **port 8000
DEAD** while others are OPEN. Dead 8000 → the proxy returns exactly **"preview
upstream unreachable" (502)** (`app.py:144`). So the preview is genuinely down; the
agent then loops trying to revive a server the sandbox won't keep alive.

The site "rendered after the first revision, then went unreachable" because the
first revision used the live auto-serve; a later step killed it.

### E1 — Background-process persistence (the real "wall", backend)
- **Root cause:** the `shell` tool runs each command as a separate `exec_run`; a
  `cmd &` backgrounds *inside that exec* and is reaped when it returns. So the agent
  **cannot keep a dev server alive** across tool calls. But the workspace IS already
  auto-served on `PREVIEW_PORT=8000` by the container's keepalive
  (`_container.py:25`), and a detached exec persists (proven by the egress proxy's
  `exec_run(detach=True)`).
- **Fix:** a first-class way to run a persistent server — either a `run_server`/
  `serve`-style tool that starts the process **detached** (survives across calls) on
  `PREVIEW_PORT`, or document that static output needs **no** server (auto-served).

### E2 — System prompt (you asked me to weigh this — yes, it's half the fix)
- The driver prompt says nothing about how to preview/serve in THIS sandbox, so the
  model invents the wrong pattern. **Add to `<deploy_rules>`:**
  - Static sites are **auto-served on port 8000** — write to the workspace root; no
    server needed. Preview shows it live.
  - **NEVER `pkill` / kill the http.server on 8000** — that IS the preview; killing
    it breaks the user's view (this is the exact failure that happened).
  - For a real dev server, bind **`0.0.0.0:8000`** and start it with the persistent
    mechanism (E1), **never** `cmd &` in `shell` (it won't survive).
  - **Do not curl-loop to self-verify** — the preview proxy is the check.

### E5 — Make the auto-serve resilient (backend) — see G
- Folded into G's resume-restart + a **supervised** keepalive (restart http.server if
  it exits) so a stray kill self-heals.

### E6 — Agent gets health CONTEXT, read-only (you asked; security-balanced)
- The agent should **understand** the preview state but not be able to break it.
  - **`preview_status` (read-only tool):** returns "serving / down / no-files" by
    checking port 8000 — pure observation, no side effects. Gives the agent the
    context it was missing.
  - **`restart_preview` (controlled action):** restarts the auto-serve safely
    (idempotent), instead of the raw `pkill`+`&` that caused this. So recovery is
    possible but *bounded*.
  - **Security call (my recommendation): hard-deny the destructive pattern** — a
    `shell` `pkill`/`kill` targeting the preview server (or http.server on :8000) is
    refused at the gate (`_hard_deny_reason`). The agent can observe + use the
    controlled restart; it cannot kill the user's window. This is the right balance:
    read freely, recover via a safe path, never break the preview.

### E7 — UI: a "Refresh preview" / restart-server button (you asked)
- A button on the preview pane that (a) reloads the iframe, and if still unreachable,
  (b) calls `restart_preview` on the backend to bring the server back. So a hung/
  down preview is **always user-fixable in one click** — never a dead end. Pairs with
  the agentic `restart_preview` (E6) so both the user and the agent can recover it.

---

## H. CRITICAL — context/cost bloat (~60–65k tokens on EVERY action)

**Confirmed root cause:** the condensation trigger is `0.65 × context_window`
(`view.py:267`), where the window is the *driver model's* max
(`_driver_context_window`). DeepSeek V4 Pro = **1,048,576** → soft trigger **≈681k**.
So at 60k the condenser NEVER fires; the loop materializes the **entire growing
history** and re-sends it every action. Receipts: 64–65k input tokens per tool call,
~$0.01–0.11 each, dozens of calls per build — *climbing* as it accumulates.

**What's actually in the 60k (the driver's View):**
| Piece | ~size | |
|---|---|---|
| System prompt (driver prompt + skills) | 2–4k | fixed |
| Tool schemas | 2–3k | fixed |
| **Event history — every action WITH full tool-call args + every observation** | **~55k, growing** | the bulk |

The dominant cost is **`file_write` carrying the entire file body in its
`arguments`**, re-materialized every turn (a 59 KB file ≈ 15k tokens *every action,
forever*), plus accumulating shell outputs.

**Scope — this is ONE role, not systemic (verified):** only `AGENT_DRIVER` (the
planner + build driver) receives the full View. The targeted roles are separate
pipeline stages with their own small inputs and **never see the transcript**:
`RAG_ANSWERER` ← passages + question (`grounding.py:99`); `QUERY_REWRITER` ← the query
(`engine.py:59`); `SUMMARIZER` ← only the span being condensed (keep_head=1/
keep_recent=6); `NLI` ← one (premise, hypothesis) pair (bundled-local). So picking a
paid model does NOT multiply the 60k across roles — the bleed is purely the driver,
linear in its turns. **Fixing the driver fixes the cost.** (Design intent confirmed:
rich context for the driver, scoped context for targeted roles — the bug is only that
the driver's "rich" lost its ceiling on a huge-window model.)

**Fixes (all target the driver's View):**
- **H1 — Decouple the driver's working-context from model max.** Cap the condensation
  trigger at a **fixed working budget** (≈16–32k, configurable):
  `soft = min(0.65 × window, BUDGET)`. A 1M window is capacity, not a license to spend
  60k/turn. Cuts ~60k → ~24k and stops the growth. Target shape for the driver:
  **rich but bounded** = recent events + plan + pinned knowledge + a running summary
  of the old span (NOT the full transcript).
- **H2 — Elide large tool-call ARGUMENTS in the View.** The Observation snip (8k)
  doesn't touch action args. Snip/reference written file content after the fact (keep
  the latest write, elide older repeats) — the file is on disk + readable; it doesn't
  belong in every prompt.
- **H3 — Bound the recitation/pin set** (plan + knowledge pins are re-sent each turn —
  fine, but cap them).
- **Measure:** log input-token count per action so a regression is visible.

## G. Session lifecycle — auto-suspend / resume (THE unifying mechanism)

One mechanism that subsumes F (leak), D1 (auto-spin), and E5 (preview restart):
**a build session is live only while you're using it.**

- **Auto-suspend (kill automatically):** when a build goes inactive — WS disconnect
  (you closed the tab), an idle TTL (no activity for N min), or a terminal-for-now
  status — **destroy the sandbox container.** That kills the dev/preview server,
  frees the published port, the memory, and the GPU. The workspace **persists** (the
  named volume), so nothing is lost. This is what stops the leak at the source (vs.
  only reaping orphans after the fact).
- **Resume (restart on demand):** clicking a project is **read-only** (no spin-up).
  An explicit **Resume** re-creates the container from the persisted workspace volume,
  **restarts the auto-serve on :8000** (preview comes back), and replays the event
  log so the loop continues exactly where it left off.
- **State machine:** `live ⇄ suspended`. Suspended = no container, workspace volume
  on disk, conversation events in the DB. Resume rehydrates all three.

This is the highest-priority backend item — it's the root cause of the leak, the
GPU-on-click complaint, AND the dead preview, in one design.

## F. (subsumed by G) — immediate orphan cleanup
- The 17 leaked containers already running need a one-time `podman rm -f` (workspaces
  survive as volumes). G prevents new ones; F clears the existing backlog.

### E3 — The `</parameter>` leak (backend, needs isolation)
- A tool-call argument got a stray `</parameter>`. Likely a model emitting XML-style
  tool calls mis-parsed, or our `openai_provider` tool-call extraction. Needs a
  reproduction to pin; real correctness bug (corrupts commands → exit 2).

### E4 — Verify-on-finish shouldn't assume a server (backend)
- `_finish_verify_passed` runs a shell command; for a **static** deliverable the
  check should be "files present + HTML parses", not "curl a running server." Add a
  static-site verify path so a page build can finish honestly.

---

## Recommended sequence

1. **H** (context/cost bloat: cap working-context + elide file args) — actively
   burning money on every paid call; highest leverage, contained backend fix.
2. **G** (session lifecycle: auto-suspend + resume) — leak + GPU-on-click + dead-
   preview root cause; includes the one-time orphan cleanup (F).
3. **E** (loop: prompt "never kill :8000" + supervised serve + read-only health +
   `restart_preview` + hard-deny preview-kill + UI refresh button + static verify).
4. **D2** (circuit-breaker: structured recovery proposals + Continue-anyway) — pairs
   with E (same failure surface).
5. **C1** (encoder UI: contextual endpoint fields).
6. **A2** (paid-model toast).
7. **B0+B1+B3** (defaults/persistence + extraction: bundled-markdown + Firecrawl + tool markdown).
8. **B2** (search: bundled `ddgs` default + paid/self-host).
9. **E3** (`</parameter>` leak) — isolate once reproducible.

Each ships with tests + a clean restart, per the working agreement.
