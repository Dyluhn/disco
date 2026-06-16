# Archived god files

Verbatim copies of the five largest files **as they were before the 2026-06-16
decomposition**, pulled from commit `01eb7f8` (the last commit before the work
started). Kept as a before/after record. **These are NOT live code** — do not
import or edit them; the real, decomposed versions are in `packages/`.

## Before → after

| File | Before (here) | After | Where the logic went |
|---|---|---|---|
| `engine.py` | **6,452** | 1,495 | `run()` 1,875→299 dispatcher; `AgentLoop` 4,703→1,050 coordinator. 10 collaborators: `loop/{signals,view_render,recitation,plan_conditions,observe,driver,finish,turn_control,plans,control}.py` |
| `runtime.py` | **3,857** | 1,559 | `ConversationRuntime` 3,405→1,159 composition-root. 10 collaborators: `{mcp_manager,share_service,resume_service,lifecycle,deep_research_service,runtime_settings,schedule_service,sessions_service,preview_service,control_ops,runtime_model_probe}.py` |
| `app.py` | **1,811** | 125 | `create_app` 1,454→76; 44 routes → `routes/` (15 domain routers) + `share_viewer.py` |
| `config_state.py` | **954** | 539 | DTOs + mappers → `config/{dtos,mappers}.py` |
| `dod_evaluator.py` | **977** | 918 | pure helpers → `dod_util.py` |

Also decomposed (not god-*files* but god-*functions/classes*): the frontend
`ExecutionCanvas.tsx` 978→177 + `blocks.tsx` 650→9 (into `build/canvas/` and
`blocks/`); the app-server `create_app` 238→30; the retrieval Deep-Research
`run()` 267→63; the tools `audio_overview` `run()` 211→51.

## The guarantee
`scripts/check_arch_budget.py` (CI/pre-commit gate) forbids any class > 800 LOC
or function > 200 LOC outside a small, capped, documented allowlist — so none of
these files can regrow into a god file. That gate is what makes this archive a
*historical* record rather than a recurring problem.

Provenance: every file here is `git show 01eb7f8:<path>`. Every behavior was
preserved through the decomposition (full test suite green at each step).
