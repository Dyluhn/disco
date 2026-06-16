# Disco — Code Organization & OSS License Audit (2026-06-15)

Read-only audit. Six Sonnet subagents: 1 internal structure audit, 2 external web-research
(top-starred repo organization), 2 license audits, synthesized here. Nothing in the codebase
was changed.

---

## PART 1 — CODE ORGANIZATION

### 1.1 Monoliths (our biggest files vs. their justified size)

| File | LOC | What's bundled inside | Suggested split | Priority |
|---|---|---|---|---|
| `core/loop/engine.py` | 6,446 | `AgentLoop` (4,603) incl. a **1,878-line `run()`**; 16 inline tool-spec factories + 10 singleton caches (~553); project-bootstrap detectors (~182); F8/F9 dedup helpers (~353) | `loop/tool_specs.py`, `loop/workspace_detector.py`, `loop/f8f9.py`; keep `engine.py` = `AgentLoop` only | **P0** |
| `agent-server/runtime.py` | 3,836 | `ConversationRuntime` core; **Deep Research orchestration inline (~1,171)**; MCP pool lifecycle (~327); share export/import (~153); model probing (~139) | `deep_research_runner.py`, `mcp_lifecycle.py`, `share.py` | **P0** |
| `agent-server/app.py` | 1,678 | every REST+WS route in one `create_app()` (~1,522) | `routes/{conversations,sessions,uploads,schedule,share,workspace}.py` | P1 |
| `app-server/config_state.py` | 931 | ~20 DTOs (~454) + `ConfigState` (~476) | `config_dtos.py` + keep `config_state.py` | P1 |
| `core/view.py` | 824 | `View`+`microcompact` (~414) + Condenser/Summarizer/LLM impl (~349) | `condensation.py` | P2 |
| `frontend/components/build/ExecutionCanvas.tsx` | 929 | StreamingFileView, FilesPane, TerminalPane, PreviewPane (~217), CockpitPane+5 subs (~320), tab orchestrator | `PreviewPane.tsx`, `CockpitPane.tsx`; canvas = thin tab router | P1 |
| `frontend/components/blocks.tsx` | 650 | inline markdown + Chart (157) + Sheet (85) + Slides (131) + BlockView + Citation | `ChartBlock.tsx`, `SheetBlock.tsx`, `SlidesBlock.tsx` (test files already isolated) | P2 |

### 1.2 Layering & coupling defects (real, with evidence)

- **P0 — circular import `tools → agent-server`**: `tools/builtin/audio_overview.py:67,156,238,262`
  imports `disco.agent_server.audio_config` / `tts_local`. `disco-tools` declares only `disco-core`.
  Creates a hidden `agent-server → tools → agent-server` cycle. Fix: move TTS config/iface down
  into `tools` (or `core`) and inject the concrete `synthesize` at runtime.
- **Undeclared transitive deps** (work only because the uv workspace shares one venv):
  - `tools/mcp/retrieval_tier.py:14` imports `disco.retrieval` — not in `tools` pyproject.
  - `app-server/config_state.py` (433,655,727,749,874,888,910) imports `disco.tools` — not declared.
- **Reaching into private internals**: `agent-server` (`app.py:41`, `host_proxy.py:69`,
  `runtime.py:112,1116`) imports from `disco.tools.sandbox._container` (USER_PORTS, PREVIEW_PORT,
  EGRESS_PROXY_PORT, proxy_env). Promote those to `sandbox/__init__.py`.
- **Concrete impl leaking from a public API**: `core/__init__.py` re-exports `SqliteEventStore`
  (42 callers use `from disco.core import SqliteEventStore`). The public boundary should be the
  `EventStore` protocol; import the concrete store from `disco.core.store.sqlite`.
- **Private-name import**: `runtime.py:75` imports `_BOOKKEEPING_TOOLS` from engine — promote to
  `loop/__init__` public API.

### 1.3 Root hygiene / misplacement

- Repo root holds gitignored dev-exhaust: `run_manual*.py`(×5), root `test_*.py`(×5), `*.diff`(×3),
  stray CSVs, `validate.py`, `validation_output.txt`. Correctly gitignored but pollutes the tree →
  move to `scratch/` or delete per-sprint.
- Live DB (`disco.db` + 4 sidecar JSON/`-wal`/`-shm`) runs from repo root → point `DISCO_DB` at
  `./data/` or `$XDG_DATA_HOME/disco/`.
- `harness/` and `integrations/messaging/` are real Python (import `disco.*`) but are **not** uv
  workspace members and lack `pyproject.toml` → `harness/tests/*` is silently excluded from the
  default `pytest` run. Make them `disco-test-harness` / `disco-bot` workspace members.
- Filename collision: `components/build/BuildSurface.tsx` only exports `UploadComposer` → rename.

### 1.4 What the most-starred repos (2025–26) do — adoptable patterns

Surveyed (file-tree-verified): OpenHands, SWE-agent, Aider, browser-use, LangGraph, Langflow, n8n,
crewAI, Dify, Cline, smolagents, PydanticAI, Mastra, Continue + frontend refs (excalidraw, dub,
cal.com, supabase, shadcn, open-webui).

1. **Organize backend by DOMAIN, not layer** — `agent/ sandbox/ tools/ memory/ retrieval/` beats
   `services/ repositories/`. (SWE-agent, browser-use, OpenHands app_server.) We already do this at
   the *package* level; the win is doing it *inside* big files.
2. **Isolate the agent loop with per-entry-point files** — SWE-agent's `run/{run_single,run_batch,
   run_replay}.py`. Directly relevant to our 1,878-line `run()`.
3. **Make boundaries real with `import-linter`** (Python) — uv workspaces share one venv and do
   **not** enforce declared deps at runtime (confirmed: astral-sh/uv#10960). A `layers` contract
   (`app_server > agent_server > retrieval > tools > core`) run as `lint-imports` in CI is the only
   thing that would have caught our `tools→agent-server` cycle. This is the single highest-leverage
   adoption.
4. **File-size CI gate** — the worst god-files in the wild (pydantic-ai `openai.py` 230KB, crewAI
   `llm.py` 104KB, smolagents `agents.py` 80KB) exist because nothing blocks them. ESLint
   `max-lines: 300/500` for TS; a ruff/pre-commit line-count gate for Python.
5. **Frontend = feature slices + companion-directory decomposition** — `features/<domain>/{components,
   hooks,api,types,index.ts}`; any component >200 lines becomes `X.tsx` + `X/` of sub-parts where the
   root file is a pure compositor. (n8n features/, open-webui Messages/, excalidraw extracted `element/`
   + `math/` packages.) One barrel per feature boundary only — deep barrels break tree-shaking
   (Next.js#12557).
6. **Tooling**: uv workspaces (we have this) + a single root `ruff.toml`; consider Biome for the
   frontend; entry-point plugin registration for swappable providers (Dify's VDB/trace pattern) —
   fits our 3-tier universal providers.

### 1.5 Verdict

The 5-package split (`core → tools/retrieval → agent-server → app-server`) is sound and largely
respected. The real problems are **scale concentration** (engine.py / runtime.py god-files) and a
handful of **mechanical boundary leaks** (one true cycle, undeclared deps, private-internal reach).
None requires a rewrite; P0 items are pure relocations + one dependency-direction fix, and
`import-linter` + a file-size gate would keep it from regressing.

---

## PART 2 — OSS LICENSE INVENTORY

### 2.1 Harvest / prior-art sources (engineering ideas & patterns)

| Project | owner/repo | License | Family | Ideas or code? | Flag |
|---|---|---|---|---|---|
| SmallCode | Doorman11991/smallcode | MIT | Permissive | design-harvested; code ports **pending** | MIT notice due when a port lands (already staged in ACKNOWLEDGEMENTS) |
| Aider | Aider-AI/aider | Apache-2.0 | Permissive | ideas (A8 snapshot) | NOTICE check if code ported |
| OpenHands | All-Hands-AI/OpenHands | **MIT** main / PolyForm-Free-Trial `enterprise/` | Permissive (main) | `stuck.py` clean reimpl (no copied code) | harvested from MIT main, NOT `enterprise/` — OK; avoid `enterprise/` in future |
| SWE-agent | SWE-agent/SWE-agent | MIT | Permissive | ideas (epochal masking) | none |
| Cline | cline/cline | Apache-2.0 | Permissive | ideas | none |
| smolagents | huggingface/smolagents | Apache-2.0 | Permissive | ideas | none |
| OpenManus | FoundationAgents/OpenManus | MIT | Permissive | ideas | repo moved from mannaandpoem/* — update refs |
| OpenCode | anomalyco/opencode (was sst/opencode) | MIT | Permissive | ideas (HS-08 reroute, compaction) | repo moved — refs still redirect |
| **Suna** | kortix-ai/suna | **Elastic License 2.0** | **Source-available, NOT OSS** | ideas only (status enum, pptx layering) | **VERIFY no code copied** — ELv2 bars managed-service + distribution; ideas aren't copyrightable so OK *if* no snippets landed |
| Manus | (proprietary) | — | — | architectural benchmark | no OSS obligation |

All confirmed via GitHub license API / raw LICENSE fetch. None unconfirmed.

### 2.2 Integrated / bundled OSS tools & provider models

Permissive (no obligation): ddgs (MIT), Crawl4AI (Apache-2.0), fastembed (Apache-2.0),
kokoro-onnx code (MIT) + Kokoro-82M weights (Apache-2.0), Speaches (MIT, med-confidence),
Marp (MIT), WeasyPrint (BSD-3), openpyxl (MIT), Python-Markdown (BSD-3), Univer (Apache-2.0 —
**confirmed clean, no commercial clause**), Chart.js (MIT), cronsim (BSD-3), mcp SDK (MIT),
e5-large embed model (MIT), bge-small (MIT), ms-marco-MiniLM rerank (Apache-2.0).

Copyleft / non-commercial (flags below):

| Tool | License | Integration | Obligation |
|---|---|---|---|
| **jina-reranker-v2-base-multilingual** (DEFAULT full-tier rerank model) | **CC-BY-NC-4.0** | downloaded + run in-process when `DISCO_ENCODER_TIER=full` (the default) | **CRITICAL — non-commercial only** |
| pandoc | GPL-2.0-or-later | `apt install` in sandbox image, shell-out for DOCX | no copyleft propagation (subprocess); distribute-image source note |
| lameenc | LGPL-3.0 | `tts` extra, in-process MP3 encode | users must be able to replace the lib (`pip install --upgrade` suffices) |
| SearXNG | AGPL-3.0 | self-host option, HTTP call | obligation is on the instance operator, not Disco |

### 2.3 Risk flags (ranked)

1. **CRITICAL — default reranker is non-commercial.** `jinaai/jina-reranker-v2-base-multilingual`
   (CC-BY-NC-4.0) is the **default** full-tier rerank model and auto-downloads on first full-tier use.
   Commercial use is prohibited with no dual-license path. Any commercial/SaaS deployment of Disco on
   defaults is non-compliant. `DISCO_RERANK_MODEL` can override it, but the *unsafe default* ships.
   Remediation: change the full-tier default to a permissive multilingual reranker (e.g. **BAAI/
   bge-reranker-v2-m3, MIT** — which is already what the homelab encoder box runs) or ms-marco-MiniLM
   (Apache-2.0, English-only). [decision for Dylan]
2. **Suna (ELv2)** — confirm no code snippets copied (`grep -r "kortix\|project_session_status" packages/`).
   Ideas-only is fine; copied code is not.
3. pandoc / lameenc / SearXNG — low risk, standard for shell-out / weak-copyleft / self-hosted-AGPL;
   add a source-availability note if a pre-built sandbox image is published.

### 2.4 Bottom line

The harvest sources are **all permissive (MIT/Apache/BSD) and were taken as ideas, not code** — clean,
with ACKNOWLEDGEMENTS already staging the MIT-notice policy for any future ports. The integrated stack
is permissive **except** the one CC-BY-NC reranker default, which is the single concrete licensing
action item (a one-line default change), plus shell-out GPL/LGPL/AGPL that don't propagate to our code.
