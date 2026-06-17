# God-file decomposition — status & handoff (2026-06-16)

Branch `build-surface-recovery-ux` (unpushed). All work below is committed and
**regression-verified** (full Python suite green for every change — see "Verification").

## Done — files shrunk (−~1,835 LOC moved into 13 focused modules, zero behavior change)

| God file | Before | Now | Extracted into |
|---|---|---|---|
| `core/loop/engine.py` | 6,452 | **5,330** | `loop/{fc_kit,bootstrap,dedup,tool_specs,messages}.py` |
| `app_server/config_state.py` | 954 | **539** | `config/{dtos,mappers}.py` |
| `core/dod_evaluator.py` | 977 | **918** | `core/dod_util.py` |
| `agent_server/runtime.py` | 3,857 | **3,815** | `runtime_model_probe.py` |
| `agent_server/app.py` | 1,811 | **1,614** | `share_viewer.py` |

Commits (this session, on top of `01eb7f8`):
- `c8bff40` engine→fc_kit · `35de94b` engine→bootstrap · `0219e46` engine→dedup · `07ea167` engine→tool_specs · `8dfc2e3` engine→messages (Opus subagent A)
- `7e560bc` config→dtos · `3df7852` config→mappers (Opus subagent B)
- `f18c2a9` dod→dod_util (MiniMax worker) · `bb29ab2` runtime→model_probe (MiniMax worker)
- `57247c5` app→share_viewer · `ee6320f` MCP fake-surface test fix (SEC-1 follow-up)
- `4ef67dd` SEC-1 (MCP stdio env leak — separate security fix)

**Method:** each extraction is a pure move — new sibling module + re-import at the
call sites + repoint external importers (incl. tests) + dead-import cleanup. Verified
per-extraction: ruff clean + import-identity + the package suite. The recipe is in any
of the commits above (`git show 35de94b` is the canonical example).

## Verification status
- **Full regression GREEN** for all changes: `core`, `agent-server`, `app-server`, `tools` all exit 0 / 0 failures.
- **Pre-existing failure (NOT from this work):** `retrieval/test_deep_research.py::test_bounds_for_three_tiers_are_distinct_and_ordered` — fails identically at `01eb7f8` (pre-session). Cause: quick AND standard DR tiers both have `max_wall_clock_s=600`, violating the "distinct & ordered" assertion. **Decision needed** (you have opinions on the quick-search clock) — not touched autonomously.
- **Pre-existing ruff debt (NOT mine):** 15 errors in untouched files — `agent_server/audio_config.py` (4), `report_audio.py` (3), `core/llm/prompts.py` (2), + a few tests. Worth a cleanup pass but separate from this work.
- **Git-history nit:** the E501 reformat of the 3 MCP test files landed in `57247c5` (share-viewer) via an `--amend` that hit HEAD instead of `ee6320f`. All changes correct; only the grouping is off — left as-is (rebase not worth the risk).

## Remaining — SAFE pure moves (regression-verifiable; importer lists pre-gathered)
These are the same low-risk pattern as above. Each has external importers that MUST be repointed (the trap that breaks weak workers):

**`runtime.py` (3,815 → ~3,480):**
- MCP cluster `_MCPToolWrapper`, `_MetaToolSearchWrapper`, `_apply_mcp_scope` → `runtime_mcp.py`. Repoint: `tests/test_mcp_tool_search_cap.py`, `test_mcp_fence.py`, `test_mcp_pool.py`.
- `build_sandbox_service` → e.g. `runtime_sandbox.py`. Repoint **`__main__.py` (PRODUCTION)** + `tests/test_sandbox_config.py`.
- `_release_process_memory` + `_has_unfinished_plan` → `runtime_helpers.py`. Repoint `tests/test_memory_release.py`.

**`app.py` (1,614 → ~1,470):**
- `CreateConversationBody`, `SendMessageBody` → `app_models.py` (matches the existing `schedule_models.py` convention).
- `_sanitize_name` (repoint `tests/test_upload.py`), `_user_message`, `_MAX_*`, `_handle_frame` (already standalone, called at app.py:~1193) → `app_helpers.py`.

**`core/view.py` (824):** `view/condense.py` + `view/microcompact.py` (per the hygiene H1 plan) — wide importers (`from ..view import …`), so convert to a package with a re-exporting `__init__`.

**Frontend:** `ExecutionCanvas.tsx` (978) → `build/canvas/{FilesPane,TerminalPane,PreviewPane,Cockpit}.tsx`; `blocks.tsx` (650) → `blocks/{ChartBlock,SheetBlock,SlidesBlock,TableView,CitedText}.tsx`. Verify with tsc + vitest + a build (+ a visual pass, since it's UI).

## Remaining — HARD cores (NOT done autonomously by design — behavior risk green tests can't fully certify)
These are god-*functions*/god-*classes*, not pure-move helpers. They require in-function/in-class refactoring where passing tests do **not** guarantee no behavior change — exactly the risk not worth taking unsupervised. Recommend doing these with a live smoke (drive the real app) in the loop, one at a time, full regression each:
1. **`engine.py` `run()`** (~1,800 LOC, lines ~4307–6185) → dispatch table of per-meta-tool handlers. HIGHEST risk (the agent-loop heart; shared mutable `events`/`noops`/`step`, `continue`-flow, streak counters). This is the single biggest remaining shrink (engine.py would drop to ~3,500) and the #1 GitHub-optics item.
2. **`app.py` `create_app`** (~1,400 LOC of nested route closures, lines ~107–1560) → per-domain `APIRouter` modules. Medium risk: mechanical FastAPI extraction, but each route closes over `store`/`runtime`, so it's a closure→`Depends`/factory conversion, not a pure move. Well-covered by the agent-server route tests.
3. **`runtime.py` `ConversationRuntime`** (~3,300 LOC) → collaborator objects: `DeepResearchService`, `McpManager`, lifecycle/suspend. Medium-high risk (method clusters share instance state).

## Notes
- Driver still on `120b-free` (`disco-config.json`). Branch unpushed — push/PR is yours to trigger.
- Orchestration scaffolding added this session: `workorders/decomp-*.md` briefs + `orders.yaml` entries (`decomp-runtime-probe`, `decomp-dod-util`) for the MiniMax work-order lane.
