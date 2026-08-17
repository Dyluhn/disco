# Walkthru fix campaign — execution plan (2026-06-17)

Fixes for the 24 issues in `docs/dylans-walkthru-6-17-26.md` (WALK-01..21).
Dylan authorized autonomous execution; report per item; mark off as verified.

## Constitution (every lane obeys)
- **Layering** (`.importlinter`): `core ← retrieval ← tools ← {agent_server|app_server}`.
  No new upward imports. `uv run lint-imports` must pass.
- **Types**: `uv run basedpyright` = ZERO errors tree-wide. No `# type: ignore`.
- **Arch budget**: `uv run python scripts/check_arch_budget.py` — no class >800 /
  function >200 LOC past caps. Engine lane especially.
- **Diagram**: if cross-package imports change, `uv run python scripts/gen_arch_diagram.py`.
- **Frontend**: `npm run typecheck:build` (shipping tsc) + `npx vitest run` (relevant).
- **Tests prove the fix**: every behavioral change gets a test that FAILS before and
  PASSES after. Live-data seam for FE (spy `isLive`→true + stub `fetch`), real-sample
  harnesses for BE. No cassettes for the final live check.
- **Exit code is truth.** Preserve originals. Match surrounding style.

## Collision-safety: file-disjoint lanes
Each lane owns an EXCLUSIVE file allowlist; agents touch nothing outside it and run
NO git commands. The integrator (Claude main loop) runs the gates on the unified
tree + the live gpt-oss-120b/Playwright verification per item.

### WAVE 1 (parallel — all allowlists disjoint)

**LANE 1 — DR-surface frontend** · WALK-01, 02, 03(render+export-strip), 04, 05, 08, 11
- `frontend/src/components/AnswerDocument.tsx`
- `frontend/src/components/research/DeepResearchSurface.tsx`
- `frontend/src/components/research/DeepReportView.tsx`
- `frontend/src/components/research/DeepProgressStrip.tsx`
- `frontend/src/hooks/useDeepResearch.ts`
- `frontend/src/hooks/useDeepResearchStream.ts`
- `frontend/src/api/deepResearch.ts` (export `[[id]]` strip only; serializer stays
  report-only — WALK-20 is Wave 2)
- `frontend/src/components/settings/DataSourcesSection.tsx` (WALK-05)
- (+ their `.test.tsx`)

**LANE 2 — DR synthesis + config backend** · WALK-03(notes scrub), 06, 07
- `packages/retrieval/src/disco/retrieval/deep_research/synthesis.py`
- the persisted-config save path for search/extraction (null base_url on bundled flip)
  — `packages/core/src/disco/core/llm/config.py` and/or `config_store.py`
- (+ their tests). NOTE: do NOT touch `TtsSettings` (Lane 5 territory is per-request).

**LANE 3 — Build preview + deliverable card + artifacts** · WALK-09, 10, 16
- `frontend/src/components/BuildSurface.tsx`
- `frontend/src/components/build/DeliverablePanel.tsx`
- `frontend/src/lib/buildTrace.ts`
- `frontend/src/components/build/canvas/PreviewPane.tsx`
- `packages/agent-server/src/disco/agent_server/routes/preview.py`
- `packages/agent-server/src/disco/agent_server/preview_service.py` (only if needed)
- `frontend/src/api/client.ts` (only the preview-URL helper, if needed)
- (+ their tests)

**LANE 4 — Agent prompts + schedule UX** · WALK-15, 17
- `packages/core/src/disco/core/llm/prompts.py`
- `frontend/src/lib/scheduleNL.ts`
- `frontend/src/components/build/ScheduleSection.tsx` (or wherever it lives)
- (+ their tests)

**LANE 5 — Audio** · WALK-13, 14, 21
- `packages/agent-server/src/disco/agent_server/tts_local.py`
- `packages/agent-server/src/disco/agent_server/report_audio.py`
- `packages/tools/src/disco/tools/builtin/audio_overview.py`
- `packages/agent-server/src/disco/agent_server/routes/report.py`
- `frontend/src/components/research/NeedMoreCard.tsx`
- `frontend/src/components/research/AudioPlayer.tsx` (NEW)
- `frontend/src/components/settings/AudioSection.tsx`
- (+ their tests). Audio mode is a PER-REQUEST param — do NOT edit `config.py`.

### WAVE 1b (sequenced after integration — engine risk, focused)
**LANE 6 — Engine pause/resume + no-progress breaker** · WALK-18, 19
- `packages/core/src/disco/core/loop/engine.py`, `loop/stuck.py`, `loop/turn_control.py`
- `packages/agent-server/.../routes/ws.py`, `control_ops.py`, `runtime.py`, `resume_service.py`
- Conservative; respect engine.py size caps; gate hard.

### WAVE 2 (after Wave 1) — Follow-up feature · WALK-12, 20
Cross-cuts DR surface + NeedMoreCard + `deep_research_service.py` + report serializers.
Sequential (depends on Wave 1 edits to those files).

## Verification (per item, before mark-off)
1. Lane self-check (its gate) green.
2. Integrator runs the four fitness gates + relevant unit suites on the unified tree.
3. LIVE: relaunch stack (app:8800 + agent:8000, driver `or-gpt-oss-120b-free`,
   frontend:5173), drive the real path in Firefox/Playwright, screenshot, `SendUserFile`.
4. Mark the item off in `docs/dylans-walkthru-6-17-26.md` Section 3 + the consolidated
   register, and report to Dylan.

## Status
- [ ] Wave 1 lanes 1-5 dispatched
- [ ] Wave 1 integrated + gated
- [ ] Wave 1b engine
- [ ] Wave 2 follow-up
- [ ] Live verification per surface
