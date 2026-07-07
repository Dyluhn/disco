OpenAI Codex v0.141.0
--------
workdir: /var/home/dylan/projects/disco
model: gpt-5.5
provider: openai
approval: never
sandbox: read-only
reasoning effort: xhigh
reasoning summaries: none
session id: 019eec1d-16e7-76b0-80c8-d47325ae6a19
--------
user
# Review: Disco "evidence-harness" test suite implementation

You are reviewing a newly-built test architecture in this repo (a local-first agentic LLM app:
Python uv monorepo `packages/{core,retrieval,tools,agent-server,app-server}` + a React/TS
frontend). Be skeptical and concrete. Your job is to find flaws, gaps, false confidence, and
correctness bugs — not to praise. Cite file:line. If something is sound, say so briefly and
move on; spend your effort on what's wrong or missing.

## Why this was built (the problem)
The repo has ~1800 green unit tests, yet real usage broke repeatedly. The diagnosed root cause:
the unit tests assert on the MOCKED MIDDLE and amputate the two endpoints that matter — the real
input and the real delivered output. Four historical bugs all passed CI:
1. Decks shipped PROCEDURAL PLACEHOLDER images (keyless `pil-procedural` backend draws
   nested-rectangle/diagonal test patterns) instead of real/no images.
2. A silent hang: an uncaught `SandboxError` (subclasses `Exception`, NOT `OSError`/
   `FileNotFoundError`) escaped ~8 `except (FileNotFoundError, OSError)` bookkeeping handlers,
   killed the loop task, and left status RUNNING forever (no crash supervisor on `kick()`).
3. Frontend build progress showed 0/4 steps on a finished small-model build (deriveBuildProgress
   early-returned when no `update_plan_progress` snapshot existed).
4. Slides defaulted to `format="html"` (a raw .html file, not a presentable deck).

## What was implemented (review each for correctness + gaps)
Read these files. The work items:
- **W1 sandbox-exception contract**: `packages/tools/src/disco/tools/sandbox/base.py` adds
  `SandboxFileNotFoundError(SandboxError, FileNotFoundError)` + `looks_like_not_found()` +
  `raise_read_error()`. `_container.py`/`podman.py` `read_file` now call `raise_read_error`.
  Test: `packages/tools/tests/test_sandbox_exception_contract.py`. Guardrail:
  `packages/tools/tests/test_sandbox_read_guardrail.py` (no backend may raise a bare
  SandboxError on a read). QUESTION: is multiple-inheritance of the two exception types sound?
  Are there OTHER sandbox read/exec paths (gvisor.py, kernel.py, session.py) that still leak an
  untyped error past the boundary? Is `looks_like_not_found` (substring "no such file") robust?
- **W2 task supervision**: `packages/agent-server/src/disco/agent_server/runtime.py` `kick()`
  adds `add_done_callback` → `_on_run_task_done` → `_terminalize_crashed` (emits terminal ERROR +
  reminder; idempotent; cancellation != error; IDLE-crash still surfaces). Tests:
  `packages/agent-server/tests/integration/test_task_supervision.py`,
  `test_supervision_guardrail.py`. QUESTION: race conditions? Could `_terminalize_crashed`'s
  `create_task` be lost on loop shutdown? Is the CONCLUDED-status set right (it excludes IDLE on
  purpose — correct)?
- **W3 slides/artifact**: `_slides_pipeline.py` `_stage_assets` skips the procedural backend;
  `slides.py` default format → `pptx`. Test: `test_slides_procedural_contract.py`. QUESTION: does
  defaulting to pptx break callers that assumed html? Is skipping by `backend.name ==
  "pil-procedural"` the right detection?
- **W4 evidence schema**: `packages/core/src/disco/core/evidence/schema.py` (pydantic
  EvidenceRecord, ErrorTaxonomy, `redact()`, traceparent). QUESTION: is `redact()` complete (does
  it miss nested secrets, list-of-dicts, or non-dict containers)?
- **W5 evidence endpoint**: `routes/debug.py` `GET /api/debug/evidence/{cid}` gated on
  DISCO_INSPECT, redacted. Test: `test_debug_evidence_route.py`. QUESTION: path-traversal? does
  it leak anything unredacted?
- **W8 scripted-agent harness**: `packages/agent-server/tests/integration/{scripted_model,harness}.py`
  drives the REAL ModelProvider→DefaultLLMRouter→RouterAgent.step boundary.
- **W9 frontend event-replay**: `frontend/src/lib/buildTrace.replay.test.ts` replays event
  fixtures through deriveBuildProgress/LiveSignal/Activity (the #3 bug lock-in), no browser.
- **W14a/W14b validators**: `packages/tools/src/disco/tools/verify/artifact_validators.py`
  (pdf/audio/sheet + `_looks_procedural` deck detector + `validate_deck_deliverable`) and
  `heavy_validators.py` (LibreOffice/poppler render). Tests: `test_artifact_validators.py`,
  `test_deck_validators.py`, `test_heavy_validators.py`. QUESTION: is `_looks_procedural`
  (≤12 colors AND corner-to-corner diagonal) robust against false positives (real flat
  illustrations/charts) and false negatives (procedural at odd sizes)?
- **W15 disco-verify runner**: `packages/agent-server/src/disco/agent_server/verify/{runner,schema,scenarios,scenarios_run}.py`
  drives the app via real HTTP/WS, locates deliverables, runs validators, writes a redacted
  dossier. Test: `test_disco_verify_runner.py`. QUESTION: does the WS/poll terminal detection
  handle AWAITING_* states and timeouts correctly (a hang must FAIL, not pass)?
- **W6 frontend hooks**: `frontend/src/lib/e2eBridge.ts` + `data-disco-control` attributes.
- **W11/W12 evidence harness**: `frontend/e2e-full/` (Playwright, VM-only) — Recorder/wsRecorder/
  artifactCollector/discoClick/uiInventory/assertions + dossier. Runs ONLY on a dedicated VM
  (Playwright OOMs the dev box); OFF the PR gate.
- **W17 CI**: `.github/workflows/evidence-harness.yml` — pr-evidence (boundary contracts +
  validators + frontend replay, no browser) + nightly-vm (Playwright + live scenarios + heavy
  validators, self-hosted).
- **W18 negative fixtures**: `packages/agent-server/tests/fixtures/negative/` (authentic
  procedural PNG, raw-html deliverable, no-plan-progress event stream).

## Deliverables I want
1. Correctness bugs (with file:line) — anything that is wrong, racy, or would pass-when-it-should-fail.
2. COVERAGE GAPS — places the harness still amputates input or output, or sandbox/exec paths W1
   missed, or surfaces/artifact types with no validator.
3. False confidence — any test that asserts on a mock where it should assert on the real boundary.
4. A short prioritized list (P0/P1/P2). Be exacting — assume I will fix every P0/P1.

codex
I’ll review this as a code review rather than a walkthrough: first map the touched files and tests, then drill into the boundaries where false positives are most likely.
  → Map changed evidence-harness files and relevant tests
  • Review sandbox exception contract and supervision runtime
  • Review artifact validators, evidence schema/route, and verify runner
  • Review frontend replay/e2e bridge and CI gating
  • Synthesize prioritized findings with file:line references
exec
/bin/bash -lc "rg --files | rg '("'^packages/(tools|agent-server|core)|''^frontend|''^.github/workflows/evidence-harness.yml)'"'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
packages/core/pyproject.toml
packages/core/tests/test_loop_stophooks.py
packages/core/tests/test_loop_state_machine.py
packages/core/tests/test_loop_integration.py
packages/core/tests/test_loop_extra.py
packages/core/tests/test_loop_confirmation.py
packages/core/tests/test_gate_scoping.py
packages/core/tests/test_equality.py
packages/core/tests/test_cluster9_errors.py
packages/core/tests/test_cluster8_kvcache.py
packages/core/tests/test_cluster7_knowledge.py
packages/core/tests/test_cluster3_security.py
packages/core/tests/test_agent_isolation.py
packages/core/tests/llm_fakes.py
packages/core/src/disco/core/evidence/__init__.py
packages/core/src/disco/core/evidence/schema.py
packages/core/src/disco/core/view.py
packages/core/src/disco/core/wire.py
packages/core/src/disco/core/events.py
packages/core/src/disco/core/equality.py
packages/core/src/disco/core/migration.py
packages/core/src/disco/core/__init__.py
packages/core/src/disco/core/dod.py
packages/core/src/disco/core/obs.py
packages/core/src/disco/core/state.py
packages/core/tests/test_evidence_schema.py
packages/core/tests/test_c6_recitation_cadence.py
packages/core/tests/test_c18_plan_step_done_condition.py
packages/core/tests/test_dc05_loop.py
packages/core/tests/test_actionless_completion.py
packages/core/tests/test_loop_step.py
packages/core/tests/test_effective_plan_progress.py
packages/core/tests/test_snapshot_binary.py
packages/core/tests/test_dod_spec.py
packages/core/tests/test_mcp_cap_requery.py
packages/core/tests/test_f4_bootstrap.py
packages/core/tests/test_replan_forces_plan.py
packages/core/tests/test_loop_stuck.py
packages/core/tests/test_k1_elision_guard.py
packages/core/tests/test_hs03_reground_cadence.py
packages/core/tests/test_f9_read_dedup.py
packages/core/tests/test_f8_arg_truncation.py
packages/core/tests/test_c21_assist_prompt.py
packages/core/tests/test_exec_policy.py
packages/core/src/disco/core/brand/css.py
packages/core/tests/test_build_agent_isolation.py
packages/core/tests/test_brand.py
packages/core/tests/test_p2_driver_routing_retry.py
packages/core/src/disco/core/brand/__init__.py
packages/core/tests/test_e5_agent_planning_capability.py
packages/core/src/disco/core/brand/tokens.py
packages/core/tests/test_w5_execution_nudge_cap.py
packages/core/src/disco/core/brand/chart_svg.py
packages/core/tests/test_w2_stale_snapshot.py
packages/core/src/disco/core/brand/mark.py
packages/core/tests/test_w1_read_breaker.py
packages/core/tests/test_vision_caps.py
packages/core/tests/test_view_kv_stability.py
packages/core/tests/test_view.py
packages/core/tests/test_store_filters.py
packages/core/tests/test_store.py
frontend/tsconfig.app.json
packages/core/tests/test_state.py
packages/core/tests/test_snapshot_truncation.py
packages/core/tests/test_serialization.py
packages/core/tests/test_secrets.py
packages/core/tests/test_p1_p2_provider_reliability.py
packages/core/tests/test_observation_masking.py
packages/core/tests/test_g1_exec_artifact_tools.py
packages/core/tests/test_finish_browser_gate.py
packages/core/tests/test_cluster2_turntaking.py
packages/core/tests/test_bp03_acceptance.py
packages/core/tests/c11_still_omitted_proof.py
packages/core/tests/c11_recover_proof.py
packages/core/tests/loop_fakes.py
packages/core/tests/test_no_read_penalty.py
packages/core/tests/test_context_budget.py
packages/core/tests/test_config_store.py
packages/core/tests/test_build_loop_regression.py
packages/core/tests/test_loop_concurrency.py
packages/core/tests/test_condenser.py
packages/core/tests/event_fakes.py
packages/core/tests/test_event_kind_frontend_contract.py
packages/core/tests/test_store_status.py
packages/core/tests/test_df08_vision_escalation.py
packages/core/tests/test_c16_hard_reset_pointer_flush.py
packages/core/tests/test_clarify_gate.py
packages/core/tests/test_toolcall_recovery.py
packages/core/tests/test_toolcall_defense.py
packages/core/tests/test_f5_thinking_budget.py
packages/core/tests/test_f1_provider_recovery.py
packages/core/tests/test_c13_exec_prefill.py
packages/core/tests/test_dod_evaluator.py
packages/core/tests/test_env_compat.py
packages/core/tests/test_hs02_compaction_template.py
packages/core/tests/test_hs08_invalid_reroute_invariant.py
packages/core/tests/test_c20_subagent_fanout.py
packages/core/tests/test_c5_pmx_memory_persistence.py
packages/core/tests/test_finish_dod_gate.py
packages/core/tests/test_loop_condensation.py
packages/core/tests/test_cluster1_context.py
packages/core/tests/test_assist_wireup.py
packages/core/tests/test_autonomous_loop.py
packages/core/tests/test_autonomous_mode.py
packages/core/tests/test_watch_it_write.py
packages/core/tests/test_vision_wiring.py
packages/core/tests/test_verify_on_finish.py
packages/core/tests/test_tail_variation.py
packages/core/tests/test_skills.py
packages/core/tests/test_serve_tool.py
packages/core/tests/test_security_vocab.py
packages/core/tests/test_security_policy.py
packages/core/tests/test_security_integration.py
packages/core/tests/test_security_analyzer.py
packages/core/tests/test_router_summarizer_stream.py
packages/core/tests/test_router_routing.py
packages/core/tests/test_router_prompts.py
packages/core/tests/test_router_overflow.py
packages/core/tests/test_router_integration.py
packages/core/tests/test_router_extra.py
packages/core/tests/test_router_errors.py
packages/core/tests/test_remember_tool.py
packages/core/tests/test_prefill_masking.py
packages/core/tests/test_planner_gate.py
packages/core/tests/test_openai_provider.py
packages/core/src/disco/core/security/analyzers.py
packages/core/src/disco/core/security/__init__.py
packages/core/src/disco/core/security/assessment.py
packages/core/src/disco/core/security/risk.py
frontend/e2e-full/tsconfig.json
packages/core/src/disco/core/brand/fonts/SchibstedGrotesk.ttf
packages/core/src/disco/core/brand/fonts/NewsreaderItalic.ttf
packages/core/src/disco/core/brand/fonts/Newsreader.ttf
packages/core/src/disco/core/brand/fonts/LICENSE-OFL.txt
packages/core/src/disco/core/brand/fonts/FrauncesItalic.ttf
packages/core/src/disco/core/brand/fonts/Fraunces.ttf
packages/core/src/disco/core/inspect.py
packages/core/src/disco/core/env.py
packages/core/src/disco/core/dod_util.py
packages/core/src/disco/core/dod_evaluator.py
packages/core/src/disco/core/skills.py
frontend/shots.mjs
frontend/e2e-full/scenarios/click-provenance.smoke.spec.ts
frontend/e2e-full/scenarios/smoke.full.spec.ts
frontend/e2e-full/full.config.ts
frontend/src/vite-env.d.ts
frontend/src/App.tsx
packages/core/src/disco/core/loop/recitation.py
packages/core/src/disco/core/loop/messages.py
packages/core/src/disco/core/loop/engine.py
packages/core/src/disco/core/loop/plan_conditions.py
packages/core/src/disco/core/loop/signals.py
packages/core/src/disco/core/loop/turn_control.py
packages/core/src/disco/core/loop/stuck.py
packages/core/src/disco/core/loop/view_render.py
packages/core/src/disco/core/loop/finish.py
packages/core/src/disco/core/loop/driver.py
packages/core/src/disco/core/loop/boundaries.py
packages/core/src/disco/core/loop/agent.py
packages/core/src/disco/core/loop/plans.py
packages/core/src/disco/core/loop/observe.py
packages/core/src/disco/core/loop/file_state.py
packages/core/src/disco/core/loop/dedup.py
packages/core/src/disco/core/loop/control.py
packages/core/src/disco/core/loop/tool_specs.py
packages/core/src/disco/core/loop/fc_kit.py
packages/core/src/disco/core/loop/bootstrap.py
packages/core/src/disco/core/loop/__init__.py
packages/core/src/disco/core/loop/policies.py
packages/core/src/disco/core/loop/stream_extract.py
packages/core/src/disco/core/llm/prompts.py
packages/core/src/disco/core/llm/__init__.py
packages/core/src/disco/core/llm/exec_policy.py
packages/core/src/disco/core/llm/config.py
packages/core/src/disco/core/llm/config_store.py
packages/core/src/disco/core/llm/vision_table.py
packages/core/src/disco/core/llm/secrets.py
packages/core/src/disco/core/llm/types.py
packages/core/src/disco/core/llm/toolcall_recovery.py
packages/core/src/disco/core/llm/openai_provider.py
packages/core/src/disco/core/llm/routing.py
packages/core/src/disco/core/llm/summarizer.py
packages/core/src/disco/core/llm/policy.py
packages/core/src/disco/core/llm/errors.py
packages/core/src/disco/core/llm/provider.py
frontend/e2e-full/support/assertions.ts
packages/core/src/disco/core/llm/nli.py
packages/core/src/disco/core/llm/wiring.py
frontend/e2e-full/support/uiInventory.ts
frontend/e2e-full/support/discoClick.ts
frontend/e2e-full/support/artifactCollector.ts
frontend/e2e-full/support/wsRecorder.ts
frontend/e2e-full/support/recorder.ts
frontend/e2e-full/support/schema.ts
frontend/e2e/imagegen-comfy-workflow.spec.ts
frontend/e2e/report-export.spec.ts
frontend/e2e/autonomous-toggle.spec.ts
frontend/e2e/contradiction-surfacing.spec.ts
frontend/e2e/clarify-gate.spec.ts
frontend/e2e/schedule-section.spec.ts
frontend/e2e/sheet-block.spec.ts
frontend/e2e/plan-drafting.spec.ts
frontend/e2e/build-controls.spec.ts
frontend/e2e/ask-gate.spec.ts
frontend/src/views/HistoryView.tsx
frontend/src/views/SettingsView.tsx
frontend/src/views/ActivityView.dashboard.test.tsx
frontend/src/views/ShareView.tsx
frontend/src/views/ImportedRunView.tsx
frontend/src/views/ActivityView.test.tsx
frontend/src/views/ActivityView.tsx
frontend/src/views/ProjectsView.tsx
frontend/src/views/settings.test.tsx
frontend/src/views/HistoryView.status.test.tsx
frontend/src/views/history.test.tsx
packages/agent-server/pyproject.toml
frontend/e2e-full/fixtures/discoHarness.ts
frontend/e2e-full/fixtures/README.md
frontend/e2e-full/fixtures/cancelled_request.json
frontend/e2e-full/fixtures/finished_with_partial_plan_updates.json
frontend/e2e-full/fixtures/tool_error_visible.json
frontend/e2e-full/fixtures/running_then_error.json
frontend/e2e-full/fixtures/no_plan_progress_finished.json
frontend/vite.config.ts
frontend/Dockerfile
frontend/package.json
frontend/tsconfig.build.json
frontend/index.html
frontend/package-lock.json
frontend/e2e/visual.spec.ts-snapshots/settings-alt-theme-firefox-linux.png
frontend/e2e/visual.spec.ts-snapshots/settings-firefox-linux.png
frontend/e2e/visual.spec.ts-snapshots/research-answer-alt-theme-firefox-linux.png
frontend/e2e/visual.spec.ts-snapshots/research-answer-firefox-linux.png
frontend/e2e/visual.spec.ts
frontend/e2e/deep-research.spec.ts
frontend/e2e/settings.spec.ts
frontend/e2e/build.spec.ts
frontend/e2e/research.spec.ts
frontend/playwright.config.ts
packages/core/src/disco/core/store/sqlite.py
packages/core/src/disco/core/store/base.py
packages/core/src/disco/core/store/__init__.py
frontend/src/types/agent.ts
frontend/src/types/grounded.ts
frontend/src/types/models.ts
frontend/src/types/project.ts
frontend/src/types/config.ts
frontend/src/types/conversation.ts
frontend/src/types/activity.ts
frontend/src/types/sandbox.ts
frontend/src/hooks/useConversations.ts
frontend/src/hooks/useResearchStream.ts
frontend/src/shell/NavRail.tsx
frontend/src/shell/NavRail.test.tsx
frontend/src/shell/Shell.tsx
frontend/src/shell/ModeSlider.tsx
frontend/src/shell/Shell.test.tsx
frontend/src/shell/mode.ts
frontend/src/shell/mode.test.ts
frontend/src/shell/ModeProvider.tsx
frontend/src/main.tsx
frontend/docker-entrypoint.d/40-disco-env.sh
frontend/nginx.conf
packages/agent-server/scripts/verify_browser_injection.py
packages/agent-server/scripts/verify_plan_approve.py
packages/agent-server/scripts/verify_build_proof_of_life.py
packages/agent-server/scripts/verify_replan_acceptance.py
frontend/scripts/shots_ab.mjs
frontend/scripts/screenshots.mjs
frontend/refs-dark.mjs
frontend/src/hooks/useBuild.ts
frontend/src/hooks/useBuildStream.ts
frontend/src/hooks/useDeepResearch.ts
frontend/src/hooks/useModels.ts
frontend/src/hooks/useTemplates.ts
frontend/src/hooks/useResearch.ts
frontend/src/hooks/useSecrets.ts
frontend/src/hooks/useDeepResearchStream.ts
frontend/src/hooks/useElementSelect.ts
frontend/src/hooks/useDriverModels.ts
frontend/src/hooks/useDeepResearchStream.test.ts
frontend/src/hooks/useProjectsConfig.ts
frontend/src/hooks/useBuildNotifications.ts
frontend/src/hooks/useBuildNotifications.test.ts
frontend/src/hooks/useActivity.ts
frontend/src/hooks/useExportCapabilities.ts
frontend/src/hooks/useSessions.test.ts
frontend/src/hooks/useSessions.ts
frontend/src/hooks/useBuildStream.suspend.test.ts
frontend/src/hooks/useProjects.ts
frontend/src/hooks/useBuildStream.askgate.test.ts
frontend/src/hooks/useConfig.ts
frontend/src/hooks/useBuildPreview.ts
frontend/src/components/SourcePanel.test.tsx
frontend/src/components/ThemeToggle.tsx
frontend/src/components/FollowUps.tsx
frontend/src/components/TtftIndicator.tsx
frontend/src/components/SourcePanel.tsx
frontend/src/components/ThinkToggle.tsx
frontend/src/components/CapabilityBadges.tsx
frontend/src/components/Citation.test.tsx
frontend/src/components/mainSurface.test.tsx
frontend/src/components/ExampleQueries.tsx
frontend/refs/manus-dark.png
frontend/refs/perplexity-dark.png
frontend/refs/manus-ai.png
frontend/refs/manus.png
frontend/refs/perplexity.png
frontend/refs.mjs
frontend/e2e-live/bp-06-feed-unmasked.spec.ts
frontend/e2e-live/bp-03-contract.spec.ts
frontend/e2e-live/preview-truth.spec.ts
frontend/e2e-live/shell-sessions.spec.ts
frontend/playwright.live.config.ts
frontend/e2e-live/bp-04-browser.spec.ts
frontend/e2e-live/bp-08-kernel.spec.ts
frontend/e2e-live/bp-10-port-pills.spec.ts
frontend/e2e-live/bp-00-vision.spec.ts
frontend/e2e-live/bp-05-verify-gate.spec.ts
frontend/e2e-live/bp-09-install.spec.ts
frontend/e2e-live/dc-01-host-preview.spec.ts
frontend/e2e-live/bp-15-screenshots.spec.ts
frontend/e2e-live/bp-14-terminal.spec.ts
frontend/e2e-live/bp-13-suspend.spec.ts
frontend/e2e-live/bp-12-resume.spec.ts
frontend/e2e-live/bp-11-upload.spec.ts
frontend/src/lib/e2eBridge.ts
frontend/src/lib/buildTrace.replay.test.ts
frontend/src/lib/buildTrace.test.ts
frontend/src/lib/buildTrace.ts
frontend/src/lib/deepResearchTrace.ts
frontend/src/lib/slidesRenderer.ts
frontend/src/lib/selectionAgent.ts
packages/tools/pyproject.toml
frontend/src/api/agent.ts
frontend/src/api/models.ts
frontend/src/api/deepResearch.test.ts
frontend/src/api/deepResearch.ts
frontend/src/api/research.ts
frontend/src/api/secrets.ts
frontend/src/api/projects.ts
frontend/src/api/client.ts
frontend/src/api/config.ts
frontend/src/api/activity.ts
frontend/src/api/agent.upload.test.ts
frontend/src/api/agent.reconnect.test.ts
frontend/src/api/conversations.ts
frontend/src/lib/resolvers/appResolver.test.ts
frontend/src/lib/resolvers/appResolver.ts
frontend/src/lib/resolvers/deckResolver.test.ts
frontend/src/lib/resolvers/deckResolver.ts
frontend/src/lib/selectionBridge.ts
frontend/src/lib/selectionBridge.test.ts
frontend/src/lib/srcdoc.test.ts
frontend/src/lib/scheduleNL.test.ts
frontend/src/lib/scheduleNL.ts
frontend/src/lib/eventDisposition.test.ts
frontend/src/lib/eventDisposition.ts
frontend/src/lib/useReplay.test.ts
frontend/src/lib/useReplay.ts
frontend/src/lib/cost.test.ts
frontend/src/lib/useTheme.test.ts
frontend/src/lib/isolation.test.ts
frontend/src/lib/isolation.ts
frontend/src/lib/date.ts
frontend/src/lib/cost.ts
frontend/src/lib/usePointer.ts
frontend/src/lib/sources.ts
frontend/src/lib/useTheme.ts
packages/tools/scripts/verify_agent_tools_local.py
frontend/src/lib/cn.ts
packages/tools/scripts/verify_b7_browser_errors.py
packages/tools/scripts/verify_shell_sessions_gvisor.py
packages/tools/scripts/verify_preview_gvisor.py
packages/tools/scripts/verify_podman_vm202.py
packages/tools/scripts/verify_local_socket.py
packages/tools/scripts/verify_gvisor_vm201.py
frontend/src/fixtures/models.ts
frontend/src/fixtures/conversations.ts
frontend/src/fixtures/agentTrace.ts
frontend/src/fixtures/answers.ts
frontend/src/fixtures/deepResearchTrace.ts
frontend/src/fixtures/config.ts
frontend/src/components/settings/ProjectStorageSection.tsx
frontend/src/components/settings/SkillsSection.tsx
frontend/src/components/settings/McpSection.live.test.tsx
frontend/src/components/settings/ModelMatrix.tsx
frontend/src/components/settings/EncoderSection.tsx
frontend/src/components/settings/SandboxSection.tsx
frontend/src/components/settings/OpenRouterSection.tsx
frontend/src/components/settings/McpSection.tsx
frontend/src/components/settings/PendingBadge.tsx
frontend/src/components/settings/NotWired.tsx
frontend/src/components/settings/DataSourcesSection.tsx
frontend/src/components/settings/RoleModelPicker.tsx
frontend/src/components/settings/ProjectStorageSection.test.tsx
frontend/src/components/settings/McpSection.approval.test.tsx
frontend/src/components/settings/ModelCatalogue.tsx
frontend/src/components/settings/AudioSection.tsx
frontend/src/components/settings/ScheduleSection.tsx
frontend/src/components/settings/DataSourcesSection.test.tsx
frontend/src/components/settings/ProviderKeysSection.tsx
frontend/src/components/settings/ProviderKeysSection.test.tsx
frontend/src/components/settings/LiveBrowserSection.tsx
frontend/src/components/QueryInput.tsx
frontend/src/components/BuildSurface.tsx
frontend/src/components/StaticRunView.tsx
frontend/src/components/ResearchSurface.tsx
frontend/src/components/AgentSurface.tsx
frontend/src/components/ResearchSurface.attach.test.tsx
frontend/src/components/AnswerDocument.test.tsx
frontend/src/components/CommandPalette.tsx
frontend/src/components/CommandPalette.test.tsx
frontend/src/components/Markdown.chart.test.tsx
frontend/src/components/ResearchSurface.test.tsx
frontend/src/components/states.test.tsx
frontend/src/components/states.tsx
frontend/src/components/controls.test.tsx
frontend/src/components/SheetBlock.test.tsx
frontend/src/components/ChartBlock.test.tsx
frontend/src/components/Markdown.test.tsx
frontend/src/components/BuildSurface.test.tsx
frontend/src/components/Toast.test.tsx
frontend/src/components/Toast.tsx
frontend/src/components/settings/ImageGenSection.tsx
frontend/src/components/settings/ImageGenSection.test.tsx
frontend/src/styles/theme.css
frontend/src/test/setup.ts
frontend/eslint.config.js
frontend/tsconfig.node.json
frontend/tsconfig.json
frontend/src/components/ScopeControl.tsx
frontend/src/components/brand/Wordmark.tsx
frontend/src/components/brand/DefinitionMark.tsx
frontend/src/components/brand/DefinitionMark.test.tsx
frontend/src/components/AnswerDocument.tsx
frontend/src/components/ModelLeaderPill.tsx
frontend/src/components/Markdown.tsx
frontend/src/components/blocks.tsx
packages/tools/tests/test_gvisor.py
packages/tools/tests/test_egress_proxy.py
packages/tools/tests/test_egress_mode.py
packages/tools/tests/test_cluster4_filesystem.py
frontend/src/components/blocks/SlidesBlock.tsx
frontend/src/components/blocks/ChartBlock.tsx
frontend/src/components/blocks/BlockView.tsx
frontend/src/components/blocks/SheetBlock.tsx
frontend/src/components/blocks/TableView.tsx
frontend/src/components/blocks/CodeBlock.tsx
frontend/src/components/blocks/CitedText.tsx
frontend/src/components/SlidesBlock.test.tsx
frontend/src/components/DemoDataBadge.test.tsx
frontend/src/components/DemoDataBadge.tsx
frontend/src/components/icons/CodeBlocksIcon.tsx
packages/tools/tests/test_podman.py
packages/tools/tests/test_mcp_tool_search.py
packages/tools/tests/test_mcp_naming.py
frontend/src/components/ConfirmDialog.tsx
packages/tools/tests/test_mcp_approval.py
packages/tools/tests/test_local.py
packages/tools/tests/test_inclusive_editing.py
frontend/src/components/research/DepthTierSelector.tsx
frontend/src/components/research/TieredSourcePanel.tsx
frontend/src/components/PathPickerDialog.tsx
packages/tools/tests/test_audio_mixer_robustness.py
packages/tools/tests/test_sandbox_wedge_and_hotapply.py
packages/tools/tests/test_kernel_idle_cull.py
packages/tools/tests/test_tool_markdown.py
packages/tools/tests/test_f7_pressure_aware_head_only.py
packages/tools/tests/test_multi_service_tracking.py
packages/tools/tests/test_preview_session.py
packages/tools/tests/test_rematerialize_servers.py
packages/agent-server/src/disco/agent_server/__main__.py
packages/agent-server/src/disco/agent_server/sessions_service.py
packages/agent-server/src/disco/agent_server/schedule_service.py
packages/agent-server/src/disco/agent_server/share_service.py
packages/agent-server/src/disco/agent_server/runtime.py
packages/agent-server/tests/test_disco_verify_runner.py
packages/agent-server/tests/test_supervision_guardrail.py
packages/agent-server/tests/test_debug_evidence_route.py
frontend/src/components/research/ReportFollowUp.tsx
frontend/src/components/research/NeedMoreCard.tsx
frontend/src/components/research/DeepResearchSurface.tsx
frontend/src/components/research/DeepBoundedNotice.tsx
frontend/src/components/research/NeedMoreCard.test.tsx
frontend/src/components/research/TemplatePicker.tsx
frontend/src/components/research/DeepResearchSurface.test.tsx
frontend/src/components/research/IterativeToggle.tsx
frontend/src/components/research/DeepResearchSurface.attach.test.tsx
frontend/src/components/research/DeepReportView.tsx
frontend/src/components/research/DeepReportView.table.test.tsx
frontend/src/components/research/RecencySelector.tsx
frontend/src/components/research/IncludeFollowUpsModal.tsx
frontend/src/components/research/IncludeFollowUpsModal.test.tsx
frontend/src/components/research/FollowUpStatus.test.tsx
frontend/src/components/research/FollowUpStatus.tsx
frontend/src/components/research/AudioPlayer.test.tsx
frontend/src/components/research/AudioPlayer.tsx
frontend/src/components/research/DeepProgressStrip.tsx
frontend/src/components/research/DeepResearchSurface.resumed.test.tsx
frontend/src/components/research/ClaimVerdicts.test.tsx
frontend/src/components/research/SupportMeter.test.tsx
frontend/src/components/research/ClaimVerdicts.tsx
frontend/src/components/research/SupportMeter.tsx
packages/agent-server/src/disco/agent_server/runtime_model_probe.py
packages/agent-server/src/disco/agent_server/schedule.py
packages/agent-server/src/disco/agent_server/__init__.py
packages/agent-server/src/disco/agent_server/schedule_models.py
packages/agent-server/src/disco/agent_server/redaction.py
packages/agent-server/src/disco/agent_server/routes/mcp.py
packages/agent-server/src/disco/agent_server/routes/health.py
packages/agent-server/src/disco/agent_server/share_viewer.py
packages/tools/src/disco/tools/secrets.py
packages/agent-server/src/disco/agent_server/routes/activity.py
packages/agent-server/src/disco/agent_server/routes/storage.py
packages/agent-server/src/disco/agent_server/verify/scenarios.py
packages/agent-server/src/disco/agent_server/verify/scenarios_run.py
packages/agent-server/src/disco/agent_server/verify/__main__.py
packages/agent-server/src/disco/agent_server/verify/__init__.py
packages/agent-server/src/disco/agent_server/verify/_setup_checks.py
packages/agent-server/src/disco/agent_server/verify/runner.py
packages/agent-server/src/disco/agent_server/verify/schema.py
packages/agent-server/src/disco/agent_server/runtime_settings.py
packages/agent-server/src/disco/agent_server/title_service.py
packages/agent-server/src/disco/agent_server/resume_service.py
packages/agent-server/src/disco/agent_server/control_ops.py
packages/agent-server/src/disco/agent_server/lifecycle.py
packages/agent-server/src/disco/agent_server/deep_research_service.py
packages/agent-server/src/disco/agent_server/report_export.py
packages/agent-server/src/disco/agent_server/preview_service.py
packages/agent-server/src/disco/agent_server/app.py
packages/agent-server/src/disco/agent_server/selection_agent.js
packages/agent-server/src/disco/agent_server/oid_stamp.py
packages/agent-server/src/disco/agent_server/report_audio.py
packages/agent-server/src/disco/agent_server/audio_config.py
packages/agent-server/src/disco/agent_server/uploads_ingest.py
packages/agent-server/src/disco/agent_server/reference.docx
packages/agent-server/src/disco/agent_server/tts_local.py
packages/agent-server/src/disco/agent_server/mcp_manager.py
packages/agent-server/src/disco/agent_server/host_proxy.py
packages/agent-server/tests/test_projects_endpoints.py
packages/agent-server/tests/test_override_persistence.py
packages/agent-server/tests/test_mcp_http.py
packages/agent-server/tests/test_share.py
packages/agent-server/tests/test_redaction.py
packages/agent-server/tests/test_mcp_fence.py
packages/agent-server/tests/test_host_proxy.py
packages/agent-server/tests/test_memory_release.py
packages/agent-server/tests/test_verify.py
packages/agent-server/tests/test_d10_unified_projection.py
packages/agent-server/tests/test_host_proxy_C2.py
packages/agent-server/tests/test_research_ws_B3.py
packages/agent-server/tests/test_probe_ttl.py
packages/agent-server/tests/test_atomic_sidecar.py
packages/agent-server/tests/test_live_probe_nonblocking.py
packages/agent-server/tests/test_wire.py
packages/agent-server/tests/test_tts_local.py
packages/agent-server/tests/test_share_import.py
packages/agent-server/tests/test_sessions_routes.py
packages/agent-server/tests/test_sessions_degrade.py
packages/agent-server/tests/test_schedule_e2e.py
packages/agent-server/tests/test_schedule_coalesce.py
packages/agent-server/tests/test_resume_condensation.py
packages/agent-server/tests/test_report_followup.py
packages/agent-server/tests/test_model_pick_pipeline.py
packages/agent-server/tests/test_mcp_retrieval_tier.py
packages/agent-server/tests/test_mcp_egress.py
packages/agent-server/tests/test_lifecycle.py
packages/agent-server/tests/test_idle_suspend.py
packages/agent-server/tests/test_health.py
packages/agent-server/tests/test_agent_surface.py
packages/agent-server/tests/test_activity.py
packages/agent-server/tests/test_report_export_pdf_fixes.py
packages/agent-server/tests/test_report_export.py
packages/agent-server/tests/test_report_audio_live.py
packages/agent-server/tests/test_report_audio.py
packages/agent-server/tests/test_preview_wake.py
packages/agent-server/tests/test_p3_sticky_model.py
packages/agent-server/tests/test_inspect_trace.py
packages/agent-server/tests/test_dr1_dr3.py
packages/agent-server/tests/test_artifact_mode.py
packages/agent-server/tests/test_upload.py
packages/agent-server/tests/test_datasource_attach.py
packages/agent-server/tests/test_artifact_route.py
packages/agent-server/tests/test_workspace_route.py
packages/agent-server/tests/test_vision_probe_startup.py
packages/agent-server/tests/test_research.py
packages/agent-server/tests/test_build_surface.py
packages/agent-server/tests/test_resume.py
packages/agent-server/tests/test_sandbox_config.py
packages/agent-server/tests/test_mcp_pool.py
packages/agent-server/tests/test_mcp_e6_new_hash.py
packages/agent-server/tests/test_mcp_poisoning.py
packages/agent-server/tests/test_control_ops_ship_it.py
packages/agent-server/tests/test_stale_policy.py
packages/agent-server/tests/test_policy_consistency.py
packages/agent-server/tests/test_mcp_tool_search_cap.py
packages/agent-server/tests/test_report_export_brand.py
packages/agent-server/tests/test_secret_overlay.py
packages/agent-server/tests/test_novnc_live.py
packages/agent-server/tests/test_runtime.py
packages/agent-server/tests/test_preview_edit_route.py
packages/agent-server/src/disco/agent_server/routes/debug.py
packages/agent-server/tests/test_oid_stamp.py
packages/agent-server/src/disco/agent_server/routes/projects.py
packages/agent-server/tests/test_upload_corpus.py
packages/agent-server/src/disco/agent_server/routes/deck_editor.py
packages/agent-server/src/disco/agent_server/routes/report.py
packages/agent-server/src/disco/agent_server/routes/ws.py
packages/agent-server/src/disco/agent_server/routes/conversations.py
packages/agent-server/src/disco/agent_server/routes/_common.py
packages/agent-server/src/disco/agent_server/routes/preview.py
packages/agent-server/src/disco/agent_server/routes/files.py
packages/agent-server/src/disco/agent_server/routes/export.py
packages/agent-server/src/disco/agent_server/routes/preview_edit.py
packages/agent-server/src/disco/agent_server/routes/__init__.py
packages/agent-server/src/disco/agent_server/routes/schedules.py
packages/agent-server/src/disco/agent_server/routes/models.py
packages/agent-server/src/disco/agent_server/routes/share.py
packages/agent-server/src/disco/agent_server/routes/sessions.py
packages/agent-server/tests/integration/test_task_supervision.py
packages/agent-server/tests/integration/scripted_model.py
packages/agent-server/tests/integration/test_scripted_smoke.py
packages/agent-server/tests/integration/harness.py
packages/agent-server/tests/integration/__init__.py
packages/tools/tests/test_preview.py
packages/tools/tests/test_e4_default_projects_root.py
packages/tools/tests/test_deck_schema.py
packages/tools/tests/test_c8_chart_layouts.py
packages/tools/tests/test_builtin_extra.py
packages/tools/tests/tool_fakes.py
packages/tools/tests/test_browser_daemon.py
frontend/src/components/build/ConnectionsStrip.tsx
packages/tools/tests/test_kernel_gateway_auth.py
packages/tools/tests/test_mcp_transport.py
frontend/src/components/build/ActivityFeed.sheet.test.tsx
packages/tools/tests/mcp_fakes.py
frontend/src/components/build/AgentStatusBar.cost.test.tsx
packages/tools/tests/test_projects.py
packages/tools/tests/test_audio_overview_three_modes.py
frontend/src/components/build/ActivityFeed.screenshot.test.tsx
packages/tools/tests/test_kernel_session.py
frontend/src/components/build/StreamingFile.test.tsx
packages/tools/tests/test_audio_overview_integration.py
frontend/src/components/build/ExecutionCanvas.terminal.test.tsx
packages/tools/tests/test_server_status.py
frontend/src/components/build/AgentStatusBar.suspend.test.tsx
packages/tools/tests/test_e8_filtered_egress_proxy_wiring.py
frontend/src/components/build/AgentStatusBar.resume.test.tsx
packages/tools/tests/test_bp_g10_filtered_default.py
frontend/src/components/build/BuildSurface.upload.test.tsx
packages/tools/tests/test_sweep_stale_roots.py
frontend/src/components/build/cluster6.test.tsx
packages/tools/tests/test_shell_sessions.py
frontend/src/components/build/AskPanel.test.tsx
frontend/src/components/build/LiveSignalBar.tsx
frontend/src/components/build/ResizableSplit.tsx
frontend/src/components/Citation.tsx
packages/tools/tests/test_agent_tools.py
packages/tools/tests/test_root2_workspace_path.py
packages/tools/tests/test_slides_pipeline.py
packages/tools/tests/test_pptx_render.py
packages/tools/tests/test_live_view.py
packages/tools/tests/test_shell_spill.py
packages/tools/tests/test_session_diagnostics.py
packages/tools/tests/test_security.py
packages/tools/tests/test_scope_advertise_split.py
packages/tools/tests/test_sandbox.py
packages/tools/tests/test_image_gen_binary.py
packages/tools/tests/test_executor_scope.py
packages/tools/tests/test_executor_assist.py
packages/tools/tests/test_deck_patch.py
packages/tools/tests/test_browser.py
packages/tools/tests/test_audio_overview_tool.py
packages/tools/tests/test_think_tool.py
packages/tools/tests/test_slides.py
packages/tools/tests/test_sheets.py
packages/tools/tests/test_planner_readonly.py
packages/tools/tests/test_policy_tool_surface.py
packages/tools/tests/test_tools_integration.py
packages/tools/tests/test_edit_guards.py
packages/tools/tests/test_w3_w4_edit_safety.py
packages/tools/src/disco/tools/verify/heavy_validators.py
packages/tools/src/disco/tools/verify/artifact_validators.py
packages/tools/src/disco/tools/verify/__init__.py
packages/tools/src/disco/tools/executor.py
packages/tools/src/disco/tools/anatomy.py
packages/tools/src/disco/tools/registry.py
packages/tools/src/disco/tools/__init__.py
frontend/src/components/build/SteerInput.tsx
frontend/src/components/build/PlanPanel.tsx
frontend/src/components/build/DeliverablePanel.tsx
frontend/src/components/build/ConfirmationPanel.tsx
packages/tools/src/disco/tools/sandbox/podman.py
frontend/src/components/build/ClarifyPanel.tsx
frontend/src/components/build/BuildSurface.tsx
packages/tools/src/disco/tools/sandbox/base.py
frontend/src/components/build/AskPanel.tsx
frontend/src/components/build/AlternativesGate.tsx
frontend/src/components/build/AgentStatusBar.tsx
packages/tools/src/disco/tools/sandbox/_container.py
frontend/src/components/build/DeckEditorPane.tsx
frontend/src/components/build/DeckEditorPane.test.tsx
packages/tools/src/disco/tools/sandbox/process.py
frontend/src/components/build/ActivityFeed.tsx
packages/tools/src/disco/tools/sandbox/session.py
frontend/src/components/build/AgentCanvas.live.test.tsx
packages/tools/src/disco/tools/sandbox/kernel.py
frontend/src/components/build/AgentCanvas.tsx
packages/tools/src/disco/tools/sandbox/egress_proxy.py
frontend/src/components/build/ExecutionCanvas.tsx
packages/tools/src/disco/tools/sandbox/shell_sessions.py
frontend/src/components/build/latestEditableDeckBase.test.ts
packages/tools/src/disco/tools/sandbox/port_owner.py
frontend/src/components/build/AgentCanvas.test.tsx
packages/tools/src/disco/tools/sandbox/local.py
packages/tools/src/disco/tools/sandbox/config.py
packages/tools/src/disco/tools/sandbox/gvisor.py
packages/tools/src/disco/tools/sandbox/isolation.py
packages/tools/src/disco/tools/sandbox/naming.py
packages/tools/tests/test_sandbox_exception_contract.py
packages/tools/src/disco/tools/sandbox/__init__.py
packages/tools/tests/test_heavy_validators.py
packages/tools/tests/test_deck_validators.py
packages/agent-server/tests/test_deck_editor_route.py
packages/tools/tests/test_sandbox_read_guardrail.py
packages/agent-server/tests/test_resume_reconstruction.py
packages/tools/tests/test_slides_procedural_contract.py
packages/agent-server/tests/test_title_service.py
packages/tools/tests/test_artifact_validators.py
packages/agent-server/tests/test_assist_tier.py
frontend/src/components/build/BuildModelPicker.p3.test.tsx
packages/tools/tests/test_validation_executor.py
packages/tools/tests/test_f3_read_before_write.py
frontend/src/components/build/ReplayScrubber.tsx
frontend/src/components/build/ExecutionCanvas.cockpit.test.tsx
packages/agent-server/tests/fixtures/negative/procedural_placeholder.png
packages/agent-server/tests/fixtures/negative/procedural_image_deck.html
frontend/src/components/build/ExecutionCanvas.preview.test.tsx
frontend/src/components/build/BuildModelPicker.tsx
frontend/src/components/build/DeliverablePanel.test.tsx
packages/tools/src/disco/tools/mcp/naming.py
packages/tools/src/disco/tools/mcp/__init__.py
packages/agent-server/tests/fixtures/negative/raw_html_deck_deliverable.json
packages/tools/src/disco/tools/mcp/approval.py
packages/tools/src/disco/tools/mcp/http_egress.py
packages/agent-server/tests/fixtures/negative/no_plan_progress_finished.json
packages/tools/src/disco/tools/mcp/fence.py
packages/agent-server/tests/fixtures/negative/README.md
frontend/src/components/build/editor/LayersPanel.tsx
frontend/src/components/build/editor/DeckEditor.test.tsx
packages/tools/src/disco/tools/projects/archive.py
packages/tools/src/disco/tools/builtin/retrieval.py
packages/tools/src/disco/tools/builtin/_audio_mixer.py
packages/tools/src/disco/tools/builtin/system.py
frontend/src/components/build/editor/DeckEditor.tsx
packages/tools/src/disco/tools/mcp/tool_search.py
packages/tools/src/disco/tools/mcp/config.py
packages/tools/src/disco/tools/projects/__init__.py
packages/tools/src/disco/tools/mcp/http.py
packages/tools/src/disco/tools/builtin/think.py
packages/tools/src/disco/tools/mcp/pool.py
packages/tools/src/disco/tools/mcp/stdio.py
frontend/src/components/build/editor/ElementBox.test.tsx
packages/tools/src/disco/tools/projects/store.py
frontend/src/components/build/canvas/CockpitPane.tsx
frontend/src/components/build/canvas/TerminalPane.tsx
frontend/src/components/build/canvas/Empty.tsx
packages/tools/src/disco/tools/mcp/migrations.py
frontend/src/components/build/canvas/SelectionOverlay.test.tsx
frontend/src/components/build/canvas/FilesPane.tsx
packages/tools/src/disco/tools/builtin/subagent.py
packages/tools/src/disco/tools/mcp/retrieval_tier.py
frontend/src/components/build/canvas/SelectionOverlay.tsx
frontend/src/components/build/canvas/EditAffordance.tsx
frontend/src/components/build/editor/types.ts
frontend/src/components/build/editor/ElementBox.tsx
frontend/src/components/build/canvas/EditAffordance.test.tsx
frontend/src/components/build/editor/SlideCanvas.test.tsx
frontend/src/components/build/editor/SlideCanvas.tsx
packages/tools/src/disco/tools/builtin/shell_sessions.py
packages/tools/src/disco/tools/builtin/server.py
packages/tools/src/disco/tools/builtin/sheets.py
packages/tools/src/disco/tools/builtin/audio_overview.py
packages/tools/src/disco/tools/builtin/browser.py
frontend/src/components/build/canvas/PreviewPane.edit.test.tsx
frontend/src/components/build/canvas/PreviewPane.tsx
packages/tools/src/disco/tools/builtin/_browser_daemon.py
packages/tools/src/disco/tools/builtin/_c8_chart_layouts.py
packages/tools/src/disco/tools/builtin/live_view.py
packages/tools/src/disco/tools/builtin/_deck_schema.py
packages/tools/tests/fixtures/artifacts/sample_report.pdf
packages/tools/src/disco/tools/builtin/image_gen.py
packages/tools/src/disco/tools/builtin/_deck_patch.py
packages/tools/src/disco/tools/builtin/plan.py
packages/tools/src/disco/tools/builtin/__init__.py
packages/tools/src/disco/tools/builtin/files.py
packages/tools/src/disco/tools/builtin/_slides_pipeline.py
packages/tools/src/disco/tools/builtin/_pptx_render.py
packages/tools/src/disco/tools/builtin/slides.py

exec
/bin/bash -lc 'rg -n "SandboxFileNotFoundError|looks_like_not_found|raise_read_error|_on_run_task_done|_terminalize_crashed|pil-procedural|validate_deck_deliverable|_looks_procedural|DISCO_INSPECT|deriveBuildProgress|data-disco-control|evidence-harness" .' in /var/home/dylan/projects/disco
 succeeded in 0ms:
./CLAUDE.md:151:## Proving the chain end-to-end (DISCO_INSPECT)
./CLAUDE.md:154:logs, set `DISCO_INSPECT=1`. The runtime then captures, per conversation, every
./frontend/e2e-full/scenarios/click-provenance.smoke.spec.ts:8: * The 10 evidence records (from evidence-harness-campaign.md):
./frontend/e2e-full/scenarios/click-provenance.smoke.spec.ts:15: *  7  DISCO_INSPECT trace          → conversations/<cid>/inspect-trace.json (live backend)
./frontend/e2e-full/scenarios/click-provenance.smoke.spec.ts:26: * evidence-harness-campaign.md W12
./frontend/e2e-full/scenarios/click-provenance.smoke.spec.ts:139:          : page.locator(`[data-disco-control="${clickTarget.controlId.replace("disco:", "")}"]`);
./frontend/e2e-full/scenarios/smoke.full.spec.ts:15: * evidence-harness-campaign.md W11
./frontend/e2e-full/support/assertions.ts:9: *   expectInspectTrace    — router/span truth: DISCO_INSPECT trace meets minimums
./frontend/e2e-full/support/assertions.ts:15: * evidence-harness-campaign.md W12
./frontend/e2e-full/support/assertions.ts:175: * Assert that the DISCO_INSPECT router/span trace for `cid` meets minimum
./frontend/e2e-full/support/assertions.ts:179: * This endpoint is only populated when the server runs with DISCO_INSPECT=1;
./frontend/e2e-full/support/uiInventory.ts:7: *   backend-command  Has data-disco-control OR matches known action names →
./frontend/e2e-full/support/uiInventory.ts:20: * evidence-harness-campaign.md W12
./frontend/e2e-full/support/uiInventory.ts:54:   * Stable identifier: data-disco-control → data-testid → kebab(name+role).
./frontend/e2e-full/support/uiInventory.ts:110: *  2. has data-disco-control → "backend-command"
./frontend/e2e-full/support/uiInventory.ts:210:  "[data-disco-control]",
./frontend/e2e-full/support/uiInventory.ts:242:        discoControl: el.getAttribute("data-disco-control"),
./frontend/e2e-full/support/discoClick.ts:9: * evidence-harness-campaign.md W12
./frontend/e2e-full/support/discoClick.ts:206:  // Selector slug: data-testid → data-disco-control → aria-label → tag name
./frontend/e2e-full/support/discoClick.ts:208:  const discoControl = await locator.getAttribute("data-disco-control");
./frontend/e2e-full/support/artifactCollector.ts:6: * silently (e.g. /api/debug/trace/{cid} is inert without DISCO_INSPECT=1).
./frontend/e2e-full/support/artifactCollector.ts:9: * evidence-harness-campaign.md W11
./frontend/e2e-full/support/artifactCollector.ts:29: * Mirrors the list in evidence-harness-campaign.md §"Three-layer harness".
./frontend/e2e-full/support/wsRecorder.ts:9: * evidence-harness-campaign.md W11
./frontend/e2e-full/support/recorder.ts:7: * evidence-harness-campaign.md W11
./docs/disco-evidence-harness-campaign.md:13:2. **Synthesize** the three plans (codex round 1, codex round 2, the evidence-harness
./docs/disco-evidence-harness-campaign.md:50:# SOURCE PLAN 3 — the evidence-harness design (Dylan-provided)
./docs/disco-evidence-harness-campaign.md:56:`DISCO_INSPECT=1` (`debug.py:42`), artifact/download routes, stampable event `meta` dict.
./docs/disco-evidence-harness-campaign.md:80:captured · 5 event-log rows appended · 6 state transition observed · 7 DISCO_INSPECT
./docs/disco-evidence-harness-campaign.md:95:   `/api/debug/trace/{cid}` (DISCO_INSPECT=1), `/api/projects/{cid}/manifest`, `/sessions`,
./docs/disco-evidence-harness-campaign.md:97:   `/api/conversations/{cid}/report/export`. DISCO_INSPECT mandatory in full e2e mode.
./docs/disco-evidence-harness-campaign.md:113:- `live-full`: real agent-server, real model, real sandbox, DISCO_INSPECT=1. (Playwright
./docs/disco-evidence-harness-campaign.md:159:stable `data-disco-control` ids on controls lacking strong accessible names · 4 debug-only
./docs/disco-evidence-harness-campaign.md:162:`DISCO_E2E=1 DISCO_INSPECT=1`, never prod-default.
./docs/disco-evidence-harness-campaign.md:165:`e2e-full-fixture`, `e2e-full-live` (DISCO_INSPECT=1 DISCO_E2E=1 PMX_LOG_JSON=1),
./docs/disco-evidence-harness-campaign.md:767:evidence/UI layer is the evidence-harness design's four-truths + dossier + control-inventory
./docs/disco-evidence-harness-campaign.md:782:  map failures onto native types: missing file → `SandboxFileNotFoundError(SandboxError,
./docs/disco-evidence-harness-campaign.md:800:- **W5 [A] `/api/debug/evidence/{cid}` (gated DISCO_INSPECT/DISCO_E2E).** One endpoint
./docs/disco-evidence-harness-campaign.md:804:  controls}` + stable `data-disco-control` ids on controls lacking strong accessible names,
./docs/disco-evidence-harness-campaign.md:950:- W1 DONE (me): SandboxFileNotFoundError(SandboxError,FileNotFoundError) + raise_read_error boundary in _container/podman; PROVEN file_state's `except (FileNotFoundError,...)` now catches it (no more silent-hang crash); 10 tests green; test-first (RED→GREEN).
./docs/disco-evidence-harness-campaign.md:956:- W2 DONE (me): task-supervision contract in kick() — `_on_run_task_done` add_done_callback → `_terminalize_crashed` emits terminal ERROR + reminder on an uncaught loop exception; idempotent (won't clobber FINISHED/ERROR/STUCK/PAUSED); cancellation != error; IDLE-crash DOES surface. 3 tests, test-first RED→GREEN, runtime+integration regression clean. SILENT HANG NOW FULLY CLOSED (W1 trigger + W2 backstop).
./docs/disco-evidence-harness-campaign.md:958:- W9 DONE (sonnet, verified by me): 17 frontend event-replay tests (deriveBuildProgress/LiveSignal/Activity) incl. the #3 no-snapshot→all-done lock-in.
./docs/disco-evidence-harness-campaign.md:963:- W3 DONE (me): slides/artifact contract. (a) _stage_assets skips the procedural placeholder backend (name=="pil-procedural") → no garish placeholder art in decks; (b) slides default format html→pptx (presentable deliverable). 3 tests test-first RED→GREEN; slides regression clean (83 tests); ruff clean. Matches docs/dylans-runthru-3-6-21-26.md W3-2 root-fix prescription.
./docs/disco-evidence-harness-campaign.md:971:- W5 DONE (sonnet, VERIFIED): GET /api/debug/evidence/{cid} gated on DISCO_INSPECT, full redact() bundle, planted-secret redaction tested; 3 tests, ruff+pyright clean.
./docs/disco-evidence-harness-campaign.md:978:- W14a-deck DONE (me): validate_deck_file (extract html base64 / pptx ppt/media images → _looks_procedural) + validate_deck_deliverable (reject raw_html_default). _looks_procedural is SEED-INDEPENDENT (≤12 colours AND corner-to-corner diagonal — the backend's structural invariants), so it catches ANY procedural placeholder, not just the fixture; real noise image NOT flagged. 5 tests on the W18 fixtures, ruff+pyright clean.
./docs/disco-evidence-harness-campaign.md:990:- W7 DONE (boundary_contract marker, 20 tests/2.6s). W12 DONE+VERIFIED (disco.click/uiInventory/assertions/smoke; tsc+eslint clean). W6 DONE+VERIFIED (e2eBridge + 35 data-disco-control across surfaces; tsc clean; ATTRIBUTE-ONLY confirmed via NeedMoreCard diff → its vitest failures are pre-existing). W17 DONE (evidence-harness.yml: pr-evidence GH-hosted [boundary+validators+replay] + nightly-vm self-hosted [Playwright+W13+W14b]). W13 runner DONE (verify/scenarios_run.py).
./frontend/e2e-full/support/schema.ts:7: * evidence-harness-campaign.md W4
./docs/dylans-runthru-3-6-21-26.md:70:   exception: missing file → `class SandboxFileNotFoundError(SandboxError, FileNotFoundError)`,
./frontend/e2e-full/fixtures/discoHarness.ts:15: * evidence-harness-campaign.md W11
./docs/disco-plan-progress-source-unify.md:41:- **Frontend:** `deriveBuildProgress` (the LIVE PlanPanel path, from #3) already handles
./docs/disco-plan-progress-source-unify.md:82:  live `deriveBuildProgress` already handles update_plan_progress. Change UI only if actually wired.
./frontend/e2e-full/fixtures/README.md:17:| `no_plan_progress_finished.json` | FINISHED | **#3 regression** — small/local model (e.g. Qwen) finished without ever calling `update_plan_progress`; `deriveBuildProgress` pre-fix returned an empty map and showed 0/4 steps done on a build that actually completed. The terminal-reconciliation path MUST set all steps to `done`. |
./frontend/e2e-full/fixtures/README.md:18:| `running_then_error.json` | ERROR | A RUNNING build transitions to ERROR via a `SandboxFileNotFoundError`. Guards: `deriveLiveSignal` must return `idle` on ERROR (not a confusingly active signal); the active progress step must become `stalled`; the errored action must appear as `status=failed` in `deriveActivity`. |
./frontend/e2e-full/fixtures/README.md:20:| `finished_with_partial_plan_updates.json` | FINISHED | A capable-model build where `update_plan_progress` was called mid-build (step 1 done, step 2 active, step 3 pending) but NO final 100%-done snapshot was sent before FINISHED. `deriveBuildProgress` MUST reconcile all steps to `done` — the stale partial snapshot must not leave the UI showing "1/3 done" after a successful build. |
./frontend/e2e-full/fixtures/finished_with_partial_plan_updates.json:65:  "_why": "A partial update_plan_progress snapshot (step 1 done, step 2 active, step 3 pending) followed by FINISHED with NO final 100%-done snapshot. deriveBuildProgress(events,'FINISHED') MUST reconcile ALL 3 steps to done. Guards against the '1/3' stale-snapshot lie after a build that actually finished — the capable-model path where the model sent a mid-build snapshot but not a final one."
./frontend/e2e-full/fixtures/running_then_error.json:43:      "error": "SandboxFileNotFoundError: input.txt not found in workspace"
./frontend/e2e-full/fixtures/running_then_error.json:56:    "failed_action_error": "SandboxFileNotFoundError: input.txt not found in workspace"
./frontend/e2e-full/fixtures/no_plan_progress_finished.json:81:  "_why": "Small/local model finished without ever emitting update_plan_progress. deriveBuildProgress(events,'FINISHED') MUST reconcile all 4 steps to done. Pre-fix it returned an empty map (0/4)."
./docs/disco-consolidated-roadmap-6-18-26.md:97:  faithfulness scorer, `eval_runner`, DoD evaluator, `obs.py` spans, `DISCO_INSPECT`, `TokenUsage`);
./docs/disco-consolidated-roadmap-6-18-26.md:186:   task-success-rate (3-5 DoD build scenarios) + latency percentiles (DISCO_INSPECT) + cost table
./docs/AUTONOMOUS-FEATURE-CAMPAIGN.md:96:    `AgentCanvas` → A2.3 patch round-trip (gate on DISCO_INSPECT trace, not vitest).
./frontend/src/vite-env.d.ts:14:// evidence-harness-campaign.md W6 — the e2e bridge metadata object.
./frontend/src/App.tsx:197:          {/* W6: evidence-harness bridge — inert in production without opt-in */}
./frontend/src/views/HistoryView.tsx:147:              data-disco-control="import-share"
./frontend/src/views/HistoryView.tsx:268:                          data-disco-control="delete-conversation"
./frontend/src/components/QueryInput.tsx:110:          data-disco-control="send-message"
./packages/agent-server/tests/test_disco_verify_runner.py:459:                "error": "SandboxFileNotFoundError: /nonexistent_verifytest_file_99999.txt",
./packages/agent-server/tests/test_supervision_guardrail.py:22:    assert "_on_run_task_done" in src, "kick() must route task completion through the supervisor"
./packages/agent-server/tests/test_supervision_guardrail.py:27:    src = inspect.getsource(ConversationRuntime._terminalize_crashed)
./packages/agent-server/tests/test_debug_evidence_route.py:1:"""Tests for GET /api/debug/evidence/{conversation_id} — evidence-harness-campaign.md W5.
./packages/agent-server/tests/test_debug_evidence_route.py:5:1. Route is inert (404) when ``DISCO_INSPECT`` is off — the endpoint must never
./packages/agent-server/tests/test_debug_evidence_route.py:7:2. When ``DISCO_INSPECT=1``, the endpoint returns the expected four-key evidence
./packages/agent-server/tests/test_debug_evidence_route.py:57:    """Without DISCO_INSPECT=1, the route returns 404 with a hint, never data."""
./packages/agent-server/tests/test_debug_evidence_route.py:59:    monkeypatch.delenv("DISCO_INSPECT", raising=False)  # type: ignore[attr-defined]
./packages/agent-server/tests/test_debug_evidence_route.py:70:    assert "DISCO_INSPECT=1" in body.get("hint", "")
./packages/agent-server/tests/test_debug_evidence_route.py:79:    """With DISCO_INSPECT=1, the endpoint returns the full evidence bundle."""
./packages/agent-server/tests/test_debug_evidence_route.py:80:    monkeypatch.setenv("DISCO_INSPECT", "1")  # type: ignore[attr-defined]
./packages/agent-server/tests/test_debug_evidence_route.py:109:    monkeypatch.setenv("DISCO_INSPECT", "1")  # type: ignore[attr-defined]
./packages/agent-server/tests/integration/scripted_model.py:1:"""Scripted LLM provider — W8 evidence-harness integration layer.
./frontend/src/components/research/ReportFollowUp.tsx:70:          data-disco-control="follow-up"
./packages/agent-server/tests/fixtures/negative/no_plan_progress_finished.json:81:  "_why": "Small/local model finished without ever emitting update_plan_progress. deriveBuildProgress(events,'FINISHED') MUST reconcile all 4 steps to done. Pre-fix it returned an empty map (0/4)."
./frontend/src/components/research/NeedMoreCard.tsx:287:              data-disco-control="export-report-md"
./frontend/src/components/research/NeedMoreCard.tsx:319:              data-disco-control="export-report-pdf"
./frontend/src/components/research/NeedMoreCard.tsx:375:              data-disco-control="export-report-docx"
./frontend/src/components/research/NeedMoreCard.tsx:471:              data-disco-control="generate-audio"
./frontend/src/components/research/NeedMoreCard.tsx:496:              data-disco-control="generate-audio"
./frontend/src/components/research/NeedMoreCard.tsx:637:            data-disco-control="export-audio"
./frontend/src/components/research/NeedMoreCard.tsx:840:          data-disco-control="build-deck"
./frontend/src/components/research/DeepResearchSurface.tsx:206:                    data-disco-control="steer-research"
./frontend/src/components/research/DeepResearchSurface.tsx:213:                <button type="button" onClick={r.stop} data-disco-control="stop" className={CTRL_BTN}>
./frontend/src/components/research/DeepResearchSurface.tsx:217:                <button type="button" onClick={r.kill} data-disco-control="kill" className={KILL_BTN}>
./frontend/src/components/research/DeepResearchSurface.tsx:226:                <button type="button" onClick={r.resume} data-disco-control="resume" className={CTRL_BTN}>
./frontend/src/components/research/DeepResearchSurface.tsx:230:                <button type="button" onClick={r.kill} data-disco-control="kill" className={KILL_BTN}>
./frontend/src/components/research/DeepResearchSurface.tsx:238:              <button type="button" onClick={r.retry} data-disco-control="retry" className={CTRL_BTN}>
./frontend/src/components/research/DeepResearchSurface.tsx:248:                  data-disco-control="export-report-md"
./frontend/src/components/research/DeepResearchSurface.tsx:264:                  data-disco-control="export-report-pdf"
./frontend/src/components/research/DeepResearchSurface.tsx:284:                  data-disco-control="export-report-docx"
./frontend/src/components/research/DeepBoundedNotice.tsx:99:                data-disco-control="retry-exhaustive"
./frontend/src/components/build/SteerInput.tsx:45:        data-disco-control="steer"
./frontend/src/components/build/PlanPanel.tsx:238:                  data-disco-control="revise-plan"
./frontend/src/components/build/PlanPanel.tsx:257:                data-disco-control="approve-plan"
./docs/disco-surgical-plan-massive-swath-6-19-26.md:55:Riskiest: A2.3 (model must reliably emit `deck_patch` from the preamble; gate "done" on DISCO_INSPECT trace, not vitest). Pure-position drags have no `json_pointer` → scope out.
./docs/disco-surgical-plan-massive-swath-6-19-26.md:129:| B6.3 | default-deployment debug-endpoint 404 acceptance (`DISCO_INSPECT` unset) | **opus** | — | S |
./docs/disco-surgical-plan-massive-swath-6-19-26.md:133:| B2.3 | latency-percentile + cost aggregators (DISCO_INSPECT spans, TokenUsage) | **opus** | B2.1-2 | M |
./docs/disco-surgical-plan-massive-swath-6-19-26.md:160:6. Every UI task owes a **real Firefox-in-app screenshot** (§D4 discipline); "done" on A2.3/A4.2 gates on a DISCO_INSPECT/live trace, not green unit tests.
./frontend/src/components/build/DeliverablePanel.tsx:65:          data-disco-control="export-manifest"
./frontend/src/components/build/DeliverablePanel.tsx:94:          data-disco-control="download-artifact"
./frontend/src/components/build/DeliverablePanel.tsx:107:          data-disco-control={isApp ? "open-app" : "download-artifact"}
./packages/agent-server/tests/test_inspect_trace.py:1:"""DISCO_INSPECT per-conversation trace — Item 2.
./packages/agent-server/tests/test_inspect_trace.py:4:DISCO_INSPECT=1, a real Build conversation's model calls are captured as a
./packages/agent-server/tests/test_inspect_trace.py:142:    monkeypatch.setenv("DISCO_INSPECT", "1")
./packages/agent-server/tests/test_inspect_trace.py:149:    monkeypatch.delenv("DISCO_INSPECT", raising=False)
./packages/agent-server/tests/test_inspect_trace.py:162:    monkeypatch.setenv("DISCO_INSPECT", "1")
./packages/agent-server/tests/test_inspect_trace.py:213:    monkeypatch.delenv("DISCO_INSPECT", raising=False)
./packages/agent-server/tests/test_inspect_trace.py:226:    monkeypatch.setenv("DISCO_INSPECT", "1")
./frontend/src/components/build/ConfirmationPanel.tsx:78:          data-disco-control="reject-action"
./frontend/src/components/build/ConfirmationPanel.tsx:86:          data-disco-control="approve-action"
./frontend/src/components/build/ClarifyPanel.tsx:140:          data-disco-control="submit-clarify"
./frontend/src/components/build/BuildSurface.tsx:96:        data-disco-control="upload-files"
./frontend/src/components/build/AskPanel.tsx:77:          data-disco-control="answer-question"
./frontend/src/components/build/AlternativesGate.tsx:62:              data-disco-control="pick-alternative"
./frontend/src/components/build/AlternativesGate.tsx:97:            data-disco-control="pick-alternative"
./frontend/src/components/build/AgentStatusBar.tsx:190:            data-disco-control="resume"
./frontend/src/components/build/AgentStatusBar.tsx:206:            data-disco-control="stop"
./frontend/src/components/build/AgentStatusBar.tsx:230:              data-disco-control="kill-confirm"
./frontend/src/components/build/AgentStatusBar.tsx:251:            data-disco-control="kill"
./packages/agent-server/src/disco/agent_server/runtime.py:543:        # DISCO_INSPECT: attach the span-capture handler once, at construction, so
./packages/agent-server/src/disco/agent_server/runtime.py:723:        # DISCO_INSPECT: when on, bind a per-conversation routing sink so every
./packages/agent-server/src/disco/agent_server/runtime.py:1422:            lambda t, _cid=conversation_id: self._on_run_task_done(_cid, t)
./packages/agent-server/src/disco/agent_server/runtime.py:1436:    def _on_run_task_done(self, conversation_id: str, task: asyncio.Task[Any]) -> None:
./packages/agent-server/src/disco/agent_server/runtime.py:1452:            asyncio.create_task(self._terminalize_crashed(conversation_id, exc))
./packages/agent-server/src/disco/agent_server/runtime.py:1454:    async def _terminalize_crashed(
./packages/agent-server/src/disco/agent_server/verify/runner.py:108:        """GET /api/debug/trace/{cid} — None when DISCO_INSPECT is off or no trace."""
./packages/agent-server/src/disco/agent_server/verify/runner.py:386:    - ``trace.json``    — DISCO_INSPECT trace (if available), redacted
./frontend/src/hooks/useBuildStream.ts:13:  deriveBuildProgress,
./frontend/src/hooks/useBuildStream.ts:360:    () => deriveBuildProgress(state.events, state.status),
./packages/agent-server/src/disco/agent_server/routes/debug.py:1:"""DISCO_INSPECT debug routes — read the per-conversation request trace.
./packages/agent-server/src/disco/agent_server/routes/debug.py:4:→ back) without grepping prod logs. They are inert unless ``DISCO_INSPECT=1``:
./packages/agent-server/src/disco/agent_server/routes/debug.py:31:                "hint": "start the agent-server with DISCO_INSPECT=1 to capture traces",
./packages/agent-server/src/disco/agent_server/routes/debug.py:66:        Gated behind the same ``DISCO_INSPECT=1`` flag as ``/api/debug/trace``.
./frontend/src/lib/e2eBridge.ts:7: * evidence-harness-campaign.md W6
./frontend/src/lib/buildTrace.replay.test.ts:10: * plan steps done because `deriveBuildProgress` early-returned an empty map when there was
./frontend/src/lib/buildTrace.replay.test.ts:19:import { deriveBuildProgress, deriveActivity, deriveLiveSignal } from "@/lib/buildTrace";
./frontend/src/lib/buildTrace.replay.test.ts:40:// update_plan_progress → deriveBuildProgress returned empty map → 0/4 shown.
./frontend/src/lib/buildTrace.replay.test.ts:48:  it("deriveBuildProgress: ALL steps reconcile to done on FINISHED with no snapshot", () => {
./frontend/src/lib/buildTrace.replay.test.ts:52:    const p = deriveBuildProgress(evts, status);
./frontend/src/lib/buildTrace.replay.test.ts:64:  it("deriveBuildProgress returns empty map while still RUNNING (no premature checks)", () => {
./frontend/src/lib/buildTrace.replay.test.ts:66:    const p = deriveBuildProgress(evts, "RUNNING");
./frontend/src/lib/buildTrace.replay.test.ts:86:  it("deriveBuildProgress: active step becomes stalled on ERROR (honest about halted work)", () => {
./frontend/src/lib/buildTrace.replay.test.ts:87:    const p = deriveBuildProgress(evts, status);
./frontend/src/lib/buildTrace.replay.test.ts:156:  it("deriveBuildProgress: all steps done even though last snapshot was partial (not 1/3)", () => {
./frontend/src/lib/buildTrace.replay.test.ts:157:    const p = deriveBuildProgress(evts, status);
./frontend/src/lib/buildTrace.replay.test.ts:169:  it("deriveBuildProgress during RUNNING: partial snapshot is applied as-is (not prematurely all-done)", () => {
./frontend/src/lib/buildTrace.replay.test.ts:171:    const p = deriveBuildProgress(evts, "RUNNING");
./frontend/src/lib/buildTrace.replay.test.ts:194:  it("deriveBuildProgress: the active step becomes stalled (honest about unfinished work)", () => {
./frontend/src/lib/buildTrace.replay.test.ts:195:    const p = deriveBuildProgress(evts, status);
./frontend/src/lib/buildTrace.replay.test.ts:199:  it("deriveBuildProgress: the already-done step stays done (cancel does not regress done steps)", () => {
./frontend/src/lib/buildTrace.replay.test.ts:200:    const p = deriveBuildProgress(evts, status);
./frontend/src/lib/buildTrace.test.ts:10:import { deriveActivity, deriveBuildProgress, deriveDeliverable, deriveFiles } from "@/lib/buildTrace";
./frontend/src/lib/buildTrace.test.ts:285:describe("deriveBuildProgress — declarative full-state snapshot (#3)", () => {
./frontend/src/lib/buildTrace.test.ts:288:    expect(deriveBuildProgress(events, "RUNNING").size).toBe(0);
./frontend/src/lib/buildTrace.test.ts:307:    const p = deriveBuildProgress(events, "RUNNING");
./frontend/src/lib/buildTrace.test.ts:322:    expect(deriveBuildProgress(events, "RUNNING").size).toBe(0);
./frontend/src/lib/buildTrace.test.ts:328:    const p = deriveBuildProgress(events, "FINISHED");
./frontend/src/lib/buildTrace.test.ts:339:    const p = deriveBuildProgress([planEvent(1, 4)], "FINISHED");
./frontend/src/lib/buildTrace.test.ts:346:    expect(deriveBuildProgress([planEvent(1, 4)], "RUNNING").size).toBe(0);
./frontend/src/lib/buildTrace.test.ts:359:    expect(deriveBuildProgress(events, "RUNNING").size).toBe(0);
./frontend/src/lib/buildTrace.test.ts:367:    expect(deriveBuildProgress(events, "STUCK").get(1)).toBe("stalled");
./frontend/src/lib/buildTrace.ts:651:export function deriveBuildProgress(
./packages/tools/tests/test_sandbox_exception_contract.py:18:    SandboxFileNotFoundError,
./packages/tools/tests/test_sandbox_exception_contract.py:19:    looks_like_not_found,
./packages/tools/tests/test_sandbox_exception_contract.py:20:    raise_read_error,
./packages/tools/tests/test_sandbox_exception_contract.py:28:    e = SandboxFileNotFoundError("read_file 'x': cat: /workspace/x: No such file")
./packages/tools/tests/test_sandbox_exception_contract.py:46:def test_looks_like_not_found(stderr, expected):
./packages/tools/tests/test_sandbox_exception_contract.py:47:    assert looks_like_not_found(stderr) is expected
./packages/tools/tests/test_sandbox_exception_contract.py:50:def test_raise_read_error_types_by_stderr():
./packages/tools/tests/test_sandbox_exception_contract.py:53:    with pytest.raises(SandboxFileNotFoundError):
./packages/tools/tests/test_sandbox_exception_contract.py:54:        raise_read_error("style.css", b"cat: /workspace/style.css: No such file or directory")
./packages/tools/tests/test_sandbox_exception_contract.py:57:        raise_read_error("x", b"permission denied")
./packages/tools/tests/test_deck_validators.py:16:    _looks_procedural,
./packages/tools/tests/test_deck_validators.py:17:    validate_deck_deliverable,
./packages/tools/tests/test_deck_validators.py:32:    assert _looks_procedural(img) is True
./packages/tools/tests/test_deck_validators.py:37:    assert "raw_html_default" in validate_deck_deliverable(descriptor)
./packages/tools/tests/test_deck_validators.py:41:    assert validate_deck_deliverable({"format": "pptx", "path": "deck.pptx"}) == []
./packages/tools/tests/test_deck_validators.py:52:    assert _looks_procedural(buf.getvalue()) is False
./packages/tools/tests/test_sandbox_read_guardrail.py:6:the loop will not catch it. Every backend read MUST funnel through ``raise_read_error`` (which
./packages/tools/tests/test_sandbox_read_guardrail.py:7:types a missing file as ``SandboxFileNotFoundError``). The ONLY legitimate
./packages/tools/tests/test_sandbox_read_guardrail.py:8:``raise SandboxError(... read_file ...)`` is inside ``raise_read_error`` itself (base.py).
./packages/tools/tests/test_sandbox_read_guardrail.py:28:            continue  # the boundary helper (raise_read_error) legitimately raises here
./packages/tools/tests/test_sandbox_read_guardrail.py:35:        f"build hangs at RUNNING. Use raise_read_error() instead. Offenders: {offenders}"
./packages/tools/tests/test_sandbox_read_guardrail.py:39:def test_raise_read_error_is_the_shared_boundary():
./packages/tools/tests/test_sandbox_read_guardrail.py:40:    """raise_read_error must still exist + type missing files (the guardrail's premise)."""
./packages/tools/tests/test_sandbox_read_guardrail.py:42:    from disco.tools.sandbox.base import SandboxFileNotFoundError, raise_read_error
./packages/tools/tests/test_sandbox_read_guardrail.py:44:    with pytest.raises(SandboxFileNotFoundError):
./packages/tools/tests/test_sandbox_read_guardrail.py:45:        raise_read_error("x", b"cat: x: No such file or directory")
./packages/tools/tests/test_slides_procedural_contract.py:6:to the keyless ``pil-procedural`` backend. A presentable deck must NOT embed procedural
./packages/tools/tests/test_image_gen_binary.py:165:    assert out.structured["backend"] == "pil-procedural"
./packages/tools/tests/test_image_gen_binary.py:480:    assert backend.name == "pil-procedural"
./packages/tools/src/disco/tools/verify/artifact_validators.py:75:def _looks_procedural(img_bytes: bytes) -> bool:
./packages/tools/src/disco/tools/verify/artifact_validators.py:76:    """True iff an image looks like the keyless ``pil-procedural`` placeholder backend's
./packages/tools/src/disco/tools/verify/artifact_validators.py:107:def validate_deck_deliverable(descriptor: dict) -> list[str]:
./packages/tools/src/disco/tools/verify/artifact_validators.py:142:    if any(_looks_procedural(b) for b in images):
./packages/tools/src/disco/tools/builtin/_slides_pipeline.py:495:    if getattr(backend, "name", "") == "pil-procedural":
./packages/tools/src/disco/tools/builtin/image_gen.py:197:    name: str  # surfaced in the deliverable summary ("backend": "pil-procedural")
./packages/tools/src/disco/tools/builtin/image_gen.py:243:    name = "pil-procedural"
./packages/tools/src/disco/tools/builtin/image_gen.py:988:        is_placeholder = backend.name == "pil-procedural"
./packages/tools/src/disco/tools/sandbox/podman.py:59:    raise_read_error,
./packages/tools/src/disco/tools/sandbox/podman.py:170:            raise_read_error(path, err)  # W1: missing file → typed FileNotFoundError
./packages/tools/src/disco/tools/sandbox/base.py:52:class SandboxFileNotFoundError(SandboxError, FileNotFoundError):
./packages/tools/src/disco/tools/sandbox/base.py:66:def looks_like_not_found(stderr: bytes | str) -> bool:
./packages/tools/src/disco/tools/sandbox/base.py:74:def raise_read_error(path: str, stderr: bytes | str) -> NoReturn:
./packages/tools/src/disco/tools/sandbox/base.py:76:    file → typed ``SandboxFileNotFoundError``; anything else → ``SandboxError``. It never
./packages/tools/src/disco/tools/sandbox/base.py:81:    if looks_like_not_found(stderr):
./packages/tools/src/disco/tools/sandbox/base.py:82:        raise SandboxFileNotFoundError(f"read_file {path!r}: {detail}")
./packages/tools/src/disco/tools/sandbox/_container.py:27:    raise_read_error,
./packages/tools/src/disco/tools/sandbox/_container.py:324:                raise_read_error(path, err)  # W1: missing file → typed FileNotFoundError
./packages/core/src/disco/core/evidence/schema.py:1:"""Evidence/correlation schema — evidence-harness-campaign.md W4.
./packages/core/src/disco/core/inspect.py:1:"""DISCO_INSPECT — per-conversation request trace for end-to-end provability.
./packages/core/src/disco/core/inspect.py:5:``DISCO_INSPECT=1`` the runtime captures, per conversation, the full chain of
./packages/core/src/disco/core/inspect.py:46:    """True iff ``DISCO_INSPECT`` (or legacy ``PMX_INSPECT``) is set truthy."""
./docs/archive/deck-editor-integration-plan-6-18-26.md:179:DISCO_INSPECT=1 + `GET /api/debug/trace/{cid}` to prove UI→loop→deck_patch→render.
./docs/archive/truth.md:49:basedpyright driven to **0** (`a11288e`), `DISCO_INSPECT` request-trace layer
./docs/archive/surgical-A-driver-and-file-delivery.md:184:  `DISCO_INSPECT` trace (`GET /api/debug/trace/{cid}`) showing the OpenRouter request
./docs/archive/surgical-A-driver-and-file-delivery.md:253:  `DISCO_INSPECT` trace shows that model driving the new run. **Firefox screenshot** of
./docs/archive/backlog-6-16-26-unaudited.md:336:Promote `obs.py` into a per-conversation **trace sink** (in-memory ring buffer keyed by `request_id`), wire the already-defined `RoutingSink`+`CostTracker` into it, expose over the existing ephemeral channel behind `DISCO_INSPECT=1`. **Reject putting trace data on the persisted event log** (replay/redaction/KV-prefix hazards + it's redundant — routing/condensation/action-obs are already durable). One **VOLATILE breadcrumb** (`request_id`) on `event.meta` re-correlates a finished run to its trace; the fat (raw prompt bytes, raw model output, token deltas) lives only in the volatile buffer.
./docs/archive/backlog-6-16-26-unaudited.md:347:3. **`runtime.py:635`** (the highest-value 2-liner) — when `DISCO_INSPECT=1`, pass `sink=TraceRoutingSink(...)` AND a process-lifetime `CostTracker` (so cost finally accrues); fan trace frames onto `publish_ephemeral(cid,{"type":"trace",...})`.
./docs/archive/backlog-6-16-26-unaudited.md:348:4. **`app.py`** — `@app.websocket("/ws/debug/{cid}")` (forwards only `type=="trace"`, 404 unless `DISCO_INSPECT=1`) + `GET /conversations/{cid}/trace` (REST snapshot for tests).
./docs/archive/backlog-6-16-26-unaudited.md:350:**Storage:** nothing new on the event log (one VOLATILE `meta` breadcrumb at `_emit` `:2094`); volatile ring buffer; optional `.disco/trace/{cid}.jsonl` only on `DISCO_INSPECT_PERSIST=1`, **excluded from the share-bundle export path** (`app.py:1468`).

exec
/bin/bash -lc 'git status --short' in /var/home/dylan/projects/disco
 succeeded in 0ms:
 M frontend/src/App.tsx
 M frontend/src/components/QueryInput.tsx
 M frontend/src/components/build/AgentStatusBar.tsx
 M frontend/src/components/build/AlternativesGate.tsx
 M frontend/src/components/build/AskPanel.tsx
 M frontend/src/components/build/BuildSurface.tsx
 M frontend/src/components/build/ClarifyPanel.tsx
 M frontend/src/components/build/ConfirmationPanel.tsx
 M frontend/src/components/build/DeliverablePanel.tsx
 M frontend/src/components/build/PlanPanel.tsx
 M frontend/src/components/build/SteerInput.tsx
 M frontend/src/components/research/DeepBoundedNotice.tsx
 M frontend/src/components/research/DeepResearchSurface.tsx
 M frontend/src/components/research/NeedMoreCard.tsx
 M frontend/src/components/research/ReportFollowUp.tsx
 M frontend/src/views/HistoryView.tsx
 M frontend/src/vite-env.d.ts
 M frontend/tsconfig.app.json
 M packages/agent-server/src/disco/agent_server/routes/debug.py
 M packages/agent-server/src/disco/agent_server/runtime.py
 D packages/agent-server/src/disco/agent_server/verify.py
M  packages/core/src/disco/core/loop/engine.py
M  packages/core/src/disco/core/loop/messages.py
 M packages/tools/src/disco/tools/builtin/_slides_pipeline.py
 M packages/tools/src/disco/tools/builtin/slides.py
 M packages/tools/src/disco/tools/sandbox/_container.py
 M packages/tools/src/disco/tools/sandbox/base.py
 M packages/tools/src/disco/tools/sandbox/podman.py
 M pyproject.toml
?? .github/workflows/evidence-harness.yml
?? docs/codex-testing-suite-design-6-21-26.md
?? docs/disco-evidence-harness-campaign.md
?? docs/dylans-runthru-3-6-21-26.md
?? frontend/e2e-full/
?? frontend/src/lib/buildTrace.replay.test.ts
?? frontend/src/lib/e2eBridge.ts
?? packages/agent-server/src/disco/agent_server/verify/
?? packages/agent-server/tests/fixtures/
?? packages/agent-server/tests/integration/
?? packages/agent-server/tests/test_debug_evidence_route.py
?? packages/agent-server/tests/test_disco_verify_runner.py
?? packages/agent-server/tests/test_supervision_guardrail.py
?? packages/core/src/disco/core/evidence/
?? packages/core/tests/test_evidence_schema.py
?? packages/tools/src/disco/tools/verify/
?? packages/tools/tests/fixtures/
?? packages/tools/tests/test_artifact_validators.py
?? packages/tools/tests/test_deck_validators.py
?? packages/tools/tests/test_heavy_validators.py
?? packages/tools/tests/test_sandbox_exception_contract.py
?? packages/tools/tests/test_sandbox_read_guardrail.py
?? packages/tools/tests/test_slides_procedural_contract.py
?? scripts/vm201-pw.sh

codex
The repo is dirty with both the evidence work and unrelated-looking frontend/core changes, so I’ll treat the current tree as the review target and avoid assuming ownership from git status. I’m starting with the sandbox and runtime changes because those are the highest-risk places for silent pass/fail behavior.
exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/sandbox/base.py | sed -n '1,140p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""Sandbox spec / instance / service — tool-sandbox-contract.md §5, §7.
     2	
     3	Spec-vs-instance split [OH]: a `SandboxSpec` (template: image, limits, permitted
     4	capabilities, egress allowlist) is distinct from a `SandboxInstance` (a running,
     5	lifecycle-managed environment). Tools execute against an instance. Backends are
     6	pluggable behind `SandboxService` (process/e2b/gvisor/remote) — the executor and
     7	tools depend only on these protocols (§5.1, the "door open" seam).
     8	"""
     9	
    10	from __future__ import annotations
    11	
    12	from typing import NoReturn, Protocol, runtime_checkable
    13	
    14	from pydantic import BaseModel, ConfigDict
    15	
    16	from ..anatomy import Capability
    17	
    18	
    19	def strip_redundant_workspace_prefix(path: str) -> str:
    20	    """ROOT-2 (runthru-v2): the sandbox cwd IS the workspace, but the build/agent
    21	    prompts say files live in '/workspace', so the model often writes paths like
    22	    'workspace/index.html' (a redundant relative prefix). Joining that onto the
    23	    workspace root produced '/workspace/workspace/index.html' — which broke the
    24	    live preview (the server serves the workspace ROOT, so it 404'd on the doubled
    25	    path) AND plan-step done-conditions / file_exists (they check the root). An
    26	    ABSOLUTE '/workspace/foo' already resolves correctly in the container but
    27	    ESCAPES in the process backend, so normalize BOTH forms to a path relative to
    28	    the workspace root. Only strips ONE leading 'workspace/' or '/workspace/' — a
    29	    nested 'src/workspace/x' is untouched.
    30	    """
    31	    for prefix in ("/workspace/", "workspace/"):
    32	        if path.startswith(prefix):
    33	            return path[len(prefix):]
    34	    if path in ("/workspace", "workspace"):
    35	        return ""  # the workspace root itself
    36	    return path
    37	
    38	
    39	class SandboxError(Exception):
    40	    """Raised by an instance on a backend/lifecycle failure (e.g. use after
    41	    destroy). The executor maps it to a `sandbox_error` ToolResult."""
    42	
    43	
    44	class SandboxUnavailableError(SandboxError):
    45	    """The backend infrastructure itself is unusable — Docker unreachable, the
    46	    `runsc` runtime missing, the base image absent, the container failed to start.
    47	    Carries the real underlying cause (reactive-error rule); distinct from a
    48	    per-command failure so the caller can tell "the box is broken" from "the
    49	    command failed"."""
    50	
    51	
    52	class SandboxFileNotFoundError(SandboxError, FileNotFoundError):
    53	    """W1: a workspace file/dir was MISSING on a read. Subclasses BOTH SandboxError (so the
    54	    executor's `sandbox_error` mapping still applies) AND FileNotFoundError (so the loop's
    55	    ~8 ``except (FileNotFoundError, OSError)`` bookkeeping handlers — file_state,
    56	    recitation, observe, view_render, … — catch it instead of letting it ESCAPE and crash
    57	    the loop task into a silent forever-RUNNING hang). The container/podman backends used to
    58	    wrap a missing file in a plain SandboxError, defeating those handlers; the local
    59	    (process) backend already raised native FileNotFoundError. This makes them agree."""
    60	
    61	
    62	# ENOENT signature in a failed `cat`/read stderr — substring of "No such file or directory".
    63	_NOT_FOUND_MARKER = "no such file"
    64	
    65	
    66	def looks_like_not_found(stderr: bytes | str) -> bool:
    67	    """True iff a failed read's stderr indicates a MISSING FILE (ENOENT), vs a missing
    68	    TOOL ("cat: not found"), permission error, etc. Lets the boundary type the error so a
    69	    renamed/deleted file becomes a FileNotFoundError, not a fatal untyped escape."""
    70	    s = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
    71	    return _NOT_FOUND_MARKER in s.lower()
    72	
    73	
    74	def raise_read_error(path: str, stderr: bytes | str) -> NoReturn:
    75	    """The SINGLE boundary every backend's `read_file` uses to raise a failure: a missing
    76	    file → typed ``SandboxFileNotFoundError``; anything else → ``SandboxError``. It never
    77	    lets a bare/untyped exception escape a read path (the W1 contract)."""
    78	    detail = (
    79	        stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
    80	    ).strip()
    81	    if looks_like_not_found(stderr):
    82	        raise SandboxFileNotFoundError(f"read_file {path!r}: {detail}")
    83	    raise SandboxError(f"read_file {path!r}: {detail}")
    84	
    85	
    86	class ExecResult(BaseModel):
    87	    model_config = ConfigDict(frozen=True)
    88	    exit_code: int
    89	    stdout: str
    90	    stderr: str
    91	    # [v1.1 adjustment, flagged] A command killed for exceeding its timeout is
    92	    # reported with this flag set (not raised), so partial stdout/stderr + the exit
    93	    # code survive and the caller can distinguish a timeout from a normal non-zero
    94	    # exit. Additive + defaulted, so existing callers are unaffected.
    95	    timed_out: bool = False
    96	
    97	
    98	class SandboxSpec(BaseModel):
    99	    """[CONTRACT] The template an instance is created from. Pure config."""
   100	
   101	    model_config = ConfigDict(frozen=True)
   102	    image: str = "python-node-base"  # base image id [VERIFY contents]
   103	    # capabilities the instance is permitted — the union of the scope's tools'
   104	    # `needs` (§2). Anything not listed is denied.
   105	    permitted: frozenset[Capability] = frozenset()
   106	    cpu: float = 1.0
   107	    memory_mb: int = 2048
   108	    disk_mb: int = 4096
   109	    timeout_s: int = 300  # default per-call ceiling
   110	    # egress allowlist (§7); empty => deny ALL network (deny-by-default).
   111	    egress_allow: frozenset[str] = frozenset()
   112	
   113	    def egress_allowed(self, host: str) -> bool:
   114	        """[CONTRACT §7] Deny-by-default: a host is reachable only if explicitly
   115	        allow-listed. Enforced outside the guest by the backend (§7 rule 3).
   116	
   117	        Matching: an exact host (`api.example.com`) OR a leading-dot suffix entry
   118	        (`.example.com`) which matches the apex and any subdomain. This is the
   119	        predicate an egress-interception proxy consults per connection (Cluster 3).
   120	        """
   121	        h = host.lower().strip()
   122	        for entry in self.egress_allow:
   123	            e = entry.lower().strip()
   124	            if e.startswith("."):
   125	                # `.example.com` → matches `example.com` and `*.example.com`
   126	                if h == e[1:] or h.endswith(e):
   127	                    return True
   128	            elif h == e:
   129	                return True
   130	        return False
   131	
   132	
   133	REGISTRY_EGRESS_ALLOW: frozenset[str] = frozenset({
   134	    "registry.npmjs.org",
   135	    ".npmjs.org",
   136	    "pypi.org",
   137	    "files.pythonhosted.org",
   138	    "github.com",
   139	    "codeload.github.com",
   140	    ".githubusercontent.com",

exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/sandbox/_container.py | sed -n '280,345p'" in /var/home/dylan/projects/disco
exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/sandbox/podman.py | sed -n '130,190p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   280	            raise SandboxError(f"path escapes workspace: {path!r}")
   281	        return target
   282	
   283	    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
   284	        """Run `cmd` in the live container, capturing stdout/stderr/exit code. The
   285	        timeout is enforced INSIDE the container (the `timeout` coreutil), with an
   286	        outer backstop in case the exec call hangs. A killed command is reported with
   287	        `timed_out=True`, not raised — partial output is preserved."""
   288	        self._alive()
   289	        wrapped = ["timeout", "-k", "5", str(timeout_s), "sh", "-c", cmd]
   290	
   291	        def _run() -> Any:
   292	            return self._container.exec_run(wrapped, demux=True, workdir=self._ws)
   293	
   294	        try:
   295	            res = await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout_s + 15)
   296	        except TimeoutError:
   297	            return ExecResult(
   298	                exit_code=124,
   299	                stdout="",
   300	                stderr=f"command exceeded its {timeout_s}s timeout",
   301	                timed_out=True,
   302	            )
   303	        except Exception as exc:  # noqa: BLE001 — classify: dead box vs per-op failure
   304	            raise self._classify_failure(exc) from exc
   305	
   306	        exit_code = res[0] if res[0] is not None else -1
   307	        out, err = res[1] if res[1] is not None else (None, None)
   308	        return ExecResult(
   309	            exit_code=exit_code,
   310	            stdout=(out or b"").decode("utf-8", errors="replace"),
   311	            stderr=(err or b"").decode("utf-8", errors="replace"),
   312	            timed_out=exit_code in TIMEOUT_EXIT_CODES,
   313	        )
   314	
   315	    async def read_file(self, path: str) -> bytes:
   316	        """Read a workspace file via `exec cat` — binary-safe, transport-agnostic."""
   317	        self._alive()
   318	        target = self._container_path(path)
   319	
   320	        def _read() -> bytes:
   321	            res = self._container.exec_run(["cat", "--", target], demux=True)
   322	            if res[0] != 0:
   323	                err = (res[1][1] if res[1] else b"") or b""
   324	                raise_read_error(path, err)  # W1: missing file → typed FileNotFoundError
   325	            return (res[1][0] if res[1] else b"") or b""
   326	
   327	        return await self._guarded(_read)
   328	
   329	    async def file_exists(self, path: str) -> bool:
   330	        """[B4] Existence check INSIDE the container — `test -f` in the box, never
   331	        a host `Path` check (the host has no view of the container FS; that host
   332	        check is exactly the C18 false-positive this fixes). A missing file is
   333	        False (the non-zero `test` exit, not a raise); a path that escapes the
   334	        workspace jail is False. A genuinely dead box still surfaces through
   335	        `_guarded` as a typed SandboxError, which C18 treats as unverifiable."""
   336	        self._alive()
   337	        try:
   338	            target = self._container_path(path)
   339	        except SandboxError:
   340	            return False
   341	
   342	        def _test() -> bool:
   343	            res = self._container.exec_run(["test", "-f", target])
   344	            return res[0] == 0
   345	

 succeeded in 0ms:
   130	        preview tunnel for it is completed at the Meta deployment. Returns None (the
   131	        agent-server surfaces the honest labeled reason) — never a fake URL."""
   132	        return None
   133	
   134	    def _exec(self, argv: list[str], timeout: float) -> tuple[int, bytes, bytes]:
   135	        return self._runner(["podman", "--url", self._cli_url, "exec", self._name, *argv], timeout)
   136	
   137	    def _raise_if_dead(self, rc: int, err: bytes) -> None:
   138	        """`podman exec` against a gone/exited container fails at the container level
   139	        (rc 125 + a 'no such container'/'not running' marker), distinct from the inner
   140	        command's own nonzero exit. Type that as SandboxUnavailableError so the session
   141	        re-creates (carried lesson #1). NOTE: not live-re-verifiable (VM 202 destroyed);
   142	        best-effort, mirrors the docker-py path's `_classify_failure`."""
   143	        if rc == 0:
   144	            return
   145	        msg = err.decode("utf-8", "replace")
   146	        if any(m in msg.lower() for m in _PODMAN_DEAD_MARKERS):
   147	            raise SandboxUnavailableError(f"sandbox container died mid-session: {msg.strip()}")
   148	
   149	    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
   150	        """Run `cmd` via the CLI native remote — the timeout is enforced in-container
   151	        (`timeout` coreutil) with an outer subprocess backstop; a killed command is
   152	        reported with `timed_out=True`, partial output preserved."""
   153	        self._alive()
   154	        argv = ["timeout", "-k", "5", str(timeout_s), "sh", "-c", cmd]
   155	        rc, out, err = await asyncio.to_thread(self._exec, argv, timeout_s + 15)
   156	        self._raise_if_dead(rc, err)
   157	        return ExecResult(
   158	            exit_code=rc,
   159	            stdout=out.decode("utf-8", "replace"),
   160	            stderr=err.decode("utf-8", "replace"),
   161	            timed_out=rc in TIMEOUT_EXIT_CODES,
   162	        )
   163	
   164	    async def read_file(self, path: str) -> bytes:
   165	        self._alive()
   166	        target = self._container_path(path)
   167	        rc, out, err = await asyncio.to_thread(self._exec, ["cat", "--", target], 60)
   168	        if rc != 0:
   169	            self._raise_if_dead(rc, err)
   170	            raise_read_error(path, err)  # W1: missing file → typed FileNotFoundError
   171	        return out
   172	
   173	    async def file_exists(self, path: str) -> bool:
   174	        """[B4] Existence check via the CLI native remote (`test -f` in-container),
   175	        matching read_file/list_dir's transport (podman-py exec is unusable over
   176	        the remote API). False on a missing file or a jail-escaping path; a dead
   177	        container is surfaced as a typed SandboxUnavailableError (→ recreate)."""
   178	        self._alive()
   179	        try:
   180	            target = self._container_path(path)
   181	        except SandboxError:
   182	            return False
   183	        rc, _out, err = await asyncio.to_thread(self._exec, ["test", "-f", target], 30)
   184	        if rc != 0:
   185	            self._raise_if_dead(rc, err)
   186	        return rc == 0
   187	
   188	    async def list_dir(self, path: str) -> list[str]:
   189	        self._alive()
   190	        target = self._container_path(path)

exec
/bin/bash -lc "nl -ba packages/tools/tests/test_sandbox_read_guardrail.py | sed -n '1,120p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""W10 — static guardrail locking in W1 (typed sandbox read errors).
     2	
     3	Prevents the whack-a-mole regression codex warned about: a NEW sandbox backend — or an edit
     4	to an existing one — that raises a BARE ``SandboxError`` on a failed read re-opens the
     5	silent-hang trigger, because the ~8 ``except (FileNotFoundError, OSError)`` handlers across
     6	the loop will not catch it. Every backend read MUST funnel through ``raise_read_error`` (which
     7	types a missing file as ``SandboxFileNotFoundError``). The ONLY legitimate
     8	``raise SandboxError(... read_file ...)`` is inside ``raise_read_error`` itself (base.py).
     9	"""
    10	
    11	from __future__ import annotations
    12	
    13	import pathlib
    14	import re
    15	
    16	import pytest
    17	
    18	pytestmark = pytest.mark.boundary_contract
    19	
    20	_SANDBOX_DIR = pathlib.Path(__file__).resolve().parents[1] / "src/disco/tools/sandbox"
    21	_BARE_READ_RAISE = re.compile(r"""raise\s+SandboxError\(\s*f?["']read_file""")
    22	
    23	
    24	def test_no_backend_raises_bare_sandbox_error_on_read():
    25	    offenders: list[str] = []
    26	    for f in sorted(_SANDBOX_DIR.glob("*.py")):
    27	        if f.name == "base.py":
    28	            continue  # the boundary helper (raise_read_error) legitimately raises here
    29	        for lineno, line in enumerate(f.read_text().splitlines(), 1):
    30	            if _BARE_READ_RAISE.search(line):
    31	                offenders.append(f"{f.name}:{lineno}")
    32	    assert not offenders, (
    33	        "A bare SandboxError on a read defeats the W1 typed-error contract — the loop's "
    34	        "`except (FileNotFoundError, OSError)` bookkeeping handlers won't catch it and the "
    35	        f"build hangs at RUNNING. Use raise_read_error() instead. Offenders: {offenders}"
    36	    )
    37	
    38	
    39	def test_raise_read_error_is_the_shared_boundary():
    40	    """raise_read_error must still exist + type missing files (the guardrail's premise)."""
    41	    import pytest
    42	    from disco.tools.sandbox.base import SandboxFileNotFoundError, raise_read_error
    43	
    44	    with pytest.raises(SandboxFileNotFoundError):
    45	        raise_read_error("x", b"cat: x: No such file or directory")

codex
The sandbox guardrail is static and narrow: it only searches for one literal `raise SandboxError("read_file...")` pattern. I’m checking the actual backend methods next, because a missed boundary there would preserve the original hang trigger while the guardrail still passes.
exec
/bin/bash -lc "rg -n \"async def read_file|def read_file|read_file\\(|SandboxError|FileNotFoundError|raise_read_error|cat\" packages/tools/src/disco/tools/sandbox/*.py" in /var/home/dylan/projects/disco
exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/sandbox/kernel.py | sed -n '1,260p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
packages/tools/src/disco/tools/sandbox/base.py:39:class SandboxError(Exception):
packages/tools/src/disco/tools/sandbox/base.py:44:class SandboxUnavailableError(SandboxError):
packages/tools/src/disco/tools/sandbox/base.py:52:class SandboxFileNotFoundError(SandboxError, FileNotFoundError):
packages/tools/src/disco/tools/sandbox/base.py:53:    """W1: a workspace file/dir was MISSING on a read. Subclasses BOTH SandboxError (so the
packages/tools/src/disco/tools/sandbox/base.py:54:    executor's `sandbox_error` mapping still applies) AND FileNotFoundError (so the loop's
packages/tools/src/disco/tools/sandbox/base.py:55:    ~8 ``except (FileNotFoundError, OSError)`` bookkeeping handlers — file_state,
packages/tools/src/disco/tools/sandbox/base.py:56:    recitation, observe, view_render, … — catch it instead of letting it ESCAPE and crash
packages/tools/src/disco/tools/sandbox/base.py:58:    wrap a missing file in a plain SandboxError, defeating those handlers; the local
packages/tools/src/disco/tools/sandbox/base.py:59:    (process) backend already raised native FileNotFoundError. This makes them agree."""
packages/tools/src/disco/tools/sandbox/base.py:62:# ENOENT signature in a failed `cat`/read stderr — substring of "No such file or directory".
packages/tools/src/disco/tools/sandbox/base.py:67:    """True iff a failed read's stderr indicates a MISSING FILE (ENOENT), vs a missing
packages/tools/src/disco/tools/sandbox/base.py:68:    TOOL ("cat: not found"), permission error, etc. Lets the boundary type the error so a
packages/tools/src/disco/tools/sandbox/base.py:69:    renamed/deleted file becomes a FileNotFoundError, not a fatal untyped escape."""
packages/tools/src/disco/tools/sandbox/base.py:74:def raise_read_error(path: str, stderr: bytes | str) -> NoReturn:
packages/tools/src/disco/tools/sandbox/base.py:76:    file → typed ``SandboxFileNotFoundError``; anything else → ``SandboxError``. It never
packages/tools/src/disco/tools/sandbox/base.py:82:        raise SandboxFileNotFoundError(f"read_file {path!r}: {detail}")
packages/tools/src/disco/tools/sandbox/base.py:83:    raise SandboxError(f"read_file {path!r}: {detail}")
packages/tools/src/disco/tools/sandbox/base.py:119:        predicate an egress-interception proxy consults per connection (Cluster 3).
packages/tools/src/disco/tools/sandbox/base.py:162:    async def read_file(self, path: str) -> bytes: ...
packages/tools/src/disco/tools/sandbox/base.py:168:    # predicate calls this instead of a host `Path.exists()`. Returns False on a
packages/tools/src/disco/tools/sandbox/base.py:181:    # the box FS). C18 and C1c consume this to resolve done-condition predicates.
packages/tools/src/disco/tools/sandbox/shell_sessions.py:42:    avoid duplicating a server that's already running on the fresh instance).
packages/tools/src/disco/tools/sandbox/shell_sessions.py:337:        fresh instance. We SKIP those to avoid duplicating a server that's
packages/tools/src/disco/tools/sandbox/shell_sessions.py:338:        already running (the no-duplication guarantee). Defaults to a /proc
packages/tools/src/disco/tools/sandbox/_container.py:24:    SandboxError,
packages/tools/src/disco/tools/sandbox/_container.py:27:    raise_read_error,
packages/tools/src/disco/tools/sandbox/_container.py:170:            raise SandboxError(f"sandbox instance {self.id} has been destroyed")
packages/tools/src/disco/tools/sandbox/_container.py:234:    def _classify_failure(self, exc: Exception) -> SandboxError:
packages/tools/src/disco/tools/sandbox/_container.py:237:        gone → SandboxUnavailableError, which the session layer catches to RE-CREATE.
packages/tools/src/disco/tools/sandbox/_container.py:238:        Otherwise it's a generic per-op SandboxError. Typing death distinctly is what
packages/tools/src/disco/tools/sandbox/_container.py:243:        catch it here and type the box as dead: the event loop NEVER blocks on
packages/tools/src/disco/tools/sandbox/_container.py:250:            # The session layer catches SandboxUnavailableError to RE-CREATE.
packages/tools/src/disco/tools/sandbox/_container.py:254:        return SandboxError(f"sandbox op failed in {self.id}: {exc}")
packages/tools/src/disco/tools/sandbox/_container.py:257:        """Run a blocking container op in a thread; let already-typed SandboxErrors
packages/tools/src/disco/tools/sandbox/_container.py:262:        except SandboxError:
packages/tools/src/disco/tools/sandbox/_container.py:280:            raise SandboxError(f"path escapes workspace: {path!r}")
packages/tools/src/disco/tools/sandbox/_container.py:315:    async def read_file(self, path: str) -> bytes:
packages/tools/src/disco/tools/sandbox/_container.py:316:        """Read a workspace file via `exec cat` — binary-safe, transport-agnostic."""
packages/tools/src/disco/tools/sandbox/_container.py:321:            res = self._container.exec_run(["cat", "--", target], demux=True)
packages/tools/src/disco/tools/sandbox/_container.py:324:                raise_read_error(path, err)  # W1: missing file → typed FileNotFoundError
packages/tools/src/disco/tools/sandbox/_container.py:335:        `_guarded` as a typed SandboxError, which C18 treats as unverifiable."""
packages/tools/src/disco/tools/sandbox/_container.py:339:        except SandboxError:
packages/tools/src/disco/tools/sandbox/_container.py:371:                raise SandboxError(f"write_file {path!r} failed")
packages/tools/src/disco/tools/sandbox/_container.py:384:                raise SandboxError(f"list_dir {path!r}: {err.decode('utf-8', 'replace').strip()}")
packages/tools/src/disco/tools/sandbox/__init__.py:8:    SandboxError,
packages/tools/src/disco/tools/sandbox/__init__.py:40:    "SandboxError",
packages/tools/src/disco/tools/sandbox/process.py:30:    SandboxError,
packages/tools/src/disco/tools/sandbox/process.py:52:            raise SandboxError(f"sandbox instance {self.id} has been destroyed")
packages/tools/src/disco/tools/sandbox/process.py:59:            raise SandboxError(f"path escapes workspace: {path!r}")
packages/tools/src/disco/tools/sandbox/process.py:80:            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
packages/tools/src/disco/tools/sandbox/process.py:98:    async def read_file(self, path: str) -> bytes:
packages/tools/src/disco/tools/sandbox/process.py:115:        target. A path that escapes the jail is False (not raised — the predicate
packages/tools/src/disco/tools/sandbox/process.py:121:        except SandboxError:
packages/tools/src/disco/tools/sandbox/process.py:127:        """W5 — expose the workspace root for C18 / C1c predicate resolution.
packages/tools/src/disco/tools/sandbox/process.py:168:        out, _ = await proc.communicate()
packages/tools/src/disco/tools/sandbox/session.py:8:finding). When that happens, the session catches it (`SandboxUnavailableError`, the
packages/tools/src/disco/tools/sandbox/session.py:10:raises a clean `SandboxError` the executor turns into an error `ToolResult` the loop
packages/tools/src/disco/tools/sandbox/session.py:29:    SandboxError,
packages/tools/src/disco/tools/sandbox/session.py:169:            raise SandboxError("sandbox session is closed")
packages/tools/src/disco/tools/sandbox/session.py:242:        # skipping ports already bound (the no-duplication guarantee).
packages/tools/src/disco/tools/sandbox/session.py:259:        # skipping ports already bound (the no-duplication guarantee).
packages/tools/src/disco/tools/sandbox/session.py:288:        clean SandboxError (→ the executor's `sandbox_error` ToolResult) so the loop
packages/tools/src/disco/tools/sandbox/session.py:295:            raise SandboxError(
packages/tools/src/disco/tools/sandbox/session.py:326:                data = await self.read_file(mem_path)
packages/tools/src/disco/tools/sandbox/session.py:328:            except (FileNotFoundError, NotADirectoryError):
packages/tools/src/disco/tools/sandbox/session.py:387:    async def read_file(self, path: str) -> bytes:
packages/tools/src/disco/tools/sandbox/session.py:388:        return await self._resilient(lambda i: i.read_file(path))
packages/tools/src/disco/tools/sandbox/session.py:400:        raises a clean SandboxError the C18 advisory treats as unverifiable."""
packages/tools/src/disco/tools/sandbox/session.py:405:        """W5 — expose the workspace root for C18 / C1c predicate resolution.
packages/tools/src/disco/tools/sandbox/session.py:522:            raise SandboxError(
packages/tools/src/disco/tools/sandbox/podman.py:55:    SandboxError,
packages/tools/src/disco/tools/sandbox/podman.py:59:    raise_read_error,
packages/tools/src/disco/tools/sandbox/podman.py:87:    except FileNotFoundError as exc:
packages/tools/src/disco/tools/sandbox/podman.py:88:        raise SandboxError(f"the `podman` CLI is required on PATH for exec: {exc}") from exc
packages/tools/src/disco/tools/sandbox/podman.py:164:    async def read_file(self, path: str) -> bytes:
packages/tools/src/disco/tools/sandbox/podman.py:167:        rc, out, err = await asyncio.to_thread(self._exec, ["cat", "--", target], 60)
packages/tools/src/disco/tools/sandbox/podman.py:170:            raise_read_error(path, err)  # W1: missing file → typed FileNotFoundError
packages/tools/src/disco/tools/sandbox/podman.py:181:        except SandboxError:
packages/tools/src/disco/tools/sandbox/podman.py:194:            raise SandboxError(f"list_dir {path!r}: {err.decode('utf-8', 'replace').strip()}")
packages/tools/src/disco/tools/sandbox/podman.py:216:                raise SandboxError(f"write_file {path!r} failed")
packages/tools/src/disco/tools/sandbox/kernel.py:20:from .base import SandboxError
packages/tools/src/disco/tools/sandbox/kernel.py:270:        """B2 kernel output discipline: truncate stdout/repr if > 2000 chars."""
packages/tools/src/disco/tools/sandbox/kernel.py:286:                truncated = f"{head}\n[full output: /workspace/{rel_path}, {len(val)} bytes]"
packages/tools/src/disco/tools/sandbox/kernel.py:287:                setattr(res, field_name, truncated)
packages/tools/src/disco/tools/sandbox/kernel.py:370:        # without it the gateway is an unauthenticated RCE endpoint on whatever
packages/tools/src/disco/tools/sandbox/kernel.py:399:        raise SandboxError(
packages/tools/src/disco/tools/sandbox/kernel.py:542:        """B2 kernel output discipline: truncate stdout/repr if > 2000 chars."""
packages/tools/src/disco/tools/sandbox/kernel.py:555:                truncated = f"{head}\n[full output: /workspace/{rel_path}, {len(val)} bytes]"
packages/tools/src/disco/tools/sandbox/kernel.py:556:                setattr(res, field_name, truncated)
packages/tools/src/disco/tools/sandbox/gvisor.py:8:when co-located, OR Docker-over-SSH (`ssh://user@host`, via the system ssh client,
packages/tools/src/disco/tools/sandbox/egress_proxy.py:22:lines (`GET http://host/path`). Both consult the same allow predicate; a denied
packages/tools/src/disco/tools/sandbox/egress_proxy.py:33:__all__ = ["host_allowed", "make_predicate", "AllowlistProxy", "main"]
packages/tools/src/disco/tools/sandbox/egress_proxy.py:36:# ---- the allow predicate (semantics mirror SandboxSpec.egress_allowed) -------
packages/tools/src/disco/tools/sandbox/egress_proxy.py:59:def make_predicate(entries: Iterable[str]) -> Callable[[str], bool]:
packages/tools/src/disco/tools/sandbox/egress_proxy.py:60:    """Freeze an allowlist into a `(host) -> bool` predicate."""
packages/tools/src/disco/tools/sandbox/egress_proxy.py:259:    proxy = AllowlistProxy(make_predicate(entries), port=port)
packages/tools/src/disco/tools/sandbox/config.py:69:    # is raised (the caller catches, the loop continues). 0.5s is a healthy

exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/sandbox/gvisor.py | sed -n '1,260p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	from __future__ import annotations
     2	
     3	import asyncio
     4	import base64
     5	import hashlib
     6	import hmac
     7	import json
     8	import logging
     9	import os
    10	import re
    11	import secrets as _secrets
    12	import time
    13	import uuid
    14	from collections.abc import Callable
    15	from dataclasses import dataclass, field
    16	from pathlib import Path
    17	from queue import Empty
    18	from typing import Any
    19	
    20	from .base import SandboxError
    21	
    22	_LOG = logging.getLogger(__name__)
    23	
    24	# Dev fallback for the kernel-gateway auth token when no app secret is set —
    25	# a process-local random key (stable for this process's lifetime).
    26	_DEV_GATEWAY_SECRET = _secrets.token_bytes(32)
    27	
    28	
    29	def _gateway_auth_token(sandbox_id: str) -> str:
    30	    """The kernel gateway's auth token for one sandbox. SECURITY: the gateway
    31	    (`jupyter kernelgateway`) is an arbitrary-code-execution endpoint; without a
    32	    token any caller that can reach its port can drive a kernel. The token is
    33	    DERIVED, not stored, so it is STABLE and re-derivable: `_ensure_gateway`
    34	    lazily REUSES a surviving gateway across suspend/resume / agent-server
    35	    restart, and a fresh `GatewayKernel` must reconnect with the SAME token (a
    36	    per-object random token would lock the object out of its own kernel).
    37	
    38	    HMAC keyed on `DISCO_SECRET_KEY` so the value is secret from other sandboxes
    39	    (interiors never receive the master key — SEC-1) and from the network; a
    40	    process-local random key is the dev fallback when no app secret is set. The
    41	    token is observable only from inside this sandbox (the gateway runs in its
    42	    own container), which is the same trust boundary it protects."""
    43	    from disco.core.env import disco_env
    44	
    45	    master = disco_env("SECRET_KEY")
    46	    key = master.encode() if master else _DEV_GATEWAY_SECRET
    47	    return hmac.new(key, f"kernel-gateway:{sandbox_id}".encode(), hashlib.sha256).hexdigest()
    48	
    49	# C15: idle-cull knob. Default 300s (5 min) — long enough that an agent thinking
    50	# between tool calls doesn't trigger churn, short enough to free a forgotten
    51	# kernel in a quiet conversation. Set to 0 to disable culling entirely.
    52	_DEFAULT_IDLE_TIMEOUT_S = 300.0
    53	_IDLE_TIMEOUT_ENV = "DISCO_KERNEL_IDLE_TIMEOUT_S"
    54	
    55	
    56	def _default_idle_timeout_s() -> float:
    57	    """Read `DISCO_KERNEL_IDLE_TIMEOUT_S`; missing/garbage -> 300.0; clamped to >=0.
    58	    Read at call-time so tests can monkeypatch the env var per-case without
    59	    process-level state."""
    60	    raw = os.environ.get(_IDLE_TIMEOUT_ENV)
    61	    if raw is None or raw.strip() == "":
    62	        return _DEFAULT_IDLE_TIMEOUT_S
    63	    try:
    64	        v = float(raw)
    65	    except ValueError:
    66	        _LOG.warning(
    67	            "%s=%r is not a float; using default %.0fs",
    68	            _IDLE_TIMEOUT_ENV, raw, _DEFAULT_IDLE_TIMEOUT_S,
    69	        )
    70	        return _DEFAULT_IDLE_TIMEOUT_S
    71	    return max(0.0, v)
    72	
    73	# ANSI escape sequence regex for stripping colors from tracebacks
    74	_ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    75	
    76	@dataclass
    77	class KernelResult:
    78	    ok: bool
    79	    stdout: str
    80	    stderr: str
    81	    result_repr: str | None = None
    82	    error_traceback: str | None = None  # ANSI-stripped
    83	    images: list[str] = field(default_factory=list)  # workspace-relative paths
    84	    timed_out: bool = False
    85	    restarted: bool = False
    86	
    87	    def __str__(self) -> str:
    88	        parts = []
    89	        if self.stdout:
    90	            parts.append(self.stdout)
    91	        if self.stderr:
    92	            parts.append(self.stderr)
    93	        if self.result_repr:
    94	            parts.append(f"→ {self.result_repr}")
    95	        if self.error_traceback:
    96	            parts.append(self.error_traceback)
    97	        for img in self.images:
    98	            parts.append(f"plot saved: {img}")
    99	        return "\n".join(parts)
   100	
   101	class KernelSession:
   102	    """One persistent IPython kernel per conversation. State lives in kernel RAM."""
   103	
   104	    async def start(self) -> None:
   105	        """Start the kernel and run initialization (e.g. memory limits)."""
   106	        raise NotImplementedError
   107	
   108	    async def execute(self, code: str, *, timeout_s: int) -> KernelResult:
   109	        """Execute code in the kernel. Handles interrupts and restarts on timeout."""
   110	        raise NotImplementedError
   111	
   112	    async def interrupt(self) -> None:
   113	        """Send a SIGINT to the kernel."""
   114	        raise NotImplementedError
   115	
   116	    async def restart(self) -> None:
   117	        """Hard restart of the kernel. RAM state is lost."""
   118	        raise NotImplementedError
   119	
   120	    async def shutdown(self) -> None:
   121	        """Cleanly shutdown the kernel."""
   122	        raise NotImplementedError
   123	
   124	class ProcessKernel(KernelSession):
   125	    """Transport for the 'process' backend: runs a local kernel via jupyter_client."""
   126	
   127	    def __init__(self, workspace_path: str) -> None:
   128	        self._workspace = Path(workspace_path).absolute()
   129	        self._km: Any | None = None
   130	        self._kc: Any | None = None
   131	        self._seq = 0
   132	
   133	    async def start(self) -> None:
   134	        from jupyter_client.manager import AsyncKernelManager
   135	        self._km = AsyncKernelManager(kernel_name="python3")
   136	        # Ensure the kernel runs in the workspace directory
   137	        self._km.extra_arguments = ["--ProjectManager.root_dir=" + str(self._workspace)]
   138	
   139	        await self._km.start_kernel(cwd=str(self._workspace))
   140	        self._kc = self._km.client()
   141	        assert self._kc is not None  # client() always returns a KernelClient
   142	        self._kc.start_channels()
   143	        await self._kc.wait_for_ready(timeout=60)
   144	
   145	        # Setup memory limit: 4GiB as required by BP-08
   146	        setup_cell = (
   147	            "import resource; "
   148	            "resource.setrlimit(resource.RLIMIT_AS, (4 << 30, 4 << 30))"
   149	        )
   150	        await self.execute(setup_cell, timeout_s=10)
   151	
   152	    async def execute(self, code: str, *, timeout_s: int) -> KernelResult:
   153	        if not self._kc:
   154	            await self.start()
   155	        assert self._kc is not None  # start() always populates both _km and _kc
   156	        kc = self._kc
   157	
   158	        msg_id = kc.execute(code)
   159	        
   160	        stdout = []
   161	        stderr = []
   162	        result_repr = None
   163	        error_traceback = None
   164	        images = []
   165	        
   166	        try:
   167	            while True:
   168	                try:
   169	                    # We use a smaller interval to check for timeout more frequently
   170	                    msg = await kc.get_iopub_msg(timeout=timeout_s)
   171	                except (TimeoutError, Empty):
   172	                    # Timeout protocol (EXACT): interrupt() -> wait <=5s -> restart() if hangs
   173	                    _LOG.warning("Kernel execution timed out, interrupting...")
   174	                    await self.interrupt()
   175	
   176	                    # Wait up to 5s for the kernel to return to idle
   177	                    idle = False
   178	                    start_wait = time.time()
   179	                    while time.time() - start_wait < 5.0:
   180	                        try:
   181	                            msg = await kc.get_iopub_msg(timeout=0.1)
   182	                        except TimeoutError:
   183	                            continue
   184	                        
   185	                        if msg.get("parent_header", {}).get("msg_id") != msg_id:
   186	                            continue
   187	                        if msg.get("header", {}).get("msg_type") == "status" and \
   188	                           msg.get("content", {}).get("execution_state") == "idle":
   189	                            idle = True
   190	                            break
   191	                    
   192	                    if not idle:
   193	                        _LOG.warning("Kernel failed to idle after interrupt, restarting...")
   194	                        await self.restart()
   195	                        return KernelResult(
   196	                            ok=False, stdout="".join(stdout), stderr="".join(stderr),
   197	                            error_traceback="Kernel timed out and was restarted",
   198	                            timed_out=True, restarted=True)
   199	                    
   200	                    return KernelResult(ok=False, stdout="".join(stdout), stderr="".join(stderr),
   201	                                       error_traceback="KeyboardInterrupt: execution timed out",
   202	                                       timed_out=True)
   203	
   204	                content = msg.get("content", {})
   205	                msg_type = msg.get("header", {}).get("msg_type")
   206	                
   207	                if msg.get("parent_header", {}).get("msg_id") != msg_id:
   208	                    continue
   209	
   210	                if msg_type == "stream":
   211	                    if content.get("name") == "stdout":
   212	                        stdout.append(content.get("text", ""))
   213	                    elif content.get("name") == "stderr":
   214	                        stderr.append(content.get("text", ""))
   215	                elif msg_type == "execute_result":
   216	                    result_repr = content.get("data", {}).get("text/plain")
   217	                elif msg_type == "display_data":
   218	                    data = content.get("data", {})
   219	                    if "image/png" in data:
   220	                        self._seq += 1
   221	                        img_path = f".pmx/plots/{self._seq:04d}.png"
   222	                        full_path = self._workspace / img_path
   223	                        full_path.parent.mkdir(parents=True, exist_ok=True)
   224	                        with open(full_path, "wb") as f:
   225	                            f.write(base64.b64decode(data["image/png"]))
   226	                        images.append(img_path)
   227	                elif msg_type == "error":
   228	                    traceback = content.get("traceback", [])
   229	                    error_traceback = _ANSI_ESCAPE.sub('', "\n".join(traceback))
   230	                elif msg_type == "status" and content.get("execution_state") == "idle":
   231	                    # Check if we've received the execute_reply
   232	                    # We might need to skip stale replies from previous interrupted executions
   233	                    try:
   234	                        while True:
   235	                            reply = await kc.get_shell_msg(timeout=1)
   236	                            if reply.get("parent_header", {}).get("msg_id") == msg_id:
   237	                                ok = reply.get("content", {}).get("status") == "ok"
   238	                                res = KernelResult(
   239	                                    ok=ok,
   240	                                    stdout="".join(stdout),
   241	                                    stderr="".join(stderr),
   242	                                    result_repr=result_repr,
   243	                                    error_traceback=error_traceback,
   244	                                    images=images
   245	                                )
   246	                                return await self._apply_discipline(res)
   247	                            else:
   248	                                stale = reply.get("parent_header", {}).get("msg_id")
   249	                                _LOG.debug(f"Skipping stale shell message for {stale}")
   250	                                continue
   251	                    except Exception:
   252	                        # If no reply yet, we might still be waiting for it or it might not come
   253	                        # For now, we return what we have if we got an idle status
   254	                        res = KernelResult(
   255	                            ok=True if not error_traceback else False,
   256	                            stdout="".join(stdout),
   257	                            stderr="".join(stderr),
   258	                            result_repr=result_repr,
   259	                            error_traceback=error_traceback,
   260	                            images=images

 succeeded in 0ms:
     1	"""gVisor sandbox backend — tool-sandbox-contract §5.1, driving a Docker host.
     2	
     3	Fulfils `SandboxService`/`SandboxInstance` over Docker (docker-py), selecting the
     4	`runsc` (gVisor) runtime. The session model + exec/file-op/lifecycle behavior is the
     5	shared `ContainerInstance`; this module is just the gVisor `create` path.
     6	
     7	Transport: the Docker endpoint is config-driven (`docker_socket`) — the LOCAL socket
     8	when co-located, OR Docker-over-SSH (`ssh://user@host`, via the system ssh client,
     9	e.g. keyless Tailscale SSH) when remote. File ops go through the container (exec/cp),
    10	never a host path, so they work over either. docker-py is synchronous; every call is
    11	run via `asyncio.to_thread`.
    12	"""
    13	
    14	from __future__ import annotations
    15	
    16	import asyncio
    17	import logging
    18	import pathlib
    19	import posixpath
    20	import uuid
    21	from typing import Any
    22	
    23	from . import egress_proxy as _egress_proxy_mod
    24	from ._container import (
    25	    EGRESS_PROXY_PORT,
    26	    PUBLISHED_PORTS,
    27	    ContainerInstance,
    28	    egress_mode,
    29	    format_allow,
    30	    proxy_env,
    31	    proxy_run_argv,
    32	    sealed,
    33	)
    34	from .base import SandboxInstance, SandboxSpec, SandboxUnavailableError
    35	from .config import SandboxConfig, default_sandbox_config
    36	from .isolation import IsolationProfile, isolation_for
    37	from .naming import (
    38	    EGR_NET_PREFIX,
    39	    LABEL_CONV,
    40	    LABEL_CONV_KEYS,
    41	    SBX_NAME_PREFIX,
    42	    SBX_NAME_PREFIXES,
    43	    conv_id_from_labels,
    44	)
    45	
    46	_LOG = logging.getLogger(__name__)
    47	
    48	
    49	def _keepalive_command() -> list[str]:
    50	    """The container's main process. Always sleep infinity."""
    51	    return ["sleep", "infinity"]
    52	
    53	
    54	def _preview_host(docker_socket: str) -> str:
    55	    """The host a published port is reachable at: localhost for a local socket; the
    56	    remote Docker host (its tailnet IP) for Docker-over-SSH — that host serves the
    57	    published port and is reachable over the proven keyless tailnet."""
    58	    if docker_socket.startswith("ssh://"):
    59	        host = docker_socket.removeprefix("ssh://").split("@")[-1]
    60	        return host.split(":")[0].split("/")[0]  # strip any port / path
    61	    return "localhost"
    62	
    63	
    64	def _put_file(container: Any, dir_path: str, name: str, data: bytes) -> None:
    65	    """`put_archive` a single file into a container dir — used to drop the
    66	    stdlib-only egress proxy script into the sidecar (no image rebuild)."""
    67	    import io
    68	    import tarfile
    69	    import time
    70	
    71	    buf = io.BytesIO()
    72	    with tarfile.open(fileobj=buf, mode="w") as tar:
    73	        info = tarfile.TarInfo(name=name)
    74	        info.size = len(data)
    75	        # [BP-00 root-cause] TarInfo defaults mtime to 0 (epoch 1970) — stamp reality.
    76	        info.mtime = int(time.time())
    77	        tar.addfile(info, io.BytesIO(data))
    78	    if not container.put_archive(dir_path, buf.getvalue()):
    79	        raise SandboxUnavailableError("failed to inject egress proxy script into sidecar")
    80	
    81	
    82	class GvisorSandboxInstance(ContainerInstance):
    83	    """A running gVisor container — shared ContainerInstance behavior. The host
    84	    bind-mount persists the workspace on the daemon host; the backend never touches
    85	    that path directly (file ops go through the container).
    86	
    87	    Filtered-egress boxes also own an allowlisting proxy SIDECAR and an internal
    88	    no-NAT network; both are torn down alongside the container so a run leaves no
    89	    orphaned infra. The aux teardown is inherited from ContainerInstance
    90	    (`_teardown_egress_aux`); the service sets `instance._egress_sidecar` /
    91	    `instance._egress_network` in create()."""
    92	
    93	
    94	class GvisorSandboxService:
    95	    """[CONTRACT boundary] The Docker/OCI backend — creates instances on a Docker host
    96	    via docker-py, selecting the configured runtime (gVisor `runsc` here). The local
    97	    container backend (`LocalSandboxService`) reuses this whole class, differing only
    98	    in its socket/runtime config and a named-volume workspace."""
    99	
   100	    name = "gvisor"
   101	    _instance_cls: type[ContainerInstance] = GvisorSandboxInstance
   102	
   103	    def __init__(self, config: SandboxConfig | None = None, *, client: Any | None = None) -> None:
   104	        self._cfg = config or default_sandbox_config()
   105	        self._injected_client = client  # test seam: a fake docker client
   106	        self._client_cache: Any | None = None
   107	        self._instances: dict[str, ContainerInstance] = {}
   108	
   109	    @property
   110	    def isolation(self) -> IsolationProfile:
   111	        """This backend's isolation profile (label + adversarial-safety + the
   112	        isolation-coupled confirmation default), surfaced at the point of choice."""
   113	        return isolation_for(self.name)
   114	
   115	    def _client(self) -> Any:
   116	        """The Docker client (lazy, cached). A connection failure is a typed
   117	        infra error carrying the real cause — Docker isn't reachable."""
   118	        if self._injected_client is not None:
   119	            return self._injected_client
   120	        if self._client_cache is None:
   121	            try:
   122	                import docker
   123	
   124	                base_url = self._cfg.docker_socket
   125	                # Bound the socket timeout (Dispo #25): a hung daemon must fail
   126	                # fast so its `to_thread` worker returns instead of leaking for
   127	                # docker-py's 60s default. `reload()` keeps its tighter 0.5s guard.
   128	                kwargs: dict[str, Any] = {
   129	                    "base_url": base_url,
   130	                    "timeout": self._cfg.client_timeout_s,
   131	                }
   132	                if base_url.startswith("ssh://"):
   133	                    # Docker-over-SSH: use the SYSTEM ssh client so the host's auth
   134	                    # (e.g. keyless Tailscale SSH) applies, not docker-py's paramiko.
   135	                    kwargs["use_ssh_client"] = True
   136	                client = docker.DockerClient(**kwargs)
   137	                client.ping()
   138	            except Exception as exc:  # noqa: BLE001 — map to a typed infra error
   139	                raise SandboxUnavailableError(
   140	                    f"Docker unreachable at {self._cfg.docker_socket}: {exc}"
   141	                ) from exc
   142	            self._client_cache = client
   143	        return self._client_cache
   144	
   145	    def _require_runtime(self, client: Any) -> None:
   146	        """Fail loud if the configured runtime (runsc / runc / …) isn't registered on
   147	        the host. A value, not a branch — the same check serves every Docker tier."""
   148	        try:
   149	            runtimes = client.info().get("Runtimes", {}) or {}
   150	        except Exception as exc:  # noqa: BLE001
   151	            raise SandboxUnavailableError(f"could not query Docker runtimes: {exc}") from exc
   152	        if self._cfg.runtime not in runtimes:
   153	            raise SandboxUnavailableError(
   154	                f"the {self._cfg.runtime!r} runtime is not configured on the Docker host"
   155	            )
   156	
   157	    def _require_image(self, client: Any) -> None:
   158	        """Image is assumed PRESENT (load it first); a missing image fails loud and we
   159	        NEVER pull. `containers.run` would otherwise silently pull from a registry —
   160	        this guard turns that into a typed error before any run."""
   161	        from docker.errors import ImageNotFound
   162	
   163	        try:
   164	            client.images.get(self._cfg.image)
   165	        except ImageNotFound as exc:
   166	            raise SandboxUnavailableError(
   167	                f"sandbox image {self._cfg.image!r} not present on the host; this backend "
   168	                f"never pulls (load it first)"
   169	            ) from exc
   170	        except Exception as exc:  # noqa: BLE001
   171	            raise SandboxUnavailableError(f"could not query the sandbox image: {exc}") from exc
   172	
   173	    def _setup_filtered_egress(
   174	        self, client: Any, spec: SandboxSpec, instance_id: str, conversation_id: str = ""
   175	    ) -> Any:
   176	        """[HARDWARE-UNVERIFIED — live-verify on VM 201] Stand up the allowlisting
   177	        egress for a "filtered" box and return (network, sidecar, env, network_name).
   178	
   179	        Containment design: an INTERNAL (no-NAT) network whose ONLY member with an
   180	        outside route is the proxy sidecar. The sandbox is attached to this network
   181	        alone, so its sole path to the internet is through the proxy — which refuses
   182	        any host the allowlist doesn't name. The HTTP(S)_PROXY env is belt-and-
   183	        suspenders; the no-route property is the real guarantee.
   184	
   185	        The proxy is the stdlib-only `egress_proxy.py`, injected into a sidecar built
   186	        from the SAME base image (its python3 runs it — no extra image, honoring the
   187	        never-pull rule).
   188	
   189	        THREE non-obvious details, each VERIFIED live on the gVisor host (VM 201);
   190	        get any wrong and containment silently breaks:
   191	
   192	        1. Two NICs AT BOOT. gVisor (runsc) freezes its netstack at sandbox-create;
   193	           a `docker network connect` AFTER `run` adds the NIC at the Docker level
   194	           but the runsc netstack never sees it (the sandbox can't reach the proxy →
   195	           RST). So the sidecar is `create`d on bridge, `connect`ed to the internal
   196	           net, and only THEN `start`ed — both interfaces exist before runsc boots.
   197	
   198	        2. A WORKING resolver. On a user-defined network Docker forces
   199	           `nameserver 127.0.0.11` (its embedded DNS), and that resolver is
   200	           unreachable from a dual-homed gVisor box → the proxy can't resolve any
   201	           upstream. Routing to the internet by IP works fine, so we point the
   202	           sidecar's resolv.conf straight at public resolvers.
   203	
   204	        3. Reach the proxy by IP, not name. The SANDBOX (on the internal net) also
   205	           can't use the embedded DNS, so it can't resolve the sidecar's name. We
   206	           read the sidecar's internal-net IP and hand the sandbox HTTP(S)_PROXY by
   207	           IP."""
   208	        net_name = f"{EGR_NET_PREFIX}{instance_id}"
   209	        labels = {LABEL_CONV: conversation_id} if conversation_id else {}
   210	        # The network carries the same label as the containers so the orphan
   211	        # sweep can find it — its NAME is instance-keyed, not conversation-keyed.
   212	        network = client.networks.create(net_name, driver="bridge", internal=True, labels=labels)
   213	        # (1) create on bridge → connect internal → start, so runsc sees BOTH NICs.
   214	        sidecar = client.containers.create(
   215	            image=self._cfg.image,
   216	            command=["sh", "-c", "exec sleep infinity"],
   217	            runtime=self._cfg.runtime,
   218	            network="bridge",  # the route to the internet (the proxy's upstream)
   219	            mem_limit="256m",
   220	            detach=True,
   221	            name=net_name,
   222	            labels=labels,
   223	        )
   224	        network.connect(sidecar)  # the internal net the sandbox shares with it
   225	        sidecar.start()
   226	        # (2) replace the dead embedded resolver with public DNS over the (working) route.
   227	        sidecar.exec_run(
   228	            ["sh", "-c", 'printf "nameserver 1.1.1.1\\nnameserver 8.8.8.8\\n" > /etc/resolv.conf']
   229	        )
   230	        # Inject the proxy script + launch it in the background on the sidecar.
   231	        script = pathlib.Path(_egress_proxy_mod.__file__).read_bytes()
   232	        _put_file(sidecar, "/", "egress_proxy.py", script)
   233	        allow = format_allow(spec.egress_allow)
   234	        argv = proxy_run_argv(allow, EGRESS_PROXY_PORT)
   235	        sidecar.exec_run(["sh", "-c", f"{' '.join(argv)} >/var/log/egress.log 2>&1 &"], detach=True)
   236	        # (3) read the sidecar's IP on the internal net; the sandbox proxies by IP.
   237	        sidecar.reload()
   238	        proxy_ip = (
   239	            sidecar.attrs.get("NetworkSettings", {})
   240	            .get("Networks", {})
   241	            .get(net_name, {})
   242	            .get("IPAddress", "")
   243	        ) or net_name  # fall back to the name (harmless for the fake/test path)
   244	        env = proxy_env(proxy_ip, EGRESS_PROXY_PORT)
   245	        return network, sidecar, env, net_name
   246	
   247	    def _start_container(
   248	        self, spec: SandboxSpec, instance_id: str, host_workspace: str, conversation_id: str = ""
   249	    ) -> Any:
   250	        """All the blocking Docker work for `create`, run in a thread. Maps infra
   251	        failures to typed errors with the real cause. `host_workspace` is a path on
   252	        the DAEMON host (not necessarily local). Returns (container, egress_aux),
   253	        where egress_aux is (network, sidecar) for a filtered box else (None, None)."""
   254	        client = self._client()
   255	        self._require_runtime(client)
   256	        self._require_image(client)  # never-pull guard, before any run
   257	
   258	        # NOTE: do NOT mkdir the workspace locally — the bind-mount source is a path
   259	        # on the Docker DAEMON host, which Docker creates on demand. Making it here
   260	        # would (wrongly) create it on whatever host runs the backend.

exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/sandbox/session.py | sed -n '1,260p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""SandboxSession — a resilient, task-scoped sandbox shared across tool calls.
     2	
     3	tool-sandbox-contract §5: a task gets ONE sandbox, created once and reused by many
     4	tool calls (`create once → exec many → close`), not a fresh container per command.
     5	
     6	The load-bearing behavior (carried lesson #1 from the sandbox arc): a box can die
     7	MID-SESSION — an OOM kills the WHOLE box, not just the offending command (the VM 202
     8	finding). When that happens, the session catches it (`SandboxUnavailableError`, the
     9	typed death signal the backends now raise), RE-CREATES the underlying instance, and
    10	raises a clean `SandboxError` the executor turns into an error `ToolResult` the loop
    11	can see — it never wedges. The agent observes "the box died and was re-created", and
    12	its next tool call runs on the fresh box.
    13	
    14	`SandboxSession` implements the `SandboxInstance` protocol, so the `DefaultToolExecutor`
    15	and the tools use it UNCHANGED — it's a drop-in, self-healing instance.
    16	"""
    17	
    18	from __future__ import annotations
    19	
    20	import asyncio
    21	import logging
    22	from collections.abc import Awaitable, Callable
    23	from dataclasses import dataclass
    24	from typing import Any, cast
    25	
    26	from ._container import PREVIEW_PORT, USER_PORTS
    27	from .base import (
    28	    ExecResult,
    29	    SandboxError,
    30	    SandboxInstance,
    31	    SandboxService,
    32	    SandboxSpec,
    33	    SandboxUnavailableError,
    34	)
    35	
    36	_LOG = logging.getLogger(__name__)
    37	
    38	
    39	@dataclass
    40	class TrackedService:
    41	    """BP-G9 — metadata for one USER_PORT service the session is responsible
    42	    for. A `TrackedService` is registered when the agent (or the static
    43	    auto-preview) starts a long-running server bound to a curated USER_PORT,
    44	    and is the source of truth for "what's exposed on this box right now".
    45	    The session can list every tracked service and its URL — so multi-service
    46	    builds (API + frontend, …) are first-class, not a special case of the
    47	    single 'preview' path.
    48	
    49	    `name` is the tmux session name (no namespace prefix); `port` is the
    50	    USER_PORT the service binds; `command` is the full command string
    51	    originally issued; `exec_dir` is the cwd it was launched in. The C3
    52	    rematerialize hook re-issues the same `command` on a fresh box, so a
    53	    multi-service build survives suspend/wake end-to-end (BP-G9 acceptance).
    54	    """
    55	    name: str
    56	    port: int
    57	    command: str
    58	    exec_dir: str | None
    59	
    60	
    61	class SandboxSession:
    62	    """[CONTRACT boundary] A task's sandbox, made resilient. Lazily creates the
    63	    instance, reuses it across calls, and re-creates it on a mid-session death. Drop-in
    64	    `SandboxInstance` — the executor/tools are agnostic to the self-healing."""
    65	
    66	    def __init__(
    67	        self,
    68	        service: SandboxService,
    69	        spec: SandboxSpec | None = None,
    70	        *,
    71	        owner_id: str = "local",
    72	        conversation_id: str = "conv",
    73	        on_recreate: Callable[[], Awaitable[None]] | None = None,
    74	        kernel_idle_timeout_s: float | None = None,
    75	    ) -> None:
    76	        self._service = service
    77	        self._spec = spec or SandboxSpec()
    78	        self.owner_id = owner_id
    79	        self.conversation_id = conversation_id
    80	        # Called (best-effort) AFTER a mid-session death is replaced by a fresh
    81	        # instance — the runtime hooks workspace rehydration here so the agent's
    82	        # retry lands on its files, not an empty dir (bp-13 §2: the conv_f3bdc842
    83	        # "all files were lost" production incident).
    84	        self._on_recreate = on_recreate
    85	        # C5 — MEMORY recovery cache. `_recreate` reads `.pmx/MEMORY.md` from
    86	        # the fresh box and stages (scope, snippet) pairs here; the agent loop
    87	        # drains them on its next step (via take_recovered_memory_facts) and
    88	        # re-emits each as a KnowledgeEvent, restoring the in-View channel to
    89	        # match the surviving on-disk mirror. The cache is consumed once
    90	        # (take_ clears it) so the recovery fires exactly once per recreate.
    91	        # None = no recovery attempted yet, OR a fresh box that had no
    92	        # `.pmx/MEMORY.md` (no prior remember to recover).
    93	        self._recovered_memory_facts: list[tuple[str, str]] | None = None
    94	        # BP-G9 — multi-service tracking. One entry per USER_PORT this session
    95	        # is responsible for: the static auto-preview (port 8000) is the
    96	        # default; any agent-launched `ensure_service(name, port, command)`
    97	        # adds another. Keyed by port so a UI/runtime can ask "is 3000
    98	        # already claimed by something on this box?" and "what's its URL?"
    99	        # in O(1). Survives `_recreate` — the C3 rematerialize hook re-issues
   100	        # each tracked service on the fresh instance. `_persistent_servers`
   101	        # (C3) is keyed by session name and is the IMPLICIT detection path
   102	        # for `shell_exec`-launched servers; this dict is the EXPLICIT
   103	        # registration path for `ensure_preview` / `ensure_service`. Both
   104	        # source the wake machinery's rematerialize step.
   105	        self._tracked_services: dict[int, TrackedService] = {}
   106	        # C15: idle-cull threshold for the persistent CodeAct kernel. None ->
   107	        # read DISCO_KERNEL_IDLE_TIMEOUT_S at first use; 0 disables culling.
   108	        self._kernel_idle_timeout_s = kernel_idle_timeout_s
   109	        self.spec = self._spec
   110	        self._instance: SandboxInstance | None = None
   111	        self._closed = False
   112	        self._generation = 0  # bumped on every (re)create — telemetry + tests
   113	        self._lock = asyncio.Lock()
   114	        self._preview_task: asyncio.Task[None] | None = None  # tracked so destroy() can cancel
   115	
   116	        from .shell_sessions import ShellSessionManager
   117	        # Process backend shares the host tmux server across conversations, so
   118	        # session names need a per-conversation namespace; container backends get
   119	        # an isolated tmux server each (service.name per SandboxService protocol).
   120	        ns = f"{conversation_id[:8]}-" if service.name == "process" else ""
   121	        self.sessions = ShellSessionManager(self._ensure, namespace=ns)
   122	        self._kernel: Any | None = None
   123	
   124	    @property
   125	    def id(self) -> str:
   126	        if self._instance is not None:
   127	            return self._instance.id
   128	        return f"session-{self.conversation_id}"
   129	
   130	    @property
   131	    def generation(self) -> int:
   132	        """How many underlying instances this session has created (1 after the first
   133	        use; >1 means it survived a death)."""
   134	        return self._generation
   135	
   136	    @property
   137	    async def kernel(self) -> Any:
   138	        """The persistent IPython kernel for this session. Lazily created on first
   139	        use; transport chosen by backend. Wrapped in a `ManagedKernel` (C15) that
   140	        culls the inner kernel after `kernel_idle_timeout_s` of inactivity and
   141	        re-spawns on the next exec — so a quiet conversation doesn't hold a
   142	        dead kernel's RAM forever."""
   143	        if self._kernel is None:
   144	            inst = await self._ensure()
   145	            from .kernel import ManagedKernel, _default_idle_timeout_s
   146	            timeout = (
   147	                self._kernel_idle_timeout_s
   148	                if self._kernel_idle_timeout_s is not None
   149	                else _default_idle_timeout_s()
   150	            )
   151	            if self._service.name == "process":
   152	                from .kernel import ProcessKernel
   153	                res = await inst.exec_shell("pwd", timeout_s=5)
   154	                ws = res.stdout.strip()
   155	                self._kernel = ManagedKernel(
   156	                    lambda: ProcessKernel(ws),
   157	                    idle_timeout_s=timeout,
   158	                )
   159	            else:
   160	                from .kernel import GatewayKernel
   161	                self._kernel = ManagedKernel(
   162	                    lambda: GatewayKernel(inst, self.sessions),
   163	                    idle_timeout_s=timeout,
   164	                )
   165	        return self._kernel
   166	
   167	    async def _ensure(self) -> SandboxInstance:
   168	        if self._closed:
   169	            raise SandboxError("sandbox session is closed")
   170	        created = False
   171	        async with self._lock:
   172	            if self._instance is None:
   173	                self._instance = await self._service.create(
   174	                    self._spec, owner_id=self.owner_id, conversation_id=self.conversation_id
   175	                )
   176	                self._generation += 1
   177	                created = True
   178	        if created:
   179	            self._spawn_auto_preview()
   180	        return self._instance
   181	
   182	    async def _recreate(self, dead: SandboxInstance) -> None:
   183	        """Replace a dead instance with a fresh one. Idempotent under concurrent
   184	        callers (only the first past the lock with the dead instance re-creates). A
   185	        create() that itself fails (infra truly down) propagates as
   186	        SandboxUnavailableError — the session can't paper over a dead host."""
   187	        async with self._lock:
   188	            if self._closed or self._instance is not dead:
   189	                return  # someone else already handled it, or we were closed
   190	            self._instance = None
   191	            try:
   192	                await dead.destroy()
   193	            except Exception:  # noqa: BLE001 — it's already gone; best-effort cleanup
   194	                pass
   195	            self._instance = await self._service.create(
   196	                self._spec, owner_id=self.owner_id, conversation_id=self.conversation_id
   197	            )
   198	            self._generation += 1
   199	            self.sessions.reset_known_sessions()
   200	            # C15: tear down the old ManagedKernel so its inner kernel process
   201	            # doesn't outlive the dead sandbox (the kernel is a child of the
   202	            # container for gateway backends, but a local subprocess for
   203	            # process backends — we always try to shut down cleanly).
   204	            old_kernel, self._kernel = self._kernel, None
   205	            if old_kernel is not None:
   206	                try:
   207	                    await old_kernel.shutdown()
   208	                except Exception:  # noqa: BLE001 — best-effort; the box is already gone
   209	                    _LOG.debug("kernel shutdown on recreate failed", exc_info=True)
   210	        # the old box took the 'preview' session down with it — bring it back up
   211	        self._spawn_auto_preview()
   212	        # Rehydrate the fresh (empty) workspace from the last snapshot, if the
   213	        # owner wired a hook. Outside the lock — the hook writes files back
   214	        # through this session, which must be able to _ensure() freely. Failures
   215	        # are logged, never raised: the agent can always rebuild by hand, which
   216	        # is exactly the (worse) status quo this hook exists to avoid.
   217	        if self._on_recreate is not None:
   218	            try:
   219	                await self._on_recreate()
   220	            except Exception:  # noqa: BLE001 — best-effort restore
   221	                _LOG.warning(
   222	                    "post-recreate rehydrate failed for %s", self.conversation_id, exc_info=True
   223	                )
   224	        # C5 — read-back: pull `.pmx/MEMORY.md` (the write-through mirror of
   225	        # the in-View KnowledgeEvent channel) off the fresh box and stage
   226	        # the facts for the agent loop to re-emit. The in-View channel is
   227	        # authoritative in-session; the file is the durable copy. A hard
   228	        # reset / box wipe erases the in-memory View but the file persists,
   229	        # so this read-back is the recovery path. Best-effort: a missing
   230	        # file (no prior remember) leaves the cache empty, and any I/O
   231	        # failure is logged but never raised.
   232	        try:
   233	            await self._recover_pmx_memory()
   234	        except Exception:  # noqa: BLE001 — recovery is a convenience, never wedge the box
   235	            _LOG.debug("pmx memory read-back failed", exc_info=True)
   236	        # C3: re-materialize the agent's own dev servers (vite / express /
   237	        # uvicorn / http.server on a non-default USER_PORT, …) on the fresh
   238	        # instance. Until this hook, only the static `python3 -m http.server`
   239	        # preview survived a recreate — a real app the agent launched simply
   240	        # vanished on suspend/wake. The shell-sessions manager records each
   241	        # port-binding command in `exec()` and replays them here, best-effort,
   242	        # skipping ports already bound (the no-duplication guarantee).
   243	        try:
   244	            logs = await self.sessions.rehydrate_persistent_servers()
   245	            for line in logs:
   246	                _LOG.info("post-recreate: %s", line)
   247	        except Exception:  # noqa: BLE001 — rehydrate is a convenience; never wedge the box
   248	            _LOG.warning(
   249	                "post-recreate server rehydrate failed for %s",
   250	                self.conversation_id,
   251	                exc_info=True,
   252	            )
   253	        # C3: re-materialize the agent's own dev servers (vite / express /
   254	        # uvicorn / http.server on a non-default USER_PORT, …) on the fresh
   255	        # instance. Until this hook, only the static `python3 -m http.server`
   256	        # preview survived a recreate — a real app the agent launched simply
   257	        # vanished on suspend/wake. The shell-sessions manager records each
   258	        # port-binding command in `exec()` and replays them here, best-effort,
   259	        # skipping ports already bound (the no-duplication guarantee).
   260	        try:

codex
So far, `gvisor` inherits the fixed container read path, but `session.py` is the choke point I need to inspect closely: it can convert backend exceptions before the loop sees them. Next I’m reading the resilient wrapper and its tests.
exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/sandbox/process.py | sed -n '1,150p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""Process sandbox backend — tool-sandbox-contract.md §5.1 (development only).
     2	
     3	A local subprocess with a per-instance workspace dir. Weak isolation (no
     4	hardware/microVM boundary) — DEV ONLY; production uses the `e2b`/Firecracker
     5	backend behind the same `SandboxService` interface (§5.1, deferred). Two
     6	properties it DOES uphold, which the security tests rely on:
     7	  - subprocesses run with a CLEAN minimal env — never `os.environ` — so no host
     8	    secret or config leaks into the box (§6 rule 1, defense in depth);
     9	  - file ops are jailed to the workspace (path-escape attempts are rejected).
    10	
    11	[CONTRACT caveat §7 rule 3] the `process` backend does NOT truly block network
    12	egress (that needs OS namespaces / a proxy, which `e2b`/`gvisor` provide). The
    13	deny-by-default egress *policy* is modeled on `SandboxSpec.egress_allowed(...)`
    14	and enforced by network-using tools; the documented guarantee here is weaker
    15	than production.
    16	"""
    17	
    18	from __future__ import annotations
    19	
    20	import asyncio
    21	import shlex
    22	import shutil
    23	import tempfile
    24	import time
    25	import uuid
    26	from pathlib import Path
    27	
    28	from .base import (
    29	    ExecResult,
    30	    SandboxError,
    31	    SandboxInstance,
    32	    SandboxSpec,
    33	    strip_redundant_workspace_prefix,
    34	)
    35	
    36	
    37	class ProcessSandboxInstance:
    38	    """[CONTRACT boundary] An in-subprocess instance with a jailed workspace."""
    39	
    40	    def __init__(
    41	        self, id: str, owner_id: str, conversation_id: str, spec: SandboxSpec, workspace: Path
    42	    ) -> None:
    43	        self.id = id
    44	        self.owner_id = owner_id
    45	        self.conversation_id = conversation_id
    46	        self.spec = spec
    47	        self._workspace = workspace.resolve()
    48	        self._destroyed = False
    49	
    50	    def _alive(self) -> None:
    51	        if self._destroyed:
    52	            raise SandboxError(f"sandbox instance {self.id} has been destroyed")
    53	
    54	    def _resolve(self, path: str) -> Path:
    55	        """Resolve `path` within the workspace; reject escapes (../, absolute)."""
    56	        path = strip_redundant_workspace_prefix(path)  # ROOT-2: workspace/foo → foo
    57	        target = (self._workspace / path).resolve()
    58	        if target != self._workspace and self._workspace not in target.parents:
    59	            raise SandboxError(f"path escapes workspace: {path!r}")
    60	        return target
    61	
    62	    def _clean_env(self) -> dict[str, str]:
    63	        # No os.environ — nothing from the host (incl. any real secret) leaks in.
    64	        return {
    65	            "PATH": "/usr/local/bin:/usr/bin:/bin",
    66	            "HOME": str(self._workspace),
    67	            "TMPDIR": str(self._workspace),
    68	        }
    69	
    70	    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
    71	        self._alive()
    72	        proc = await asyncio.create_subprocess_shell(
    73	            cmd,
    74	            cwd=str(self._workspace),
    75	            env=self._clean_env(),
    76	            stdout=asyncio.subprocess.PIPE,
    77	            stderr=asyncio.subprocess.PIPE,
    78	        )
    79	        try:
    80	            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    81	        except TimeoutError:
    82	            proc.kill()
    83	            await proc.wait()
    84	            # Report timed-out (not raised), consistent with the gVisor sibling: the
    85	            # caller gets the flag + exit code rather than losing it to an exception.
    86	            return ExecResult(
    87	                exit_code=124,
    88	                stdout="",
    89	                stderr=f"command timed out after {timeout_s}s",
    90	                timed_out=True,
    91	            )
    92	        return ExecResult(
    93	            exit_code=proc.returncode if proc.returncode is not None else -1,
    94	            stdout=out.decode("utf-8", errors="replace"),
    95	            stderr=err.decode("utf-8", errors="replace"),
    96	        )
    97	
    98	    async def read_file(self, path: str) -> bytes:
    99	        self._alive()
   100	        return self._resolve(path).read_bytes()
   101	
   102	    async def write_file(self, path: str, data: bytes) -> None:
   103	        self._alive()
   104	        target = self._resolve(path)
   105	        target.parent.mkdir(parents=True, exist_ok=True)
   106	        target.write_bytes(data)
   107	
   108	    async def list_dir(self, path: str) -> list[str]:
   109	        self._alive()
   110	        return sorted(p.name for p in self._resolve(path).iterdir())
   111	
   112	    async def file_exists(self, path: str) -> bool:
   113	        """[B4] Workspace-jailed existence check. The process workspace lives on
   114	        the host FS, so this is a direct `Path.exists()` against the resolved
   115	        target. A path that escapes the jail is False (not raised — the predicate
   116	        named an out-of-scope path, which simply does not exist *in* the
   117	        workspace); a plain absence is False."""
   118	        self._alive()
   119	        try:
   120	            target = self._resolve(path)
   121	        except SandboxError:
   122	            return False
   123	        return target.exists()
   124	
   125	    @property
   126	    def workspace_path(self) -> str | None:
   127	        """W5 — expose the workspace root for C18 / C1c predicate resolution.
   128	        Process backend: always an absolute host path (the temp dir is on the host).
   129	        Container backends return None (they have a separate FS namespace)."""
   130	        return str(self._workspace)
   131	
   132	    def display_url(self) -> str | None:
   133	        return None  # the process backend has no display
   134	
   135	    def expose_port(self, port: int) -> str | None:
   136	        """Dev-mode usability: host processes bind host ports directly, so hand
   137	        back the local URL when the port is actually bound. No isolation boundary
   138	        to defend on this backend, but stay within the curated USER set."""
   139	        from ._container import USER_PORTS
   140	
   141	        if port not in USER_PORTS:
   142	            return None
   143	        import socket
   144	
   145	        try:
   146	            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
   147	                pass
   148	        except OSError:
   149	            return None
   150	        return f"http://127.0.0.1:{port}"

exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/sandbox/session.py | sed -n '260,430p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   260	        try:
   261	            logs = await self.sessions.rehydrate_persistent_servers()
   262	            for line in logs:
   263	                _LOG.info("post-recreate: %s", line)
   264	        except Exception:  # noqa: BLE001 — rehydrate is a convenience; never wedge the box
   265	            _LOG.warning(
   266	                "post-recreate server rehydrate failed for %s",
   267	                self.conversation_id,
   268	                exc_info=True,
   269	            )
   270	
   271	    def _spawn_auto_preview(self) -> None:
   272	        """Fire-and-forget the static auto-serve (BP-02): every fresh box comes up with
   273	        the workspace served as session 'preview'. Idempotent and polite — if anything
   274	        already owns the port (e.g. the process backend's host, where :8000 is the
   275	        agent-server itself), ensure_preview() backs off with False. Failures are
   276	        logged, never raised: preview is a convenience, not a dependency of the box."""
   277	
   278	        async def _auto() -> None:
   279	            try:
   280	                await self.ensure_preview()
   281	            except Exception:  # noqa: BLE001 — best-effort; the box must not care
   282	                _LOG.debug("auto preview start failed", exc_info=True)
   283	
   284	        self._preview_task = asyncio.create_task(_auto())
   285	
   286	    async def _resilient(self, op):
   287	        """Run one instance op; on a typed mid-session death, re-create and raise a
   288	        clean SandboxError (→ the executor's `sandbox_error` ToolResult) so the loop
   289	        sees it and the NEXT call lands on the fresh box. Never wedges."""
   290	        inst = await self._ensure()
   291	        try:
   292	            return await op(inst)
   293	        except SandboxUnavailableError as exc:
   294	            await self._recreate(inst)
   295	            raise SandboxError(
   296	                f"sandbox died mid-session and was re-created (generation {self._generation}); "
   297	                f"retry the action"
   298	            ) from exc
   299	
   300	    # ---- C5: MEMORY write-through read-back ---------------------------------
   301	
   302	    # Path of the on-disk MEMORY mirror. Must match engine.py's _MEMORY_PATH
   303	    # exactly — these two are the only writers/readers of the file and the
   304	    # read-back's parse (## scope heading + list items) must match the
   305	    # write-through's format. `_LEGACY_MEMORY_PATH` (the pre-rename `.pmx/` path)
   306	    # is read as a fallback so a resumed pre-upgrade workspace isn't lost.
   307	    _MEMORY_PATH = ".disco/MEMORY.md"
   308	    _LEGACY_MEMORY_PATH = ".pmx/MEMORY.md"
   309	
   310	    async def _recover_pmx_memory(self) -> None:
   311	        """C5 — read `.disco/MEMORY.md` (legacy `.pmx/` as a fallback) from the LIVE
   312	        instance and stage its facts in `self._recovered_memory_facts` for the agent
   313	        loop to drain. Called from `_recreate` AFTER the user's on_recreate hook has
   314	        run and the fresh box is up.
   315	
   316	        A missing file (no prior `remember` calls → no facts to recover) is
   317	        a normal, silent no-op: the cache stays None and the loop sees no
   318	        recovery. A present-but-malformed file is logged and ignored: a
   319	        bad mirror must not break the next step. A transient I/O error is
   320	        also best-effort (a dead box can't recover, but the loop will
   321	        surface the error via the next tool call anyway).
   322	        """
   323	        data = None
   324	        for mem_path in (self._MEMORY_PATH, self._LEGACY_MEMORY_PATH):
   325	            try:
   326	                data = await self.read_file(mem_path)
   327	                break
   328	            except (FileNotFoundError, NotADirectoryError):
   329	                continue  # try the legacy path, then give up
   330	            except Exception:  # noqa: BLE001 — read flakiness on a fresh box is best-effort
   331	                _LOG.debug("disco memory read-back failed (no facts recovered)", exc_info=True)
   332	                self._recovered_memory_facts = None
   333	                return
   334	        if data is None:
   335	            # No mirror (new or legacy) on the new box — nothing to recover. Leave
   336	            # cache None (a fresh box / no prior remember) so the loop sees clean state.
   337	            self._recovered_memory_facts = None
   338	            return
   339	        if not data:
   340	            self._recovered_memory_facts = None
   341	            return
   342	        try:
   343	            text = data.decode("utf-8", errors="replace")
   344	        except Exception:  # noqa: BLE001 — decode error
   345	            self._recovered_memory_facts = None
   346	            return
   347	        facts: list[tuple[str, str]] = []
   348	        current_scope = ""
   349	        for raw in text.splitlines():
   350	            line = raw.rstrip()
   351	            if line.startswith("## "):
   352	                current_scope = line[3:].strip()
   353	                continue
   354	            # Skip the leading comment / headings — they're prose, not facts.
   355	            if line.startswith("# "):
   356	                continue
   357	            # List item: `- <snippet>`. The write-through only ever produces
   358	            # `- ` list items under `## <scope>` headings, so this parser
   359	            # matches the writer exactly. (An empty line just advances.)
   360	            if line.startswith("- "):
   361	                snippet = line[2:].strip()
   362	                if snippet:
   363	                    facts.append((current_scope, snippet))
   364	        self._recovered_memory_facts = facts or None
   365	
   366	    def take_recovered_memory_facts(self) -> list[tuple[str, str]]:
   367	        """C5 — pop the staged recovery facts (set by `_recover_pmx_memory`).
   368	        The agent loop calls this on each step to drain the cache; it
   369	        returns an empty list and clears the cache if nothing is staged.
   370	        Consuming once-per-recreate is the contract: a second call on
   371	        the same session returns []. The list elements are `(scope, snippet)`
   372	        pairs ready to be re-emitted as KnowledgeEvents.
   373	        """
   374	        facts = self._recovered_memory_facts or []
   375	        self._recovered_memory_facts = None
   376	        return facts
   377	
   378	    def peek_recovered_memory_facts(self) -> list[tuple[str, str]]:
   379	        """C5 — read-only variant of `take_recovered_memory_facts` for tests
   380	        and diagnostics. Does NOT clear the cache — use `take_` to consume.
   381	        """
   382	        return list(self._recovered_memory_facts or [])
   383	
   384	    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
   385	        return await self._resilient(lambda i: i.exec_shell(cmd, timeout_s=timeout_s))
   386	
   387	    async def read_file(self, path: str) -> bytes:
   388	        return await self._resilient(lambda i: i.read_file(path))
   389	
   390	    async def write_file(self, path: str, data: bytes) -> None:
   391	        await self._resilient(lambda i: i.write_file(path, data))
   392	
   393	    async def list_dir(self, path: str) -> list[str]:
   394	        return await self._resilient(lambda i: i.list_dir(path))
   395	
   396	    async def file_exists(self, path: str) -> bool:
   397	        """[B4] Delegate the existence check to the live instance (which resolves
   398	        in its OWN namespace — host FS for process, inside-the-box for container).
   399	        Goes through `_resilient`, so a mid-session box death re-creates and
   400	        raises a clean SandboxError the C18 advisory treats as unverifiable."""
   401	        return await self._resilient(lambda i: i.file_exists(path))
   402	
   403	    @property
   404	    def workspace_path(self) -> str | None:
   405	        """W5 — expose the workspace root for C18 / C1c predicate resolution.
   406	        Delegates to the inner SandboxInstance; None when no instance exists yet
   407	        or when the backend doesn't expose a host-side path (container backends)."""
   408	        if self._instance is not None:
   409	            return getattr(self._instance, "workspace_path", None)
   410	        return None
   411	
   412	    def display_url(self) -> str | None:
   413	        return self._instance.display_url() if self._instance is not None else None
   414	
   415	    def expose_port(self, port: int) -> str | None:
   416	        return self._instance.expose_port(port) if self._instance is not None else None
   417	
   418	    async def _detect_serve_dir(self, inst: SandboxInstance, workspace: str) -> str:
   419	        """W6 — detect the subdirectory containing index.html and serve THAT dir.
   420	
   421	        Supports subdir apps (e.g. `macos-clone/index.html`): the preview should
   422	        serve the subdir, not the workspace root (which shows raw files instead of
   423	        the app). Falls back to workspace root when no index.html is found or the
   424	        shell command fails.
   425	
   426	        Skips `.pmx/` and `node_modules/` — those directories are internal and
   427	        should never be the serve root."""
   428	        try:
   429	            res = await inst.exec_shell(
   430	                # Find first index.html, skipping internal dirs, sort shallowest first

exec
/bin/bash -lc "nl -ba packages/tools/tests/test_sandbox_exception_contract.py | sed -n '1,160p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""W1 — sandbox exception CONTRACT (the silent-hang root cause).
     2	
     3	A missing-file sandbox read MUST satisfy ``isinstance(e, FileNotFoundError)`` and no
     4	*untyped* ``SandboxError`` may leak past the read boundary. The container/podman backends
     5	ran ``cat`` and wrapped a missing file in a plain ``SandboxError`` (subclass of Exception,
     6	NOT FileNotFoundError) — which silently defeated ~8 ``except (FileNotFoundError, OSError)``
     7	handlers across the loop, so the exception escaped, killed the loop task, and left the build
     8	status RUNNING forever. The local (process) backend already raised native FileNotFoundError;
     9	this contract makes every backend agree.
    10	"""
    11	
    12	import tempfile
    13	from pathlib import Path
    14	
    15	import pytest
    16	from disco.tools.sandbox.base import (
    17	    SandboxError,
    18	    SandboxFileNotFoundError,
    19	    looks_like_not_found,
    20	    raise_read_error,
    21	)
    22	
    23	pytestmark = pytest.mark.boundary_contract
    24	
    25	
    26	def test_sandbox_file_not_found_is_both_types():
    27	    """Multiple inheritance is real and instantiable (codex caveat: prove it)."""
    28	    e = SandboxFileNotFoundError("read_file 'x': cat: /workspace/x: No such file")
    29	    assert isinstance(e, FileNotFoundError)  # the 8 handlers catch it
    30	    assert isinstance(e, OSError)  # FileNotFoundError IS an OSError
    31	    assert isinstance(e, SandboxError)  # still a SandboxError for the executor's map
    32	
    33	
    34	@pytest.mark.parametrize(
    35	    "stderr,expected",
    36	    [
    37	        (b"cat: /workspace/style.css: No such file or directory", True),
    38	        (b"cat: cannot open 'x' for reading: No such file or directory", True),
    39	        ("No such file or directory", True),
    40	        (b"/bin/sh: 1: cat: not found", False),  # the TOOL is missing, not the file
    41	        (b"permission denied", False),
    42	        (b"some unrelated failure", False),
    43	        (b"", False),
    44	    ],
    45	)
    46	def test_looks_like_not_found(stderr, expected):
    47	    assert looks_like_not_found(stderr) is expected
    48	
    49	
    50	def test_raise_read_error_types_by_stderr():
    51	    """The shared boundary helper: not-found stderr → typed; anything else → SandboxError
    52	    (but NEVER a bare untyped escape — it always raises a SandboxError subclass)."""
    53	    with pytest.raises(SandboxFileNotFoundError):
    54	        raise_read_error("style.css", b"cat: /workspace/style.css: No such file or directory")
    55	    # a non-not-found failure stays a SandboxError (not a FileNotFoundError false-positive)
    56	    with pytest.raises(SandboxError) as ei:
    57	        raise_read_error("x", b"permission denied")
    58	    assert not isinstance(ei.value, FileNotFoundError)
    59	
    60	
    61	async def test_process_backend_missing_file_is_native_filenotfound():
    62	    """The local backend already raises native FileNotFoundError — lock it in."""
    63	    from disco.tools.sandbox.base import SandboxSpec
    64	    from disco.tools.sandbox.process import ProcessSandboxInstance
    65	
    66	    with tempfile.TemporaryDirectory() as d:
    67	        sbx = ProcessSandboxInstance(
    68	            id="t", owner_id="local", conversation_id="c", spec=SandboxSpec(), workspace=Path(d)
    69	        )
    70	        with pytest.raises(FileNotFoundError):
    71	            await sbx.read_file("does-not-exist.txt")

exec
/bin/bash -lc "rg -n \"except \\(FileNotFoundError|SandboxError|read_file\\(|FileNotFoundError\" packages/core/src/disco/core packages/agent-server/src/disco/agent_server packages/tools/src/disco/tools | head -n 200" in /var/home/dylan/projects/disco
 succeeded in 0ms:
packages/tools/src/disco/tools/executor.py:24:from .sandbox.base import SandboxError, SandboxInstance
packages/tools/src/disco/tools/executor.py:161:        except SandboxError as e:
packages/tools/src/disco/tools/__init__.py:37:    SandboxError,
packages/tools/src/disco/tools/__init__.py:65:    "SandboxError",
packages/agent-server/src/disco/agent_server/verify/scenarios.py:49:# hang." (The crash→ERROR path of W1/W2 — an uncaught SandboxError in the loop's own
packages/tools/src/disco/tools/projects/archive.py:61:    async def read_file(self, path: str) -> bytes: ...
packages/tools/src/disco/tools/projects/archive.py:141:            # raises SandboxError. We try read_file first (the common case is a
packages/tools/src/disco/tools/projects/archive.py:146:                data = await sandbox.read_file(child)
packages/tools/src/disco/tools/projects/archive.py:207:        raise FileNotFoundError(f"workspace directory not found: {src}")
packages/agent-server/src/disco/agent_server/report_export.py:712:    return await sandbox.read_file("_export.docx")
packages/tools/src/disco/tools/builtin/files.py:245:        data = await ctx.sandbox.read_file(args.path)
packages/tools/src/disco/tools/builtin/files.py:250:        # ctx.sandbox.read_file() calls inside _gated_write and each mutator's
packages/tools/src/disco/tools/builtin/files.py:347:            old_text = (await ctx.sandbox.read_file(args.path)).decode("utf-8", errors="replace")
packages/tools/src/disco/tools/builtin/files.py:414:            existing = await ctx.sandbox.read_file(args.path)
packages/tools/src/disco/tools/builtin/files.py:537:        text = (await ctx.sandbox.read_file(args.path)).decode("utf-8", errors="replace")
packages/tools/src/disco/tools/builtin/files.py:604:        text = (await ctx.sandbox.read_file(args.path)).decode("utf-8", errors="replace")
packages/tools/src/disco/tools/builtin/files.py:679:        text = (await ctx.sandbox.read_file(args.path)).decode("utf-8", errors="replace")
packages/tools/src/disco/tools/builtin/files.py:760:        text = (await ctx.sandbox.read_file(args.path)).decode("utf-8", errors="replace")
packages/core/src/disco/core/dod_evaluator.py:121:    FileNotFoundError: '/bin/nosuch'". The verdict turns a non-passing
packages/core/src/disco/core/skills.py:140:            skill = self._read_file(f)
packages/core/src/disco/core/skills.py:148:        return self._read_file(f) if (f is not None and f.is_file()) else None
packages/core/src/disco/core/skills.py:204:    def _read_file(self, f: Path) -> Skill | None:
packages/tools/src/disco/tools/builtin/_deck_patch.py:292:            raw_bytes = await ctx.sandbox.read_file(args.deck_file)
packages/tools/src/disco/tools/builtin/_deck_patch.py:350:                data = await ctx.sandbox.read_file(f"{stem}_img_{i}.png")
packages/tools/src/disco/tools/builtin/live_view.py:97:    except (FileNotFoundError, subprocess.TimeoutExpired):
packages/tools/src/disco/tools/builtin/live_view.py:124:    except FileNotFoundError:
packages/agent-server/src/disco/agent_server/routes/conversations.py:223:                data = await session.read_file(norm)
packages/core/src/disco/core/loop/recitation.py:269:            existing = (await sbx.read_file(path)).decode("utf-8", errors="replace")
packages/core/src/disco/core/loop/recitation.py:270:        except (FileNotFoundError, NotADirectoryError):
packages/core/src/disco/core/loop/recitation.py:273:                    await sbx.read_file(self._loop._LEGACY_MEMORY_PATH)
packages/agent-server/src/disco/agent_server/routes/files.py:70:            return await session.read_file(norm)
packages/agent-server/src/disco/agent_server/routes/files.py:128:                    existing_bytes += len(await session.read_file(f"uploads/{fname}"))
packages/core/src/disco/core/loop/observe.py:114:                await sbx.read_file(memory_path)
packages/core/src/disco/core/loop/observe.py:117:            except (FileNotFoundError, NotADirectoryError):
packages/core/src/disco/core/loop/file_state.py:141:                    sbx.read_file(path),  # type: ignore[attr-defined]
packages/core/src/disco/core/loop/file_state.py:144:            except (FileNotFoundError, TimeoutError, PermissionError, OSError):
packages/core/src/disco/core/loop/view_render.py:83:        raw = await asyncio.wait_for(sbx.read_file(path), timeout=_WS_READ_TIMEOUT_S)
packages/core/src/disco/core/loop/view_render.py:89:    except FileNotFoundError:
packages/core/src/disco/core/loop/boundaries.py:88:    async def read_file(self, path: str) -> bytes: ...
packages/tools/src/disco/tools/sandbox/__init__.py:8:    SandboxError,
packages/tools/src/disco/tools/sandbox/__init__.py:40:    "SandboxError",
packages/tools/src/disco/tools/sandbox/kernel.py:20:from .base import SandboxError
packages/tools/src/disco/tools/sandbox/kernel.py:399:        raise SandboxError(
packages/tools/src/disco/tools/sandbox/base.py:39:class SandboxError(Exception):
packages/tools/src/disco/tools/sandbox/base.py:44:class SandboxUnavailableError(SandboxError):
packages/tools/src/disco/tools/sandbox/base.py:52:class SandboxFileNotFoundError(SandboxError, FileNotFoundError):
packages/tools/src/disco/tools/sandbox/base.py:53:    """W1: a workspace file/dir was MISSING on a read. Subclasses BOTH SandboxError (so the
packages/tools/src/disco/tools/sandbox/base.py:54:    executor's `sandbox_error` mapping still applies) AND FileNotFoundError (so the loop's
packages/tools/src/disco/tools/sandbox/base.py:55:    ~8 ``except (FileNotFoundError, OSError)`` bookkeeping handlers — file_state,
packages/tools/src/disco/tools/sandbox/base.py:58:    wrap a missing file in a plain SandboxError, defeating those handlers; the local
packages/tools/src/disco/tools/sandbox/base.py:59:    (process) backend already raised native FileNotFoundError. This makes them agree."""
packages/tools/src/disco/tools/sandbox/base.py:69:    renamed/deleted file becomes a FileNotFoundError, not a fatal untyped escape."""
packages/tools/src/disco/tools/sandbox/base.py:76:    file → typed ``SandboxFileNotFoundError``; anything else → ``SandboxError``. It never
packages/tools/src/disco/tools/sandbox/base.py:82:        raise SandboxFileNotFoundError(f"read_file {path!r}: {detail}")
packages/tools/src/disco/tools/sandbox/base.py:83:    raise SandboxError(f"read_file {path!r}: {detail}")
packages/tools/src/disco/tools/sandbox/base.py:162:    async def read_file(self, path: str) -> bytes: ...
packages/tools/src/disco/tools/sandbox/process.py:30:    SandboxError,
packages/tools/src/disco/tools/sandbox/process.py:52:            raise SandboxError(f"sandbox instance {self.id} has been destroyed")
packages/tools/src/disco/tools/sandbox/process.py:59:            raise SandboxError(f"path escapes workspace: {path!r}")
packages/tools/src/disco/tools/sandbox/process.py:98:    async def read_file(self, path: str) -> bytes:
packages/tools/src/disco/tools/sandbox/process.py:121:        except SandboxError:
packages/tools/src/disco/tools/sandbox/podman.py:55:    SandboxError,
packages/tools/src/disco/tools/sandbox/podman.py:87:    except FileNotFoundError as exc:
packages/tools/src/disco/tools/sandbox/podman.py:88:        raise SandboxError(f"the `podman` CLI is required on PATH for exec: {exc}") from exc
packages/tools/src/disco/tools/sandbox/podman.py:164:    async def read_file(self, path: str) -> bytes:
packages/tools/src/disco/tools/sandbox/podman.py:170:            raise_read_error(path, err)  # W1: missing file → typed FileNotFoundError
packages/tools/src/disco/tools/sandbox/podman.py:181:        except SandboxError:
packages/tools/src/disco/tools/sandbox/podman.py:194:            raise SandboxError(f"list_dir {path!r}: {err.decode('utf-8', 'replace').strip()}")
packages/tools/src/disco/tools/sandbox/podman.py:216:                raise SandboxError(f"write_file {path!r} failed")
packages/tools/src/disco/tools/sandbox/session.py:10:raises a clean `SandboxError` the executor turns into an error `ToolResult` the loop
packages/tools/src/disco/tools/sandbox/session.py:29:    SandboxError,
packages/tools/src/disco/tools/sandbox/session.py:169:            raise SandboxError("sandbox session is closed")
packages/tools/src/disco/tools/sandbox/session.py:288:        clean SandboxError (→ the executor's `sandbox_error` ToolResult) so the loop
packages/tools/src/disco/tools/sandbox/session.py:295:            raise SandboxError(
packages/tools/src/disco/tools/sandbox/session.py:326:                data = await self.read_file(mem_path)
packages/tools/src/disco/tools/sandbox/session.py:328:            except (FileNotFoundError, NotADirectoryError):
packages/tools/src/disco/tools/sandbox/session.py:387:    async def read_file(self, path: str) -> bytes:
packages/tools/src/disco/tools/sandbox/session.py:388:        return await self._resilient(lambda i: i.read_file(path))
packages/tools/src/disco/tools/sandbox/session.py:400:        raises a clean SandboxError the C18 advisory treats as unverifiable."""
packages/tools/src/disco/tools/sandbox/session.py:522:            raise SandboxError(
packages/tools/src/disco/tools/sandbox/_container.py:24:    SandboxError,
packages/tools/src/disco/tools/sandbox/_container.py:170:            raise SandboxError(f"sandbox instance {self.id} has been destroyed")
packages/tools/src/disco/tools/sandbox/_container.py:234:    def _classify_failure(self, exc: Exception) -> SandboxError:
packages/tools/src/disco/tools/sandbox/_container.py:238:        Otherwise it's a generic per-op SandboxError. Typing death distinctly is what
packages/tools/src/disco/tools/sandbox/_container.py:254:        return SandboxError(f"sandbox op failed in {self.id}: {exc}")
packages/tools/src/disco/tools/sandbox/_container.py:257:        """Run a blocking container op in a thread; let already-typed SandboxErrors
packages/tools/src/disco/tools/sandbox/_container.py:262:        except SandboxError:
packages/tools/src/disco/tools/sandbox/_container.py:280:            raise SandboxError(f"path escapes workspace: {path!r}")
packages/tools/src/disco/tools/sandbox/_container.py:315:    async def read_file(self, path: str) -> bytes:
packages/tools/src/disco/tools/sandbox/_container.py:324:                raise_read_error(path, err)  # W1: missing file → typed FileNotFoundError
packages/tools/src/disco/tools/sandbox/_container.py:335:        `_guarded` as a typed SandboxError, which C18 treats as unverifiable."""
packages/tools/src/disco/tools/sandbox/_container.py:339:        except SandboxError:
packages/tools/src/disco/tools/sandbox/_container.py:371:                raise SandboxError(f"write_file {path!r} failed")
packages/tools/src/disco/tools/sandbox/_container.py:384:                raise SandboxError(f"list_dir {path!r}: {err.decode('utf-8', 'replace').strip()}")
packages/core/src/disco/core/llm/secrets.py:232:        except (FileNotFoundError, ValueError, OSError):
packages/core/src/disco/core/llm/config_store.py:228:        except (FileNotFoundError, ValueError, OSError):

codex
One concrete gap is emerging: the contract only fixes missing files. Other read-time filesystem conditions still normalize differently across backends, so any loop path relying on `OSError` parity can still crash on container/podman. I’ll verify whether that becomes terminalized cleanly or remains a false-positive in supervision tests.
exec
/bin/bash -lc "nl -ba packages/agent-server/src/disco/agent_server/runtime.py | sed -n '1380,1495p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
  1380	    def research_stream(
  1381	        self,
  1382	        query: str,
  1383	        *,
  1384	        model_override: str | None = None,
  1385	        drop_weak: bool = False,
  1386	        domains_deny: frozenset[str] = frozenset(),
  1387	        think: bool = False,
  1388	        conversation_id: str | None = None,
  1389	    ) -> AsyncIterator[dict[str, Any]]:
  1390	        return self._dr.research_stream(
  1391	            query,
  1392	            model_override=model_override,
  1393	            drop_weak=drop_weak,
  1394	            domains_deny=domains_deny,
  1395	            think=think,
  1396	            conversation_id=conversation_id,
  1397	        )
  1398	
  1399	    def kick(self, conversation_id: str) -> None:
  1400	        """Schedule the loop to run (idempotent: a no-op if already running). The
  1401	        loop itself decides if there's unprocessed work and streams events.
  1402	
  1403	        Wraps `loop.run()` in a task that — for Build conversations with a valid
  1404	        ProjectStore configured — first REHYDRATES the persisted workspace on
  1405	        the very first kick, and SNAPSHOTS it after every run that ends in
  1406	        FINISHED. Failures are reported as ambient system-reminder events on
  1407	        the conversation log (the user + model both see them), never as
  1408	        exceptions that crash the task."""
  1409	        # Auto-title from the first user message (idempotent, detached, no-op once
  1410	        # titled). Placed here because kick() is the single chokepoint every user
  1411	        # message flows through — covers WS + REST send_message + first-kick alike.
  1412	        self._title_service.schedule(conversation_id)
  1413	        existing = self._tasks.get(conversation_id)
  1414	        if existing is not None and not existing.done():
  1415	            return  # already running; the new message is picked up at the next step
  1416	        loop = self._loop_for(conversation_id)
  1417	        task = asyncio.create_task(self._run_with_persistence(conversation_id, loop))
  1418	        # W2 supervision: the run task ALWAYS resolves to a terminal status. Without this
  1419	        # callback an exception escaping loop.run() killed the task silently and left the
  1420	        # conversation at RUNNING forever (the silent hang Dylan hit).
  1421	        task.add_done_callback(
  1422	            lambda t, _cid=conversation_id: self._on_run_task_done(_cid, t)
  1423	        )
  1424	        self._tasks[conversation_id] = task
  1425	
  1426	    # W2: statuses that mean "the run already concluded" — terminalization must not clobber.
  1427	    _CONCLUDED_STATUSES = frozenset(
  1428	        {
  1429	            ConversationStatus.FINISHED,
  1430	            ConversationStatus.ERROR,
  1431	            ConversationStatus.STUCK,
  1432	            ConversationStatus.PAUSED,
  1433	        }
  1434	    )
  1435	
  1436	    def _on_run_task_done(self, conversation_id: str, task: asyncio.Task[Any]) -> None:
  1437	        """W2 supervision callback. Deregister the task; on an UNHANDLED exception (NOT
  1438	        cancellation), schedule terminalization to ERROR so the conversation can never sit
  1439	        at RUNNING forever."""
  1440	        if self._tasks.get(conversation_id) is task:
  1441	            self._tasks.pop(conversation_id, None)
  1442	        if task.cancelled():
  1443	            return  # cancellation is not an error
  1444	        try:
  1445	            exc = task.exception()
  1446	        except asyncio.CancelledError:
  1447	            return
  1448	        if exc is None:
  1449	            return  # clean completion — the loop set its own terminal status
  1450	        # An exception escaped loop.run(). Schedule (best-effort) a terminal ERROR.
  1451	        with contextlib.suppress(RuntimeError):  # no running loop (shutdown) → skip
  1452	            asyncio.create_task(self._terminalize_crashed(conversation_id, exc))
  1453	
  1454	    async def _terminalize_crashed(
  1455	        self, conversation_id: str, exc: BaseException
  1456	    ) -> None:
  1457	        """Append a terminal ERROR status + a user-visible reminder for a crashed run.
  1458	        Idempotent: never overwrites an already-concluded status."""
  1459	        try:
  1460	            state = await self._store.get_state(conversation_id)
  1461	            if state.execution_status in self._CONCLUDED_STATUSES:
  1462	                return  # already concluded — don't clobber
  1463	            detail = f"uncaught {type(exc).__name__}: {exc}"[:200]
  1464	            logger.error("run task for %s crashed: %s", conversation_id, detail)
  1465	            await self._store.append(
  1466	                conversation_id,
  1467	                StatusEvent(status=ConversationStatus.ERROR, detail=detail),
  1468	            )
  1469	            await self._emit_persistence_reminder(
  1470	                conversation_id,
  1471	                f"The run stopped on an unexpected internal error ({type(exc).__name__}). "
  1472	                "It has been recorded as failed; you can retry or adjust the task.",
  1473	            )
  1474	        except Exception:  # noqa: BLE001 — supervision is best-effort, never re-raise
  1475	            logger.exception("crash terminalization failed for %s", conversation_id)
  1476	
  1477	    async def _run_with_persistence(
  1478	        self, conversation_id: str, loop: AgentLoop
  1479	    ) -> Any:
  1480	        """Snapshot/rehydrate wrapper around `loop.run()`. Surface-aware:
  1481	        - Build → snapshot+rehydrate the workspace as before.
  1482	        - Deep Research → short-circuit `loop.run()` on the post-plan-approval
  1483	          turn and run the DeepResearchRun engine; emit the ReportEvent +
  1484	          StatusEvent(FINISHED) directly.
  1485	        - Research → unchanged (the loop runs, no persistence)."""
  1486	        surface = self._surface_of(conversation_id)
  1487	
  1488	        if surface == "deep_research":
  1489	            # Short-circuit the loop for Deep Research. The plan-mode intercept
  1490	            # the loop would otherwise run isn't useful here — Deep Research
  1491	            # decomposes the query algorithmically (not via an LLM planning
  1492	            # round) and the engine handles the rest. The loop's plan-approval
  1493	            # state machine is reused via control-op routing; the loop's
  1494	            # `run()` itself is not the right driver.
  1495	            await self._maybe_run_deep_research(conversation_id)

exec
/bin/bash -lc "nl -ba packages/agent-server/tests/integration/test_task_supervision.py | sed -n '1,240p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""W2 — request/task SUPERVISION contract (the silent-hang AMPLIFIER fix).
     2	
     3	W1 fixed the *trigger* (a missing-file sandbox read now raises a FileNotFoundError the
     4	bookkeeping handlers catch). W2 is the *backstop*: even when SOME future unhandled
     5	exception escapes ``loop.run()``, the conversation must terminate in a durable ``ERROR``
     6	status — never sit at ``RUNNING`` forever (the silent hang Dylan hit). And cancellation
     7	must NOT be reported as an error. Terminalization must be idempotent.
     8	"""
     9	
    10	from __future__ import annotations
    11	
    12	import asyncio
    13	import contextlib
    14	import uuid
    15	
    16	import pytest
    17	from disco.agent_server import ConversationRuntime
    18	from disco.core import SqliteEventStore
    19	from disco.core.events import (
    20	    ConversationStatus,
    21	    EventSource,
    22	    LLMMessage,
    23	    MessageEvent,
    24	    StatusEvent,
    25	)
    26	from disco.core.llm import DefaultLLMRouter, ModelEntry, ProposedToolCall, RouterConfig
    27	from disco.tools import ProcessSandboxService
    28	
    29	from .scripted_model import ScriptedProvider
    30	
    31	pytestmark = pytest.mark.boundary_contract
    32	
    33	_ENTRY = ModelEntry(model_id="scripted-m", provider="scripted", context_window=8192)
    34	_CFG = RouterConfig(models={"scripted-m": _ENTRY}, default_model="scripted-m")
    35	# A single trivial step so ScriptedProvider constructs — the loop is monkeypatched away in
    36	# every test here (we inject crashing/cancelling/finishing loops), so it's never executed.
    37	_STEP = [("noop", [ProposedToolCall(tool_name="finish", arguments={})])]
    38	
    39	
    40	def _runtime(store: SqliteEventStore) -> ConversationRuntime:
    41	    return ConversationRuntime(
    42	        store,
    43	        router=DefaultLLMRouter(_CFG, {"scripted": ScriptedProvider(_STEP)}),
    44	        sandbox_service=ProcessSandboxService(),
    45	    )
    46	
    47	
    48	async def _kicked_conversation(store, runtime) -> str:
    49	    cid = f"sup-{uuid.uuid4().hex[:8]}"
    50	    store.create_conversation(cid, owner_id="local", surface="build")
    51	    runtime.set_surface(cid, "build")
    52	    runtime.set_artifact_mode(cid, True)
    53	    await store.append(
    54	        cid,
    55	        MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="go")),
    56	    )
    57	    return cid
    58	
    59	
    60	class _BoomLoop:
    61	    """A loop whose run() raises an exception that escapes the harness (simulates the
    62	    bookkeeping/sandbox exceptions that used to crash the task silently)."""
    63	
    64	    async def run(self):
    65	        raise RuntimeError("boom: an unhandled exception escaped the loop")
    66	
    67	
    68	class _CancelLoop:
    69	    async def run(self):
    70	        await asyncio.sleep(3600)  # block until cancelled
    71	
    72	
    73	async def test_uncaught_loop_exception_terminalizes_as_error(monkeypatch):
    74	    store = SqliteEventStore(":memory:")
    75	    runtime = _runtime(store)
    76	    cid = await _kicked_conversation(store, runtime)
    77	    monkeypatch.setattr(runtime, "_loop_for", lambda _c: _BoomLoop())
    78	
    79	    runtime.kick(cid)
    80	    task = runtime._tasks.get(cid)
    81	    assert task is not None
    82	    with contextlib.suppress(Exception):
    83	        await asyncio.wait_for(asyncio.shield(task), timeout=10)
    84	    await asyncio.sleep(0.2)  # let the supervisor's terminalize task run
    85	
    86	    state = await store.get_state(cid)
    87	    assert (
    88	        state.execution_status == ConversationStatus.ERROR
    89	    ), "crashed run must end ERROR, not hang RUNNING"
    90	    events = await store.get_events(cid)
    91	    assert any(
    92	        isinstance(e, StatusEvent) and e.status == ConversationStatus.ERROR for e in events
    93	    ), "a terminal ERROR StatusEvent must be visible to the UI"
    94	
    95	
    96	async def test_cancellation_is_not_reported_as_error(monkeypatch):
    97	    store = SqliteEventStore(":memory:")
    98	    runtime = _runtime(store)
    99	    cid = await _kicked_conversation(store, runtime)
   100	    monkeypatch.setattr(runtime, "_loop_for", lambda _c: _CancelLoop())
   101	
   102	    runtime.kick(cid)
   103	    task = runtime._tasks.get(cid)
   104	    assert task is not None
   105	    await asyncio.sleep(0.1)
   106	    task.cancel()
   107	    with contextlib.suppress(asyncio.CancelledError, Exception):
   108	        await task
   109	    await asyncio.sleep(0.2)
   110	
   111	    events = await store.get_events(cid)
   112	    assert not any(
   113	        isinstance(e, StatusEvent) and e.status == ConversationStatus.ERROR for e in events
   114	    ), "a cancelled run must NOT be reported as ERROR"
   115	
   116	
   117	async def test_terminalize_is_idempotent(monkeypatch):
   118	    """If the run already concluded (e.g. FINISHED) before the callback, don't overwrite."""
   119	    store = SqliteEventStore(":memory:")
   120	    runtime = _runtime(store)
   121	    cid = await _kicked_conversation(store, runtime)
   122	
   123	    class _FinishThenRaise:
   124	        async def run(self):
   125	            await store.append(cid, StatusEvent(status=ConversationStatus.FINISHED))
   126	            raise RuntimeError("late crash after finishing")
   127	
   128	    monkeypatch.setattr(runtime, "_loop_for", lambda _c: _FinishThenRaise())
   129	    runtime.kick(cid)
   130	    task = runtime._tasks.get(cid)
   131	    with contextlib.suppress(Exception):
   132	        await asyncio.wait_for(asyncio.shield(task), timeout=10)
   133	    await asyncio.sleep(0.2)
   134	
   135	    state = await store.get_state(cid)
   136	    assert (
   137	        state.execution_status == ConversationStatus.FINISHED
   138	    ), "must not clobber an already-terminal status"

exec
/bin/bash -lc "nl -ba packages/agent-server/tests/test_supervision_guardrail.py | sed -n '1,140p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""W10 — static guardrail locking in W2 (loop-task supervision).
     2	
     3	A future refactor of ``kick()`` that drops the done-callback would silently re-open the
     4	silent-hang: an exception escaping ``loop.run()`` would again leave the conversation at
     5	RUNNING forever. This asserts the supervision wiring stays in place. (W2's behavioural tests
     6	prove it WORKS; this proves it can't be quietly deleted.)
     7	"""
     8	
     9	from __future__ import annotations
    10	
    11	import inspect
    12	
    13	import pytest
    14	from disco.agent_server.runtime import ConversationRuntime
    15	
    16	pytestmark = pytest.mark.boundary_contract
    17	
    18	
    19	def test_kick_wires_the_supervision_callback():
    20	    src = inspect.getsource(ConversationRuntime.kick)
    21	    assert "add_done_callback" in src, "kick() must register a done-callback on the loop task"
    22	    assert "_on_run_task_done" in src, "kick() must route task completion through the supervisor"
    23	
    24	
    25	def test_supervisor_terminalizes_to_error():
    26	    """The supervisor method must emit a terminal ERROR (not just log) on an uncaught crash."""
    27	    src = inspect.getsource(ConversationRuntime._terminalize_crashed)
    28	    assert "ConversationStatus.ERROR" in src, "a crashed run must terminalize to ERROR"

exec
/bin/bash -lc "rg -n \"CONCLUDED|status|RuntimeStatus|AWAITING|IDLE|FINISHED|ERROR|STUCK|PAUSED|_terminalize_crashed|kick\\(\" packages/agent-server/src/disco/agent_server/runtime.py | head -n 220" in /var/home/dylan/projects/disco
 succeeded in 0ms:
120:# terminal status, so the task returns at its next step boundary; the cap only
259:    """True when an approved plan exists but FINISHED was never recorded — the
265:        isinstance(e, StatusEvent) and e.status == ConversationStatus.FINISHED
322:    /health) AND from kick()'s synchronous loop composition, all on the loop. When a
423:    AgentLoop per conversation. `kick(cid)` schedules the loop in the background.
469:        # from kick(); uses the cheap SUMMARIZER role; idempotent + non-blocking.
749:    # plan→iterate→report flow DOES halt at AWAITING_PLAN_APPROVAL — so a
800:        if store is not None and store.status() == StorageStatus.OK:
1399:    def kick(self, conversation_id: str) -> None:
1406:        FINISHED. Failures are reported as ambient system-reminder events on
1410:        # titled). Placed here because kick() is the single chokepoint every user
1418:        # W2 supervision: the run task ALWAYS resolves to a terminal status. Without this
1426:    # W2: statuses that mean "the run already concluded" — terminalization must not clobber.
1427:    _CONCLUDED_STATUSES = frozenset(
1429:            ConversationStatus.FINISHED,
1430:            ConversationStatus.ERROR,
1431:            ConversationStatus.STUCK,
1432:            ConversationStatus.PAUSED,
1438:        cancellation), schedule terminalization to ERROR so the conversation can never sit
1449:            return  # clean completion — the loop set its own terminal status
1450:        # An exception escaped loop.run(). Schedule (best-effort) a terminal ERROR.
1452:            asyncio.create_task(self._terminalize_crashed(conversation_id, exc))
1454:    async def _terminalize_crashed(
1457:        """Append a terminal ERROR status + a user-visible reminder for a crashed run.
1458:        Idempotent: never overwrites an already-concluded status."""
1461:            if state.execution_status in self._CONCLUDED_STATUSES:
1467:                StatusEvent(status=ConversationStatus.ERROR, detail=detail),
1484:          StatusEvent(FINISHED) directly.
1516:        # on FINISHED. A run that ends STUCK/ERROR/PAUSED still wrote real files (the
1521:            ConversationStatus.FINISHED,
1522:            ConversationStatus.STUCK,
1523:            ConversationStatus.ERROR,
1524:            ConversationStatus.PAUSED,
1525:            ConversationStatus.IDLE,
1527:        if surface in self._BUILD_LIKE_SURFACES and state.execution_status in _ENDED:
1529:            # FINISHED now rides the idle sweep like STUCK/ERROR/PAUSED;
1596:        ``.status()`` for the full validation classification (ok / not_found /
1663:        :class:`ProjectStore`; the validity status is always queryable via
1664:        ``store.status()`` at the use site."""
1767:        """Resume from AWAITING_USER_DECISION by selecting the agent's proposed
1782:        status, but its loop task may still be finishing its in-flight model
1783:        step. `kick()` is idempotent over a non-done task, so re-kicking before
1796:        """Continue a stopped (PAUSED) Deep Research run. Clears the cancel flag and
1797:        re-kicks; `_maybe_run_deep_research` detects the PAUSED state and re-runs the
1800:        self.kick(conversation_id)
1870:        the ground truth of "what's executing right now" (cached status can lag a

exec
/bin/bash -lc "nl -ba packages/agent-server/src/disco/agent_server/runtime.py | sed -n '1495,1545p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
  1495	            await self._maybe_run_deep_research(conversation_id)
  1496	            # Return the (possibly-updated) state. The store reflects whatever
  1497	            # we emitted.
  1498	            return await self._store.get_state(conversation_id)
  1499	
  1500	        # Rehydrate hook: BEFORE the loop runs for the first time, if a project
  1501	        # storage path is configured AND a prior snapshot exists for this cid,
  1502	        # write its files into the (lazy) sandbox so the agent sees them. Reads
  1503	        # are cheap; the rehydrate only writes if there are files on disk.
  1504	        if surface in self._BUILD_LIKE_SURFACES:
  1505	            await self._maybe_rehydrate(conversation_id)
  1506	            # DC-07: re-materialize server-side uploads into the fresh sandbox.
  1507	            # This is the SINGLE chokepoint that covers ALL paths:
  1508	            #   - post-restart resume → fresh sandbox via lazy compose
  1509	            #   - first build after upload (pending session adoption or fresh)
  1510	            #   - mid-run recreation (also handled by _rehydrate_after_recreate)
  1511	            await self._rematerialize_uploads(conversation_id)
  1512	
  1513	        state = await loop.run()
  1514	
  1515	        # Snapshot hook: capture the workspace whenever a build run ENDS — not only
  1516	        # on FINISHED. A run that ends STUCK/ERROR/PAUSED still wrote real files (the
  1517	        # model may have built most of the deliverable before getting stuck); snapshot
  1518	        # so that work is NOT lost (it was — a stuck iteration silently discarded a
  1519	        # whole feature it had written). Snapshot is idempotent + cheap.
  1520	        _ENDED = {
  1521	            ConversationStatus.FINISHED,
  1522	            ConversationStatus.STUCK,
  1523	            ConversationStatus.ERROR,
  1524	            ConversationStatus.PAUSED,
  1525	            ConversationStatus.IDLE,
  1526	        }
  1527	        if surface in self._BUILD_LIKE_SURFACES and state.execution_status in _ENDED:
  1528	            await self._maybe_snapshot(conversation_id)
  1529	            # FINISHED now rides the idle sweep like STUCK/ERROR/PAUSED;
  1530	            # suspend = sweep_idle_once -> _suspend
  1531	        return state
  1532	
  1533	    async def _teardown_sandbox(self, conversation_id: str) -> None:
  1534	        return await self._lifecycle._teardown_sandbox(conversation_id)
  1535	
  1536	    async def reconcile_orphaned_runs(self, *, owner_id: str = DEFAULT_OWNER_ID) -> int:
  1537	        return await self._lifecycle.reconcile_orphaned_runs(owner_id=owner_id)
  1538	
  1539	    # ---- MCP pool lifecycle (RP-05 rung A) — delegators to McpManager --------
  1540	
  1541	    async def _start_mcp_pool(self) -> None:
  1542	        return await self._mcp._start_mcp_pool()
  1543	
  1544	    async def _close_mcp_pool(self) -> None:
  1545	        return await self._mcp._close_mcp_pool()

exec
/bin/bash -lc 'rg -n "AWAITING|ConversationStatus" packages/core/src/disco/core/events.py packages/core/src/disco/core/state.py packages/core/src/disco/core/loop/*.py | head -n 200' in /var/home/dylan/projects/disco
 succeeded in 0ms:
packages/core/src/disco/core/events.py:187:class ConversationStatus(str, Enum):
packages/core/src/disco/core/events.py:198:    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
packages/core/src/disco/core/events.py:205:    AWAITING_USER_DECISION = "AWAITING_USER_DECISION"
packages/core/src/disco/core/events.py:208:    # AWAITING_USER_DECISION (pick a card), this is an open question the user
packages/core/src/disco/core/events.py:210:    # signal — same re-kick semantics as AWAITING_USER_DECISION, just a different
packages/core/src/disco/core/events.py:212:    AWAITING_USER_QUESTION = "AWAITING_USER_QUESTION"
packages/core/src/disco/core/events.py:397:    status: ConversationStatus
packages/core/src/disco/core/events.py:489:    those become this event, the loop halts at AWAITING_USER_DECISION, and the
packages/core/src/disco/core/events.py:650:    AWAITING_USER_QUESTION. The user answers each question; the answers are
packages/core/src/disco/core/loop/finish.py:23:    ConversationStatus,
packages/core/src/disco/core/loop/finish.py:633:                        status=ConversationStatus.RUNNING,
packages/core/src/disco/core/loop/finish.py:748:                        status=ConversationStatus.FINISHED,
packages/core/src/disco/core/loop/finish.py:815:                                status=ConversationStatus.RUNNING,
packages/core/src/disco/core/loop/finish.py:886:            await self._loop._emit(StatusEvent(status=ConversationStatus.FINISHED))
packages/core/src/disco/core/loop/turn_control.py:29:    ConversationStatus,
packages/core/src/disco/core/loop/turn_control.py:155:                        status=ConversationStatus.FINISHED,
packages/core/src/disco/core/loop/turn_control.py:176:                    StatusEvent(status=ConversationStatus.PAUSED, detail="actionless")
packages/core/src/disco/core/loop/turn_control.py:201:                    StatusEvent(status=ConversationStatus.PAUSED, detail="noop_limit")
packages/core/src/disco/core/loop/turn_control.py:221:                    StatusEvent(status=ConversationStatus.FINISHED, detail="noop_limit")
packages/core/src/disco/core/loop/turn_control.py:363:                    StatusEvent(status=ConversationStatus.RUNNING, detail="stuck_escape")
packages/core/src/disco/core/loop/turn_control.py:368:                await self._loop._emit(StatusEvent(status=ConversationStatus.STUCK))
packages/core/src/disco/core/loop/turn_control.py:381:        # it halts at AWAITING_USER_DECISION with a summary of what failed.
packages/core/src/disco/core/loop/turn_control.py:428:                    status=ConversationStatus.RUNNING, detail="recovery_requested"
packages/core/src/disco/core/loop/turn_control.py:435:                # indefinite AWAITING_USER_DECISION stall. Bounded: we only
packages/core/src/disco/core/loop/turn_control.py:453:                await self._loop._emit(StatusEvent(status=ConversationStatus.STUCK))
packages/core/src/disco/core/loop/turn_control.py:489:                    status=ConversationStatus.AWAITING_USER_DECISION,
packages/core/src/disco/core/loop/turn_control.py:515:                StatusEvent(status=ConversationStatus.RUNNING, detail="no_progress")
packages/core/src/disco/core/loop/turn_control.py:526:                StatusEvent(status=ConversationStatus.STUCK, detail="no_progress")
packages/core/src/disco/core/loop/turn_control.py:588:                        status=ConversationStatus.STUCK, detail="bookkeeping_only"
packages/core/src/disco/core/loop/turn_control.py:973:            # halt at AWAITING_PLAN_APPROVAL forever — a headless stall.
packages/core/src/disco/core/loop/turn_control.py:1023:                        status=ConversationStatus.STUCK,
packages/core/src/disco/core/loop/turn_control.py:1031:                    status=ConversationStatus.RUNNING, detail="plan_approved"
packages/core/src/disco/core/loop/turn_control.py:1037:                status=ConversationStatus.AWAITING_PLAN_APPROVAL,
packages/core/src/disco/core/loop/turn_control.py:1049:        # and halts at AWAITING_USER_QUESTION. The user answers
packages/core/src/disco/core/loop/turn_control.py:1094:                    status=ConversationStatus.AWAITING_USER_QUESTION,
packages/core/src/disco/core/loop/turn_control.py:1106:                status=ConversationStatus.AWAITING_USER_QUESTION,
packages/core/src/disco/core/loop/turn_control.py:1120:                    status=ConversationStatus.AWAITING_USER_DECISION,
packages/core/src/disco/core/loop/turn_control.py:1128:        # two-way Ask-gate (AWAITING_USER_QUESTION) — the UI renders
packages/core/src/disco/core/loop/turn_control.py:1146:                status=ConversationStatus.AWAITING_USER_QUESTION,
packages/core/src/disco/core/loop/tool_specs.py:28:# sandbox). WITH options it becomes an AlternativesEvent + AWAITING_USER_DECISION
packages/core/src/disco/core/loop/tool_specs.py:30:# AWAITING_USER_QUESTION (the user types an answer). Either way the reply resumes
packages/core/src/disco/core/loop/tool_specs.py:38:# questions, and halts at AWAITING_USER_QUESTION. The user answers each
packages/core/src/disco/core/loop/driver.py:19:    ConversationStatus,
packages/core/src/disco/core/loop/driver.py:153:                status=ConversationStatus.PAUSED,
packages/core/src/disco/core/loop/signals.py:16:    ConversationStatus,
packages/core/src/disco/core/loop/signals.py:271:            and e.status == ConversationStatus.RUNNING
packages/core/src/disco/core/loop/signals.py:467:        ConversationStatus.RUNNING,
packages/core/src/disco/core/loop/signals.py:468:        ConversationStatus.FINISHED,
packages/core/src/disco/core/loop/signals.py:469:        ConversationStatus.STUCK,
packages/core/src/disco/core/loop/signals.py:470:        ConversationStatus.ERROR,
packages/core/src/disco/core/state.py:20:    ConversationStatus,
packages/core/src/disco/core/state.py:38:    execution_status: ConversationStatus = ConversationStatus.IDLE
packages/core/src/disco/core/state.py:45:    # The id of a plan awaiting approval, if status is AWAITING_PLAN_APPROVAL.
packages/core/src/disco/core/state.py:49:    # AWAITING_USER_DECISION. The third gate (with pending_action_id and
packages/core/src/disco/core/state.py:53:    # answer, if status is AWAITING_USER_QUESTION. The two-way Ask-gate's pending
packages/core/src/disco/core/state.py:57:    # AWAITING_USER_QUESTION and the gate is a clarify card (not a free-form
packages/core/src/disco/core/state.py:78:        # loop transitions to AWAITING_PLAN_APPROVAL (the plan-mode gate).
packages/core/src/disco/core/state.py:80:        # Likewise for the alternatives gate (AWAITING_USER_DECISION).
packages/core/src/disco/core/state.py:83:        # transitions to AWAITING_USER_QUESTION (the free-form Ask-gate).
packages/core/src/disco/core/state.py:86:        # transitions to AWAITING_USER_QUESTION via the clarify gate.
packages/core/src/disco/core/state.py:95:                if e.status == ConversationStatus.WAITING_FOR_CONFIRMATION:
packages/core/src/disco/core/state.py:100:                if e.status == ConversationStatus.AWAITING_PLAN_APPROVAL:
packages/core/src/disco/core/state.py:104:                if e.status == ConversationStatus.AWAITING_USER_DECISION:
packages/core/src/disco/core/state.py:108:                if e.status == ConversationStatus.AWAITING_USER_QUESTION:
packages/core/src/disco/core/state.py:140:                st.execution_status = ConversationStatus.ERROR
packages/core/src/disco/core/loop/engine.py:33:    ConversationStatus,
packages/core/src/disco/core/loop/engine.py:164:        ConversationStatus.PAUSED,
packages/core/src/disco/core/loop/engine.py:165:        ConversationStatus.IDLE,
packages/core/src/disco/core/loop/engine.py:166:        ConversationStatus.FINISHED,
packages/core/src/disco/core/loop/engine.py:167:        ConversationStatus.STUCK,
packages/core/src/disco/core/loop/engine.py:168:        ConversationStatus.ERROR,
packages/core/src/disco/core/loop/engine.py:169:        ConversationStatus.WAITING_FOR_CONFIRMATION,
packages/core/src/disco/core/loop/engine.py:170:        ConversationStatus.AWAITING_PLAN_APPROVAL,
packages/core/src/disco/core/loop/engine.py:174:        ConversationStatus.AWAITING_USER_DECISION,
packages/core/src/disco/core/loop/engine.py:175:        ConversationStatus.AWAITING_USER_QUESTION,
packages/core/src/disco/core/loop/engine.py:537:        # instead of an indefinite AWAITING_USER_DECISION stall.
packages/core/src/disco/core/loop/engine.py:591:        # harness hands off to the user (AWAITING_USER_DECISION) instead of
packages/core/src/disco/core/loop/engine.py:894:                            status=ConversationStatus.RUNNING, detail="plan_approved"
packages/core/src/disco/core/loop/engine.py:900:                        status=ConversationStatus.AWAITING_PLAN_APPROVAL,
packages/core/src/disco/core/loop/engine.py:1015:                    status=ConversationStatus.WAITING_FOR_CONFIRMATION,
packages/core/src/disco/core/loop/engine.py:1071:            ConversationStatus.IDLE
packages/core/src/disco/core/loop/engine.py:1076:                ConversationStatus.PAUSED,
packages/core/src/disco/core/loop/engine.py:1077:                ConversationStatus.WAITING_FOR_CONFIRMATION,
packages/core/src/disco/core/loop/engine.py:1078:                ConversationStatus.AWAITING_PLAN_APPROVAL,
packages/core/src/disco/core/loop/engine.py:1081:            # AWAITING_USER_DECISION / AWAITING_USER_QUESTION: the agent
packages/core/src/disco/core/loop/engine.py:1086:            # FINISHED/STUCK/ERROR/AWAITING_USER_*/no new work → idle.
packages/core/src/disco/core/loop/engine.py:1111:        await self._emit(StatusEvent(status=ConversationStatus.RUNNING))
packages/core/src/disco/core/loop/engine.py:1129:                    await self._emit(StatusEvent(status=ConversationStatus.PAUSED))
packages/core/src/disco/core/loop/engine.py:1134:                    ConversationStatus.PAUSED,
packages/core/src/disco/core/loop/engine.py:1135:                    ConversationStatus.IDLE,
packages/core/src/disco/core/loop/engine.py:1136:                    ConversationStatus.FINISHED,
packages/core/src/disco/core/loop/engine.py:1137:                    ConversationStatus.STUCK,
packages/core/src/disco/core/loop/engine.py:1138:                    ConversationStatus.ERROR,
packages/core/src/disco/core/loop/engine.py:1139:                    ConversationStatus.WAITING_FOR_CONFIRMATION,
packages/core/src/disco/core/loop/engine.py:1140:                    ConversationStatus.AWAITING_PLAN_APPROVAL,
packages/core/src/disco/core/loop/engine.py:1287:                # AWAITING_USER_QUESTION — a silent headless stall, since there is no
packages/core/src/disco/core/loop/engine.py:1360:                ConversationStatus.FINISHED,
packages/core/src/disco/core/loop/engine.py:1361:                ConversationStatus.STUCK,
packages/core/src/disco/core/loop/engine.py:1363:                await self._emit(StatusEvent(status=ConversationStatus.IDLE))
packages/core/src/disco/core/loop/engine.py:1375:                state.execution_status != ConversationStatus.WAITING_FOR_CONFIRMATION
packages/core/src/disco/core/loop/engine.py:1380:            await self._emit(StatusEvent(status=ConversationStatus.RUNNING))
packages/core/src/disco/core/loop/engine.py:1395:            if state.execution_status != ConversationStatus.WAITING_FOR_CONFIRMATION:
packages/core/src/disco/core/loop/engine.py:1419:            await self._emit(StatusEvent(status=ConversationStatus.RUNNING))
packages/core/src/disco/core/loop/engine.py:1428:            if state.execution_status != ConversationStatus.AWAITING_PLAN_APPROVAL:
packages/core/src/disco/core/loop/engine.py:1432:                StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved")
packages/core/src/disco/core/loop/engine.py:1437:        """Resume from AWAITING_USER_DECISION by selecting one of the agent's
packages/core/src/disco/core/loop/engine.py:1448:            if state.execution_status != ConversationStatus.AWAITING_USER_DECISION:
packages/core/src/disco/core/loop/engine.py:1461:                    StatusEvent(status=ConversationStatus.RUNNING, detail="alternatives_missing")
packages/core/src/disco/core/loop/engine.py:1484:                    StatusEvent(status=ConversationStatus.RUNNING, detail="continue_anyway")
packages/core/src/disco/core/loop/engine.py:1534:                    status=ConversationStatus.RUNNING,
packages/core/src/disco/core/loop/engine.py:1570:            await self._emit(StatusEvent(status=ConversationStatus.RUNNING, detail="planning"))
packages/core/src/disco/core/loop/engine.py:1589:            await self._emit(StatusEvent(status=ConversationStatus.RUNNING))
packages/core/src/disco/core/loop/engine.py:1596:            await self._emit(StatusEvent(status=ConversationStatus.IDLE, detail="cancelled"))

exec
/bin/bash -lc "nl -ba packages/core/src/disco/core/events.py | sed -n '1,90p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""The Event type hierarchy — event-state-contract.md §2.
     2	
     3	Events are the atomic unit of all conversation and agent state. The log of
     4	events is append-only and the single source of truth (BoD Principle 3); State
     5	(state.py) and the LLM View (view.py) are *pure functions* of the ordered log.
     6	
     7	Field names, types, and method signatures here are **normative** (the contract
     8	marks them [CONTRACT]); other subsystems deserialize and compare these shapes
     9	without further coordination. Method *bodies* are implementation.
    10	"""
    11	
    12	from __future__ import annotations
    13	
    14	import re
    15	import uuid
    16	from datetime import UTC, datetime
    17	from enum import Enum
    18	from typing import Annotated, Any, Literal
    19	
    20	from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
    21	
    22	# Bump on a *breaking* change to any event shape. Adding an optional field with
    23	# a default is backward-compatible and does NOT require a bump (§4 rule 2).
    24	SCHEMA_VERSION = 1
    25	
    26	
    27	class EventSource(str, Enum):
    28	    """Who/what produced an event. Security and UI both depend on this (§1.6)."""
    29	
    30	    USER = "user"
    31	    AGENT = "agent"
    32	    ENVIRONMENT = "environment"  # tool results, injected feedback, hooks
    33	    SYSTEM = "system"  # lifecycle/status, condensation, errors
    34	
    35	
    36	class EventKind(str, Enum):
    37	    """Discriminator for the Event union. Serialized by value (§4 rule 4)."""
    38	
    39	    MESSAGE = "message"
    40	    ACTION = "action"
    41	    OBSERVATION = "observation"
    42	    AGENT_ERROR = "agent_error"
    43	    CONDENSATION = "condensation"
    44	    STATUS = "status"
    45	    ERROR = "error"  # conversation-level error (distinct from agent_error)
    46	    PLAN = "plan"  # a proposed, structured plan awaiting approval (Build plan-mode)
    47	    REPORT = "report"  # a finished Deep Research multi-section grounded report
    48	    ALTERNATIVES = "alternatives"  # 2–3 user-choosable options after repeated tool failure
    49	    KNOWLEDGE = "knowledge"  # a scoped best-practice snippet (Cluster 7)
    50	    DATASOURCE = "datasource"  # durable API/schema docs, condensation-immune (Cluster 7)
    51	    DELIVERABLE = "deliverable"  # the agent's finished-artifact handoff signal
    52	    SCHEDULE = "schedule"  # a schedule was created or deleted (RP-08)
    53	    SCHEDULE_RUN = "schedule_run"  # a scheduled run fired (RP-08)
    54	    CLARIFY = "clarify"  # pre-plan typed clarification questions (RP-13)
    55	
    56	
    57	def _new_id() -> str:
    58	    return f"evt_{uuid.uuid4().hex}"
    59	
    60	
    61	def _now() -> datetime:
    62	    return datetime.now(UTC)
    63	
    64	
    65	class BaseEvent(BaseModel):
    66	    """Common envelope for every event. Subtypes add a typed payload.
    67	
    68	    [CONTRACT] These fields exist on every event and never change meaning.
    69	
    70	    `frozen=True` makes events immutable at the type level (invariant #1):
    71	    accidental mutation becomes a runtime error, not a silent bug. The only
    72	    field "filled in later" is `seq`, set by the store via `model_copy` (a new
    73	    instance), never by in-place mutation.
    74	    """
    75	
    76	    model_config = ConfigDict(frozen=True, extra="forbid")
    77	
    78	    id: str = Field(default_factory=_new_id)
    79	    # The `kind` discriminator is declared on each concrete subclass as a
    80	    # `Literal[EventKind.X]` (the idiomatic Pydantic discriminated-union shape;
    81	    # see `Event` below with `Field(discriminator="kind")`). Every concrete event
    82	    # therefore carries `kind` — BaseEvent is the never-instantiated envelope.
    83	    source: EventSource
    84	    timestamp: datetime = Field(default_factory=_now)  # VOLATILE (§6.3)
    85	    schema_version: int = Field(default=SCHEMA_VERSION)
    86	
    87	    # Assigned by the EventStore at append time. None before persistence.
    88	    # [CONTRACT] Monotonic, gap-free, per-conversation. Do NOT set manually.
    89	    seq: int | None = Field(default=None)
    90	

exec
/bin/bash -lc "rg -n \"StatusEvent\\(|ConversationStatus\\.\" packages/core/src/disco/core/loop packages/agent-server/src/disco/agent_server/runtime.py | head -n 220" in /var/home/dylan/projects/disco
 succeeded in 0ms:
packages/agent-server/src/disco/agent_server/runtime.py:265:        isinstance(e, StatusEvent) and e.status == ConversationStatus.FINISHED
packages/agent-server/src/disco/agent_server/runtime.py:1429:            ConversationStatus.FINISHED,
packages/agent-server/src/disco/agent_server/runtime.py:1430:            ConversationStatus.ERROR,
packages/agent-server/src/disco/agent_server/runtime.py:1431:            ConversationStatus.STUCK,
packages/agent-server/src/disco/agent_server/runtime.py:1432:            ConversationStatus.PAUSED,
packages/agent-server/src/disco/agent_server/runtime.py:1467:                StatusEvent(status=ConversationStatus.ERROR, detail=detail),
packages/agent-server/src/disco/agent_server/runtime.py:1484:          StatusEvent(FINISHED) directly.
packages/agent-server/src/disco/agent_server/runtime.py:1521:            ConversationStatus.FINISHED,
packages/agent-server/src/disco/agent_server/runtime.py:1522:            ConversationStatus.STUCK,
packages/agent-server/src/disco/agent_server/runtime.py:1523:            ConversationStatus.ERROR,
packages/agent-server/src/disco/agent_server/runtime.py:1524:            ConversationStatus.PAUSED,
packages/agent-server/src/disco/agent_server/runtime.py:1525:            ConversationStatus.IDLE,
packages/core/src/disco/core/loop/engine.py:164:        ConversationStatus.PAUSED,
packages/core/src/disco/core/loop/engine.py:165:        ConversationStatus.IDLE,
packages/core/src/disco/core/loop/engine.py:166:        ConversationStatus.FINISHED,
packages/core/src/disco/core/loop/engine.py:167:        ConversationStatus.STUCK,
packages/core/src/disco/core/loop/engine.py:168:        ConversationStatus.ERROR,
packages/core/src/disco/core/loop/engine.py:169:        ConversationStatus.WAITING_FOR_CONFIRMATION,
packages/core/src/disco/core/loop/engine.py:170:        ConversationStatus.AWAITING_PLAN_APPROVAL,
packages/core/src/disco/core/loop/engine.py:174:        ConversationStatus.AWAITING_USER_DECISION,
packages/core/src/disco/core/loop/engine.py:175:        ConversationStatus.AWAITING_USER_QUESTION,
packages/core/src/disco/core/loop/engine.py:893:                        StatusEvent(
packages/core/src/disco/core/loop/engine.py:894:                            status=ConversationStatus.RUNNING, detail="plan_approved"
packages/core/src/disco/core/loop/engine.py:899:                    StatusEvent(
packages/core/src/disco/core/loop/engine.py:900:                        status=ConversationStatus.AWAITING_PLAN_APPROVAL,
packages/core/src/disco/core/loop/engine.py:1014:                StatusEvent(
packages/core/src/disco/core/loop/engine.py:1015:                    status=ConversationStatus.WAITING_FOR_CONFIRMATION,
packages/core/src/disco/core/loop/engine.py:1071:            ConversationStatus.IDLE
packages/core/src/disco/core/loop/engine.py:1076:                ConversationStatus.PAUSED,
packages/core/src/disco/core/loop/engine.py:1077:                ConversationStatus.WAITING_FOR_CONFIRMATION,
packages/core/src/disco/core/loop/engine.py:1078:                ConversationStatus.AWAITING_PLAN_APPROVAL,
packages/core/src/disco/core/loop/engine.py:1111:        await self._emit(StatusEvent(status=ConversationStatus.RUNNING))
packages/core/src/disco/core/loop/engine.py:1129:                    await self._emit(StatusEvent(status=ConversationStatus.PAUSED))
packages/core/src/disco/core/loop/engine.py:1134:                    ConversationStatus.PAUSED,
packages/core/src/disco/core/loop/engine.py:1135:                    ConversationStatus.IDLE,
packages/core/src/disco/core/loop/engine.py:1136:                    ConversationStatus.FINISHED,
packages/core/src/disco/core/loop/engine.py:1137:                    ConversationStatus.STUCK,
packages/core/src/disco/core/loop/engine.py:1138:                    ConversationStatus.ERROR,
packages/core/src/disco/core/loop/engine.py:1139:                    ConversationStatus.WAITING_FOR_CONFIRMATION,
packages/core/src/disco/core/loop/engine.py:1140:                    ConversationStatus.AWAITING_PLAN_APPROVAL,
packages/core/src/disco/core/loop/engine.py:1360:                ConversationStatus.FINISHED,
packages/core/src/disco/core/loop/engine.py:1361:                ConversationStatus.STUCK,
packages/core/src/disco/core/loop/engine.py:1363:                await self._emit(StatusEvent(status=ConversationStatus.IDLE))
packages/core/src/disco/core/loop/engine.py:1375:                state.execution_status != ConversationStatus.WAITING_FOR_CONFIRMATION
packages/core/src/disco/core/loop/engine.py:1380:            await self._emit(StatusEvent(status=ConversationStatus.RUNNING))
packages/core/src/disco/core/loop/engine.py:1395:            if state.execution_status != ConversationStatus.WAITING_FOR_CONFIRMATION:
packages/core/src/disco/core/loop/engine.py:1419:            await self._emit(StatusEvent(status=ConversationStatus.RUNNING))
packages/core/src/disco/core/loop/engine.py:1428:            if state.execution_status != ConversationStatus.AWAITING_PLAN_APPROVAL:
packages/core/src/disco/core/loop/engine.py:1432:                StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved")
packages/core/src/disco/core/loop/engine.py:1448:            if state.execution_status != ConversationStatus.AWAITING_USER_DECISION:
packages/core/src/disco/core/loop/engine.py:1461:                    StatusEvent(status=ConversationStatus.RUNNING, detail="alternatives_missing")
packages/core/src/disco/core/loop/engine.py:1484:                    StatusEvent(status=ConversationStatus.RUNNING, detail="continue_anyway")
packages/core/src/disco/core/loop/engine.py:1533:                StatusEvent(
packages/core/src/disco/core/loop/engine.py:1534:                    status=ConversationStatus.RUNNING,
packages/core/src/disco/core/loop/engine.py:1570:            await self._emit(StatusEvent(status=ConversationStatus.RUNNING, detail="planning"))
packages/core/src/disco/core/loop/engine.py:1589:            await self._emit(StatusEvent(status=ConversationStatus.RUNNING))
packages/core/src/disco/core/loop/engine.py:1596:            await self._emit(StatusEvent(status=ConversationStatus.IDLE, detail="cancelled"))
packages/core/src/disco/core/loop/signals.py:266:    StatusEvent(RUNNING, detail="resumed") or since start."""
packages/core/src/disco/core/loop/signals.py:271:            and e.status == ConversationStatus.RUNNING
packages/core/src/disco/core/loop/signals.py:467:        ConversationStatus.RUNNING,
packages/core/src/disco/core/loop/signals.py:468:        ConversationStatus.FINISHED,
packages/core/src/disco/core/loop/signals.py:469:        ConversationStatus.STUCK,
packages/core/src/disco/core/loop/signals.py:470:        ConversationStatus.ERROR,
packages/core/src/disco/core/loop/turn_control.py:154:                    StatusEvent(
packages/core/src/disco/core/loop/turn_control.py:155:                        status=ConversationStatus.FINISHED,
packages/core/src/disco/core/loop/turn_control.py:176:                    StatusEvent(status=ConversationStatus.PAUSED, detail="actionless")
packages/core/src/disco/core/loop/turn_control.py:201:                    StatusEvent(status=ConversationStatus.PAUSED, detail="noop_limit")
packages/core/src/disco/core/loop/turn_control.py:221:                    StatusEvent(status=ConversationStatus.FINISHED, detail="noop_limit")
packages/core/src/disco/core/loop/turn_control.py:363:                    StatusEvent(status=ConversationStatus.RUNNING, detail="stuck_escape")
packages/core/src/disco/core/loop/turn_control.py:368:                await self._loop._emit(StatusEvent(status=ConversationStatus.STUCK))
packages/core/src/disco/core/loop/turn_control.py:427:                StatusEvent(
packages/core/src/disco/core/loop/turn_control.py:428:                    status=ConversationStatus.RUNNING, detail="recovery_requested"
packages/core/src/disco/core/loop/turn_control.py:453:                await self._loop._emit(StatusEvent(status=ConversationStatus.STUCK))
packages/core/src/disco/core/loop/turn_control.py:488:                StatusEvent(
packages/core/src/disco/core/loop/turn_control.py:489:                    status=ConversationStatus.AWAITING_USER_DECISION,
packages/core/src/disco/core/loop/turn_control.py:515:                StatusEvent(status=ConversationStatus.RUNNING, detail="no_progress")
packages/core/src/disco/core/loop/turn_control.py:526:                StatusEvent(status=ConversationStatus.STUCK, detail="no_progress")
packages/core/src/disco/core/loop/turn_control.py:587:                    StatusEvent(
packages/core/src/disco/core/loop/turn_control.py:588:                        status=ConversationStatus.STUCK, detail="bookkeeping_only"
packages/core/src/disco/core/loop/turn_control.py:1022:                    StatusEvent(
packages/core/src/disco/core/loop/turn_control.py:1023:                        status=ConversationStatus.STUCK,
packages/core/src/disco/core/loop/turn_control.py:1030:                StatusEvent(
packages/core/src/disco/core/loop/turn_control.py:1031:                    status=ConversationStatus.RUNNING, detail="plan_approved"
packages/core/src/disco/core/loop/turn_control.py:1036:            StatusEvent(
packages/core/src/disco/core/loop/turn_control.py:1037:                status=ConversationStatus.AWAITING_PLAN_APPROVAL,
packages/core/src/disco/core/loop/turn_control.py:1093:                StatusEvent(
packages/core/src/disco/core/loop/turn_control.py:1094:                    status=ConversationStatus.AWAITING_USER_QUESTION,
packages/core/src/disco/core/loop/turn_control.py:1105:            StatusEvent(
packages/core/src/disco/core/loop/turn_control.py:1106:                status=ConversationStatus.AWAITING_USER_QUESTION,
packages/core/src/disco/core/loop/turn_control.py:1119:                StatusEvent(
packages/core/src/disco/core/loop/turn_control.py:1120:                    status=ConversationStatus.AWAITING_USER_DECISION,
packages/core/src/disco/core/loop/turn_control.py:1145:            StatusEvent(
packages/core/src/disco/core/loop/turn_control.py:1146:                status=ConversationStatus.AWAITING_USER_QUESTION,
packages/core/src/disco/core/loop/finish.py:632:                    StatusEvent(
packages/core/src/disco/core/loop/finish.py:633:                        status=ConversationStatus.RUNNING,
packages/core/src/disco/core/loop/finish.py:747:                    StatusEvent(
packages/core/src/disco/core/loop/finish.py:748:                        status=ConversationStatus.FINISHED,
packages/core/src/disco/core/loop/finish.py:814:                            StatusEvent(
packages/core/src/disco/core/loop/finish.py:815:                                status=ConversationStatus.RUNNING,
packages/core/src/disco/core/loop/finish.py:886:            await self._loop._emit(StatusEvent(status=ConversationStatus.FINISHED))
packages/core/src/disco/core/loop/driver.py:152:            StatusEvent(
packages/core/src/disco/core/loop/driver.py:153:                status=ConversationStatus.PAUSED,

codex
The supervisor sets an ERROR by spawning another task from a done callback. That handles the tested case, but it is still best-effort and can be skipped during loop shutdown; I’m checking whether the runner/CI treats that as acceptable or accidentally hides it.
exec
/bin/bash -lc "nl -ba packages/core/src/disco/core/loop/engine.py | sed -n '1050,1165p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
  1050	        # resume (or a steer / first start) gets a fresh post-resume
  1051	        # one-shot AND a fresh per-boundary counter (the per-boundary
  1052	        # counter is what prevents re-emit on consecutive steps at
  1053	        # the same boundary; resetting on every segment makes the
  1054	        # cadence scoped to the current run, not the conversation).
  1055	        # Contrast with _bootstrap_emitted (F4), which persists across
  1056	        # run() segments — the F4 bootstrap is "fire once per
  1057	        # conversation" (the model already saw it), while HS-03 is
  1058	        # "fire once per resume" (a pause may have lost context, so
  1059	        # a resume is exactly when the recap matters).
  1060	        self._hs03_reground_post_resume_emitted = False
  1061	        self._hs03_reground_last_action_count = -1
  1062	        # C18 — NOTE: `_plan_step_predicates` is intentionally NOT reset
  1063	        # here. The map is per-(plan_revision, step_index) and is
  1064	        # populated by `_plan_from_args` at submit_plan / re-plan time;
  1065	        # a re-plan overwrites by revision so stale entries can't match.
  1066	        # Clearing on every run() entry would wipe the predicates
  1067	        # between planning and the first post-approve execution call,
  1068	        # silently disabling the advisory check.
  1069	        state = await self.get_state()
  1070	        if state.execution_status in _TERMINAL_FOR_NOW and state.execution_status != (
  1071	            ConversationStatus.IDLE
  1072	        ):
  1073	            # Paused/waiting/finished/stuck/error: a fresh run must be re-armed by
  1074	            # a control op (resume/confirm) or a new message. IDLE means "ready".
  1075	            if state.execution_status in (
  1076	                ConversationStatus.PAUSED,
  1077	                ConversationStatus.WAITING_FOR_CONFIRMATION,
  1078	                ConversationStatus.AWAITING_PLAN_APPROVAL,
  1079	            ):
  1080	                return state
  1081	            # AWAITING_USER_DECISION / AWAITING_USER_QUESTION: the agent
  1082	            # voluntarily paused for the user (pick-a-card or free-form question).
  1083	            # A new user message (steer / send_message) IS the resume signal —
  1084	            # treat it like FINISHED/STUCK below (re-kick if there's fresh work).
  1085	            # No special control op needed; the user just typing IS the answer.
  1086	            # FINISHED/STUCK/ERROR/AWAITING_USER_*/no new work → idle.
  1087	            if not self._has_unprocessed_user_message(await self._events()):
  1088	                return state
  1089	        # Restore execution mode after a server restart. `self.mode` is in-memory only;
  1090	        # a fresh loop always starts in PLANNING mode regardless of what the event log
  1091	        # says. Scan the event log to determine whether the current lifecycle is in
  1092	        # execution mode (plan was approved and not subsequently re-entered planning).
  1093	        # This fixes post-restart resumes: without it, finish triggers PLAN_NUDGE.
  1094	        if self.mode == OperatingMode.PLANNING and self._execution_mode != OperatingMode.PLANNING:
  1095	            _boot_events = await self._events()
  1096	            _plan_approved_seq: int | None = None
  1097	            _reenter_planning_seq: int | None = None
  1098	            for _e in _boot_events:
  1099	                if isinstance(_e, StatusEvent):
  1100	                    if _e.detail == "plan_approved":
  1101	                        _plan_approved_seq = _e.seq or 0
  1102	                    elif _e.detail == "planning":  # request_plan re-enters planning mode
  1103	                        _reenter_planning_seq = _e.seq or 0
  1104	            # In execution mode if: plan approved AND not re-entered planning after that.
  1105	            if _plan_approved_seq is not None and (
  1106	                _reenter_planning_seq is None
  1107	                or _plan_approved_seq > _reenter_planning_seq
  1108	            ):
  1109	                self.mode = self._execution_mode
  1110	
  1111	        await self._emit(StatusEvent(status=ConversationStatus.RUNNING))
  1112	
  1113	        while True:
  1114	            action_to_execute: ActionEvent | None = None
  1115	            async with self._lock:
  1116	                events = await self._events()
  1117	                state = ConversationState.reconstruct(
  1118	                    self.conversation_id, events, max_iterations=self.max_iterations
  1119	                )
  1120	                status = state.execution_status
  1121	
  1122	                # WALK-18 — cooperative pause lands here at the step boundary
  1123	                # (pause() only SET the flag, without the lock). Emit PAUSED
  1124	                # ourselves and return; resume re-kicks (the flag is cleared at
  1125	                # run() entry). This is a step-boundary pause — a true
  1126	                # mid-model-step interrupt would need driver cooperation.
  1127	                if self._pause_requested.is_set():
  1128	                    self._pause_requested.clear()
  1129	                    await self._emit(StatusEvent(status=ConversationStatus.PAUSED))
  1130	                    return await self.get_state()
  1131	
  1132	                # (a) honor control transitions decided between steps
  1133	                if status in (
  1134	                    ConversationStatus.PAUSED,
  1135	                    ConversationStatus.IDLE,
  1136	                    ConversationStatus.FINISHED,
  1137	                    ConversationStatus.STUCK,
  1138	                    ConversationStatus.ERROR,
  1139	                    ConversationStatus.WAITING_FOR_CONFIRMATION,
  1140	                    ConversationStatus.AWAITING_PLAN_APPROVAL,
  1141	                ):
  1142	                    return state
  1143	
  1144	                # (b) iteration ceiling — the ultimate backstop (principle 4)
  1145	                if state.iteration >= self.max_iterations:
  1146	                    await self._emit(
  1147	                        ErrorEvent(
  1148	                            code="max_iterations",
  1149	                            detail=f"reached {self.max_iterations}",
  1150	                        )
  1151	                    )
  1152	                    return await self.get_state()
  1153	
  1154	                # C5 — drain any recovered MEMORY facts the session has staged
  1155	                # from a post-recreate read of `.pmx/MEMORY.md` (SandboxSession
  1156	                # sets these on a mid-session box death / hard reset). The
  1157	                # in-View channel is the authoritative in-session source; this
  1158	                # is the moment the recovered facts are re-emitted as
  1159	                # KnowledgeEvents, restoring the View to match the surviving
  1160	                # on-disk mirror. Runs once per recreate (the session drains
  1161	                # itself on `take_recovered_memory_facts`).
  1162	                await self._recit.drain_recovered_memory_facts()
  1163	
  1164	                events = await self._valve.gate_f4_bootstrap(events)
  1165	

exec
/bin/bash -lc "nl -ba packages/core/src/disco/core/loop/engine.py | sed -n '1335,1610p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
  1335	
  1336	            # (j) EXECUTE outside the lock (long-running; lock only guards state)
  1337	            await self._emit(action_to_execute)
  1338	            await self._execute_and_observe(action_to_execute)
  1339	            # loop continues
  1340	
  1341	    # _has_unprocessed_user_message delegates to signals (external callers + run()).
  1342	    _has_unprocessed_user_message = staticmethod(signals.has_unprocessed_user_message)
  1343	
  1344	    # ---- control operations (§7) — map from the wire frames -----------------
  1345	
  1346	    async def send_message(self, text: str, *, steer: bool = False) -> ConversationState:
  1347	        """Append a USER message. Never dropped; picked up at the next iteration
  1348	        (the next View includes it). If the conversation had FINISHED/STUCK, it
  1349	        reopens to IDLE (§2). `steer` differs only in UI intent (BoD §13.4)."""
  1350	        async with self._lock:
  1351	            state = await self.get_state()
  1352	            await self._emit(
  1353	                MessageEvent(
  1354	                    source=EventSource.USER,
  1355	                    message=LLMMessage(role="user", content=text),
  1356	                    meta={"steer": True} if steer else {},
  1357	                )
  1358	            )
  1359	            if state.execution_status in (
  1360	                ConversationStatus.FINISHED,
  1361	                ConversationStatus.STUCK,
  1362	            ):
  1363	                await self._emit(StatusEvent(status=ConversationStatus.IDLE))
  1364	        return await self.get_state()
  1365	
  1366	    async def steer(self, text: str) -> ConversationState:
  1367	        return await self.send_message(text, steer=True)
  1368	
  1369	    async def confirm(self) -> ConversationState:
  1370	        """Phase 2 of the confirmation gate: execute EXACTLY the pending action
  1371	        (no re-ask to the model), then resume (§5)."""
  1372	        async with self._lock:
  1373	            state = await self.get_state()
  1374	            if (
  1375	                state.execution_status != ConversationStatus.WAITING_FOR_CONFIRMATION
  1376	                or state.pending_action_id is None
  1377	            ):
  1378	                return state
  1379	            pending = await self._event_by_id(state.pending_action_id)
  1380	            await self._emit(StatusEvent(status=ConversationStatus.RUNNING))
  1381	        if isinstance(pending, ActionEvent):
  1382	            await self._execute_and_observe(pending)  # outside the lock
  1383	        return await self.get_state()
  1384	
  1385	    async def reject(self, reason: str = "rejected by user") -> ConversationState:
  1386	        """Deny the pending action: record the denial (so the model sees it next
  1387	        View) and resume to RUNNING without executing (§5).
  1388	
  1389	        The rejection is recorded as an `AgentErrorEvent` whose content is wrapped
  1390	        in an implicit `<system-reminder>` — the action did not produce an error,
  1391	        the human declined it. Paired with the proposed action's call_id so the
  1392	        provider adapter sees a properly-correlated tool result."""
  1393	        async with self._lock:
  1394	            state = await self.get_state()
  1395	            if state.execution_status != ConversationStatus.WAITING_FOR_CONFIRMATION:
  1396	                return state
  1397	            pending = (
  1398	                await self._event_by_id(state.pending_action_id)
  1399	                if state.pending_action_id
  1400	                else None
  1401	            )
  1402	            call_id: str | None = None
  1403	            if isinstance(pending, ActionEvent) and pending.tool_call:
  1404	                call_id = pending.tool_call.call_id
  1405	            reminder = (
  1406	                "<system-reminder>\n"
  1407	                f"The user reviewed the proposed action and did not approve it "
  1408	                f"({reason}). The action was NOT executed. Pick a different "
  1409	                "approach.\n"
  1410	                "</system-reminder>"
  1411	            )
  1412	            await self._emit(
  1413	                AgentErrorEvent(
  1414	                    error=reminder,
  1415	                    action_id=state.pending_action_id,
  1416	                    tool_call_id=call_id,
  1417	                )
  1418	            )
  1419	            await self._emit(StatusEvent(status=ConversationStatus.RUNNING))
  1420	        return await self.get_state()
  1421	
  1422	    async def approve_plan(self) -> ConversationState:
  1423	        """Approve the pending plan: flip into execution mode (full tools restored)
  1424	        and resume to RUNNING. The caller then re-runs the loop. The per-action
  1425	        risk gate still governs the build that follows (defense in depth)."""
  1426	        async with self._lock:
  1427	            state = await self.get_state()
  1428	            if state.execution_status != ConversationStatus.AWAITING_PLAN_APPROVAL:
  1429	                return state
  1430	            self.mode = self._execution_mode
  1431	            await self._emit(
  1432	                StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved")
  1433	            )
  1434	        return await self.get_state()
  1435	
  1436	    async def pick_alternative(self, option_id: str) -> ConversationState:
  1437	        """Resume from AWAITING_USER_DECISION by selecting one of the agent's
  1438	        proposed alternatives. Pulls the option's ToolCall, synthesizes an
  1439	        ActionEvent under EventSource.AGENT (the loop "speaks for" the agent
  1440	        here — the option WAS the agent's own proposal, the user is just
  1441	        choosing which one), and transitions to RUNNING. The loop's next
  1442	        iteration sees the dangling ActionEvent and executes it via the normal
  1443	        _execute_and_observe path (including the risk gate).
  1444	
  1445	        Idempotent: a pick on a non-pending state is a no-op."""
  1446	        async with self._lock:
  1447	            state = await self.get_state()
  1448	            if state.execution_status != ConversationStatus.AWAITING_USER_DECISION:
  1449	                return state
  1450	            # Find the latest AlternativesEvent + the picked option.
  1451	            events = await self._events()
  1452	            alt: AlternativesEvent | None = None
  1453	            for e in reversed(events):
  1454	                if isinstance(e, AlternativesEvent):
  1455	                    alt = e
  1456	                    break
  1457	            if alt is None:
  1458	                # No alternatives event but status said pending — log a
  1459	                # reminder + clear to RUNNING so the user isn't trapped.
  1460	                await self._emit(
  1461	                    StatusEvent(status=ConversationStatus.RUNNING, detail="alternatives_missing")
  1462	                )
  1463	                return await self.get_state()
  1464	            # D2 BYPASS — "Continue anyway": the user lets the agent keep going with
  1465	            # its own judgment. RESET the failure streak (a USER message resets
  1466	            # _count_recent_failures) and resume RUNNING; the agent picks its own next
  1467	            # step instead of one of the proposed alternatives. This is the escape
  1468	            # hatch the crude breaker lacked.
  1469	            if option_id == _CONTINUE_OPTION_ID:
  1470	                await self._emit(
  1471	                    MessageEvent(
  1472	                        source=EventSource.USER,
  1473	                        message=LLMMessage(
  1474	                            role="user",
  1475	                            content=(
  1476	                                "Continue — keep working. I've reviewed the failures and "
  1477	                                "want you to proceed with your own best next step. Don't just "
  1478	                                "repeat the exact action that was failing; adjust your approach."
  1479	                            ),
  1480	                        ),
  1481	                    )
  1482	                )
  1483	                await self._emit(
  1484	                    StatusEvent(status=ConversationStatus.RUNNING, detail="continue_anyway")
  1485	                )
  1486	                return await self.get_state()
  1487	            option = next((o for o in alt.options if o.id == option_id), None)
  1488	            if option is None:
  1489	                # Bad pick (stale id?) — record + return without resuming so
  1490	                # the UI can re-prompt with the same gate.
  1491	                await self._emit(
  1492	                    MessageEvent(
  1493	                        source=EventSource.ENVIRONMENT,
  1494	                        message=LLMMessage(
  1495	                            role="user",
  1496	                            content=(
  1497	                                "<system-reminder>\n"
  1498	                                f"User picked an unknown alternative id ({option_id!r}). "
  1499	                                "Re-emit the gate or ask for direction.\n"
  1500	                                "</system-reminder>"
  1501	                            ),
  1502	                        ),
  1503	                    )
  1504	                )
  1505	                return await self.get_state()
  1506	            # Record the human's pick as a user message — keeps the log honest
  1507	            # (the audit trail shows "user chose X") AND lands in the LLM View
  1508	            # so a follow-up turn has the context.
  1509	            await self._emit(
  1510	                MessageEvent(
  1511	                    source=EventSource.USER,
  1512	                    message=LLMMessage(
  1513	                        role="user",
  1514	                        content=(
  1515	                            f"Try the alternative approach: “{option.title}”. "
  1516	                            f"{option.description}"
  1517	                        ),
  1518	                    ),
  1519	                )
  1520	            )
  1521	            # Synthesize the ActionEvent. The thought records WHY we're running
  1522	            # it (the option's description) so the trace stays self-explanatory.
  1523	            action = ActionEvent(
  1524	                source=EventSource.AGENT,
  1525	                thought=f"User picked alternative: {option.title}. {option.description}",
  1526	                tool_call=ToolCall(
  1527	                    tool_name=option.tool_name,
  1528	                    arguments=option.arguments,
  1529	                ),
  1530	            )
  1531	            emitted = await self._emit(action)
  1532	            await self._emit(
  1533	                StatusEvent(
  1534	                    status=ConversationStatus.RUNNING,
  1535	                    detail=f"alternative_picked:{option.id}",
  1536	                )
  1537	            )
  1538	        # Execute the synthesized action directly so the option actually runs
  1539	        # before returning to the main loop (analogous to confirm()'s
  1540	        # post-gate execute). The loop's next call to run() then proceeds
  1541	        # with the freshly-emitted observation in view.
  1542	        # `emitted` is the persisted copy of the ActionEvent above (store.append
  1543	        # returns the same event type with `seq` filled); _emit's return is typed
  1544	        # as the broad Event union.
  1545	        await self._execute_and_observe(cast("ActionEvent", emitted))
  1546	        return await self.run()
  1547	
  1548	    async def enter_planning(self, text: str = "") -> ConversationState:
  1549	        """(Re-)enter PLANNING mode — the entry point for the first plan AND for
  1550	        re-planning after a build, so focused, diff-style changes are articulated
  1551	        and re-approved rather than free-form steered. `text` is the user's
  1552	        instruction for what to (re)plan. Reopens from FINISHED/STUCK."""
  1553	        async with self._lock:
  1554	            # (B2/B6) A revision iff a plan was already approved earlier in this
  1555	            # lifecycle. Compute BEFORE emitting the new user turn / planning
  1556	            # status so neither perturbs the check.
  1557	            is_revision = any(
  1558	                isinstance(e, StatusEvent) and e.detail == "plan_approved"
  1559	                for e in await self._events()
  1560	            )
  1561	            if text.strip():
  1562	                await self._emit(
  1563	                    MessageEvent(
  1564	                        source=EventSource.USER,
  1565	                        message=LLMMessage(role="user", content=text),
  1566	                    )
  1567	                )
  1568	            self.mode = OperatingMode.PLANNING
  1569	            self._plan_explore_reads = 0  # (B2/B6) fresh planning segment
  1570	            await self._emit(StatusEvent(status=ConversationStatus.RUNNING, detail="planning"))
  1571	            # (B2/B6) On a revision (prior plan_approved) with a concrete new
  1572	            # instruction, frame the turn so the model RE-plans instead of
  1573	            # free-building against the OLD plan (logic in Planner).
  1574	            await self._planner.emit_replan_framing_if_revision(text)
  1575	        return await self.get_state()
  1576	
  1577	    async def pause(self) -> ConversationState:
  1578	        """WALK-18 — cooperative pause. Deliberately does NOT take self._lock
  1579	        (unlike cancel): the running turn holds the lock across its model step,
  1580	        so taking it here would block until the step finishes — exactly the
  1581	        "pause does nothing until I steer" bug. Instead set a flag the run()
  1582	        loop observes at its next step boundary, where it emits PAUSED."""
  1583	        self._pause_requested.set()
  1584	        return await self.get_state()
  1585	
  1586	    async def resume(self) -> ConversationState:
  1587	        self._pause_requested.clear()  # WALK-18 — resume cancels a pending pause
  1588	        async with self._lock:
  1589	            await self._emit(StatusEvent(status=ConversationStatus.RUNNING))
  1590	        return await self.run()
  1591	
  1592	    async def cancel(self) -> ConversationState:
  1593	        """Cooperative stop (distinct from the network-level kill switch, §7.3).
  1594	        Emits a terminal IDLE; the loop returns at its next checkpoint."""
  1595	        async with self._lock:
  1596	            await self._emit(StatusEvent(status=ConversationStatus.IDLE, detail="cancelled"))
  1597	        return await self.get_state()

exec
/bin/bash -lc "nl -ba packages/core/src/disco/core/state.py | sed -n '88,155p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
    88	
    89	        for e in events:
    90	            if e.seq is not None:
    91	                st.last_seq = e.seq
    92	
    93	            if isinstance(e, StatusEvent):
    94	                st.execution_status = e.status
    95	                if e.status == ConversationStatus.WAITING_FOR_CONFIRMATION:
    96	                    # The pending action is the latest one the agent proposed.
    97	                    st.pending_action_id = last_action_id
    98	                else:
    99	                    st.pending_action_id = None
   100	                if e.status == ConversationStatus.AWAITING_PLAN_APPROVAL:
   101	                    st.pending_plan_id = last_plan_id
   102	                else:
   103	                    st.pending_plan_id = None
   104	                if e.status == ConversationStatus.AWAITING_USER_DECISION:
   105	                    st.pending_alternatives_id = last_alternatives_id
   106	                else:
   107	                    st.pending_alternatives_id = None
   108	                if e.status == ConversationStatus.AWAITING_USER_QUESTION:
   109	                    # Prefer the explicit id the engine stamped on the status
   110	                    # event; fall back to the last agent message.
   111	                    st.pending_question_id = e.detail or last_agent_message_id
   112	                    # This gate is a clarify card ONLY when the engine stamped THIS
   113	                    # status with the clarify event's id. A free-form ask_user gate
   114	                    # stamps the agent message id instead — so gating on the detail
   115	                    # (not just "the last clarify seen") stops a stale clarify card
   116	                    # from shadowing a later ask_user question on reload/reconnect.
   117	                    st.pending_clarify_id = (
   118	                        last_clarify_id if e.detail == last_clarify_id else None
   119	                    )
   120	                else:
   121	                    st.pending_question_id = None
   122	                    st.pending_clarify_id = None
   123	            elif isinstance(e, ActionEvent):
   124	                run_iteration += 1
   125	                last_action_id = e.id
   126	            elif isinstance(e, PlanEvent):
   127	                last_plan_id = e.id
   128	            elif isinstance(e, AlternativesEvent):
   129	                last_alternatives_id = e.id
   130	            elif isinstance(e, MessageEvent) and e.source == EventSource.USER:
   131	                # A fresh user instruction starts a new run (ceiling + stuck).
   132	                run_iteration = 0
   133	            elif isinstance(e, MessageEvent) and e.source == EventSource.AGENT:
   134	                # Track the latest agent message — the free-form Ask-gate's
   135	                # question is the agent message just before the status flip.
   136	                last_agent_message_id = e.id
   137	            elif isinstance(e, ClarifyEvent):
   138	                last_clarify_id = e.id
   139	            elif isinstance(e, ErrorEvent):
   140	                st.execution_status = ConversationStatus.ERROR
   141	
   142	        st.iteration = run_iteration
   143	        return st

exec
/bin/bash -lc "nl -ba packages/core/src/disco/core/events.py | sed -n '180,220p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   180	
   181	    UNKNOWN = "UNKNOWN"
   182	    LOW = "LOW"
   183	    MEDIUM = "MEDIUM"
   184	    HIGH = "HIGH"
   185	
   186	
   187	class ConversationStatus(str, Enum):
   188	    """The loop's explicit execution state machine (BoD §12.1)."""
   189	
   190	    IDLE = "IDLE"
   191	    RUNNING = "RUNNING"
   192	    PAUSED = "PAUSED"
   193	    STUCK = "STUCK"
   194	    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
   195	    # The agent proposed a plan and the loop is halted until the human approves
   196	    # it (Build plan-mode). Mirrors WAITING_FOR_CONFIRMATION but gates the whole
   197	    # plan up front, not one risky action.
   198	    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
   199	    # The run is halted for the user to decide. Reached two ways: (1) the agent
   200	    # itself calls `ask_user` with options; (2) the harness circuit breaker fires
   201	    # after `_circuit_breaker_threshold` consecutive failures and hands off rather
   202	    # than grinding. The user clicks an option (when present) or steers via
   203	    # send_message. (There is no `propose_alternatives` tool — the model uses
   204	    # `ask_user`; the breaker is harness-driven.)
   205	    AWAITING_USER_DECISION = "AWAITING_USER_DECISION"
   206	    # The agent called `ask_user` with a FREE-FORM question (no options) and the
   207	    # loop is halted until the user TYPES an answer. The two-way Ask-gate: unlike
   208	    # AWAITING_USER_DECISION (pick a card), this is an open question the user
   209	    # answers in prose. The user's reply (send_message / steer) IS the resume
   210	    # signal — same re-kick semantics as AWAITING_USER_DECISION, just a different
   211	    # surface (AskPanel, not the alternatives cards).
   212	    AWAITING_USER_QUESTION = "AWAITING_USER_QUESTION"
   213	    FINISHED = "FINISHED"
   214	    ERROR = "ERROR"
   215	
   216	
   217	# ---- concrete event types ---------------------------------------------------
   218	
   219	
   220	class MessageEvent(BaseEvent, LLMConvertible):

codex
The status model confirms AWAITING states are deliberate pause states, not terminal success. That matters later for the verify runner: a scenario ending in an AWAITING gate needs an explicit expected outcome, otherwise it can be mistaken for “not hanging.”
exec
/bin/bash -lc "nl -ba packages/core/src/disco/core/loop/engine.py | sed -n '145,180p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   145	# has drifted through dozens of actions can lose track of the stable facts
   146	# (goal, plan state, recently-touched files). The fix mirrors the smolagents
   147	# `planning_interval` pattern: every N actions, re-anchor with a short
   148	# RECAP. The recap is FACTS ONLY — no imperative/steer language — so the
   149	# no-automatic-nudge invariant (commit c97c1b3) survives. Assist OFF
   150	# (capable-model default) is byte-identical to today: the gate is closed
   151	# and the helper is never called. Post-resume the recap fires ONCE on the
   152	# very first step (a pause may have lost context). Cadence thereafter:
   153	# `_HS03_REGROUND_INTERVAL` actions since the last resume. A per-boundary
   154	# guard (the last-emitted action count) prevents re-firing on consecutive
   155	# steps at the same boundary. Interval chosen at 12 (vs. C6's 5) so the
   156	# recaps don't pile up: the C6 recap fires every 5 steps for plan state;
   157	# the HS-03 recap fires every 12 steps and carries the broader
   158	# goal/files/constraints anchors a drifted model most often loses.
   159	_HS03_REGROUND_INTERVAL = 12
   160	
   161	# Statuses at which the loop yields control back to the caller at a checkpoint.
   162	_TERMINAL_FOR_NOW = frozenset(
   163	    {
   164	        ConversationStatus.PAUSED,
   165	        ConversationStatus.IDLE,
   166	        ConversationStatus.FINISHED,
   167	        ConversationStatus.STUCK,
   168	        ConversationStatus.ERROR,
   169	        ConversationStatus.WAITING_FOR_CONFIRMATION,
   170	        ConversationStatus.AWAITING_PLAN_APPROVAL,
   171	        # ask_user halts the loop — kicks must not race past the gate. Both the
   172	        # pick-a-card (DECISION) and the free-form (QUESTION) Ask-gates re-kick
   173	        # only on the user's reply.
   174	        ConversationStatus.AWAITING_USER_DECISION,
   175	        ConversationStatus.AWAITING_USER_QUESTION,
   176	    }
   177	)
   178	
   179	# Injected (as an implicit system-reminder) when the planner answers in prose
   180	# instead of calling submit_plan: in PLANNING mode the terminal moves are to

exec
/bin/bash -lc "nl -ba packages/core/src/disco/core/loop/turn_control.py | sed -n '1030,1160p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
  1030	                StatusEvent(
  1031	                    status=ConversationStatus.RUNNING, detail="plan_approved"
  1032	                )
  1033	            )
  1034	            return Disp.CONTINUE
  1035	        await self._loop._emit(
  1036	            StatusEvent(
  1037	                status=ConversationStatus.AWAITING_PLAN_APPROVAL,
  1038	                detail=new_plan.id,
  1039	            )
  1040	        )
  1041	        return Disp.HALT
  1042	
  1043	    async def handle_clarify(self, step: AgentStep, events: list[Event]) -> Disp:
  1044	        assert step.tool_call is not None  # caller (engine loop) dispatches by tool_name
  1045	        # clarify → ClarifyEvent with typed questions. The planner
  1046	        # calls this when SEVERAL specifics are missing and
  1047	        # guessing would produce a bad plan. The loop emits a
  1048	        # ClarifyEvent (carrying the structured question items)
  1049	        # and halts at AWAITING_USER_QUESTION. The user answers
  1050	        # each question; the answers are re-injected as a user
  1051	        # message that resumes planning.
  1052	        from ..events import ClarifyEvent as _ClarifyEvent
  1053	        from ..events import ClarifyQuestionItem
  1054	
  1055	        question = str(
  1056	            step.tool_call.arguments.get("question") or ""
  1057	        ).strip() or step.thought.strip()
  1058	        raw_items = step.tool_call.arguments.get("questions") or []
  1059	        items: list[ClarifyQuestionItem] = []
  1060	        for it in raw_items:
  1061	            if not isinstance(it, dict):
  1062	                continue
  1063	            qid = str(it.get("id") or f"q{len(items) + 1}").strip()
  1064	            qtext = str(it.get("question") or "").strip()
  1065	            if not qtext:
  1066	                continue
  1067	            qtype = str(it.get("type") or "short_text").strip()
  1068	            if qtype not in ("short_text", "long_text", "choice"):
  1069	                qtype = "short_text"
  1070	            qopts = it.get("options") or []
  1071	            if isinstance(qopts, list):
  1072	                qopts = [str(o) for o in qopts]
  1073	            else:
  1074	                qopts = []
  1075	            items.append(
  1076	                ClarifyQuestionItem(
  1077	                    id=qid, question=qtext, type=qtype, options=qopts
  1078	                )
  1079	            )
  1080	        if not items:
  1081	            # No valid questions → fall back to free-form ask_user
  1082	            q_event = MessageEvent(
  1083	                source=EventSource.AGENT,
  1084	                message=LLMMessage(
  1085	                    role="assistant",
  1086	                    content=(
  1087	                        question or "The agent needs clarification before planning."
  1088	                    ),
  1089	                ),
  1090	            )
  1091	            await self._loop._emit(q_event)
  1092	            await self._loop._emit(
  1093	                StatusEvent(
  1094	                    status=ConversationStatus.AWAITING_USER_QUESTION,
  1095	                    detail=q_event.id,
  1096	                )
  1097	            )
  1098	            return Disp.HALT
  1099	        clarify_event = _ClarifyEvent(
  1100	            question=question or "The agent needs clarification before planning.",
  1101	            items=items,
  1102	        )
  1103	        await self._loop._emit(clarify_event)
  1104	        await self._loop._emit(
  1105	            StatusEvent(
  1106	                status=ConversationStatus.AWAITING_USER_QUESTION,
  1107	                detail=clarify_event.id,
  1108	            )
  1109	        )
  1110	        return Disp.HALT
  1111	
  1112	    async def handle_ask_user(self, step: AgentStep, events: list[Event]) -> Disp:
  1113	        assert step.tool_call is not None  # caller (engine loop) dispatches by tool_name
  1114	        alt = self._loop._alternatives_from_args(step.tool_call.arguments, events)
  1115	        if alt is not None:
  1116	            # ask_user WITH options → AlternativesEvent + gate
  1117	            await self._loop._emit(alt)
  1118	            await self._loop._emit(
  1119	                StatusEvent(
  1120	                    status=ConversationStatus.AWAITING_USER_DECISION,
  1121	                    detail=alt.id,
  1122	                )
  1123	            )
  1124	            return Disp.HALT
  1125	        # ask_user WITHOUT options → free-form question. Emit it as
  1126	        # an assistant message (from the tool's `question` arg or
  1127	        # the step's thought, whichever has content) and halt at the
  1128	        # two-way Ask-gate (AWAITING_USER_QUESTION) — the UI renders
  1129	        # an AskPanel with a focused answer box, not muted prose. The
  1130	        # status's detail carries the question message's id so the
  1131	        # surface can resolve it. The user's reply (send_message /
  1132	        # steer) IS the resume signal.
  1133	        question = str(
  1134	            step.tool_call.arguments.get("question") or ""
  1135	        ).strip() or step.thought.strip()
  1136	        question_id: str | None = None
  1137	        if question:
  1138	            q_event = MessageEvent(
  1139	                source=EventSource.AGENT,
  1140	                message=LLMMessage(role="assistant", content=question),
  1141	            )
  1142	            question_id = q_event.id
  1143	            await self._loop._emit(q_event)
  1144	        await self._loop._emit(
  1145	            StatusEvent(
  1146	                status=ConversationStatus.AWAITING_USER_QUESTION,
  1147	                detail=question_id or "free_form_question",
  1148	            )
  1149	        )
  1150	        return Disp.HALT

exec
/bin/bash -lc 'rg -n "AWAITING_USER_DECISION|AWAITING_USER_QUESTION" packages/core/src/disco/core/loop/engine.py packages/core/src/disco/core/loop/turn_control.py' in /var/home/dylan/projects/disco
 succeeded in 0ms:
packages/core/src/disco/core/loop/turn_control.py:381:        # it halts at AWAITING_USER_DECISION with a summary of what failed.
packages/core/src/disco/core/loop/turn_control.py:435:                # indefinite AWAITING_USER_DECISION stall. Bounded: we only
packages/core/src/disco/core/loop/turn_control.py:489:                    status=ConversationStatus.AWAITING_USER_DECISION,
packages/core/src/disco/core/loop/turn_control.py:1049:        # and halts at AWAITING_USER_QUESTION. The user answers
packages/core/src/disco/core/loop/turn_control.py:1094:                    status=ConversationStatus.AWAITING_USER_QUESTION,
packages/core/src/disco/core/loop/turn_control.py:1106:                status=ConversationStatus.AWAITING_USER_QUESTION,
packages/core/src/disco/core/loop/turn_control.py:1120:                    status=ConversationStatus.AWAITING_USER_DECISION,
packages/core/src/disco/core/loop/turn_control.py:1128:        # two-way Ask-gate (AWAITING_USER_QUESTION) — the UI renders
packages/core/src/disco/core/loop/turn_control.py:1146:                status=ConversationStatus.AWAITING_USER_QUESTION,
packages/core/src/disco/core/loop/engine.py:174:        ConversationStatus.AWAITING_USER_DECISION,
packages/core/src/disco/core/loop/engine.py:175:        ConversationStatus.AWAITING_USER_QUESTION,
packages/core/src/disco/core/loop/engine.py:537:        # instead of an indefinite AWAITING_USER_DECISION stall.
packages/core/src/disco/core/loop/engine.py:591:        # harness hands off to the user (AWAITING_USER_DECISION) instead of
packages/core/src/disco/core/loop/engine.py:1081:            # AWAITING_USER_DECISION / AWAITING_USER_QUESTION: the agent
packages/core/src/disco/core/loop/engine.py:1287:                # AWAITING_USER_QUESTION — a silent headless stall, since there is no
packages/core/src/disco/core/loop/engine.py:1437:        """Resume from AWAITING_USER_DECISION by selecting one of the agent's
packages/core/src/disco/core/loop/engine.py:1448:            if state.execution_status != ConversationStatus.AWAITING_USER_DECISION:

exec
/bin/bash -lc "nl -ba packages/core/src/disco/core/loop/turn_control.py | sed -n '465,505p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   465	                + "\n".join(f"  • {err[:200]}" for err in recent_errors[:4])
   466	            )
   467	            failed_action_id = next(
   468	                (e.id for e in reversed(events) if isinstance(e, ActionEvent)), ""
   469	            )
   470	            alt = AlternativesEvent(
   471	                failed_action_id=failed_action_id,
   472	                summary=summary,
   473	                options=[
   474	                    AlternativeOption(
   475	                        id=_CONTINUE_OPTION_ID,
   476	                        title="Continue anyway",
   477	                        description=(
   478	                            "Reset the failure streak and let the agent try another "
   479	                            "approach with its own judgment."
   480	                        ),
   481	                        tool_name="",  # not a tool — the loop intercepts this id
   482	                        arguments={},
   483	                    )
   484	                ],
   485	            )
   486	            await self._loop._emit(alt)
   487	            await self._loop._emit(
   488	                StatusEvent(
   489	                    status=ConversationStatus.AWAITING_USER_DECISION,
   490	                    detail=alt.id,
   491	                )
   492	            )
   493	            return Disp.HALT
   494	        return Disp.FALLTHROUGH
   495	
   496	    async def gate_no_progress(self, events: list[Event]) -> Disp:
   497	        # (c.4) WALK-19 — SEMANTIC no-progress breaker. Failure-INDEPENDENT and
   498	        # NOT assist-gated: catches the "successful but useless varied edits"
   499	        # loop (writes apply, dev server 200, no errors — the black-screen-game)
   500	        # that every failure-keyed breaker (stuck/circuit) sails past, grinding
   501	        # to max_iterations. Escalate-then-halt like gate_stuck: a corrective
   502	        # nudge first, then — if the SAME outcome persists after the model acted
   503	        # on the nudge — halt STUCK rather than burn the rest of the budget.
   504	        if not repeated_verify_no_progress(self._loop._recent(events)):
   505	            return Disp.FALLTHROUGH

codex
Runtime supervision behavior is plausible for normal running loops, but the test coverage uses a monkeypatched loop and a fixed `sleep(0.2)` for terminalization. I’ll look for a test that drives `_run_with_persistence` or storage failures, because that’s where the callback’s “create another task” can disappear or fail silently.
exec
/bin/bash -lc "nl -ba packages/agent-server/tests/integration/test_task_supervision.py | sed -n '117,260p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   117	async def test_terminalize_is_idempotent(monkeypatch):
   118	    """If the run already concluded (e.g. FINISHED) before the callback, don't overwrite."""
   119	    store = SqliteEventStore(":memory:")
   120	    runtime = _runtime(store)
   121	    cid = await _kicked_conversation(store, runtime)
   122	
   123	    class _FinishThenRaise:
   124	        async def run(self):
   125	            await store.append(cid, StatusEvent(status=ConversationStatus.FINISHED))
   126	            raise RuntimeError("late crash after finishing")
   127	
   128	    monkeypatch.setattr(runtime, "_loop_for", lambda _c: _FinishThenRaise())
   129	    runtime.kick(cid)
   130	    task = runtime._tasks.get(cid)
   131	    with contextlib.suppress(Exception):
   132	        await asyncio.wait_for(asyncio.shield(task), timeout=10)
   133	    await asyncio.sleep(0.2)
   134	
   135	    state = await store.get_state(cid)
   136	    assert (
   137	        state.execution_status == ConversationStatus.FINISHED
   138	    ), "must not clobber an already-terminal status"

exec
/bin/bash -lc "rg -n \"terminalize|supervision|_on_run_task_done|_terminalize_crashed|RUNNING forever|sleep\\(0\\.2\\)|IDLE\" packages/agent-server/tests packages/core/tests packages/tools/tests | head -n 220" in /var/home/dylan/projects/disco
 succeeded in 0ms:
packages/tools/tests/test_sandbox_exception_contract.py:8:status RUNNING forever. The local (process) backend already raised native FileNotFoundError;
packages/agent-server/tests/test_supervision_guardrail.py:1:"""W10 — static guardrail locking in W2 (loop-task supervision).
packages/agent-server/tests/test_supervision_guardrail.py:5:RUNNING forever. This asserts the supervision wiring stays in place. (W2's behavioural tests
packages/agent-server/tests/test_supervision_guardrail.py:19:def test_kick_wires_the_supervision_callback():
packages/agent-server/tests/test_supervision_guardrail.py:22:    assert "_on_run_task_done" in src, "kick() must route task completion through the supervisor"
packages/agent-server/tests/test_supervision_guardrail.py:25:def test_supervisor_terminalizes_to_error():
packages/agent-server/tests/test_supervision_guardrail.py:27:    src = inspect.getsource(ConversationRuntime._terminalize_crashed)
packages/agent-server/tests/test_supervision_guardrail.py:28:    assert "ConversationStatus.ERROR" in src, "a crashed run must terminalize to ERROR"
packages/agent-server/tests/integration/test_task_supervision.py:73:async def test_uncaught_loop_exception_terminalizes_as_error(monkeypatch):
packages/agent-server/tests/integration/test_task_supervision.py:84:    await asyncio.sleep(0.2)  # let the supervisor's terminalize task run
packages/agent-server/tests/integration/test_task_supervision.py:109:    await asyncio.sleep(0.2)
packages/agent-server/tests/integration/test_task_supervision.py:117:async def test_terminalize_is_idempotent(monkeypatch):
packages/agent-server/tests/integration/test_task_supervision.py:133:    await asyncio.sleep(0.2)
packages/agent-server/tests/test_control_ops_ship_it.py:247:    a non-FINISHED conversation (e.g. IDLE — a fresh conversation) must fall
packages/agent-server/tests/test_control_ops_ship_it.py:251:    # IDLE by default (no StatusEvent appended) — NOT FINISHED.
packages/tools/tests/test_kernel_idle_cull.py:396:    """`DISCO_KERNEL_IDLE_TIMEOUT_S` controls the threshold; missing ->
packages/tools/tests/test_kernel_idle_cull.py:399:    monkeypatch.delenv("DISCO_KERNEL_IDLE_TIMEOUT_S", raising=False)
packages/tools/tests/test_kernel_idle_cull.py:403:    monkeypatch.setenv("DISCO_KERNEL_IDLE_TIMEOUT_S", "42")
packages/tools/tests/test_kernel_idle_cull.py:407:    monkeypatch.setenv("DISCO_KERNEL_IDLE_TIMEOUT_S", "12.5")
packages/tools/tests/test_kernel_idle_cull.py:411:    monkeypatch.setenv("DISCO_KERNEL_IDLE_TIMEOUT_S", "0")
packages/tools/tests/test_kernel_idle_cull.py:415:    monkeypatch.setenv("DISCO_KERNEL_IDLE_TIMEOUT_S", "-5")
packages/tools/tests/test_kernel_idle_cull.py:419:    monkeypatch.setenv("DISCO_KERNEL_IDLE_TIMEOUT_S", "not-a-float")
packages/tools/tests/test_kernel_idle_cull.py:423:    monkeypatch.setenv("DISCO_KERNEL_IDLE_TIMEOUT_S", "")
packages/tools/tests/test_kernel_idle_cull.py:434:    monkeypatch.setenv("DISCO_KERNEL_IDLE_TIMEOUT_S", "5")
packages/core/tests/test_loop_state_machine.py:51:    # A new message reopens the conversation (FINISHED → IDLE), then it runs.
packages/core/tests/test_loop_state_machine.py:53:    assert reopened.execution_status == ConversationStatus.IDLE
packages/core/tests/test_serialization.py:90:    raw = event_to_json_dict(status(ConversationStatus.IDLE))
packages/core/tests/test_loop_concurrency.py:113:    assert state.execution_status == ConversationStatus.IDLE  # cooperative stop
packages/agent-server/tests/test_lifecycle.py:50:    if status != ConversationStatus.IDLE:
packages/agent-server/tests/test_lifecycle.py:62:    ConversationStatus.IDLE,
packages/agent-server/tests/test_lifecycle.py:76:    with patch.dict("os.environ", {"PMX_IDLE_SUSPEND_S": "0"}):
packages/agent-server/tests/test_lifecycle.py:93:    with patch.dict("os.environ", {"PMX_IDLE_SUSPEND_S": "0"}):
packages/agent-server/tests/test_lifecycle.py:111:    with patch.dict("os.environ", {"PMX_IDLE_SUSPEND_S": "0"}):
packages/agent-server/tests/test_lifecycle.py:129:    with patch.dict("os.environ", {"PMX_IDLE_SUSPEND_S": "99999"}):
packages/agent-server/tests/test_lifecycle.py:137:    """PMX_IDLE_SUSPEND_S is read per-sweep so tests can compress time."""
packages/agent-server/tests/test_lifecycle.py:147:    with patch.dict("os.environ", {"PMX_IDLE_SUSPEND_S": "99999"}):
packages/agent-server/tests/test_lifecycle.py:152:    with patch.dict("os.environ", {"PMX_IDLE_SUSPEND_S": "0"}):
packages/agent-server/tests/test_lifecycle.py:163:    with patch.dict("os.environ", {"PMX_IDLE_SUSPEND_S": "0"}):
packages/agent-server/tests/test_lifecycle.py:242:    """IDLE is the PRIMARY live stop state (bp-12) — its containers are just as
packages/agent-server/tests/test_lifecycle.py:248:    idle_cid = await _make_conversation(store, ConversationStatus.IDLE)
packages/agent-server/tests/test_lifecycle.py:258:    assert idle_cid in destroyed, "IDLE conversation's container must be swept"
packages/agent-server/tests/test_idle_suspend.py:39:    if status != ConversationStatus.IDLE:
packages/agent-server/tests/test_idle_suspend.py:84:    with patch.dict("os.environ", {"PMX_IDLE_SUSPEND_S": "0"}):
packages/agent-server/tests/test_idle_suspend.py:92:    """TTL source precedence: env PMX_IDLE_SUSPEND_S > config sandbox.idle_ttl_s > default.
packages/agent-server/tests/test_idle_suspend.py:119:    with patch.dict(os.environ, {"PMX_IDLE_SUSPEND_S": "0"}):
packages/agent-server/tests/test_idle_suspend.py:128:        os.environ.pop("PMX_IDLE_SUSPEND_S", None)
packages/agent-server/tests/test_idle_suspend.py:136:        os.environ.pop("PMX_IDLE_SUSPEND_S", None)
packages/agent-server/tests/test_share.py:135:    assert bundle["state"]["execution_status"] in ("IDLE", "FINISHED", "RUNNING")
packages/agent-server/tests/test_resume.py:131:    """IDLE + unfinished plan → resume_conversation → ok=True."""
packages/agent-server/tests/test_resume.py:138:    await store.append(CID, StatusEvent(status=ConversationStatus.IDLE))
packages/agent-server/tests/test_resume.py:148:    """IDLE with no plan → illegal (no work to resume)."""
packages/agent-server/tests/test_override_persistence.py:180:    # an IDLE (finished) build with a live sandbox → suspend tears it down
packages/agent-server/tests/test_build_surface.py:461:    echo at "sending…" forever while the server stayed IDLE at seq 0."""

exec
/bin/bash -lc 'rg -n "def _emit_persistence_reminder|_emit_persistence_reminder|SystemReminder|system-reminder" packages/agent-server/src/disco/agent_server/runtime.py packages/core/src/disco/core/loop -S' in /var/home/dylan/projects/disco
 succeeded in 0ms:
packages/agent-server/src/disco/agent_server/runtime.py:1406:        FINISHED. Failures are reported as ambient system-reminder events on
packages/agent-server/src/disco/agent_server/runtime.py:1469:            await self._emit_persistence_reminder(
packages/agent-server/src/disco/agent_server/runtime.py:1680:    async def _emit_persistence_reminder(self, conversation_id: str, body: str) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1682:        system-reminder — same pattern as the loop's other gates, so the model
packages/agent-server/src/disco/agent_server/runtime.py:1691:                        "<system-reminder>\n"
packages/agent-server/src/disco/agent_server/runtime.py:1693:                        "</system-reminder>"
packages/core/src/disco/core/loop/recitation.py:195:        contract for harness-injected system-reminders across F4
packages/core/src/disco/core/loop/messages.py:40:    "<system-reminder>\n"
packages/core/src/disco/core/loop/messages.py:45:    "</system-reminder>"
packages/core/src/disco/core/loop/messages.py:52:    "<system-reminder>\n"
packages/core/src/disco/core/loop/messages.py:56:    "</system-reminder>"
packages/core/src/disco/core/loop/messages.py:260:# `<system-reminder>` phrasings selected by attempt count (modulo the pool
packages/core/src/disco/core/loop/messages.py:271:    "<system-reminder>\n"
packages/core/src/disco/core/loop/messages.py:276:    "</system-reminder>",
packages/core/src/disco/core/loop/messages.py:277:    "<system-reminder>\n"
packages/core/src/disco/core/loop/messages.py:282:    "</system-reminder>",
packages/core/src/disco/core/loop/messages.py:283:    "<system-reminder>\n"
packages/core/src/disco/core/loop/messages.py:289:    "</system-reminder>",
packages/core/src/disco/core/loop/engine.py:120:    "<system-reminder>\n"
packages/core/src/disco/core/loop/engine.py:123:    "</system-reminder>"
packages/core/src/disco/core/loop/engine.py:179:# Injected (as an implicit system-reminder) when the planner answers in prose
packages/core/src/disco/core/loop/engine.py:185:    "<system-reminder>\n"
packages/core/src/disco/core/loop/engine.py:196:    "</system-reminder>"
packages/core/src/disco/core/loop/engine.py:212:    "<system-reminder>\n"
packages/core/src/disco/core/loop/engine.py:217:    "</system-reminder>"
packages/core/src/disco/core/loop/engine.py:224:    "<system-reminder>\n"
packages/core/src/disco/core/loop/engine.py:228:    "</system-reminder>"
packages/core/src/disco/core/loop/engine.py:414:# a decision gate (no human in the loop), we emit a system-reminder and
packages/core/src/disco/core/loop/engine.py:953:                        "<system-reminder>\n"
packages/core/src/disco/core/loop/engine.py:957:                        "</system-reminder>"
packages/core/src/disco/core/loop/engine.py:1291:                # then feed back a system-reminder that there's no user and it must
packages/core/src/disco/core/loop/engine.py:1390:        in an implicit `<system-reminder>` — the action did not produce an error,
packages/core/src/disco/core/loop/engine.py:1406:                "<system-reminder>\n"
packages/core/src/disco/core/loop/engine.py:1410:                "</system-reminder>"
packages/core/src/disco/core/loop/engine.py:1497:                                "<system-reminder>\n"
packages/core/src/disco/core/loop/engine.py:1500:                                "</system-reminder>"
packages/core/src/disco/core/loop/plan_conditions.py:52:          * never nudges the agent (no <system-reminder>, no auto-continue,
packages/core/src/disco/core/loop/plan_conditions.py:101:            # The note is a <system-reminder>-less MessageEvent from ENVIRONMENT: it is
packages/core/src/disco/core/loop/plan_conditions.py:103:            # normal message (NOT a system-reminder, so it does NOT nudge — the model is
packages/core/src/disco/core/loop/turn_control.py:80:    "<system-reminder>\n"
packages/core/src/disco/core/loop/turn_control.py:90:    "</system-reminder>"
packages/core/src/disco/core/loop/turn_control.py:192:                                "<system-reminder>\n⚠ finishing was"
packages/core/src/disco/core/loop/turn_control.py:195:                                "\n</system-reminder>"
packages/core/src/disco/core/loop/turn_control.py:210:                                "<system-reminder>\n"
packages/core/src/disco/core/loop/turn_control.py:215:                                "</system-reminder>"
packages/core/src/disco/core/loop/turn_control.py:232:                            "<system-reminder>\n"
packages/core/src/disco/core/loop/turn_control.py:236:                            "</system-reminder>"
packages/core/src/disco/core/loop/turn_control.py:272:        # with assist=ON, emit a one-shot <system-reminder> listing
packages/core/src/disco/core/loop/turn_control.py:398:                    "<system-reminder>\n"
packages/core/src/disco/core/loop/turn_control.py:406:                    "</system-reminder>"
packages/core/src/disco/core/loop/turn_control.py:410:                    "<system-reminder>\n"
packages/core/src/disco/core/loop/turn_control.py:418:                    "</system-reminder>"
packages/core/src/disco/core/loop/turn_control.py:547:                            "<system-reminder>\n"
packages/core/src/disco/core/loop/turn_control.py:554:                            "</system-reminder>"
packages/core/src/disco/core/loop/turn_control.py:772:        #      past the cap → refuse with a system-reminder, no
packages/core/src/disco/core/loop/turn_control.py:798:            # stuck-detector); we emit a system-reminder +
packages/core/src/disco/core/loop/turn_control.py:815:                        "<system-reminder>\n"
packages/core/src/disco/core/loop/turn_control.py:823:                        "</system-reminder>"
packages/core/src/disco/core/loop/turn_control.py:942:                        "<system-reminder>\n"
packages/core/src/disco/core/loop/turn_control.py:949:                        + "\n</system-reminder>"
packages/core/src/disco/core/loop/finish.py:73:# `<system-reminder>` (ambient, implicit) rather than a user-tone scolding: the
packages/core/src/disco/core/loop/finish.py:76:    "<system-reminder>\n"
packages/core/src/disco/core/loop/finish.py:81:    "</system-reminder>"
packages/core/src/disco/core/loop/finish.py:332:                        "<system-reminder>\n"
packages/core/src/disco/core/loop/finish.py:335:                        "</system-reminder>"
packages/core/src/disco/core/loop/finish.py:349:                        "<system-reminder>\n"
packages/core/src/disco/core/loop/finish.py:354:                        "</system-reminder>"
packages/core/src/disco/core/loop/finish.py:421:        Visible: the refusal is a `<system-reminder>` MessageEvent with the
packages/core/src/disco/core/loop/finish.py:491:                        "<system-reminder>\n"
packages/core/src/disco/core/loop/finish.py:500:                        "</system-reminder>"
packages/core/src/disco/core/loop/finish.py:589:                                "<system-reminder>\n"
packages/core/src/disco/core/loop/finish.py:597:                                "</system-reminder>"
packages/core/src/disco/core/loop/finish.py:612:                                "<system-reminder>\n"
packages/core/src/disco/core/loop/finish.py:621:                                "</system-reminder>"
packages/core/src/disco/core/loop/finish.py:643:                                "<system-reminder>\n"
packages/core/src/disco/core/loop/finish.py:651:                                "</system-reminder>"
packages/core/src/disco/core/loop/finish.py:705:        # approval, refuse the finish: append an IMPLICIT system-reminder
packages/core/src/disco/core/loop/finish.py:733:                                "<system-reminder>\n"
packages/core/src/disco/core/loop/finish.py:738:                                "</system-reminder>"
packages/core/src/disco/core/loop/driver.py:283:                # calls it past the cap is refused with a system-reminder,
packages/core/src/disco/core/loop/observe.py:175:        session death the session healed), append an implicit system-reminder
packages/core/src/disco/core/loop/observe.py:292:        had a live instance (gen_before > 0), append an implicit system-reminder
packages/core/src/disco/core/loop/observe.py:306:                        "<system-reminder>\n"
packages/core/src/disco/core/loop/observe.py:312:                        "</system-reminder>"
packages/core/src/disco/core/loop/bootstrap.py:204:    Returns a `<system-reminder>`-formatted string the engine can hand to the
packages/core/src/disco/core/loop/bootstrap.py:248:        "<system-reminder>\n"
packages/core/src/disco/core/loop/bootstrap.py:255:        "</system-reminder>"

exec
/bin/bash -lc "nl -ba packages/agent-server/src/disco/agent_server/runtime.py | sed -n '1545,1590p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
  1545	        return await self._mcp._close_mcp_pool()
  1546	
  1547	    @property
  1548	    def _mcp_call_target(self) -> Any:
  1549	        return self._mcp._mcp_call_target
  1550	
  1551	    def mcp_approval_state(self) -> dict[str, dict]:
  1552	        return self._mcp.mcp_approval_state()
  1553	
  1554	    def on_connect(self, conversation_id: str) -> None:
  1555	        return self._lifecycle.on_connect(conversation_id)
  1556	
  1557	    def on_disconnect(self, conversation_id: str, *, grace_s: float = 60.0) -> None:
  1558	        return self._lifecycle.on_disconnect(conversation_id, grace_s=grace_s)
  1559	
  1560	    async def _suspend(self, conversation_id: str) -> None:
  1561	        return await self._lifecycle._suspend(conversation_id)
  1562	
  1563	    def sandbox_state(self, conversation_id: str) -> str | None:
  1564	        return self._lifecycle.sandbox_state(conversation_id)
  1565	
  1566	    async def sweep_idle_once(self) -> int:
  1567	        return await self._lifecycle.sweep_idle_once()
  1568	
  1569	    async def _idle_sweep_loop(self) -> None:
  1570	        return await self._lifecycle._idle_sweep_loop()
  1571	
  1572	    async def _maybe_run_deep_research(self, conversation_id: str) -> None:
  1573	        return await self._dr._maybe_run_deep_research(conversation_id)
  1574	
  1575	    async def _propose_deep_research_plan(self, conversation_id: str, events: list) -> None:
  1576	        return await self._dr._propose_deep_research_plan(conversation_id, events)
  1577	
  1578	    async def _execute_deep_research(
  1579	        self,
  1580	        conversation_id: str,
  1581	        plan: PlanEvent,
  1582	        *,
  1583	        resume_from: ReportEvent | None = None,
  1584	    ) -> None:
  1585	        return await self._dr._execute_deep_research(
  1586	            conversation_id, plan, resume_from=resume_from
  1587	        )
  1588	
  1589	    def title_service(self) -> TitleService:
  1590	        """Public accessor for the auto-titler (used by the projects backfill route)."""

codex
No test currently waits on the terminalization task itself or forces storage/reminder failures, so this area gives some false confidence. I’ll keep that as a coverage/failure-mode note unless I find a direct pass-when-fail path.
exec
/bin/bash -lc "nl -ba packages/agent-server/src/disco/agent_server/runtime.py | sed -n '1675,1705p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
  1675	        return await self._lifecycle._rematerialize_uploads(conversation_id)
  1676	
  1677	    async def _maybe_snapshot(self, conversation_id: str) -> None:
  1678	        return await self._lifecycle._maybe_snapshot(conversation_id)
  1679	
  1680	    async def _emit_persistence_reminder(self, conversation_id: str, body: str) -> None:
  1681	        """Surface a project-persistence problem on the event log as an implicit
  1682	        system-reminder — same pattern as the loop's other gates, so the model
  1683	        and the UI both see what went wrong, named."""
  1684	        await self._store.append(
  1685	            conversation_id,
  1686	            MessageEvent(
  1687	                source=EventSource.ENVIRONMENT,
  1688	                message=LLMMessage(
  1689	                    role="user",
  1690	                    content=(
  1691	                        "<system-reminder>\n"
  1692	                        f"Project persistence note: {body}\n"
  1693	                        "</system-reminder>"
  1694	                    ),
  1695	                ),
  1696	            ),
  1697	        )
  1698	
  1699	    def resolve_cid_prefix(self, cid8: str) -> str | None:
  1700	        return self._preview.resolve_cid_prefix(cid8)
  1701	
  1702	    def preview_upstream(self, conversation_id: str) -> str | None:
  1703	        """The URL the AGENT-SERVER can reach the conversation's dev server at (the backend
  1704	        owns how — localhost for local, the remote host's tailnet IP for gVisor). The
  1705	        browser never touches this; the agent-server proxies it (single origin)."""

codex
Next I’m switching to the artifact side: procedural image prevention and deck defaults. Here I’m looking for brittle detection, descriptor/raw-file mismatches, and whether tests validate the shipped bytes rather than just the summary object.
exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/builtin/_slides_pipeline.py | sed -n '455,520p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   455	    messages.append({"role": "user", "content": _retry_msg(err)})
   456	    try:
   457	        raw2 = await _call_llm(messages, llm_url, model, api_key=api_key)
   458	    except Exception as e:  # noqa: BLE001
   459	        return None, f"Fill retry failed: {e}"
   460	
   461	    deck2, err2 = _parse_authored_deck(raw2)
   462	    if deck2 is not None:
   463	        return deck2, ""
   464	    return None, f"Fill parse failed after retry: {err2}"
   465	
   466	
   467	# ---------------------------------------------------------------------------
   468	# Stage 3: Asset generation (image_prompt → image bytes)
   469	# ---------------------------------------------------------------------------
   470	
   471	
   472	async def _stage_assets(
   473	    authored: AuthoredDeck,
   474	    ctx: ToolContext,
   475	    backend: ImageBackend,
   476	    filename_base: str,
   477	) -> dict[int, bytes]:
   478	    """For each slide with image_prompt, generate the image, write it to the sandbox
   479	    as a standalone artifact, AND return {authored_slide_index: image_bytes}.
   480	
   481	    The returned map is handed to ``lower_deck(image_assets=...)`` so the generated
   482	    picture is embedded directly into the rendered deck (C7 wire) — previously the
   483	    bytes were written to disk but never reached the renderer, so every image slide
   484	    fell back to the ``[image]`` placeholder. The on-disk copy is kept too (a
   485	    browsable artifact + back-compat for any path-based consumer).
   486	    """
   487	    import hashlib
   488	
   489	    assets: dict[int, bytes] = {}
   490	    # W3: never embed PROCEDURAL placeholder art into a deliverable deck. The keyless
   491	    # default backend draws abstract test-pattern graphics (nested rectangles + a diagonal)
   492	    # — honest for an "image-gen as a tool" demo, garish in a finished presentation. With no
   493	    # real image provider configured, omit images (text-only slides) instead of shipping
   494	    # placeholders. Configure ComfyUI/OpenAI/OpenRouter to include real images.
   495	    if getattr(backend, "name", "") == "pil-procedural":
   496	        _LOG.info("image provider is the procedural placeholder — omitting deck images")
   497	        return assets
   498	    for i, slide in enumerate(authored.slides):
   499	        if not slide.image_prompt:
   500	            continue
   501	        img_name = f"{filename_base}_img_{i}.png"
   502	        h = hashlib.sha256(slide.image_prompt.encode("utf-8")).digest()
   503	        seed = int.from_bytes(h[:4], "big", signed=False)
   504	
   505	        try:
   506	            img_bytes = backend.generate(
   507	                prompt=slide.image_prompt,
   508	                # 16:9 landscape, both dims >= the ComfyUI 512 floor so a real
   509	                # diffusion backend doesn't snap 384 up to 1024 and hand back a
   510	                # distorted portrait for a slide that wants a wide image.
   511	                width=1024,
   512	                height=576,
   513	                seed=seed,
   514	                fmt="png",
   515	            )
   516	            # Only embed real raster bytes — a backend that returns something other
   517	            # than PNG/JPEG (mis-config, error blob) must fall back to the [image]
   518	            # placeholder, not a broken data-URI / corrupt add_picture.
   519	            if not (img_bytes.startswith(b"\x89PNG\r\n\x1a\n") or img_bytes[:3] == b"\xff\xd8\xff"):
   520	                _LOG.warning("Slide %d image is not PNG/JPEG — skipping embed", i)

exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/builtin/slides.py | sed -n '1,140p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""Slide generation tool — writes rendered slide decks (HTML/PDF/PPTX) to
     2	the workspace.
     3	
     4	Primary path (C2): when a ``goal`` is supplied, the staged C2 pipeline
     5	(outline → fill → assets → lower_deck → C3 render) generates a structured
     6	AuthoredDeck and renders it to native editable .pptx / brand HTML.  On parse
     7	failure after one retry the Marp fallback is taken automatically — no crash.
     8	
     9	Marp fallback: when only ``markdown`` is supplied, or when C2 fails, the
    10	tool falls back to Marp CLI (or the pure-HTML fallback when Marp is absent).
    11	
    12	Format support:
    13	  - html: always works (C3 brand HTML, or Marp CLI, or self-contained fallback).
    14	  - pdf: requires marp + Chromium or LibreOffice in the sandbox image.
    15	  - pptx: C3 native editable PPTX (real text boxes) via python-pptx; or
    16	          Marp --pptx (image-based) when the C2 path is not used.
    17	"""
    18	
    19	from __future__ import annotations
    20	
    21	import html
    22	import re
    23	import shlex
    24	from textwrap import dedent
    25	from typing import Literal
    26	
    27	from pydantic import BaseModel, Field
    28	
    29	from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
    30	from .image_gen import select_image_backend
    31	
    32	# ---- args model --------------------------------------------------------------
    33	
    34	
    35	class SlidesGenerateArgs(BaseModel):
    36	    """Arguments for the slides_generate tool."""
    37	
    38	    goal: str | None = Field(
    39	        default=None,
    40	        description=(
    41	            "Natural-language description of the desired slide deck (e.g. "
    42	            "'A 6-slide investor pitch for an EV battery startup'). "
    43	            "When provided, the C2 structured pipeline generates the deck "
    44	            "automatically via outline → fill → render stages. "
    45	            "Takes priority over ``markdown`` when both are supplied."
    46	        ),
    47	    )
    48	    markdown: str = Field(
    49	        default="",
    50	        description=(
    51	            "Marp-compatible markdown source for the slide deck (backward-compat "
    52	            "path). Use CommonMark with '---' (three dashes on their own line) to "
    53	            "separate slides. Ignored when ``goal`` is supplied."
    54	        ),
    55	    )
    56	    filename: str = Field(
    57	        description=(
    58	            "Base filename WITHOUT extension (e.g. 'my-deck'). The tool appends "
    59	            ".html, .pdf, or .pptx based on the format argument."
    60	        )
    61	    )
    62	    format: str = Field(
    63	        default="pptx",
    64	        description=(
    65	            "Output format: 'pptx' (default — a presentable, editable deck), 'pdf', or "
    66	            "'html'. Prefer 'pptx' for a deliverable the user opens/presents; 'html' is a "
    67	            "self-contained web deck."
    68	        ),
    69	    )
    70	    theme: str | None = Field(
    71	        default=None,
    72	        description="Optional Marp theme CSS to include inline (e.g. custom colors, fonts).",
    73	    )
    74	    slide_count: int = Field(
    75	        default=5,
    76	        ge=2,
    77	        le=30,
    78	        description="Approximate number of slides (used by the C2 pipeline; ignored for markdown path).",
    79	    )
    80	    mode: Literal["deck", "markdown"] = Field(
    81	        default="deck",
    82	        description=(
    83	            "'deck' uses the C2 structured pipeline (default when goal supplied). "
    84	            "'markdown' forces the Marp/fallback path regardless of goal."
    85	        ),
    86	    )
    87	
    88	
    89	# ---- HTML fallback (used when marp CLI is unavailable) -----------------------
    90	
    91	_MARP_MINIMAL_THEME = dedent("""\
    92	/* Minimal built-in theme fallback */
    93	section {
    94	  font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    95	  font-size: 1.5em;
    96	  padding: 2em;
    97	  background: #fff;
    98	  color: #222;
    99	}
   100	h1 { font-size: 2em; margin-bottom: 0.3em; }
   101	h2 { font-size: 1.6em; margin-bottom: 0.3em; }
   102	h3 { font-size: 1.3em; margin-bottom: 0.2em; }
   103	code { background: #f0f0f0; padding: 0.1em 0.3em; border-radius: 3px; font-size: 0.85em; }
   104	pre { background: #f0f0f0; padding: 1em; border-radius: 6px; overflow-x: auto; }
   105	pre code { background: none; padding: 0; }
   106	ul, ol { text-align: left; padding-left: 1.5em; }
   107	li { margin-bottom: 0.3em; }
   108	blockquote {
   109	  border-left: 4px solid #ccc; margin: 0.5em 0; padding: 0.3em 1em;
   110	  color: #555; font-style: italic;
   111	}
   112	table { border-collapse: collapse; margin: 0.5em auto; }
   113	th, td { border: 1px solid #ddd; padding: 0.4em 0.8em; text-align: left; }
   114	th { background: #f5f5f5; }
   115	img { max-width: 100%; height: auto; }
   116	a { color: #0366d6; }
   117	""")
   118	
   119	
   120	# Simple inline markup patterns applied BEFORE the html.escape pass.
   121	# Order: protect code spans/spans first (they may contain other tokens),
   122	# then block-level, then inline.
   123	_FENCE_RE = re.compile(r"```(.*?)\n(.*?)```", re.DOTALL)
   124	_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
   125	_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
   126	_ITALIC_RE = re.compile(r"\*(.+?)\*")
   127	_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
   128	_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
   129	
   130	
   131	def _inline_markdown_to_html(text: str) -> str:
   132	    """Convert inline markdown (bold, italic, code, links, images) to HTML.
   133	    Safe to apply on already-HTML-escaped text — the regexes match literal
   134	    markdown tokens that survive escaping."""
   135	    text = _IMAGE_RE.sub(r'<img src="\2" alt="\1">', text)
   136	    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)
   137	    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
   138	    text = _ITALIC_RE.sub(r"<em>\1</em>", text)
   139	    text = _INLINE_CODE_RE.sub(r"<code>\1</code>", text)
   140	    return text

exec
/bin/bash -lc 'rg -n "format|html|pptx|pil-procedural|_stage_assets|slides" packages/tools/src/disco/tools/builtin/slides.py packages/tools/src/disco/tools/builtin/_slides_pipeline.py packages/tools/tests/test_slides_procedural_contract.py packages/tools/tests/test_slides.py packages/tools/tests/test_pptx_render.py' in /var/home/dylan/projects/disco
 succeeded in 0ms:
packages/tools/tests/test_pptx_render.py:1:"""Tests for C3 — native editable PPTX renderer (_pptx_render.py).
packages/tools/tests/test_pptx_render.py:4:  - render_pptx: the .pptx round-trips via python-pptx and contains REAL text
packages/tools/tests/test_pptx_render.py:6:  - render_html: the output is valid 16:9 HTML with one <section> per slide,
packages/tools/tests/test_pptx_render.py:9:  - render_deck: returns both pptx_bytes and html_str.
packages/tools/tests/test_pptx_render.py:10:  - Binary round-trip: pptx_bytes are valid ZIP/OOXML (magic bytes 50 4B).
packages/tools/tests/test_pptx_render.py:20:from disco.tools.builtin._pptx_render import (
packages/tools/tests/test_pptx_render.py:25:    render_html,
packages/tools/tests/test_pptx_render.py:26:    render_pptx,
packages/tools/tests/test_pptx_render.py:39:        slides=[
packages/tools/tests/test_pptx_render.py:71:        slides=[
packages/tools/tests/test_pptx_render.py:83:        slides=[
packages/tools/tests/test_pptx_render.py:94:def test_pptx_binary_magic_bytes():
packages/tools/tests/test_pptx_render.py:95:    """The pptx_bytes start with the ZIP magic bytes PK (50 4B 03 04)."""
packages/tools/tests/test_pptx_render.py:97:    data = render_pptx(deck)
packages/tools/tests/test_pptx_render.py:102:def test_pptx_round_trips_via_python_pptx():
packages/tools/tests/test_pptx_render.py:103:    """python-pptx can re-open the rendered bytes without error."""
packages/tools/tests/test_pptx_render.py:106:    from pptx import Presentation
packages/tools/tests/test_pptx_render.py:109:    data = render_pptx(deck)
packages/tools/tests/test_pptx_render.py:112:    assert len(prs.slides) == len(deck.slides)
packages/tools/tests/test_pptx_render.py:115:def test_pptx_contains_real_text_runs():
packages/tools/tests/test_pptx_render.py:116:    """The .pptx contains real text runs — NOT a single embedded image per slide.
packages/tools/tests/test_pptx_render.py:123:    from pptx import Presentation
packages/tools/tests/test_pptx_render.py:124:    from pptx.shapes.picture import Picture
packages/tools/tests/test_pptx_render.py:127:    data = render_pptx(deck)
packages/tools/tests/test_pptx_render.py:130:    for i, (pptx_slide, deck_slide) in enumerate(zip(prs.slides, deck.slides)):
packages/tools/tests/test_pptx_render.py:134:        for shape in pptx_slide.shapes:
packages/tools/tests/test_pptx_render.py:153:def test_pptx_16_9_canvas():
packages/tools/tests/test_pptx_render.py:157:    from pptx import Presentation
packages/tools/tests/test_pptx_render.py:160:    data = render_pptx(deck)
packages/tools/tests/test_pptx_render.py:166:def test_pptx_bullets_slide_has_bullet_text():
packages/tools/tests/test_pptx_render.py:170:    from pptx import Presentation
packages/tools/tests/test_pptx_render.py:174:        slides=[
packages/tools/tests/test_pptx_render.py:182:    data = render_pptx(deck)
packages/tools/tests/test_pptx_render.py:184:    slide = prs.slides[0]
packages/tools/tests/test_pptx_render.py:192:def test_pptx_title_layout():
packages/tools/tests/test_pptx_render.py:196:    from pptx import Presentation
packages/tools/tests/test_pptx_render.py:200:        slides=[
packages/tools/tests/test_pptx_render.py:208:    data = render_pptx(deck)
packages/tools/tests/test_pptx_render.py:210:    slide = prs.slides[0]
packages/tools/tests/test_pptx_render.py:218:def test_pptx_section_layout():
packages/tools/tests/test_pptx_render.py:222:    from pptx import Presentation
packages/tools/tests/test_pptx_render.py:226:        slides=[
packages/tools/tests/test_pptx_render.py:230:    data = render_pptx(deck)
packages/tools/tests/test_pptx_render.py:232:    slide = prs.slides[0]
packages/tools/tests/test_pptx_render.py:239:def test_pptx_dark_theme():
packages/tools/tests/test_pptx_render.py:243:    from pptx import Presentation
packages/tools/tests/test_pptx_render.py:245:    data = render_pptx(_dark_deck())
packages/tools/tests/test_pptx_render.py:247:    assert len(prs.slides) == 1
packages/tools/tests/test_pptx_render.py:250:def test_pptx_neutral_theme():
packages/tools/tests/test_pptx_render.py:254:    from pptx import Presentation
packages/tools/tests/test_pptx_render.py:256:    data = render_pptx(_neutral_deck())
packages/tools/tests/test_pptx_render.py:258:    assert len(prs.slides) == 1
packages/tools/tests/test_pptx_render.py:261:def test_pptx_with_notes():
packages/tools/tests/test_pptx_render.py:265:    from pptx import Presentation
packages/tools/tests/test_pptx_render.py:269:        slides=[
packages/tools/tests/test_pptx_render.py:278:    data = render_pptx(deck)
packages/tools/tests/test_pptx_render.py:280:    notes_text = prs.slides[0].notes_slide.notes_text_frame.text
packages/tools/tests/test_pptx_render.py:284:def test_pptx_empty_deck():
packages/tools/tests/test_pptx_render.py:285:    """An empty deck (no slides) produces valid 0-slide PPTX."""
packages/tools/tests/test_pptx_render.py:288:    from pptx import Presentation
packages/tools/tests/test_pptx_render.py:290:    deck = MinimalDeck(title="Empty", slides=[])
packages/tools/tests/test_pptx_render.py:291:    data = render_pptx(deck)
packages/tools/tests/test_pptx_render.py:293:    assert len(prs.slides) == 0
packages/tools/tests/test_pptx_render.py:301:def test_html_is_string():
packages/tools/tests/test_pptx_render.py:302:    """render_html returns a string."""
packages/tools/tests/test_pptx_render.py:303:    html_str = render_html(_sample_deck())
packages/tools/tests/test_pptx_render.py:304:    assert isinstance(html_str, str)
packages/tools/tests/test_pptx_render.py:307:def test_html_one_section_per_slide():
packages/tools/tests/test_pptx_render.py:310:    html_str = render_html(deck)
packages/tools/tests/test_pptx_render.py:311:    count = html_str.count('<section class="slide')
packages/tools/tests/test_pptx_render.py:312:    assert count == len(deck.slides), (
packages/tools/tests/test_pptx_render.py:313:        f"Expected {len(deck.slides)} slide sections, found {count}"
packages/tools/tests/test_pptx_render.py:317:def test_html_16_9_ratio():
packages/tools/tests/test_pptx_render.py:319:    html_str = render_html(_sample_deck())
packages/tools/tests/test_pptx_render.py:320:    assert "56.25vw" in html_str, "Missing 16:9 viewport-unit height in CSS"
packages/tools/tests/test_pptx_render.py:323:def test_html_contains_title_text():
packages/tools/tests/test_pptx_render.py:326:    html_str = render_html(deck)
packages/tools/tests/test_pptx_render.py:327:    for slide in deck.slides:
packages/tools/tests/test_pptx_render.py:328:        assert slide.title in html_str, (
packages/tools/tests/test_pptx_render.py:333:def test_html_has_keyboard_nav_script():
packages/tools/tests/test_pptx_render.py:335:    html_str = render_html(_sample_deck())
packages/tools/tests/test_pptx_render.py:336:    assert "ArrowRight" in html_str
packages/tools/tests/test_pptx_render.py:337:    assert "ArrowLeft" in html_str
packages/tools/tests/test_pptx_render.py:340:def test_html_has_font_face_declarations():
packages/tools/tests/test_pptx_render.py:342:    html_str = render_html(_sample_deck())
packages/tools/tests/test_pptx_render.py:344:    assert "@font-face" in html_str
packages/tools/tests/test_pptx_render.py:345:    assert "Fraunces" in html_str
packages/tools/tests/test_pptx_render.py:346:    assert "Schibsted Grotesk" in html_str
packages/tools/tests/test_pptx_render.py:347:    assert "Newsreader" in html_str
packages/tools/tests/test_pptx_render.py:350:def test_html_has_brand_css_vars():
packages/tools/tests/test_pptx_render.py:352:    html_str = render_html(_sample_deck())
packages/tools/tests/test_pptx_render.py:353:    assert "--bg:" in html_str
packages/tools/tests/test_pptx_render.py:354:    assert "--accent:" in html_str
packages/tools/tests/test_pptx_render.py:355:    assert "--text:" in html_str
packages/tools/tests/test_pptx_render.py:358:def test_html_first_slide_is_active():
packages/tools/tests/test_pptx_render.py:360:    html_str = render_html(_sample_deck())
packages/tools/tests/test_pptx_render.py:362:    first_active = html_str.find('class="slide active"')
packages/tools/tests/test_pptx_render.py:365:    assert html_str.count('class="slide active"') == 1
packages/tools/tests/test_pptx_render.py:368:def test_html_escapes_special_chars():
packages/tools/tests/test_pptx_render.py:372:        slides=[
packages/tools/tests/test_pptx_render.py:380:    html_str = render_html(deck)
packages/tools/tests/test_pptx_render.py:382:    assert "<Script>" not in html_str
packages/tools/tests/test_pptx_render.py:383:    assert "<b>bold" not in html_str
packages/tools/tests/test_pptx_render.py:385:    assert "&lt;Script&gt;" in html_str
packages/tools/tests/test_pptx_render.py:386:    assert "&lt;b&gt;" in html_str
packages/tools/tests/test_pptx_render.py:389:def test_html_dark_theme_bg():
packages/tools/tests/test_pptx_render.py:391:    html_str = render_html(_dark_deck())
packages/tools/tests/test_pptx_render.py:393:    assert "#0e0f12" in html_str
packages/tools/tests/test_pptx_render.py:409:    ok, err = await convert_to_pdf(ctx, "deck.pptx")
packages/tools/tests/test_pptx_render.py:425:    ok, err = await convert_to_pdf(ctx, "deck.pptx")
packages/tools/tests/test_pptx_render.py:443:    ok, err = await convert_to_pdf(ctx, "deck.pptx")
packages/tools/tests/test_pptx_render.py:447:    # Second call should include soffice + pptx name
packages/tools/tests/test_pptx_render.py:450:    assert "deck.pptx" in str(convert_call)
packages/tools/tests/test_pptx_render.py:461:    ok, err = await convert_to_pdf(ctx, "deck.pptx")
packages/tools/tests/test_pptx_render.py:476:    ok, err = await convert_to_pdf(ctx, "deck.pptx")
packages/tools/tests/test_pptx_render.py:488:    """render_deck returns pptx_bytes (bytes) and html_str (str)."""
packages/tools/tests/test_pptx_render.py:490:    assert "pptx_bytes" in result
packages/tools/tests/test_pptx_render.py:491:    assert "html_str" in result
packages/tools/tests/test_pptx_render.py:492:    assert isinstance(result["pptx_bytes"], bytes)
packages/tools/tests/test_pptx_render.py:493:    assert isinstance(result["html_str"], str)
packages/tools/tests/test_pptx_render.py:496:def test_render_deck_pptx_is_valid():
packages/tools/tests/test_pptx_render.py:497:    """render_deck pptx_bytes are a valid OOXML file."""
packages/tools/tests/test_pptx_render.py:500:    from pptx import Presentation
packages/tools/tests/test_pptx_render.py:503:    prs = Presentation(io.BytesIO(result["pptx_bytes"]))
packages/tools/tests/test_pptx_render.py:504:    assert len(prs.slides) == len(_sample_deck().slides)
packages/tools/tests/test_pptx_render.py:507:def test_render_deck_html_is_16_9():
packages/tools/tests/test_pptx_render.py:508:    """render_deck html_str is a 16:9 HTML deck."""
packages/tools/tests/test_pptx_render.py:510:    assert "56.25vw" in result["html_str"]
packages/tools/tests/test_pptx_render.py:530:        slides=[
packages/tools/tests/test_pptx_render.py:538:def _pptx_slide_texts(pptx_bytes: bytes) -> list[list[str]]:
packages/tools/tests/test_pptx_render.py:541:    from pptx import Presentation
packages/tools/tests/test_pptx_render.py:543:    prs = Presentation(io.BytesIO(pptx_bytes))
packages/tools/tests/test_pptx_render.py:545:    for s in prs.slides:
packages/tools/tests/test_pptx_render.py:550:def test_pptx_wordmark_on_every_branded_slide():
packages/tools/tests/test_pptx_render.py:552:    texts = _pptx_slide_texts(render_pptx(lower_deck(_branded_deck())))
packages/tools/tests/test_pptx_render.py:558:def test_pptx_colophon_only_on_cover():
packages/tools/tests/test_pptx_render.py:560:    texts = _pptx_slide_texts(render_pptx(lower_deck(_branded_deck())))
packages/tools/tests/test_pptx_render.py:568:def test_pptx_no_brand_marks_when_unbranded():
packages/tools/tests/test_pptx_render.py:570:    texts = _pptx_slide_texts(render_pptx(lower_deck(_branded_deck(theme="neutral"))))
packages/tools/tests/test_pptx_render.py:576:def test_html_brand_marks_branded_vs_neutral():
packages/tools/tests/test_pptx_render.py:579:    html_branded = render_html(lower_deck(_branded_deck()))
packages/tools/tests/test_pptx_render.py:580:    # one wordmark div per slide (3 slides); match the element, not the CSS rule
packages/tools/tests/test_pptx_render.py:581:    assert html_branded.count('class="brand-wordmark"') == 3
packages/tools/tests/test_pptx_render.py:583:    assert html_branded.count('class="brand-colophon"') == 1
packages/tools/tests/test_pptx_render.py:584:    assert "I become acquainted with" in html_branded
packages/tools/tests/test_pptx_render.py:586:    html_neutral = render_html(lower_deck(_branded_deck(theme="neutral")))
packages/tools/tests/test_pptx_render.py:587:    assert 'class="brand-wordmark"' not in html_neutral
packages/tools/tests/test_pptx_render.py:588:    assert 'class="brand-colophon"' not in html_neutral
packages/tools/tests/test_pptx_render.py:591:def test_html_brand_chrome_is_pointer_events_none():
packages/tools/tests/test_pptx_render.py:595:    css = render_html(lower_deck(_branded_deck()))
packages/tools/tests/test_pptx_render.py:610:    Image.new("RGB", (32, 24), (123, 200, 90)).save(buf, format="PNG")
packages/tools/tests/test_pptx_render.py:614:def test_lower_deck_embeds_image_assets_into_pptx_and_html():
packages/tools/tests/test_pptx_render.py:616:    element so render_pptx EMBEDS a real picture (ppt/media non-empty) and render_html
packages/tools/tests/test_pptx_render.py:621:    from disco.tools.builtin._pptx_render import render_html, render_pptx
packages/tools/tests/test_pptx_render.py:627:        slides=[
packages/tools/tests/test_pptx_render.py:636:    assert all(el.image_bytes is None for s in deck_plain.slides for el in s.elements)
packages/tools/tests/test_pptx_render.py:637:    assert "[image]" in render_html(deck_plain)
packages/tools/tests/test_pptx_render.py:641:    img_els = [el for s in deck.slides for el in s.elements if el.kind == "image"]
packages/tools/tests/test_pptx_render.py:644:    pptx = render_pptx(deck)
packages/tools/tests/test_pptx_render.py:645:    with zipfile.ZipFile(__import__("io").BytesIO(pptx)) as z:
packages/tools/tests/test_pptx_render.py:647:    assert media, "render_pptx embedded NO media — the image did not make it into the deck"
packages/tools/tests/test_pptx_render.py:649:    html_out = render_html(deck)
packages/tools/tests/test_pptx_render.py:650:    assert "data:image/png;base64," in html_out, "render_html did not inline the image bytes"
packages/tools/tests/test_pptx_render.py:658:                            slides=[AuthoredSlide(type="full_image", title="C", body=[],
packages/tools/tests/test_pptx_render.py:661:    assert all(el.image_bytes is None for s in deck.slides for el in s.elements)
packages/tools/tests/test_slides_procedural_contract.py:1:"""W3 — slides/artifact contract.
packages/tools/tests/test_slides_procedural_contract.py:3:The runthru bug: a "make slides" request shipped a deck whose image slots rendered as
packages/tools/tests/test_slides_procedural_contract.py:6:to the keyless ``pil-procedural`` backend. A presentable deck must NOT embed procedural
packages/tools/tests/test_slides_procedural_contract.py:8:the slides tool default format must be a presentable deck (``pptx``), not raw ``html``.
packages/tools/tests/test_slides_procedural_contract.py:16:from disco.tools.builtin._slides_pipeline import _stage_assets
packages/tools/tests/test_slides_procedural_contract.py:25:        slides=[
packages/tools/tests/test_slides_procedural_contract.py:52:    assets = await _stage_assets(_deck_with_image(), ctx, _PILProceduralBackend(), "f")
packages/tools/tests/test_slides_procedural_contract.py:59:    assets = await _stage_assets(_deck_with_image(), ctx, _RealBackend(), "f")
packages/tools/tests/test_slides_procedural_contract.py:64:def test_slides_default_format_is_pptx():
packages/tools/tests/test_slides_procedural_contract.py:65:    """The slides tool must default to a presentable deck format, not raw html."""
packages/tools/tests/test_slides_procedural_contract.py:66:    from disco.tools.builtin.slides import SlidesGenerateArgs
packages/tools/tests/test_slides_procedural_contract.py:68:    assert SlidesGenerateArgs.model_fields["format"].default == "pptx"
packages/tools/tests/test_slides.py:1:"""Unit tests for the slides_generate tool.
packages/tools/tests/test_slides.py:4:  - markdown → slides split (3 '---'-separated slides → 3 slides in output).
packages/tools/tests/test_slides.py:9:  - Format routing: invalid format → clean failure.
packages/tools/tests/test_slides.py:28:from disco.tools.builtin.slides import (
packages/tools/tests/test_slides.py:31:    _split_slides,
packages/tools/tests/test_slides.py:93:def test_split_slides_basic():
packages/tools/tests/test_slides.py:94:    """Three '---'-separated slides → 3 slides."""
packages/tools/tests/test_slides.py:95:    slides = _split_slides(_THREE_SLIDE_MD)
packages/tools/tests/test_slides.py:96:    assert len(slides) == 3
packages/tools/tests/test_slides.py:97:    assert slides[0].startswith("# Slide 1")
packages/tools/tests/test_slides.py:98:    assert slides[1].startswith("## Slide 2")
packages/tools/tests/test_slides.py:99:    assert slides[2].startswith("# Slide 3")
packages/tools/tests/test_slides.py:102:def test_split_slides_single():
packages/tools/tests/test_slides.py:104:    slides = _split_slides("# Only Slide\n\nSome content.")
packages/tools/tests/test_slides.py:105:    assert len(slides) == 1
packages/tools/tests/test_slides.py:108:def test_split_slides_crlf():
packages/tools/tests/test_slides.py:111:    slides = _split_slides(md)
packages/tools/tests/test_slides.py:112:    assert len(slides) == 2
packages/tools/tests/test_slides.py:115:def test_split_slides_empty_filtered():
packages/tools/tests/test_slides.py:116:    """Empty slides from leading/trailing separators are filtered."""
packages/tools/tests/test_slides.py:118:    slides = _split_slides(md)
packages/tools/tests/test_slides.py:119:    assert len(slides) == 1
packages/tools/tests/test_slides.py:120:    assert "# Real" in slides[0]
packages/tools/tests/test_slides.py:126:async def test_html_fallback_writes_artifact(tmp_workspace):
packages/tools/tests/test_slides.py:132:    with patch("disco.tools.builtin.slides._marp_available", return_value=False):
packages/tools/tests/test_slides.py:137:                format="html",
packages/tools/tests/test_slides.py:143:    assert outcome.artifacts == ["test-deck.html"]
packages/tools/tests/test_slides.py:146:    file_path = tmp_workspace / "test-deck.html"
packages/tools/tests/test_slides.py:151:    assert "<!DOCTYPE html>" in content
packages/tools/tests/test_slides.py:153:    # Three slides
packages/tools/tests/test_slides.py:164:async def test_html_fallback_with_theme(tmp_workspace):
packages/tools/tests/test_slides.py:171:    with patch("disco.tools.builtin.slides._marp_available", return_value=False):
packages/tools/tests/test_slides.py:176:                format="html",
packages/tools/tests/test_slides.py:183:    content = (tmp_workspace / "themed.html").read_text("utf-8")
packages/tools/tests/test_slides.py:187:async def test_html_fallback_slide_count_in_result(tmp_workspace):
packages/tools/tests/test_slides.py:192:    with patch("disco.tools.builtin.slides._marp_available", return_value=False):
packages/tools/tests/test_slides.py:197:                format="html",
packages/tools/tests/test_slides.py:207:# ---- format routing ----------------------------------------------------------
packages/tools/tests/test_slides.py:210:async def test_invalid_format_rejected(tmp_workspace):
packages/tools/tests/test_slides.py:211:    """Unknown format returns a clean failure."""
packages/tools/tests/test_slides.py:219:            format="docx",
packages/tools/tests/test_slides.py:225:    assert "Unsupported format" in outcome.content
packages/tools/tests/test_slides.py:239:    with patch("disco.tools.builtin.slides._marp_available", return_value=False):
packages/tools/tests/test_slides.py:244:                format="pdf",
packages/tools/tests/test_slides.py:253:    assert "sandbox" in outcome.content.lower() and "html" in outcome.content.lower()
packages/tools/tests/test_slides.py:259:async def test_pptx_fails_when_marp_absent(tmp_workspace):
packages/tools/tests/test_slides.py:264:    with patch("disco.tools.builtin.slides._marp_available", return_value=False):
packages/tools/tests/test_slides.py:269:                format="pptx",
packages/tools/tests/test_slides.py:276:    assert "html" in outcome.content.lower()  # suggests HTML fallback
packages/tools/tests/test_slides.py:278:    assert not (tmp_workspace / "deck.pptx").exists()
packages/tools/tests/test_slides.py:295:    escape_target = tmp_workspace.parent / "escape.html"
packages/tools/tests/test_slides.py:300:            "slides_generate",
packages/tools/tests/test_slides.py:303:            format="html",
packages/tools/tests/test_slides.py:319:    with patch("disco.tools.builtin.slides._marp_available", return_value=False):
packages/tools/tests/test_slides.py:324:                format="html",
packages/tools/tests/test_slides.py:330:    assert outcome.artifacts == ["host-cwd-test.html"]
packages/tools/tests/test_slides.py:333:    assert (tmp_workspace / "host-cwd-test.html").exists(), (
packages/tools/tests/test_slides.py:337:    cwd_file = Path.cwd() / "host-cwd-test.html"
packages/tools/tests/test_slides.py:347:    """slides_generate is in the default registry AND in agent scope."""
packages/tools/tests/test_slides.py:350:    assert "slides_generate" in reg.names()
packages/tools/tests/test_slides.py:351:    assert "slides_generate" in agt_scope.allowed_tools
packages/tools/tests/test_slides.py:353:    tool = reg.get("slides_generate", scope=agt_scope)
packages/tools/tests/test_slides.py:355:    assert tool.definition.name == "slides_generate"
packages/tools/tests/test_slides.py:358:async def test_executor_rejects_slides_in_research_scope():
packages/tools/tests/test_slides.py:359:    """slides_generate is agent-scope only, not research."""
packages/tools/tests/test_slides.py:364:            "slides_generate",
packages/tools/tests/test_slides.py:367:            format="html",
packages/tools/tests/test_slides.py:378:    """slides_generate must declare runs_in='sandbox' to enforce jail."""
packages/tools/src/disco/tools/builtin/_slides_pipeline.py:4:  1. Outline (LLM): Generate {type, title} for all slides (structural skeleton).
packages/tools/src/disco/tools/builtin/_slides_pipeline.py:8:  5. Render:       Deck → .pptx + .html via C3.
packages/tools/src/disco/tools/builtin/_slides_pipeline.py:44:_LOG = logging.getLogger("disco.tools.slides_pipeline")
packages/tools/src/disco/tools/builtin/_slides_pipeline.py:57:def _resolve_slides_llm() -> tuple[str, str, str | None]:
packages/tools/src/disco/tools/builtin/_slides_pipeline.py:58:    """Return (base_url, model_id, api_key) for the slides generation LLM.
packages/tools/src/disco/tools/builtin/_slides_pipeline.py:137:  "slides": [AuthoredSlide, ...]
packages/tools/src/disco/tools/builtin/_slides_pipeline.py:176:  "slides": [
packages/tools/src/disco/tools/builtin/_slides_pipeline.py:221:Generate an outline of {n} slides for this deck.
packages/tools/src/disco/tools/builtin/_slides_pipeline.py:394:        {"role": "user", "content": _OUTLINE_USER_TMPL.format(
packages/tools/src/disco/tools/builtin/_slides_pipeline.py:441:        {"role": "user", "content": _FILL_USER_TMPL.format(outline_json=outline_json)},
packages/tools/src/disco/tools/builtin/_slides_pipeline.py:472:async def _stage_assets(
packages/tools/src/disco/tools/builtin/_slides_pipeline.py:493:    # real image provider configured, omit images (text-only slides) instead of shipping
packages/tools/src/disco/tools/builtin/_slides_pipeline.py:495:    if getattr(backend, "name", "") == "pil-procedural":
packages/tools/src/disco/tools/builtin/_slides_pipeline.py:498:    for i, slide in enumerate(authored.slides):
packages/tools/src/disco/tools/builtin/_slides_pipeline.py:537:    if outline is None or not outline.slides:
packages/tools/src/disco/tools/builtin/_slides_pipeline.py:542:    for slide in outline.slides:
packages/tools/src/disco/tools/builtin/_slides_pipeline.py:582:        llm_url, model, api_key = _resolve_slides_llm()
packages/tools/src/disco/tools/builtin/_slides_pipeline.py:606:        image_assets = await _stage_assets(filled, ctx, backend, filename)
packages/tools/src/disco/tools/builtin/slides.py:6:AuthoredDeck and renders it to native editable .pptx / brand HTML.  On parse
packages/tools/src/disco/tools/builtin/slides.py:13:  - html: always works (C3 brand HTML, or Marp CLI, or self-contained fallback).
packages/tools/src/disco/tools/builtin/slides.py:15:  - pptx: C3 native editable PPTX (real text boxes) via python-pptx; or
packages/tools/src/disco/tools/builtin/slides.py:16:          Marp --pptx (image-based) when the C2 path is not used.
packages/tools/src/disco/tools/builtin/slides.py:21:import html
packages/tools/src/disco/tools/builtin/slides.py:36:    """Arguments for the slides_generate tool."""
packages/tools/src/disco/tools/builtin/slides.py:53:            "separate slides. Ignored when ``goal`` is supplied."
packages/tools/src/disco/tools/builtin/slides.py:59:            ".html, .pdf, or .pptx based on the format argument."
packages/tools/src/disco/tools/builtin/slides.py:62:    format: str = Field(
packages/tools/src/disco/tools/builtin/slides.py:63:        default="pptx",
packages/tools/src/disco/tools/builtin/slides.py:65:            "Output format: 'pptx' (default — a presentable, editable deck), 'pdf', or "
packages/tools/src/disco/tools/builtin/slides.py:66:            "'html'. Prefer 'pptx' for a deliverable the user opens/presents; 'html' is a "
packages/tools/src/disco/tools/builtin/slides.py:78:        description="Approximate number of slides (used by the C2 pipeline; ignored for markdown path).",
packages/tools/src/disco/tools/builtin/slides.py:120:# Simple inline markup patterns applied BEFORE the html.escape pass.
packages/tools/src/disco/tools/builtin/slides.py:131:def _inline_markdown_to_html(text: str) -> str:
packages/tools/src/disco/tools/builtin/slides.py:143:def _basic_md_to_html(md: str) -> str:
packages/tools/src/disco/tools/builtin/slides.py:146:    inline formatting. Deliberately simple — full Marp rendering is preferred."""
packages/tools/src/disco/tools/builtin/slides.py:162:            code_html = html.escape("\n".join(block_lines))
packages/tools/src/disco/tools/builtin/slides.py:163:            cls = f' class="language-{html.escape(lang)}"' if lang else ""
packages/tools/src/disco/tools/builtin/slides.py:164:            out.append(f"<pre><code{cls}>{code_html}</code></pre>")
packages/tools/src/disco/tools/builtin/slides.py:172:            text = _inline_markdown_to_html(html.escape(heading_match.group(2)))
packages/tools/src/disco/tools/builtin/slides.py:184:                _inline_markdown_to_html(html.escape(ln)) for ln in bq_lines
packages/tools/src/disco/tools/builtin/slides.py:193:                text = _inline_markdown_to_html(
packages/tools/src/disco/tools/builtin/slides.py:194:                    html.escape(re.sub(r"^[-*+]\s+", "", lines[i]))
packages/tools/src/disco/tools/builtin/slides.py:205:                text = _inline_markdown_to_html(
packages/tools/src/disco/tools/builtin/slides.py:206:                    html.escape(re.sub(r"^\d+\.\s+", "", lines[i]))
packages/tools/src/disco/tools/builtin/slides.py:236:                _inline_markdown_to_html(html.escape(ln)) for ln in para_lines
packages/tools/src/disco/tools/builtin/slides.py:243:def _split_slides(markdown: str) -> list[str]:
packages/tools/src/disco/tools/builtin/slides.py:244:    """Split markdown into slides on '---' separators. Handles leading/trailing
packages/tools/src/disco/tools/builtin/slides.py:250:    # Filter empty slides
packages/tools/src/disco/tools/builtin/slides.py:254:def _fallback_html(markdown: str, theme: str | None = None) -> str:
packages/tools/src/disco/tools/builtin/slides.py:257:    slides_raw = _split_slides(markdown)
packages/tools/src/disco/tools/builtin/slides.py:261:    for i, slide_md in enumerate(slides_raw):
packages/tools/src/disco/tools/builtin/slides.py:262:        html_body = _basic_md_to_html(slide_md)
packages/tools/src/disco/tools/builtin/slides.py:265:            f"    {html_body}\n"
packages/tools/src/disco/tools/builtin/slides.py:271:    <!DOCTYPE html>
packages/tools/src/disco/tools/builtin/slides.py:272:    <html lang="en">
packages/tools/src/disco/tools/builtin/slides.py:293:    </html>"""
packages/tools/src/disco/tools/builtin/slides.py:308:    assert ctx.sandbox is not None  # slides tool declares runs_in="sandbox"
packages/tools/src/disco/tools/builtin/slides.py:326:    assert ctx.sandbox is not None  # slides tool declares runs_in="sandbox"
packages/tools/src/disco/tools/builtin/slides.py:327:    pptx = "--pptx " if fmt == "pptx" else ""
packages/tools/src/disco/tools/builtin/slides.py:328:    cmd = f"marp {pptx}{shlex.quote(src_name)} -o {shlex.quote(out_name)}"
packages/tools/src/disco/tools/builtin/slides.py:342:        name="slides_generate",
packages/tools/src/disco/tools/builtin/slides.py:364:        fmt = args.format.lower()
packages/tools/src/disco/tools/builtin/slides.py:365:        if fmt not in ("html", "pdf", "pptx"):
packages/tools/src/disco/tools/builtin/slides.py:368:                content=f"Unsupported format: {fmt!r}. Use 'html', 'pdf', or 'pptx'.",
packages/tools/src/disco/tools/builtin/slides.py:369:                error=f"Unsupported format: {fmt!r}.",
packages/tools/src/disco/tools/builtin/slides.py:384:        from disco.tools.builtin._slides_pipeline import generate_deck
packages/tools/src/disco/tools/builtin/slides.py:404:        # C2 failed → fall back to Marp/html fallback with the generated markdown
packages/tools/src/disco/tools/builtin/slides.py:410:                format=args.format,
packages/tools/src/disco/tools/builtin/slides.py:446:        from disco.tools.builtin._pptx_render import convert_to_pdf, render_html, render_pptx
packages/tools/src/disco/tools/builtin/slides.py:458:        if fmt == "html":
packages/tools/src/disco/tools/builtin/slides.py:459:            html_str = render_html(deck)
packages/tools/src/disco/tools/builtin/slides.py:460:            await sbx.write_file(out_filename, html_str.encode("utf-8"))
packages/tools/src/disco/tools/builtin/slides.py:466:                    f"Slides: {len(deck.slides)}"
packages/tools/src/disco/tools/builtin/slides.py:472:                    "format": "html",
packages/tools/src/disco/tools/builtin/slides.py:473:                    "slide_count": len(deck.slides),
packages/tools/src/disco/tools/builtin/slides.py:479:                    "slides": [{"type": s.type, "layout": s.layout} for s in deck.slides],
packages/tools/src/disco/tools/builtin/slides.py:483:        if fmt == "pptx":
packages/tools/src/disco/tools/builtin/slides.py:484:            pptx_bytes = render_pptx(deck)
packages/tools/src/disco/tools/builtin/slides.py:485:            await sbx.write_file(out_filename, pptx_bytes)
packages/tools/src/disco/tools/builtin/slides.py:488:            html_name = f"{args.filename}.html"
packages/tools/src/disco/tools/builtin/slides.py:490:                html_str = render_html(deck)
packages/tools/src/disco/tools/builtin/slides.py:491:                await sbx.write_file(html_name, html_str.encode("utf-8"))
packages/tools/src/disco/tools/builtin/slides.py:492:                artifacts = [out_filename, html_name]
packages/tools/src/disco/tools/builtin/slides.py:510:                    f"Slides: {len(deck.slides)}{pdf_note}"
packages/tools/src/disco/tools/builtin/slides.py:516:                    "format": "pptx",
packages/tools/src/disco/tools/builtin/slides.py:517:                    "slide_count": len(deck.slides),
packages/tools/src/disco/tools/builtin/slides.py:518:                    "renderer": "pptx-native",
packages/tools/src/disco/tools/builtin/slides.py:521:                    "slides": [{"type": s.type, "layout": s.layout} for s in deck.slides],
packages/tools/src/disco/tools/builtin/slides.py:527:            pptx_bytes = render_pptx(deck)
packages/tools/src/disco/tools/builtin/slides.py:528:            pptx_name = f"{args.filename}.pptx"
packages/tools/src/disco/tools/builtin/slides.py:529:            await sbx.write_file(pptx_name, pptx_bytes)
packages/tools/src/disco/tools/builtin/slides.py:530:            pdf_ok, pdf_err = await convert_to_pdf(ctx, pptx_name)
packages/tools/src/disco/tools/builtin/slides.py:542:                    f"Slides: {len(deck.slides)}"
packages/tools/src/disco/tools/builtin/slides.py:544:                artifacts=[out_filename, pptx_name],
packages/tools/src/disco/tools/builtin/slides.py:548:                    "format": "pdf",
packages/tools/src/disco/tools/builtin/slides.py:549:                    "slide_count": len(deck.slides),
packages/tools/src/disco/tools/builtin/slides.py:551:                    # A2: a fresh sidecar makes even a pdf-format deck editable (the
packages/tools/src/disco/tools/builtin/slides.py:552:                    # editor re-renders html/pptx; the pdf is flagged stale on save).
packages/tools/src/disco/tools/builtin/slides.py:559:            content=f"Unsupported format: {fmt!r}",
packages/tools/src/disco/tools/builtin/slides.py:560:            error=f"Unsupported format: {fmt!r}",
packages/tools/src/disco/tools/builtin/slides.py:577:        if fmt == "html":
packages/tools/src/disco/tools/builtin/slides.py:580:            return await self._render_html_fallback(markdown, out_filename, ctx, args)
packages/tools/src/disco/tools/builtin/slides.py:588:                    f"as a fallback — re-run with format='html' for a rendered deck."
packages/tools/src/disco/tools/builtin/slides.py:606:        assert ctx.sandbox is not None  # slides tool declares runs_in="sandbox"
packages/tools/src/disco/tools/builtin/slides.py:629:        if fmt == "pptx":
packages/tools/src/disco/tools/builtin/slides.py:631:                "\nNOTE: Marp PPTX output is image-based slides (each slide is a static image). "
packages/tools/src/disco/tools/builtin/slides.py:633:                "(_pptx_render.py) when a structured deck is supplied (C2 pipeline)."
packages/tools/src/disco/tools/builtin/slides.py:636:        slide_count = len(_split_slides(args.markdown))
packages/tools/src/disco/tools/builtin/slides.py:648:                "format": fmt,
packages/tools/src/disco/tools/builtin/slides.py:654:    async def _render_html_fallback(
packages/tools/src/disco/tools/builtin/slides.py:663:        assert ctx.sandbox is not None  # slides tool declares runs_in="sandbox"
packages/tools/src/disco/tools/builtin/slides.py:664:        html_content = _fallback_html(markdown, args.theme)
packages/tools/src/disco/tools/builtin/slides.py:665:        html_bytes = html_content.encode("utf-8")
packages/tools/src/disco/tools/builtin/slides.py:668:        await ctx.sandbox.write_file(out_filename, html_bytes)
packages/tools/src/disco/tools/builtin/slides.py:670:        slide_count = len(_split_slides(args.markdown))
packages/tools/src/disco/tools/builtin/slides.py:682:                "format": "html",

exec
/bin/bash -lc "nl -ba packages/tools/tests/test_slides_procedural_contract.py | sed -n '1,180p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""W3 — slides/artifact contract.
     2	
     3	The runthru bug: a "make slides" request shipped a deck whose image slots rendered as
     4	garish procedural placeholder graphics (nested rectangles + diagonal), because the deck
     5	pipeline auto-generates an image per ``image_prompt`` slide and the image provider defaulted
     6	to the keyless ``pil-procedural`` backend. A presentable deck must NOT embed procedural
     7	placeholders — when no real image provider is configured, omit the image (text slide). Also:
     8	the slides tool default format must be a presentable deck (``pptx``), not raw ``html``.
     9	"""
    10	
    11	from __future__ import annotations
    12	
    13	import pytest
    14	from disco.tools.anatomy import ToolContext
    15	from disco.tools.builtin._deck_schema import AuthoredDeck, AuthoredSlide
    16	from disco.tools.builtin._slides_pipeline import _stage_assets
    17	from disco.tools.builtin.image_gen import _PILProceduralBackend
    18	
    19	pytestmark = pytest.mark.boundary_contract
    20	
    21	
    22	def _deck_with_image() -> AuthoredDeck:
    23	    return AuthoredDeck(
    24	        title="History of Three.js",
    25	        slides=[
    26	            AuthoredSlide(
    27	                type="title", title="Hero", body=[],
    28	                image_prompt="a hero image of the three.js logo",
    29	            ),
    30	        ],
    31	    )
    32	
    33	
    34	class _RealBackend:
    35	    """A stand-in for a real (connected) image provider — NOT the placeholder one."""
    36	
    37	    name = "fake-real"
    38	    is_remote = True
    39	
    40	    def generate(self, *, prompt, width, height, seed, fmt) -> bytes:  # noqa: ANN001
    41	        # a minimal valid 1x1 PNG
    42	        import base64
    43	
    44	        return base64.b64decode(
    45	            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    46	        )
    47	
    48	
    49	async def test_procedural_backend_omits_deck_images():
    50	    """The placeholder backend must NOT embed images in the deck (no garish boxes)."""
    51	    ctx = ToolContext.model_construct(sandbox=None)
    52	    assets = await _stage_assets(_deck_with_image(), ctx, _PILProceduralBackend(), "f")
    53	    assert assets == {}, "procedural placeholder backend must not embed images into a deck"
    54	
    55	
    56	async def test_real_backend_still_stages_images():
    57	    """A connected provider DOES embed images (the fix must not break real image decks)."""
    58	    ctx = ToolContext.model_construct(sandbox=None)
    59	    assets = await _stage_assets(_deck_with_image(), ctx, _RealBackend(), "f")
    60	    assert 0 in assets, "a real backend must still stage images"
    61	    assert assets[0].startswith(b"\x89PNG")
    62	
    63	
    64	def test_slides_default_format_is_pptx():
    65	    """The slides tool must default to a presentable deck format, not raw html."""
    66	    from disco.tools.builtin.slides import SlidesGenerateArgs
    67	
    68	    assert SlidesGenerateArgs.model_fields["format"].default == "pptx"

exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/builtin/_slides_pipeline.py | sed -n '520,650p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   520	                _LOG.warning("Slide %d image is not PNG/JPEG — skipping embed", i)
   521	                continue
   522	            assets[i] = img_bytes
   523	            if ctx.sandbox is not None:
   524	                await ctx.sandbox.write_file(img_name, img_bytes)
   525	        except Exception as e:  # noqa: BLE001
   526	            _LOG.warning("Image generation failed for slide %d: %s", i, e)
   527	    return assets
   528	
   529	
   530	# ---------------------------------------------------------------------------
   531	# Fallback markdown generator (used when fill stage fails)
   532	# ---------------------------------------------------------------------------
   533	
   534	
   535	def _outline_to_markdown(outline: AuthoredDeck | None, goal: str) -> str:
   536	    """Convert an outline (or None) to minimal Marp markdown as a fallback."""
   537	    if outline is None or not outline.slides:
   538	        # Bare minimum fallback
   539	        return f"# {goal}\n\n---\n\n*Deck generation failed — please try again.*"
   540	
   541	    parts: list[str] = []
   542	    for slide in outline.slides:
   543	        title = slide.title or "Slide"
   544	        body = "\n".join(f"- {b}" for b in slide.body) if slide.body else ""
   545	        parts.append(f"# {title}\n\n{body}".strip())
   546	    return "\n\n---\n\n".join(parts)
   547	
   548	
   549	# ---------------------------------------------------------------------------
   550	# generate_deck — main pipeline entry point
   551	# ---------------------------------------------------------------------------
   552	
   553	
   554	async def generate_deck(
   555	    goal: str,
   556	    filename: str,
   557	    ctx: ToolContext,
   558	    backend: ImageBackend,
   559	    *,
   560	    slide_count: int = 5,
   561	) -> tuple[Deck | None, str | None, str | None, str | None]:
   562	    """Run the full C2 generation pipeline.
   563	
   564	    Returns:
   565	        (c1_deck, fallback_markdown, error_msg, authored_sidecar)
   566	
   567	    ``authored_sidecar`` is the ``{filename}.authored.json`` path IFF this run wrote
   568	    it successfully (else None) — the caller advertises the in-app editor only on a
   569	    real, fresh sidecar, never a stale leftover from a prior run whose write failed.
   570	
   571	    On success: (Deck, None, None, sidecar-or-None)
   572	    On fill-stage failure: (None, fallback_markdown_str, error_msg, None)
   573	    """
   574	    # ROOT-5: honor the CONVERSATION's effective (override-aware) driver model when the
   575	    # executor supplied it — so a deck authored in a conversation that picked a specific
   576	    # model (e.g. a remote DeepSeek) is generated by THAT model, not the global default
   577	    # AGENT_DRIVER. The key env-var name is resolved here (ctx never carries raw secrets).
   578	    if ctx.driver_llm is not None:
   579	        base_url, model, api_key_env = ctx.driver_llm
   580	        llm_url, api_key = base_url.rstrip("/"), _resolve_llm_key(api_key_env)
   581	    else:
   582	        llm_url, model, api_key = _resolve_slides_llm()
   583	    system = _WEAK_SYSTEM if ctx.assist else _CAPABLE_SYSTEM
   584	
   585	    # Stage 1: Outline
   586	    outline, _raw_outline, outline_err = await _stage_outline(
   587	        goal, slide_count, system, llm_url, model, api_key
   588	    )
   589	
   590	    if outline is None:
   591	        _LOG.warning("Outline stage failed: %s", outline_err)
   592	        fallback_md = _outline_to_markdown(None, goal)
   593	        return None, fallback_md, outline_err, None
   594	
   595	    # Stage 2: Fill
   596	    filled, fill_err = await _stage_fill(outline, system, llm_url, model, api_key)
   597	
   598	    if filled is None:
   599	        _LOG.warning("Fill stage failed: %s — using outline as fallback", fill_err)
   600	        fallback_md = _outline_to_markdown(outline, goal)
   601	        return None, fallback_md, fill_err, None
   602	
   603	    # Stage 3: Assets (image_prompt → image bytes + sandbox files)
   604	    image_assets: dict[int, bytes] = {}
   605	    if ctx.sandbox is not None:
   606	        image_assets = await _stage_assets(filled, ctx, backend, filename)
   607	
   608	    # Stage 4: Lower to C1 Deck (carry generated image bytes so they embed — C7)
   609	    try:
   610	        deck = lower_deck(filled, image_assets=image_assets)
   611	    except Exception as e:  # noqa: BLE001
   612	        _LOG.warning("lower_deck failed: %s — falling back to Marp", e)
   613	        fallback_md = _outline_to_markdown(filled, goal)
   614	        return None, fallback_md, str(e), None
   615	
   616	    # A2.0: persist the editable AuthoredDeck source alongside the renders so the
   617	    # in-app deck editor (+ deck_patch tool) can read it back. Best-effort: a write
   618	    # failure here must not sink an otherwise-successful generation — but we report
   619	    # the sidecar path ONLY when the write actually succeeded, so the caller never
   620	    # advertises the editor against a stale leftover sidecar from an earlier run.
   621	    authored_sidecar: str | None = None
   622	    if ctx.sandbox is not None:
   623	        sidecar = f"{filename}.authored.json"
   624	        try:
   625	            await ctx.sandbox.write_file(
   626	                sidecar, filled.model_dump_json(indent=2).encode()
   627	            )
   628	            authored_sidecar = sidecar
   629	        except Exception as e:  # noqa: BLE001
   630	            _LOG.warning("Failed to persist authored.json for %s: %s", filename, e)
   631	
   632	    return deck, None, None, authored_sidecar

exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/builtin/image_gen.py | sed -n '180,270p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   180	        ),
   181	    )
   182	    format: str = Field(
   183	        default="png",
   184	        description="Output format: 'png' (lossless) or 'jpeg' (smaller). Default: 'png'.",
   185	    )
   186	
   187	
   188	# ---- backend protocol + default implementation -----------------------------
   189	
   190	
   191	class ImageBackend(Protocol):
   192	    """The interface a real image-gen backend must speak. The tool only
   193	    depends on this shape — the implementation is injected at runtime, so any
   194	    new provider backend can replace `_PILProceduralBackend` without touching
   195	    the tool's `run()` body."""
   196	
   197	    name: str  # surfaced in the deliverable summary ("backend": "pil-procedural")
   198	    is_remote: bool  # network-bound? (False for both current backends)
   199	
   200	    def generate(
   201	        self,
   202	        *,
   203	        prompt: str,
   204	        width: int,
   205	        height: int,
   206	        seed: int,
   207	        fmt: str,
   208	    ) -> bytes:
   209	        """Return the encoded image bytes (PNG or JPEG container). Must
   210	        return raw `bytes` — never `str`, never a file handle. The tool
   211	        writes them straight to the sandbox; any text-mode conversion here
   212	        would silently corrupt bytes >= 0x80."""
   213	        ...
   214	
   215	
   216	def _prompt_seed(prompt: str) -> int:
   217	    """Derive a stable 32-bit seed from a free-form prompt. SHA-256 is overkill
   218	    for a seed but is already in stdlib, deterministic across Python versions,
   219	    and gives the model a clean "same prompt → same image" property for tests
   220	    and for the agent to iterate on a prompt visually."""
   221	    h = hashlib.sha256(prompt.encode("utf-8")).digest()
   222	    return int.from_bytes(h[:4], "big", signed=False)
   223	
   224	
   225	class _PILProceduralBackend:
   226	    """Keyless / local / default backend. No network, no model download, no
   227	    API key — Pillow + numpy are the only deps, both already in the tools
   228	    package's runtime.
   229	
   230	    Renders a small, deterministic geometric pattern: a base hue seeded from
   231	    the prompt, a concentric ring count + thickness from the seed, and a
   232	    three-color palette. Same prompt + same seed → byte-identical PNG
   233	    (PNG encoding is deterministic for the same input array + same PIL
   234	    build), which is what the round-trip test asserts.
   235	
   236	    NOTE — this is NOT a generative model; it's the keyless DEFAULT that
   237	    guarantees `image_generate` always produces a valid image file offline.
   238	    For photoreal / subject-accurate output, configure the ComfyUI (self-host)
   239	    or OpenAI-compatible (paid) tier in Settings — both speak the same
   240	    `ImageBackend` protocol and route through this same binary write path.
   241	    """
   242	
   243	    name = "pil-procedural"
   244	    is_remote = False
   245	
   246	    def generate(
   247	        self,
   248	        *,
   249	        prompt: str,
   250	        width: int,
   251	        height: int,
   252	        seed: int,
   253	        fmt: str,
   254	    ) -> bytes:
   255	        # Lazy imports keep the tools package importable even on a Pillow-less
   256	        # minimal install (the build is fully headless otherwise). The tool
   257	        # only needs them at run-time, in the sandbox, when the model asks
   258	        # for an image.
   259	        from PIL import Image, ImageDraw
   260	
   261	        rng_state = seed & 0xFFFFFFFF
   262	        # A three-color palette derived from the seed — the visual signal
   263	        # that "different prompt → different image" holds in the round-trip
   264	        # test (asserted via SHA-256 of the bytes, not by eye).
   265	        hue1 = (rng_state >> 0) & 0xFF
   266	        hue2 = (rng_state >> 8) & 0xFF
   267	        hue3 = (rng_state >> 16) & 0xFF
   268	        c1 = (hue1, (hue1 * 7) & 0xFF, 255 - hue1)
   269	        c2 = (hue2, 255 - (hue2 * 3) & 0xFF, hue2)
   270	        c3 = (255 - hue3, hue3, (hue3 * 5) & 0xFF)

exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/builtin/image_gen.py | sed -n '930,1010p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   930	        # JPEG) and a text encode round-trip would silently corrupt them.
   931	        # This is exactly the kind of bug the task brief calls out.
   932	        assert ctx.sandbox is not None, (
   933	            "image_generate requires a sandbox instance for the binary file write"
   934	        )
   935	        # Detect the ACTUAL format from the produced bytes — the remote tiers (OpenAI,
   936	        # ComfyUI) ignore the requested `fmt` and emit PNG regardless, so trust the bytes,
   937	        # not the request. This keeps the extension, the magic-bytes check, the on-disk
   938	        # file, and the reported format all consistent with what was really produced
   939	        # (so `format="jpeg"` against a PNG-only provider yields an honest .png, not a
   940	        # rejected request). PNG and JPEG are the only supported deliverable formats.
   941	        if image_bytes.startswith(_PNG_MAGIC):
   942	            actual_fmt = "png"
   943	        elif image_bytes.startswith(_JPEG_MAGIC):
   944	            actual_fmt = "jpeg"
   945	        else:
   946	            return ToolOutcome(
   947	                success=False,
   948	                content=(
   949	                    f"image_generate: backend produced bytes that are neither a PNG nor "
   950	                    f"a JPEG — refusing to surface a corrupt image as a deliverable. "
   951	                    f"(backend={backend.name})"
   952	                ),
   953	                error="unrecognized_image_format",
   954	            )
   955	        ext = "jpg" if actual_fmt == "jpeg" else "png"
   956	        # Sanitize the user-provided base filename: strip directory components and force a
   957	        # sane extension. The sandbox jails paths, but the filename should also be a real
   958	        # file basename so the deliverable UI can show it cleanly.
   959	        safe_base = os.path.basename(args.filename)
   960	        if not safe_base or safe_base in {".", ".."}:
   961	            safe_base = "image"
   962	        out_path = f"{safe_base}.{ext}"
   963	        await ctx.sandbox.write_file(out_path, image_bytes)
   964	
   965	        # ---- success: short text summary, image bytes NEVER in content -
   966	        # The model should see size + dimensions + format + seed, NOT the
   967	        # image bytes themselves. The bytes live on disk in the sandbox
   968	        # and are referenced by path. Surfacing them in `content` would
   969	        # both bloat the model's context AND risk any downstream
   970	        # text-mode transport mangling the binary.
   971	        magic_hex = image_bytes[:8].hex()
   972	        # Report the ACTUAL produced dimensions, not the requested ones: some backends
   973	        # can't honor arbitrary sizes (the OpenAI tier snaps to 1024x1024), so reporting
   974	        # args.width/height would be false metadata and silently swallow the aspect ratio.
   975	        from PIL import Image as _PILImage
   976	
   977	        try:
   978	            actual_w, actual_h = _PILImage.open(io.BytesIO(image_bytes)).size
   979	        except Exception:  # noqa: BLE001 — fall back to requested dims if decode fails
   980	            actual_w, actual_h = args.width, args.height
   981	
   982	        # The procedural backend ALWAYS "succeeds" (it emits a valid PNG), so without
   983	        # an explicit signal the agent can't tell it isn't really connected to a
   984	        # generative model — and will retry the same prompt expecting a photo it can
   985	        # never get. Make the placeholder nature UNMISTAKABLE in the observation and
   986	        # tell the agent NOT to retry. (The remote tiers raise/fail loudly instead, so
   987	        # this note only rides the keyless procedural default.)
   988	        is_placeholder = backend.name == "pil-procedural"
   989	        placeholder_note = (
   990	            " — NOTE: this is an ABSTRACT PROCEDURAL PLACEHOLDER, not a depiction of "
   991	            "the prompt: no real image-generation backend is connected. Retrying will "
   992	            "NOT change this. Use it only as decorative/background art; for real or "
   993	            "photoreal images a ComfyUI (self-host) or OpenAI-compatible (paid) backend "
   994	            "must be configured in Settings → Image generation."
   995	            if is_placeholder
   996	            else ""
   997	        )
   998	        return ToolOutcome(
   999	            success=True,
  1000	            content=(
  1001	                f"Image written: {len(image_bytes)} bytes to {out_path} "
  1002	                f"({actual_w}x{actual_h} {actual_fmt.upper()}, "
  1003	                f"seed={seed}, backend={backend.name}){placeholder_note}"
  1004	            ),
  1005	            artifacts=[out_path],
  1006	            structured={
  1007	                "path": out_path,
  1008	                "filename_base": safe_base,
  1009	                "format": actual_fmt,
  1010	                "width": actual_w,

exec
/bin/bash -lc 'rg -n "class .*Backend|name =|def select_image_backend|ImageBackend" packages/tools/src/disco/tools/builtin/image_gen.py' in /var/home/dylan/projects/disco
 succeeded in 0ms:
7:The tool is structured around an `ImageBackend` protocol with THREE tiers, the
13:- `_OpenAIImageBackend`: OpenAI-compatible /v1/images/generations API (paid).
191:class ImageBackend(Protocol):
225:class _PILProceduralBackend:
240:    `ImageBackend` protocol and route through this same binary write path.
243:    name = "pil-procedural"
307:class _OpenAIImageBackend:
326:    name = "openai-compatible"
409:class _OpenRouterImageBackend:
416:    `_OpenAIImageBackend`, hence its own backend.
425:    name = "openrouter-image"
514:class _ComfyUIBackend:
534:    name = "comfyui"
747:def select_image_backend() -> ImageBackend:
766:    # For openai, we need a key. base_url is the ORIGIN only — _OpenAIImageBackend.generate
781:                return _OpenAIImageBackend(base_url, api_key, model=settings.model)
803:            return _OpenRouterImageBackend(
856:    def __init__(self, backend: ImageBackend | None = None) -> None:
863:        self._backend: ImageBackend = backend or _PILProceduralBackend()
865:    def _resolve_backend(self) -> ImageBackend:
988:        is_placeholder = backend.name == "pil-procedural"

exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/builtin/slides.py | sed -n '330,430p' && nl -ba packages/tools/src/disco/tools/builtin/slides.py | sed -n '570,650p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   330	    if res.timed_out:
   331	        return False, f"marp render timed out after {timeout_s}s"
   332	    if res.exit_code != 0:
   333	        return False, (res.stderr or "").strip() or f"marp exited with code {res.exit_code}"
   334	    return True, ""
   335	
   336	
   337	# ---- tool implementation -----------------------------------------------------
   338	
   339	
   340	class SlidesTool:
   341	    definition = ToolDef(
   342	        name="slides_generate",
   343	        description=(
   344	            "Write a slide deck (HTML/PDF/PPTX) to the workspace.\n\n"
   345	            "Primary path (recommended): supply ``goal`` (e.g. 'A 6-slide investor "
   346	            "pitch for an EV battery startup') and the C2 pipeline generates a "
   347	            "structured, brand-themed deck automatically (native editable PPTX + "
   348	            "brand HTML).  On failure, automatically falls back to Marp.\n\n"
   349	            "Markdown path (backward-compat): supply ``markdown`` with '---' slide "
   350	            "separators; rendered via Marp CLI (image-based PPTX) or the HTML "
   351	            "fallback.\n\n"
   352	            "HTML always works.  PDF and native PPTX require the sandbox toolchain."
   353	        ),
   354	        args_model=SlidesGenerateArgs,
   355	        needs=frozenset({Capability.FILESYSTEM}),
   356	        base_risk=None,
   357	        runs_in="sandbox",
   358	        read_only=False,
   359	    )
   360	
   361	    async def run(self, args: SlidesGenerateArgs, ctx: ToolContext) -> ToolOutcome:
   362	        assert ctx.sandbox is not None  # sandbox tools always receive an instance
   363	
   364	        fmt = args.format.lower()
   365	        if fmt not in ("html", "pdf", "pptx"):
   366	            return ToolOutcome(
   367	                success=False,
   368	                content=f"Unsupported format: {fmt!r}. Use 'html', 'pdf', or 'pptx'.",
   369	                error=f"Unsupported format: {fmt!r}.",
   370	            )
   371	
   372	        # ---- C2 deck pipeline (primary path when goal is supplied) ----
   373	        use_c2 = bool(args.goal) and args.mode != "markdown"
   374	        if use_c2:
   375	            return await self._run_c2_pipeline(args, ctx, fmt)
   376	
   377	        # ---- Marp / markdown path (fallback or explicit mode="markdown") ----
   378	        return await self._run_marp_path(args, ctx, fmt)
   379	
   380	    async def _run_c2_pipeline(
   381	        self, args: SlidesGenerateArgs, ctx: ToolContext, fmt: str
   382	    ) -> ToolOutcome:
   383	        """Run the C2 staged generation pipeline.  Falls back to Marp on failure."""
   384	        from disco.tools.builtin._slides_pipeline import generate_deck
   385	
   386	        assert args.goal is not None  # caller-checked
   387	        backend = select_image_backend()
   388	
   389	        c1_deck, fallback_md, err, authored_sidecar = await generate_deck(
   390	            args.goal,
   391	            args.filename,
   392	            ctx,
   393	            backend,
   394	            slide_count=args.slide_count,
   395	        )
   396	
   397	        if c1_deck is not None:
   398	            # C2 succeeded — render via C3. authored_sidecar is the fresh editable
   399	            # source (or None if the sidecar write failed) → gates the editor.
   400	            return await self._render_c1_deck(
   401	                c1_deck, args, ctx, fmt, editable_source=authored_sidecar
   402	            )
   403	
   404	        # C2 failed → fall back to Marp/html fallback with the generated markdown
   405	        if fallback_md:
   406	            marp_args = SlidesGenerateArgs(
   407	                goal=None,
   408	                markdown=fallback_md,
   409	                filename=args.filename,
   410	                format=args.format,
   411	                theme=args.theme,
   412	                mode="markdown",
   413	            )
   414	            outcome = await self._run_marp_path(marp_args, ctx, fmt)
   415	            # Annotate content to indicate C2 failure + fallback
   416	            note = f"\n[C2 pipeline failed ({err}); rendered via Marp fallback]"
   417	            return ToolOutcome(
   418	                success=outcome.success,
   419	                content=outcome.content + note,
   420	                error=outcome.error,
   421	                artifacts=outcome.artifacts,
   422	                structured=outcome.structured,
   423	            )
   424	
   425	        return ToolOutcome(
   426	            success=False,
   427	            content=f"Deck generation failed: {err}",
   428	            error=err or "deck_generation_failed",
   429	        )
   430	
   570	        markdown = args.markdown
   571	        if args.theme:
   572	            theme_block = f"<!-- theme: custom -->\n<style>\n{args.theme}\n</style>\n\n"
   573	            markdown = theme_block + markdown
   574	
   575	        have_marp = await _marp_available(ctx)
   576	
   577	        if fmt == "html":
   578	            if have_marp:
   579	                return await self._render_with_marp(markdown, out_filename, fmt, ctx, args)
   580	            return await self._render_html_fallback(markdown, out_filename, ctx, args)
   581	
   582	        if not have_marp:
   583	            return ToolOutcome(
   584	                success=False,
   585	                content=(
   586	                    f"{fmt.upper()} export needs the Marp CLI + Chromium in the sandbox "
   587	                    f"image, which isn't present in this sandbox. HTML export works now "
   588	                    f"as a fallback — re-run with format='html' for a rendered deck."
   589	                ),
   590	                error=f"marp CLI not found in the sandbox — {fmt.upper()} export unavailable.",
   591	            )
   592	
   593	        return await self._render_with_marp(markdown, out_filename, fmt, ctx, args)
   594	
   595	    async def _render_with_marp(
   596	        self,
   597	        markdown: str,
   598	        out_filename: str,
   599	        fmt: str,
   600	        ctx: ToolContext,
   601	        args: SlidesGenerateArgs,
   602	    ) -> ToolOutcome:
   603	        """Render via marp INSIDE the sandbox: write the markdown source into the
   604	        jailed workspace, run marp in-box (output lands directly in the
   605	        workspace), then clean up the source. No host temp files, no read-back."""
   606	        assert ctx.sandbox is not None  # slides tool declares runs_in="sandbox"
   607	        # A hidden workspace-relative source file marp reads (workdir = workspace).
   608	        src_name = f".{args.filename}.marp-src.md"
   609	        await ctx.sandbox.write_file(src_name, markdown.encode("utf-8"))
   610	        try:
   611	            ok, err = await _marp_render_in_sandbox(ctx, src_name, out_filename, fmt)
   612	        finally:
   613	            # Best-effort cleanup of the transient source inside the sandbox.
   614	            try:
   615	                await ctx.sandbox.exec_shell(f"rm -f {shlex.quote(src_name)}", timeout_s=10)
   616	            except Exception:
   617	                pass
   618	
   619	        if not ok:
   620	            return ToolOutcome(
   621	                success=False,
   622	                content=f"Marp render failed: {err}",
   623	                error=f"Marp render failed: {err}",
   624	            )
   625	        # The output already lives in the workspace at out_filename (marp wrote it
   626	        # there) — jailed by construction; nothing to read back.
   627	
   628	        note = ""
   629	        if fmt == "pptx":
   630	            note = (
   631	                "\nNOTE: Marp PPTX output is image-based slides (each slide is a static image). "
   632	                "Native editable PPTX with real text boxes is available via the C3 renderer "
   633	                "(_pptx_render.py) when a structured deck is supplied (C2 pipeline)."
   634	            )
   635	
   636	        slide_count = len(_split_slides(args.markdown))
   637	        return ToolOutcome(
   638	            success=True,
   639	            content=(
   640	                f"Slide deck '{args.filename}' written to {out_filename}\n"
   641	                f"Format: {fmt.upper()}\n"
   642	                f"Slides: {slide_count}{note}"
   643	            ),
   644	            artifacts=[out_filename],
   645	            structured={
   646	                "filename": out_filename,
   647	                "base_name": args.filename,
   648	                "format": fmt,
   649	                "slide_count": slide_count,
   650	                "renderer": "marp",

exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/builtin/slides.py | sed -n '430,570p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   430	
   431	    async def _render_c1_deck(
   432	        self,
   433	        deck,
   434	        args: SlidesGenerateArgs,
   435	        ctx: ToolContext,
   436	        fmt: str,
   437	        *,
   438	        editable_source: str | None = None,
   439	    ) -> ToolOutcome:
   440	        """Render a C1 Deck to the sandbox and return a ToolOutcome.
   441	
   442	        ``editable_source`` is the ``{name}.authored.json`` path IFF this generation
   443	        freshly wrote it (from generate_deck). It gates the in-app editor: a swallowed
   444	        sidecar-write failure → None → no "Edit Slides" affordance, and never an
   445	        affordance pointing at a stale leftover sidecar (no false affordance)."""
   446	        from disco.tools.builtin._pptx_render import convert_to_pdf, render_html, render_pptx
   447	
   448	        if ctx.sandbox is None:
   449	            return ToolOutcome(success=False, content="No sandbox available to write the slide deck.")
   450	        sbx = ctx.sandbox
   451	        out_filename = f"{args.filename}.{fmt}"
   452	
   453	        # A2: advertise the in-app editor ONLY when THIS run wrote a fresh sidecar.
   454	        editable: dict[str, str] = (
   455	            {"editable_source": editable_source} if editable_source else {}
   456	        )
   457	
   458	        if fmt == "html":
   459	            html_str = render_html(deck)
   460	            await sbx.write_file(out_filename, html_str.encode("utf-8"))
   461	            return ToolOutcome(
   462	                success=True,
   463	                content=(
   464	                    f"Slide deck '{args.filename}' written to {out_filename}\n"
   465	                    f"Format: HTML (C3 brand renderer)\n"
   466	                    f"Slides: {len(deck.slides)}"
   467	                ),
   468	                artifacts=[out_filename],
   469	                structured={
   470	                    "filename": out_filename,
   471	                    "base_name": args.filename,
   472	                    "format": "html",
   473	                    "slide_count": len(deck.slides),
   474	                    "renderer": "c3-brand",
   475	                    # A2.0/A2.2: editable_source (the AuthoredDeck sidecar) is present
   476	                    # ONLY when the sidecar write actually succeeded — its presence is
   477	                    # what gates the in-app deck editor tab.
   478	                    **editable,
   479	                    "slides": [{"type": s.type, "layout": s.layout} for s in deck.slides],
   480	                },
   481	            )
   482	
   483	        if fmt == "pptx":
   484	            pptx_bytes = render_pptx(deck)
   485	            await sbx.write_file(out_filename, pptx_bytes)
   486	
   487	            # Also write brand HTML alongside
   488	            html_name = f"{args.filename}.html"
   489	            try:
   490	                html_str = render_html(deck)
   491	                await sbx.write_file(html_name, html_str.encode("utf-8"))
   492	                artifacts = [out_filename, html_name]
   493	            except Exception:
   494	                artifacts = [out_filename]
   495	
   496	            # Attempt PDF via LibreOffice
   497	            pdf_note = ""
   498	            pdf_ok, pdf_err = await convert_to_pdf(ctx, out_filename)
   499	            if pdf_ok:
   500	                pdf_name = f"{args.filename}.pdf"
   501	                artifacts.append(pdf_name)
   502	            else:
   503	                pdf_note = f"\nPDF: {pdf_err}"
   504	
   505	            return ToolOutcome(
   506	                success=True,
   507	                content=(
   508	                    f"Slide deck '{args.filename}' written to {out_filename}\n"
   509	                    f"Format: PPTX (C3 native editable — real text boxes)\n"
   510	                    f"Slides: {len(deck.slides)}{pdf_note}"
   511	                ),
   512	                artifacts=artifacts,
   513	                structured={
   514	                    "filename": out_filename,
   515	                    "base_name": args.filename,
   516	                    "format": "pptx",
   517	                    "slide_count": len(deck.slides),
   518	                    "renderer": "pptx-native",
   519	                    # A2.0/A2.2: present only when the sidecar write actually succeeded.
   520	                    **editable,
   521	                    "slides": [{"type": s.type, "layout": s.layout} for s in deck.slides],
   522	                },
   523	            )
   524	
   525	        if fmt == "pdf":
   526	            # Render PPTX first, then convert
   527	            pptx_bytes = render_pptx(deck)
   528	            pptx_name = f"{args.filename}.pptx"
   529	            await sbx.write_file(pptx_name, pptx_bytes)
   530	            pdf_ok, pdf_err = await convert_to_pdf(ctx, pptx_name)
   531	            if not pdf_ok:
   532	                return ToolOutcome(
   533	                    success=False,
   534	                    content=f"PDF conversion failed: {pdf_err}",
   535	                    error=pdf_err,
   536	                )
   537	            return ToolOutcome(
   538	                success=True,
   539	                content=(
   540	                    f"Slide deck '{args.filename}' written to {out_filename}\n"
   541	                    f"Format: PDF (via LibreOffice + C3 PPTX)\n"
   542	                    f"Slides: {len(deck.slides)}"
   543	                ),
   544	                artifacts=[out_filename, pptx_name],
   545	                structured={
   546	                    "filename": out_filename,
   547	                    "base_name": args.filename,
   548	                    "format": "pdf",
   549	                    "slide_count": len(deck.slides),
   550	                    "renderer": "libreoffice",
   551	                    # A2: a fresh sidecar makes even a pdf-format deck editable (the
   552	                    # editor re-renders html/pptx; the pdf is flagged stale on save).
   553	                    **editable,
   554	                },
   555	            )
   556	
   557	        return ToolOutcome(
   558	            success=False,
   559	            content=f"Unsupported format: {fmt!r}",
   560	            error=f"Unsupported format: {fmt!r}",
   561	        )
   562	
   563	    async def _run_marp_path(
   564	        self, args: SlidesGenerateArgs, ctx: ToolContext, fmt: str
   565	    ) -> ToolOutcome:
   566	        """Run the Marp/markdown rendering path."""
   567	        out_filename = f"{args.filename}.{fmt}"
   568	
   569	        # Prepare markdown: inject theme as frontmatter if provided
   570	        markdown = args.markdown

codex
There is a real compatibility risk in slides: changing the default to `pptx` routes markdown-only callers into the Marp-required path, while `html` is the only guaranteed fallback. I’m moving to evidence/redaction now because that affects what the harness can safely expose.
exec
/bin/bash -lc "nl -ba packages/core/src/disco/core/evidence/schema.py | sed -n '1,260p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""Evidence/correlation schema — evidence-harness-campaign.md W4.
     2	
     3	Canonical data shapes for the harness. No HTTP/WS wiring here; that is a
     4	later work item. This module is the single Python source of truth for:
     5	
     6	  - The ``EvidenceRecord`` model stamped into event ``meta`` and harness logs.
     7	  - The ``ErrorTaxonomy`` enum for structured failure classification.
     8	  - ``REDACTION_KEY_PATTERNS`` + ``redact()`` for sanitising serialized bundles.
     9	  - W3C Trace Context helpers: ``make_traceparent`` / ``parse_traceparent``.
    10	"""
    11	
    12	from __future__ import annotations
    13	
    14	import re
    15	from enum import Enum
    16	from typing import Any
    17	
    18	from pydantic import BaseModel, ConfigDict, Field
    19	
    20	# Bump when any field is removed or its semantics change in a breaking way.
    21	# Adding an optional field with a default does NOT require a bump.
    22	SCHEMA_VERSION: int = 1
    23	
    24	
    25	# ---------------------------------------------------------------------------
    26	# Error taxonomy
    27	# ---------------------------------------------------------------------------
    28	
    29	
    30	class ErrorTaxonomy(str, Enum):
    31	    """Structured classification of harness-observed failures.
    32	
    33	    The harness records one of these on every request that does not reach
    34	    FINISHED, giving the dossier a machine-readable failure mode instead of a
    35	    raw exception string.
    36	    """
    37	
    38	    SANDBOX_MISSING_FILE = "SANDBOX_MISSING_FILE"
    39	    SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"
    40	    TOOL_ERROR = "TOOL_ERROR"
    41	    PROVIDER_AUTH = "PROVIDER_AUTH"
    42	    PROVIDER_BUDGET = "PROVIDER_BUDGET"
    43	    TIMEOUT = "TIMEOUT"
    44	    UNCAUGHT_TASK_EXCEPTION = "UNCAUGHT_TASK_EXCEPTION"
    45	    USER_CANCELLED = "USER_CANCELLED"
    46	    VALIDATION_ERROR = "VALIDATION_ERROR"
    47	    UNKNOWN = "UNKNOWN"
    48	
    49	
    50	# ---------------------------------------------------------------------------
    51	# Redaction
    52	# ---------------------------------------------------------------------------
    53	
    54	# Regex fragments matched (case-insensitively) against dict key names.
    55	# A key whose normalised name matches any pattern has its value replaced with
    56	# _REDACTED before any serialised evidence is written to disk or uploaded.
    57	# Extend this list to cover any new secret-bearing header or config field.
    58	REDACTION_KEY_PATTERNS: list[str] = [
    59	    r"api[_\-]?key",        # api_key, apiKey, DISCO_API_KEY, …
    60	    r"secret",              # secret, DISCO_SECRET_KEY, client_secret, …
    61	    r"token",               # token, access_token, refresh_token, …
    62	    r"password",            # password, passwd, …
    63	    r"auth(?:orization)?",  # auth, authorization, Authorization, …
    64	    r"cookie",              # cookie, set-cookie, Set-Cookie, …
    65	    r"bearer",              # bearer (Authorization: Bearer …)
    66	]
    67	
    68	_COMPILED: list[re.Pattern[str]] = [
    69	    re.compile(p, re.IGNORECASE) for p in REDACTION_KEY_PATTERNS
    70	]
    71	
    72	_REDACTED = "***REDACTED***"
    73	
    74	
    75	def _key_is_sensitive(key: str) -> bool:
    76	    return any(pat.search(key) for pat in _COMPILED)
    77	
    78	
    79	def redact(obj: Any) -> Any:
    80	    """Deep-traverse *obj* and replace sensitive-key values with ``'***REDACTED***'``.
    81	
    82	    Sensitive keys are identified by case-insensitive match against
    83	    ``REDACTION_KEY_PATTERNS``. The entire value (including any nested
    84	    structure) is replaced — a nested dict under a sensitive key is not
    85	    preserved.
    86	
    87	    Handles arbitrarily nested dicts and lists. Scalars are returned as-is
    88	    (they carry no key context and are therefore safe by default).
    89	    """
    90	    if isinstance(obj, dict):
    91	        result: dict[Any, Any] = {}
    92	        for k, v in obj.items():
    93	            if isinstance(k, str) and _key_is_sensitive(k):
    94	                result[k] = _REDACTED
    95	            else:
    96	                result[k] = redact(v)
    97	        return result
    98	    if isinstance(obj, list):
    99	        return [redact(item) for item in obj]
   100	    # int, float, str, bool, None, datetime, … — immutable, safe as-is.
   101	    return obj
   102	
   103	
   104	# ---------------------------------------------------------------------------
   105	# W3C Trace Context helpers (§3.2)
   106	# ---------------------------------------------------------------------------
   107	
   108	_HEX32 = re.compile(r"^[0-9a-f]{32}$")
   109	_HEX16 = re.compile(r"^[0-9a-f]{16}$")
   110	
   111	
   112	def make_traceparent(trace_id: str, span_id: str) -> str:
   113	    """Return a W3C traceparent header value: ``00-<32hex>-<16hex>-01``.
   114	
   115	    Both *trace_id* and *span_id* must be lowercase hex strings of the
   116	    correct length; no validation is performed here — the caller is
   117	    responsible for supplying valid ids (e.g. from ``uuid.uuid4().hex``).
   118	    """
   119	    return f"00-{trace_id}-{span_id}-01"
   120	
   121	
   122	def parse_traceparent(s: str) -> tuple[str, str] | None:
   123	    """Parse a W3C traceparent string into ``(trace_id, span_id)``.
   124	
   125	    Returns ``None`` if *s* is not a valid ``00-<32hex>-<16hex>-<flags>``
   126	    header value.  Only version ``"00"`` is accepted; unknown versions are
   127	    treated as invalid.
   128	    """
   129	    parts = s.strip().split("-")
   130	    if len(parts) != 4:
   131	        return None
   132	    version, trace_id, span_id, _flags = parts
   133	    if version != "00":
   134	        return None
   135	    if not _HEX32.match(trace_id):
   136	        return None
   137	    if not _HEX16.match(span_id):
   138	        return None
   139	    return trace_id, span_id
   140	
   141	
   142	# ---------------------------------------------------------------------------
   143	# Canonical evidence record
   144	# ---------------------------------------------------------------------------
   145	
   146	
   147	class EvidenceRecord(BaseModel):
   148	    """Canonical correlation record for one harness-instrumented interaction.
   149	
   150	    This model is stamped into the ``meta`` dict of every event emitted while
   151	    ``DISCO_E2E=1`` is set, and is stored as the root object of each
   152	    ``ui-actions.jsonl`` entry.  It is the schema-level source of truth; HTTP
   153	    and WS wiring that *propagates* these ids is a separate work item (W5+).
   154	
   155	    ``frozen=True`` makes instances immutable at the type level, matching the
   156	    convention established in ``events.py`` (``BaseEvent``).
   157	    """
   158	
   159	    model_config = ConfigDict(frozen=True, extra="forbid")
   160	
   161	    schema_version: int = Field(default=SCHEMA_VERSION)
   162	
   163	    # The id assigned to this harness run (e.g. ``run_<timestamp>_<hex>``).
   164	    # Stamped on every HTTP request and WS frame in the run so the dossier
   165	    # can be reconstructed from any layer of the stack.
   166	    harness_run_id: str
   167	
   168	    # W3C Trace Context — None when the caller does not supply a traceparent.
   169	    trace_id: str | None = None
   170	    span_id: str | None = None
   171	
   172	    # App-level correlation ids extracted from the HTTP response or WS payload.
   173	    request_id: str | None = None
   174	    conversation_id: str | None = None
   175	
   176	    # The id of the ``disco.click()`` action that triggered this observation.
   177	    # Links the event-log entry back to the specific UI gesture in
   178	    # ``ui-actions.jsonl``.
   179	    ui_action_id: str | None = None
   180	
   181	    # Artifact ids produced or observed as a result of this interaction
   182	    # (deliverable ids from DeliverableEvent, download filenames, …).
   183	    artifact_ids: list[str] = Field(default_factory=list)

exec
/bin/bash -lc "nl -ba packages/core/tests/test_evidence_schema.py | sed -n '1,240p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""Tests for disco.core.evidence.schema — W4 evidence/correlation schema.
     2	
     3	Covers:
     4	  1. EvidenceRecord round-trips (model_validate / model_dump)
     5	  2. redact() — nested sensitive-key replacement, non-sensitive keys untouched
     6	  3. make_traceparent / parse_traceparent round-trips and rejection cases
     7	  4. ErrorTaxonomy — exhaustiveness / value shapes
     8	"""
     9	
    10	from __future__ import annotations
    11	
    12	import uuid
    13	
    14	import pytest
    15	from disco.core.evidence.schema import (
    16	    REDACTION_KEY_PATTERNS,
    17	    SCHEMA_VERSION,
    18	    ErrorTaxonomy,
    19	    EvidenceRecord,
    20	    make_traceparent,
    21	    parse_traceparent,
    22	    redact,
    23	)
    24	from pydantic import ValidationError
    25	
    26	# ---------------------------------------------------------------------------
    27	# 1. EvidenceRecord round-trips
    28	# ---------------------------------------------------------------------------
    29	
    30	
    31	def _run_id() -> str:
    32	    return f"run_{uuid.uuid4().hex[:12]}"
    33	
    34	
    35	def test_evidence_record_minimal_round_trip() -> None:
    36	    """Only the required field (harness_run_id) is supplied."""
    37	    rec = EvidenceRecord(harness_run_id="run_abc123")
    38	    dumped = rec.model_dump()
    39	    restored = EvidenceRecord.model_validate(dumped)
    40	    assert restored == rec
    41	    assert restored.schema_version == SCHEMA_VERSION
    42	    assert restored.trace_id is None
    43	    assert restored.artifact_ids == []
    44	
    45	
    46	def test_evidence_record_full_round_trip() -> None:
    47	    """All fields populated; round-trip preserves values exactly."""
    48	    run_id = _run_id()
    49	    trace_id = uuid.uuid4().hex  # uuid4().hex = 32 lowercase hex chars
    50	    span_id = uuid.uuid4().hex[:16]  # 16 hex
    51	    rec = EvidenceRecord(
    52	        harness_run_id=run_id,
    53	        trace_id=trace_id,
    54	        span_id=span_id,
    55	        request_id="req_001",
    56	        conversation_id="cid_001",
    57	        ui_action_id="act_001",
    58	        artifact_ids=["art_a", "art_b"],
    59	    )
    60	    dumped = rec.model_dump()
    61	    restored = EvidenceRecord.model_validate(dumped)
    62	    assert restored == rec
    63	    assert restored.trace_id == trace_id
    64	    assert restored.span_id == span_id
    65	    assert restored.artifact_ids == ["art_a", "art_b"]
    66	
    67	
    68	def test_evidence_record_schema_version_default() -> None:
    69	    rec = EvidenceRecord(harness_run_id="run_x")
    70	    assert rec.schema_version == 1
    71	
    72	
    73	def test_evidence_record_extra_fields_forbidden() -> None:
    74	    """extra='forbid' — unknown fields must raise ValidationError."""
    75	    with pytest.raises(ValidationError):
    76	        EvidenceRecord.model_validate({"harness_run_id": "r", "unknown_field": True})
    77	
    78	
    79	def test_evidence_record_json_round_trip() -> None:
    80	    """model_dump(mode='json') / model_validate round-trip stays consistent."""
    81	    rec = EvidenceRecord(harness_run_id="run_json", artifact_ids=["x"])
    82	    as_json_dict = rec.model_dump(mode="json")
    83	    restored = EvidenceRecord.model_validate(as_json_dict)
    84	    assert restored == rec
    85	
    86	
    87	# ---------------------------------------------------------------------------
    88	# 2. redact()
    89	# ---------------------------------------------------------------------------
    90	
    91	
    92	def test_redact_flat_dict_sensitive_key() -> None:
    93	    data = {"api_key": "sk-secret-value", "model": "gpt-5"}
    94	    result = redact(data)
    95	    assert isinstance(result, dict)
    96	    assert result["api_key"] == "***REDACTED***"
    97	    assert result["model"] == "gpt-5"
    98	
    99	
   100	def test_redact_nested_dict() -> None:
   101	    data = {
   102	        "config": {
   103	            "provider": "openrouter",
   104	            "token": "tok_abc",
   105	            "timeout": 30,
   106	        }
   107	    }
   108	    result = redact(data)
   109	    assert isinstance(result, dict)
   110	    assert isinstance(result["config"], dict)
   111	    assert result["config"]["token"] == "***REDACTED***"
   112	    assert result["config"]["provider"] == "openrouter"
   113	    assert result["config"]["timeout"] == 30
   114	
   115	
   116	def test_redact_nested_value_under_sensitive_key_is_fully_replaced() -> None:
   117	    """Even a nested dict under a sensitive key is replaced, not traversed."""
   118	    data = {"secret": {"nested_key": "should-not-appear", "another": 42}}
   119	    result = redact(data)
   120	    assert isinstance(result, dict)
   121	    assert result["secret"] == "***REDACTED***"
   122	
   123	
   124	def test_redact_list_traversed() -> None:
   125	    data = [{"password": "hunter2", "user": "alice"}, {"normal": "value"}]
   126	    result = redact(data)
   127	    assert isinstance(result, list)
   128	    assert result[0]["password"] == "***REDACTED***"
   129	    assert result[0]["user"] == "alice"
   130	    assert result[1]["normal"] == "value"
   131	
   132	
   133	def test_redact_case_insensitive() -> None:
   134	    """Keys are matched case-insensitively."""
   135	    data = {
   136	        "API_KEY": "upper",
   137	        "Authorization": "Bearer tok",
   138	        "COOKIE": "sid=abc",
   139	        "disco_secret_key": "shhh",
   140	        "BEARER": "raw-bearer",
   141	    }
   142	    result = redact(data)
   143	    assert isinstance(result, dict)
   144	    for k in data:
   145	        assert result[k] == "***REDACTED***", f"expected {k} to be redacted"
   146	
   147	
   148	def test_redact_non_sensitive_keys_untouched() -> None:
   149	    data = {
   150	        "model": "qwen",
   151	        "surface": "build",
   152	        "conversation_id": "cid_123",
   153	        "artifact_ids": ["a", "b"],
   154	        "count": 42,
   155	        "enabled": True,
   156	        "nothing": None,
   157	    }
   158	    result = redact(data)
   159	    assert result == data
   160	
   161	
   162	def test_redact_scalar_passthrough() -> None:
   163	    assert redact("hello") == "hello"
   164	    assert redact(42) == 42
   165	    assert redact(None) is None
   166	    assert redact(True) is True
   167	
   168	
   169	def test_redact_deeply_nested() -> None:
   170	    data = {"a": {"b": {"c": {"api_key": "deep-secret", "safe": "ok"}}}}
   171	    result = redact(data)
   172	    assert isinstance(result, dict)
   173	    assert result["a"]["b"]["c"]["api_key"] == "***REDACTED***"
   174	    assert result["a"]["b"]["c"]["safe"] == "ok"
   175	
   176	
   177	def test_redact_list_of_lists() -> None:
   178	    data = [["no_match", {"token": "tok"}]]
   179	    result = redact(data)
   180	    assert isinstance(result, list)
   181	    assert result[0][1]["token"] == "***REDACTED***"
   182	
   183	
   184	def test_redaction_key_patterns_non_empty() -> None:
   185	    assert len(REDACTION_KEY_PATTERNS) >= 5, "expected at least 5 redaction patterns"
   186	
   187	
   188	# ---------------------------------------------------------------------------
   189	# 3. make_traceparent / parse_traceparent
   190	# ---------------------------------------------------------------------------
   191	
   192	
   193	def _valid_trace_id() -> str:
   194	    return uuid.uuid4().hex  # uuid4().hex is already 32 lowercase hex chars
   195	
   196	
   197	def _valid_span_id() -> str:
   198	    return uuid.uuid4().hex[:16]  # 16 lowercase hex chars
   199	
   200	
   201	def test_make_traceparent_format() -> None:
   202	    trace_id = _valid_trace_id()
   203	    span_id = _valid_span_id()
   204	    tp = make_traceparent(trace_id, span_id)
   205	    assert tp == f"00-{trace_id}-{span_id}-01"
   206	
   207	
   208	def test_parse_traceparent_round_trip() -> None:
   209	    trace_id = _valid_trace_id()
   210	    span_id = _valid_span_id()
   211	    tp = make_traceparent(trace_id, span_id)
   212	    parsed = parse_traceparent(tp)
   213	    assert parsed is not None
   214	    got_trace, got_span = parsed
   215	    assert got_trace == trace_id
   216	    assert got_span == span_id
   217	
   218	
   219	def test_parse_traceparent_rejects_invalid_version() -> None:
   220	    trace_id = _valid_trace_id()
   221	    span_id = _valid_span_id()
   222	    tp = f"01-{trace_id}-{span_id}-01"
   223	    assert parse_traceparent(tp) is None
   224	
   225	
   226	def test_parse_traceparent_rejects_short_trace_id() -> None:
   227	    # 31 chars instead of 32
   228	    trace_id = "a" * 31
   229	    span_id = _valid_span_id()
   230	    tp = f"00-{trace_id}-{span_id}-01"
   231	    assert parse_traceparent(tp) is None
   232	
   233	
   234	def test_parse_traceparent_rejects_short_span_id() -> None:
   235	    trace_id = _valid_trace_id()
   236	    span_id = "b" * 15  # 15 instead of 16
   237	    tp = f"00-{trace_id}-{span_id}-01"
   238	    assert parse_traceparent(tp) is None
   239	
   240	

exec
/bin/bash -lc "nl -ba packages/agent-server/src/disco/agent_server/routes/debug.py | sed -n '1,180p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""DISCO_INSPECT debug routes — read the per-conversation request trace.
     2	
     3	These exist to PROVE the request chain end-to-end (UI → loop → router → provider
     4	→ back) without grepping prod logs. They are inert unless ``DISCO_INSPECT=1``:
     5	with the flag off the trace registry is never populated and these return 404 with
     6	a hint, so the surface can't leak routing internals in a default deployment.
     7	"""
     8	
     9	from __future__ import annotations
    10	
    11	from typing import Any
    12	
    13	from disco.core.evidence.schema import redact
    14	from disco.core.inspect import inspect_enabled, registry
    15	from disco.core.store.sqlite import SqliteEventStore
    16	from fastapi import APIRouter
    17	from fastapi.responses import JSONResponse
    18	
    19	from ..runtime import ConversationRuntime
    20	
    21	
    22	def make_debug_router(
    23	    store: SqliteEventStore, runtime: ConversationRuntime | None
    24	) -> APIRouter:
    25	    router = APIRouter()
    26	
    27	    def _disabled() -> JSONResponse:
    28	        return JSONResponse(
    29	            {
    30	                "error": "inspect disabled",
    31	                "hint": "start the agent-server with DISCO_INSPECT=1 to capture traces",
    32	            },
    33	            status_code=404,
    34	        )
    35	
    36	    @router.get("/api/debug/inspect")
    37	    async def inspect_status() -> JSONResponse:
    38	        """Is inspect on, and which conversations currently have a trace?"""
    39	        if not inspect_enabled():
    40	            return _disabled()
    41	        return JSONResponse(
    42	            {"enabled": True, "conversations": registry().conversations()}
    43	        )
    44	
    45	    @router.get("/api/debug/trace/{conversation_id}")
    46	    async def trace(conversation_id: str) -> JSONResponse:
    47	        """The full interleaved routing+span trace for one conversation.
    48	
    49	        404 when inspect is off, or when no trace exists yet for the id (the
    50	        conversation hasn't made a model call, or it aged out of the ring)."""
    51	        if not inspect_enabled():
    52	            return _disabled()
    53	        snap = registry().snapshot(conversation_id)
    54	        if snap is None:
    55	            return JSONResponse(
    56	                {"error": "no trace", "conversation_id": conversation_id},
    57	                status_code=404,
    58	            )
    59	        return JSONResponse(snap)
    60	
    61	    @router.get("/api/debug/evidence/{conversation_id}")
    62	    async def evidence(conversation_id: str) -> JSONResponse:
    63	        """Bundled, redacted evidence for one conversation: state, event log,
    64	        inspect trace, and project manifest (metadata only — no file contents).
    65	
    66	        Gated behind the same ``DISCO_INSPECT=1`` flag as ``/api/debug/trace``.
    67	        Returns 404 with a clear hint when the flag is off so the endpoint is
    68	        completely inert in a default deployment.
    69	
    70	        The entire bundle is passed through :func:`disco.core.evidence.schema.redact`
    71	        before serialisation — any dict key matching a secret pattern (api_key,
    72	        token, secret, …) has its value replaced with ``'***REDACTED***'``.
    73	
    74	        Path-traversal safety: FastAPI's ``{conversation_id}`` path segment does
    75	        not permit ``/`` or ``..``. No workspace files or arbitrary paths are read;
    76	        only structured store data is returned."""
    77	        if not inspect_enabled():
    78	            return _disabled()
    79	
    80	        # Conversation state (reconstructed from the append-only event log).
    81	        state_dict: dict[str, Any] = (
    82	            await store.get_state(conversation_id)
    83	        ).model_dump(mode="json")
    84	
    85	        # Full event log, each event serialised to its JSON-ready dict form.
    86	        events_list: list[dict[str, Any]] = [
    87	            e.model_dump(mode="json")
    88	            for e in await store.get_events(conversation_id)
    89	        ]
    90	
    91	        # Inspect trace — may be None when inspect is on but no model call has
    92	        # been captured yet for this conversation (or it aged out of the ring).
    93	        inspect_trace: dict[str, Any] | None = registry().snapshot(conversation_id)
    94	
    95	        # Project manifest — metadata only (title, dates, counts, last deliverable).
    96	        # Workspace file paths and content are intentionally excluded to eliminate
    97	        # any path-traversal or workspace-content leakage risk.
    98	        project_manifest: dict[str, Any] | None = None
    99	        if runtime is not None:
   100	            ps_method = getattr(runtime, "project_store", None)
   101	            if ps_method is not None:
   102	                ps = ps_method()
   103	                if ps is not None:
   104	                    record = ps.get(conversation_id)
   105	                    if record is not None:
   106	                        project_manifest = {
   107	                            "conversation_id": conversation_id,
   108	                            "title": record.title or "(untitled)",
   109	                            "created_at": record.created_at,
   110	                            "last_snapshot_at": record.last_snapshot_at,
   111	                            "file_count": record.file_count,
   112	                            "total_bytes": record.total_bytes,
   113	                        }
   114	
   115	        bundle: dict[str, Any] = {
   116	            "conversation_id": conversation_id,
   117	            "state": state_dict,
   118	            "events": events_list,
   119	            "inspect_trace": inspect_trace,
   120	            "project_manifest": project_manifest,
   121	        }
   122	
   123	        # Redact sensitive keys before any bytes leave the process.
   124	        return JSONResponse(redact(bundle))
   125	
   126	    return router

exec
/bin/bash -lc "nl -ba packages/agent-server/tests/test_debug_evidence_route.py | sed -n '1,180p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""Tests for GET /api/debug/evidence/{conversation_id} — evidence-harness-campaign.md W5.
     2	
     3	Three assertions:
     4	
     5	1. Route is inert (404) when ``DISCO_INSPECT`` is off — the endpoint must never
     6	   expose conversation internals in a default deployment.
     7	2. When ``DISCO_INSPECT=1``, the endpoint returns the expected four-key evidence
     8	   bundle (state / events / inspect_trace / project_manifest).
     9	3. A secret planted in an event's ``meta`` dict is replaced with
    10	   ``'***REDACTED***'`` in the response — keys never leak through this surface.
    11	
    12	All tests drive the real ASGI app via ``create_app`` + ``TestClient``, matching
    13	the pattern established by ``test_health.py`` and ``test_activity.py``.
    14	"""
    15	
    16	from __future__ import annotations
    17	
    18	import asyncio
    19	import json
    20	
    21	from disco.agent_server import create_app
    22	from disco.core import EventSource, LLMMessage, MessageEvent, SqliteEventStore
    23	from fastapi.testclient import TestClient
    24	
    25	# ---------------------------------------------------------------------------
    26	# Helpers
    27	# ---------------------------------------------------------------------------
    28	
    29	
    30	def _seeded_store(cid: str, *, meta: dict | None = None) -> SqliteEventStore:
    31	    """Return an in-memory store pre-populated with *cid* and one user message.
    32	
    33	    ``meta`` is forwarded to the event's ``meta`` dict so tests can plant
    34	    secrets there.  The ``_write_lock`` is reset after the synchronous
    35	    bootstrap so TestClient's internal event loop can acquire it cleanly."""
    36	    store = SqliteEventStore(":memory:")
    37	    store.create_conversation(cid, owner_id="local")
    38	    event = MessageEvent(
    39	        source=EventSource.USER,
    40	        message=LLMMessage(role="user", content="hello"),
    41	        meta=meta or {},
    42	    )
    43	    # store.append is async; asyncio.run bootstraps a temporary loop.
    44	    asyncio.run(store.append(cid, event))
    45	    # The write-lock was acquired by the temporary loop above.  Replace it so
    46	    # TestClient's ASGI event loop can acquire it without a "wrong loop" error.
    47	    store._write_lock = asyncio.Lock()
    48	    return store
    49	
    50	
    51	# ---------------------------------------------------------------------------
    52	# 1. Flag off — route is inert
    53	# ---------------------------------------------------------------------------
    54	
    55	
    56	def test_evidence_inert_when_inspect_off(monkeypatch: object) -> None:
    57	    """Without DISCO_INSPECT=1, the route returns 404 with a hint, never data."""
    58	    # Strip both the new and legacy env vars so inspect_enabled() is False.
    59	    monkeypatch.delenv("DISCO_INSPECT", raising=False)  # type: ignore[attr-defined]
    60	    monkeypatch.delenv("PMX_INSPECT", raising=False)  # type: ignore[attr-defined]
    61	
    62	    store = SqliteEventStore(":memory:")
    63	    client = TestClient(create_app(store, runtime=None))
    64	    resp = client.get("/api/debug/evidence/any-cid")
    65	
    66	    assert resp.status_code == 404
    67	    body = resp.json()
    68	    assert body.get("error") == "inspect disabled"
    69	    # The hint must name the flag so an operator knows exactly what to enable.
    70	    assert "DISCO_INSPECT=1" in body.get("hint", "")
    71	
    72	
    73	# ---------------------------------------------------------------------------
    74	# 2. Flag on — returns the four-key bundle
    75	# ---------------------------------------------------------------------------
    76	
    77	
    78	def test_evidence_returns_bundle_when_inspect_on(monkeypatch: object) -> None:
    79	    """With DISCO_INSPECT=1, the endpoint returns the full evidence bundle."""
    80	    monkeypatch.setenv("DISCO_INSPECT", "1")  # type: ignore[attr-defined]
    81	
    82	    cid = "conv_ev_bundle_01"
    83	    store = _seeded_store(cid)
    84	    client = TestClient(create_app(store, runtime=None))
    85	    resp = client.get(f"/api/debug/evidence/{cid}")
    86	
    87	    assert resp.status_code == 200
    88	    body = resp.json()
    89	
    90	    # Top-level keys the harness depends on.
    91	    assert body["conversation_id"] == cid
    92	    assert "state" in body, "bundle must include conversation state"
    93	    assert "events" in body, "bundle must include the event log"
    94	    assert "inspect_trace" in body, "bundle must include the inspect trace (may be null)"
    95	    assert "project_manifest" in body, "bundle must include the project manifest (may be null)"
    96	
    97	    # The seeded user message must appear in the event list.
    98	    assert isinstance(body["events"], list)
    99	    assert len(body["events"]) >= 1, "at least the seeded event must be present"
   100	
   101	
   102	# ---------------------------------------------------------------------------
   103	# 3. Planted secret is REDACTED
   104	# ---------------------------------------------------------------------------
   105	
   106	
   107	def test_evidence_redacts_secret_in_event_meta(monkeypatch: object) -> None:
   108	    """A secret key in an event's meta is replaced; the raw value must not leak."""
   109	    monkeypatch.setenv("DISCO_INSPECT", "1")  # type: ignore[attr-defined]
   110	
   111	    secret_value = "sk-super-secret-9999"
   112	    cid = "conv_ev_secret_01"
   113	    store = _seeded_store(cid, meta={"api_key": secret_value})
   114	    client = TestClient(create_app(store, runtime=None))
   115	    resp = client.get(f"/api/debug/evidence/{cid}")
   116	
   117	    assert resp.status_code == 200
   118	    body = resp.json()
   119	
   120	    # The raw secret must not appear anywhere in the serialised response body.
   121	    raw_response = json.dumps(body)
   122	    assert secret_value not in raw_response, (
   123	        f"secret '{secret_value}' must not appear in the response"
   124	    )
   125	
   126	    # The redaction marker must be present at the correct location.
   127	    events = body["events"]
   128	    assert len(events) >= 1
   129	    assert events[0]["meta"]["api_key"] == "***REDACTED***", (
   130	        "api_key in event meta must be replaced with ***REDACTED***"
   131	    )

codex
Redaction handles nested dicts and lists, but only those container types. I need to check how the evidence endpoint serializes objects; if it always converts to JSON-ready dict/list first, the remaining issue is mainly reuse of `redact()` elsewhere on sets/tuples/model instances.
exec
/bin/bash -lc "nl -ba packages/core/src/disco/core/evidence/schema.py | sed -n '183,260p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   183	    artifact_ids: list[str] = Field(default_factory=list)

exec
/bin/bash -lc "nl -ba packages/core/src/disco/core/inspect.py | sed -n '1,260p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""DISCO_INSPECT — per-conversation request trace for end-to-end provability.
     2	
     3	The testing rule is "prove the process works from the UI all the way to the other
     4	end of the application". This module is the machinery for the *other end*: behind
     5	``DISCO_INSPECT=1`` the runtime captures, per conversation, the full chain of
     6	
     7	  (a) ROUTING decisions — which model handled each role, the path (pinned/manual/
     8	      overflow), retry count, terminal failures — via a real :class:`RoutingSink`
     9	      bound to the conversation, and
    10	  (b) timed SPANS from :mod:`disco.core.obs` (``agent.step`` + its measured
    11	      token/finish fields) via a logging handler on the ``disco.span`` logger.
    12	
    13	A REST snapshot endpoint (agent-server ``/api/debug/trace/{cid}``) exposes the
    14	trace so a test — or a developer — can PROVE a request travelled UI → loop →
    15	router → provider → back, without grepping prod logs.
    16	
    17	Zero cost when off: :func:`inspect_enabled` is ``False``, the runtime installs no
    18	handler and gives the router a ``NullRoutingSink``, so neither seam allocates
    19	anything. Bounded when on: a ring of the most-recent ``_MAX_CONVERSATIONS`` traces,
    20	each capped at ``_MAX_EVENTS`` events, so a long-running server (or a runaway loop)
    21	can't grow the buffer without limit.
    22	"""
    23	
    24	from __future__ import annotations
    25	
    26	import logging
    27	import threading
    28	from collections import OrderedDict, deque
    29	from dataclasses import dataclass
    30	from typing import Any
    31	
    32	from .env import disco_env
    33	from .llm.routing import RoutingSink
    34	from .llm.types import RoutingDecision
    35	
    36	# Ring bounds. Inspect is a developer/test affordance, not durable storage — keep
    37	# the most-recent conversations and cap per-conversation depth so the buffer is
    38	# O(_MAX_CONVERSATIONS * _MAX_EVENTS) regardless of uptime or loop length.
    39	_MAX_CONVERSATIONS = 64
    40	_MAX_EVENTS = 1024
    41	
    42	_TRUTHY = {"1", "true", "yes", "on"}
    43	
    44	
    45	def inspect_enabled() -> bool:
    46	    """True iff ``DISCO_INSPECT`` (or legacy ``PMX_INSPECT``) is set truthy."""
    47	    return (disco_env("INSPECT") or "").strip().lower() in _TRUTHY
    48	
    49	
    50	# ---- trace data --------------------------------------------------------------
    51	
    52	
    53	@dataclass(frozen=True)
    54	class TraceEvent:
    55	    """One entry in a conversation's trace. ``kind`` is ``"routing"`` (a
    56	    RoutingDecision) or ``"span"`` (a disco.span record); ``data`` is the
    57	    JSON-ready field dict. ``seq`` is a global monotonically-increasing counter so
    58	    routing and span events interleave in true emission order in a snapshot."""
    59	
    60	    seq: int
    61	    kind: str
    62	    data: dict[str, Any]
    63	
    64	
    65	class ConversationTrace:
    66	    """The bounded event log for a single conversation."""
    67	
    68	    def __init__(self, conversation_id: str, max_events: int) -> None:
    69	        self.conversation_id = conversation_id
    70	        self.events: deque[TraceEvent] = deque(maxlen=max_events)
    71	
    72	    def snapshot(self) -> dict[str, Any]:
    73	        """A JSON-ready view: the interleaved event stream plus the routing /
    74	        span projections (so a caller can assert on either dimension without
    75	        re-filtering)."""
    76	        evs = list(self.events)
    77	        return {
    78	            "conversation_id": self.conversation_id,
    79	            "event_count": len(evs),
    80	            "routing_decisions": [e.data for e in evs if e.kind == "routing"],
    81	            "spans": [e.data for e in evs if e.kind == "span"],
    82	            "events": [{"seq": e.seq, "kind": e.kind, **e.data} for e in evs],
    83	        }
    84	
    85	
    86	class InspectRegistry:
    87	    """Thread-safe, bounded store of per-conversation traces.
    88	
    89	    Thread-safety matters: span records arrive on whatever thread the logging
    90	    framework flushes on, while routing decisions arrive on the asyncio loop
    91	    thread. A single lock around the (cheap) append keeps the deque consistent.
    92	    """
    93	
    94	    def __init__(
    95	        self,
    96	        max_conversations: int = _MAX_CONVERSATIONS,
    97	        max_events: int = _MAX_EVENTS,
    98	    ) -> None:
    99	        self._lock = threading.Lock()
   100	        self._traces: OrderedDict[str, ConversationTrace] = OrderedDict()
   101	        self._seq = 0
   102	        self._max_conversations = max_conversations
   103	        self._max_events = max_events
   104	
   105	    def _trace_for(self, cid: str) -> ConversationTrace:
   106	        # Caller holds the lock. LRU: touch on access, evict oldest over the cap.
   107	        tr = self._traces.get(cid)
   108	        if tr is None:
   109	            tr = ConversationTrace(cid, self._max_events)
   110	            self._traces[cid] = tr
   111	            while len(self._traces) > self._max_conversations:
   112	                self._traces.popitem(last=False)
   113	        self._traces.move_to_end(cid)
   114	        return tr
   115	
   116	    def add(self, cid: str | None, kind: str, data: dict[str, Any]) -> None:
   117	        """Record one event. A falsy ``cid`` is dropped (an un-attributable span
   118	        is noise, not a trace)."""
   119	        if not cid:
   120	            return
   121	        with self._lock:
   122	            self._seq += 1
   123	            self._trace_for(cid).events.append(TraceEvent(self._seq, kind, data))
   124	
   125	    def snapshot(self, cid: str) -> dict[str, Any] | None:
   126	        with self._lock:
   127	            tr = self._traces.get(cid)
   128	            return tr.snapshot() if tr is not None else None
   129	
   130	    def conversations(self) -> list[str]:
   131	        """Most-recently-touched last (deque/LRU order)."""
   132	        with self._lock:
   133	            return list(self._traces.keys())
   134	
   135	    def clear(self) -> None:
   136	        with self._lock:
   137	            self._traces.clear()
   138	            self._seq = 0
   139	
   140	
   141	# Module-level singleton so the routing sink, the span handler, and the REST
   142	# route all converge on one registry without threading it through constructors.
   143	_REGISTRY: InspectRegistry | None = None
   144	
   145	
   146	def registry() -> InspectRegistry:
   147	    global _REGISTRY
   148	    if _REGISTRY is None:
   149	        _REGISTRY = InspectRegistry()
   150	    return _REGISTRY
   151	
   152	
   153	# ---- routing seam ------------------------------------------------------------
   154	
   155	
   156	def _decision_fields(d: RoutingDecision) -> dict[str, Any]:
   157	    """Flatten a RoutingDecision to the trace's JSON-ready field dict."""
   158	    return {
   159	        "role": d.profile.role.value,
   160	        "chosen_model": d.chosen_model,
   161	        "provider": d.provider,
   162	        "path": d.path,
   163	        "reason": d.reason,
   164	        "attempt": d.attempt,
   165	        "overflow_triggers": list(d.overflow_triggers),
   166	    }
   167	
   168	
   169	class InspectRoutingSink:
   170	    """A :class:`RoutingSink` that records each decision into the inspect registry
   171	    under a FIXED conversation id. The router is built per-conversation (see
   172	    ``ConversationRuntime._loop_for``), so binding the cid at construction is exact
   173	    — every decision this router emits belongs to ``conversation_id``."""
   174	
   175	    def __init__(self, conversation_id: str, reg: InspectRegistry | None = None) -> None:
   176	        self._cid = conversation_id
   177	        self._reg = reg or registry()
   178	
   179	    def record(self, decision: RoutingDecision) -> None:
   180	        self._reg.add(self._cid, "routing", _decision_fields(decision))
   181	
   182	
   183	def routing_sink_for(conversation_id: str | None) -> RoutingSink | None:
   184	    """The sink to hand a per-conversation router: a real inspect sink when
   185	    inspect is enabled and a cid is known, else ``None`` (→ NullRoutingSink)."""
   186	    if conversation_id and inspect_enabled():
   187	        return InspectRoutingSink(conversation_id)
   188	    return None
   189	
   190	
   191	# ---- span seam ---------------------------------------------------------------
   192	
   193	
   194	class _SpanHandler(logging.Handler):
   195	    """Captures ``disco.span`` records into the registry, attributed by the ``cid``
   196	    field that :func:`disco.core.obs.log_span` stamps on each record."""
   197	
   198	    def __init__(self, reg: InspectRegistry) -> None:
   199	        super().__init__()
   200	        self._reg = reg
   201	
   202	    def emit(self, record: logging.LogRecord) -> None:
   203	        fields = getattr(record, "_fields", None)
   204	        if not isinstance(fields, dict):
   205	            return
   206	        cid = fields.get("cid")
   207	        if cid:
   208	            self._reg.add(str(cid), "span", dict(fields))
   209	
   210	
   211	_INSTALLED = False
   212	
   213	
   214	def install(reg: InspectRegistry | None = None) -> InspectRegistry:
   215	    """Attach the span handler to the ``disco.span`` logger (idempotent). The
   216	    runtime calls this at startup ONLY when ``inspect_enabled()`` — when off, no
   217	    handler is ever attached and spans flow only to ordinary logging."""
   218	    global _INSTALLED
   219	    r = reg or registry()
   220	    if not _INSTALLED:
   221	        span_log = logging.getLogger("disco.span")
   222	        # log_span emits at INFO; the logger inherits root's default WARNING unless
   223	        # raised, which would drop every span BEFORE it reached our handler. Pin it
   224	        # to INFO so spans actually flow to the inspect handler (independent of
   225	        # whatever the root handler is configured to show).
   226	        if span_log.level == logging.NOTSET or span_log.level > logging.INFO:
   227	            span_log.setLevel(logging.INFO)
   228	        span_log.addHandler(_SpanHandler(r))
   229	        _INSTALLED = True
   230	    return r

exec
/bin/bash -lc "rg -n \"TraceRoutingSink|registry\\(\\)|record|snapshot|inspect_enabled|DISCO_INSPECT|trace\" packages/core/src/disco/core packages/agent-server/src/disco/agent_server/runtime.py packages/core/src/disco/core/llm -S | head -n 260" in /var/home/dylan/projects/disco
 succeeded in 0ms:
packages/agent-server/src/disco/agent_server/runtime.py:44:from disco.core.inspect import inspect_enabled, routing_sink_for
packages/agent-server/src/disco/agent_server/runtime.py:89:# MCP client pool (RP-05 rung A) — built once at start, snapshotted per conversation.
packages/agent-server/src/disco/agent_server/runtime.py:259:    """True when an approved plan exists but FINISHED was never recorded — the
packages/agent-server/src/disco/agent_server/runtime.py:300:# as the build loop's live workspace snapshot: read the truth, don't assume it.
packages/agent-server/src/disco/agent_server/runtime.py:478:        # _maybe_snapshot (which no-ops without a configured projects_root), so a
packages/agent-server/src/disco/agent_server/runtime.py:543:        # DISCO_INSPECT: attach the span-capture handler once, at construction, so
packages/agent-server/src/disco/agent_server/runtime.py:545:        # traces. Idempotent + no-op when the flag is off.
packages/agent-server/src/disco/agent_server/runtime.py:546:        if inspect_enabled():
packages/agent-server/src/disco/agent_server/runtime.py:610:        # snapshot+rehydrate). _connections/_suspend_tasks stay on the runtime
packages/agent-server/src/disco/agent_server/runtime.py:723:        # DISCO_INSPECT: when on, bind a per-conversation routing sink so every
packages/agent-server/src/disco/agent_server/runtime.py:724:        # RoutingDecision this (per-conversation) router emits lands in the trace.
packages/agent-server/src/disco/agent_server/runtime.py:743:    # workspace snapshot/rehydrate). "build" and "agent" are behaviorally identical;
packages/agent-server/src/disco/agent_server/runtime.py:773:          "agent" from "build" (the project manifest can't — they snapshot
packages/agent-server/src/disco/agent_server/runtime.py:776:        - a project manifest on disk → a build-like surface (Research never snapshots).
packages/agent-server/src/disco/agent_server/runtime.py:1222:                # Mid-run death (transport drop / OOM): restore the last snapshot
packages/agent-server/src/disco/agent_server/runtime.py:1236:            build_default_registry(),
packages/agent-server/src/disco/agent_server/runtime.py:1253:        # snapshot (stdio) AND the HTTP-managed tools (rung B streamable_http).
packages/agent-server/src/disco/agent_server/runtime.py:1254:        # The snapshot is frozen per conversation — list_changed notifications do
packages/agent-server/src/disco/agent_server/runtime.py:1258:            all_mcp_tools.extend(self._mcp_pool.snapshot())
packages/agent-server/src/disco/agent_server/runtime.py:1472:                "It has been recorded as failed; you can retry or adjust the task.",
packages/agent-server/src/disco/agent_server/runtime.py:1481:        - Build → snapshot+rehydrate the workspace as before.
packages/agent-server/src/disco/agent_server/runtime.py:1501:        # storage path is configured AND a prior snapshot exists for this cid,
packages/agent-server/src/disco/agent_server/runtime.py:1517:        # model may have built most of the deliverable before getting stuck); snapshot
packages/agent-server/src/disco/agent_server/runtime.py:1528:            await self._maybe_snapshot(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1677:    async def _maybe_snapshot(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1678:        return await self._lifecycle._maybe_snapshot(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1731:    async def sessions_snapshot(self, conversation_id: str) -> tuple[list[SessionInfo], bool]:
packages/agent-server/src/disco/agent_server/runtime.py:1732:        return await self._sessions.sessions_snapshot(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1736:        return (await self.sessions_snapshot(conversation_id))[0]
packages/core/src/disco/core/llm/prompts.py:202:    "  • `remember(fact, scope?)` — record a durable fact you'll need LATER (a chosen "
packages/core/src/disco/core/llm/prompts.py:223:    "'active' | 'done'), rewriting the whole snapshot (not a delta). Mark the step you're "
packages/core/src/disco/core/llm/prompts.py:344:    "  • `remember(fact)` — record a durable fact you'll need LATER (chosen "
packages/core/src/disco/core/llm/config.py:112:    the agent-server reads it to snapshot/rehydrate Build workspaces. Empty by
packages/core/src/disco/core/llm/config_store.py:162:        new path drives the NEXT snapshot/rehydrate."""
packages/core/src/disco/core/evidence/__init__.py:14:    make_traceparent,
packages/core/src/disco/core/evidence/__init__.py:15:    parse_traceparent,
packages/core/src/disco/core/evidence/__init__.py:24:    "make_traceparent",
packages/core/src/disco/core/evidence/__init__.py:25:    "parse_traceparent",
packages/core/src/disco/core/evidence/schema.py:9:  - W3C Trace Context helpers: ``make_traceparent`` / ``parse_traceparent``.
packages/core/src/disco/core/evidence/schema.py:33:    The harness records one of these on every request that does not reach
packages/core/src/disco/core/evidence/schema.py:112:def make_traceparent(trace_id: str, span_id: str) -> str:
packages/core/src/disco/core/evidence/schema.py:113:    """Return a W3C traceparent header value: ``00-<32hex>-<16hex>-01``.
packages/core/src/disco/core/evidence/schema.py:115:    Both *trace_id* and *span_id* must be lowercase hex strings of the
packages/core/src/disco/core/evidence/schema.py:119:    return f"00-{trace_id}-{span_id}-01"
packages/core/src/disco/core/evidence/schema.py:122:def parse_traceparent(s: str) -> tuple[str, str] | None:
packages/core/src/disco/core/evidence/schema.py:123:    """Parse a W3C traceparent string into ``(trace_id, span_id)``.
packages/core/src/disco/core/evidence/schema.py:132:    version, trace_id, span_id, _flags = parts
packages/core/src/disco/core/evidence/schema.py:135:    if not _HEX32.match(trace_id):
packages/core/src/disco/core/evidence/schema.py:139:    return trace_id, span_id
packages/core/src/disco/core/evidence/schema.py:143:# Canonical evidence record
packages/core/src/disco/core/evidence/schema.py:148:    """Canonical correlation record for one harness-instrumented interaction.
packages/core/src/disco/core/evidence/schema.py:168:    # W3C Trace Context — None when the caller does not supply a traceparent.
packages/core/src/disco/core/evidence/schema.py:169:    trace_id: str | None = None
packages/core/src/disco/core/view.py:5:`CondensationEvent` tombstone recording a span to drop and a summary to insert.
packages/core/src/disco/core/view.py:142:    that records a seq range + a summary. `View.of` uses the range to FILTER
packages/core/src/disco/core/view.py:189:    `update_plan_progress({steps:[{index,state}...]})` full-state snapshots (capable
packages/core/src/disco/core/view.py:199:    checklist). Marks/snapshots apply in event order — by `seq`, falling back to LIST
packages/core/src/disco/core/view.py:202:    `update_plan_progress` applies the states it LISTS; indices a snapshot omits RETAIN
packages/core/src/disco/core/view.py:203:    their prior known state (a partial snapshot never silently un-completes an omitted
packages/core/src/disco/core/view.py:607:        lines.append("Standing memory (recorded this run):")
packages/core/src/disco/core/wire.py:39:    # type == "state": a ConversationState snapshot (on connect + on change).
packages/core/src/disco/core/llm/routing.py:104:    def record(self, decision: RoutingDecision) -> None: ...
packages/core/src/disco/core/llm/routing.py:110:    def record(self, decision: RoutingDecision) -> None:  # noqa: D102
packages/core/src/disco/core/llm/routing.py:120:    def record(self, decision: RoutingDecision) -> None:
packages/core/src/disco/core/llm/routing.py:238:            self._sink.record(
packages/core/src/disco/core/llm/routing.py:261:                self._sink.record(
packages/core/src/disco/core/llm/routing.py:337:            self._record_failure(
packages/core/src/disco/core/llm/routing.py:359:                self._record_failure(req, entry, path, reason, exc)
packages/core/src/disco/core/llm/routing.py:364:                    self._record_failure(req, entry, path, reason, None)
packages/core/src/disco/core/llm/routing.py:378:                self._sink.record(decision)
packages/core/src/disco/core/llm/routing.py:411:            recorded = False
packages/core/src/disco/core/llm/routing.py:416:                        if not recorded:
packages/core/src/disco/core/llm/routing.py:417:                            self._sink.record(decision)
packages/core/src/disco/core/llm/routing.py:419:                            recorded = True
packages/core/src/disco/core/llm/routing.py:427:                self._record_failure(req, entry, path, reason, exc)
packages/core/src/disco/core/llm/routing.py:431:                    self._record_failure(req, entry, path, reason, None)
packages/core/src/disco/core/llm/routing.py:438:    def _record_failure(
packages/core/src/disco/core/llm/routing.py:447:        self._sink.record(
packages/core/src/disco/core/llm/routing.py:480:#         self._sink.record(RoutingDecision(profile=profile, chosen_model="",
packages/core/src/disco/core/llm/routing.py:498:#                 self._sink.record(RoutingDecision(... "hard budget cap reached; "
packages/core/src/disco/core/events.py:293:# is reworded to point at the live CURRENT WORKSPACE snapshot (never "use
packages/core/src/disco/core/events.py:375:    """A TOMBSTONE. Marks a span of prior events as forgotten and records the
packages/core/src/disco/core/obs.py:1:"""Lightweight structured observability — the substrate for trace-based assertions
packages/core/src/disco/core/obs.py:5:`log_span()` context manager that emits start/end records carrying structured
packages/core/src/disco/core/obs.py:27:    """One JSON object per log record; structured fields ride on `record._fields`."""
packages/core/src/disco/core/obs.py:29:    def format(self, record: logging.LogRecord) -> str:
packages/core/src/disco/core/obs.py:31:            "level": record.levelname,
packages/core/src/disco/core/obs.py:32:            "logger": record.name,
packages/core/src/disco/core/obs.py:33:            "msg": record.getMessage(),
packages/core/src/disco/core/obs.py:35:        fields = getattr(record, "_fields", None)
packages/core/src/disco/core/obs.py:59:    """Time a span; emit `start` then `end` (with duration_ms) structured records.
packages/core/src/disco/core/obs.py:61:    that land on the `end` record:
packages/core/src/disco/core/state.py:54:    # id — lets a reconnecting surface resolve the question from a state snapshot.
packages/core/src/disco/core/brand/tokens.py:20:    """One resolved brand theme (a snapshot of all tokens + font stacks).
packages/core/src/disco/core/security/assessment.py:1:"""The RiskAssessment record + the SecurityAnalyzer protocol — contract §3.
packages/core/src/disco/core/loop/recitation.py:238:        # post-resume one-shot we record (at count 0) so a step that
packages/core/src/disco/core/inspect.py:1:"""DISCO_INSPECT — per-conversation request trace for end-to-end provability.
packages/core/src/disco/core/inspect.py:5:``DISCO_INSPECT=1`` the runtime captures, per conversation, the full chain of
packages/core/src/disco/core/inspect.py:13:A REST snapshot endpoint (agent-server ``/api/debug/trace/{cid}``) exposes the
packages/core/src/disco/core/inspect.py:14:trace so a test — or a developer — can PROVE a request travelled UI → loop →
packages/core/src/disco/core/inspect.py:17:Zero cost when off: :func:`inspect_enabled` is ``False``, the runtime installs no
packages/core/src/disco/core/inspect.py:19:anything. Bounded when on: a ring of the most-recent ``_MAX_CONVERSATIONS`` traces,
packages/core/src/disco/core/inspect.py:45:def inspect_enabled() -> bool:
packages/core/src/disco/core/inspect.py:46:    """True iff ``DISCO_INSPECT`` (or legacy ``PMX_INSPECT``) is set truthy."""
packages/core/src/disco/core/inspect.py:50:# ---- trace data --------------------------------------------------------------
packages/core/src/disco/core/inspect.py:55:    """One entry in a conversation's trace. ``kind`` is ``"routing"`` (a
packages/core/src/disco/core/inspect.py:56:    RoutingDecision) or ``"span"`` (a disco.span record); ``data`` is the
packages/core/src/disco/core/inspect.py:58:    routing and span events interleave in true emission order in a snapshot."""
packages/core/src/disco/core/inspect.py:72:    def snapshot(self) -> dict[str, Any]:
packages/core/src/disco/core/inspect.py:87:    """Thread-safe, bounded store of per-conversation traces.
packages/core/src/disco/core/inspect.py:89:    Thread-safety matters: span records arrive on whatever thread the logging
packages/core/src/disco/core/inspect.py:100:        self._traces: OrderedDict[str, ConversationTrace] = OrderedDict()
packages/core/src/disco/core/inspect.py:105:    def _trace_for(self, cid: str) -> ConversationTrace:
packages/core/src/disco/core/inspect.py:107:        tr = self._traces.get(cid)
packages/core/src/disco/core/inspect.py:110:            self._traces[cid] = tr
packages/core/src/disco/core/inspect.py:111:            while len(self._traces) > self._max_conversations:
packages/core/src/disco/core/inspect.py:112:                self._traces.popitem(last=False)
packages/core/src/disco/core/inspect.py:113:        self._traces.move_to_end(cid)
packages/core/src/disco/core/inspect.py:118:        is noise, not a trace)."""
packages/core/src/disco/core/inspect.py:123:            self._trace_for(cid).events.append(TraceEvent(self._seq, kind, data))
packages/core/src/disco/core/inspect.py:125:    def snapshot(self, cid: str) -> dict[str, Any] | None:
packages/core/src/disco/core/inspect.py:127:            tr = self._traces.get(cid)
packages/core/src/disco/core/inspect.py:128:            return tr.snapshot() if tr is not None else None
packages/core/src/disco/core/inspect.py:133:            return list(self._traces.keys())
packages/core/src/disco/core/inspect.py:137:            self._traces.clear()
packages/core/src/disco/core/inspect.py:146:def registry() -> InspectRegistry:
packages/core/src/disco/core/inspect.py:157:    """Flatten a RoutingDecision to the trace's JSON-ready field dict."""
packages/core/src/disco/core/inspect.py:170:    """A :class:`RoutingSink` that records each decision into the inspect registry
packages/core/src/disco/core/inspect.py:177:        self._reg = reg or registry()
packages/core/src/disco/core/inspect.py:179:    def record(self, decision: RoutingDecision) -> None:
packages/core/src/disco/core/inspect.py:186:    if conversation_id and inspect_enabled():
packages/core/src/disco/core/inspect.py:195:    """Captures ``disco.span`` records into the registry, attributed by the ``cid``
packages/core/src/disco/core/inspect.py:196:    field that :func:`disco.core.obs.log_span` stamps on each record."""
packages/core/src/disco/core/inspect.py:202:    def emit(self, record: logging.LogRecord) -> None:
packages/core/src/disco/core/inspect.py:203:        fields = getattr(record, "_fields", None)
packages/core/src/disco/core/inspect.py:216:    runtime calls this at startup ONLY when ``inspect_enabled()`` — when off, no
packages/core/src/disco/core/inspect.py:219:    r = reg or registry()
packages/core/src/disco/core/loop/messages.py:8:  lists, most-recent first; the snapshot + HS-03 recap both build on it.
packages/core/src/disco/core/loop/messages.py:16:the workspace-snapshot size caps (`_WS_*`) stay in engine.py — only the
packages/core/src/disco/core/loop/messages.py:30:# plan — the live trace showed ~17 reads and no submit_plan, so the revise
packages/core/src/disco/core/loop/messages.py:104:    are never pushed out of the snapshot window by read-heavy exploration
packages/core/src/disco/core/loop/messages.py:168:         body — the snapshot is the authoritative current state).
packages/core/src/disco/core/dod_evaluator.py:27:The verdict records WHAT was checked and the per-predicate result, so the
packages/core/src/disco/core/dod_evaluator.py:176:    # observed predicate field, ...). The verdict carries the FULL record so
packages/core/src/disco/core/dod_evaluator.py:194:    `results` is the FULL per-predicate record (passed + failed). The audit
packages/core/src/disco/core/dod_evaluator.py:282:    stderr); we never tee the output to the agent's trace — the evaluator's
packages/core/src/disco/core/dod_evaluator.py:285:    A denied command is recorded as `CommandResult(denied=True,
packages/core/src/disco/core/dod_evaluator.py:390:        # HTTP 4xx/5xx is a real response — record the status (it's the
packages/core/src/disco/core/dod_evaluator.py:542:        raises — every failure mode is recorded as a `DoDPredicateResult`.
packages/core/src/disco/core/loop/engine.py:117:# _WS_* snapshot budget constants moved to loop/view_render.py with the snapshot fn.
packages/core/src/disco/core/loop/engine.py:202:# plan — the live trace showed ~17 reads and no submit_plan, so the revise
packages/core/src/disco/core/loop/engine.py:240:# command, a constraint the user stated, a dead-end to avoid), it records it
packages/core/src/disco/core/loop/engine.py:606:        # gate → F8 → snapshot append). Reads condenser/summarizer + self._recit.
packages/core/src/disco/core/loop/engine.py:776:    async def _workspace_snapshot_message(self, events: list[Event]) -> LLMMessage | None:
packages/core/src/disco/core/loop/engine.py:777:        """Delegates to view_render.workspace_snapshot_message over the executor's
packages/core/src/disco/core/loop/engine.py:779:        return await view_render.workspace_snapshot_message(
packages/core/src/disco/core/loop/engine.py:949:            await self._emit(action)  # record the proposed action for audit
packages/core/src/disco/core/loop/engine.py:1012:            await self._emit(action)  # record the PROPOSED action
packages/core/src/disco/core/loop/engine.py:1212:                #     record an agent message and CONTINUE (the safe talk-back
packages/core/src/disco/core/loop/engine.py:1258:                # (g) no-op step (thought only) — record and continue. GAP B
packages/core/src/disco/core/loop/engine.py:1288:                # user to answer. Convert it into a self-directed nudge: record the
packages/core/src/disco/core/loop/engine.py:1386:        """Deny the pending action: record the denial (so the model sees it next
packages/core/src/disco/core/loop/engine.py:1389:        The rejection is recorded as an `AgentErrorEvent` whose content is wrapped
packages/core/src/disco/core/loop/engine.py:1489:                # Bad pick (stale id?) — record + return without resuming so
packages/core/src/disco/core/loop/engine.py:1521:            # Synthesize the ActionEvent. The thought records WHY we're running
packages/core/src/disco/core/loop/engine.py:1522:            # it (the option's description) so the trace stays self-explanatory.
packages/core/src/disco/core/loop/plan_conditions.py:41:        note in the trace. Handles BOTH progress channels:
packages/core/src/disco/core/loop/plan_conditions.py:44:          * `update_plan_progress({steps:[...]})` — the declarative full-state snapshot
packages/core/src/disco/core/loop/plan_conditions.py:46:            done in THIS action (diffed against the prior effective state) — a snapshot
packages/core/src/disco/core/loop/plan_conditions.py:48:            would re-spam an advisory for every step on every snapshot.
packages/core/src/disco/core/loop/plan_conditions.py:102:            # visible in the trace for the human and the LLM sees it next turn as a
packages/core/src/disco/core/loop/plan_conditions.py:105:            # explicit in the trace.
packages/core/src/disco/core/loop/plan_conditions.py:140:        readable summary the C18 note emits in the trace; on failure it
packages/core/src/disco/core/loop/file_state.py:1:"""W2 — file-state tracker for the workspace snapshot (per-path SHA-based staleness).
packages/core/src/disco/core/loop/file_state.py:9:  * Track the SHA of each file the snapshot has shown in full (so the next
packages/core/src/disco/core/loop/file_state.py:11:  * ``record_agent_io`` — call after a successful file_read or file_write /
packages/core/src/disco/core/loop/file_state.py:40:    """Per-path snapshot record: the SHA and seq of the last full-body render."""
packages/core/src/disco/core/loop/file_state.py:42:    last_seen_seq: int  # event seq at which the snapshot last showed this file
packages/core/src/disco/core/loop/file_state.py:48:    """Mutable per-ViewBuilder tracker: records which files have been shown
packages/core/src/disco/core/loop/file_state.py:49:    in full and with what content SHA. Used by ``workspace_snapshot_message``
packages/core/src/disco/core/loop/file_state.py:68:    def record_agent_io(
packages/core/src/disco/core/loop/file_state.py:75:        """Record that the agent (or snapshot) last saw ``path`` with
packages/core/src/disco/core/loop/file_state.py:84:            staleness on the NEXT snapshot build).
packages/core/src/disco/core/loop/file_state.py:85:          * ``workspace_snapshot_message`` showing a file in full (the
packages/core/src/disco/core/loop/file_state.py:86:            snapshot function calls this itself; callers need not duplicate
packages/core/src/disco/core/loop/file_state.py:87:            it after a snapshot build).
packages/core/src/disco/core/loop/file_state.py:123:        diverges from the last-seen SHA recorded in this tracker.
packages/core/src/disco/core/loop/file_state.py:126:        they are "never-shown" and handled separately by the snapshot's
packages/core/src/disco/core/loop/file_state.py:130:        treated as "not stale" (the snapshot will handle the missing case).
packages/core/src/disco/core/loop/file_state.py:163:    and the snapshot function can detect it reliably. The body is minimal —
packages/core/src/disco/core/loop/file_state.py:165:    does NOT reproduce file content (that is the snapshot's job).
packages/core/src/disco/core/loop/dedup.py:8:  content is on disk + in the workspace snapshot).
packages/core/src/disco/core/loop/dedup.py:20:A8 workspace-snapshot machinery. Keeping them here lets this module avoid an
packages/core/src/disco/core/loop/dedup.py:30:# _workspace_snapshot_message. file_list is excluded (its path is a directory).
packages/core/src/disco/core/loop/dedup.py:32:# snapshot by exploration reads (steelman finding #1).
packages/core/src/disco/core/loop/dedup.py:47:# and in the workspace snapshot. This reclaims context window. Lossless:
packages/core/src/disco/core/loop/dedup.py:48:# the content is recoverable via file_read or the snapshot.
packages/core/src/disco/core/loop/dedup.py:152:# snapshot; F9 covers READS only.
packages/core/src/disco/core/loop/dedup.py:336:    # Forward scan: for each path, record the index of the LAST tool-result
packages/core/src/disco/core/loop/signals.py:49:        "update_plan_progress",  # declarative progress snapshot — pure UI signal, no work
packages/core/src/disco/core/loop/signals.py:54:        "remember",  # bookkeeping — recording a fact isn't task progress on its own
packages/core/src/disco/core/loop/turn_control.py:668:                    content="Already recorded — not stored again.",
packages/core/src/disco/core/loop/turn_control.py:832:        # Under the cap → record the proposed action, dispatch
packages/core/src/disco/core/loop/agent.py:173:            # measured fields land on the span's `end` record (trace-assertable)
packages/core/src/disco/core/loop/observe.py:62:             from the snapshot because the model has no business re-reading
packages/core/src/disco/core/loop/observe.py:66:          3. `.pmx/MEMORY.md` — standing memory the agent recorded this run
packages/core/src/disco/core/loop/observe.py:159:        watches the trace and can steer at any moment, and the agent has a
packages/core/src/disco/core/loop/observe.py:227:        # CURRENT WORKSPACE snapshot. Catches ALL execution paths (run loop,
packages/core/src/disco/core/store/sqlite.py:460:        record). Sync + non-blocking so the agent loop can call it inline."""
packages/core/src/disco/core/store/sqlite.py:674:        records the conversation + the bundle's last_seq at export time. A
packages/core/src/disco/core/store/sqlite.py:857:        # history snapshot and going live; the overlap is deduped by seq.
packages/core/src/disco/core/store/base.py:95:        listener. Display-only — the durable record is the final persisted event."""
packages/core/src/disco/core/loop/finish.py:124:    # raises URLError → nonzero exit + a traceback the agent reads as "not serving".
packages/core/src/disco/core/loop/finish.py:303:        VISIBLE in the trace, and on failure REFUSE the finish so the agent fixes
packages/core/src/disco/core/loop/finish.py:424:        ALWAYS leaves a trace event. Same shape as verify-on-finish's
packages/core/src/disco/core/loop/finish.py:571:        # forcing function. The check is visible in the trace; on
packages/core/src/disco/core/loop/view_render.py:4:projections — the live workspace snapshot, the F8 arg-shrink transform, and the
packages/core/src/disco/core/loop/view_render.py:5:router overflow signal. They carry no loop state: the snapshot takes the sandbox
packages/core/src/disco/core/loop/view_render.py:43:_WS_MAX_FILES = 8  # cap the snapshot breadth (most-recently-touched first)
packages/core/src/disco/core/loop/view_render.py:45:_WS_TOTAL_CHARS = 16_000  # total snapshot budget (~4k tokens), bounded vs the condenser
packages/core/src/disco/core/loop/view_render.py:46:_WS_READ_TIMEOUT_S = 2.0  # per-file read cap — the snapshot runs under the conversation
packages/core/src/disco/core/loop/view_render.py:64:    """Typed sentinel: a hung read ⇒ wedged sandbox → stop the snapshot.
packages/core/src/disco/core/loop/view_render.py:75:    """Read one working-set file's raw bytes for the snapshot. Returns the
packages/core/src/disco/core/loop/view_render.py:78:    ``_SNAPSHOT_READ_HALT`` to STOP the snapshot (a hung read implies a wedged/
packages/core/src/disco/core/loop/view_render.py:81:    workspace_snapshot_message to keep that coordinator under the size cap."""
packages/core/src/disco/core/loop/view_render.py:85:        # Degrade to the snapshot collected so far. The sandbox isn't healed
packages/core/src/disco/core/loop/view_render.py:96:        # is in its working set but the snapshot can't show it.
packages/core/src/disco/core/loop/view_render.py:110:    # binary signal; the snapshot's only value is content the model can act on.
packages/core/src/disco/core/loop/view_render.py:119:async def workspace_snapshot_message(
packages/core/src/disco/core/loop/view_render.py:148:        _workspace_snapshot_message delegator and legacy tests).
packages/core/src/disco/core/loop/view_render.py:153:        and passed in so the snapshot does not re-read just for staleness.
packages/core/src/disco/core/loop/view_render.py:178:        "acknowledge the snapshot in your reply (no 'I can see the files', "
packages/core/src/disco/core/loop/view_render.py:208:        # set snapshot would (a) be a divergent second store from the
packages/core/src/disco/core/loop/view_render.py:215:            break  # hung read ⇒ wedged sandbox; degrade to the snapshot so far
packages/core/src/disco/core/loop/view_render.py:251:        # Plain text delimiters — NOT markdown ``` fences. A fenced snapshot
packages/core/src/disco/core/loop/view_render.py:255:        # returns raw content with no fences; the snapshot matches that.
packages/core/src/disco/core/loop/view_render.py:264:        # W2 — update tracker after showing full body (seq=0: the snapshot
packages/core/src/disco/core/loop/view_render.py:267:            tracker.record_agent_io(path, raw, seq=0)
packages/core/src/disco/core/loop/view_render.py:277:    # keeps the snapshot's shape consistent; the notes are short
packages/core/src/disco/core/loop/view_render.py:283:            f"(snapshot budget) — file_read them when you need their content.]"
packages/core/src/disco/core/loop/view_render.py:304:    workspace snapshot).
packages/core/src/disco/core/loop/view_render.py:371:    append the always-fresh workspace snapshot. Back-ref collaborator: body is
packages/core/src/disco/core/loop/view_render.py:376:    the stale-aware snapshot (full body only for stale/never-shown files,
packages/core/src/disco/core/loop/view_render.py:382:        # turns). Starts empty; populated by workspace_snapshot_message as
packages/core/src/disco/core/loop/view_render.py:398:        # W2 — stale check before snapshot: identify files whose disk SHA
packages/core/src/disco/core/loop/view_render.py:399:        # diverged from what the snapshot last showed (externally changed).
packages/core/src/disco/core/loop/view_render.py:400:        # This runs first so the snapshot can mark those files as full-body
packages/core/src/disco/core/loop/view_render.py:409:        # A8: build the live workspace snapshot up-front so its size is counted in
packages/core/src/disco/core/loop/view_render.py:412:        # estimation gap the A-S1 fix closed). The snapshot content is disk-derived
packages/core/src/disco/core/loop/view_render.py:416:        # _workspace_snapshot_message delegator) so we can pass the tracker +
packages/core/src/disco/core/loop/view_render.py:418:        snapshot = await workspace_snapshot_message(
packages/core/src/disco/core/loop/view_render.py:424:        snap_tokens = len(snapshot.content) // 4 if snapshot is not None else 0
packages/core/src/disco/core/loop/view_render.py:439:        # Append the snapshot AFTER any condensation (so it is never rebuilt away by
packages/core/src/disco/core/loop/view_render.py:442:        # C6 — gate the tail-recap on cadence + drift BEFORE the snapshot
packages/core/src/disco/core/loop/view_render.py:445:        # snapshot. The gate never touches the snapshot; the snapshot
packages/core/src/disco/core/loop/view_render.py:460:        # Lossless: the full content lives on disk (and in the snapshot) —
packages/core/src/disco/core/loop/view_render.py:465:        # and after the snapshot append so the snapshot (authoritative
packages/core/src/disco/core/loop/view_render.py:473:        # W2 — assemble trailing context: stale notice (if any) + snapshot.
packages/core/src/disco/core/loop/view_render.py:474:        # Stale notice goes BEFORE the snapshot so the model reads "these
packages/core/src/disco/core/loop/view_render.py:482:        if snapshot is not None:

exec
/bin/bash -lc "nl -ba packages/core/src/disco/core/llm/routing.py | sed -n '1,240p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""The Router — llm-router-contract.md §4 (v1.2 deterministic config-driven).
     2	
     3	The single entry point callers use. v1.2 LOBOTOMY demoted it from intelligent
     4	routing to a deterministic, config-driven lookup pipeline. It still owns:
     5	role→model resolution (now a direct config lookup, no policy), prompt-variant
     6	injection, provider execution, passive cost observation, and emitting the
     7	RoutingDecision. What it NO LONGER owns: the overflow policy, difficulty
     8	assessment, capability-based escalation, and local-failure/stuck/low-confidence
     9	escalation — all preserved DORMANT (commented) at the bottom of this file and in
    10	policy.py / config.py, so the revival path is a diff. See the v1.2 contract.
    11	
    12	Guarantees (RT1–RT4) still hold:
    13	  RT1 complete() returns a CompletionResponse with .routing and .usage
    14	      populated, or raises a typed LLMError.
    15	  RT2 (v1.2 amended) the model is selected by config assignment — `default_model`
    16	      / `assignments[role]` — optionally overridden per conversation by the model
    17	      pill (CallContext.model_override). Callers still cannot pin an arbitrary
    18	      name through the request; the override is an explicit operator surface.
    19	  RT3 the matching prompt variant is injected when the caller supplied no system
    20	      message.
    21	  RT4 every call emits exactly one RoutingDecision (success or terminal failure)
    22	      to the sink. The decision's path is "pinned" (settings assignment) or
    23	      "manual" (per-conversation override); reason is "config".
    24	
    25	[EXTENSION] `CallContext` threads the loop's runtime signals + the conversation
    26	id (for passive per-conversation cost) and, in v1.2, the per-conversation
    27	`model_override` (the main-screen model pill). `complete`/`stream_complete` take
    28	it as an optional keyword-only `context`; callers using `complete(req)` alone are
    29	unaffected.
    30	"""
    31	
    32	from __future__ import annotations
    33	
    34	from collections import defaultdict
    35	from collections.abc import AsyncIterator
    36	from typing import Literal, Protocol, runtime_checkable
    37	
    38	from pydantic import BaseModel, ConfigDict
    39	
    40	from ..events import LLMMessage
    41	from .config import ModelEntry, RouterConfig
    42	from .errors import (
    43	    BudgetExceeded,
    44	    LLMAuthError,
    45	    LLMContentFiltered,
    46	    LLMContextWindowExceeded,
    47	    LLMTransientError,
    48	    NoEligibleModel,
    49	)
    50	from .prompts import (
    51	    PromptProvider,
    52	    StaticPromptProvider,
    53	    default_mode_for_role,
    54	    derive_family,
    55	)
    56	from .provider import ModelProvider
    57	from .types import (
    58	    CompletionRequest,
    59	    CompletionResponse,
    60	    ModelRole,
    61	    Requirement,
    62	    RoutingDecision,
    63	    StreamChunk,
    64	)
    65	
    66	# Cap on same-model transient retries within a single call. The loop's
    67	# max_iterations ceiling (event contract) is the ultimate backstop. v1.2: this is
    68	# pure resilience (retry the SAME assigned model), never model escalation.
    69	_MAX_ATTEMPTS = 5
    70	
    71	# The routing paths. v1.2 emits "pinned"/"manual"; "local"/"overflow" are kept in
    72	# the Literal for the DORMANT intelligent-routing revival path and for back-compat
    73	# of any persisted decisions.
    74	Path = Literal["local", "overflow", "pinned", "manual"]
    75	
    76	
    77	class CallContext(BaseModel):
    78	    """[EXTENSION] Optional per-call runtime context from the loop. See module
    79	    docstring.
    80	
    81	    v1.2 adds `model_override`: the model KEY chosen for THIS conversation by the
    82	    main-screen model pill. It overrides the settings assignment for the
    83	    AGENT_DRIVER role only (the model that "leads the show"); other roles always
    84	    follow their settings assignment. None means "use the settings assignment".
    85	
    86	    `consecutive_tool_errors`/`last_local_confidence` are now ADVISORY (carried for
    87	    observability); the deterministic router does not route on them."""
    88	
    89	    model_config = ConfigDict(frozen=True)
    90	    conversation_id: str | None = None
    91	    consecutive_tool_errors: int = 0
    92	    last_local_confidence: float | None = None
    93	    model_override: str | None = None  # v1.2 per-conversation model pill (driver)
    94	
    95	
    96	# ---- observability sink (RT4) -----------------------------------------------
    97	
    98	
    99	@runtime_checkable
   100	class RoutingSink(Protocol):
   101	    """[CONTRACT boundary] Receives one RoutingDecision per call for the
   102	    metrics/audit layer (BoD §18/§20)."""
   103	
   104	    def record(self, decision: RoutingDecision) -> None: ...
   105	
   106	
   107	class NullRoutingSink:
   108	    """Default sink: drops decisions (used when observability isn't wired)."""
   109	
   110	    def record(self, decision: RoutingDecision) -> None:  # noqa: D102
   111	        return None
   112	
   113	
   114	class InMemoryRoutingSink:
   115	    """Collects decisions in a list. For tests and simple local observability."""
   116	
   117	    def __init__(self) -> None:
   118	        self.decisions: list[RoutingDecision] = []
   119	
   120	    def record(self, decision: RoutingDecision) -> None:
   121	        self.decisions.append(decision)
   122	
   123	
   124	# ---- cost accounting (§5.2) — PASSIVE in v1.2 -------------------------------
   125	
   126	
   127	class CostTracker:
   128	    """Running cost, global and per-conversation. [INTERIOR] accounting store.
   129	
   130	    `*_daily` global caps are modeled as a single running total in v1 (no day
   131	    rollover); the door to a real time-windowed store is open behind this class.
   132	    v1.2: this still ACCRUES spend on every call (observability), and the hard cap
   133	    is honored as a spend backstop, but cost never chooses a model.
   134	    """
   135	
   136	    def __init__(self) -> None:
   137	        self._global = 0.0
   138	        self._per_conversation: dict[str, float] = defaultdict(float)
   139	
   140	    def add(self, cost_usd: float, conversation_id: str | None) -> None:
   141	        self._global += cost_usd
   142	        if conversation_id is not None:
   143	            self._per_conversation[conversation_id] += cost_usd
   144	
   145	    def global_cost(self) -> float:
   146	        return self._global
   147	
   148	    def conversation_cost(self, conversation_id: str | None) -> float:
   149	        if conversation_id is None:
   150	            return 0.0
   151	        return self._per_conversation[conversation_id]
   152	
   153	
   154	def _hard_cap_exceeded(
   155	    accrued_conv: float,
   156	    accrued_global: float,
   157	    config: RouterConfig,
   158	) -> bool:
   159	    """True if either HARD cap (per-conversation or global) has been reached.
   160	    v1.2: the SOFT caps are observe-only (they no longer suppress anything, since
   161	    there is no discretionary overflow to suppress)."""
   162	
   163	    def hit(accrued: float, cap: float | None) -> bool:
   164	        return cap is not None and accrued >= cap
   165	
   166	    return hit(accrued_conv, config.hard_budget_usd_per_conversation) or hit(
   167	        accrued_global, config.hard_budget_usd_global_daily
   168	    )
   169	
   170	
   171	# ---- the router protocol + default implementation ---------------------------
   172	
   173	
   174	@runtime_checkable
   175	class LLMRouter(Protocol):
   176	    """[CONTRACT] The single entry point for all model calls."""
   177	
   178	    async def complete(self, req: CompletionRequest) -> CompletionResponse: ...
   179	
   180	    # NOTE: declared as a plain `def` returning an AsyncIterator — the correct
   181	    # type for an async-generator method (you iterate the result directly with
   182	    # `async for`, never `await` it). The contract wrote `async def`, which types
   183	    # as a coroutine-returning-an-iterator and would force `async for x in await
   184	    # ...`; the async-generator form below matches the streaming usage and the
   185	    # fake/real adapters. (Same reasoning applies to ModelProvider.stream_complete.)
   186	    def stream_complete(self, req: CompletionRequest) -> AsyncIterator[StreamChunk]: ...
   187	
   188	
   189	class DefaultLLMRouter:
   190	    """[CONTRACT] The reference LLMRouter (RT1–RT4), v1.2 deterministic."""
   191	
   192	    def __init__(
   193	        self,
   194	        config: RouterConfig,
   195	        providers: dict[str, ModelProvider],
   196	        *,
   197	        prompt_provider: PromptProvider | None = None,
   198	        sink: RoutingSink | None = None,
   199	        cost_tracker: CostTracker | None = None,
   200	    ) -> None:
   201	        self._config = config
   202	        self._providers = providers
   203	        self._prompts = prompt_provider or StaticPromptProvider()
   204	        self._sink = sink or NullRoutingSink()
   205	        self._cost = cost_tracker or CostTracker()
   206	
   207	    # -- resolution (v1.2: direct config lookup, no policy) -------------------
   208	
   209	    def _resolve(
   210	        self, req: CompletionRequest, ctx: CallContext
   211	    ) -> tuple[ModelEntry, Path, str, list[str]]:
   212	        """Deterministic role→model resolution. Returns (entry, path, reason,
   213	        overflow_triggers).
   214	
   215	        Precedence is owned by `RouterConfig.model_for`: per-conversation override
   216	        (driver only) > settings assignment > default_model.
   217	
   218	        REACTIVE error surfacing (no pre-call capability prediction): the router
   219	        does NOT inspect `entry.capabilities` to decide whether the model can
   220	        handle the request. It sends the request as assigned; if the model can't
   221	        do what was asked, the PROVIDER says so and that error is surfaced with its
   222	        real content (see `complete`/`_execute_and_observe`/the loop). The only
   223	        check here is config integrity — that the assignment resolves to a model
   224	        that actually exists in the catalogue (a misconfiguration, not a
   225	        capability judgement)."""
   226	        profile = req.profile
   227	
   228	        # The model pill overrides the driver only; other roles follow settings.
   229	        override = ctx.model_override if profile.role == ModelRole.AGENT_DRIVER else None
   230	        key = self._config.model_for(profile.role, override=override)
   231	        path: Path = "manual" if override is not None else "pinned"
   232	
   233	        entry = self._config.models.get(key)
   234	        if entry is None:
   235	            # Config integrity (NOT a capability check): the assignment points at a
   236	            # model that isn't in the catalogue. Fail loud — there is nothing to
   237	            # send a request to. Capabilities are never consulted here.
   238	            self._sink.record(
   239	                RoutingDecision(
   240	                    profile=profile,

exec
/bin/bash -lc "nl -ba packages/agent-server/src/disco/agent_server/runtime.py | sed -n '700,745p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   700	        # are ignored — fail safe to the saved assignment.)
   701	        if pick and pick in cfg.models:
   702	            reassigned = {**cfg.assignments, **{r: pick for r in self._GENERATIVE_ROLES}}
   703	            cfg = cfg.model_copy(update={"assignments": reassigned})
   704	        # Overlay decrypted provider keys into the (per-request copy of the) env
   705	        # that build_providers reads, so any model authenticates from the encrypted
   706	        # store without its key being on disk in plaintext. OpenRouter uses a
   707	        # reserved slot mapped to its env-var name; every other stored secret is
   708	        # keyed BY its api_key_env var name, so it overlays onto itself.
   709	        env = dict(os.environ)
   710	        self._overlay_stored_secrets(env)
   711	        thinking = self._enable_thinking if enable_thinking is None else enable_thinking
   712	        providers = build_providers(cfg, env=env, enable_thinking=thinking)
   713	        # DriverPrompts gives the AGENT_DRIVER role phase-aware system prompts (the
   714	        # plan→approve→build flow); every other role/mode defers to the default
   715	        # provider, so Research is unaffected. Enabled SKILLS (the user's reusable
   716	        # .md instructions) are rendered and prepended to the driver prompts —
   717	        # read fresh each request so a Settings toggle takes effect next run.
   718	        # `surface` filters to skills scoped to it (build vs agent) — None = all.
   719	        skills_block = render_skills_for_prompt(self._skill_store.enabled(), surface=surface)
   720	        # The "agent" surface gets the task-agent prompt flavor (an identity reframe);
   721	        # build + every other surface keep the build driver prompts unchanged.
   722	        flavor = "agent" if surface == "agent" else "build"
   723	        # DISCO_INSPECT: when on, bind a per-conversation routing sink so every
   724	        # RoutingDecision this (per-conversation) router emits lands in the trace.
   725	        # Off → None → the router's NullRoutingSink, i.e. zero overhead.
   726	        sink = routing_sink_for(conversation_id)
   727	        return DefaultLLMRouter(
   728	            cfg,
   729	            providers,
   730	            prompt_provider=DriverPrompts(
   731	                skills_block=skills_block, flavor=flavor, autonomous=autonomous
   732	            ),
   733	            sink=sink,
   734	        )
   735	
   736	    # Four surfaces: research (single-pass /ws/research stream), build (agent +
   737	    # tools + plan gate, framed for software), agent (the SAME agent machinery
   738	    # framed as a general task agent), deep_research (long-horizon plan → iterate
   739	    # → report). "agent" is a framing copy of "build" — identical loop/tools/
   740	    # sandbox/persistence — so it travels with build through every surface branch.
   741	    _VALID_SURFACES: frozenset[str] = frozenset({"research", "build", "agent", "deep_research"})
   742	    # The surfaces that compose the build agent loop (tools + sandbox + gate +
   743	    # workspace snapshot/rehydrate). "build" and "agent" are behaviorally identical;
   744	    # they differ only in frontend framing + entry. Branch on this set, never on the
   745	    # bare string, so a new build-like surface can't silently miss a call site.

codex
Important distinction: `/api/debug/evidence/{cid}` redacts the inspect trace before returning it, but `/api/debug/trace/{cid}` still returns the raw trace. If the trace ever gains prompt/header fields later, the evidence route will be safer than the older trace route.
exec
/bin/bash -lc "nl -ba packages/core/src/disco/core/loop/agent.py | sed -n '130,210p' && nl -ba packages/core/src/disco/core/obs.py | sed -n '50,100p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   130	            # A long uniform run at temp 0.0 is a near-deterministic self-imitation
   131	            # chain — maximally prone to repeating a prior failing pattern. The
   132	            # summarizer + other structured roles stay at 0.0 (they set it
   133	            # explicitly); only the acting driver gets the jitter. A per-step
   134	            # `temperature` override (the loop's stuck-escape bump) wins when given.
   135	            temperature=temperature if temperature is not None else self._temperature,
   136	            request_id=f"req_{uuid.uuid4().hex}",
   137	            # F5 — repair-attempt counter. The engine increments this on each
   138	            # retry of a failed call; the provider reads it to disable thinking
   139	            # on attempt ≥ 2. Default 1 = first try; assist-OFF callers ignore.
   140	            attempt=attempt,
   141	            # P2 — provider routing escalation (threaded from the driver on
   142	            # LLMProviderUnavailable retries; None on first/normal calls).
   143	            provider_prefs=provider_prefs,
   144	        )
   145	        ctx = CallContext(
   146	            conversation_id=self._cid,
   147	            consecutive_tool_errors=overflow_signal.consecutive_tool_errors,
   148	            last_local_confidence=overflow_signal.last_local_confidence,
   149	            model_override=self._model_override,
   150	        )
   151	        # STREAM the driver call, not `complete()`. This is load-bearing, not an
   152	        # optimization: for large tool-call arguments (e.g. a `file_write` with a big
   153	        # file body), some OpenAI-compatible providers (observed: DeepSeek via
   154	        # OpenRouter/NovitaAI) return EMPTY `arguments` in non-streaming mode but
   155	        # assemble them correctly from streamed deltas — and far faster (~18s vs ~5min
   156	        # for the same request). Consuming the stream to its final chunk gives the
   157	        # correctly-assembled response; surfacing the intermediate deltas to the UI
   158	        # (watch-it-write) is the next layer on top of this.
   159	        resp = None
   160	        with log_span("agent.step", role="agent_driver", cid=self._cid) as span:
   161	            async for chunk in self._router.stream_complete(req, context=ctx):
   162	                # Forward tool-call arg deltas (the file body) to the watch-it-write
   163	                # hook. The agent stays ignorant of tool semantics — it just relays the
   164	                # raw fragments; the loop accumulates + extracts the field to display.
   165	                if on_stream is not None and chunk.tool_args_delta:
   166	                    await on_stream(chunk)
   167	                if chunk.done and chunk.final is not None:
   168	                    resp = chunk.final
   169	            if resp is None:
   170	                # The stream produced no terminal chunk — fall back to the non-stream
   171	                # call so a provider that doesn't stream still works (never wedge).
   172	                resp = await self._router.complete(req, context=ctx)
   173	            # measured fields land on the span's `end` record (trace-assertable)
   174	            span["model"] = resp.model_used
   175	            span["in_tokens"] = resp.usage.input_tokens
   176	            span["out_tokens"] = resp.usage.output_tokens
   177	            span["finish"] = str(resp.finish_reason)
   178	
   179	        if resp.tool_calls:
   180	            # One-action-per-iteration: take the FIRST proposed call (§3).
   181	            pc = resp.tool_calls[0]
   182	            return AgentStep(
   183	                thought=resp.text,
   184	                tool_call=ToolCall(tool_name=pc.tool_name, arguments=pc.arguments),
   185	                finished=False,
   186	                llm_response_id=resp.request_id,
   187	            )
   188	        # No tool call → a tool-less PROSE turn. Completion semantics are owned
   189	        # by the AGENT (Research↔Build isolation), not derived from the per-step
   190	        # mode:
   191	        #   - ResearchAgent (prose_finishes=True): the prose IS the answer →
   192	        #     finished. Research/chat completion depends on this.
   193	        #   - BuildAgent (prose_finishes=False): talking is NOT finishing. The
   194	        #     run ends only via the affirmative `finish` tool the loop intercepts,
   195	        #     so mid-run talk-back can't silently end a build.
   196	        return AgentStep(
   197	            thought=resp.text,
   198	            tool_call=None,
   199	            finished=self._prose_finishes,
   200	            llm_response_id=resp.request_id,
   201	        )
   202	
   203	
   204	class ResearchAgent(RouterAgent):
   205	    """The Research / plain-chat brain. A tool-less prose turn IS the answer →
   206	    the run finishes (single-pass-ish grounded chat). Deterministic by default
   207	    (temperature 0.0) — research answers shouldn't jitter. This is a distinct
   208	    class from BuildAgent so a Build-surface change can never silently alter
   209	    Research's completion behavior (the leak that caused the GAP B bug)."""
   210	
    50	
    51	
    52	def log_event(name: str, **fields: Any) -> None:
    53	    """Emit a single structured event (a point-in-time span)."""
    54	    _LOG.info(name, extra={"_fields": {"span": name, "event": "point", **fields}})
    55	
    56	
    57	@contextlib.contextmanager
    58	def log_span(name: str, **fields: Any) -> Iterator[dict[str, Any]]:
    59	    """Time a span; emit `start` then `end` (with duration_ms) structured records.
    60	    Yields a mutable dict so the body can attach measured fields (e.g. token counts)
    61	    that land on the `end` record:
    62	
    63	        with log_span("agent.step", role=role) as span:
    64	            resp = await call()
    65	            span["tokens"] = resp.usage.output_tokens
    66	    """
    67	    extra = dict(fields)
    68	    _LOG.info(name, extra={"_fields": {"span": name, "event": "start", **extra}})
    69	    start = time.monotonic()
    70	    measured: dict[str, Any] = {}
    71	    try:
    72	        yield measured
    73	    finally:
    74	        ms = round((time.monotonic() - start) * 1000)
    75	        _LOG.info(
    76	            name,
    77	            extra={"_fields": {"span": name, "event": "end", "ms": ms, **extra, **measured}},
    78	        )

codex
For path traversal, the debug evidence route does not open workspace paths directly, but it passes the raw `conversation_id` into project-store lookup. I’m checking whether that store maps IDs to filesystem paths without sanitization.
exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/projects/store.py | sed -n '1,260p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""ProjectStore — the disk-side catalogue + tree under the user-chosen path.
     2	
     3	Layout (the chosen design — mirrored 1:1, no zip-per-project):
     4	
     5	    <projects_root>/
     6	      <conversation_id>/
     7	        manifest.json
     8	        workspace/<file tree>
     9	
    10	The manifest is the cheap row for the list view (title, file_count, byte total,
    11	last_snapshot_at); the workspace dir is the load-bearing artifact. Path traversal
    12	is rejected (no `..`, no absolute child paths).
    13	
    14	`validate_root()` returns a typed status the agent-server maps to clear, named
    15	errors — never crashes, never silently "reloads empty." The full graceful-
    16	failure matrix:
    17	  - root unset (`projects_root == ""`)            → StorageStatus.UNSET
    18	  - root path doesn't exist                       → StorageStatus.NOT_FOUND
    19	  - exists but isn't a directory                  → StorageStatus.NOT_A_DIRECTORY
    20	  - exists but can't be written / probed          → StorageStatus.NOT_WRITABLE
    21	  - all good                                      → StorageStatus.OK
    22	
    23	Zero-config self-healing (E4):
    24	`resolve_projects_root(configured)` converts a configured value to an effective
    25	path.  When `configured` is empty (fresh install / unset), it falls back to
    26	`default_projects_root()` and ensures that directory exists.  When `configured`
    27	is non-empty, it is returned verbatim so the user-set path keeps its current
    28	validate behavior (NOT_FOUND / NOT_WRITABLE surface correctly).
    29	
    30	`ProjectStore(root_str)` calls `resolve_projects_root` internally, so
    31	`ProjectStore("")` gives a ready-to-use store pointing at the auto-default path
    32	rather than raising / returning UNSET on a fresh machine.
    33	"""
    34	
    35	from __future__ import annotations
    36	
    37	import json
    38	import os
    39	import shutil
    40	from collections.abc import Iterator
    41	from dataclasses import dataclass
    42	from datetime import UTC, datetime
    43	from enum import Enum
    44	from pathlib import Path
    45	from typing import Any
    46	
    47	# Used both as the manifest filename and the workspace subdirectory; constants
    48	# here so the agent-server doesn't depend on string literals scattered around.
    49	_MANIFEST = "manifest.json"
    50	_WORKSPACE = "workspace"
    51	
    52	
    53	def default_projects_root() -> str:
    54	    """Compute the platform-appropriate default projects_root for this machine.
    55	
    56	    Priority (first match wins):
    57	    1. ``DISCO_DATA_DIR`` (or legacy ``PMX_DATA_DIR``) env var → ``<DATA_DIR>/projects``
    58	       This is what the container entrypoint sets (default ``/data``).
    59	    2. ``XDG_DATA_HOME`` env var → ``<XDG_DATA_HOME>/disco/projects``
    60	    3. POSIX fallback → ``~/.local/share/disco/projects``
    61	
    62	    The directory is NOT created here; callers that want ensure-exists behaviour
    63	    should use :func:`resolve_projects_root` instead.
    64	    """
    65	    from disco.core.env import disco_env
    66	
    67	    data_dir = disco_env("DATA_DIR")
    68	    if data_dir:
    69	        return str(Path(data_dir) / "projects")
    70	    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    71	    return str(Path(xdg) / "disco" / "projects")
    72	
    73	
    74	def resolve_projects_root(configured: str) -> str:
    75	    """Return the effective projects_root path, ENSURING it exists.
    76	
    77	    Both an explicit ``configured`` path AND the zero-config default are created
    78	    with ``mkdir -p`` so a path that doesn't exist YET but is creatable (writable
    79	    parent) is ready to use — "works on a fresh machine", whether the user set a
    80	    path or not. A genuinely UNREACHABLE path (unplugged drive / permission) makes
    81	    mkdir fail; we swallow that here and let :func:`validate_root` surface
    82	    NOT_FOUND at the use site, so the UI can warn + offer the default rather than
    83	    silently relocating the user's data.
    84	
    85	    This is the single place that turns a configured-or-empty value into a real,
    86	    live directory; all other code calls this rather than branching themselves.
    87	    """
    88	    root = configured.strip() or default_projects_root()
    89	    try:
    90	        Path(root).expanduser().mkdir(parents=True, exist_ok=True)
    91	    except OSError:
    92	        pass  # unreachable path → validate_root reports NOT_FOUND at the use site
    93	    return root
    94	
    95	
    96	class StorageStatus(str, Enum):
    97	    """The validation result for a candidate projects_root. The settings PUT
    98	    endpoint maps each to a named 400 reason; the agent-server's snapshot path
    99	    checks before writing so a misconfigured root fails loud, never silent."""
   100	
   101	    OK = "ok"
   102	    UNSET = "unset"
   103	    NOT_FOUND = "not_found"
   104	    NOT_A_DIRECTORY = "not_a_directory"
   105	    NOT_WRITABLE = "not_writable"
   106	
   107	
   108	class StorageError(Exception):
   109	    """Raised when an operation against the store fails for a reason the caller
   110	    needs to surface (project missing, manifest corrupt, etc.)."""
   111	
   112	
   113	@dataclass(frozen=True)
   114	class ProjectRecord:
   115	    """A row in the Projects list — read from the manifest, plus a live check on
   116	    whether the workspace tree is still on disk (the graceful-failure flag)."""
   117	
   118	    conversation_id: str
   119	    title: str | None
   120	    owner_id: str | None
   121	    created_at: str | None  # ISO-8601, may be None for legacy entries
   122	    last_snapshot_at: str | None  # ISO-8601
   123	    file_count: int
   124	    total_bytes: int
   125	    files_missing: bool  # True iff manifest exists but workspace/ is gone or empty
   126	
   127	
   128	def _now_iso() -> str:
   129	    return datetime.now(UTC).isoformat()
   130	
   131	
   132	def _safe_segment(s: str) -> bool:
   133	    """A conversation ID is a primary key but defensively: reject anything that
   134	    isn't a plain segment (no slashes, no dot-dot, no nulls). Belt and braces
   135	    against a poisoned input becoming a path traversal."""
   136	    if not s or s in {".", ".."}:
   137	        return False
   138	    if "/" in s or "\\" in s or "\x00" in s:
   139	        return False
   140	    return True
   141	
   142	
   143	def validate_root(root_str: str) -> StorageStatus:
   144	    """Classify a candidate projects_root path. Pure, side-effect-free EXCEPT
   145	    for a probe-write that's torn down before returning — that's the only way
   146	    to detect "not writable" cross-platform without trusting `os.access`,
   147	    which lies on some filesystems."""
   148	    if not root_str.strip():
   149	        return StorageStatus.UNSET
   150	    root = Path(root_str).expanduser()
   151	    if not root.exists():
   152	        return StorageStatus.NOT_FOUND
   153	    if not root.is_dir():
   154	        return StorageStatus.NOT_A_DIRECTORY
   155	    # probe write: create a unique sentinel, then remove it. Cross-platform
   156	    # truth about writability without relying on POSIX permission bits.
   157	    probe = root / f".pmx-write-probe-{os.getpid()}"
   158	    try:
   159	        probe.write_bytes(b"")
   160	        probe.unlink()
   161	    except OSError:
   162	        return StorageStatus.NOT_WRITABLE
   163	    return StorageStatus.OK
   164	
   165	
   166	class ProjectStore:
   167	    """The disk side of project persistence. List/get/delete + path resolvers
   168	    callers (the agent-server) use to wire snapshot, rehydrate, and download
   169	    against a per-project directory.
   170	
   171	    Construction does NOT throw on a bad root — the validation status is
   172	    queryable via `status()` / `validate_root(...)`. Operations that REQUIRE
   173	    a valid root (snapshot, list) raise StorageError with the reason; the
   174	    settings round-trip + the read-only browse endpoint work even on a bad
   175	    root (the user has to be able to see + fix the configuration)."""
   176	
   177	    def __init__(self, root_str: str) -> None:
   178	        # _configured keeps the raw value for the settings DTO (so the UI can
   179	        # display what the user explicitly saved, not the auto-default path).
   180	        self._configured_str = root_str
   181	        # _root_str is the effective path used for all disk operations.  Empty
   182	        # root_str → resolve_projects_root auto-creates and returns the default.
   183	        self._root_str = resolve_projects_root(root_str)
   184	        self._root = Path(self._root_str).expanduser()
   185	
   186	    @property
   187	    def root(self) -> Path | None:
   188	        """The expanded absolute effective root path. Always set (never None)
   189	        after construction; the auto-default is created on first use."""
   190	        return self._root
   191	
   192	    def status(self) -> StorageStatus:
   193	        return validate_root(self._root_str)
   194	
   195	    def path_for(self, conversation_id: str) -> Path:
   196	        """The on-disk `workspace/` directory for a project. Raises on a poisoned
   197	        ID — the agent-server only ever passes UUIDs, but this is defense in
   198	        depth against any tampered input."""
   199	        if self._root is None:
   200	            raise StorageError("projects_root is not configured")
   201	        if not _safe_segment(conversation_id):
   202	            raise StorageError(f"unsafe conversation_id: {conversation_id!r}")
   203	        return self._root / conversation_id / _WORKSPACE
   204	
   205	    def manifest_for(self, conversation_id: str) -> Path:
   206	        if self._root is None:
   207	            raise StorageError("projects_root is not configured")
   208	        if not _safe_segment(conversation_id):
   209	            raise StorageError(f"unsafe conversation_id: {conversation_id!r}")
   210	        return self._root / conversation_id / _MANIFEST
   211	
   212	    def list_projects(self) -> list[ProjectRecord]:
   213	        """All projects with a manifest under the root, newest snapshot first.
   214	        Returns an empty list (not an error) if the root is unset / missing,
   215	        so the list endpoint can fall back to its own graceful-empty state."""
   216	        if self._root is None or not self._root.exists():
   217	            return []
   218	        records: list[ProjectRecord] = []
   219	        for entry in sorted(self._root.iterdir()):
   220	            if not entry.is_dir():
   221	                continue
   222	            manifest = entry / _MANIFEST
   223	            if not manifest.is_file():
   224	                continue
   225	            try:
   226	                data = json.loads(manifest.read_text())
   227	            except (OSError, json.JSONDecodeError):
   228	                continue  # don't surface a corrupt manifest as a project row
   229	            workspace = entry / _WORKSPACE
   230	            files_missing = (
   231	                not workspace.exists()
   232	                or not workspace.is_dir()
   233	                or not any(workspace.rglob("*"))
   234	            )
   235	            records.append(
   236	                ProjectRecord(
   237	                    conversation_id=entry.name,
   238	                    title=data.get("title"),
   239	                    owner_id=data.get("owner_id"),
   240	                    created_at=data.get("created_at"),
   241	                    last_snapshot_at=data.get("last_snapshot_at"),
   242	                    file_count=int(data.get("file_count") or 0),
   243	                    total_bytes=int(data.get("total_bytes") or 0),
   244	                    files_missing=files_missing,
   245	                )
   246	            )
   247	        # Newest last-snapshot first; entries with no timestamp sort last.
   248	        records.sort(key=lambda r: r.last_snapshot_at or "", reverse=True)
   249	        return records
   250	
   251	    def get(self, conversation_id: str) -> ProjectRecord | None:
   252	        """Single-row lookup. Returns None if the project doesn't exist (the
   253	        manifest is gone). Returns a record with files_missing=True if the
   254	        manifest is there but the workspace is gone — that's a real state."""
   255	        manifest = self.manifest_for(conversation_id)
   256	        if not manifest.is_file():
   257	            return None
   258	        try:
   259	            data = json.loads(manifest.read_text())
   260	        except (OSError, json.JSONDecodeError) as exc:

exec
/bin/bash -lc "rg -n \"class ProjectStore|def get\\(|conversation_id|snapshot|manifest|projects_root|sanitize|Path\\(\" packages/agent-server/src/disco/agent_server packages/tools/src/disco/tools/projects packages/core/src/disco/core/store -S" in /var/home/dylan/projects/disco
 succeeded in 0ms:
packages/core/src/disco/core/store/sqlite.py:40:    conversation_id TEXT    NOT NULL,
packages/core/src/disco/core/store/sqlite.py:47:    PRIMARY KEY (conversation_id, seq),
packages/core/src/disco/core/store/sqlite.py:48:    UNIQUE (conversation_id, id)
packages/core/src/disco/core/store/sqlite.py:51:    conversation_id TEXT PRIMARY KEY,
packages/core/src/disco/core/store/sqlite.py:70:    conversation_id TEXT   NOT NULL,
packages/core/src/disco/core/store/sqlite.py:81:    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
packages/core/src/disco/core/store/sqlite.py:84:    ON share_tokens (conversation_id);
packages/core/src/disco/core/store/sqlite.py:92:    conversation_id TEXT NOT NULL,
packages/core/src/disco/core/store/sqlite.py:101:    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
packages/core/src/disco/core/store/sqlite.py:104:    ON schedules (owner_id, conversation_id);
packages/core/src/disco/core/store/sqlite.py:110:    conversation_id TEXT NOT NULL,
packages/core/src/disco/core/store/sqlite.py:151:    -- `conversation_id` raises `DoDSpecAlreadySet`. The `replace_dod_spec`
packages/core/src/disco/core/store/sqlite.py:161:    -- evaluator. The PK is `conversation_id` (one spec per conversation).
packages/core/src/disco/core/store/sqlite.py:162:    conversation_id TEXT PRIMARY KEY,
packages/core/src/disco/core/store/sqlite.py:166:    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
packages/core/src/disco/core/store/sqlite.py:208:        # conversation_id -> set of live subscriber queues.
packages/core/src/disco/core/store/sqlite.py:210:        # conversation_id -> set of EPHEMERAL subscriber queues. These carry
packages/core/src/disco/core/store/sqlite.py:225:        conversation_id: str,
packages/core/src/disco/core/store/sqlite.py:241:            "(conversation_id, owner_id, space_id, title, created_at, surface, origin) "
packages/core/src/disco/core/store/sqlite.py:244:                conversation_id,
packages/core/src/disco/core/store/sqlite.py:255:    async def update_title(self, conversation_id: str, title: str) -> None:
packages/core/src/disco/core/store/sqlite.py:266:                    "UPDATE conversations SET title = ? WHERE conversation_id = ?",
packages/core/src/disco/core/store/sqlite.py:267:                    (title, conversation_id),
packages/core/src/disco/core/store/sqlite.py:270:    async def get_title(self, conversation_id: str) -> str | None:
packages/core/src/disco/core/store/sqlite.py:274:            "SELECT title FROM conversations WHERE conversation_id = ?",
packages/core/src/disco/core/store/sqlite.py:275:            (conversation_id,),
packages/core/src/disco/core/store/sqlite.py:284:    # the same conversation_id raises DoDSpecAlreadySet and leaves the
packages/core/src/disco/core/store/sqlite.py:290:        self, conversation_id: str, spec: DoDSpec, *, set_by: str = "system"
packages/core/src/disco/core/store/sqlite.py:320:                    "SELECT 1 FROM dod_specs WHERE conversation_id = ?",
packages/core/src/disco/core/store/sqlite.py:321:                    (conversation_id,),
packages/core/src/disco/core/store/sqlite.py:325:                        f"DoD spec for {conversation_id!r} is already set; "
packages/core/src/disco/core/store/sqlite.py:336:                    "(conversation_id, owner_id, created_at) "
packages/core/src/disco/core/store/sqlite.py:338:                    (conversation_id, DEFAULT_OWNER_ID, datetime.now().isoformat()),
packages/core/src/disco/core/store/sqlite.py:342:                    "(conversation_id, spec, set_at, set_by) "
packages/core/src/disco/core/store/sqlite.py:345:                        conversation_id,
packages/core/src/disco/core/store/sqlite.py:353:    async def get_dod_spec(self, conversation_id: str) -> DoDSpec | None:
packages/core/src/disco/core/store/sqlite.py:361:            "SELECT spec FROM dod_specs WHERE conversation_id = ?",
packages/core/src/disco/core/store/sqlite.py:362:            (conversation_id,),
packages/core/src/disco/core/store/sqlite.py:369:        self, conversation_id: str, spec: DoDSpec, *, actor: str = "system"
packages/core/src/disco/core/store/sqlite.py:391:        existing = await self.get_dod_spec(conversation_id)
packages/core/src/disco/core/store/sqlite.py:394:                f"DoD spec for {conversation_id!r} is already set; replace "
packages/core/src/disco/core/store/sqlite.py:398:        return await self.set_dod_spec(conversation_id, spec, set_by=actor)
packages/core/src/disco/core/store/sqlite.py:402:    def _store_one(self, conversation_id: str, event: Event) -> tuple[Event, bool]:
packages/core/src/disco/core/store/sqlite.py:404:        is_new). Idempotent on (conversation_id, id) — G4."""
packages/core/src/disco/core/store/sqlite.py:407:            "SELECT payload FROM events WHERE conversation_id = ? AND id = ?",
packages/core/src/disco/core/store/sqlite.py:408:            (conversation_id, event.id),
packages/core/src/disco/core/store/sqlite.py:415:            "INSERT OR IGNORE INTO conversations (conversation_id, owner_id, created_at) "
packages/core/src/disco/core/store/sqlite.py:417:            (conversation_id, DEFAULT_OWNER_ID, datetime.now().isoformat()),
packages/core/src/disco/core/store/sqlite.py:422:            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM events WHERE conversation_id = ?",
packages/core/src/disco/core/store/sqlite.py:423:            (conversation_id,),
packages/core/src/disco/core/store/sqlite.py:430:            "INSERT INTO events (conversation_id, seq, id, kind, source, created_at, payload) "
packages/core/src/disco/core/store/sqlite.py:433:                conversation_id,
packages/core/src/disco/core/store/sqlite.py:446:                "UPDATE conversations SET status = ? WHERE conversation_id = ?",
packages/core/src/disco/core/store/sqlite.py:447:                (payload["status"], conversation_id),
packages/core/src/disco/core/store/sqlite.py:452:    def _publish(self, conversation_id: str, event: Event) -> None:
packages/core/src/disco/core/store/sqlite.py:453:        for q in self._subscribers.get(conversation_id, set()):
packages/core/src/disco/core/store/sqlite.py:456:    def publish_ephemeral(self, conversation_id: str, frame: dict) -> None:
packages/core/src/disco/core/store/sqlite.py:461:        for q in self._eph_subscribers.get(conversation_id, set()):
packages/core/src/disco/core/store/sqlite.py:464:    async def subscribe_ephemeral(self, conversation_id: str) -> AsyncIterator[dict]:
packages/core/src/disco/core/store/sqlite.py:466:        return self._subscribe_ephemeral(conversation_id)
packages/core/src/disco/core/store/sqlite.py:468:    async def _subscribe_ephemeral(self, conversation_id: str) -> AsyncIterator[dict]:
packages/core/src/disco/core/store/sqlite.py:470:        self._eph_subscribers[conversation_id].add(queue)
packages/core/src/disco/core/store/sqlite.py:475:            self._eph_subscribers[conversation_id].discard(queue)
packages/core/src/disco/core/store/sqlite.py:477:    async def append(self, conversation_id: str, event: Event) -> Event:
packages/core/src/disco/core/store/sqlite.py:480:                stored, is_new = self._store_one(conversation_id, event)
packages/core/src/disco/core/store/sqlite.py:482:                self._publish(conversation_id, stored)
packages/core/src/disco/core/store/sqlite.py:485:    async def append_many(self, conversation_id: str, events: list[Event]) -> list[Event]:
packages/core/src/disco/core/store/sqlite.py:491:                    stored, is_new = self._store_one(conversation_id, event)
packages/core/src/disco/core/store/sqlite.py:496:                self._publish(conversation_id, ev)
packages/core/src/disco/core/store/sqlite.py:502:        self, conversation_id: str, filter: EventFilter | None, limit: int | None = None
packages/core/src/disco/core/store/sqlite.py:504:        clauses = ["conversation_id = ?"]
packages/core/src/disco/core/store/sqlite.py:505:        params: list[object] = [conversation_id]
packages/core/src/disco/core/store/sqlite.py:533:        self, conversation_id: str, filter: EventFilter | None = None
packages/core/src/disco/core/store/sqlite.py:535:        return self._query(conversation_id, filter)
packages/core/src/disco/core/store/sqlite.py:539:        conversation_id: str,
packages/core/src/disco/core/store/sqlite.py:550:        rows = self._query(conversation_id, eff, limit=limit + 1)
packages/core/src/disco/core/store/sqlite.py:556:    async def get_state(self, conversation_id: str) -> ConversationState:
packages/core/src/disco/core/store/sqlite.py:557:        events = self._query(conversation_id, None)
packages/core/src/disco/core/store/sqlite.py:558:        return ConversationState.reconstruct(conversation_id, events)
packages/core/src/disco/core/store/sqlite.py:560:    async def conversation_exists(self, conversation_id: str) -> bool:
packages/core/src/disco/core/store/sqlite.py:562:            "SELECT 1 FROM conversations WHERE conversation_id = ? LIMIT 1",
packages/core/src/disco/core/store/sqlite.py:563:            (conversation_id,),
packages/core/src/disco/core/store/sqlite.py:574:            "SELECT conversation_id FROM conversations WHERE owner_id = ? "
packages/core/src/disco/core/store/sqlite.py:575:            "ORDER BY created_at DESC, conversation_id DESC LIMIT ? OFFSET ?",
packages/core/src/disco/core/store/sqlite.py:578:        return [r["conversation_id"] for r in rows]
packages/core/src/disco/core/store/sqlite.py:588:            "SELECT conversation_id, owner_id, title, created_at, status, surface, origin "
packages/core/src/disco/core/store/sqlite.py:590:            "WHERE owner_id = ? ORDER BY created_at DESC, conversation_id DESC LIMIT ? OFFSET ?",
packages/core/src/disco/core/store/sqlite.py:596:            cid = r["conversation_id"]
packages/core/src/disco/core/store/sqlite.py:606:                    conversation_id=cid,
packages/core/src/disco/core/store/sqlite.py:618:                "UPDATE conversations SET status = ? WHERE conversation_id = ?",
packages/core/src/disco/core/store/sqlite.py:625:    def conversation_origin(self, conversation_id: str) -> str | None:
packages/core/src/disco/core/store/sqlite.py:629:            "SELECT origin FROM conversations WHERE conversation_id = ?",
packages/core/src/disco/core/store/sqlite.py:630:            (conversation_id,),
packages/core/src/disco/core/store/sqlite.py:634:    async def delete_conversation(self, conversation_id: str, *, owner_id: str) -> bool:
packages/core/src/disco/core/store/sqlite.py:640:                "DELETE FROM conversations WHERE conversation_id = ? AND owner_id = ?",
packages/core/src/disco/core/store/sqlite.py:641:                (conversation_id, owner_id),
packages/core/src/disco/core/store/sqlite.py:646:                "DELETE FROM events WHERE conversation_id = ?", (conversation_id,)
packages/core/src/disco/core/store/sqlite.py:654:        self, conversation_id: str, after_seq: int | None = None
packages/core/src/disco/core/store/sqlite.py:661:        return self._subscribe(conversation_id, after_seq)
packages/core/src/disco/core/store/sqlite.py:668:        conversation_id: str,
packages/core/src/disco/core/store/sqlite.py:681:            "(token, conversation_id, owner_id, created_at, bundle_seq) "
packages/core/src/disco/core/store/sqlite.py:685:                conversation_id,
packages/core/src/disco/core/store/sqlite.py:700:            "SELECT token, conversation_id, owner_id, created_at, bundle_seq "
packages/core/src/disco/core/store/sqlite.py:708:            "conversation_id": row["conversation_id"],
packages/core/src/disco/core/store/sqlite.py:719:            "SELECT token, conversation_id, owner_id, created_at, bundle_seq "
packages/core/src/disco/core/store/sqlite.py:727:                "conversation_id": r["conversation_id"],
packages/core/src/disco/core/store/sqlite.py:755:            "(schedule_id, conversation_id, owner_id, rrule, description, "
packages/core/src/disco/core/store/sqlite.py:760:                row["conversation_id"],
packages/core/src/disco/core/store/sqlite.py:773:    def list_schedules(self, *, owner_id: str, conversation_id: str | None = None) -> list[dict]:
packages/core/src/disco/core/store/sqlite.py:775:        if conversation_id is not None:
packages/core/src/disco/core/store/sqlite.py:777:                "SELECT * FROM schedules WHERE owner_id = ? AND conversation_id = ? "
packages/core/src/disco/core/store/sqlite.py:779:                (owner_id, conversation_id),
packages/core/src/disco/core/store/sqlite.py:816:            "(run_id, schedule_id, conversation_id, fired_at, coalesced) "
packages/core/src/disco/core/store/sqlite.py:821:                row["conversation_id"],
packages/core/src/disco/core/store/sqlite.py:841:        Returns rows: run_id, schedule_id, conversation_id, fired_at, coalesced,
packages/core/src/disco/core/store/sqlite.py:844:            "SELECT sr.run_id, sr.schedule_id, sr.conversation_id, sr.fired_at, "
packages/core/src/disco/core/store/sqlite.py:848:            "LEFT JOIN conversations c ON c.conversation_id = sr.conversation_id "
packages/core/src/disco/core/store/sqlite.py:855:    async def _subscribe(self, conversation_id: str, after_seq: int | None) -> AsyncIterator[Event]:
packages/core/src/disco/core/store/sqlite.py:857:        # history snapshot and going live; the overlap is deduped by seq.
packages/core/src/disco/core/store/sqlite.py:859:        self._subscribers[conversation_id].add(queue)
packages/core/src/disco/core/store/sqlite.py:862:                conversation_id, EventFilter(after_seq=after_seq) if after_seq else None
packages/core/src/disco/core/store/sqlite.py:877:            self._subscribers[conversation_id].discard(queue)
packages/core/src/disco/core/store/base.py:45:    conversation_id: str
packages/core/src/disco/core/store/base.py:64:      G4 idempotency: appending an event whose (conversation_id, id) already
packages/core/src/disco/core/store/base.py:69:    async def append(self, conversation_id: str, event: Event) -> Event: ...
packages/core/src/disco/core/store/base.py:71:    async def append_many(self, conversation_id: str, events: list[Event]) -> list[Event]: ...
packages/core/src/disco/core/store/base.py:74:        self, conversation_id: str, filter: EventFilter | None = None
packages/core/src/disco/core/store/base.py:79:        conversation_id: str,
packages/core/src/disco/core/store/base.py:86:    async def get_state(self, conversation_id: str) -> ConversationState: ...
packages/core/src/disco/core/store/base.py:89:        self, conversation_id: str, after_seq: int | None = None
packages/core/src/disco/core/store/base.py:92:    def publish_ephemeral(self, conversation_id: str, frame: dict) -> None:
packages/core/src/disco/core/store/base.py:98:    async def subscribe_ephemeral(self, conversation_id: str) -> AsyncIterator[dict]:
packages/core/src/disco/core/store/base.py:113:        self, conversation_id: str, spec: Any, *, set_by: str = "system"
packages/core/src/disco/core/store/base.py:116:        with the same `conversation_id` raises `DoDSpecAlreadySet`. The agent
packages/core/src/disco/core/store/base.py:121:    async def get_dod_spec(self, conversation_id: str) -> Any:
packages/core/src/disco/core/store/base.py:129:        self, conversation_id: str, spec: Any, *, actor: str = "system"
packages/core/src/disco/core/store/base.py:141:    async def conversation_exists(self, conversation_id: str) -> bool: ...
packages/core/src/disco/core/store/base.py:143:    async def update_title(self, conversation_id: str, title: str) -> None: ...
packages/core/src/disco/core/store/base.py:145:    async def get_title(self, conversation_id: str) -> str | None: ...
packages/tools/src/disco/tools/projects/store.py:5:    <projects_root>/
packages/tools/src/disco/tools/projects/store.py:6:      <conversation_id>/
packages/tools/src/disco/tools/projects/store.py:7:        manifest.json
packages/tools/src/disco/tools/projects/store.py:10:The manifest is the cheap row for the list view (title, file_count, byte total,
packages/tools/src/disco/tools/projects/store.py:11:last_snapshot_at); the workspace dir is the load-bearing artifact. Path traversal
packages/tools/src/disco/tools/projects/store.py:17:  - root unset (`projects_root == ""`)            → StorageStatus.UNSET
packages/tools/src/disco/tools/projects/store.py:24:`resolve_projects_root(configured)` converts a configured value to an effective
packages/tools/src/disco/tools/projects/store.py:26:`default_projects_root()` and ensures that directory exists.  When `configured`
packages/tools/src/disco/tools/projects/store.py:30:`ProjectStore(root_str)` calls `resolve_projects_root` internally, so
packages/tools/src/disco/tools/projects/store.py:47:# Used both as the manifest filename and the workspace subdirectory; constants
packages/tools/src/disco/tools/projects/store.py:49:_MANIFEST = "manifest.json"
packages/tools/src/disco/tools/projects/store.py:53:def default_projects_root() -> str:
packages/tools/src/disco/tools/projects/store.py:54:    """Compute the platform-appropriate default projects_root for this machine.
packages/tools/src/disco/tools/projects/store.py:63:    should use :func:`resolve_projects_root` instead.
packages/tools/src/disco/tools/projects/store.py:69:        return str(Path(data_dir) / "projects")
packages/tools/src/disco/tools/projects/store.py:71:    return str(Path(xdg) / "disco" / "projects")
packages/tools/src/disco/tools/projects/store.py:74:def resolve_projects_root(configured: str) -> str:
packages/tools/src/disco/tools/projects/store.py:75:    """Return the effective projects_root path, ENSURING it exists.
packages/tools/src/disco/tools/projects/store.py:88:    root = configured.strip() or default_projects_root()
packages/tools/src/disco/tools/projects/store.py:90:        Path(root).expanduser().mkdir(parents=True, exist_ok=True)
packages/tools/src/disco/tools/projects/store.py:97:    """The validation result for a candidate projects_root. The settings PUT
packages/tools/src/disco/tools/projects/store.py:98:    endpoint maps each to a named 400 reason; the agent-server's snapshot path
packages/tools/src/disco/tools/projects/store.py:110:    needs to surface (project missing, manifest corrupt, etc.)."""
packages/tools/src/disco/tools/projects/store.py:115:    """A row in the Projects list — read from the manifest, plus a live check on
packages/tools/src/disco/tools/projects/store.py:118:    conversation_id: str
packages/tools/src/disco/tools/projects/store.py:122:    last_snapshot_at: str | None  # ISO-8601
packages/tools/src/disco/tools/projects/store.py:125:    files_missing: bool  # True iff manifest exists but workspace/ is gone or empty
packages/tools/src/disco/tools/projects/store.py:144:    """Classify a candidate projects_root path. Pure, side-effect-free EXCEPT
packages/tools/src/disco/tools/projects/store.py:150:    root = Path(root_str).expanduser()
packages/tools/src/disco/tools/projects/store.py:166:class ProjectStore:
packages/tools/src/disco/tools/projects/store.py:168:    callers (the agent-server) use to wire snapshot, rehydrate, and download
packages/tools/src/disco/tools/projects/store.py:173:    a valid root (snapshot, list) raise StorageError with the reason; the
packages/tools/src/disco/tools/projects/store.py:182:        # root_str → resolve_projects_root auto-creates and returns the default.
packages/tools/src/disco/tools/projects/store.py:183:        self._root_str = resolve_projects_root(root_str)
packages/tools/src/disco/tools/projects/store.py:184:        self._root = Path(self._root_str).expanduser()
packages/tools/src/disco/tools/projects/store.py:195:    def path_for(self, conversation_id: str) -> Path:
packages/tools/src/disco/tools/projects/store.py:200:            raise StorageError("projects_root is not configured")
packages/tools/src/disco/tools/projects/store.py:201:        if not _safe_segment(conversation_id):
packages/tools/src/disco/tools/projects/store.py:202:            raise StorageError(f"unsafe conversation_id: {conversation_id!r}")
packages/tools/src/disco/tools/projects/store.py:203:        return self._root / conversation_id / _WORKSPACE
packages/tools/src/disco/tools/projects/store.py:205:    def manifest_for(self, conversation_id: str) -> Path:
packages/tools/src/disco/tools/projects/store.py:207:            raise StorageError("projects_root is not configured")
packages/tools/src/disco/tools/projects/store.py:208:        if not _safe_segment(conversation_id):
packages/tools/src/disco/tools/projects/store.py:209:            raise StorageError(f"unsafe conversation_id: {conversation_id!r}")
packages/tools/src/disco/tools/projects/store.py:210:        return self._root / conversation_id / _MANIFEST
packages/tools/src/disco/tools/projects/store.py:213:        """All projects with a manifest under the root, newest snapshot first.
packages/tools/src/disco/tools/projects/store.py:222:            manifest = entry / _MANIFEST
packages/tools/src/disco/tools/projects/store.py:223:            if not manifest.is_file():
packages/tools/src/disco/tools/projects/store.py:226:                data = json.loads(manifest.read_text())
packages/tools/src/disco/tools/projects/store.py:228:                continue  # don't surface a corrupt manifest as a project row
packages/tools/src/disco/tools/projects/store.py:237:                    conversation_id=entry.name,
packages/tools/src/disco/tools/projects/store.py:241:                    last_snapshot_at=data.get("last_snapshot_at"),
packages/tools/src/disco/tools/projects/store.py:247:        # Newest last-snapshot first; entries with no timestamp sort last.
packages/tools/src/disco/tools/projects/store.py:248:        records.sort(key=lambda r: r.last_snapshot_at or "", reverse=True)
packages/tools/src/disco/tools/projects/store.py:251:    def get(self, conversation_id: str) -> ProjectRecord | None:
packages/tools/src/disco/tools/projects/store.py:253:        manifest is gone). Returns a record with files_missing=True if the
packages/tools/src/disco/tools/projects/store.py:254:        manifest is there but the workspace is gone — that's a real state."""
packages/tools/src/disco/tools/projects/store.py:255:        manifest = self.manifest_for(conversation_id)
packages/tools/src/disco/tools/projects/store.py:256:        if not manifest.is_file():
packages/tools/src/disco/tools/projects/store.py:259:            data = json.loads(manifest.read_text())
packages/tools/src/disco/tools/projects/store.py:261:            raise StorageError(f"manifest unreadable: {exc}") from exc
packages/tools/src/disco/tools/projects/store.py:262:        workspace = self.path_for(conversation_id)
packages/tools/src/disco/tools/projects/store.py:269:            conversation_id=conversation_id,
packages/tools/src/disco/tools/projects/store.py:273:            last_snapshot_at=data.get("last_snapshot_at"),
packages/tools/src/disco/tools/projects/store.py:279:    def write_manifest(
packages/tools/src/disco/tools/projects/store.py:281:        conversation_id: str,
packages/tools/src/disco/tools/projects/store.py:289:        """Write the manifest after a snapshot. Atomic via tmp + rename so a
packages/tools/src/disco/tools/projects/store.py:290:        crash mid-write never leaves the file half-written. Returns the manifest
packages/tools/src/disco/tools/projects/store.py:292:        manifest = self.manifest_for(conversation_id)
packages/tools/src/disco/tools/projects/store.py:293:        manifest.parent.mkdir(parents=True, exist_ok=True)
packages/tools/src/disco/tools/projects/store.py:295:            "conversation_id": conversation_id,
packages/tools/src/disco/tools/projects/store.py:299:            "last_snapshot_at": _now_iso(),
packages/tools/src/disco/tools/projects/store.py:303:        tmp = manifest.with_suffix(manifest.suffix + ".tmp")
packages/tools/src/disco/tools/projects/store.py:305:        tmp.replace(manifest)  # atomic on POSIX; close-enough elsewhere
packages/tools/src/disco/tools/projects/store.py:306:        return manifest
packages/tools/src/disco/tools/projects/store.py:308:    def delete(self, conversation_id: str) -> bool:
packages/tools/src/disco/tools/projects/store.py:309:        """Remove a project's manifest + workspace. The conversation events in
packages/tools/src/disco/tools/projects/store.py:314:        project_dir = self._root / conversation_id
packages/tools/src/disco/tools/projects/store.py:320:    def iter_workspace(self, conversation_id: str) -> Iterator[Path]:
packages/tools/src/disco/tools/projects/store.py:323:        workspace = self.path_for(conversation_id)
packages/tools/src/disco/tools/projects/__init__.py:7:  (snapshot OUT to disk, rehydrate IN from disk, stream a zip for download).
packages/tools/src/disco/tools/projects/__init__.py:8:- `store`: per-project manifest + workspace tree on disk, with named typed
packages/tools/src/disco/tools/projects/__init__.py:19:    snapshot_workspace,
packages/tools/src/disco/tools/projects/__init__.py:27:    default_projects_root,
packages/tools/src/disco/tools/projects/__init__.py:28:    resolve_projects_root,
packages/tools/src/disco/tools/projects/__init__.py:39:    "default_projects_root",
packages/tools/src/disco/tools/projects/__init__.py:41:    "resolve_projects_root",
packages/tools/src/disco/tools/projects/__init__.py:42:    "snapshot_workspace",
packages/tools/src/disco/tools/projects/archive.py:1:"""Workspace snapshot / rehydrate / zip — transport-agnostic, via SandboxInstance.
packages/tools/src/disco/tools/projects/archive.py:30:# Dependency/cache directories are never snapshotted: they are reproducible from
packages/tools/src/disco/tools/projects/archive.py:31:# the source tree (package.json / lockfiles ARE snapshotted), and walking them
packages/tools/src/disco/tools/projects/archive.py:34:# transport and died with a broken pipe, and the manifest was never written.
packages/tools/src/disco/tools/projects/archive.py:66:    """A snapshot or rehydrate hit a hard limit / I/O failure. Carries the offending
packages/tools/src/disco/tools/projects/archive.py:72:    """What a snapshot produced. Returned to the agent-server so it can update
packages/tools/src/disco/tools/projects/archive.py:73:    the manifest + tell the model/user what was saved."""
packages/tools/src/disco/tools/projects/archive.py:81:# ---- snapshot OUT ----------------------------------------------------------
packages/tools/src/disco/tools/projects/archive.py:84:async def snapshot_workspace(
packages/tools/src/disco/tools/projects/archive.py:100:    entry no longer aborts the snapshot — it's recorded in `result.skipped`
packages/tools/src/disco/tools/projects/archive.py:101:    and the walk continues, so the manifest still gets written and the project
packages/tools/src/disco/tools/projects/archive.py:120:                f"{reason}) — transport presumed dead, aborting snapshot"
packages/tools/src/disco/tools/projects/archive.py:138:                continue  # reproducible dependency/cache tree — never snapshot
packages/tools/src/disco/tools/projects/archive.py:176:    """Walk a snapshot tree on disk and write_file each entry into the sandbox.
packages/tools/src/disco/tools/projects/archive.py:202:    Build the zip into a BytesIO buffer (the tree is bounded by the snapshot
packages/agent-server/src/disco/agent_server/schedule_models.py:31:    conversation_id: str
packages/agent-server/src/disco/agent_server/schedule_models.py:44:            "conversation_id": self.conversation_id,
packages/agent-server/src/disco/agent_server/schedule_models.py:61:            conversation_id=row["conversation_id"],
packages/agent-server/src/disco/agent_server/schedule_models.py:78:    conversation_id: str
packages/agent-server/src/disco/agent_server/schedule_models.py:86:            "conversation_id": self.conversation_id,
packages/agent-server/src/disco/agent_server/schedule.py:121:        conversation_id: str,
packages/agent-server/src/disco/agent_server/schedule.py:136:            conversation_id=conversation_id,
packages/agent-server/src/disco/agent_server/schedule.py:149:        self, *, owner_id: str, conversation_id: str | None = None
packages/agent-server/src/disco/agent_server/schedule.py:151:        rows = self._store.list_schedules(owner_id=owner_id, conversation_id=conversation_id)
packages/agent-server/src/disco/agent_server/schedule.py:223:    async def _original_query(self, conversation_id: str) -> str | None:
packages/agent-server/src/disco/agent_server/schedule.py:229:        for e in await self._store.get_events(conversation_id):
packages/agent-server/src/disco/agent_server/schedule.py:245:            query = await self._original_query(sched.conversation_id)
packages/agent-server/src/disco/agent_server/schedule.py:250:                    sched.conversation_id,
packages/agent-server/src/disco/agent_server/schedule.py:256:            self._runtime.set_model_override(sched.conversation_id, sched.model_override)
packages/agent-server/src/disco/agent_server/schedule.py:258:                self._runtime.set_depth(sched.conversation_id, sched.depth)
packages/agent-server/src/disco/agent_server/schedule.py:262:                sched.conversation_id,
packages/agent-server/src/disco/agent_server/schedule.py:272:                sched.conversation_id,
packages/agent-server/src/disco/agent_server/schedule.py:282:                    conversation_id=sched.conversation_id,
packages/agent-server/src/disco/agent_server/schedule.py:288:            self._runtime.kick(sched.conversation_id)
packages/agent-server/src/disco/agent_server/schedule.py:292:                sched.conversation_id,
packages/agent-server/src/disco/agent_server/schedule.py:299:                sched.conversation_id,
packages/agent-server/src/disco/agent_server/tts_local.py:46:    d = Path(base)
packages/agent-server/src/disco/agent_server/schedule_service.py:44:        conversation_id: str,
packages/agent-server/src/disco/agent_server/schedule_service.py:54:            conversation_id=conversation_id,
packages/agent-server/src/disco/agent_server/schedule_service.py:64:        self, *, owner_id: str, conversation_id: str | None = None
packages/agent-server/src/disco/agent_server/schedule_service.py:70:                owner_id=owner_id, conversation_id=conversation_id
packages/agent-server/src/disco/agent_server/mcp_manager.py:366:        Scans both the stdio pool snapshot and HTTP tools for search/fetch-shaped
packages/agent-server/src/disco/agent_server/mcp_manager.py:377:            for tdef in self._rt._mcp_pool.snapshot():
packages/agent-server/src/disco/agent_server/share_service.py:42:        conversation_id: str,
packages/agent-server/src/disco/agent_server/share_service.py:62:          - `conversation_id`, `owner_id`, `exported_at`: provenance.
packages/agent-server/src/disco/agent_server/share_service.py:81:        row = next((s for s in summaries if s.conversation_id == conversation_id), None)
packages/agent-server/src/disco/agent_server/share_service.py:87:        events = await self._store.get_events(conversation_id)
packages/agent-server/src/disco/agent_server/share_service.py:88:        state = await self._store.get_state(conversation_id)
packages/agent-server/src/disco/agent_server/share_service.py:114:            "conversation_id": conversation_id,
packages/agent-server/src/disco/agent_server/share_service.py:116:            "surface": row.surface or self._surface_of(conversation_id),
packages/agent-server/src/disco/agent_server/share_service.py:140:        that the server edge enforces read-only against. Returns {ok, conversation_id}
packages/agent-server/src/disco/agent_server/share_service.py:186:        cid = f"conv_{_uuid.uuid4().hex}"  # importer-minted — never trust bundle.conversation_id
packages/agent-server/src/disco/agent_server/share_service.py:191:        return {"ok": True, "conversation_id": cid}
packages/agent-server/src/disco/agent_server/share_service.py:195:        conversation_id: str,
packages/agent-server/src/disco/agent_server/share_service.py:209:        conversation_id: str,
packages/agent-server/src/disco/agent_server/share_service.py:232:        row = next((s for s in summaries if s.conversation_id == conversation_id), None)
packages/agent-server/src/disco/agent_server/share_service.py:240:        state = await self._store.get_state(conversation_id)
packages/agent-server/src/disco/agent_server/share_service.py:246:            conversation_id,
packages/agent-server/src/disco/agent_server/share_service.py:253:            "conversation_id": conversation_id,
packages/agent-server/src/disco/agent_server/share_service.py:259:        """Resolve a share token to its (conversation_id, owner_id) row.
packages/agent-server/src/disco/agent_server/sessions_service.py:6:(`sessions_snapshot`), the coalesced capture-pane read (`session_view`), and
packages/agent-server/src/disco/agent_server/sessions_service.py:17:`sessions_list` stays on the runtime and calls the `sessions_snapshot`
packages/agent-server/src/disco/agent_server/sessions_service.py:37:    def upload_session(self, conversation_id: str) -> SandboxSession:
packages/agent-server/src/disco/agent_server/sessions_service.py:40:        executor = self._rt._executors.get(conversation_id)
packages/agent-server/src/disco/agent_server/sessions_service.py:43:        if conversation_id not in self._rt._pending_sessions:
packages/agent-server/src/disco/agent_server/sessions_service.py:44:            self._rt._pending_sessions[conversation_id] = SandboxSession(
packages/agent-server/src/disco/agent_server/sessions_service.py:47:                    surface=self._rt._surface_of(conversation_id),
packages/agent-server/src/disco/agent_server/sessions_service.py:50:                conversation_id=conversation_id,
packages/agent-server/src/disco/agent_server/sessions_service.py:51:                on_recreate=lambda: self._rt._rehydrate_after_recreate(conversation_id),
packages/agent-server/src/disco/agent_server/sessions_service.py:53:        return self._rt._pending_sessions[conversation_id]
packages/agent-server/src/disco/agent_server/sessions_service.py:55:    async def sessions_snapshot(
packages/agent-server/src/disco/agent_server/sessions_service.py:56:        self, conversation_id: str
packages/agent-server/src/disco/agent_server/sessions_service.py:62:        session = self._rt.live_session(conversation_id)
packages/agent-server/src/disco/agent_server/sessions_service.py:72:                self._rt._last_sessions[conversation_id] = filtered
packages/agent-server/src/disco/agent_server/sessions_service.py:77:            "sessions_snapshot: %s failed after %d attempts: %s",
packages/agent-server/src/disco/agent_server/sessions_service.py:78:            conversation_id,
packages/agent-server/src/disco/agent_server/sessions_service.py:82:        return (self._rt._last_sessions.get(conversation_id, []), True)
packages/agent-server/src/disco/agent_server/sessions_service.py:85:        self, conversation_id: str, name: str, tail_chars: int
packages/agent-server/src/disco/agent_server/sessions_service.py:89:        session = self._rt.live_session(conversation_id)
packages/agent-server/src/disco/agent_server/sessions_service.py:92:        key = (conversation_id, name)
packages/agent-server/src/disco/agent_server/preview_service.py:52:    def port_upstream(self, conversation_id: str, port: int) -> str | None:
packages/agent-server/src/disco/agent_server/preview_service.py:64:        executor = self._rt._executors.get(conversation_id)
packages/agent-server/src/disco/agent_server/preview_service.py:87:                s.conversation_id
packages/agent-server/src/disco/agent_server/preview_service.py:89:                if s.conversation_id.removeprefix("conv_").startswith(cid8)
packages/agent-server/src/disco/agent_server/preview_service.py:116:    async def preview(self, conversation_id: str) -> dict[str, Any]:
packages/agent-server/src/disco/agent_server/preview_service.py:121:        executor = self._rt._executors.get(conversation_id)
packages/agent-server/src/disco/agent_server/preview_service.py:178:    async def ensure_preview(self, conversation_id: str) -> bool:
packages/agent-server/src/disco/agent_server/preview_service.py:185:        rehydrate the snapshot, then start the preview — the user explicitly asked to
packages/agent-server/src/disco/agent_server/preview_service.py:186:        see the artifact again, and that's exactly what the snapshot is for."""
packages/agent-server/src/disco/agent_server/preview_service.py:187:        executor = self._rt._executors.get(conversation_id)
packages/agent-server/src/disco/agent_server/preview_service.py:193:                record = store.get(conversation_id)
packages/agent-server/src/disco/agent_server/preview_service.py:194:            except Exception:  # noqa: BLE001 — manifest unreadable: nothing to revive
packages/agent-server/src/disco/agent_server/preview_service.py:197:                return False  # never had a build snapshot (e.g. research) — no preview
packages/agent-server/src/disco/agent_server/preview_service.py:198:            self._rt._loop_for(conversation_id)  # registers a fresh executor + lazy sandbox
packages/agent-server/src/disco/agent_server/preview_service.py:199:            await self._rt._maybe_rehydrate(conversation_id)
packages/agent-server/src/disco/agent_server/preview_service.py:200:            executor = self._rt._executors.get(conversation_id)
packages/agent-server/src/disco/agent_server/app.py:45:from .routes._common import _sanitize_name, make_preview_upstream_resolver
packages/agent-server/src/disco/agent_server/app.py:48:# `_sanitize_name` is re-exported here for tests that import it from this module
packages/agent-server/src/disco/agent_server/app.py:50:__all__ = ["_sanitize_name", "create_app"]
packages/agent-server/src/disco/agent_server/report_audio.py:16:    are isolated by `<cache>/<conversation_id>/` so the GET jail can resolve a
packages/agent-server/src/disco/agent_server/report_audio.py:125:    Mirrors `default_projects_root` (packages/tools/src/disco/tools/projects/store.py):
packages/agent-server/src/disco/agent_server/report_audio.py:134:        return Path(data_dir) / "cache" / "tts"
packages/agent-server/src/disco/agent_server/report_audio.py:136:    return Path(xdg) / "disco" / "cache" / "tts"
packages/agent-server/src/disco/agent_server/report_audio.py:141:    under `<cache>/<conversation_id>/<file>.mp3` / `.md` so the GET jail is just
packages/agent-server/src/disco/agent_server/uploads_ingest.py:45:    ext = PurePosixPath(filename).suffix.lower()
packages/agent-server/src/disco/agent_server/uploads_ingest.py:160:    stem = PurePosixPath(filename).stem
packages/agent-server/src/disco/agent_server/control_ops.py:89:    async def confirm(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/control_ops.py:97:        loop = self._rt._loop_for(conversation_id)
packages/agent-server/src/disco/agent_server/control_ops.py:99:        self._rt.kick(conversation_id)  # continue plan→act→observe past the gate
packages/agent-server/src/disco/agent_server/control_ops.py:101:    async def reject(self, conversation_id: str, reason: str = "rejected by user") -> None:
packages/agent-server/src/disco/agent_server/control_ops.py:104:        loop = self._rt._loop_for(conversation_id)
packages/agent-server/src/disco/agent_server/control_ops.py:106:        self._rt.kick(conversation_id)
packages/agent-server/src/disco/agent_server/control_ops.py:108:    async def approve_plan(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/control_ops.py:112:        loop = self._rt._loop_for(conversation_id)
packages/agent-server/src/disco/agent_server/control_ops.py:114:        self._rt.kick(conversation_id)  # start building the approved plan
packages/agent-server/src/disco/agent_server/control_ops.py:116:    async def request_plan(self, conversation_id: str, text: str = "") -> None:
packages/agent-server/src/disco/agent_server/control_ops.py:132:        state = await self._rt._store.get_state(conversation_id)
packages/agent-server/src/disco/agent_server/control_ops.py:138:                    conversation_id,
packages/agent-server/src/disco/agent_server/control_ops.py:145:        loop = self._rt._loop_for(conversation_id)
packages/agent-server/src/disco/agent_server/control_ops.py:147:        self._rt.kick(conversation_id)  # produce the (revised) plan
packages/agent-server/src/disco/agent_server/control_ops.py:149:    async def pause(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/control_ops.py:155:        loop = self._rt._loops.get(conversation_id)
packages/agent-server/src/disco/agent_server/control_ops.py:159:    async def cancel(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/control_ops.py:163:        self._rt._cancel_flags.setdefault(conversation_id, asyncio.Event()).set()
packages/agent-server/src/disco/agent_server/control_ops.py:164:        loop = self._rt._loops.get(conversation_id)
packages/agent-server/src/disco/agent_server/control_ops.py:168:    async def kill(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/control_ops.py:174:        task = self._rt._tasks.pop(conversation_id, None)
packages/agent-server/src/disco/agent_server/control_ops.py:186:        executor = self._rt._executors.pop(conversation_id, None)
packages/agent-server/src/disco/agent_server/control_ops.py:189:        pending = self._rt._pending_sessions.pop(conversation_id, None)
packages/agent-server/src/disco/agent_server/control_ops.py:193:        self._rt._loops.pop(conversation_id, None)
packages/agent-server/src/disco/agent_server/control_ops.py:197:            conversation_id,
packages/agent-server/src/disco/agent_server/routes/debug.py:45:    @router.get("/api/debug/trace/{conversation_id}")
packages/agent-server/src/disco/agent_server/routes/debug.py:46:    async def trace(conversation_id: str) -> JSONResponse:
packages/agent-server/src/disco/agent_server/routes/debug.py:53:        snap = registry().snapshot(conversation_id)
packages/agent-server/src/disco/agent_server/routes/debug.py:56:                {"error": "no trace", "conversation_id": conversation_id},
packages/agent-server/src/disco/agent_server/routes/debug.py:61:    @router.get("/api/debug/evidence/{conversation_id}")
packages/agent-server/src/disco/agent_server/routes/debug.py:62:    async def evidence(conversation_id: str) -> JSONResponse:
packages/agent-server/src/disco/agent_server/routes/debug.py:64:        inspect trace, and project manifest (metadata only — no file contents).
packages/agent-server/src/disco/agent_server/routes/debug.py:74:        Path-traversal safety: FastAPI's ``{conversation_id}`` path segment does
packages/agent-server/src/disco/agent_server/routes/debug.py:82:            await store.get_state(conversation_id)
packages/agent-server/src/disco/agent_server/routes/debug.py:88:            for e in await store.get_events(conversation_id)
packages/agent-server/src/disco/agent_server/routes/debug.py:93:        inspect_trace: dict[str, Any] | None = registry().snapshot(conversation_id)
packages/agent-server/src/disco/agent_server/routes/debug.py:95:        # Project manifest — metadata only (title, dates, counts, last deliverable).
packages/agent-server/src/disco/agent_server/routes/debug.py:98:        project_manifest: dict[str, Any] | None = None
packages/agent-server/src/disco/agent_server/routes/debug.py:104:                    record = ps.get(conversation_id)
packages/agent-server/src/disco/agent_server/routes/debug.py:106:                        project_manifest = {
packages/agent-server/src/disco/agent_server/routes/debug.py:107:                            "conversation_id": conversation_id,
packages/agent-server/src/disco/agent_server/routes/debug.py:110:                            "last_snapshot_at": record.last_snapshot_at,
packages/agent-server/src/disco/agent_server/routes/debug.py:116:            "conversation_id": conversation_id,
packages/agent-server/src/disco/agent_server/routes/debug.py:120:            "project_manifest": project_manifest,
packages/agent-server/src/disco/agent_server/routes/projects.py:1:"""Build-project routes — persistence list / download / manifest / delete."""
packages/agent-server/src/disco/agent_server/routes/projects.py:25:        """List Build projects under the configured projects_root, joined with
packages/agent-server/src/disco/agent_server/routes/projects.py:37:        # come from the authoritative store (manifest can drift on rename).
packages/agent-server/src/disco/agent_server/routes/projects.py:41:        by_id = {s.conversation_id: s for s in summaries}
packages/agent-server/src/disco/agent_server/routes/projects.py:44:            s = by_id.get(r.conversation_id)
packages/agent-server/src/disco/agent_server/routes/projects.py:47:                    "id": r.conversation_id,
packages/agent-server/src/disco/agent_server/routes/projects.py:52:                    # surfaces are the only ones that snapshot, so default to "build".
packages/agent-server/src/disco/agent_server/routes/projects.py:55:                    "last_snapshot_at": r.last_snapshot_at,
packages/agent-server/src/disco/agent_server/routes/projects.py:77:        untitled = [s.conversation_id for s in summaries if not s.title]
packages/agent-server/src/disco/agent_server/routes/projects.py:81:    @router.get("/api/projects/{conversation_id}/download")
packages/agent-server/src/disco/agent_server/routes/projects.py:82:    async def download_project(conversation_id: str) -> StreamingResponse:
packages/agent-server/src/disco/agent_server/routes/projects.py:85:        have been deleted under the manifest."""
packages/agent-server/src/disco/agent_server/routes/projects.py:92:        record = ps.get(conversation_id)
packages/agent-server/src/disco/agent_server/routes/projects.py:97:        workspace = ps.path_for(conversation_id)
packages/agent-server/src/disco/agent_server/routes/projects.py:100:                f'attachment; filename="{conversation_id}.zip"'
packages/agent-server/src/disco/agent_server/routes/projects.py:109:    @router.get("/api/projects/{conversation_id}/manifest")
packages/agent-server/src/disco/agent_server/routes/projects.py:110:    async def project_manifest(conversation_id: str) -> dict:
packages/agent-server/src/disco/agent_server/routes/projects.py:111:        """Export a JSON manifest of the project: metadata, the file tree (path +
packages/agent-server/src/disco/agent_server/routes/projects.py:118:        record = ps.get(conversation_id)
packages/agent-server/src/disco/agent_server/routes/projects.py:123:        workspace = ps.path_for(conversation_id)
packages/agent-server/src/disco/agent_server/routes/projects.py:133:            for e in reversed(await store.get_events(conversation_id)):
packages/agent-server/src/disco/agent_server/routes/projects.py:143:            "conversation_id": conversation_id,
packages/agent-server/src/disco/agent_server/routes/projects.py:146:            "last_snapshot_at": record.last_snapshot_at,
packages/agent-server/src/disco/agent_server/routes/projects.py:153:    @router.delete("/api/projects/{conversation_id}")
packages/agent-server/src/disco/agent_server/routes/projects.py:154:    async def delete_project(conversation_id: str) -> dict:
packages/agent-server/src/disco/agent_server/routes/projects.py:155:        """Remove a project's manifest + workspace from disk. The conversation
packages/agent-server/src/disco/agent_server/routes/projects.py:164:        deleted = ps.delete(conversation_id)
packages/agent-server/src/disco/agent_server/routes/projects.py:165:        return {"id": conversation_id, "deleted": deleted}
packages/agent-server/src/disco/agent_server/lifecycle.py:1:"""Sandbox lifecycle — auto-suspend, idle sweep, orphan reconcile, snapshot/
packages/agent-server/src/disco/agent_server/lifecycle.py:14:    _rematerialize_uploads / _maybe_snapshot
packages/agent-server/src/disco/agent_server/lifecycle.py:50:    snapshot_workspace,
packages/agent-server/src/disco/agent_server/lifecycle.py:62:    async def _teardown_sandbox(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/lifecycle.py:64:        the idle preview server) while KEEPING the event log + the project snapshot. A
packages/agent-server/src/disco/agent_server/lifecycle.py:66:        workspace is durable (snapshotted) first — this does not snapshot."""
packages/agent-server/src/disco/agent_server/lifecycle.py:67:        executor = self._rt._executors.pop(conversation_id, None)
packages/agent-server/src/disco/agent_server/lifecycle.py:77:            clear_conversation_read_state(conversation_id)
packages/agent-server/src/disco/agent_server/lifecycle.py:78:        pending = self._rt._pending_sessions.pop(conversation_id, None)
packages/agent-server/src/disco/agent_server/lifecycle.py:82:        self._rt._loops.pop(conversation_id, None)  # force a fresh sandbox on the next run
packages/agent-server/src/disco/agent_server/lifecycle.py:86:        for key in [k for k in self._rt._session_view_cache if k[0] == conversation_id]:
packages/agent-server/src/disco/agent_server/lifecycle.py:88:        for key in [k for k in self._rt._session_view_locks if k[0] == conversation_id]:
packages/agent-server/src/disco/agent_server/lifecycle.py:90:        self._rt._wake_locks.pop(conversation_id, None)
packages/agent-server/src/disco/agent_server/lifecycle.py:91:        self._rt._last_sessions.pop(conversation_id, None)  # don't ghost a stale list (DC-04b)
packages/agent-server/src/disco/agent_server/lifecycle.py:93:        # snapshot into a fresh sandbox. Clear the rehydrate-once flag — otherwise
packages/agent-server/src/disco/agent_server/lifecycle.py:99:            rehydrated.discard(conversation_id)
packages/agent-server/src/disco/agent_server/lifecycle.py:215:    def on_connect(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/lifecycle.py:218:        self._rt._connections[conversation_id] = self._rt._connections.get(conversation_id, 0) + 1
packages/agent-server/src/disco/agent_server/lifecycle.py:219:        task = self._rt._suspend_tasks.pop(conversation_id, None)
packages/agent-server/src/disco/agent_server/lifecycle.py:223:    def on_disconnect(self, conversation_id: str, *, grace_s: float = 60.0) -> None:
packages/agent-server/src/disco/agent_server/lifecycle.py:227:        n = self._rt._connections.get(conversation_id, 0) - 1
packages/agent-server/src/disco/agent_server/lifecycle.py:229:            self._rt._connections[conversation_id] = n
packages/agent-server/src/disco/agent_server/lifecycle.py:231:        self._rt._connections.pop(conversation_id, None)
packages/agent-server/src/disco/agent_server/lifecycle.py:232:        old = self._rt._suspend_tasks.pop(conversation_id, None)
packages/agent-server/src/disco/agent_server/lifecycle.py:235:        self._rt._suspend_tasks[conversation_id] = asyncio.create_task(
packages/agent-server/src/disco/agent_server/lifecycle.py:236:            self._suspend_after_grace(conversation_id, grace_s)
packages/agent-server/src/disco/agent_server/lifecycle.py:239:    async def _suspend_after_grace(self, conversation_id: str, grace_s: float) -> None:
packages/agent-server/src/disco/agent_server/lifecycle.py:244:        if self._rt._connections.get(conversation_id, 0) <= 0:
packages/agent-server/src/disco/agent_server/lifecycle.py:245:            await self._rt._suspend(conversation_id)
packages/agent-server/src/disco/agent_server/lifecycle.py:246:        self._rt._suspend_tasks.pop(conversation_id, None)
packages/agent-server/src/disco/agent_server/lifecycle.py:248:    async def _suspend(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/lifecycle.py:249:        """Free an IDLE build's sandbox (its last UI closed): snapshot first, then
packages/agent-server/src/disco/agent_server/lifecycle.py:251:        live sandbox, when storage isn't ready (no durable snapshot → keep the
packages/agent-server/src/disco/agent_server/lifecycle.py:254:        the next message) re-creates the sandbox and rehydrates from the snapshot."""
packages/agent-server/src/disco/agent_server/lifecycle.py:255:        if conversation_id not in self._rt._executors:
packages/agent-server/src/disco/agent_server/lifecycle.py:259:        state = await self._rt._store.get_state(conversation_id)
packages/agent-server/src/disco/agent_server/lifecycle.py:263:            await self._rt._maybe_snapshot(conversation_id)
packages/agent-server/src/disco/agent_server/lifecycle.py:264:            await self._rt._teardown_sandbox(conversation_id)
packages/agent-server/src/disco/agent_server/lifecycle.py:265:            _LOG.info("auto-suspended idle conversation %s (no UI connected)", conversation_id)
packages/agent-server/src/disco/agent_server/lifecycle.py:267:    def sandbox_state(self, conversation_id: str) -> str | None:
packages/agent-server/src/disco/agent_server/lifecycle.py:269:        conversation_id. Return 'suspended' when a snapshot record exists (the sandbox
packages/agent-server/src/disco/agent_server/lifecycle.py:271:        (research surface / no snapshot)."""
packages/agent-server/src/disco/agent_server/lifecycle.py:272:        if conversation_id in self._rt._executors or conversation_id in self._rt._pending_sessions:
packages/agent-server/src/disco/agent_server/lifecycle.py:278:            record = store.get(conversation_id)
packages/agent-server/src/disco/agent_server/lifecycle.py:279:        except Exception:  # noqa: BLE001 — unreadable manifest: treat as absent
packages/agent-server/src/disco/agent_server/lifecycle.py:348:    async def _maybe_rehydrate(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/lifecycle.py:349:        """Restore the workspace files from a prior snapshot into the live
packages/agent-server/src/disco/agent_server/lifecycle.py:350:        sandbox, if a snapshot exists. Idempotent: tracked via an in-process
packages/agent-server/src/disco/agent_server/lifecycle.py:355:        if conversation_id in self._rt._rehydrated:
packages/agent-server/src/disco/agent_server/lifecycle.py:357:        self._rt._rehydrated.add(conversation_id)
packages/agent-server/src/disco/agent_server/lifecycle.py:363:            record = store.get(conversation_id)
packages/agent-server/src/disco/agent_server/lifecycle.py:364:        except Exception:  # noqa: BLE001 — manifest unreadable: treat as no record
packages/agent-server/src/disco/agent_server/lifecycle.py:368:        executor = self._rt._executors.get(conversation_id)
packages/agent-server/src/disco/agent_server/lifecycle.py:373:            await rehydrate_workspace(session, store.path_for(conversation_id))
packages/agent-server/src/disco/agent_server/lifecycle.py:376:                conversation_id,
packages/agent-server/src/disco/agent_server/lifecycle.py:380:    async def _rehydrate_after_recreate(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/lifecycle.py:387:        Best-effort: with no snapshot yet (first run), there is nothing to
packages/agent-server/src/disco/agent_server/lifecycle.py:391:            rehydrated.discard(conversation_id)
packages/agent-server/src/disco/agent_server/lifecycle.py:392:        await self._rt._maybe_rehydrate(conversation_id)
packages/agent-server/src/disco/agent_server/lifecycle.py:394:        await self._rt._rematerialize_uploads(conversation_id)
packages/agent-server/src/disco/agent_server/lifecycle.py:396:    async def _rematerialize_uploads(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/lifecycle.py:403:        executor = self._rt._executors.get(conversation_id)
packages/agent-server/src/disco/agent_server/lifecycle.py:406:            session = self._rt._pending_sessions.get(conversation_id)
packages/agent-server/src/disco/agent_server/lifecycle.py:411:        uploads_dir = Path(self._rt._uploads_base) / conversation_id
packages/agent-server/src/disco/agent_server/lifecycle.py:426:                        p.name, conversation_id,
packages/agent-server/src/disco/agent_server/lifecycle.py:431:                written, conversation_id,
packages/agent-server/src/disco/agent_server/lifecycle.py:434:    async def _maybe_snapshot(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/lifecycle.py:435:        """Mirror the live workspace out to disk + update the manifest."""
packages/agent-server/src/disco/agent_server/lifecycle.py:442:                conversation_id,
packages/agent-server/src/disco/agent_server/lifecycle.py:446:        executor = self._rt._executors.get(conversation_id)
packages/agent-server/src/disco/agent_server/lifecycle.py:459:            row = next((s for s in summaries if s.conversation_id == conversation_id), None)
packages/agent-server/src/disco/agent_server/lifecycle.py:467:            result = await snapshot_workspace(session, store.path_for(conversation_id))
packages/agent-server/src/disco/agent_server/lifecycle.py:468:            store.write_manifest(
packages/agent-server/src/disco/agent_server/lifecycle.py:469:                conversation_id,
packages/agent-server/src/disco/agent_server/lifecycle.py:478:                conversation_id,
packages/agent-server/src/disco/agent_server/lifecycle.py:479:                f"snapshot failed: {exc}",
packages/agent-server/src/disco/agent_server/runtime.py:89:# MCP client pool (RP-05 rung A) — built once at start, snapshotted per conversation.
packages/agent-server/src/disco/agent_server/runtime.py:300:# as the build loop's live workspace snapshot: read the truth, don't assume it.
packages/agent-server/src/disco/agent_server/runtime.py:477:        # manifest to derive "build" but the manifest is only written by
packages/agent-server/src/disco/agent_server/runtime.py:478:        # _maybe_snapshot (which no-ops without a configured projects_root), so a
packages/agent-server/src/disco/agent_server/runtime.py:566:        # Keyed by conversation_id; value is a flat list of Passage objects.
packages/agent-server/src/disco/agent_server/runtime.py:610:        # snapshot+rehydrate). _connections/_suspend_tasks stay on the runtime
packages/agent-server/src/disco/agent_server/runtime.py:684:        conversation_id: str | None = None,
packages/agent-server/src/disco/agent_server/runtime.py:726:        sink = routing_sink_for(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:743:    # workspace snapshot/rehydrate). "build" and "agent" are behaviorally identical;
packages/agent-server/src/disco/agent_server/runtime.py:754:    def set_surface(self, conversation_id: str, surface: str) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:760:        self._surface[conversation_id] = (
packages/agent-server/src/disco/agent_server/runtime.py:765:    def _surface_of(self, conversation_id: str) -> str:
packages/agent-server/src/disco/agent_server/runtime.py:773:          "agent" from "build" (the project manifest can't — they snapshot
packages/agent-server/src/disco/agent_server/runtime.py:775:          Build with no manifest yet mis-derived deep_research below.
packages/agent-server/src/disco/agent_server/runtime.py:776:        - a project manifest on disk → a build-like surface (Research never snapshots).
packages/agent-server/src/disco/agent_server/runtime.py:780:        cached = self._surface.get(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:790:                    "SELECT surface FROM conversations WHERE conversation_id = ?",
packages/agent-server/src/disco/agent_server/runtime.py:791:                    (conversation_id,),
packages/agent-server/src/disco/agent_server/runtime.py:794:                    self._surface[conversation_id] = row["surface"]
packages/agent-server/src/disco/agent_server/runtime.py:802:                if store.get(conversation_id) is not None:
packages/agent-server/src/disco/agent_server/runtime.py:805:                    self._surface[conversation_id] = "build"
packages/agent-server/src/disco/agent_server/runtime.py:819:                        "SELECT DISTINCT kind FROM events WHERE conversation_id = ?",
packages/agent-server/src/disco/agent_server/runtime.py:820:                        (conversation_id,),
packages/agent-server/src/disco/agent_server/runtime.py:824:                    self._surface[conversation_id] = "deep_research"
packages/agent-server/src/disco/agent_server/runtime.py:829:                # an on-disk project manifest, caught above).
packages/agent-server/src/disco/agent_server/runtime.py:831:                    self._surface[conversation_id] = "deep_research"
packages/agent-server/src/disco/agent_server/runtime.py:908:    def set_model_override(self, conversation_id: str, model_id: str | None) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:909:        self._settings.set_model_override(conversation_id, model_id)
packages/agent-server/src/disco/agent_server/runtime.py:923:    def set_autonomous(self, conversation_id: str, value: bool = True) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:924:        self._settings.set_autonomous(conversation_id, value)
packages/agent-server/src/disco/agent_server/runtime.py:926:    def _effective_autonomous(self, conversation_id: str) -> bool:
packages/agent-server/src/disco/agent_server/runtime.py:927:        return self._settings._effective_autonomous(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:929:    def is_autonomous(self, conversation_id: str) -> bool:
packages/agent-server/src/disco/agent_server/runtime.py:930:        return self._settings.is_autonomous(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:941:    def set_assist(self, conversation_id: str, value: bool = True) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:942:        self._settings.set_assist(conversation_id, value)
packages/agent-server/src/disco/agent_server/runtime.py:944:    def _effective_policy(self, conversation_id: str) -> ModelExecutionPolicy:
packages/agent-server/src/disco/agent_server/runtime.py:947:        return self._settings._effective_policy(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:950:        self, conversation_id: str
packages/agent-server/src/disco/agent_server/runtime.py:954:        return self._settings._effective_driver_endpoint(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:956:    def _effective_assist(self, conversation_id: str) -> bool:
packages/agent-server/src/disco/agent_server/runtime.py:957:        return self._settings._effective_assist(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:959:    def is_assist(self, conversation_id: str) -> bool:
packages/agent-server/src/disco/agent_server/runtime.py:960:        return self._settings.is_assist(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:964:        conversation_id: str,
packages/agent-server/src/disco/agent_server/runtime.py:972:            conversation_id, model_override=model_override, assist=assist
packages/agent-server/src/disco/agent_server/runtime.py:977:    def set_artifact_mode(self, conversation_id: str, on: bool) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:978:        self._settings.set_artifact_mode(conversation_id, on)
packages/agent-server/src/disco/agent_server/runtime.py:980:    def _effective_artifact_mode(self, conversation_id: str) -> bool:
packages/agent-server/src/disco/agent_server/runtime.py:981:        return self._settings._effective_artifact_mode(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1061:    def _loop_for(self, conversation_id: str) -> AgentLoop:
packages/agent-server/src/disco/agent_server/runtime.py:1062:        loop = self._loops.get(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1068:            override = self._model_override.get(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1071:            surface = self._surface_of(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1074:            autonomous = self._effective_autonomous(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1079:                conversation_id=conversation_id,
packages/agent-server/src/disco/agent_server/runtime.py:1087:                    router, conversation_id=conversation_id, model_override=override
packages/agent-server/src/disco/agent_server/runtime.py:1091:                    router, conversation_id=conversation_id, model_override=override
packages/agent-server/src/disco/agent_server/runtime.py:1094:                loop = self._compose_build_loop(conversation_id, router, agent)
packages/agent-server/src/disco/agent_server/runtime.py:1096:                loop = self._compose_deep_research_loop(conversation_id, router, agent)
packages/agent-server/src/disco/agent_server/runtime.py:1099:                    conversation_id,
packages/agent-server/src/disco/agent_server/runtime.py:1116:            loop.stream_sink = lambda frame, _cid=conversation_id: self._store.publish_ephemeral(
packages/agent-server/src/disco/agent_server/runtime.py:1119:            self._loops[conversation_id] = loop
packages/agent-server/src/disco/agent_server/runtime.py:1160:    def upload_session(self, conversation_id: str) -> SandboxSession:
packages/agent-server/src/disco/agent_server/runtime.py:1161:        return self._sessions.upload_session(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1163:    def store_upload(self, conversation_id: str, filename: str, data: bytes) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1167:        path = Path(self._uploads_base) / conversation_id / filename
packages/agent-server/src/disco/agent_server/runtime.py:1171:    def get_upload_names(self, conversation_id: str) -> set[str]:
packages/agent-server/src/disco/agent_server/runtime.py:1175:        path = Path(self._uploads_base) / conversation_id
packages/agent-server/src/disco/agent_server/runtime.py:1180:    def get_upload_size(self, conversation_id: str) -> int:
packages/agent-server/src/disco/agent_server/runtime.py:1184:        path = Path(self._uploads_base) / conversation_id
packages/agent-server/src/disco/agent_server/runtime.py:1189:    def add_upload_passages(self, conversation_id: str, passages: list[Any]) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1192:        files. The passages are keyed by conversation_id and retrieved at run
packages/agent-server/src/disco/agent_server/runtime.py:1194:        bucket = self._upload_passages.setdefault(conversation_id, [])
packages/agent-server/src/disco/agent_server/runtime.py:1197:    def get_upload_passages(self, conversation_id: str) -> list[Any]:
packages/agent-server/src/disco/agent_server/runtime.py:1200:        return list(self._upload_passages.get(conversation_id, []))
packages/agent-server/src/disco/agent_server/runtime.py:1203:        self, conversation_id: str, router: DefaultLLMRouter, agent: RouterAgent
packages/agent-server/src/disco/agent_server/runtime.py:1213:        session = self._pending_sessions.pop(conversation_id, None)
packages/agent-server/src/disco/agent_server/runtime.py:1218:                    surface=self._surface_of(conversation_id),
packages/agent-server/src/disco/agent_server/runtime.py:1221:                conversation_id=conversation_id,
packages/agent-server/src/disco/agent_server/runtime.py:1222:                # Mid-run death (transport drop / OOM): restore the last snapshot
packages/agent-server/src/disco/agent_server/runtime.py:1224:                on_recreate=lambda: self._rehydrate_after_recreate(conversation_id),
packages/agent-server/src/disco/agent_server/runtime.py:1230:        model_policy = self._effective_policy(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1233:        _art_mode = self._effective_artifact_mode(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1246:            conversation_id=conversation_id,
packages/agent-server/src/disco/agent_server/runtime.py:1250:            driver_llm=self._effective_driver_endpoint(conversation_id),
packages/agent-server/src/disco/agent_server/runtime.py:1253:        # snapshot (stdio) AND the HTTP-managed tools (rung B streamable_http).
packages/agent-server/src/disco/agent_server/runtime.py:1254:        # The snapshot is frozen per conversation — list_changed notifications do
packages/agent-server/src/disco/agent_server/runtime.py:1258:            all_mcp_tools.extend(self._mcp_pool.snapshot())
packages/agent-server/src/disco/agent_server/runtime.py:1286:                    conversation_id,
packages/agent-server/src/disco/agent_server/runtime.py:1296:        self._executors[conversation_id] = executor
packages/agent-server/src/disco/agent_server/runtime.py:1306:                conversation_id,
packages/agent-server/src/disco/agent_server/runtime.py:1320:                autonomous=self._effective_autonomous(conversation_id),
packages/agent-server/src/disco/agent_server/runtime.py:1324:            conversation_id,
packages/agent-server/src/disco/agent_server/runtime.py:1351:            autonomous=self._effective_autonomous(conversation_id),
packages/agent-server/src/disco/agent_server/runtime.py:1358:        self, conversation_id: str, router: DefaultLLMRouter, agent: RouterAgent
packages/agent-server/src/disco/agent_server/runtime.py:1360:        return self._dr._compose_deep_research_loop(conversation_id, router, agent)
packages/agent-server/src/disco/agent_server/runtime.py:1362:    def _depth_for(self, conversation_id: str) -> DepthTier:
packages/agent-server/src/disco/agent_server/runtime.py:1363:        return self._dr._depth_for(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1365:    def set_depth(self, conversation_id: str, tier: str | None) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1366:        return self._dr.set_depth(conversation_id, tier)
packages/agent-server/src/disco/agent_server/runtime.py:1368:    def _iterative_for(self, conversation_id: str) -> bool:
packages/agent-server/src/disco/agent_server/runtime.py:1369:        return self._dr._iterative_for(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1371:    def set_iterative(self, conversation_id: str, enabled: bool) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1372:        return self._dr.set_iterative(conversation_id, enabled)
packages/agent-server/src/disco/agent_server/runtime.py:1374:    def set_recency(self, conversation_id: str, window: str | None) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1375:        return self._dr.set_recency(conversation_id, window)
packages/agent-server/src/disco/agent_server/runtime.py:1388:        conversation_id: str | None = None,
packages/agent-server/src/disco/agent_server/runtime.py:1396:            conversation_id=conversation_id,
packages/agent-server/src/disco/agent_server/runtime.py:1399:    def kick(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1412:        self._title_service.schedule(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1413:        existing = self._tasks.get(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1416:        loop = self._loop_for(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1417:        task = asyncio.create_task(self._run_with_persistence(conversation_id, loop))
packages/agent-server/src/disco/agent_server/runtime.py:1422:            lambda t, _cid=conversation_id: self._on_run_task_done(_cid, t)
packages/agent-server/src/disco/agent_server/runtime.py:1424:        self._tasks[conversation_id] = task
packages/agent-server/src/disco/agent_server/runtime.py:1436:    def _on_run_task_done(self, conversation_id: str, task: asyncio.Task[Any]) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1440:        if self._tasks.get(conversation_id) is task:
packages/agent-server/src/disco/agent_server/runtime.py:1441:            self._tasks.pop(conversation_id, None)
packages/agent-server/src/disco/agent_server/runtime.py:1452:            asyncio.create_task(self._terminalize_crashed(conversation_id, exc))
packages/agent-server/src/disco/agent_server/runtime.py:1455:        self, conversation_id: str, exc: BaseException
packages/agent-server/src/disco/agent_server/runtime.py:1460:            state = await self._store.get_state(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1464:            logger.error("run task for %s crashed: %s", conversation_id, detail)
packages/agent-server/src/disco/agent_server/runtime.py:1466:                conversation_id,
packages/agent-server/src/disco/agent_server/runtime.py:1470:                conversation_id,
packages/agent-server/src/disco/agent_server/runtime.py:1475:            logger.exception("crash terminalization failed for %s", conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1478:        self, conversation_id: str, loop: AgentLoop
packages/agent-server/src/disco/agent_server/runtime.py:1481:        - Build → snapshot+rehydrate the workspace as before.
packages/agent-server/src/disco/agent_server/runtime.py:1486:        surface = self._surface_of(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1495:            await self._maybe_run_deep_research(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1498:            return await self._store.get_state(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1501:        # storage path is configured AND a prior snapshot exists for this cid,
packages/agent-server/src/disco/agent_server/runtime.py:1505:            await self._maybe_rehydrate(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1511:            await self._rematerialize_uploads(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1517:        # model may have built most of the deliverable before getting stuck); snapshot
packages/agent-server/src/disco/agent_server/runtime.py:1528:            await self._maybe_snapshot(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1533:    async def _teardown_sandbox(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1534:        return await self._lifecycle._teardown_sandbox(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1554:    def on_connect(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1555:        return self._lifecycle.on_connect(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1557:    def on_disconnect(self, conversation_id: str, *, grace_s: float = 60.0) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1558:        return self._lifecycle.on_disconnect(conversation_id, grace_s=grace_s)
packages/agent-server/src/disco/agent_server/runtime.py:1560:    async def _suspend(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1561:        return await self._lifecycle._suspend(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1563:    def sandbox_state(self, conversation_id: str) -> str | None:
packages/agent-server/src/disco/agent_server/runtime.py:1564:        return self._lifecycle.sandbox_state(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1572:    async def _maybe_run_deep_research(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1573:        return await self._dr._maybe_run_deep_research(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1575:    async def _propose_deep_research_plan(self, conversation_id: str, events: list) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1576:        return await self._dr._propose_deep_research_plan(conversation_id, events)
packages/agent-server/src/disco/agent_server/runtime.py:1580:        conversation_id: str,
packages/agent-server/src/disco/agent_server/runtime.py:1586:            conversation_id, plan, resume_from=resume_from
packages/agent-server/src/disco/agent_server/runtime.py:1597:        not_writable etc.).  An empty ``projects_root`` resolves to the
packages/agent-server/src/disco/agent_server/runtime.py:1605:        conversation_id: str,
packages/agent-server/src/disco/agent_server/runtime.py:1609:        return await self._share.share_export(conversation_id, owner_id=owner_id)
packages/agent-server/src/disco/agent_server/runtime.py:1624:        conversation_id: str,
packages/agent-server/src/disco/agent_server/runtime.py:1629:        return await self._dr.export_report(conversation_id, fmt, owner_id=owner_id)
packages/agent-server/src/disco/agent_server/runtime.py:1633:        conversation_id: str,
packages/agent-server/src/disco/agent_server/runtime.py:1637:        return self._share.create_share_link(conversation_id, owner_id=owner_id)
packages/agent-server/src/disco/agent_server/runtime.py:1641:        conversation_id: str,
packages/agent-server/src/disco/agent_server/runtime.py:1646:            conversation_id, owner_id=owner_id
packages/agent-server/src/disco/agent_server/runtime.py:1661:        discipline). An empty ``projects_root`` (fresh install) resolves to the
packages/agent-server/src/disco/agent_server/runtime.py:1662:        auto-default path via :func:`resolve_projects_root` inside
packages/agent-server/src/disco/agent_server/runtime.py:1665:        root = self._config_store.load().projects.projects_root
packages/agent-server/src/disco/agent_server/runtime.py:1668:    async def _maybe_rehydrate(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1669:        return await self._lifecycle._maybe_rehydrate(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1671:    async def _rehydrate_after_recreate(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1672:        return await self._lifecycle._rehydrate_after_recreate(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1674:    async def _rematerialize_uploads(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1675:        return await self._lifecycle._rematerialize_uploads(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1677:    async def _maybe_snapshot(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1678:        return await self._lifecycle._maybe_snapshot(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1680:    async def _emit_persistence_reminder(self, conversation_id: str, body: str) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1685:            conversation_id,
packages/agent-server/src/disco/agent_server/runtime.py:1702:    def preview_upstream(self, conversation_id: str) -> str | None:
packages/agent-server/src/disco/agent_server/runtime.py:1706:        return self.port_upstream(conversation_id, PREVIEW_PORT)
packages/agent-server/src/disco/agent_server/runtime.py:1708:    def port_upstream(self, conversation_id: str, port: int) -> str | None:
packages/agent-server/src/disco/agent_server/runtime.py:1709:        return self._preview.port_upstream(conversation_id, port)
packages/agent-server/src/disco/agent_server/runtime.py:1714:    def live_session(self, conversation_id: str) -> SandboxSession | None:
packages/agent-server/src/disco/agent_server/runtime.py:1717:        executor = self._executors.get(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1731:    async def sessions_snapshot(self, conversation_id: str) -> tuple[list[SessionInfo], bool]:
packages/agent-server/src/disco/agent_server/runtime.py:1732:        return await self._sessions.sessions_snapshot(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1734:    async def sessions_list(self, conversation_id: str) -> list[SessionInfo]:
packages/agent-server/src/disco/agent_server/runtime.py:1736:        return (await self.sessions_snapshot(conversation_id))[0]
packages/agent-server/src/disco/agent_server/runtime.py:1742:        self, conversation_id: str, name: str, tail_chars: int
packages/agent-server/src/disco/agent_server/runtime.py:1744:        return await self._sessions.session_view(conversation_id, name, tail_chars)
packages/agent-server/src/disco/agent_server/runtime.py:1746:    async def preview(self, conversation_id: str) -> dict[str, Any]:
packages/agent-server/src/disco/agent_server/runtime.py:1747:        return await self._preview.preview(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1749:    async def ensure_preview(self, conversation_id: str) -> bool:
packages/agent-server/src/disco/agent_server/runtime.py:1750:        return await self._preview.ensure_preview(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1754:    async def confirm(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1755:        return await self._control.confirm(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1757:    async def reject(self, conversation_id: str, reason: str = "rejected by user") -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1758:        return await self._control.reject(conversation_id, reason)
packages/agent-server/src/disco/agent_server/runtime.py:1760:    async def approve_plan(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1761:        return await self._control.approve_plan(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1763:    async def request_plan(self, conversation_id: str, text: str = "") -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1764:        return await self._control.request_plan(conversation_id, text)
packages/agent-server/src/disco/agent_server/runtime.py:1766:    async def pick_alternative(self, conversation_id: str, option_id: str) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1771:        loop = self._loop_for(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1774:    async def pause(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1775:        return await self._control.pause(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1777:    async def cancel(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1778:        return await self._control.cancel(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1780:    async def _drain_finishing_task(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1789:        task = self._tasks.pop(conversation_id, None)
packages/agent-server/src/disco/agent_server/runtime.py:1795:    async def resume(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1799:        self._cancel_flags.pop(conversation_id, None)
packages/agent-server/src/disco/agent_server/runtime.py:1800:        self.kick(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1806:        self, conversation_id: str, events: list
packages/agent-server/src/disco/agent_server/runtime.py:1808:        return await self._resume._reconstruct_resume_context(conversation_id, events)
packages/agent-server/src/disco/agent_server/runtime.py:1810:    async def resume_conversation(self, conversation_id: str) -> dict:
packages/agent-server/src/disco/agent_server/runtime.py:1811:        return await self._resume.resume_conversation(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1813:    async def kill(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/runtime.py:1814:        return await self._control.kill(conversation_id)
packages/agent-server/src/disco/agent_server/runtime.py:1837:        conversation_id: str,
packages/agent-server/src/disco/agent_server/runtime.py:1845:            conversation_id=conversation_id,
packages/agent-server/src/disco/agent_server/runtime.py:1854:        self, *, owner_id: str, conversation_id: str | None = None
packages/agent-server/src/disco/agent_server/runtime.py:1857:            owner_id=owner_id, conversation_id=conversation_id
packages/agent-server/src/disco/agent_server/runtime.py:1868:    def running_conversation_ids(self) -> set[str]:
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:36:    store: SqliteEventStore, conversation_id: str, base: str
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:50:    for e in await store.get_events(conversation_id):
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:75:    runtime: ConversationRuntime, conversation_id: str, base: str, authored: Any
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:88:        data = await _read_artifact_bytes(runtime, conversation_id, f"{base}_img_{i}.png")
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:103:    @router.get("/conversations/{conversation_id}/deck/editor")
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:105:        conversation_id: str,
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:115:        if authored_rel not in await _declared_artifacts(store, conversation_id):
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:120:        if not await _sidecar_is_current(store, conversation_id, base):
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:123:        data = await _read_artifact_bytes(runtime, conversation_id, authored_rel)
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:134:    @router.get("/conversations/{conversation_id}/deck/editor/render")
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:136:        conversation_id: str,
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:156:        if authored_rel not in await _declared_artifacts(store, conversation_id):
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:158:        if not await _sidecar_is_current(store, conversation_id, base):
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:163:        raw = await _read_artifact_bytes(runtime, conversation_id, authored_rel)
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:171:        image_assets = await _reload_deck_image_assets(runtime, conversation_id, base, authored)
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:186:    @router.get("/conversations/{conversation_id}/deck/export")
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:188:        conversation_id: str,
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:196:        OR host snapshot via ``_read_artifact_bytes``) — re-themed via the lower_deck
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:209:        if authored_rel not in await _declared_artifacts(store, conversation_id):
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:215:        if not await _sidecar_is_current(store, conversation_id, base):
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:224:        raw = await _read_artifact_bytes(runtime, conversation_id, authored_rel)
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:232:        image_assets = await _reload_deck_image_assets(runtime, conversation_id, base, authored)
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:256:    @router.put("/conversations/{conversation_id}/deck/editor")
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:258:        conversation_id: str,
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:282:        declared = await _declared_artifacts(store, conversation_id)
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:287:        if not await _sidecar_is_current(store, conversation_id, base):
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:290:        # Read the current authored JSON (live session → host snapshot fallback).
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:291:        raw = await _read_artifact_bytes(runtime, conversation_id, authored_rel)
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:322:            runtime, conversation_id, base, authored_deck
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:336:        session = runtime.live_session(conversation_id)
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:350:            rel: await _read_artifact_bytes(runtime, conversation_id, rel)
packages/agent-server/src/disco/agent_server/routes/deck_editor.py:381:        pdf_stale = (await _read_artifact_bytes(runtime, conversation_id, pdf_rel)) is not None
packages/agent-server/src/disco/agent_server/routes/report.py:114:    conversation_id: str,
packages/agent-server/src/disco/agent_server/routes/report.py:156:        events = await store.get_events(conversation_id)
packages/agent-server/src/disco/agent_server/routes/report.py:183:            conversation_id, fmt, owner_id=DEFAULT_OWNER_ID
packages/agent-server/src/disco/agent_server/routes/report.py:200:    @router.post("/api/conversations/{conversation_id}/report/export")
packages/agent-server/src/disco/agent_server/routes/report.py:202:        conversation_id: str,
packages/agent-server/src/disco/agent_server/routes/report.py:231:            conversation_id,
packages/agent-server/src/disco/agent_server/routes/report.py:239:        safe_cid = conversation_id.replace("/", "-").replace("..", "-")
packages/agent-server/src/disco/agent_server/routes/report.py:255:    @router.post("/conversations/{conversation_id}/report/audio")
packages/agent-server/src/disco/agent_server/routes/report.py:257:        conversation_id: str,
packages/agent-server/src/disco/agent_server/routes/report.py:287:            all_events = await store.get_events(conversation_id)
packages/agent-server/src/disco/agent_server/routes/report.py:306:        out_dir = report_audio_cache_dir() / conversation_id
packages/agent-server/src/disco/agent_server/routes/report.py:330:            "mp3_url": f"/conversations/{conversation_id}/report/audio/{mp3_path.name}",
packages/agent-server/src/disco/agent_server/routes/report.py:332:                f"/conversations/{conversation_id}/report/audio/{transcript_path.name}"
packages/agent-server/src/disco/agent_server/routes/report.py:336:    @router.get("/conversations/{conversation_id}/report/audio/{name}")
packages/agent-server/src/disco/agent_server/routes/report.py:337:    async def report_audio_file(conversation_id: str, name: str) -> Response:
packages/agent-server/src/disco/agent_server/routes/report.py:346:        cid_cache = (report_audio_cache_dir() / conversation_id).resolve()
packages/agent-server/src/disco/agent_server/deep_research_service.py:16:`_sandbox_service_now`, `_sandbox_spec`, `_maybe_snapshot`, `_secret_store`,
packages/agent-server/src/disco/agent_server/deep_research_service.py:85:        self, conversation_id: str, router: DefaultLLMRouter, agent: RouterAgent
packages/agent-server/src/disco/agent_server/deep_research_service.py:101:            conversation_id,
packages/agent-server/src/disco/agent_server/deep_research_service.py:119:    def _depth_for(self, conversation_id: str) -> DepthTier:
packages/agent-server/src/disco/agent_server/deep_research_service.py:123:        raw = self._rt._depth.get(conversation_id) if hasattr(self._rt, "_depth") else None
packages/agent-server/src/disco/agent_server/deep_research_service.py:131:    def set_depth(self, conversation_id: str, tier: str | None) -> None:
packages/agent-server/src/disco/agent_server/deep_research_service.py:139:            self._rt._depth[conversation_id] = tier
packages/agent-server/src/disco/agent_server/deep_research_service.py:143:    def _iterative_for(self, conversation_id: str) -> bool:
packages/agent-server/src/disco/agent_server/deep_research_service.py:149:        return bool(self._rt._iterative.get(conversation_id, False))
packages/agent-server/src/disco/agent_server/deep_research_service.py:151:    def set_iterative(self, conversation_id: str, enabled: bool) -> None:
packages/agent-server/src/disco/agent_server/deep_research_service.py:157:        self._rt._iterative[conversation_id] = bool(enabled)
packages/agent-server/src/disco/agent_server/deep_research_service.py:161:    def set_recency(self, conversation_id: str, window: str | None) -> None:
packages/agent-server/src/disco/agent_server/deep_research_service.py:172:            self._rt._recency[conversation_id] = window
packages/agent-server/src/disco/agent_server/deep_research_service.py:174:            self._rt._recency.pop(conversation_id, None)
packages/agent-server/src/disco/agent_server/deep_research_service.py:176:    def _recency_for(self, conversation_id: str) -> Literal["month", "week"] | None:
packages/agent-server/src/disco/agent_server/deep_research_service.py:180:        val = self._rt._recency.get(conversation_id)
packages/agent-server/src/disco/agent_server/deep_research_service.py:231:        conversation_id: str | None = None,
packages/agent-server/src/disco/agent_server/deep_research_service.py:240:        G1/DR-4 F3: when ``conversation_id`` is provided and the conversation has
packages/agent-server/src/disco/agent_server/deep_research_service.py:256:            self._rt.get_upload_passages(conversation_id)
packages/agent-server/src/disco/agent_server/deep_research_service.py:257:            if conversation_id
packages/agent-server/src/disco/agent_server/deep_research_service.py:281:    async def _maybe_run_deep_research(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/deep_research_service.py:294:        events = await self._rt._store.get_events(conversation_id)
packages/agent-server/src/disco/agent_server/deep_research_service.py:295:        state = await self._rt._store.get_state(conversation_id)
packages/agent-server/src/disco/agent_server/deep_research_service.py:301:            await self._rt._propose_deep_research_plan(conversation_id, events)
packages/agent-server/src/disco/agent_server/deep_research_service.py:312:                conversation_id,
packages/agent-server/src/disco/agent_server/deep_research_service.py:316:                conversation_id, plans[-1], resume_from=partial
packages/agent-server/src/disco/agent_server/deep_research_service.py:332:                await self._rt._execute_deep_research(conversation_id, plans[-1])
packages/agent-server/src/disco/agent_server/deep_research_service.py:338:            await self._follow_up_deep_research(conversation_id, events, reports[-1])
packages/agent-server/src/disco/agent_server/deep_research_service.py:341:        self, conversation_id: str, events: list
packages/agent-server/src/disco/agent_server/deep_research_service.py:360:            conversation_id,
packages/agent-server/src/disco/agent_server/deep_research_service.py:363:        tier = self._rt._depth_for(conversation_id)
packages/agent-server/src/disco/agent_server/deep_research_service.py:374:        router = self._rt._router_now(pick=self._rt._model_override.get(conversation_id))
packages/agent-server/src/disco/agent_server/deep_research_service.py:375:        recency_window = self._recency_for(conversation_id)
packages/agent-server/src/disco/agent_server/deep_research_service.py:383:                conversation_id,
packages/agent-server/src/disco/agent_server/deep_research_service.py:417:        await self._rt._store.append(conversation_id, plan)
packages/agent-server/src/disco/agent_server/deep_research_service.py:418:        if self._rt._effective_autonomous(conversation_id):
packages/agent-server/src/disco/agent_server/deep_research_service.py:425:                conversation_id,
packages/agent-server/src/disco/agent_server/deep_research_service.py:430:            await self._rt._execute_deep_research(conversation_id, plan)
packages/agent-server/src/disco/agent_server/deep_research_service.py:433:            conversation_id,
packages/agent-server/src/disco/agent_server/deep_research_service.py:441:        conversation_id: str,
packages/agent-server/src/disco/agent_server/deep_research_service.py:457:        events = await self._rt._store.get_events(conversation_id)
packages/agent-server/src/disco/agent_server/deep_research_service.py:467:        tier = self._rt._depth_for(conversation_id)
packages/agent-server/src/disco/agent_server/deep_research_service.py:468:        recency_window = self._recency_for(conversation_id)
packages/agent-server/src/disco/agent_server/deep_research_service.py:486:            pick=self._rt._model_override.get(conversation_id)
packages/agent-server/src/disco/agent_server/deep_research_service.py:513:        upload_passages = self._rt.get_upload_passages(conversation_id)
packages/agent-server/src/disco/agent_server/deep_research_service.py:523:            conversation_id=conversation_id,
packages/agent-server/src/disco/agent_server/deep_research_service.py:529:            iterative=self._iterative_for(conversation_id),
packages/agent-server/src/disco/agent_server/deep_research_service.py:567:                                await self._rt._store.get_events(conversation_id)
packages/agent-server/src/disco/agent_server/deep_research_service.py:575:                    conversation_id,
packages/agent-server/src/disco/agent_server/deep_research_service.py:595:            await self._rt._store.append(conversation_id, event)
packages/agent-server/src/disco/agent_server/deep_research_service.py:620:        self._rt._cancel_flags[conversation_id] = flag
packages/agent-server/src/disco/agent_server/deep_research_service.py:626:        self._rt._dr_steer[conversation_id] = []
packages/agent-server/src/disco/agent_server/deep_research_service.py:627:        self._rt._dr_injected_sources[conversation_id] = []
packages/agent-server/src/disco/agent_server/deep_research_service.py:631:            queue = self._rt._dr_steer.get(conversation_id, [])
packages/agent-server/src/disco/agent_server/deep_research_service.py:639:            queue = self._rt._dr_injected_sources.get(conversation_id, [])
packages/agent-server/src/disco/agent_server/deep_research_service.py:660:                conversation_id,
packages/agent-server/src/disco/agent_server/deep_research_service.py:668:            self._rt._cancel_flags.pop(conversation_id, None)
packages/agent-server/src/disco/agent_server/deep_research_service.py:671:            self._rt._dr_steer.pop(conversation_id, None)
packages/agent-server/src/disco/agent_server/deep_research_service.py:672:            self._rt._dr_injected_sources.pop(conversation_id, None)
packages/agent-server/src/disco/agent_server/deep_research_service.py:682:        await self._rt._store.append(conversation_id, result.to_event())
packages/agent-server/src/disco/agent_server/deep_research_service.py:685:            conversation_id,
packages/agent-server/src/disco/agent_server/deep_research_service.py:712:        conversation_id: str,
packages/agent-server/src/disco/agent_server/deep_research_service.py:742:            conversation_id,
packages/agent-server/src/disco/agent_server/deep_research_service.py:753:                conversation_id,
packages/agent-server/src/disco/agent_server/deep_research_service.py:772:                    conversation_id,
packages/agent-server/src/disco/agent_server/deep_research_service.py:780:                    conversation_id,
packages/agent-server/src/disco/agent_server/deep_research_service.py:794:                    conversation_id,
packages/agent-server/src/disco/agent_server/deep_research_service.py:802:                conversation_id,
packages/agent-server/src/disco/agent_server/deep_research_service.py:840:                conversation_id,
packages/agent-server/src/disco/agent_server/deep_research_service.py:859:                    conversation_id,
packages/agent-server/src/disco/agent_server/deep_research_service.py:867:                    conversation_id,
packages/agent-server/src/disco/agent_server/deep_research_service.py:881:                    conversation_id,
packages/agent-server/src/disco/agent_server/deep_research_service.py:887:            conversation_id,
packages/agent-server/src/disco/agent_server/deep_research_service.py:893:        conversation_id: str,
packages/agent-server/src/disco/agent_server/deep_research_service.py:909:        events = await self._rt._store.get_events(conversation_id)
packages/agent-server/src/disco/agent_server/deep_research_service.py:937:            self._rt._sandbox_spec, owner_id=owner_id, conversation_id=cid
packages/agent-server/src/disco/agent_server/routes/ws.py:26:    conversation_id: str,
packages/agent-server/src/disco/agent_server/routes/ws.py:36:        store.conversation_origin(conversation_id) == "imported"
packages/agent-server/src/disco/agent_server/routes/ws.py:54:            await store.append(conversation_id, _context_message(frame.context))
packages/agent-server/src/disco/agent_server/routes/ws.py:55:        await store.append(conversation_id, _user_message(frame.content))
packages/agent-server/src/disco/agent_server/routes/ws.py:57:            runtime.kick(conversation_id)
packages/agent-server/src/disco/agent_server/routes/ws.py:63:        if runtime is not None and conversation_id in runtime._dr_steer:
packages/agent-server/src/disco/agent_server/routes/ws.py:64:            runtime._dr_steer[conversation_id].append(frame.steer_text)
packages/agent-server/src/disco/agent_server/routes/ws.py:66:            await store.append(conversation_id, _user_message(frame.steer_text, steer=True))
packages/agent-server/src/disco/agent_server/routes/ws.py:68:                runtime.kick(conversation_id)
packages/agent-server/src/disco/agent_server/routes/ws.py:75:        if runtime is not None and conversation_id in runtime._dr_injected_sources:
packages/agent-server/src/disco/agent_server/routes/ws.py:88:            runtime._dr_injected_sources[conversation_id].append(passage)
packages/agent-server/src/disco/agent_server/routes/ws.py:91:        await runtime.confirm(conversation_id)
packages/agent-server/src/disco/agent_server/routes/ws.py:94:        await runtime.reject(conversation_id)
packages/agent-server/src/disco/agent_server/routes/ws.py:97:        await runtime.approve_plan(conversation_id)
packages/agent-server/src/disco/agent_server/routes/ws.py:100:        await runtime.request_plan(conversation_id, frame.content or "")
packages/agent-server/src/disco/agent_server/routes/ws.py:103:        await runtime.pick_alternative(conversation_id, frame.option_id)
packages/agent-server/src/disco/agent_server/routes/ws.py:108:        await runtime.pause(conversation_id)
packages/agent-server/src/disco/agent_server/routes/ws.py:111:        await runtime.cancel(conversation_id)
packages/agent-server/src/disco/agent_server/routes/ws.py:115:        await runtime.resume_conversation(conversation_id)
packages/agent-server/src/disco/agent_server/routes/ws.py:123:    @router.websocket("/ws/conversations/{conversation_id}")
packages/agent-server/src/disco/agent_server/routes/ws.py:126:        conversation_id: str,
packages/agent-server/src/disco/agent_server/routes/ws.py:131:            runtime.on_connect(conversation_id)
packages/agent-server/src/disco/agent_server/routes/ws.py:133:        # (1) On connect: one state snapshot, then replay events after last_seq,
packages/agent-server/src/disco/agent_server/routes/ws.py:135:        state = await store.get_state(conversation_id)
packages/agent-server/src/disco/agent_server/routes/ws.py:138:            sstate = runtime.sandbox_state(conversation_id)
packages/agent-server/src/disco/agent_server/routes/ws.py:141:            if runtime.is_autonomous(conversation_id):
packages/agent-server/src/disco/agent_server/routes/ws.py:145:            state.extras["assist"] = runtime.is_assist(conversation_id)
packages/agent-server/src/disco/agent_server/routes/ws.py:154:        stream = await store.subscribe(conversation_id, after_seq=last_seq)
packages/agent-server/src/disco/agent_server/routes/ws.py:165:        eph_stream = await store.subscribe_ephemeral(conversation_id)
packages/agent-server/src/disco/agent_server/routes/ws.py:206:                await _handle_frame(store, websocket, conversation_id, frame, runtime)
packages/agent-server/src/disco/agent_server/routes/ws.py:217:                runtime.on_disconnect(conversation_id)
packages/agent-server/src/disco/agent_server/routes/ws.py:254:        # G1/DR-4: optional conversation_id lets the server load seed passages from
packages/agent-server/src/disco/agent_server/routes/ws.py:257:        conversation_id = body.get("conversation_id") or None
packages/agent-server/src/disco/agent_server/routes/ws.py:258:        if conversation_id is not None:
packages/agent-server/src/disco/agent_server/routes/ws.py:259:            conversation_id = str(conversation_id).strip() or None
packages/agent-server/src/disco/agent_server/routes/ws.py:267:                conversation_id=conversation_id,
packages/agent-server/src/disco/agent_server/report_export.py:503:    ref_path = Path(str(pkg)) / "reference.docx"
packages/agent-server/src/disco/agent_server/title_service.py:70:def sanitize_title(raw: str) -> str:
packages/agent-server/src/disco/agent_server/title_service.py:112:    return sanitize_title(" ".join(words))
packages/agent-server/src/disco/agent_server/title_service.py:125:    def schedule(self, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/title_service.py:128:        if conversation_id in self._inflight:
packages/agent-server/src/disco/agent_server/title_service.py:130:        self._inflight.add(conversation_id)
packages/agent-server/src/disco/agent_server/title_service.py:131:        asyncio.create_task(self._run_guarded(conversation_id))
packages/agent-server/src/disco/agent_server/title_service.py:172:            return sanitize_title(getattr(resp, "text", "") or "")
packages/agent-server/src/disco/agent_server/title_service.py:177:    async def backfill(self, conversation_ids: Sequence[str]) -> dict[str, str]:
packages/agent-server/src/disco/agent_server/title_service.py:182:        for cid in conversation_ids:
packages/agent-server/src/disco/agent_server/routes/conversations.py:50:        conversation_id = f"conv_{uuid.uuid4().hex}"
packages/agent-server/src/disco/agent_server/routes/conversations.py:52:            conversation_id,
packages/agent-server/src/disco/agent_server/routes/conversations.py:61:            runtime.set_surface(conversation_id, body.surface)
packages/agent-server/src/disco/agent_server/routes/conversations.py:63:                conversation_id, _resolve_model(body.model_override, runtime)
packages/agent-server/src/disco/agent_server/routes/conversations.py:66:                runtime.set_autonomous(conversation_id, True)
packages/agent-server/src/disco/agent_server/routes/conversations.py:70:                runtime.set_assist(conversation_id, body.assist)
packages/agent-server/src/disco/agent_server/routes/conversations.py:74:                runtime.set_depth(conversation_id, body.depth_tier)
packages/agent-server/src/disco/agent_server/routes/conversations.py:78:                runtime.set_iterative(conversation_id, True)
packages/agent-server/src/disco/agent_server/routes/conversations.py:81:                runtime.set_recency(conversation_id, body.recency_window)
packages/agent-server/src/disco/agent_server/routes/conversations.py:84:                runtime.set_artifact_mode(conversation_id, True)
packages/agent-server/src/disco/agent_server/routes/conversations.py:86:            "conversation_id": conversation_id,
packages/agent-server/src/disco/agent_server/routes/conversations.py:87:            "conversation_url": f"/ws/conversations/{conversation_id}",
packages/agent-server/src/disco/agent_server/routes/conversations.py:92:    @router.patch("/conversations/{conversation_id}/settings")
packages/agent-server/src/disco/agent_server/routes/conversations.py:93:    async def update_settings(conversation_id: str, body: UpdateSettingsBody) -> dict:
packages/agent-server/src/disco/agent_server/routes/conversations.py:109:        _reject_if_imported(store, conversation_id)
packages/agent-server/src/disco/agent_server/routes/conversations.py:119:                conversation_id,
packages/agent-server/src/disco/agent_server/routes/conversations.py:133:            runtime.set_autonomous(conversation_id, body.autonomous)
packages/agent-server/src/disco/agent_server/routes/conversations.py:134:        return {"ok": True, "model_override": runtime._model_override.get(conversation_id)}
packages/agent-server/src/disco/agent_server/routes/conversations.py:136:    @router.post("/conversations/{conversation_id}/messages")
packages/agent-server/src/disco/agent_server/routes/conversations.py:137:    async def post_message(conversation_id: str, body: SendMessageBody) -> dict:
packages/agent-server/src/disco/agent_server/routes/conversations.py:140:        _reject_if_imported(store, conversation_id)
packages/agent-server/src/disco/agent_server/routes/conversations.py:141:        stored = await store.append(conversation_id, _user_message(body.content))
packages/agent-server/src/disco/agent_server/routes/conversations.py:143:            runtime.kick(conversation_id)
packages/agent-server/src/disco/agent_server/routes/conversations.py:146:    @router.post("/conversations/{conversation_id}/followup")
packages/agent-server/src/disco/agent_server/routes/conversations.py:147:    async def post_followup(conversation_id: str, body: SendMessageBody) -> dict:
packages/agent-server/src/disco/agent_server/routes/conversations.py:155:        _reject_if_imported(store, conversation_id)
packages/agent-server/src/disco/agent_server/routes/conversations.py:156:        state = await store.get_state(conversation_id)
packages/agent-server/src/disco/agent_server/routes/conversations.py:169:        stored = await store.append(conversation_id, _user_message(body.content))
packages/agent-server/src/disco/agent_server/routes/conversations.py:170:        runtime.kick(conversation_id)
packages/agent-server/src/disco/agent_server/routes/conversations.py:173:    @router.get("/conversations/{conversation_id}/events")
packages/agent-server/src/disco/agent_server/routes/conversations.py:175:        conversation_id: str,
packages/agent-server/src/disco/agent_server/routes/conversations.py:179:        page = await store.paginate(conversation_id, after_seq=after_seq, limit=limit)
packages/agent-server/src/disco/agent_server/routes/conversations.py:182:    @router.get("/conversations/{conversation_id}/state")
packages/agent-server/src/disco/agent_server/routes/conversations.py:183:    async def get_state(conversation_id: str) -> dict:
packages/agent-server/src/disco/agent_server/routes/conversations.py:184:        state = await store.get_state(conversation_id)
packages/agent-server/src/disco/agent_server/routes/conversations.py:189:            sstate = runtime.sandbox_state(conversation_id)
packages/agent-server/src/disco/agent_server/routes/conversations.py:193:            if runtime.is_autonomous(conversation_id):
packages/agent-server/src/disco/agent_server/routes/conversations.py:198:            state.extras["assist"] = runtime.is_assist(conversation_id)
packages/agent-server/src/disco/agent_server/routes/conversations.py:207:    @router.get("/conversations/{conversation_id}/workspace/{path:path}")
packages/agent-server/src/disco/agent_server/routes/conversations.py:208:    async def workspace_file(conversation_id: str, path: str) -> Response:
packages/agent-server/src/disco/agent_server/routes/conversations.py:219:        session = runtime.live_session(conversation_id)
packages/agent-server/src/disco/agent_server/routes/conversations.py:232:                    store_path = ps.path_for(conversation_id)
packages/agent-server/src/disco/agent_server/routes/conversations.py:234:                        disk_path = store_path / "snapshot" / norm
packages/agent-server/src/disco/agent_server/routes/conversations.py:235:                        # Prevent traversal out of snapshot
packages/agent-server/src/disco/agent_server/routes/conversations.py:238:                            snap_base = (store_path / "snapshot").resolve()
packages/agent-server/src/disco/agent_server/routes/conversations.py:252:    @router.post("/conversations/{conversation_id}/kill")
packages/agent-server/src/disco/agent_server/routes/conversations.py:253:    async def kill_conversation(conversation_id: str) -> dict:
packages/agent-server/src/disco/agent_server/routes/conversations.py:257:            await runtime.kill(conversation_id)
packages/agent-server/src/disco/agent_server/routes/conversations.py:258:        state = await store.get_state(conversation_id)
packages/agent-server/src/disco/agent_server/routes/conversations.py:261:    @router.post("/conversations/{conversation_id}/resume")
packages/agent-server/src/disco/agent_server/routes/conversations.py:262:    async def post_resume_conversation(conversation_id: str) -> dict:
packages/agent-server/src/disco/agent_server/routes/conversations.py:274:        _reject_if_imported(store, conversation_id)
packages/agent-server/src/disco/agent_server/routes/conversations.py:275:        result = await runtime.resume_conversation(conversation_id)
packages/agent-server/src/disco/agent_server/routes/conversations.py:287:        return {"conversation_ids": ids}
packages/agent-server/src/disco/agent_server/verify/runner.py:67:        """POST /conversations → return the new conversation_id."""
packages/agent-server/src/disco/agent_server/verify/runner.py:103:        """GET /conversations/{cid}/state (one-shot snapshot after run)."""
packages/agent-server/src/disco/agent_server/verify/runner.py:112:    async def get_manifest(self, cid: str) -> dict[str, Any] | None:
packages/agent-server/src/disco/agent_server/verify/runner.py:113:        """GET /api/projects/{cid}/manifest — None when project storage is unavailable."""
packages/agent-server/src/disco/agent_server/verify/runner.py:137:            return str(resp.json()["conversation_id"])
packages/agent-server/src/disco/agent_server/verify/runner.py:240:    async def get_manifest(self, cid: str) -> dict[str, Any] | None:
packages/agent-server/src/disco/agent_server/verify/runner.py:242:            resp = await hc.get(f"/api/projects/{cid}/manifest")
packages/agent-server/src/disco/agent_server/verify/runner.py:358:        if not path or not Path(path).exists():
packages/agent-server/src/disco/agent_server/verify/runner.py:377:    manifest: dict[str, Any] | None,
packages/agent-server/src/disco/agent_server/verify/runner.py:385:    - ``state.json``    — final state snapshot, redacted
packages/agent-server/src/disco/agent_server/verify/runner.py:387:    - ``manifest.json`` — project manifest (if available), redacted
packages/agent-server/src/disco/agent_server/verify/runner.py:396:                    "conversation_id": cid,
packages/agent-server/src/disco/agent_server/verify/runner.py:421:    if manifest is not None:
packages/agent-server/src/disco/agent_server/verify/runner.py:422:        (dossier / "manifest.json").write_text(
packages/agent-server/src/disco/agent_server/verify/runner.py:423:            json.dumps(redact(manifest), indent=2),
packages/agent-server/src/disco/agent_server/verify/runner.py:449:    (d) Fetch evidence: events, state, /api/debug/trace/{cid}, /api/projects/{cid}/manifest.
packages/agent-server/src/disco/agent_server/verify/runner.py:468:        dossier_base = Path("test-record") / "disco-verify"
packages/agent-server/src/disco/agent_server/verify/runner.py:495:    # (d) fetch evidence — fresh snapshots after the run terminates
packages/agent-server/src/disco/agent_server/verify/runner.py:497:    # Re-fetch state for the dossier (clean post-run snapshot distinct from the
packages/agent-server/src/disco/agent_server/verify/runner.py:501:    manifest = await client.get_manifest(cid)
packages/agent-server/src/disco/agent_server/verify/runner.py:528:    # (g) write dossier — use evidence_state (fresh post-run snapshot) for state.json
packages/agent-server/src/disco/agent_server/verify/runner.py:529:    _write_dossier(dossier_path, cid, scenario, evidence_state, events, trace, manifest, result)
packages/agent-server/src/disco/agent_server/resume_service.py:11:`_cancel_flags`, `kick`, `sessions_snapshot`, `_surface_of`,
packages/agent-server/src/disco/agent_server/resume_service.py:160:        self, conversation_id: str, events: list
packages/agent-server/src/disco/agent_server/resume_service.py:214:        if conversation_id in self._rt._executors:
packages/agent-server/src/disco/agent_server/resume_service.py:226:        # Workspace file listing from the project-store snapshot (up to 30 paths).
packages/agent-server/src/disco/agent_server/resume_service.py:231:                workspace = store.path_for(conversation_id)
packages/agent-server/src/disco/agent_server/resume_service.py:232:                raw_paths = list(store.iter_workspace(conversation_id))[:30]
packages/agent-server/src/disco/agent_server/resume_service.py:244:        upload_names = sorted(list(self._rt.get_upload_names(conversation_id)))
packages/agent-server/src/disco/agent_server/resume_service.py:251:                if conversation_id in self._rt._executors
packages/agent-server/src/disco/agent_server/resume_service.py:256:        # Session list (degrades gracefully — sessions_snapshot never raises).
packages/agent-server/src/disco/agent_server/resume_service.py:257:        sessions, _ = await self._rt.sessions_snapshot(conversation_id)
packages/agent-server/src/disco/agent_server/resume_service.py:292:    async def resume_conversation(self, conversation_id: str) -> dict:
packages/agent-server/src/disco/agent_server/resume_service.py:308:        state = await self._rt._store.get_state(conversation_id)
packages/agent-server/src/disco/agent_server/resume_service.py:318:        events = await self._rt._store.get_events(conversation_id)
packages/agent-server/src/disco/agent_server/resume_service.py:323:            await self._rt._store.append(conversation_id, tombstone)
packages/agent-server/src/disco/agent_server/resume_service.py:324:            events = await self._rt._store.get_events(conversation_id)
packages/agent-server/src/disco/agent_server/resume_service.py:337:        await self._rt._drain_finishing_task(conversation_id)
packages/agent-server/src/disco/agent_server/resume_service.py:342:        new_events = await self._reconstruct_resume_context(conversation_id, events)
packages/agent-server/src/disco/agent_server/resume_service.py:344:            await self._rt._store.append(conversation_id, event)
packages/agent-server/src/disco/agent_server/resume_service.py:347:        self._rt._cancel_flags.pop(conversation_id, None)
packages/agent-server/src/disco/agent_server/resume_service.py:352:        _loop = self._rt._loops.get(conversation_id)
packages/agent-server/src/disco/agent_server/resume_service.py:356:        surface = self._rt._surface_of(conversation_id)
packages/agent-server/src/disco/agent_server/resume_service.py:365:                conversation_id,
packages/agent-server/src/disco/agent_server/resume_service.py:369:        self._rt.kick(conversation_id)
packages/agent-server/src/disco/agent_server/routes/_common.py:65:def _sanitize_name(raw: str) -> str | None:
packages/agent-server/src/disco/agent_server/routes/_common.py:67:    name = Path(raw).name            # kills traversal (../../etc/passwd → passwd)
packages/agent-server/src/disco/agent_server/routes/_common.py:148:def _reject_if_imported(store: SqliteEventStore, conversation_id: str) -> None:
packages/agent-server/src/disco/agent_server/routes/_common.py:154:    if store.conversation_origin(conversation_id) == "imported":
packages/agent-server/src/disco/agent_server/routes/_common.py:158:async def _declared_artifacts(store: SqliteEventStore, conversation_id: str) -> set[str]:
packages/agent-server/src/disco/agent_server/routes/_common.py:172:        for e in await store.get_events(conversation_id):
packages/agent-server/src/disco/agent_server/routes/preview.py:19:def _serve_static_from_snapshot(
packages/agent-server/src/disco/agent_server/routes/preview.py:20:    runtime: ConversationRuntime, conversation_id: str, rel_path: str
packages/agent-server/src/disco/agent_server/routes/preview.py:23:    ProjectStore snapshot when the sandbox can't be woken (build finished + reaped,
packages/agent-server/src/disco/agent_server/routes/preview.py:26:    user hit ("preview not available" on a completed app). Jailed to the snapshot
packages/agent-server/src/disco/agent_server/routes/preview.py:32:        ws = ps.path_for(conversation_id).resolve()
packages/agent-server/src/disco/agent_server/routes/preview.py:33:    except Exception:  # noqa: BLE001 — no snapshot → caller falls back to 503
packages/agent-server/src/disco/agent_server/routes/preview.py:50:    @router.get("/conversations/{conversation_id}/browser/live-url")
packages/agent-server/src/disco/agent_server/routes/preview.py:51:    async def browser_live_url(conversation_id: str) -> Response:
packages/agent-server/src/disco/agent_server/routes/preview.py:81:        cid8 = conversation_id.removeprefix("conv_")[:8]
packages/agent-server/src/disco/agent_server/routes/preview.py:84:        session = runtime.live_session(conversation_id)
packages/agent-server/src/disco/agent_server/routes/preview.py:172:    @router.post("/conversations/{conversation_id}/browser/live-touch")
packages/agent-server/src/disco/agent_server/routes/preview.py:173:    async def browser_live_touch(conversation_id: str) -> Response:
packages/agent-server/src/disco/agent_server/routes/preview.py:179:        session = runtime.live_session(conversation_id)
packages/agent-server/src/disco/agent_server/routes/preview.py:195:    @router.post("/conversations/{conversation_id}/browser/live-stop")
packages/agent-server/src/disco/agent_server/routes/preview.py:196:    async def browser_live_stop(conversation_id: str) -> Response:
packages/agent-server/src/disco/agent_server/routes/preview.py:204:        session = runtime.live_session(conversation_id)
packages/agent-server/src/disco/agent_server/routes/preview.py:220:    @router.get("/conversations/{conversation_id}/preview")
packages/agent-server/src/disco/agent_server/routes/preview.py:221:    async def get_preview(conversation_id: str) -> dict:
packages/agent-server/src/disco/agent_server/routes/preview.py:225:        return await runtime.preview(conversation_id)
packages/agent-server/src/disco/agent_server/routes/preview.py:227:    @router.post("/conversations/{conversation_id}/preview/restart")
packages/agent-server/src/disco/agent_server/routes/preview.py:228:    async def ensure_preview(conversation_id: str) -> dict:
packages/agent-server/src/disco/agent_server/routes/preview.py:233:        return {"ok": await runtime.ensure_preview(conversation_id)}
packages/agent-server/src/disco/agent_server/routes/preview.py:235:    @router.get("/conversations/{conversation_id}/preview-app/{path:path}")
packages/agent-server/src/disco/agent_server/routes/preview.py:236:    @router.get("/conversations/{conversation_id}/preview-app/")
packages/agent-server/src/disco/agent_server/routes/preview.py:240:    async def preview_app(conversation_id: str, path: str = "") -> Response:
packages/agent-server/src/disco/agent_server/routes/preview.py:250:        cid8 = conversation_id.removeprefix("conv_")[:8]
packages/agent-server/src/disco/agent_server/routes/preview.py:255:            # snapshot — serve it directly instead of a 503 for a file we have.
packages/agent-server/src/disco/agent_server/routes/preview.py:256:            served = _serve_static_from_snapshot(runtime, conversation_id, path)
packages/agent-server/src/disco/agent_server/routes/preview.py:271:    @router.get("/conversations/{conversation_id}/port/{port}/{path:path}")
packages/agent-server/src/disco/agent_server/routes/preview.py:272:    @router.get("/conversations/{conversation_id}/port/{port}/")
packages/agent-server/src/disco/agent_server/routes/preview.py:275:    async def port_app(conversation_id: str, port: int, path: str = "") -> Response:
packages/agent-server/src/disco/agent_server/routes/preview.py:285:        cid8 = conversation_id.removeprefix("conv_")[:8]
packages/agent-server/src/disco/agent_server/share_viewer.py:103:      titleEl.textContent = bundle.title || ('Shared run ' + (bundle.conversation_id || ''));
packages/agent-server/src/disco/agent_server/runtime_settings.py:79:        self, conversation_id: str, model_id: str | None
packages/agent-server/src/disco/agent_server/runtime_settings.py:85:            self._rt._model_override[conversation_id] = model_id
packages/agent-server/src/disco/agent_server/runtime_settings.py:91:    def set_model_override(self, conversation_id: str, model_id: str | None) -> None:
packages/agent-server/src/disco/agent_server/runtime_settings.py:98:        self._set_model_override_unlocked(conversation_id, model_id)
packages/agent-server/src/disco/agent_server/runtime_settings.py:186:    def set_autonomous(self, conversation_id: str, value: bool = True) -> None:
packages/agent-server/src/disco/agent_server/runtime_settings.py:188:        self._rt._autonomous[conversation_id] = bool(value)
packages/agent-server/src/disco/agent_server/runtime_settings.py:191:    def _effective_autonomous(self, conversation_id: str) -> bool:
packages/agent-server/src/disco/agent_server/runtime_settings.py:200:            self._rt._autonomous.get(conversation_id, False)
packages/agent-server/src/disco/agent_server/runtime_settings.py:201:            and self._rt._surface_of(conversation_id) in self._rt._AUTONOMOUS_SURFACES
packages/agent-server/src/disco/agent_server/runtime_settings.py:204:    def is_autonomous(self, conversation_id: str) -> bool:
packages/agent-server/src/disco/agent_server/runtime_settings.py:207:        return self._effective_autonomous(conversation_id)
packages/agent-server/src/disco/agent_server/runtime_settings.py:242:    def _set_assist_unlocked(self, conversation_id: str, value: bool) -> None:
packages/agent-server/src/disco/agent_server/runtime_settings.py:246:        self._rt._assist[conversation_id] = bool(value)
packages/agent-server/src/disco/agent_server/runtime_settings.py:249:    def set_assist(self, conversation_id: str, value: bool = True) -> None:
packages/agent-server/src/disco/agent_server/runtime_settings.py:254:        self._set_assist_unlocked(conversation_id, value)
packages/agent-server/src/disco/agent_server/runtime_settings.py:256:    def _effective_policy(self, conversation_id: str) -> ModelExecutionPolicy:
packages/agent-server/src/disco/agent_server/runtime_settings.py:269:        override = self._rt._model_override.get(conversation_id)
packages/agent-server/src/disco/agent_server/runtime_settings.py:275:            assist_override=self._rt._assist.get(conversation_id),
packages/agent-server/src/disco/agent_server/runtime_settings.py:281:    def _effective_assist(self, conversation_id: str) -> bool:
packages/agent-server/src/disco/agent_server/runtime_settings.py:283:        return self._effective_policy(conversation_id).assist
packages/agent-server/src/disco/agent_server/runtime_settings.py:286:        self, conversation_id: str
packages/agent-server/src/disco/agent_server/runtime_settings.py:295:        override = self._rt._model_override.get(conversation_id)
packages/agent-server/src/disco/agent_server/runtime_settings.py:303:    def is_assist(self, conversation_id: str) -> bool:
packages/agent-server/src/disco/agent_server/runtime_settings.py:304:        return self._effective_assist(conversation_id)
packages/agent-server/src/disco/agent_server/runtime_settings.py:308:    async def _conversation_is_pristine(self, conversation_id: str) -> bool:
packages/agent-server/src/disco/agent_server/runtime_settings.py:322:        if conversation_id in self._rt._loops:
packages/agent-server/src/disco/agent_server/runtime_settings.py:324:        task = self._rt._tasks.get(conversation_id)
packages/agent-server/src/disco/agent_server/runtime_settings.py:328:        events = await self._rt._store.get_events(conversation_id)
packages/agent-server/src/disco/agent_server/runtime_settings.py:348:        conversation_id: str,
packages/agent-server/src/disco/agent_server/runtime_settings.py:363:        lock = self._settings_locks.setdefault(conversation_id, asyncio.Lock())
packages/agent-server/src/disco/agent_server/runtime_settings.py:365:            if not await self._conversation_is_pristine(conversation_id):
packages/agent-server/src/disco/agent_server/runtime_settings.py:374:            if conversation_id in self._rt._loops:
packages/agent-server/src/disco/agent_server/runtime_settings.py:376:            task = self._rt._tasks.get(conversation_id)
packages/agent-server/src/disco/agent_server/runtime_settings.py:380:                self._set_model_override_unlocked(conversation_id, model_override)
packages/agent-server/src/disco/agent_server/runtime_settings.py:382:                self._set_assist_unlocked(conversation_id, assist)
packages/agent-server/src/disco/agent_server/runtime_settings.py:387:    def set_artifact_mode(self, conversation_id: str, on: bool) -> None:
packages/agent-server/src/disco/agent_server/runtime_settings.py:391:        self._rt._artifact_mode[conversation_id] = bool(on)
packages/agent-server/src/disco/agent_server/runtime_settings.py:393:    def _effective_artifact_mode(self, conversation_id: str) -> bool:
packages/agent-server/src/disco/agent_server/runtime_settings.py:395:        return self._rt._artifact_mode.get(conversation_id, False)
packages/agent-server/src/disco/agent_server/routes/share.py:19:    @router.post("/api/conversations/{conversation_id}/share")
packages/agent-server/src/disco/agent_server/routes/share.py:20:    async def create_share(conversation_id: str) -> dict:
packages/agent-server/src/disco/agent_server/routes/share.py:33:            conversation_id, owner_id=DEFAULT_OWNER_ID
packages/agent-server/src/disco/agent_server/routes/share.py:44:            "conversation_id": conversation_id,
packages/agent-server/src/disco/agent_server/routes/share.py:69:    @router.get("/api/conversations/{conversation_id}/share/bundle")
packages/agent-server/src/disco/agent_server/routes/share.py:70:    async def export_share_bundle(conversation_id: str) -> dict:
packages/agent-server/src/disco/agent_server/routes/share.py:80:            conversation_id, owner_id=DEFAULT_OWNER_ID
packages/agent-server/src/disco/agent_server/routes/share.py:142:            row["conversation_id"], owner_id=row["owner_id"]
packages/agent-server/src/disco/agent_server/routes/files.py:32:    _sanitize_name,
packages/agent-server/src/disco/agent_server/routes/files.py:61:    runtime: Any, conversation_id: str, norm: str
packages/agent-server/src/disco/agent_server/routes/files.py:64:    ProjectStore snapshot (so a FINISHED run with a reaped sandbox still serves).
packages/agent-server/src/disco/agent_server/routes/files.py:67:    session = runtime.live_session(conversation_id)
packages/agent-server/src/disco/agent_server/routes/files.py:71:    # 2) host ProjectStore snapshot (finished run, sandbox reaped)
packages/agent-server/src/disco/agent_server/routes/files.py:75:            workspace = ps.path_for(conversation_id).resolve()
packages/agent-server/src/disco/agent_server/routes/files.py:87:    @router.post("/conversations/{conversation_id}/files")
packages/agent-server/src/disco/agent_server/routes/files.py:89:        conversation_id: str,
packages/agent-server/src/disco/agent_server/routes/files.py:97:        _reject_if_imported(store, conversation_id)
packages/agent-server/src/disco/agent_server/routes/files.py:98:        state = await store.get_state(conversation_id)
packages/agent-server/src/disco/agent_server/routes/files.py:110:        session = runtime.upload_session(conversation_id)
packages/agent-server/src/disco/agent_server/routes/files.py:114:        server_names = runtime.get_upload_names(conversation_id)
packages/agent-server/src/disco/agent_server/routes/files.py:124:        existing_bytes = runtime.get_upload_size(conversation_id)
packages/agent-server/src/disco/agent_server/routes/files.py:138:            clean = _sanitize_name(raw_name)
packages/agent-server/src/disco/agent_server/routes/files.py:159:            stem = Path(clean).stem
packages/agent-server/src/disco/agent_server/routes/files.py:160:            suffix = Path(clean).suffix
packages/agent-server/src/disco/agent_server/routes/files.py:170:            runtime.store_upload(conversation_id, final_name, data)
packages/agent-server/src/disco/agent_server/routes/files.py:180:            upload_doc = parse_upload_to_doc(final_name, data, conversation_id)
packages/agent-server/src/disco/agent_server/routes/files.py:182:                runtime.add_upload_passages(conversation_id, list(upload_doc.passages))
packages/agent-server/src/disco/agent_server/routes/files.py:190:                conversation_id,
packages/agent-server/src/disco/agent_server/routes/files.py:214:                conversation_id,
packages/agent-server/src/disco/agent_server/routes/files.py:222:    @router.get("/conversations/{conversation_id}/artifacts/{path:path}")
packages/agent-server/src/disco/agent_server/routes/files.py:224:        conversation_id: str,
packages/agent-server/src/disco/agent_server/routes/files.py:232:        ProjectStore snapshot so a FINISHED run (no live session) still serves.
packages/agent-server/src/disco/agent_server/routes/files.py:253:        if norm not in await _declared_artifacts(store, conversation_id):
packages/agent-server/src/disco/agent_server/routes/files.py:256:        data = await _read_artifact_bytes(runtime, conversation_id, norm)
packages/agent-server/src/disco/agent_server/routes/activity.py:31:        live = runtime.running_conversation_ids()
packages/agent-server/src/disco/agent_server/routes/activity.py:37:            by_id = {s.conversation_id: s for s in summaries}
packages/agent-server/src/disco/agent_server/routes/storage.py:1:"""Server-side directory picker route (settings projects_root selector)."""
packages/agent-server/src/disco/agent_server/routes/storage.py:26:            target = Path(path).expanduser().resolve() if path else Path.home()
packages/agent-server/src/disco/agent_server/routes/storage.py:58:        # Whether the CURRENT path is selectable as a projects_root.
packages/agent-server/src/disco/agent_server/routes/sessions.py:17:    @router.get("/conversations/{conversation_id}/sessions")
packages/agent-server/src/disco/agent_server/routes/sessions.py:18:    async def list_sessions(conversation_id: str) -> dict:
packages/agent-server/src/disco/agent_server/routes/sessions.py:23:        sessions, stale = await runtime.sessions_snapshot(conversation_id)
packages/agent-server/src/disco/agent_server/routes/sessions.py:39:    @router.get("/conversations/{conversation_id}/sessions/{name}/view")
packages/agent-server/src/disco/agent_server/routes/sessions.py:41:        conversation_id: str,
packages/agent-server/src/disco/agent_server/routes/sessions.py:51:        sessions = await runtime.sessions_list(conversation_id)
packages/agent-server/src/disco/agent_server/routes/sessions.py:54:        view = await runtime.session_view(conversation_id, name, tail_chars)
packages/agent-server/src/disco/agent_server/routes/preview_edit.py:17:     live sandbox first, then the host ProjectStore snapshot).
packages/agent-server/src/disco/agent_server/routes/preview_edit.py:59:    runtime: ConversationRuntime, conversation_id: str, norm: str
packages/agent-server/src/disco/agent_server/routes/preview_edit.py:61:    """Read a workspace file SYMLINK-SAFELY from the host ProjectStore snapshot.
packages/agent-server/src/disco/agent_server/routes/preview_edit.py:67:    symlink-safe. The host snapshot path resolves the real path and verifies it stays
packages/agent-server/src/disco/agent_server/routes/preview_edit.py:69:    escapes). Tradeoff: a just-written file not yet snapshotted won't preview-edit —
packages/agent-server/src/disco/agent_server/routes/preview_edit.py:76:        workspace = ps.path_for(conversation_id).resolve()
packages/agent-server/src/disco/agent_server/routes/preview_edit.py:126:    @router.get("/conversations/{conversation_id}/preview-edit/{path:path}")
packages/agent-server/src/disco/agent_server/routes/preview_edit.py:127:    async def preview_edit(conversation_id: str, path: str) -> Response:
packages/agent-server/src/disco/agent_server/routes/preview_edit.py:144:        data = _read_workspace_file_safe(runtime, conversation_id, norm)
packages/agent-server/src/disco/agent_server/routes/schedules.py:19:    @router.post("/api/conversations/{conversation_id}/schedules")
packages/agent-server/src/disco/agent_server/routes/schedules.py:21:        conversation_id: str,
packages/agent-server/src/disco/agent_server/routes/schedules.py:30:        _reject_if_imported(store, conversation_id)  # a schedule would revive a read-only import
packages/agent-server/src/disco/agent_server/routes/schedules.py:42:                conversation_id=conversation_id,
packages/agent-server/src/disco/agent_server/routes/schedules.py:53:    @router.get("/api/conversations/{conversation_id}/schedules")
packages/agent-server/src/disco/agent_server/routes/schedules.py:54:    async def list_schedules_for_conversation(conversation_id: str) -> dict:
packages/agent-server/src/disco/agent_server/routes/schedules.py:60:                owner_id=DEFAULT_OWNER_ID, conversation_id=conversation_id

exec
/bin/bash -lc "rg -n \"def project_store|ProjectStore|project_store\\(\" packages/agent-server/src/disco/agent_server/runtime.py packages/agent-server/src/disco/agent_server/lifecycle.py packages/agent-server/src/disco/agent_server/app.py" in /var/home/dylan/projects/disco
 succeeded in 0ms:
packages/agent-server/src/disco/agent_server/runtime.py:92:    ProjectStore,
packages/agent-server/src/disco/agent_server/runtime.py:1404:        ProjectStore configured — first REHYDRATES the persisted workspace on
packages/agent-server/src/disco/agent_server/runtime.py:1593:    def project_store(self) -> ProjectStore:
packages/agent-server/src/disco/agent_server/runtime.py:1595:        projects endpoints). Always returns a ProjectStore — callers check
packages/agent-server/src/disco/agent_server/runtime.py:1658:    def _project_store_now(self) -> ProjectStore:
packages/agent-server/src/disco/agent_server/runtime.py:1659:        """Build a ProjectStore from the current settings. Built per-call (cheap;
packages/agent-server/src/disco/agent_server/runtime.py:1663:        :class:`ProjectStore`; the validity status is always queryable via
packages/agent-server/src/disco/agent_server/runtime.py:1666:        return ProjectStore(root)

codex
ProjectStore rejects unsafe IDs before touching disk, so the evidence route’s `ps.get(conversation_id)` is covered by that lower-level guard. I’m now on validators, where false positives/negatives are likely to be more concrete.
exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/projects/store.py | sed -n '251,330p' && nl -ba packages/agent-server/src/disco/agent_server/runtime.py | sed -n '1588,1670p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   251	    def get(self, conversation_id: str) -> ProjectRecord | None:
   252	        """Single-row lookup. Returns None if the project doesn't exist (the
   253	        manifest is gone). Returns a record with files_missing=True if the
   254	        manifest is there but the workspace is gone — that's a real state."""
   255	        manifest = self.manifest_for(conversation_id)
   256	        if not manifest.is_file():
   257	            return None
   258	        try:
   259	            data = json.loads(manifest.read_text())
   260	        except (OSError, json.JSONDecodeError) as exc:
   261	            raise StorageError(f"manifest unreadable: {exc}") from exc
   262	        workspace = self.path_for(conversation_id)
   263	        files_missing = (
   264	            not workspace.exists()
   265	            or not workspace.is_dir()
   266	            or not any(workspace.rglob("*"))
   267	        )
   268	        return ProjectRecord(
   269	            conversation_id=conversation_id,
   270	            title=data.get("title"),
   271	            owner_id=data.get("owner_id"),
   272	            created_at=data.get("created_at"),
   273	            last_snapshot_at=data.get("last_snapshot_at"),
   274	            file_count=int(data.get("file_count") or 0),
   275	            total_bytes=int(data.get("total_bytes") or 0),
   276	            files_missing=files_missing,
   277	        )
   278	
   279	    def write_manifest(
   280	        self,
   281	        conversation_id: str,
   282	        *,
   283	        title: str | None,
   284	        owner_id: str | None,
   285	        created_at: str | None,
   286	        file_count: int,
   287	        total_bytes: int,
   288	    ) -> Path:
   289	        """Write the manifest after a snapshot. Atomic via tmp + rename so a
   290	        crash mid-write never leaves the file half-written. Returns the manifest
   291	        path."""
   292	        manifest = self.manifest_for(conversation_id)
   293	        manifest.parent.mkdir(parents=True, exist_ok=True)
   294	        payload: dict[str, Any] = {
   295	            "conversation_id": conversation_id,
   296	            "title": title,
   297	            "owner_id": owner_id,
   298	            "created_at": created_at,
   299	            "last_snapshot_at": _now_iso(),
   300	            "file_count": file_count,
   301	            "total_bytes": total_bytes,
   302	        }
   303	        tmp = manifest.with_suffix(manifest.suffix + ".tmp")
   304	        tmp.write_text(json.dumps(payload, indent=2))
   305	        tmp.replace(manifest)  # atomic on POSIX; close-enough elsewhere
   306	        return manifest
   307	
   308	    def delete(self, conversation_id: str) -> bool:
   309	        """Remove a project's manifest + workspace. The conversation events in
   310	        the SQLite store are left untouched — that's a separate decision the
   311	        caller (the agent-server) makes. Returns True if anything was removed."""
   312	        if self._root is None:
   313	            return False
   314	        project_dir = self._root / conversation_id
   315	        if not project_dir.exists():
   316	            return False
   317	        shutil.rmtree(project_dir)
   318	        return True
   319	
   320	    def iter_workspace(self, conversation_id: str) -> Iterator[Path]:
   321	        """Yield every file path in a project's workspace tree, sorted. Used by
   322	        the download endpoint and by tests. Raises if the workspace is gone."""
   323	        workspace = self.path_for(conversation_id)
   324	        if not workspace.is_dir():
   325	            raise StorageError(f"workspace directory missing: {workspace}")
   326	        yield from sorted(p for p in workspace.rglob("*") if p.is_file())
  1588	
  1589	    def title_service(self) -> TitleService:
  1590	        """Public accessor for the auto-titler (used by the projects backfill route)."""
  1591	        return self._title_service
  1592	
  1593	    def project_store(self) -> ProjectStore:
  1594	        """Public accessor for the live project store (used by the agent-server's
  1595	        projects endpoints). Always returns a ProjectStore — callers check
  1596	        ``.status()`` for the full validation classification (ok / not_found /
  1597	        not_writable etc.).  An empty ``projects_root`` resolves to the
  1598	        auto-default path rather than returning None."""
  1599	        return self._project_store_now()
  1600	
  1601	    # ---- share export (RP-06) ----------------------------------------------
  1602	
  1603	    async def share_export(
  1604	        self,
  1605	        conversation_id: str,
  1606	        *,
  1607	        owner_id: str = DEFAULT_OWNER_ID,
  1608	    ) -> dict[str, Any]:
  1609	        return await self._share.share_export(conversation_id, owner_id=owner_id)
  1610	
  1611	    _IMPORT_MAX_EVENTS = 20_000
  1612	
  1613	    async def share_import(
  1614	        self, bundle: Any, *, owner_id: str = DEFAULT_OWNER_ID
  1615	    ) -> dict[str, Any]:
  1616	        return await self._share.share_import(bundle, owner_id=owner_id)
  1617	
  1618	    @staticmethod
  1619	    def _has_fresh_user_message(events, reports) -> bool:
  1620	        return DeepResearchService._has_fresh_user_message(events, reports)
  1621	
  1622	    async def export_report(
  1623	        self,
  1624	        conversation_id: str,
  1625	        fmt: str,
  1626	        *,
  1627	        owner_id: str = DEFAULT_OWNER_ID,
  1628	    ) -> tuple[bytes, str, str] | None:
  1629	        return await self._dr.export_report(conversation_id, fmt, owner_id=owner_id)
  1630	
  1631	    def create_share_link(
  1632	        self,
  1633	        conversation_id: str,
  1634	        *,
  1635	        owner_id: str = DEFAULT_OWNER_ID,
  1636	    ) -> dict[str, Any]:
  1637	        return self._share.create_share_link(conversation_id, owner_id=owner_id)
  1638	
  1639	    async def create_share_link_async(
  1640	        self,
  1641	        conversation_id: str,
  1642	        *,
  1643	        owner_id: str = DEFAULT_OWNER_ID,
  1644	    ) -> dict[str, Any]:
  1645	        return await self._share.create_share_link_async(
  1646	            conversation_id, owner_id=owner_id
  1647	        )
  1648	
  1649	    def lookup_share_link(self, token: str) -> dict | None:
  1650	        return self._share.lookup_share_link(token)
  1651	
  1652	    def list_share_links(self, *, owner_id: str) -> list[dict]:
  1653	        return self._share.list_share_links(owner_id=owner_id)
  1654	
  1655	    def revoke_share_link(self, token: str, *, owner_id: str) -> bool:
  1656	        return self._share.revoke_share_link(token, owner_id=owner_id)
  1657	
  1658	    def _project_store_now(self) -> ProjectStore:
  1659	        """Build a ProjectStore from the current settings. Built per-call (cheap;
  1660	        matches the rest of the runtime's "reload the config each request"
  1661	        discipline). An empty ``projects_root`` (fresh install) resolves to the
  1662	        auto-default path via :func:`resolve_projects_root` inside
  1663	        :class:`ProjectStore`; the validity status is always queryable via
  1664	        ``store.status()`` at the use site."""
  1665	        root = self._config_store.load().projects.projects_root
  1666	        return ProjectStore(root)
  1667	
  1668	    async def _maybe_rehydrate(self, conversation_id: str) -> None:
  1669	        return await self._lifecycle._maybe_rehydrate(conversation_id)
  1670	

exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/verify/artifact_validators.py | sed -n '1,260p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""Cheap (PR-tier) mechanical artifact validators for the evidence harness (W14a)."""
     2	
     3	import subprocess
     4	
     5	
     6	def validate_pdf(path: str) -> list[str]:
     7	    """Validate a PDF file using pdfinfo and pdftotext."""
     8	    problems: list[str] = []
     9	
    10	    # Check page count
    11	    result = subprocess.run(
    12	        ["pdfinfo", path], capture_output=True, text=True
    13	    )
    14	    if result.returncode != 0:
    15	        problems.append(f"pdfinfo failed: {result.stderr}")
    16	    else:
    17	        pages = None
    18	        for line in result.stdout.splitlines():
    19	            if line.startswith("Pages:"):
    20	                pages = int(line.split(":", 1)[1].strip())
    21	                break
    22	        if pages is None or pages == 0:
    23	            problems.append("pdf has 0 pages")
    24	
    25	    # Check for extractable text
    26	    result = subprocess.run(
    27	        ["pdftotext", path, "-"], capture_output=True, text=True
    28	    )
    29	    if result.returncode == 0 and not result.stdout.strip():
    30	        problems.append("pdf has no extractable text")
    31	
    32	    return problems
    33	
    34	
    35	def validate_audio(path: str) -> list[str]:
    36	    """Validate an audio file using ffprobe."""
    37	    problems: list[str] = []
    38	
    39	    result = subprocess.run(
    40	        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
    41	         "-of", "default=noprint_wrappers=1:nokey=1", path],
    42	        capture_output=True,
    43	        text=True
    44	    )
    45	    if result.returncode != 0:
    46	        problems.append(f"ffprobe failed: {result.stderr}")
    47	    else:
    48	        try:
    49	            duration = float(result.stdout.strip())
    50	            if duration <= 0:
    51	                problems.append("audio duration is zero")
    52	        except ValueError:
    53	            problems.append("audio duration is zero")
    54	
    55	    return problems
    56	
    57	
    58	def validate_sheet(path: str) -> list[str]:
    59	    """Validate an Excel workbook using openpyxl."""
    60	    import openpyxl
    61	
    62	    problems: list[str] = []
    63	
    64	    try:
    65	        wb = openpyxl.load_workbook(path)
    66	    except Exception as e:
    67	        return [f"sheet failed to open: {e}"]
    68	
    69	    if len(wb.sheetnames) == 0:
    70	        problems.append("sheet has no worksheets")
    71	
    72	    return problems
    73	
    74	
    75	def _looks_procedural(img_bytes: bytes) -> bool:
    76	    """True iff an image looks like the keyless ``pil-procedural`` placeholder backend's
    77	    output (W3 bug signature). That backend draws a flat ≤~7-colour palette plus a single
    78	    corner-to-corner diagonal; real photos/diffusion images have hundreds+ of colours.
    79	    Requires BOTH low colour diversity AND the corner-to-corner diagonal so a genuine flat
    80	    illustration/chart is not falsely flagged. Seed-independent (catches any placeholder)."""
    81	    from collections import Counter
    82	    from io import BytesIO
    83	
    84	    try:
    85	        from PIL import Image
    86	    except ImportError:
    87	        return False  # can't introspect without Pillow → don't flag
    88	    try:
    89	        img = Image.open(BytesIO(img_bytes)).convert("RGB")
    90	    except Exception:
    91	        return False
    92	    w, h = img.size
    93	    if w < 4 or h < 4:
    94	        return False
    95	    colors = img.getcolors(maxcolors=4096)
    96	    if colors is None or len(colors) > 12:
    97	        return False  # rich image → not a flat placeholder
    98	    px = img.load()
    99	    if px is None:
   100	        return False
   101	    n = min(w, h)
   102	    diag = [px[round(i * (w - 1) / (n - 1)), round(i * (h - 1) / (n - 1))] for i in range(n)]
   103	    most = Counter(diag).most_common(1)[0][1]
   104	    return most >= n * 0.6  # one colour dominates the diagonal → the placeholder accent line
   105	
   106	
   107	def validate_deck_deliverable(descriptor: dict) -> list[str]:
   108	    """A presentable deck deliverable must not default to RAW HTML (W3/#4). ``descriptor`` is
   109	    the deliverable metadata (e.g. the deck event payload) carrying a ``format`` field."""
   110	    problems: list[str] = []
   111	    if str(descriptor.get("format", "")).lower() == "html":
   112	        problems.append("raw_html_default")
   113	    return problems
   114	
   115	
   116	def validate_deck_file(path: str) -> list[str]:
   117	    """Inspect a rendered deck (``.pptx`` or ``.html``) for embedded PROCEDURAL placeholder
   118	    images (W3 bug). Extracts images (pptx ``ppt/media/*`` or html base64 data-URIs) and flags
   119	    if any looks procedural."""
   120	    import base64
   121	    import pathlib
   122	    import re
   123	    import zipfile
   124	
   125	    problems: list[str] = []
   126	    images: list[bytes] = []
   127	    low = path.lower()
   128	    if low.endswith((".html", ".htm")):
   129	        text = pathlib.Path(path).read_text(errors="replace")
   130	        for m in re.finditer(r"data:image/(?:png|jpe?g);base64,([A-Za-z0-9+/=]+)", text):
   131	            try:
   132	                images.append(base64.b64decode(m.group(1)))
   133	            except Exception:
   134	                continue
   135	    elif low.endswith(".pptx"):
   136	        with zipfile.ZipFile(path) as z:
   137	            for name in z.namelist():
   138	                if name.startswith("ppt/media/") and name.lower().endswith(
   139	                    (".png", ".jpg", ".jpeg")
   140	                ):
   141	                    images.append(z.read(name))
   142	    if any(_looks_procedural(b) for b in images):
   143	        problems.append("procedural_placeholder_image")
   144	    return problems

exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/verify/heavy_validators.py | sed -n '1,260p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""Heavy (VM-only / nightly-tier) artifact validators.
     2	
     3	These RENDER the artifact — LibreOffice for decks, poppler for PDFs, ffmpeg for audio —
     4	rather than only reading metadata, so they catch corruption a mime/format check misses (a
     5	.pptx that unzips fine but won't open, a PDF whose pages are blank). They need binaries
     6	(`soffice`, `pdftoppm`, `ffprobe`) that live on the VM 201 evidence host, NOT the PR gate —
     7	so each returns a single "tool unavailable" note when its binary is missing rather than
     8	raising, and the suite is wired into the nightly (VM) tier in W17.
     9	"""
    10	
    11	from __future__ import annotations
    12	
    13	import pathlib
    14	import shutil
    15	import subprocess
    16	import tempfile
    17	
    18	
    19	def _missing(tool: str) -> bool:
    20	    return shutil.which(tool) is None
    21	
    22	
    23	def validate_pptx_renders(path: str, *, workdir: str | None = None) -> list[str]:
    24	    """Render a ``.pptx`` to PDF with headless LibreOffice — proves the deck actually opens
    25	    and lays out, then reuses the cheap PDF checks on the result."""
    26	    if _missing("soffice"):
    27	        return ["soffice unavailable — run this heavy validator on the VM 201 evidence host"]
    28	    work = workdir or tempfile.mkdtemp(prefix="deckrender-")
    29	    proc = subprocess.run(
    30	        ["soffice", "--headless", "--convert-to", "pdf", "--outdir", work, path],
    31	        capture_output=True,
    32	        text=True,
    33	        timeout=180,
    34	    )
    35	    if proc.returncode != 0:
    36	        return [f"soffice convert failed: {proc.stderr.strip()[:200]}"]
    37	    pdf = pathlib.Path(work) / (pathlib.Path(path).stem + ".pdf")
    38	    if not pdf.exists() or pdf.stat().st_size == 0:
    39	        return ["pptx did not render to a pdf (deck does not open)"]
    40	    from disco.tools.verify.artifact_validators import validate_pdf
    41	
    42	    return validate_pdf(str(pdf))
    43	
    44	
    45	def validate_pdf_renders(path: str, *, workdir: str | None = None) -> list[str]:
    46	    """Rasterize page 1 of a PDF with poppler — proves the page is non-blank (real content,
    47	    not an empty/black page)."""
    48	    if _missing("pdftoppm"):
    49	        return ["pdftoppm unavailable — run this heavy validator on the VM 201 evidence host"]
    50	    work = workdir or tempfile.mkdtemp(prefix="pdfrender-")
    51	    out = pathlib.Path(work) / "page"
    52	    proc = subprocess.run(
    53	        ["pdftoppm", "-png", "-f", "1", "-l", "1", "-r", "50", path, str(out)],
    54	        capture_output=True,
    55	        text=True,
    56	        timeout=120,
    57	    )
    58	    if proc.returncode != 0:
    59	        return [f"pdftoppm failed: {proc.stderr.strip()[:200]}"]
    60	    pngs = list(pathlib.Path(work).glob("page*.png"))
    61	    if not pngs:
    62	        return ["pdf did not rasterize to an image"]
    63	    try:
    64	        from PIL import Image
    65	
    66	        img = Image.open(pngs[0]).convert("L")
    67	        extrema = img.getextrema()  # (min, max) luminance
    68	        if extrema[0] == extrema[1]:
    69	            return ["pdf page 1 is uniformly blank"]
    70	    except ImportError:
    71	        pass  # Pillow optional; the render itself succeeding is already signal
    72	    return []

exec
/bin/bash -lc "nl -ba packages/tools/tests/test_artifact_validators.py | sed -n '1,220p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""Tests for artifact validators."""
     2	
     3	from disco.tools.verify.artifact_validators import (
     4	    validate_pdf,
     5	    validate_sheet,
     6	)
     7	
     8	
     9	def test_validate_pdf_ok():
    10	    """Test that a valid PDF returns an empty problems list."""
    11	    result = validate_pdf("packages/tools/tests/fixtures/artifacts/sample_report.pdf")
    12	    assert result == []
    13	
    14	
    15	def test_validate_pdf_missing():
    16	    """Test that a missing PDF returns a non-empty problems list."""
    17	    result = validate_pdf("/no/such.pdf")
    18	    assert len(result) > 0
    19	
    20	
    21	def test_validate_sheet_missing():
    22	    """Test that a missing sheet returns a non-empty problems list."""
    23	    result = validate_sheet("/no/such.xlsx")
    24	    assert len(result) > 0

exec
/bin/bash -lc "nl -ba packages/tools/tests/test_deck_validators.py | sed -n '1,180p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""W14a-deck — deck provenance validators (procedural-image + raw-html-default detection).
     2	
     3	Drives the W18 negative fixtures: a deck embedding the AUTHENTIC procedural placeholder PNG
     4	must be flagged, and a deck deliverable that defaulted to raw html must be flagged — while a
     5	deck with a real (high-entropy) image is NOT flagged.
     6	"""
     7	
     8	from __future__ import annotations
     9	
    10	import base64
    11	import io
    12	import json
    13	import pathlib
    14	
    15	from disco.tools.verify.artifact_validators import (
    16	    _looks_procedural,
    17	    validate_deck_deliverable,
    18	    validate_deck_file,
    19	)
    20	
    21	_REPO = pathlib.Path(__file__).resolve().parents[3]
    22	_NEG = _REPO / "packages/agent-server/tests/fixtures/negative"
    23	
    24	
    25	def test_detects_procedural_image_in_html_deck():
    26	    problems = validate_deck_file(str(_NEG / "procedural_image_deck.html"))
    27	    assert "procedural_placeholder_image" in problems
    28	
    29	
    30	def test_procedural_png_fixture_is_detected_directly():
    31	    img = (_NEG / "procedural_placeholder.png").read_bytes()
    32	    assert _looks_procedural(img) is True
    33	
    34	
    35	def test_rejects_raw_html_default_deliverable():
    36	    descriptor = json.loads((_NEG / "raw_html_deck_deliverable.json").read_text())
    37	    assert "raw_html_default" in validate_deck_deliverable(descriptor)
    38	
    39	
    40	def test_pptx_deliverable_is_accepted():
    41	    assert validate_deck_deliverable({"format": "pptx", "path": "deck.pptx"}) == []
    42	
    43	
    44	def test_real_image_is_not_flagged_as_procedural():
    45	    """A high-entropy (noise) image must NOT trip the procedural heuristic."""
    46	    from PIL import Image
    47	
    48	    rng = bytes((i * 73 + i * i * 11) & 0xFF for i in range(64 * 64 * 3))
    49	    img = Image.frombytes("RGB", (64, 64), rng)
    50	    buf = io.BytesIO()
    51	    img.save(buf, format="PNG")
    52	    assert _looks_procedural(buf.getvalue()) is False
    53	
    54	    # and a deck embedding it produces no procedural flag
    55	    deck = (
    56	        '<html><body><img src="data:image/png;base64,'
    57	        + base64.b64encode(buf.getvalue()).decode()
    58	        + '"></body></html>'
    59	    )
    60	    p = _REPO / "packages/tools/tests" / "_tmp_real_deck.html"
    61	    try:
    62	        p.write_text(deck)
    63	        assert validate_deck_file(str(p)) == []
    64	    finally:
    65	        p.unlink(missing_ok=True)

codex
The procedural heuristic is cheap but fragile: it assumes a low-color, single-color diagonal is unique to the placeholder. I’m checking the negative fixtures and renderer path to see whether the heuristic is evaluated on the real delivered file, not just standalone PNGs.
exec
/bin/bash -lc "nl -ba packages/tools/tests/test_heavy_validators.py | sed -n '1,220p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""W14b — heavy (render-it) validators. poppler (pdftoppm/pdfinfo) is present in CI-lite and
     2	on the VM 201 host, so the PDF rasterizer is unit-tested here; LibreOffice (soffice) is
     3	VM-only, so the pptx renderer's guard path is tested here and the real render runs on VM 201
     4	(proven live against a generated W3-clean deck — see test-record/e2e-full/artifacts/)."""
     5	
     6	from __future__ import annotations
     7	
     8	import pathlib
     9	import shutil
    10	
    11	import pytest
    12	from disco.tools.verify.heavy_validators import validate_pdf_renders, validate_pptx_renders
    13	
    14	_REPO = pathlib.Path(__file__).resolve().parents[3]
    15	_PDF = _REPO / "packages/tools/tests/fixtures/artifacts/sample_report.pdf"
    16	
    17	
    18	@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler not installed")
    19	def test_validate_pdf_renders_real_pdf_nonblank():
    20	    assert validate_pdf_renders(str(_PDF)) == []
    21	
    22	
    23	@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler not installed")
    24	def test_validate_pdf_renders_flags_missing():
    25	    problems = validate_pdf_renders("/no/such/file.pdf")
    26	    assert problems and "failed" in problems[0]
    27	
    28	
    29	def test_validate_pptx_renders_guards_when_soffice_absent():
    30	    """When soffice is missing (the PR gate), the heavy validator returns a clear note rather
    31	    than raising — so it degrades to a skip instead of a false failure."""
    32	    if shutil.which("soffice") is not None:
    33	        pytest.skip("soffice present — guard path not exercised here")
    34	    problems = validate_pptx_renders("anything.pptx")
    35	    assert problems == ["soffice unavailable — run this heavy validator on the VM 201 evidence host"]

exec
/bin/bash -lc "find packages/agent-server/tests/fixtures/negative packages/tools/tests/fixtures -maxdepth 3 -type f -printf '%p %s bytes\\n'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
packages/agent-server/tests/fixtures/negative/procedural_placeholder.png 2373 bytes
packages/agent-server/tests/fixtures/negative/procedural_image_deck.html 3484 bytes
packages/agent-server/tests/fixtures/negative/raw_html_deck_deliverable.json 308 bytes
packages/agent-server/tests/fixtures/negative/no_plan_progress_finished.json 1563 bytes
packages/agent-server/tests/fixtures/negative/README.md 994 bytes
packages/tools/tests/fixtures/artifacts/sample_report.pdf 80059 bytes

exec
/bin/bash -lc "python - <<'PY'
from pathlib import Path
from PIL import Image
from io import BytesIO
from disco.tools.verify.artifact_validators import _looks_procedural
for p in [Path('packages/agent-server/tests/fixtures/negative/procedural_placeholder.png')]:
    img=Image.open(p)
    print(p, img.size, len(img.getcolors(maxcolors=100000) or []), _looks_procedural(p.read_bytes()))
PY" in /var/home/dylan/projects/disco
 exited 1 in 0ms:
Traceback (most recent call last):
  File "<stdin>", line 4, in <module>
ModuleNotFoundError: No module named 'disco'

exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/builtin/image_gen.py | sed -n '270,315p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   270	        c3 = (255 - hue3, hue3, (hue3 * 5) & 0xFF)
   271	        bg = ((seed * 13) & 0xFF, (seed * 17) & 0xFF, (seed * 23) & 0xFF)
   272	
   273	        img = Image.new("RGB", (width, height), bg)
   274	        draw = ImageDraw.Draw(img)
   275	
   276	        # Concentric rings: count + thickness from the seed. Always renders,
   277	        # even at 8x8, so a tiny test image still looks like "something".
   278	        n_rings = 3 + (seed % 4)
   279	        for i in range(n_rings):
   280	            t = (i + 1) / n_rings
   281	            inset = int(min(width, height) * (1 - t) / 2)
   282	            color = (c1, c2, c3)[i % 3]
   283	            draw.rectangle(
   284	                (inset, inset, width - 1 - inset, height - 1 - inset),
   285	                outline=color,
   286	                width=max(1, (seed >> (i * 2)) % 3 + 1),
   287	            )
   288	
   289	        # A diagonal accent: a single line from corner to corner, also
   290	        # seed-driven, so two different seeds don't accidentally produce
   291	        # the same byte stream.
   292	        draw.line((0, 0, width - 1, height - 1), fill=c3, width=1)
   293	
   294	        # PIL's PNG encoder is deterministic for a given input array + build
   295	        # version, which is what makes the round-trip test "byte-identical"
   296	        # hold without needing a fixed binary build. JPEG is NOT deterministic
   297	        # at the bit level across encoders (the JFIF spec leaves a few
   298	        # encoder-chosen fields free), so PNG is the default and the test
   299	        # asserts on PNG magic — JPEG is supported but the test focuses on
   300	        # PNG.
   301	        out_fmt = "JPEG" if fmt == "jpeg" else "PNG"
   302	        buf = io.BytesIO()
   303	        img.save(buf, format=out_fmt)
   304	        return buf.getvalue()
   305	
   306	
   307	class _OpenAIImageBackend:
   308	    """OpenAI-compatible images API backend (OpenAI, ImageRouter, Together, Azure …).
   309	
   310	    Posts to `{base_url}/v1/images/generations` for a bare origin, OR to `base_url`
   311	    verbatim when it already names the full endpoint path (e.g. ImageRouter's
   312	    `https://api.imagerouter.io/v1/openai/images/generations`, which has an extra
   313	    `/openai/` segment that the default suffix can't express). Uses `api_key_env`
   314	    to look up the secret from the encrypted store. Never logs or echoes the key —
   315	    only presence/absence is surfaced in error messages.

codex
Validator issues are concrete: several “validators” can raise when a binary or archive is missing/corrupt, which means the runner may crash or mark the wrong failure unless it wraps every call. I’ll read the verify runner now to see how those exceptions propagate and how terminal state is decided.
exec
/bin/bash -lc "nl -ba packages/agent-server/src/disco/agent_server/verify/runner.py | sed -n '1,240p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""disco-verify API-first runner (W15).
     2	
     3	Drives the app through its HTTP/WS API exactly like a frontend would — no
     4	browser, no internal calls — and writes a redacted evidence dossier per run.
     5	
     6	Key invariant (from codex): ``disco-verify`` is API-FIRST and has NO
     7	Playwright/browser dependency. Every signal comes from the real HTTP/WS
     8	boundary the frontend uses.
     9	
    10	Usage::
    11	
    12	    from disco.agent_server.verify.runner import run_scenario
    13	    from disco.agent_server.verify.scenarios import slides_from_research_report
    14	
    15	    result = await run_scenario(slides_from_research_report, agent_base="http://127.0.0.1:8000")
    16	    print(result.passed, result.dossier_path)
    17	
    18	Injectable transport (``_client`` param) keeps IO decoupled from logic so unit
    19	tests can feed canned events without a live server.
    20	"""
    21	
    22	from __future__ import annotations
    23	
    24	import asyncio
    25	import json
    26	import logging
    27	import time
    28	import uuid
    29	from abc import ABC, abstractmethod
    30	from datetime import UTC, datetime
    31	from pathlib import Path
    32	from typing import Any
    33	
    34	import disco.tools.verify.artifact_validators as _av
    35	import httpx
    36	from disco.core.evidence.schema import redact
    37	from websockets.asyncio.client import connect as _ws_connect  # has py.typed
    38	
    39	from .schema import Scenario, VerifyResult
    40	
    41	log = logging.getLogger(__name__)
    42	
    43	# Statuses from which the conversation will never advance without user action.
    44	_TERMINAL: frozenset[str] = frozenset({"FINISHED", "ERROR", "STUCK", "IDLE"})
    45	
    46	# Seconds between HTTP state polls.
    47	_POLL_INTERVAL: float = 2.0
    48	
    49	
    50	# ---------------------------------------------------------------------------
    51	# Abstract transport (injectable for testing)
    52	# ---------------------------------------------------------------------------
    53	
    54	
    55	class AbstractVerifyClient(ABC):
    56	    """Injectable IO layer for ``run_scenario``.
    57	
    58	    The production implementation (``HttpVerifyClient``) uses real httpx and
    59	    websockets connections. Unit tests inject a ``FakeVerifyClient`` that
    60	    returns pre-canned responses without a live server.
    61	    """
    62	
    63	    @abstractmethod
    64	    async def create_conversation(
    65	        self, surface: str, model_override: str | None
    66	    ) -> str:
    67	        """POST /conversations → return the new conversation_id."""
    68	        ...
    69	
    70	    @abstractmethod
    71	    async def run_ws_exchange(
    72	        self,
    73	        cid: str,
    74	        prompt: str,
    75	        *,
    76	        approve_plan: bool,
    77	        timeout_s: float,
    78	    ) -> None:
    79	        """Open the WS, send the user message, optionally approve the plan.
    80	
    81	        The connection is closed before returning; the caller then polls
    82	        HTTP state separately.
    83	        """
    84	        ...
    85	
    86	    @abstractmethod
    87	    async def poll_until_terminal(
    88	        self,
    89	        cid: str,
    90	        *,
    91	        timeout_s: float,
    92	    ) -> dict[str, Any]:
    93	        """GET /conversations/{cid}/state in a loop until a terminal status or timeout."""
    94	        ...
    95	
    96	    @abstractmethod
    97	    async def get_events(self, cid: str) -> list[dict[str, Any]]:
    98	        """GET /conversations/{cid}/events (all pages)."""
    99	        ...
   100	
   101	    @abstractmethod
   102	    async def get_state(self, cid: str) -> dict[str, Any]:
   103	        """GET /conversations/{cid}/state (one-shot snapshot after run)."""
   104	        ...
   105	
   106	    @abstractmethod
   107	    async def get_trace(self, cid: str) -> dict[str, Any] | None:
   108	        """GET /api/debug/trace/{cid} — None when DISCO_INSPECT is off or no trace."""
   109	        ...
   110	
   111	    @abstractmethod
   112	    async def get_manifest(self, cid: str) -> dict[str, Any] | None:
   113	        """GET /api/projects/{cid}/manifest — None when project storage is unavailable."""
   114	        ...
   115	
   116	
   117	# ---------------------------------------------------------------------------
   118	# Production HTTP/WS client
   119	# ---------------------------------------------------------------------------
   120	
   121	
   122	class HttpVerifyClient(AbstractVerifyClient):
   123	    """Production transport: drives the real app over HTTP and WebSocket."""
   124	
   125	    def __init__(self, base_url: str) -> None:
   126	        self._base_url = base_url.rstrip("/")
   127	
   128	    async def create_conversation(
   129	        self, surface: str, model_override: str | None
   130	    ) -> str:
   131	        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as hc:
   132	            resp = await hc.post(
   133	                "/conversations",
   134	                json={"surface": surface, "model_override": model_override},
   135	            )
   136	            resp.raise_for_status()
   137	            return str(resp.json()["conversation_id"])
   138	
   139	    async def run_ws_exchange(
   140	        self,
   141	        cid: str,
   142	        prompt: str,
   143	        *,
   144	        approve_plan: bool,
   145	        timeout_s: float,
   146	    ) -> None:
   147	        ws_base = (
   148	            self._base_url.replace("http://", "ws://").replace("https://", "wss://")
   149	        )
   150	        ws_url = f"{ws_base}/ws/conversations/{cid}"
   151	        deadline = time.monotonic() + timeout_s
   152	        try:
   153	            async with _ws_connect(ws_url) as ws:
   154	                await ws.send(
   155	                    json.dumps({"type": "send_message", "content": prompt})
   156	                )
   157	                if not approve_plan:
   158	                    return
   159	                # Wait for AWAITING_PLAN_APPROVAL and then approve it.
   160	                while True:
   161	                    remaining = deadline - time.monotonic()
   162	                    if remaining <= 0:
   163	                        log.warning(
   164	                            "run_ws_exchange: timed out waiting for AWAITING_PLAN_APPROVAL on %s",
   165	                            cid,
   166	                        )
   167	                        break
   168	                    try:
   169	                        raw: str = await asyncio.wait_for(
   170	                            ws.recv(decode=True), timeout=min(remaining, 10.0)
   171	                        )
   172	                    except TimeoutError:
   173	                        continue
   174	                    frame: dict[str, Any] = json.loads(raw)
   175	                    status = _status_from_frame(frame)
   176	                    if status == "AWAITING_PLAN_APPROVAL":
   177	                        await ws.send(json.dumps({"type": "approve_plan"}))
   178	                        break
   179	                    if status in _TERMINAL:
   180	                        break
   181	        except Exception as exc:  # noqa: BLE001
   182	            log.warning("run_ws_exchange error on %s: %s", cid, exc)
   183	
   184	    async def poll_until_terminal(
   185	        self,
   186	        cid: str,
   187	        *,
   188	        timeout_s: float,
   189	    ) -> dict[str, Any]:
   190	        deadline = time.monotonic() + timeout_s
   191	        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as hc:
   192	            while True:
   193	                resp = await hc.get(f"/conversations/{cid}/state")
   194	                resp.raise_for_status()
   195	                state: dict[str, Any] = resp.json()
   196	                status = str(state.get("execution_status", ""))
   197	                if status in _TERMINAL:
   198	                    return state
   199	                if time.monotonic() >= deadline:
   200	                    log.warning(
   201	                        "poll_until_terminal: timeout for %s (last status=%s)", cid, status
   202	                    )
   203	                    return state
   204	                await asyncio.sleep(_POLL_INTERVAL)
   205	
   206	    async def get_events(self, cid: str) -> list[dict[str, Any]]:
   207	        all_events: list[dict[str, Any]] = []
   208	        after_seq: int | None = None
   209	        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as hc:
   210	            while True:
   211	                params: dict[str, Any] = {"limit": 200}
   212	                if after_seq is not None:
   213	                    params["after_seq"] = after_seq
   214	                resp = await hc.get(f"/conversations/{cid}/events", params=params)
   215	                resp.raise_for_status()
   216	                data: dict[str, Any] = resp.json()
   217	                page_events: list[dict[str, Any]] = data.get("events") or []
   218	                all_events.extend(page_events)
   219	                next_cursor = data.get("next_cursor")
   220	                if next_cursor is None:
   221	                    break
   222	                after_seq = int(next_cursor)
   223	        return all_events
   224	
   225	    async def get_state(self, cid: str) -> dict[str, Any]:
   226	        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as hc:
   227	            resp = await hc.get(f"/conversations/{cid}/state")
   228	            resp.raise_for_status()
   229	            result: dict[str, Any] = resp.json()
   230	            return result
   231	
   232	    async def get_trace(self, cid: str) -> dict[str, Any] | None:
   233	        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as hc:
   234	            resp = await hc.get(f"/api/debug/trace/{cid}")
   235	            if resp.status_code == 200:
   236	                result: dict[str, Any] = resp.json()
   237	                return result
   238	            return None
   239	
   240	    async def get_manifest(self, cid: str) -> dict[str, Any] | None:

exec
/bin/bash -lc "nl -ba packages/agent-server/src/disco/agent_server/verify/runner.py | sed -n '240,520p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   240	    async def get_manifest(self, cid: str) -> dict[str, Any] | None:
   241	        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as hc:
   242	            resp = await hc.get(f"/api/projects/{cid}/manifest")
   243	            if resp.status_code == 200:
   244	                result: dict[str, Any] = resp.json()
   245	                return result
   246	            return None
   247	
   248	
   249	# ---------------------------------------------------------------------------
   250	# Internal helpers
   251	# ---------------------------------------------------------------------------
   252	
   253	
   254	def _status_from_frame(frame: dict[str, Any]) -> str:
   255	    """Extract an execution_status string from a WS server frame, or ''."""
   256	    if frame.get("type") == "state":
   257	        state = frame.get("state") or {}
   258	        return str(state.get("execution_status", ""))
   259	    if frame.get("type") == "event":
   260	        event = frame.get("event") or {}
   261	        if event.get("kind") == "status":
   262	            return str(event.get("status", ""))
   263	    return ""
   264	
   265	
   266	def _locate_deliverables(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
   267	    """Scan the event log and return workspace-relative deliverable paths.
   268	
   269	    Recognises the same tool names as ``_common._declared_artifacts`` so that
   270	    both the download jail and the verifier agree on what the run produced.
   271	
   272	    Each entry is ``{"path": str, "kind": str, "tool": str}``.
   273	    """
   274	    deliverables: list[dict[str, Any]] = []
   275	    for evt in events:
   276	        kind = evt.get("kind")
   277	        if kind == "observation":
   278	            tr: dict[str, Any] = evt.get("tool_result") or {}
   279	            if not tr.get("success"):
   280	                continue
   281	            tool_name = str(tr.get("tool_name", ""))
   282	            structured: dict[str, Any] = tr.get("structured") or {}
   283	            if tool_name in ("sheet_generate", "slides_generate"):
   284	                fn = structured.get("filename")
   285	                if isinstance(fn, str) and fn:
   286	                    deliverables.append({"path": fn, "kind": "file", "tool": tool_name})
   287	                es = structured.get("editable_source")
   288	                if isinstance(es, str) and es:
   289	                    deliverables.append({"path": es, "kind": "editable_source", "tool": tool_name})
   290	            elif tool_name == "image_generate":
   291	                p = structured.get("path")
   292	                if isinstance(p, str) and p:
   293	                    deliverables.append({"path": p, "kind": "image", "tool": tool_name})
   294	            elif tool_name == "audio_overview":
   295	                for key in ("mp3_path", "transcript_path"):
   296	                    p = structured.get(key)
   297	                    if isinstance(p, str) and p:
   298	                        deliverables.append(
   299	                            {"path": p, "kind": key.replace("_path", ""), "tool": tool_name}
   300	                        )
   301	        elif kind == "deliverable":
   302	            artifact_kind = str(evt.get("artifact_kind", "app"))
   303	            if artifact_kind == "files":
   304	                path = str(evt.get("path", ""))
   305	                if path:
   306	                    deliverables.append({"path": path, "kind": "files", "tool": "deliverable"})
   307	    return deliverables
   308	
   309	
   310	def _run_validators(
   311	    scenario: Scenario,
   312	    deliverables: list[dict[str, Any]],
   313	    events: list[dict[str, Any]],
   314	) -> list[str]:
   315	    """Run forbid checks and file-level validators; return all problem strings.
   316	
   317	    Forbid checks operate on the event log and deliverable list (no file IO).
   318	    File validators (pdf/audio/sheet) run only when the path exists on disk —
   319	    in integration tests the sandbox workspace is locally accessible; in unit
   320	    tests with fake clients the paths won't exist so file validators are skipped.
   321	    """
   322	    problems: list[str] = []
   323	
   324	    # ---- forbid checks --------------------------------------------------------
   325	    for item in scenario.forbid:
   326	        if item == "raw_html_default":
   327	            html_paths = [d["path"] for d in deliverables if str(d["path"]).endswith(".html")]
   328	            non_html = [d["path"] for d in deliverables if not str(d["path"]).endswith(".html")]
   329	            if html_paths and not non_html:
   330	                problems.append(
   331	                    f"forbid.raw_html_default: all deliverables are raw HTML: {html_paths}"
   332	                )
   333	        elif item == "procedural_image_provider":
   334	            found = False
   335	            for evt in events:
   336	                if evt.get("kind") != "observation":
   337	                    continue
   338	                tr = evt.get("tool_result") or {}
   339	                if tr.get("tool_name") != "image_generate":
   340	                    continue
   341	                if not tr.get("success"):
   342	                    continue
   343	                structured: dict[str, Any] = tr.get("structured") or {}
   344	                if str(structured.get("provider", "")) == "procedural":
   345	                    problems.append(
   346	                        "forbid.procedural_image_provider: image_generate used provider=procedural"
   347	                    )
   348	                    found = True
   349	                    break
   350	            if found:
   351	                pass  # already appended
   352	        else:
   353	            log.debug("Unknown forbid item %r — skipped", item)
   354	
   355	    # ---- file validators ------------------------------------------------------
   356	    for d in deliverables:
   357	        path = str(d.get("path", ""))
   358	        if not path or not Path(path).exists():
   359	            continue  # workspace-relative; skip when not locally accessible
   360	        if path.endswith(".pdf"):
   361	            problems.extend(_av.validate_pdf(path))
   362	        elif path.endswith((".mp3", ".wav")):
   363	            problems.extend(_av.validate_audio(path))
   364	        elif path.endswith(".xlsx"):
   365	            problems.extend(_av.validate_sheet(path))
   366	
   367	    return problems
   368	
   369	
   370	def _write_dossier(
   371	    dossier: Path,
   372	    cid: str,
   373	    scenario: Scenario,
   374	    final_state: dict[str, Any],
   375	    events: list[dict[str, Any]],
   376	    trace: dict[str, Any] | None,
   377	    manifest: dict[str, Any] | None,
   378	    result: VerifyResult,
   379	) -> None:
   380	    """Write the redacted evidence dossier to *dossier* (created if absent).
   381	
   382	    Files written:
   383	    - ``result.json``    — scenario + VerifyResult, redacted
   384	    - ``events.jsonl``  — one event per line, redacted
   385	    - ``state.json``    — final state snapshot, redacted
   386	    - ``trace.json``    — DISCO_INSPECT trace (if available), redacted
   387	    - ``manifest.json`` — project manifest (if available), redacted
   388	    """
   389	    dossier.mkdir(parents=True, exist_ok=True)
   390	
   391	    (dossier / "result.json").write_text(
   392	        json.dumps(
   393	            redact(
   394	                {
   395	                    "run_id": dossier.name,
   396	                    "conversation_id": cid,
   397	                    "scenario": scenario.model_dump(mode="json"),
   398	                    "result": result.model_dump(mode="json"),
   399	                }
   400	            ),
   401	            indent=2,
   402	        ),
   403	        encoding="utf-8",
   404	    )
   405	
   406	    with (dossier / "events.jsonl").open("w", encoding="utf-8") as fh:
   407	        for evt in events:
   408	            fh.write(json.dumps(redact(evt)) + "\n")
   409	
   410	    (dossier / "state.json").write_text(
   411	        json.dumps(redact(final_state), indent=2),
   412	        encoding="utf-8",
   413	    )
   414	
   415	    if trace is not None:
   416	        (dossier / "trace.json").write_text(
   417	            json.dumps(redact(trace), indent=2),
   418	            encoding="utf-8",
   419	        )
   420	
   421	    if manifest is not None:
   422	        (dossier / "manifest.json").write_text(
   423	            json.dumps(redact(manifest), indent=2),
   424	            encoding="utf-8",
   425	        )
   426	
   427	
   428	# ---------------------------------------------------------------------------
   429	# Public entry point
   430	# ---------------------------------------------------------------------------
   431	
   432	
   433	async def run_scenario(
   434	    scenario: Scenario,
   435	    *,
   436	    agent_base: str = "http://127.0.0.1:8000",
   437	    timeout_s: float = 600,
   438	    dossier_base: Path | None = None,
   439	    _client: AbstractVerifyClient | None = None,
   440	) -> VerifyResult:
   441	    """Run one scenario against the live app and return a ``VerifyResult``.
   442	
   443	    Drives the app through its HTTP/WS API (no browser, no internal calls):
   444	
   445	    (a) POST /conversations with the scenario's surface/model_override.
   446	    (b) Open the WS, send {"type":"send_message",...}; if ``approve_plan`` is
   447	        True, wait for AWAITING_PLAN_APPROVAL and send {"type":"approve_plan"}.
   448	    (c) Poll /conversations/{cid}/state until FINISHED/ERROR/STUCK/IDLE or timeout.
   449	    (d) Fetch evidence: events, state, /api/debug/trace/{cid}, /api/projects/{cid}/manifest.
   450	    (e) Locate deliverables from the event log.
   451	    (f) Run forbid checks and file validators (pdf/audio/sheet from artifact_validators).
   452	    (g) Write a redacted dossier under ``dossier_base/<run-id>/``.
   453	
   454	    Parameters
   455	    ----------
   456	    scenario:
   457	        The scenario to execute.
   458	    agent_base:
   459	        Base URL of the agent-server (e.g. ``http://127.0.0.1:8000``).
   460	    timeout_s:
   461	        Hard wall-clock timeout; effective timeout = min(scenario.timeout_s, timeout_s).
   462	    dossier_base:
   463	        Root directory for evidence output. Defaults to ``./test-record/disco-verify``.
   464	    _client:
   465	        Injectable transport for testing. Pass a ``FakeVerifyClient`` to skip live IO.
   466	    """
   467	    if dossier_base is None:
   468	        dossier_base = Path("test-record") / "disco-verify"
   469	
   470	    effective_timeout = min(float(scenario.timeout_s), timeout_s)
   471	    client: AbstractVerifyClient = _client if _client is not None else HttpVerifyClient(agent_base)
   472	
   473	    run_ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
   474	    run_id = f"run_{run_ts}_{uuid.uuid4().hex[:8]}"
   475	
   476	    log.info("[%s] scenario=%r surface=%s", run_id, scenario.id, scenario.surface)
   477	
   478	    # (a) create conversation
   479	    cid = await client.create_conversation(scenario.surface, scenario.model_override)
   480	    log.info("[%s] cid=%s", run_id, cid)
   481	
   482	    # (b) WS exchange: send message; optionally wait for plan approval
   483	    await client.run_ws_exchange(
   484	        cid,
   485	        scenario.prompt,
   486	        approve_plan=scenario.approve_plan,
   487	        timeout_s=effective_timeout,
   488	    )
   489	
   490	    # (c) poll until terminal
   491	    final_state = await client.poll_until_terminal(cid, timeout_s=effective_timeout)
   492	    terminal_status = str(final_state.get("execution_status", "UNKNOWN"))
   493	    log.info("[%s] cid=%s terminal_status=%s", run_id, cid, terminal_status)
   494	
   495	    # (d) fetch evidence — fresh snapshots after the run terminates
   496	    events = await client.get_events(cid)
   497	    # Re-fetch state for the dossier (clean post-run snapshot distinct from the
   498	    # poll's last-seen state, which may have been fetched mid-sleep).
   499	    evidence_state = await client.get_state(cid)
   500	    trace = await client.get_trace(cid)
   501	    manifest = await client.get_manifest(cid)
   502	
   503	    # (e) locate deliverables
   504	    deliverables = _locate_deliverables(events)
   505	    log.info("[%s] deliverables=%d", run_id, len(deliverables))
   506	
   507	    # (f) validate
   508	    validator_problems = _run_validators(scenario, deliverables, events)
   509	    if validator_problems:
   510	        log.warning("[%s] %d problem(s): %s", run_id, len(validator_problems), validator_problems)
   511	
   512	    # compute passed
   513	    expected_status = scenario.expect.get("terminal_status")
   514	    status_ok = expected_status is None or terminal_status == str(expected_status)
   515	    passed = not validator_problems and status_ok
   516	
   517	    # build result with the dossier path already set
   518	    dossier_path = dossier_base / run_id
   519	    result = VerifyResult(
   520	        scenario_id=scenario.id,

exec
/bin/bash -lc "nl -ba packages/agent-server/src/disco/agent_server/verify/schema.py | sed -n '1,260p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""Pydantic models for the disco-verify API-first runner (W15).
     2	
     3	``Scenario`` describes what to submit to the app and what invariants to check.
     4	``VerifyResult`` captures the outcome of one ``run_scenario`` call, including
     5	the path to the written evidence dossier.
     6	"""
     7	
     8	from __future__ import annotations
     9	
    10	from typing import Any
    11	
    12	from pydantic import BaseModel, Field
    13	
    14	
    15	class Scenario(BaseModel):
    16	    """A single disco-verify scenario — what to submit and what to assert.
    17	
    18	    The runner drives the app through its HTTP/WS API using these fields and
    19	    then checks the declared invariants against the live evidence.
    20	    """
    21	
    22	    # Unique id used in dossier filenames and result reporting.
    23	    id: str
    24	    # Which surface to open: "research" | "build" | "agent" | "deep_research".
    25	    surface: str = "agent"
    26	    # The user message to send once the conversation is created.
    27	    prompt: str
    28	    # Optional model key to pin for this conversation (None → server default).
    29	    model_override: str | None = None
    30	    # When True, the runner waits for AWAITING_PLAN_APPROVAL over the WS and
    31	    # sends {"type": "approve_plan"} before polling for terminal state.
    32	    approve_plan: bool = False
    33	    # Expected invariants (machine-readable). Known keys:
    34	    #   terminal_status: "FINISHED" | "ERROR" | "STUCK" | "IDLE"
    35	    #   deliverable_type: "deck" | "pdf" | "audio" | "sheet" | …
    36	    expect: dict[str, Any] = Field(default_factory=dict)
    37	    # Logical invariants that must NOT be present. Known tokens:
    38	    #   "raw_html_default"         — no .html-only deliverable output
    39	    #   "procedural_image_provider" — image_generate must not use provider=procedural
    40	    forbid: list[str] = Field(default_factory=list)
    41	    # Per-scenario timeout in seconds (overridden downward by run_scenario timeout_s).
    42	    timeout_s: int = 600
    43	
    44	
    45	class VerifyResult(BaseModel):
    46	    """The outcome of one ``run_scenario`` call.
    47	
    48	    Written as ``result.json`` inside the dossier and returned to the caller.
    49	    ``passed`` is the single go/no-go signal: True iff no validator problems
    50	    AND the declared ``expect.terminal_status`` (if any) was reached.
    51	    """
    52	
    53	    # Mirrors the scenario id for traceability.
    54	    scenario_id: str
    55	    # The conversation's ``execution_status`` at the time polling terminated.
    56	    terminal_status: str
    57	    # Deliverables located from the event log: each dict has ``path``, ``kind``,
    58	    # ``tool`` keys and is workspace-relative.
    59	    deliverables: list[dict[str, Any]]
    60	    # Non-empty when any validator or forbid check fired. Each entry is a
    61	    # human-readable problem description.
    62	    validator_problems: list[str]
    63	    # True iff validator_problems is empty AND the expected terminal_status matched.
    64	    passed: bool
    65	    # Absolute path to the written dossier folder.
    66	    dossier_path: str

exec
/bin/bash -lc "nl -ba packages/agent-server/src/disco/agent_server/verify/scenarios.py | sed -n '1,220p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""Pre-defined disco-verify scenarios (W15).
     2	
     3	Import these objects and pass them directly to ``run_scenario``::
     4	
     5	    from disco.agent_server.verify.scenarios import slides_from_research_report
     6	    from disco.agent_server.verify.runner import run_scenario
     7	
     8	    result = await run_scenario(slides_from_research_report)
     9	
    10	Each scenario encodes exactly what to submit to the app and which invariants
    11	to check — the harness fills in the evidence and decides passed/failed.
    12	"""
    13	
    14	from __future__ import annotations
    15	
    16	from .schema import Scenario
    17	
    18	# ---------------------------------------------------------------------------
    19	# slides_from_research_report
    20	# ---------------------------------------------------------------------------
    21	# The canonical "slides" scenario from the codex round-1 / campaign design.
    22	# Checks that the default slides deliverable is a presentable format (pptx/pdf)
    23	# and that neither raw HTML nor procedural placeholder images are produced.
    24	
    25	slides_from_research_report = Scenario(
    26	    id="slides_from_research_report",
    27	    surface="agent",
    28	    prompt="Make slides from this research report",
    29	    model_override=None,
    30	    approve_plan=True,  # approve the plan so the build actually runs to a deliverable
    31	    expect={
    32	        "terminal_status": "FINISHED",
    33	        "deliverable_type": "deck",
    34	    },
    35	    forbid=[
    36	        "raw_html_default",        # deliverables must not be raw HTML only
    37	        "procedural_image_provider",  # images must not come from the procedural tier
    38	    ],
    39	    timeout_s=600,
    40	)
    41	
    42	# ---------------------------------------------------------------------------
    43	# missing_file_sandbox_error
    44	# ---------------------------------------------------------------------------
    45	# Exercises the W1/W2 contract LIVE: a build that touches a nonexistent file must reach a
    46	# TERMINAL state (never hang at RUNNING). A missing-file READ through the agent's own tool is
    47	# handled gracefully (the agent is told the file is absent and finishes), so the realistic
    48	# terminal here is FINISHED — the invariant under test is "the loop terminalizes, no silent
    49	# hang." (The crash→ERROR path of W1/W2 — an uncaught SandboxError in the loop's own
    50	# bookkeeping reads — is proven deterministically by the W1/W2 unit contracts.)
    51	
    52	missing_file_sandbox_error = Scenario(
    53	    id="missing_file_sandbox_error",
    54	    surface="build",
    55	    prompt="Read the workspace file missing_notes_99999.txt and summarise it",
    56	    model_override=None,
    57	    approve_plan=True,  # approve so the build runs and actually touches the missing file
    58	    expect={
    59	        "terminal_status": "FINISHED",
    60	    },
    61	    forbid=[],
    62	    timeout_s=180,
    63	)

codex
I found a pass-when-it-should-fail path in `disco-verify`: it only fails on status mismatch if the scenario declares an expected terminal status. A timed-out RUNNING scenario with no expectation can pass. I’m checking the unit tests to see if they lock in or miss this.
exec
/bin/bash -lc "nl -ba packages/agent-server/src/disco/agent_server/verify/runner.py | sed -n '512,570p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   512	    # compute passed
   513	    expected_status = scenario.expect.get("terminal_status")
   514	    status_ok = expected_status is None or terminal_status == str(expected_status)
   515	    passed = not validator_problems and status_ok
   516	
   517	    # build result with the dossier path already set
   518	    dossier_path = dossier_base / run_id
   519	    result = VerifyResult(
   520	        scenario_id=scenario.id,
   521	        terminal_status=terminal_status,
   522	        deliverables=deliverables,
   523	        validator_problems=validator_problems,
   524	        passed=passed,
   525	        dossier_path=str(dossier_path.resolve()),
   526	    )
   527	
   528	    # (g) write dossier — use evidence_state (fresh post-run snapshot) for state.json
   529	    _write_dossier(dossier_path, cid, scenario, evidence_state, events, trace, manifest, result)
   530	    log.info("[%s] dossier=%s passed=%s", run_id, dossier_path, passed)
   531	
   532	    return result

exec
/bin/bash -lc "nl -ba packages/agent-server/tests/test_disco_verify_runner.py | sed -n '1,260p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""Unit tests for the disco-verify API-first runner (W15).
     2	
     3	These tests exercise ``run_scenario``'s logic against a FAKE in-process client
     4	so NO live server is required.  The ``FakeVerifyClient`` feeds canned events and
     5	a predetermined final state; the test asserts that the runner:
     6	
     7	 1. Creates the conversation and sends the message (via the client calls).
     8	 2. Locates deliverables from the event log.
     9	 3. Runs validators (forbid checks + file-level validators).
    10	 4. Computes ``passed`` correctly (all green → True; any problem → False).
    11	 5. Writes a dossier with the expected files.
    12	
    13	Async test execution uses ``asyncio_mode = "auto"`` (set in pyproject.toml).
    14	"""
    15	
    16	from __future__ import annotations
    17	
    18	import json
    19	from pathlib import Path
    20	from typing import Any
    21	
    22	import pytest
    23	from disco.agent_server.verify.runner import (
    24	    AbstractVerifyClient,
    25	    _locate_deliverables,
    26	    _run_validators,
    27	    run_scenario,
    28	)
    29	from disco.agent_server.verify.scenarios import (
    30	    missing_file_sandbox_error,
    31	    slides_from_research_report,
    32	)
    33	from disco.agent_server.verify.schema import Scenario, VerifyResult
    34	
    35	# ---------------------------------------------------------------------------
    36	# Fake transport — feeds canned responses, records calls
    37	# ---------------------------------------------------------------------------
    38	
    39	
    40	class FakeVerifyClient(AbstractVerifyClient):
    41	    """Injectable transport for unit tests — no network, no live app."""
    42	
    43	    def __init__(
    44	        self,
    45	        *,
    46	        cid: str = "conv_fake0001",
    47	        final_state: dict[str, Any] | None = None,
    48	        events: list[dict[str, Any]] | None = None,
    49	        trace: dict[str, Any] | None = None,
    50	        manifest: dict[str, Any] | None = None,
    51	    ) -> None:
    52	        self.cid = cid
    53	        self._final_state: dict[str, Any] = final_state or {"execution_status": "FINISHED"}
    54	        self._events: list[dict[str, Any]] = events or []
    55	        self._trace = trace
    56	        self._manifest = manifest
    57	        # call-tracking for assertions
    58	        self.calls: list[str] = []
    59	        self.ws_prompt: str | None = None
    60	        self.ws_approve_plan: bool = False
    61	
    62	    async def create_conversation(
    63	        self, surface: str, model_override: str | None
    64	    ) -> str:
    65	        self.calls.append("create_conversation")
    66	        return self.cid
    67	
    68	    async def run_ws_exchange(
    69	        self,
    70	        cid: str,
    71	        prompt: str,
    72	        *,
    73	        approve_plan: bool,
    74	        timeout_s: float,
    75	    ) -> None:
    76	        self.calls.append("run_ws_exchange")
    77	        self.ws_prompt = prompt
    78	        self.ws_approve_plan = approve_plan
    79	
    80	    async def poll_until_terminal(
    81	        self,
    82	        cid: str,
    83	        *,
    84	        timeout_s: float,
    85	    ) -> dict[str, Any]:
    86	        self.calls.append("poll_until_terminal")
    87	        return dict(self._final_state)
    88	
    89	    async def get_events(self, cid: str) -> list[dict[str, Any]]:
    90	        self.calls.append("get_events")
    91	        return list(self._events)
    92	
    93	    async def get_state(self, cid: str) -> dict[str, Any]:
    94	        self.calls.append("get_state")
    95	        return dict(self._final_state)
    96	
    97	    async def get_trace(self, cid: str) -> dict[str, Any] | None:
    98	        self.calls.append("get_trace")
    99	        return self._trace
   100	
   101	    async def get_manifest(self, cid: str) -> dict[str, Any] | None:
   102	        self.calls.append("get_manifest")
   103	        return self._manifest
   104	
   105	
   106	# ---------------------------------------------------------------------------
   107	# Helper factories for canned events
   108	# ---------------------------------------------------------------------------
   109	
   110	
   111	def _slides_observation_event(filename: str = "output.pptx") -> dict[str, Any]:
   112	    """A successful slides_generate ObservationEvent with a .pptx deliverable."""
   113	    return {
   114	        "kind": "observation",
   115	        "tool_result": {
   116	            "success": True,
   117	            "tool_name": "slides_generate",
   118	            "call_id": "call_001",
   119	            "content": "Slides generated.",
   120	            "structured": {"filename": filename, "slide_count": 5},
   121	        },
   122	    }
   123	
   124	
   125	def _deliverable_files_event(path: str = "output.pptx") -> dict[str, Any]:
   126	    """A DeliverableEvent of kind 'files'."""
   127	    return {
   128	        "kind": "deliverable",
   129	        "artifact_kind": "files",
   130	        "path": path,
   131	        "title": "Research Slides",
   132	        "deployment_url": "",
   133	    }
   134	
   135	
   136	def _image_observation_event(provider: str = "openrouter") -> dict[str, Any]:
   137	    """A successful image_generate ObservationEvent."""
   138	    return {
   139	        "kind": "observation",
   140	        "tool_result": {
   141	            "success": True,
   142	            "tool_name": "image_generate",
   143	            "call_id": "call_002",
   144	            "content": "Image generated.",
   145	            "structured": {"path": "slide_image.png", "provider": provider},
   146	        },
   147	    }
   148	
   149	
   150	def _html_observation_event() -> dict[str, Any]:
   151	    """A successful slides_generate ObservationEvent producing raw HTML."""
   152	    return {
   153	        "kind": "observation",
   154	        "tool_result": {
   155	            "success": True,
   156	            "tool_name": "slides_generate",
   157	            "call_id": "call_003",
   158	            "content": "HTML generated.",
   159	            "structured": {"filename": "output.html"},
   160	        },
   161	    }
   162	
   163	
   164	def _pdf_observation_event(path: str = "report.pdf") -> dict[str, Any]:
   165	    """A successful validate_pdf-worthy ObservationEvent (audio_overview mp3_path)."""
   166	    # We use a pdf path directly via a deliverable event
   167	    return _deliverable_files_event(path)
   168	
   169	
   170	def _audio_observation_event(path: str = "overview.mp3") -> dict[str, Any]:
   171	    """A successful audio_overview ObservationEvent."""
   172	    return {
   173	        "kind": "observation",
   174	        "tool_result": {
   175	            "success": True,
   176	            "tool_name": "audio_overview",
   177	            "call_id": "call_004",
   178	            "content": "Audio generated.",
   179	            "structured": {"mp3_path": path, "transcript_path": "overview_transcript.md"},
   180	        },
   181	    }
   182	
   183	
   184	# ---------------------------------------------------------------------------
   185	# Tests: _locate_deliverables
   186	# ---------------------------------------------------------------------------
   187	
   188	
   189	def test_locate_deliverables_slides_generate() -> None:
   190	    events = [_slides_observation_event("deck.pptx")]
   191	    deliverables = _locate_deliverables(events)
   192	    assert len(deliverables) == 1
   193	    assert deliverables[0]["path"] == "deck.pptx"
   194	    assert deliverables[0]["tool"] == "slides_generate"
   195	
   196	
   197	def test_locate_deliverables_deliverable_event() -> None:
   198	    events = [_deliverable_files_event("output.pptx")]
   199	    deliverables = _locate_deliverables(events)
   200	    assert any(d["path"] == "output.pptx" for d in deliverables)
   201	
   202	
   203	def test_locate_deliverables_audio_overview() -> None:
   204	    events = [_audio_observation_event("overview.mp3")]
   205	    deliverables = _locate_deliverables(events)
   206	    paths = [d["path"] for d in deliverables]
   207	    assert "overview.mp3" in paths
   208	    assert "overview_transcript.md" in paths
   209	
   210	
   211	def test_locate_deliverables_image_generate() -> None:
   212	    events = [_image_observation_event()]
   213	    deliverables = _locate_deliverables(events)
   214	    assert any(d["path"] == "slide_image.png" for d in deliverables)
   215	
   216	
   217	def test_locate_deliverables_skips_failed_observations() -> None:
   218	    failed_evt: dict[str, Any] = {
   219	        "kind": "observation",
   220	        "tool_result": {
   221	            "success": False,
   222	            "tool_name": "slides_generate",
   223	            "call_id": "c1",
   224	            "structured": {"filename": "should_not_appear.pptx"},
   225	        },
   226	    }
   227	    deliverables = _locate_deliverables([failed_evt])
   228	    assert deliverables == []
   229	
   230	
   231	def test_locate_deliverables_empty_events() -> None:
   232	    assert _locate_deliverables([]) == []
   233	
   234	
   235	# ---------------------------------------------------------------------------
   236	# Tests: _run_validators (forbid checks, no file IO)
   237	# ---------------------------------------------------------------------------
   238	
   239	
   240	def test_validator_no_problems_on_pptx() -> None:
   241	    scenario = Scenario(
   242	        id="test",
   243	        prompt="p",
   244	        forbid=["raw_html_default", "procedural_image_provider"],
   245	    )
   246	    events = [_slides_observation_event("deck.pptx")]
   247	    deliverables = _locate_deliverables(events)
   248	    problems = _run_validators(scenario, deliverables, events)
   249	    assert problems == []
   250	
   251	
   252	def test_validator_raw_html_default_fires_when_html_only() -> None:
   253	    scenario = Scenario(id="test", prompt="p", forbid=["raw_html_default"])
   254	    events = [_html_observation_event()]
   255	    deliverables = _locate_deliverables(events)
   256	    problems = _run_validators(scenario, deliverables, events)
   257	    assert any("raw_html_default" in p for p in problems)
   258	
   259	
   260	def test_validator_raw_html_default_ok_when_pptx_also_present() -> None:

exec
/bin/bash -lc "nl -ba packages/agent-server/tests/test_disco_verify_runner.py | sed -n '260,560p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   260	def test_validator_raw_html_default_ok_when_pptx_also_present() -> None:
   261	    """HTML deliverable is NOT a problem when a non-HTML file was also produced."""
   262	    scenario = Scenario(id="test", prompt="p", forbid=["raw_html_default"])
   263	    events = [_html_observation_event(), _slides_observation_event("deck.pptx")]
   264	    deliverables = _locate_deliverables(events)
   265	    problems = _run_validators(scenario, deliverables, events)
   266	    assert not any("raw_html_default" in p for p in problems)
   267	
   268	
   269	def test_validator_procedural_image_fires() -> None:
   270	    scenario = Scenario(
   271	        id="test", prompt="p", forbid=["procedural_image_provider"]
   272	    )
   273	    events = [_image_observation_event(provider="procedural")]
   274	    deliverables = _locate_deliverables(events)
   275	    problems = _run_validators(scenario, deliverables, events)
   276	    assert any("procedural_image_provider" in p for p in problems)
   277	
   278	
   279	def test_validator_procedural_image_ok_for_real_provider() -> None:
   280	    scenario = Scenario(
   281	        id="test", prompt="p", forbid=["procedural_image_provider"]
   282	    )
   283	    events = [_image_observation_event(provider="openrouter")]
   284	    deliverables = _locate_deliverables(events)
   285	    problems = _run_validators(scenario, deliverables, events)
   286	    assert not any("procedural" in p for p in problems)
   287	
   288	
   289	def test_validator_file_validators_called_for_existing_file(
   290	    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
   291	) -> None:
   292	    """When a pdf path actually exists, validate_pdf is called."""
   293	    # create a fake PDF file so the path.exists() guard passes
   294	    pdf_path = tmp_path / "report.pdf"
   295	    pdf_path.write_bytes(b"%PDF-1.4 fake")
   296	
   297	    # monkeypatch validate_pdf to track the call and return no problems
   298	    called_with: list[str] = []
   299	
   300	    import disco.tools.verify.artifact_validators as _av_mod
   301	    original = _av_mod.validate_pdf
   302	
   303	    def fake_validate_pdf(path: str) -> list[str]:
   304	        called_with.append(path)
   305	        return []
   306	
   307	    monkeypatch.setattr(_av_mod, "validate_pdf", fake_validate_pdf)
   308	    try:
   309	        scenario = Scenario(id="test", prompt="p", forbid=[])
   310	        deliverables = [{"path": str(pdf_path), "kind": "file", "tool": "deliverable"}]
   311	        problems = _run_validators(scenario, deliverables, [])
   312	        assert called_with == [str(pdf_path)]
   313	        assert problems == []
   314	    finally:
   315	        monkeypatch.setattr(_av_mod, "validate_pdf", original)
   316	
   317	
   318	def test_validator_file_validators_report_problems(
   319	    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
   320	) -> None:
   321	    """validate_pdf problems flow into the result's validator_problems list."""
   322	    pdf_path = tmp_path / "bad.pdf"
   323	    pdf_path.write_bytes(b"not a real pdf")
   324	
   325	    import disco.tools.verify.artifact_validators as _av_mod
   326	
   327	    monkeypatch.setattr(_av_mod, "validate_pdf", lambda p: ["pdf has 0 pages"])
   328	
   329	    scenario = Scenario(id="test", prompt="p", forbid=[])
   330	    deliverables = [{"path": str(pdf_path), "kind": "file", "tool": "deliverable"}]
   331	    problems = _run_validators(scenario, deliverables, [])
   332	    assert problems == ["pdf has 0 pages"]
   333	
   334	
   335	# ---------------------------------------------------------------------------
   336	# Tests: run_scenario end-to-end (fake client, no live server)
   337	# ---------------------------------------------------------------------------
   338	
   339	
   340	async def test_run_scenario_happy_path_writes_dossier(tmp_path: Path) -> None:
   341	    """Scenario finishes successfully: deliverables located, passed=True, dossier written."""
   342	    client = FakeVerifyClient(
   343	        cid="conv_test001",
   344	        final_state={"execution_status": "FINISHED"},
   345	        events=[
   346	            _slides_observation_event("deck.pptx"),
   347	            _deliverable_files_event("deck.pptx"),
   348	        ],
   349	    )
   350	    scenario = slides_from_research_report
   351	
   352	    result = await run_scenario(
   353	        scenario,
   354	        dossier_base=tmp_path / "dossiers",
   355	        _client=client,
   356	    )
   357	
   358	    assert result.scenario_id == scenario.id
   359	    assert result.terminal_status == "FINISHED"
   360	    assert len(result.deliverables) >= 1
   361	    assert result.passed
   362	    assert result.validator_problems == []
   363	
   364	    # dossier written
   365	    dossier = Path(result.dossier_path)
   366	    assert dossier.exists()
   367	    assert (dossier / "result.json").exists()
   368	    assert (dossier / "events.jsonl").exists()
   369	    assert (dossier / "state.json").exists()
   370	    # trace.json absent (no trace returned)
   371	    assert not (dossier / "trace.json").exists()
   372	
   373	    # result.json is valid JSON containing scenario id and result
   374	    data = json.loads((dossier / "result.json").read_text())
   375	    assert data["scenario"]["id"] == scenario.id
   376	    assert data["result"]["passed"] is True
   377	    assert data["conversation_id"] == "conv_test001"
   378	
   379	
   380	async def test_run_scenario_all_client_methods_called(tmp_path: Path) -> None:
   381	    """run_scenario calls all 7 AbstractVerifyClient methods."""
   382	    client = FakeVerifyClient(
   383	        final_state={"execution_status": "FINISHED"},
   384	        events=[],
   385	    )
   386	    await run_scenario(
   387	        Scenario(id="coverage", prompt="hello"),
   388	        dossier_base=tmp_path / "d",
   389	        _client=client,
   390	    )
   391	    for method in (
   392	        "create_conversation",
   393	        "run_ws_exchange",
   394	        "poll_until_terminal",
   395	        "get_events",
   396	        "get_state",
   397	        "get_trace",
   398	        "get_manifest",
   399	    ):
   400	        assert method in client.calls, f"Expected {method!r} to be called"
   401	
   402	
   403	async def test_run_scenario_ws_prompt_forwarded(tmp_path: Path) -> None:
   404	    """The scenario's prompt is forwarded to run_ws_exchange."""
   405	    client = FakeVerifyClient(final_state={"execution_status": "FINISHED"})
   406	    scenario = Scenario(id="prompt_check", prompt="Make me slides please")
   407	    await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
   408	    assert client.ws_prompt == "Make me slides please"
   409	
   410	
   411	async def test_run_scenario_approve_plan_forwarded(tmp_path: Path) -> None:
   412	    """approve_plan=True is forwarded to run_ws_exchange."""
   413	    client = FakeVerifyClient(final_state={"execution_status": "FINISHED"})
   414	    scenario = Scenario(id="approve_test", prompt="Build something", approve_plan=True)
   415	    await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
   416	    assert client.ws_approve_plan is True
   417	
   418	
   419	async def test_run_scenario_failed_when_forbid_fired(tmp_path: Path) -> None:
   420	    """passed=False when a forbid check fires."""
   421	    client = FakeVerifyClient(
   422	        final_state={"execution_status": "FINISHED"},
   423	        events=[_html_observation_event()],  # html only → raw_html_default fires
   424	    )
   425	    scenario = Scenario(
   426	        id="fail_test",
   427	        prompt="Make slides",
   428	        forbid=["raw_html_default"],
   429	        expect={"terminal_status": "FINISHED"},
   430	    )
   431	    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
   432	    assert not result.passed
   433	    assert any("raw_html_default" in p for p in result.validator_problems)
   434	
   435	
   436	async def test_run_scenario_failed_when_status_mismatch(tmp_path: Path) -> None:
   437	    """passed=False when terminal_status != expected terminal_status."""
   438	    client = FakeVerifyClient(
   439	        final_state={"execution_status": "ERROR"},
   440	        events=[],
   441	    )
   442	    scenario = Scenario(
   443	        id="status_mismatch",
   444	        prompt="do something",
   445	        expect={"terminal_status": "FINISHED"},
   446	    )
   447	    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
   448	    assert not result.passed
   449	    assert result.terminal_status == "ERROR"
   450	
   451	
   452	async def test_run_scenario_missing_file_sandbox_error(tmp_path: Path) -> None:
   453	    """The missing_file_sandbox_error scenario passes when server returns ERROR."""
   454	    client = FakeVerifyClient(
   455	        final_state={"execution_status": "ERROR"},
   456	        events=[
   457	            {
   458	                "kind": "agent_error",
   459	                "error": "SandboxFileNotFoundError: /nonexistent_verifytest_file_99999.txt",
   460	            }
   461	        ],
   462	    )
   463	    result = await run_scenario(
   464	        missing_file_sandbox_error,
   465	        dossier_base=tmp_path / "d",
   466	        _client=client,
   467	    )
   468	    assert result.passed
   469	    assert result.terminal_status == "ERROR"
   470	    assert result.deliverables == []
   471	
   472	
   473	async def test_run_scenario_dossier_has_trace_when_available(tmp_path: Path) -> None:
   474	    """trace.json is written when get_trace returns a trace dict."""
   475	    client = FakeVerifyClient(
   476	        final_state={"execution_status": "FINISHED"},
   477	        trace={"spans": [{"id": "span1", "model": "gpt-5"}]},
   478	    )
   479	    result = await run_scenario(
   480	        Scenario(id="trace_test", prompt="hi"),
   481	        dossier_base=tmp_path / "d",
   482	        _client=client,
   483	    )
   484	    dossier = Path(result.dossier_path)
   485	    assert (dossier / "trace.json").exists()
   486	    trace_data = json.loads((dossier / "trace.json").read_text())
   487	    assert "spans" in trace_data
   488	
   489	
   490	async def test_run_scenario_dossier_events_jsonl_redacted(tmp_path: Path) -> None:
   491	    """Sensitive keys are redacted in the events.jsonl output."""
   492	    client = FakeVerifyClient(
   493	        final_state={"execution_status": "FINISHED"},
   494	        events=[
   495	            {"kind": "message", "content": "hello", "api_key": "sk-super-secret"}
   496	        ],
   497	    )
   498	    result = await run_scenario(
   499	        Scenario(id="redact_test", prompt="hi"),
   500	        dossier_base=tmp_path / "d",
   501	        _client=client,
   502	    )
   503	    dossier = Path(result.dossier_path)
   504	    raw = (dossier / "events.jsonl").read_text()
   505	    assert "sk-super-secret" not in raw
   506	    assert "***REDACTED***" in raw
   507	
   508	
   509	async def test_run_scenario_passed_when_no_expect_status(tmp_path: Path) -> None:
   510	    """When expect has no terminal_status, any terminal status is acceptable."""
   511	    client = FakeVerifyClient(
   512	        final_state={"execution_status": "STUCK"},
   513	        events=[],
   514	    )
   515	    scenario = Scenario(id="no_expect", prompt="hi", expect={}, forbid=[])
   516	    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
   517	    # no forbid violations + no expected status → passed
   518	    assert result.passed
   519	
   520	
   521	# ---------------------------------------------------------------------------
   522	# Tests: exported scenario objects
   523	# ---------------------------------------------------------------------------
   524	
   525	
   526	def test_slides_scenario_has_expected_shape() -> None:
   527	    s = slides_from_research_report
   528	    assert s.surface == "agent"
   529	    assert "Make slides" in s.prompt
   530	    assert "raw_html_default" in s.forbid
   531	    assert "procedural_image_provider" in s.forbid
   532	    assert s.expect.get("terminal_status") == "FINISHED"
   533	
   534	
   535	def test_missing_file_scenario_has_expected_shape() -> None:
   536	    s = missing_file_sandbox_error
   537	    assert s.surface == "build"
   538	    assert s.expect.get("terminal_status") == "ERROR"
   539	    assert s.forbid == []
   540	
   541	
   542	def test_verify_result_is_pydantic() -> None:
   543	    r = VerifyResult(
   544	        scenario_id="x",
   545	        terminal_status="FINISHED",
   546	        deliverables=[],
   547	        validator_problems=[],
   548	        passed=True,
   549	        dossier_path="/tmp/dossier",
   550	    )
   551	    assert r.model_dump()["passed"] is True

exec
/bin/bash -lc "nl -ba packages/agent-server/src/disco/agent_server/verify/scenarios_run.py | sed -n '1,220p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""W13 — run the disco-verify live scenarios against a running stack and write dossiers.
     2	
     3	Entry point::
     4	
     5	    python -m disco.agent_server.verify.scenarios_run [--agent-base URL] [--only ID ...]
     6	
     7	Each scenario is driven through the REAL HTTP/WS API (``run_scenario``), waits for a terminal
     8	status, locates the delivered artifacts, runs the W14 validators, and writes a redacted
     9	dossier under ``test-record/disco-verify/``. Exit code is non-zero if any scenario fails — so
    10	this doubles as the nightly-VM gate's W13 step. The stack (app-server + agent-server + a live
    11	driver model) must be reachable; on the VM 201 evidence host that is via the reverse tunnel.
    12	"""
    13	
    14	from __future__ import annotations
    15	
    16	import argparse
    17	import asyncio
    18	import sys
    19	
    20	from .runner import run_scenario
    21	from .scenarios import missing_file_sandbox_error, slides_from_research_report
    22	
    23	_SCENARIOS = {
    24	    "slides_from_research_report": slides_from_research_report,
    25	    "missing_file_sandbox_error": missing_file_sandbox_error,
    26	}
    27	
    28	
    29	async def _run(agent_base: str, only: list[str]) -> int:
    30	    selected = [s for k, s in _SCENARIOS.items() if not only or k in only]
    31	    failures = 0
    32	    for scenario in selected:
    33	        result = await run_scenario(scenario, agent_base=agent_base)
    34	        status = "PASS" if result.passed else "FAIL"
    35	        print(
    36	            f"[{status}] {scenario.id}: terminal={result.terminal_status} "
    37	            f"problems={result.validator_problems} dossier={result.dossier_path}"
    38	        )
    39	        if not result.passed:
    40	            failures += 1
    41	    print(f"\n{len(selected) - failures}/{len(selected)} scenarios passed")
    42	    return 1 if failures else 0
    43	
    44	
    45	def main() -> None:
    46	    ap = argparse.ArgumentParser(description="Run disco-verify live scenarios (W13).")
    47	    ap.add_argument("--agent-base", default="http://127.0.0.1:8000")
    48	    ap.add_argument("--only", nargs="*", default=[], help="scenario id(s) to run")
    49	    args = ap.parse_args()
    50	    sys.exit(asyncio.run(_run(args.agent_base, args.only)))
    51	
    52	
    53	if __name__ == "__main__":
    54	    main()

codex
A bigger issue is now clear: `disco-verify` does not fetch delivered files from the app. It validates only paths that happen to exist on the runner’s local filesystem, and it never calls the deck-file validator at all. That would miss the exact procedural-image deck regression in live scenarios.
exec
/bin/bash -lc "nl -ba packages/agent-server/src/disco/agent_server/routes/_common.py | sed -n '150,210p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   150	    third-party events, so reviving them would feed an attacker's content to the
   151	    agent with this instance's tools/credentials. Every loop-kicking / mutating
   152	    endpoint rejects them at the server EDGE (409); the UI hiding the affordance
   153	    is defense-in-depth, never the boundary."""
   154	    if store.conversation_origin(conversation_id) == "imported":
   155	        raise HTTPException(status_code=409, detail={"reason": "imported_read_only"})
   156	
   157	
   158	async def _declared_artifacts(store: SqliteEventStore, conversation_id: str) -> set[str]:
   159	    """The set of workspace-relative paths this conversation EMITTED as results.
   160	    The download jail: only emitted artifacts are reachable, never arbitrary
   161	    workspace paths (user code, secrets, uploads).
   162	
   163	    Sources collected per tool:
   164	      sheet_generate   — structured["filename"]
   165	      slides_generate  — structured["filename"]  (same key)
   166	      image_generate   — structured["path"]
   167	      audio_overview   — structured["mp3_path"] + structured["transcript_path"]
   168	      DeliverableEvent artifact_kind="files" — e.path
   169	    """
   170	    out: set[str] = set()
   171	    with contextlib.suppress(Exception):
   172	        for e in await store.get_events(conversation_id):
   173	            if (
   174	                isinstance(e, ObservationEvent)
   175	                and e.tool_result.success
   176	                and e.tool_result.structured
   177	            ):
   178	                tn = e.tool_result.tool_name
   179	                s = e.tool_result.structured
   180	                if tn in ("sheet_generate", "slides_generate"):
   181	                    fn = s.get("filename")
   182	                    if isinstance(fn, str) and fn:
   183	                        out.add(posixpath.normpath(fn))
   184	                    # A2.0: the editable AuthoredDeck sidecar (slides C2 path) is a
   185	                    # reachable artifact so the deck-editor route can read it back.
   186	                    es = s.get("editable_source")
   187	                    if isinstance(es, str) and es:
   188	                        out.add(posixpath.normpath(es))
   189	                elif tn == "image_generate":
   190	                    p = s.get("path")
   191	                    if isinstance(p, str) and p:
   192	                        out.add(posixpath.normpath(p))
   193	                elif tn == "audio_overview":
   194	                    for key in ("mp3_path", "transcript_path"):
   195	                        p = s.get(key)
   196	                        if isinstance(p, str) and p:
   197	                            out.add(posixpath.normpath(p))
   198	            elif isinstance(e, DeliverableEvent) and e.artifact_kind == "files":
   199	                out.add(posixpath.normpath(e.path))
   200	    return out
   201	
   202	
   203	def make_preview_upstream_resolver(runtime: ConversationRuntime | None):
   204	    """DC-01: build the `{cid8}-{port}.localhost → sandbox upstream` resolver the
   205	    HostPreviewProxyMiddleware calls. Returned closure captures `runtime` (None in
   206	    wire-only tests → always resolves to None)."""
   207	
   208	    async def _preview_upstream_resolver(cid8: str, port: int) -> str | None:
   209	        if runtime is None:
   210	            return None

exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/anatomy.py | sed -n '1,100p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""Tool anatomy — tool-sandbox-contract.md §2/§3.
     2	
     3	Every tool is three separable things (BoD §10.1): a schema (what the model sees
     4	+ how arguments are validated), a validator/repair wrapper (executor.py), and an
     5	executor (`Tool.run`). This separation lets the same tool run in different
     6	sandbox backends and keeps validation uniform.
     7	
     8	[CONTRACT DEVIATION — flagged for a contract patch] §2 types `ToolDef.needs` as
     9	`frozenset[Requirement]` and references NETWORK/FILESYSTEM, but the router
    10	contract's `Requirement` enum holds only *model* capabilities (VISION/
    11	LONG_CONTEXT/TOOL_CALLING/JSON_MODE). Tool `needs` are *execution-environment*
    12	capabilities — a distinct concept — so we introduce a `Capability` enum here
    13	rather than overload `Requirement`. The Tool/Sandbox contract §2 should be
    14	patched to reference `Capability`.
    15	"""
    16	
    17	from __future__ import annotations
    18	
    19	from enum import Enum
    20	from typing import Any, Literal, Protocol, runtime_checkable
    21	
    22	from disco.core import SecurityRisk
    23	from disco.core.llm import ToolSpec
    24	from pydantic import BaseModel, ConfigDict, Field
    25	
    26	
    27	class Capability(str, Enum):
    28	    """Execution-environment capabilities a tool requires (distinct from the
    29	    router's model `Requirement`). Drives scoping (§8) and the sandbox spec
    30	    (§5/§7) — anything a tool doesn't declare, its instance is denied.
    31	
    32	    Members match tool-sandbox-contract.md v1.1 §2."""
    33	
    34	    NETWORK = "network"  # raw egress (browser to arbitrary sites, deploy preview)
    35	    FILESYSTEM = "filesystem"  # workspace file access
    36	    CODE_EXEC = "code_exec"  # runs arbitrary code
    37	    SHELL = "shell"  # runs shell commands
    38	    DISPLAY = "display"  # a GUI/display (browser noVNC view)
    39	
    40	
    41	class ToolContext(BaseModel):
    42	    """[CONTRACT] What a tool's run() receives: the sandbox handle, the
    43	    workspace, scoped capabilities, and the per-call timeout — but NEVER raw
    44	    secrets. A tool needing an external credential receives a scoped, short-lived
    45	    CAPABILITY (a mediated handle), not the secret itself (§6)."""
    46	
    47	    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)
    48	    sandbox: Any | None  # SandboxInstance | None (None for in_process tools)
    49	    sessions: Any | None = None  # ShellSessionManager | None (BP-01)
    50	    kernel: Any | None = None  # KernelSession | None (BP-08)
    51	    workspace_path: str
    52	    timeout_s: int
    53	    capabilities: Any  # CapabilitySet (scoped grants; secrets.py)
    54	    owner_id: str
    55	    conversation_id: str
    56	    assist: bool = False
    57	    # ROOT-5: the conversation's EFFECTIVE driver endpoint (override-aware) for
    58	    # LLM-using tools (e.g. slides_generate), as (base_url, model_id, api_key_env).
    59	    # NEVER the resolved key — only the env-var NAME (§6, no raw secrets in ctx);
    60	    # the tool resolves the secret itself. None ⇒ the tool falls back to the global
    61	    # AGENT_DRIVER from ConfigStore (behavior-preserving for non-build executors).
    62	    driver_llm: tuple[str, str, str | None] | None = None
    63	
    64	
    65	class ToolOutcome(BaseModel):
    66	    """What a tool's run() returns. The executor maps this into the event
    67	    contract's ToolResult — separating them lets tools return rich results
    68	    without knowing the event schema."""
    69	
    70	    model_config = ConfigDict(frozen=True)
    71	    success: bool
    72	    content: str  # human/LLM-readable result text
    73	    structured: dict[str, Any] | None = None
    74	    error: str | None = None  # populated iff not success
    75	    # Artifacts produced in the workspace (paths), surfaced to the UI's
    76	    # Inspector/Assets view (BoD §13.4/§13.5).
    77	    artifacts: list[str] = Field(default_factory=list)
    78	
    79	
    80	class ToolDef(BaseModel):
    81	    """[CONTRACT] The static definition of one tool. `to_spec()` produces the
    82	    provider-neutral ToolSpec the model sees (router contract); `args_model`
    83	    validates the model's arguments before execution — the SAME object, no
    84	    drifting second copy (§3)."""
    85	
    86	    model_config = ConfigDict(frozen=True)
    87	    name: str
    88	    description: str
    89	    args_model: type[BaseModel]  # validates ToolCall.arguments
    90	    needs: frozenset[Capability] = frozenset()
    91	    # Coarse static risk hint; the SecurityAnalyzer may refine per-call (§6.1).
    92	    # None => the analyzer decides entirely.
    93	    base_risk: SecurityRisk | None = None
    94	    runs_in: Literal["sandbox", "in_process"] = "sandbox"
    95	    # Planner-safety flag (Claude-Code plan-mode parity). True iff the tool only
    96	    # OBSERVES — it makes no durable change to the workspace, sandbox, or the
    97	    # outside world (reads/searches/the plan-submission signal). The loop shows
    98	    # ONLY read-only tools to the PLANNING agent, so a plan is drafted from
    99	    # context-gathering alone and writes/exec are off the table until approval.
   100	    # FAIL-SAFE DEFAULT (False): an unmarked tool is treated as mutating and is

exec
/bin/bash -lc "rg -n \"artifacts=|structured=|ToolOutcome\\(\" packages/tools/src/disco/tools/builtin | head -n 220" in /var/home/dylan/projects/disco
 succeeded in 0ms:
packages/tools/src/disco/tools/builtin/slides.py:366:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/slides.py:417:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/slides.py:421:                artifacts=outcome.artifacts,
packages/tools/src/disco/tools/builtin/slides.py:422:                structured=outcome.structured,
packages/tools/src/disco/tools/builtin/slides.py:425:        return ToolOutcome(
packages/tools/src/disco/tools/builtin/slides.py:449:            return ToolOutcome(success=False, content="No sandbox available to write the slide deck.")
packages/tools/src/disco/tools/builtin/slides.py:461:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/slides.py:468:                artifacts=[out_filename],
packages/tools/src/disco/tools/builtin/slides.py:469:                structured={
packages/tools/src/disco/tools/builtin/slides.py:505:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/slides.py:512:                artifacts=artifacts,
packages/tools/src/disco/tools/builtin/slides.py:513:                structured={
packages/tools/src/disco/tools/builtin/slides.py:532:                return ToolOutcome(
packages/tools/src/disco/tools/builtin/slides.py:537:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/slides.py:544:                artifacts=[out_filename, pptx_name],
packages/tools/src/disco/tools/builtin/slides.py:545:                structured={
packages/tools/src/disco/tools/builtin/slides.py:557:        return ToolOutcome(
packages/tools/src/disco/tools/builtin/slides.py:583:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/slides.py:620:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/slides.py:637:        return ToolOutcome(
packages/tools/src/disco/tools/builtin/slides.py:644:            artifacts=[out_filename],
packages/tools/src/disco/tools/builtin/slides.py:645:            structured={
packages/tools/src/disco/tools/builtin/slides.py:671:        return ToolOutcome(
packages/tools/src/disco/tools/builtin/slides.py:678:            artifacts=[out_filename],
packages/tools/src/disco/tools/builtin/slides.py:679:            structured={
packages/tools/src/disco/tools/builtin/files.py:202:    return ToolOutcome(
packages/tools/src/disco/tools/builtin/files.py:259:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/files.py:297:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/files.py:321:        return ToolOutcome(success=True, content=header + "\n".join(out))
packages/tools/src/disco/tools/builtin/files.py:363:                return ToolOutcome(
packages/tools/src/disco/tools/builtin/files.py:382:        return ToolOutcome(
packages/tools/src/disco/tools/builtin/files.py:383:            success=True, content=f"wrote {len(raw)} bytes to {args.path}", artifacts=[args.path]
packages/tools/src/disco/tools/builtin/files.py:426:        return ToolOutcome(
packages/tools/src/disco/tools/builtin/files.py:429:            artifacts=[args.path],
packages/tools/src/disco/tools/builtin/files.py:450:        return ToolOutcome(
packages/tools/src/disco/tools/builtin/files.py:453:            structured={"path": args.path, "entries": entries},
packages/tools/src/disco/tools/builtin/files.py:528:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/files.py:540:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/files.py:556:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/files.py:571:        return ToolOutcome(
packages/tools/src/disco/tools/builtin/files.py:572:            success=True, content=f"edited {args.path} ({how})", artifacts=[args.path]
packages/tools/src/disco/tools/builtin/files.py:608:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/files.py:621:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/files.py:645:        return ToolOutcome(
packages/tools/src/disco/tools/builtin/files.py:649:            artifacts=[args.path],
packages/tools/src/disco/tools/builtin/files.py:683:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/files.py:699:        return ToolOutcome(
packages/tools/src/disco/tools/builtin/files.py:702:            artifacts=[args.path],
packages/tools/src/disco/tools/builtin/files.py:765:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/files.py:788:                    return ToolOutcome(
packages/tools/src/disco/tools/builtin/files.py:791:                        artifacts=[args.path],
packages/tools/src/disco/tools/builtin/files.py:793:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/files.py:806:        return ToolOutcome(
packages/tools/src/disco/tools/builtin/files.py:809:            artifacts=[args.path],
packages/tools/src/disco/tools/builtin/plan.py:93:        return ToolOutcome(success=True, content="plan received")
packages/tools/src/disco/tools/builtin/plan.py:118:        return ToolOutcome(
packages/tools/src/disco/tools/builtin/plan.py:121:            structured={"index": args.index, "state": args.state},
packages/tools/src/disco/tools/builtin/plan.py:173:        return ToolOutcome(
packages/tools/src/disco/tools/builtin/plan.py:176:            structured={"steps": [s.model_dump() for s in args.steps]},
packages/tools/src/disco/tools/builtin/image_gen.py:881:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/image_gen.py:904:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/image_gen.py:914:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/image_gen.py:946:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/image_gen.py:998:        return ToolOutcome(
packages/tools/src/disco/tools/builtin/image_gen.py:1005:            artifacts=[out_path],
packages/tools/src/disco/tools/builtin/image_gen.py:1006:            structured={
packages/tools/src/disco/tools/builtin/_deck_patch.py:294:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/_deck_patch.py:303:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/_deck_patch.py:313:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/_deck_patch.py:323:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/_deck_patch.py:362:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/_deck_patch.py:384:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/_deck_patch.py:397:        return ToolOutcome(
packages/tools/src/disco/tools/builtin/_deck_patch.py:400:            artifacts=written,
packages/tools/src/disco/tools/builtin/_deck_patch.py:401:            structured={
packages/tools/src/disco/tools/builtin/browser.py:250:                return ToolOutcome(
packages/tools/src/disco/tools/builtin/browser.py:261:                return ToolOutcome(
packages/tools/src/disco/tools/builtin/browser.py:265:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/browser.py:268:                structured=data,
packages/tools/src/disco/tools/builtin/browser.py:271:            return ToolOutcome(success=False, content="", error=f"browser tool error: {e}")
packages/tools/src/disco/tools/builtin/audio_overview.py:325:            return None, ToolOutcome(
packages/tools/src/disco/tools/builtin/audio_overview.py:375:            return None, ToolOutcome(
packages/tools/src/disco/tools/builtin/audio_overview.py:402:                return None, ToolOutcome(
packages/tools/src/disco/tools/builtin/audio_overview.py:414:                return None, ToolOutcome(
packages/tools/src/disco/tools/builtin/audio_overview.py:444:                return None, ToolOutcome(
packages/tools/src/disco/tools/builtin/audio_overview.py:454:                return None, ToolOutcome(
packages/tools/src/disco/tools/builtin/audio_overview.py:463:                return None, ToolOutcome(
packages/tools/src/disco/tools/builtin/audio_overview.py:501:        return ToolOutcome(
packages/tools/src/disco/tools/builtin/audio_overview.py:510:            artifacts=[mp3_path, transcript_path],
packages/tools/src/disco/tools/builtin/audio_overview.py:511:            structured={
packages/tools/src/disco/tools/builtin/sheets.py:256:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/sheets.py:313:        return ToolOutcome(
packages/tools/src/disco/tools/builtin/sheets.py:320:            artifacts=[args.filename],
packages/tools/src/disco/tools/builtin/sheets.py:321:            structured={
packages/tools/src/disco/tools/builtin/shell_sessions.py:15:    return ToolOutcome(success=False, content=msg, error=msg)
packages/tools/src/disco/tools/builtin/shell_sessions.py:53:            return ToolOutcome(success=True, content=content.strip())
packages/tools/src/disco/tools/builtin/shell_sessions.py:84:            return ToolOutcome(success=True, content=content.strip())
packages/tools/src/disco/tools/builtin/shell_sessions.py:111:            return ToolOutcome(success=True, content=content.strip())
packages/tools/src/disco/tools/builtin/shell_sessions.py:137:            return ToolOutcome(success=True, content=f"Wrote to session '{args.session}'.")
packages/tools/src/disco/tools/builtin/shell_sessions.py:164:            return ToolOutcome(success=True, content=res)
packages/tools/src/disco/tools/builtin/server.py:76:        return ToolOutcome(
packages/tools/src/disco/tools/builtin/subagent.py:124:        return ToolOutcome(
packages/tools/src/disco/tools/builtin/think.py:55:        return ToolOutcome(success=True, content="thought acknowledged")
packages/tools/src/disco/tools/builtin/system.py:67:    return ToolOutcome(
packages/tools/src/disco/tools/builtin/system.py:70:        structured={
packages/tools/src/disco/tools/builtin/system.py:160:            return ToolOutcome(
packages/tools/src/disco/tools/builtin/system.py:163:                structured=res.__dict__,
packages/tools/src/disco/tools/builtin/system.py:164:                artifacts=res.images
packages/tools/src/disco/tools/builtin/retrieval.py:91:        return ToolOutcome(
packages/tools/src/disco/tools/builtin/retrieval.py:94:            structured={"results": results} if isinstance(results, list | dict) else None,
packages/tools/src/disco/tools/builtin/retrieval.py:116:        return ToolOutcome(
packages/tools/src/disco/tools/builtin/retrieval.py:119:            structured={"doc": doc} if isinstance(doc, dict) else None,

exec
/bin/bash -lc "nl -ba packages/agent-server/src/disco/agent_server/routes/files.py | sed -n '222,265p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   222	    @router.get("/conversations/{conversation_id}/artifacts/{path:path}")
   223	    async def artifact_file(
   224	        conversation_id: str,
   225	        path: str,
   226	        inline: bool = Query(default=False),
   227	    ) -> Response:
   228	        """Download a generated artifact by its workspace-relative path.
   229	        Jails: (1) the path must have been DECLARED as an artifact in the event log;
   230	        (2) extension allowlist (_ARTIFACT_TYPES); (3) traversal-normalized + host-path
   231	        resolve-jail.  Reads the live sandbox first, falling back to the host
   232	        ProjectStore snapshot so a FINISHED run (no live session) still serves.
   233	        404 uniformly on any rejection (no probe).
   234	
   235	        C5: ?inline=true returns HTML with Content-Disposition: inline + a strict
   236	        Content-Security-Policy (sandbox; no allow-same-origin) so an HTML artifact
   237	        can be embedded in an iframe without granting same-origin access to this
   238	        instance's APIs.  Only .html is allowed in inline mode — all other extensions
   239	        still 404 on ?inline=true so the inline allowlist stays minimal.
   240	        The default (no param) is unchanged: always attachment."""
   241	        norm = posixpath.normpath(path)
   242	        if posixpath.isabs(norm) or norm.startswith(".."):
   243	            raise HTTPException(status_code=404)
   244	        _, ext = posixpath.splitext(norm)
   245	        media_type = _ARTIFACT_TYPES.get(ext.lower())
   246	        if media_type is None:
   247	            raise HTTPException(status_code=404)
   248	        # C5: inline mode is restricted to .html only — no other type may be inlined.
   249	        if inline and ext.lower() != ".html":
   250	            raise HTTPException(status_code=404)
   251	        if runtime is None:
   252	            raise HTTPException(status_code=404)
   253	        if norm not in await _declared_artifacts(store, conversation_id):
   254	            raise HTTPException(status_code=404)
   255	
   256	        data = await _read_artifact_bytes(runtime, conversation_id, norm)
   257	        if data is None:
   258	            raise HTTPException(status_code=404)
   259	        if len(data) > 50 * 1024 * 1024:  # 50 MB cap
   260	            raise HTTPException(status_code=404)
   261	
   262	        return _artifact_response(data, media_type, posixpath.basename(norm), inline)
   263	
   264	    return router
   265	

codex
The runner also ignores `ToolOutcome.artifacts`, mirroring the server artifact allowlist rather than using the actual delivered artifacts list. I’m checking the scripted-model harness next to see whether it really crosses the model/router/agent boundary or still has mock gaps.
exec
/bin/bash -lc "nl -ba packages/agent-server/tests/integration/scripted_model.py | sed -n '1,260p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""Scripted LLM provider — W8 evidence-harness integration layer.
     2	
     3	Conforms to the real ``ModelProvider`` protocol (packages/core/src/disco/core/llm/provider.py)
     4	so it wires through the REAL ``DefaultLLMRouter`` → ``RouterAgent.step()`` boundary,
     5	NOT by calling internal loop functions directly.
     6	
     7	Interface targeted:
     8	  - ``ModelProvider.name: str``
     9	  - ``ModelProvider.complete(req: CompletionRequest, *, model: str) -> CompletionResponse``
    10	  - ``ModelProvider.stream_complete(req: CompletionRequest, *, model: str)
    11	        -> AsyncIterator[StreamChunk]``
    12	  - ``ModelProvider.supports(requirement: Requirement, *, model: str) -> bool``
    13	
    14	The router calls ``provider.stream_complete(exec_req, model=entry.model_id)``
    15	(routing.py line ~413) and assembles the final ``CompletionResponse`` from the
    16	terminal ``StreamChunk.final``.  A step that carries zero ``ProposedToolCall``s
    17	produces a tool-less prose turn; the agent interprets that as finishing (for the
    18	``ResearchAgent`` / ``prose_finishes=True`` case) or as a no-op (for
    19	``BuildAgent``).  Always include an explicit ``finish`` call in the step list to
    20	terminate a build-surface run cleanly.
    21	
    22	Role filtering
    23	--------------
    24	The scripted step sequence targets the ``AGENT_DRIVER`` role only.  Background
    25	roles (``SUMMARIZER``, ``QUERY_REWRITER``, ``RAG_ANSWERER``, ``NLI_VERIFIER``)
    26	are served a silent no-op response without consuming a scripted step.  This
    27	prevents background services (e.g. the ``TitleService`` that calls the
    28	``SUMMARIZER`` role on every kick) from interfering with the scripted sequence.
    29	
    30	Usage::
    31	
    32	    from disco.core.llm import ProposedToolCall
    33	    from .scripted_model import ScriptedProvider
    34	
    35	    steps = [
    36	        ("writing output", [ProposedToolCall(tool_name="file_write",
    37	                                             arguments={"path": "out.txt",
    38	                                                        "content": "hello"})]),
    39	        ("all done",       [ProposedToolCall(tool_name="finish",
    40	                                             arguments={"summary": "done"})]),
    41	    ]
    42	    provider = ScriptedProvider(steps)
    43	"""
    44	
    45	from __future__ import annotations
    46	
    47	from typing import TYPE_CHECKING
    48	
    49	from disco.core.llm import (
    50	    CompletionResponse,
    51	    StreamChunk,
    52	    TokenUsage,
    53	)
    54	from disco.core.llm.types import ModelRole, ProposedToolCall, Requirement
    55	
    56	if TYPE_CHECKING:
    57	    from collections.abc import AsyncIterator
    58	
    59	    from disco.core.llm.types import CompletionRequest
    60	
    61	# A step is (assistant_text, list_of_tool_calls).
    62	# An empty tool-call list means a tool-less prose turn.
    63	Step = tuple[str, list[ProposedToolCall]]
    64	
    65	# Roles whose calls should NOT consume a scripted step.  Background services
    66	# (TitleService, RouterSummarizer, retrieval answerers) use these roles and
    67	# must not shift the agent-driver step index.
    68	_BACKGROUND_ROLES = frozenset(
    69	    {
    70	        ModelRole.SUMMARIZER,
    71	        ModelRole.QUERY_REWRITER,
    72	        ModelRole.RAG_ANSWERER,
    73	        ModelRole.NLI_VERIFIER,
    74	    }
    75	)
    76	
    77	
    78	def _noop_response(req: CompletionRequest, model: str) -> CompletionResponse:
    79	    """A silent, tool-free response for background-role calls that should not
    80	    consume a scripted step (summarizer, rewriter, etc.)."""
    81	    return CompletionResponse(
    82	        text="",
    83	        tool_calls=[],
    84	        usage=TokenUsage(input_tokens=0, output_tokens=0),
    85	        finish_reason="stop",
    86	        model_used=model,
    87	        request_id=getattr(req, "request_id", None),
    88	        routing=None,
    89	    )
    90	
    91	
    92	class ScriptedProvider:
    93	    """A deterministic ``ModelProvider`` that replays a caller-supplied script.
    94	
    95	    Each entry in *steps* is ``(text, tool_calls)`` where *tool_calls* is a
    96	    list of ``ProposedToolCall`` objects (or an empty list for a prose-only
    97	    turn).  Responses are served in order; the last step is repeated when the
    98	    script is exhausted (guards against loops that call the model more times
    99	    than the script accounts for, which would otherwise crash with an
   100	    ``IndexError``).
   101	
   102	    Only ``AGENT_DRIVER`` role calls consume a scripted step.  All other roles
   103	    (``SUMMARIZER``, ``QUERY_REWRITER``, etc.) receive a silent no-op response
   104	    so that background services (TitleService, condenser summarization) do not
   105	    shift the step index and corrupt the scripted sequence.
   106	
   107	    The provider emits through the real ``DefaultLLMRouter.stream_complete``
   108	    path — the same path the production model uses — so every loop invariant
   109	    (tool-call parsing, iteration counting, event emission) is exercised
   110	    without network access or a real LLM.
   111	    """
   112	
   113	    name = "scripted"
   114	
   115	    def __init__(self, steps: list[Step]) -> None:
   116	        if not steps:
   117	            raise ValueError("ScriptedProvider requires at least one step")
   118	        self._steps = list(steps)
   119	        self.call_count = 0
   120	
   121	    def _response_for(self, req: CompletionRequest, model: str) -> CompletionResponse:
   122	        """Return the next scripted response (agent-driver only), clamped to the
   123	        last step.  Non-driver roles receive a no-op response without advancing
   124	        the step counter."""
   125	        if req.profile.role in _BACKGROUND_ROLES:
   126	            return _noop_response(req, model)
   127	        idx = min(self.call_count, len(self._steps) - 1)
   128	        self.call_count += 1
   129	        text, tool_calls = self._steps[idx]
   130	        return CompletionResponse(
   131	            text=text,
   132	            tool_calls=list(tool_calls),
   133	            usage=TokenUsage(input_tokens=1, output_tokens=1),
   134	            finish_reason="tool_calls" if tool_calls else "stop",
   135	            model_used=model,
   136	            request_id=getattr(req, "request_id", None),
   137	            routing=None,  # router attaches RoutingDecision (RT1)
   138	        )
   139	
   140	    async def complete(
   141	        self, req: CompletionRequest, *, model: str
   142	    ) -> CompletionResponse:
   143	        return self._response_for(req, model)
   144	
   145	    async def stream_complete(
   146	        self, req: CompletionRequest, *, model: str
   147	    ) -> AsyncIterator[StreamChunk]:
   148	        # Yield a single terminal chunk carrying the full response.
   149	        # The router's stream_complete path (routing.py) iterates this
   150	        # generator and attaches the RoutingDecision to chunk.final.
   151	        yield StreamChunk(
   152	            done=True,
   153	            final=self._response_for(req, model),
   154	        )
   155	
   156	    def supports(self, requirement: Requirement, *, model: str) -> bool:  # noqa: ARG002
   157	        return True

exec
/bin/bash -lc "nl -ba packages/agent-server/tests/integration/harness.py | sed -n '1,260p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""W8 scripted-agent integration harness.
     2	
     3	Wires the REAL agent loop + REAL event store + REAL sandbox (ProcessSandbox)
     4	with a scripted model (``ScriptedProvider``) that replays a caller-supplied
     5	sequence of tool calls.  The scripted model emits through the same
     6	``DefaultLLMRouter`` → ``RouterAgent.step()`` boundary the production model
     7	uses — NOT by calling internal functions directly.
     8	
     9	Public API::
    10	
    11	    result = await run_scripted(steps, timeout_s=30.0)
    12	    # result["events"]       — list[Event] from the event store
    13	    # result["final_status"] — ConversationStatus (FINISHED / ERROR / …)
    14	    # result["workspace_path"] — str | None (host path to the sandbox workspace)
    15	
    16	Surface and lifecycle
    17	---------------------
    18	Conversations are created on the **build** surface with ``artifact_mode=True``
    19	(``OperatingMode.INTERACTIVE``, no plan gate).  This lets the simplest scripted
    20	sequence be ``file_write → finish`` without a preceding ``submit_plan`` /
    21	plan-approval round-trip.  Tests that need the full plan lifecycle can set
    22	``artifact_mode=False`` (default build lifecycle) and include ``submit_plan`` +
    23	``set_autonomous=True`` in their step sequence.
    24	
    25	Watchdog
    26	--------
    27	``asyncio.wait_for`` wraps the loop task with a hard *timeout_s* ceiling.  A
    28	hang (e.g. the loop stalls waiting for a model response that never arrives)
    29	raises ``asyncio.TimeoutError`` and fails the test rather than blocking
    30	forever.
    31	"""
    32	
    33	from __future__ import annotations
    34	
    35	import asyncio
    36	import uuid
    37	
    38	from disco.agent_server import ConversationRuntime
    39	from disco.core import SqliteEventStore
    40	from disco.core.events import EventSource, LLMMessage, MessageEvent
    41	from disco.core.llm import (
    42	    DefaultLLMRouter,
    43	    ModelEntry,
    44	    RouterConfig,
    45	)
    46	from disco.tools import ProcessSandboxService
    47	
    48	from .scripted_model import ScriptedProvider, Step
    49	
    50	# A minimal RouterConfig that routes every role through our scripted provider.
    51	_SCRIPTED_MODEL_ENTRY = ModelEntry(
    52	    model_id="scripted-m",
    53	    provider="scripted",
    54	    context_window=8192,
    55	)
    56	_SCRIPTED_ROUTER_CONFIG = RouterConfig(
    57	    models={"scripted-m": _SCRIPTED_MODEL_ENTRY},
    58	    default_model="scripted-m",
    59	)
    60	
    61	
    62	def _build_runtime(
    63	    store: SqliteEventStore,
    64	    steps: list[Step],
    65	) -> ConversationRuntime:
    66	    """Build a ConversationRuntime wired with a scripted model and a real
    67	    ProcessSandbox.  No network calls; no real LLM.  The runtime uses the
    68	    injected router so the provider selection never reads from disk config."""
    69	    provider = ScriptedProvider(steps)
    70	    router = DefaultLLMRouter(
    71	        _SCRIPTED_ROUTER_CONFIG,
    72	        {"scripted": provider},
    73	    )
    74	    return ConversationRuntime(
    75	        store,
    76	        router=router,
    77	        sandbox_service=ProcessSandboxService(),
    78	    )
    79	
    80	
    81	async def run_scripted(
    82	    steps: list[Step],
    83	    *,
    84	    artifact_mode: bool = True,
    85	    autonomous: bool = False,
    86	    timeout_s: float = 30.0,
    87	) -> dict:
    88	    """Run the REAL agent loop with a scripted model and return evidence.
    89	
    90	    Parameters
    91	    ----------
    92	    steps:
    93	        Ordered list of ``(text, [ProposedToolCall, ...])`` tuples to replay.
    94	        The last step is repeated if the loop calls the model more times than
    95	        the script provides.
    96	    artifact_mode:
    97	        When ``True`` (default), the conversation uses ``OperatingMode.INTERACTIVE``
    98	        (no plan gate): the model can call tools immediately, then call ``finish``
    99	        to end the run.  Set to ``False`` for the full plan-first lifecycle (the
   100	        scripted steps must include a ``submit_plan`` call and, if
   101	        ``autonomous=False``, the test must call ``runtime.approve_plan(cid)``
   102	        after the first kick).
   103	    autonomous:
   104	        When ``True``, the loop auto-approves the submitted plan (relevant only
   105	        when ``artifact_mode=False``).
   106	    timeout_s:
   107	        Hard ceiling on how long to wait for the loop to reach a terminal
   108	        status.  Raises ``asyncio.TimeoutError`` on breach so a hung test
   109	        fails immediately rather than blocking the suite.
   110	
   111	    Returns
   112	    -------
   113	    dict with keys:
   114	        - ``events``: ``list[Event]`` — the full event log from the store
   115	        - ``final_status``: ``ConversationStatus`` — the terminal status
   116	        - ``workspace_path``: ``str | None`` — host path to the sandbox workspace
   117	          (available when at least one sandbox tool was called; ``None`` if
   118	          the run used no sandbox tools or the backend does not expose a host path)
   119	        - ``runtime``: ``ConversationRuntime`` — for test-level inspection
   120	        - ``cid``: ``str`` — the conversation id
   121	    """
   122	    cid = f"scripted-{uuid.uuid4().hex[:8]}"
   123	    store = SqliteEventStore(":memory:")
   124	
   125	    runtime = _build_runtime(store, steps)
   126	
   127	    # Register the conversation.
   128	    store.create_conversation(cid, owner_id="local", surface="build")
   129	    runtime.set_surface(cid, "build")
   130	
   131	    if artifact_mode:
   132	        runtime.set_artifact_mode(cid, True)
   133	    if autonomous:
   134	        runtime.set_autonomous(cid, True)
   135	
   136	    # Append the triggering user message that kicks the loop.
   137	    await store.append(
   138	        cid,
   139	        MessageEvent(
   140	            source=EventSource.USER,
   141	            message=LLMMessage(role="user", content="run the scripted test task"),
   142	        ),
   143	    )
   144	
   145	    # Kick the loop; wait for it to reach a terminal status.
   146	    runtime.kick(cid)
   147	    task = runtime._tasks.get(cid)
   148	    if task is None:
   149	        raise RuntimeError("runtime.kick() did not schedule a task — check surface wiring")
   150	
   151	    try:
   152	        await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
   153	    except TimeoutError:
   154	        task.cancel()
   155	        raise
   156	
   157	    # Collect evidence.
   158	    events = await store.get_events(cid)
   159	    state = await store.get_state(cid)
   160	
   161	    # Workspace path: executor holds the sandbox session; the session exposes
   162	    # the host-side workspace root once at least one sandbox tool has run.
   163	    executor = runtime._executors.get(cid)
   164	    sandbox = getattr(executor, "sandbox", None) if executor is not None else None
   165	    workspace_path: str | None = getattr(sandbox, "workspace_path", None)
   166	
   167	    return {
   168	        "events": events,
   169	        "final_status": state.execution_status,
   170	        "workspace_path": workspace_path,
   171	        "runtime": runtime,
   172	        "cid": cid,
   173	    }

exec
/bin/bash -lc "nl -ba packages/agent-server/tests/integration/test_scripted_smoke.py | sed -n '1,240p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""W8 smoke test — scripted-agent integration harness (one end-to-end scenario).
     2	
     3	Proves the harness works end-to-end:
     4	  1. The scripted model emits a ``file_write`` tool call through the REAL
     5	     ``DefaultLLMRouter`` → ``RouterAgent.step()`` → ``DefaultToolExecutor``
     6	     → ``ProcessSandbox`` boundary.
     7	  2. Then emits a ``finish`` call (intercepted by the loop, not the executor).
     8	  3. The run reaches ``FINISHED`` within the timeout.
     9	  4. The written file exists on the host filesystem at the reported
    10	     ``workspace_path``.
    11	  5. The event log contains at least one ``ActionEvent`` (for the file_write)
    12	     and at least one ``ObservationEvent`` (the sandbox result), proving the
    13	     tool call actually executed through the real sandbox rather than being
    14	     short-circuited.
    15	
    16	This scenario does NOT test failure paths (missing-file sandbox errors,
    17	crash-supervisor, etc.) — those are W8 follow-on scenarios.  The sole
    18	purpose here is verifying that the harness wiring is correct before
    19	depending on it in other tests.
    20	"""
    21	
    22	from __future__ import annotations
    23	
    24	import os
    25	
    26	import pytest
    27	from disco.core.events import ActionEvent, ConversationStatus, ObservationEvent
    28	from disco.core.llm import ProposedToolCall
    29	
    30	from .harness import run_scripted
    31	
    32	
    33	@pytest.mark.asyncio
    34	async def test_file_write_then_finish_reaches_finished() -> None:
    35	    """Smoke: scripted file_write → finish lands in FINISHED with real sandbox."""
    36	    output_path = "smoke_output.txt"
    37	    output_content = "hello from the scripted harness"
    38	
    39	    steps = [
    40	        # Step 1: write a file through the real sandbox.
    41	        (
    42	            "writing the output file",
    43	            [
    44	                ProposedToolCall(
    45	                    tool_name="file_write",
    46	                    arguments={"path": output_path, "content": output_content},
    47	                )
    48	            ],
    49	        ),
    50	        # Step 2: finish the run (intercepted by the loop as the affirmative
    51	        # terminal move; never reaches the executor).
    52	        (
    53	            "task complete",
    54	            [
    55	                ProposedToolCall(
    56	                    tool_name="finish",
    57	                    arguments={"summary": "wrote output file"},
    58	                )
    59	            ],
    60	        ),
    61	    ]
    62	
    63	    result = await run_scripted(steps, timeout_s=30.0)
    64	
    65	    # ── 1. Terminal status ────────────────────────────────────────────────────
    66	    assert result["final_status"] == ConversationStatus.FINISHED, (
    67	        f"Expected FINISHED, got {result['final_status']}. "
    68	        f"Events: {[type(e).__name__ for e in result['events']]}"
    69	    )
    70	
    71	    # ── 2. Event log contains the real tool execution ─────────────────────────
    72	    events = result["events"]
    73	    action_events = [e for e in events if isinstance(e, ActionEvent)]
    74	    obs_events = [e for e in events if isinstance(e, ObservationEvent)]
    75	
    76	    assert action_events, "No ActionEvent in the event log — tool call was not recorded"
    77	    assert obs_events, (
    78	        "No ObservationEvent in the event log — "
    79	        "tool call may not have executed through the real sandbox"
    80	    )
    81	
    82	    # The file_write action should be first.
    83	    first_action = action_events[0]
    84	    assert first_action.tool_call is not None
    85	    assert first_action.tool_call.tool_name == "file_write", (
    86	        f"Expected first action to be file_write, got "
    87	        f"{first_action.tool_call.tool_name!r}"
    88	    )
    89	
    90	    # ── 3. File exists in the sandbox workspace ───────────────────────────────
    91	    workspace = result["workspace_path"]
    92	    assert workspace is not None, (
    93	        "workspace_path is None — the sandbox did not create a host-side workspace. "
    94	        "Check that ProcessSandboxService is wired correctly."
    95	    )
    96	
    97	    host_path = os.path.join(workspace, output_path)
    98	    assert os.path.exists(host_path), (
    99	        f"Written file not found at {host_path!r}. "
   100	        f"workspace_path={workspace!r}"
   101	    )
   102	
   103	    with open(host_path) as f:
   104	        actual_content = f.read()
   105	    assert actual_content == output_content, (
   106	        f"File content mismatch: expected {output_content!r}, got {actual_content!r}"
   107	    )

exec
/bin/bash -lc 'rg -n "ScriptedProvider|Scripted|harness|RouterAgent|DefaultLLMRouter|ModelProvider" packages/agent-server/tests/integration packages/agent-server/tests packages/core/tests | head -n 240' in /var/home/dylan/projects/disco
 succeeded in 0ms:
packages/agent-server/tests/integration/test_task_supervision.py:26:from disco.core.llm import DefaultLLMRouter, ModelEntry, ProposedToolCall, RouterConfig
packages/agent-server/tests/integration/test_task_supervision.py:29:from .scripted_model import ScriptedProvider
packages/agent-server/tests/integration/test_task_supervision.py:35:# A single trivial step so ScriptedProvider constructs — the loop is monkeypatched away in
packages/agent-server/tests/integration/test_task_supervision.py:43:        router=DefaultLLMRouter(_CFG, {"scripted": ScriptedProvider(_STEP)}),
packages/agent-server/tests/integration/test_task_supervision.py:61:    """A loop whose run() raises an exception that escapes the harness (simulates the
packages/core/tests/test_cluster9_errors.py:48:    from disco.core.loop.agent import RouterAgent
packages/core/tests/test_cluster9_errors.py:50:    agent = RouterAgent(router=None)  # ctor doesn't touch the router
packages/core/tests/test_cluster9_errors.py:56:    from disco.core.loop.agent import RouterAgent
packages/core/tests/test_cluster9_errors.py:58:    agent = RouterAgent(router=None, temperature=0.0)
packages/agent-server/tests/integration/scripted_model.py:1:"""Scripted LLM provider — W8 evidence-harness integration layer.
packages/agent-server/tests/integration/scripted_model.py:3:Conforms to the real ``ModelProvider`` protocol (packages/core/src/disco/core/llm/provider.py)
packages/agent-server/tests/integration/scripted_model.py:4:so it wires through the REAL ``DefaultLLMRouter`` → ``RouterAgent.step()`` boundary,
packages/agent-server/tests/integration/scripted_model.py:8:  - ``ModelProvider.name: str``
packages/agent-server/tests/integration/scripted_model.py:9:  - ``ModelProvider.complete(req: CompletionRequest, *, model: str) -> CompletionResponse``
packages/agent-server/tests/integration/scripted_model.py:10:  - ``ModelProvider.stream_complete(req: CompletionRequest, *, model: str)
packages/agent-server/tests/integration/scripted_model.py:12:  - ``ModelProvider.supports(requirement: Requirement, *, model: str) -> bool``
packages/agent-server/tests/integration/scripted_model.py:33:    from .scripted_model import ScriptedProvider
packages/agent-server/tests/integration/scripted_model.py:42:    provider = ScriptedProvider(steps)
packages/agent-server/tests/integration/scripted_model.py:92:class ScriptedProvider:
packages/agent-server/tests/integration/scripted_model.py:93:    """A deterministic ``ModelProvider`` that replays a caller-supplied script.
packages/agent-server/tests/integration/scripted_model.py:107:    The provider emits through the real ``DefaultLLMRouter.stream_complete``
packages/agent-server/tests/integration/scripted_model.py:117:            raise ValueError("ScriptedProvider requires at least one step")
packages/core/tests/test_router_overflow.py:7:these tests are kept (skipped) as the revival harness. Un-skip them, restore the
packages/core/tests/test_router_overflow.py:36:from llm_fakes import FakeModelProvider, build_router, simple_config
packages/core/tests/test_router_overflow.py:177:    local = FakeModelProvider("ollama", raises=LLMTransientError("flaky"))  # always fails
packages/core/tests/test_router_overflow.py:178:    overflow = FakeModelProvider("openrouter", text="frontier-recovered", cost_usd=0.02)
packages/agent-server/tests/integration/test_scripted_smoke.py:1:"""W8 smoke test — scripted-agent integration harness (one end-to-end scenario).
packages/agent-server/tests/integration/test_scripted_smoke.py:3:Proves the harness works end-to-end:
packages/agent-server/tests/integration/test_scripted_smoke.py:5:     ``DefaultLLMRouter`` → ``RouterAgent.step()`` → ``DefaultToolExecutor``
packages/agent-server/tests/integration/test_scripted_smoke.py:18:purpose here is verifying that the harness wiring is correct before
packages/agent-server/tests/integration/test_scripted_smoke.py:30:from .harness import run_scripted
packages/agent-server/tests/integration/test_scripted_smoke.py:37:    output_content = "hello from the scripted harness"
packages/core/tests/test_cluster3_security.py:13:from loop_fakes import ScriptedAgent, action_step, build_loop
packages/core/tests/test_cluster3_security.py:52:    agent = ScriptedAgent(
packages/core/tests/test_router_integration.py:25:from llm_fakes import FakeModelProvider, build_router
packages/core/tests/test_router_integration.py:42:    driver = FakeModelProvider("ollama", text="I'll list the files.", tool_calls=[proposed])
packages/agent-server/tests/integration/harness.py:1:"""W8 scripted-agent integration harness.
packages/agent-server/tests/integration/harness.py:4:with a scripted model (``ScriptedProvider``) that replays a caller-supplied
packages/agent-server/tests/integration/harness.py:6:``DefaultLLMRouter`` → ``RouterAgent.step()`` boundary the production model
packages/agent-server/tests/integration/harness.py:42:    DefaultLLMRouter,
packages/agent-server/tests/integration/harness.py:48:from .scripted_model import ScriptedProvider, Step
packages/agent-server/tests/integration/harness.py:69:    provider = ScriptedProvider(steps)
packages/agent-server/tests/integration/harness.py:70:    router = DefaultLLMRouter(
packages/core/tests/test_agent_isolation.py:8:from disco.core.llm import DefaultLLMRouter, OperatingMode, OverflowSignal
packages/core/tests/test_agent_isolation.py:10:from disco.core.loop import BuildAgent, ResearchAgent, RouterAgent
packages/core/tests/test_agent_isolation.py:20:    return DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
packages/core/tests/test_agent_isolation.py:74:    # Direct RouterAgent(router) callers keep the original v1 prose=finished
packages/core/tests/test_agent_isolation.py:76:    assert RouterAgent(_router())._prose_finishes is True
packages/core/tests/test_loop_state_machine.py:17:from loop_fakes import ScriptedAgent, action_step, build_loop, finish_step
packages/core/tests/test_loop_state_machine.py:28:    agent = ScriptedAgent([action_step(), finish_step()])
packages/core/tests/test_loop_state_machine.py:43:    agent = ScriptedAgent(
packages/core/tests/test_loop_state_machine.py:62:    agent = ScriptedAgent([action_step()])
packages/core/tests/llm_fakes.py:13:    DefaultLLMRouter,
packages/core/tests/llm_fakes.py:30:class FakeModelProvider:
packages/core/tests/llm_fakes.py:31:    """A `ModelProvider` double. Deterministic; records the requests it saw so
packages/core/tests/llm_fakes.py:134:    local: FakeModelProvider | None = None,
packages/core/tests/llm_fakes.py:135:    overflow: FakeModelProvider | None = None,
packages/core/tests/llm_fakes.py:137:) -> tuple[DefaultLLMRouter, InMemoryRoutingSink, dict[str, FakeModelProvider]]:
packages/core/tests/llm_fakes.py:141:        "ollama": local or FakeModelProvider("ollama", text="local-says-hi"),
packages/core/tests/llm_fakes.py:143:        or FakeModelProvider("openrouter", text="frontier-says-hi", cost_usd=0.02),
packages/core/tests/llm_fakes.py:146:    router = DefaultLLMRouter(config, providers, sink=sink, **router_kwargs)
packages/core/tests/test_router_errors.py:21:from llm_fakes import FakeModelProvider, build_router
packages/core/tests/test_router_errors.py:33:    local = FakeModelProvider("ollama", raises=LLMContextWindowExceeded("ctx too big"))
packages/core/tests/test_router_errors.py:42:    local = FakeModelProvider("ollama", raises=LLMContextWindowExceeded("ctx"))
packages/core/tests/test_router_errors.py:50:    local = FakeModelProvider("ollama", raises=LLMAuthError("bad key"))
packages/core/tests/test_router_errors.py:58:    local = FakeModelProvider("ollama", raises=LLMContentFiltered("refused"))
packages/core/tests/test_router_errors.py:68:    local = FakeModelProvider("ollama", raises=LLMTransientError("flaky"))
packages/core/tests/test_router_errors.py:77:    local = FakeModelProvider(
packages/core/tests/test_toolcall_defense.py:6:from loop_fakes import ScriptedAgent, action_step, build_loop
packages/core/tests/test_toolcall_defense.py:116:    """DC-05: an exhausted ScriptedAgent in a requery must not hang.
packages/core/tests/test_toolcall_defense.py:120:    # 2. ScriptedAgent only has ONE step. Requery calls it again.
packages/core/tests/test_toolcall_defense.py:121:    # 3. ScriptedAgent repeats the last step (exhausted).
packages/core/tests/test_toolcall_defense.py:124:    agent = ScriptedAgent([
packages/core/tests/test_toolcall_defense.py:136:        pytest.fail("AgentLoop.run() hung on exhausted ScriptedAgent during requery")
packages/core/tests/test_gate_scoping.py:23:    ScriptedAgent,
packages/core/tests/test_gate_scoping.py:127:    agent = ScriptedAgent([action_step(tool="shell", args={"command": "rm -rf /"}), finish_step()])
packages/core/tests/test_gate_scoping.py:159:    agent = ScriptedAgent(
packages/core/tests/test_gate_scoping.py:180:    agent = ScriptedAgent(
packages/core/tests/test_gate_scoping.py:206:    agent = ScriptedAgent([action_step(tool="shell", args={"command": "curl host"}), finish_step()])
packages/core/tests/test_gate_scoping.py:221:    agent = ScriptedAgent(
packages/core/tests/test_gate_scoping.py:251:    agent = ScriptedAgent([action_step(tool="shell", args={"command": "echo hi"}), finish_step()])
packages/core/tests/test_evidence_schema.py:36:    """Only the required field (harness_run_id) is supplied."""
packages/core/tests/test_evidence_schema.py:37:    rec = EvidenceRecord(harness_run_id="run_abc123")
packages/core/tests/test_evidence_schema.py:52:        harness_run_id=run_id,
packages/core/tests/test_evidence_schema.py:69:    rec = EvidenceRecord(harness_run_id="run_x")
packages/core/tests/test_evidence_schema.py:76:        EvidenceRecord.model_validate({"harness_run_id": "r", "unknown_field": True})
packages/core/tests/test_evidence_schema.py:81:    rec = EvidenceRecord(harness_run_id="run_json", artifact_ids=["x"])
packages/core/tests/test_remember_tool.py:17:from disco.core.llm import DefaultLLMRouter, OperatingMode, ProposedToolCall
packages/core/tests/test_remember_tool.py:18:from disco.core.loop import NeverConfirm, RouterAgent
packages/core/tests/test_remember_tool.py:28:    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
packages/core/tests/test_remember_tool.py:29:    return RouterAgent(router, conversation_id=CID), provider
packages/core/tests/test_loop_integration.py:4:real RouterAgent builds a CompletionRequest(AGENT_DRIVER) to the (faked) router →
packages/core/tests/test_loop_integration.py:19:from disco.core.llm import DefaultLLMRouter, LLMContentFiltered, ProposedToolCall
packages/core/tests/test_loop_integration.py:20:from disco.core.loop import NeverConfirm, RouterAgent
packages/core/tests/test_loop_integration.py:21:from llm_fakes import FakeModelProvider, simple_config  # the router-contract test config
packages/core/tests/test_loop_integration.py:42:    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
packages/core/tests/test_loop_integration.py:43:    agent = RouterAgent(router, conversation_id=CID)
packages/core/tests/test_loop_integration.py:85:    rejecting = FakeModelProvider("ollama", raises=LLMContentFiltered(real_reason))
packages/core/tests/test_loop_integration.py:86:    router = DefaultLLMRouter(
packages/core/tests/test_loop_integration.py:87:        simple_config(), {"ollama": rejecting, "openrouter": FakeModelProvider("openrouter")}
packages/core/tests/test_loop_integration.py:89:    agent = RouterAgent(router, conversation_id=CID)
packages/core/tests/test_prefill_masking.py:6:from disco.core.loop.agent import RouterAgent
packages/core/tests/test_prefill_masking.py:32:    agent = RouterAgent(router)
packages/core/tests/test_loop_confirmation.py:15:    ScriptedAgent,
packages/core/tests/test_loop_confirmation.py:25:    agent = ScriptedAgent([action_step(thought="risky"), finish_step()])
packages/core/tests/test_loop_confirmation.py:55:    agent = ScriptedAgent([action_step(thought="risky"), finish_step()])
packages/core/tests/test_loop_confirmation.py:72:    agent = ScriptedAgent([action_step(), finish_step()])
packages/core/tests/test_loop_confirmation.py:84:    agent = ScriptedAgent([action_step(thought="the plan"), finish_step()])
packages/core/tests/test_loop_extra.py:28:    ScriptedAgent,
packages/core/tests/test_loop_extra.py:45:    loop, store = build_loop(ScriptedAgent([action_step(), finish_step()]), condenser=cond)
packages/core/tests/test_loop_extra.py:56:    agent = ScriptedAgent(
packages/core/tests/test_loop_extra.py:77:    agent = ScriptedAgent([action_step(), finish_step()])
packages/core/tests/test_loop_extra.py:87:    agent = ScriptedAgent([finish_step()])
packages/core/tests/test_loop_extra.py:98:    agent = ScriptedAgent([action_step(), finish_step()])
packages/core/tests/test_loop_extra.py:111:        ScriptedAgent([action_step(), finish_step()]),
packages/core/tests/test_c6_recitation_cadence.py:37:from loop_fakes import FakeAnalyzer, FakeExecutor, FakeSummarizer, NeverConfirm, ScriptedAgent
packages/core/tests/test_c6_recitation_cadence.py:86:        ScriptedAgent([]),  # never stepped
packages/core/tests/test_c6_recitation_cadence.py:511:    and cadence=3. The ScriptedAgent marks the plan step done first
packages/core/tests/test_c6_recitation_cadence.py:546:    agent = ScriptedAgent(actions)
packages/core/tests/test_loop_stophooks.py:6:from loop_fakes import ScriptedAgent, ScriptedStopHook, build_loop, finish_step
packages/core/tests/test_loop_stophooks.py:13:    agent = ScriptedAgent([finish_step(), finish_step()])
packages/core/tests/test_loop_stophooks.py:14:    hook = ScriptedStopHook([False, True])
packages/core/tests/test_loop_stophooks.py:30:    agent = ScriptedAgent([finish_step()])
packages/core/tests/test_c13_exec_prefill.py:41:from disco.core.loop.agent import RouterAgent
packages/core/tests/test_c13_exec_prefill.py:49:def _make_agent_with_capture() -> tuple[RouterAgent, list[CompletionRequest]]:
packages/core/tests/test_c13_exec_prefill.py:50:    """Build a RouterAgent whose router captures every CompletionRequest the
packages/core/tests/test_c13_exec_prefill.py:72:    agent = RouterAgent(router)
packages/core/tests/test_openai_provider.py:1:"""OpenAI-compatible ModelProvider adapter — hermetic tests (httpx.MockTransport).
packages/core/tests/test_watch_it_write.py:19:from loop_fakes import ScriptedAgent, build_loop, finish_step
packages/core/tests/test_watch_it_write.py:96:    loop, _ = build_loop(ScriptedAgent([finish_step()]), store=store, conversation_id=cid)
packages/core/tests/test_watch_it_write.py:129:    loop, _ = build_loop(ScriptedAgent([finish_step()]), store=store, conversation_id=cid)
packages/core/tests/test_watch_it_write.py:143:    loop, _ = build_loop(ScriptedAgent([finish_step()]), store=store, conversation_id="b3")
packages/core/tests/test_security_integration.py:13:from loop_fakes import FakeExecutor, ScriptedAgent, action_step, build_loop, finish_step
packages/core/tests/test_security_integration.py:18:def _agent(cmd: str) -> ScriptedAgent:
packages/core/tests/test_security_integration.py:19:    return ScriptedAgent([action_step(tool="shell", args={"command": cmd}), finish_step()])
packages/core/tests/test_dod_evaluator.py:25:hermetic — no real subprocess, no real network), and a `ScriptedJudge` for
packages/core/tests/test_dod_evaluator.py:117:class ScriptedJudge(SubjectiveJudge):
packages/core/tests/test_dod_evaluator.py:420:    """A wired `ScriptedJudge` (fake — no live LLM) is invoked with a
packages/core/tests/test_dod_evaluator.py:426:    judge = ScriptedJudge(
packages/core/tests/test_c18_plan_step_done_condition.py:52:from loop_fakes import ScriptedAgent, action_step, build_loop, finish_step
packages/core/tests/test_c18_plan_step_done_condition.py:158:    agent = ScriptedAgent(
packages/core/tests/test_c18_plan_step_done_condition.py:226:    agent = ScriptedAgent(
packages/core/tests/test_c18_plan_step_done_condition.py:292:    agent = ScriptedAgent(
packages/core/tests/test_c18_plan_step_done_condition.py:389:    agent = ScriptedAgent(
packages/core/tests/test_c18_plan_step_done_condition.py:437:    agent = ScriptedAgent(
packages/core/tests/test_c18_plan_step_done_condition.py:486:    agent = ScriptedAgent(
packages/core/tests/test_c18_plan_step_done_condition.py:567:    agent = ScriptedAgent(
packages/core/tests/test_c18_plan_step_done_condition.py:622:    agent = ScriptedAgent(
packages/core/tests/test_c18_plan_step_done_condition.py:674:    agent = ScriptedAgent(
packages/core/tests/test_c18_plan_step_done_condition.py:730:    agent = ScriptedAgent(
packages/core/tests/test_c18_plan_step_done_condition.py:774:    agent = ScriptedAgent(
packages/core/tests/test_vision_wiring.py:10:from disco.core.llm.routing import CallContext, DefaultLLMRouter
packages/core/tests/test_vision_wiring.py:50:    router = DefaultLLMRouter(config, providers)
packages/core/tests/test_serve_tool.py:14:from disco.core.llm import DefaultLLMRouter, OperatingMode, ProposedToolCall
packages/core/tests/test_serve_tool.py:15:from disco.core.loop import NeverConfirm, RouterAgent
packages/core/tests/test_serve_tool.py:24:    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
packages/core/tests/test_serve_tool.py:25:    return RouterAgent(router, conversation_id=CID)
packages/core/tests/test_router_summarizer_stream.py:15:    DefaultLLMRouter,
packages/core/tests/test_router_summarizer_stream.py:19:from llm_fakes import FakeModelProvider, build_router, simple_config
packages/core/tests/test_router_summarizer_stream.py:24:    summ_provider = FakeModelProvider("ollama", text="THE SUMMARY")
packages/core/tests/test_router_summarizer_stream.py:25:    providers = {"ollama": summ_provider, "openrouter": FakeModelProvider("openrouter")}
packages/core/tests/test_router_summarizer_stream.py:26:    router = DefaultLLMRouter(simple_config(), providers)
packages/core/tests/test_router_summarizer_stream.py:47:    local = FakeModelProvider("ollama", text=text, cost_usd=0.0)
packages/core/tests/test_router_summarizer_stream.py:48:    providers = {"ollama": local, "openrouter": FakeModelProvider("openrouter")}
packages/core/tests/test_router_summarizer_stream.py:49:    router = DefaultLLMRouter(simple_config(), providers)
packages/core/tests/test_hs02_compaction_template.py:23:instruction. They do NOT touch a real model — a `FakeModelProvider`
packages/core/tests/test_hs02_compaction_template.py:37:from llm_fakes import FakeModelProvider, build_router
packages/core/tests/test_hs02_compaction_template.py:52:def _recording_summarizer() -> tuple[RouterSummarizer, FakeModelProvider]:
packages/core/tests/test_hs02_compaction_template.py:58:    recorder = FakeModelProvider("ollama", text="OK")
packages/core/tests/test_snapshot_truncation.py:14:    ScriptedAgent,
packages/core/tests/test_snapshot_truncation.py:55:    agent = ScriptedAgent([finish_step()])
packages/core/tests/test_verify_on_finish.py:18:from disco.core.llm import DefaultLLMRouter, ProposedToolCall
packages/core/tests/test_verify_on_finish.py:19:from disco.core.loop import ConfirmRisky, NeverConfirm, RouterAgent
packages/core/tests/test_verify_on_finish.py:28:    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
packages/core/tests/test_verify_on_finish.py:29:    return RouterAgent(router, conversation_id=CID)
packages/core/tests/test_dc05_loop.py:17:from loop_fakes import ScriptedAgent, action_step, build_loop, finish_step
packages/core/tests/test_dc05_loop.py:41:    agent = ScriptedAgent([
packages/core/tests/test_dc05_loop.py:73:    agent = ScriptedAgent([
packages/core/tests/test_dc05_loop.py:104:    agent = ScriptedAgent([
packages/core/tests/test_dc05_loop.py:150:    agent = ScriptedAgent([finish_step()])   # repeats forever
packages/core/tests/test_dc05_loop.py:183:    agent = ScriptedAgent([action_step("shell", {}), finish_step()])
packages/core/tests/test_dc05_loop.py:199:    agent = ScriptedAgent([
packages/core/tests/test_dc05_loop.py:241:    agent = ScriptedAgent([
packages/core/tests/test_dc05_loop.py:264:    # Single-item list: ScriptedAgent repeats it on every call (min(i, 0) == 0).
packages/core/tests/test_dc05_loop.py:265:    agent = ScriptedAgent([LLMTransientError("unavailable")])
packages/core/tests/test_dc05_loop.py:285:    agent = ScriptedAgent([
packages/core/tests/test_dc05_loop.py:327:    agent = ScriptedAgent([
packages/core/tests/test_dc05_loop.py:350:    agent = ScriptedAgent([
packages/core/tests/test_dc05_loop.py:372:    agent = ScriptedAgent([
packages/core/tests/test_dc05_loop.py:392:    agent = ScriptedAgent([
packages/core/tests/test_dc05_loop.py:416:    agent = ScriptedAgent([
packages/core/tests/test_dc05_loop.py:435:    agent = ScriptedAgent([
packages/core/tests/test_dc05_loop.py:472:    agent = ScriptedAgent([
packages/core/tests/test_dc05_loop.py:499:    agent = ScriptedAgent([
packages/core/tests/test_dc05_loop.py:552:    agent = ScriptedAgent([finish_step()])
packages/core/tests/test_dc05_loop.py:568:    agent = ScriptedAgent([
packages/core/tests/test_dc05_loop.py:588:    agent = ScriptedAgent([
packages/core/tests/test_dc05_loop.py:630:    agent = ScriptedAgent([
packages/core/tests/test_router_routing.py:18:    DefaultLLMRouter,
packages/core/tests/test_router_routing.py:27:from llm_fakes import FakeModelProvider, build_router
packages/core/tests/test_router_routing.py:45:    params = set(inspect.signature(DefaultLLMRouter.complete).parameters)
packages/core/tests/test_router_routing.py:108:    local = FakeModelProvider("ollama", text="answered anyway")
packages/core/tests/test_router_routing.py:121:    local = FakeModelProvider("ollama", raises=LLMContentFiltered(real_reason))
packages/core/tests/test_finish_dod_gate.py:18:from disco.core.llm import DefaultLLMRouter
packages/core/tests/test_finish_dod_gate.py:19:from disco.core.loop import RouterAgent
packages/core/tests/test_finish_dod_gate.py:32:    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
packages/core/tests/test_finish_dod_gate.py:33:    return RouterAgent(router, conversation_id=CID)
packages/core/tests/test_hs08_invalid_reroute_invariant.py:51:from loop_fakes import ScriptedAgent, action_step, build_loop, finish_step
packages/core/tests/test_hs08_invalid_reroute_invariant.py:168:    The ScriptedAgent repeats the unknown tool step until the engine
packages/core/tests/test_hs08_invalid_reroute_invariant.py:177:    # Single scripted step; ScriptedAgent repeats it on exhaustion.
packages/core/tests/test_hs08_invalid_reroute_invariant.py:178:    agent = ScriptedAgent([action_step(unknown)])
packages/core/tests/test_hs08_invalid_reroute_invariant.py:286:    agent = ScriptedAgent([action_step(unknown)])
packages/core/tests/test_hs08_invalid_reroute_invariant.py:319:#     ScriptedAgent calls `finish` — the loop terminates as FINISHED
packages/core/tests/test_hs08_invalid_reroute_invariant.py:339:    agent = ScriptedAgent([
packages/core/tests/test_hs08_invalid_reroute_invariant.py:391:    agent = ScriptedAgent([action_step(unknown)])
packages/core/tests/test_hs08_invalid_reroute_invariant.py:397:    # The ScriptedAgent captured the views the loop sent it. The
packages/core/tests/test_assist_wireup.py:1:"""Wire-through: the `assist` flag must SURVIVE from RouterAgent.step into the
packages/core/tests/test_assist_wireup.py:8:RouterAgent.step(assist=...) flows the value through. These tests close that hole
packages/core/tests/test_assist_wireup.py:9:end-to-end: RouterAgent -> DefaultLLMRouter -> provider, asserting on the req the
packages/core/tests/test_assist_wireup.py:15:from disco.core.llm import DefaultLLMRouter, OperatingMode, OverflowSignal
packages/core/tests/test_assist_wireup.py:17:from disco.core.loop import RouterAgent
packages/core/tests/test_assist_wireup.py:26:    """A RouterAgent whose router forwards to a SequenceProvider we can inspect.
packages/core/tests/test_assist_wireup.py:30:    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
packages/core/tests/test_assist_wireup.py:31:    return RouterAgent(router), provider
packages/core/tests/test_router_prompts.py:14:    DefaultLLMRouter,
packages/core/tests/test_router_prompts.py:20:from llm_fakes import FakeModelProvider, simple_config
packages/core/tests/test_router_prompts.py:45:    local = FakeModelProvider("ollama")
packages/core/tests/test_router_prompts.py:46:    providers = {"ollama": local, "openrouter": FakeModelProvider("openrouter")}
packages/core/tests/test_router_prompts.py:47:    router = DefaultLLMRouter(
packages/core/tests/test_router_prompts.py:63:    local = FakeModelProvider("ollama")
packages/core/tests/test_router_prompts.py:64:    providers = {"ollama": local, "openrouter": FakeModelProvider("openrouter")}
packages/core/tests/test_router_prompts.py:65:    router = DefaultLLMRouter(
packages/core/tests/test_actionless_completion.py:23:from loop_fakes import ScriptedAgent, action_step, build_loop, finish_step
packages/core/tests/test_actionless_completion.py:56:    agent = ScriptedAgent([
packages/core/tests/test_actionless_completion.py:87:    agent = ScriptedAgent([
packages/core/tests/test_actionless_completion.py:140:    agent = ScriptedAgent([
packages/core/tests/test_actionless_completion.py:171:    agent = ScriptedAgent([
packages/core/tests/test_actionless_completion.py:203:    agent = ScriptedAgent([_notify(f"musing {i}") for i in range(8)])
packages/core/tests/test_loop_condensation.py:15:    ScriptedAgent,
packages/core/tests/test_loop_condensation.py:30:    agent = ScriptedAgent([action_step(), finish_step()])

codex
The scripted harness genuinely goes through `DefaultLLMRouter` and `RouterAgent.step`, but it only uses the process sandbox. That is useful for loop/tool plumbing and weak for the container/podman exception contract that caused W1. I’m now checking frontend replay and bridge wiring.
exec
/bin/bash -lc "nl -ba frontend/src/lib/buildTrace.ts | sed -n '620,730p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   620	  });
   621	  events.forEach((e, i) => {
   622	    if (i < latestPlanIdx) return; // belongs to a superseded plan
   623	    if (e.kind !== "action" || !e.tool_call || e.tool_call.tool_name !== "plan_step") return;
   624	    const idx = Number(e.tool_call.arguments.index);
   625	    const state = String(e.tool_call.arguments.state);
   626	    if (!Number.isFinite(idx)) return;
   627	    if (state === "done") progress.set(idx, "done");
   628	    else if (state === "active" && progress.get(idx) !== "done") progress.set(idx, "active");
   629	  });
   630	  // Stalled-step reconciliation: in terminal states, an "active" marker means
   631	  // the agent started a step and the loop stopped before it finished. Show that
   632	  // honestly instead of pretending it's still working.
   633	  const terminalStopped =
   634	    status === "FINISHED" || status === "STUCK" || status === "ERROR" || status === "IDLE";
   635	  if (terminalStopped) {
   636	    for (const [idx, st] of progress) {
   637	      if (st === "active") progress.set(idx, "stalled");
   638	    }
   639	  }
   640	  return progress;
   641	}
   642	
   643	/** runthru-v2 (#3): Build progress for CAPABLE models — derived from the LATEST
   644	 * declarative `update_plan_progress` snapshot (a full-state rewrite, not a delta).
   645	 * Because the latest snapshot carries the COMPLETE state of every step, it is
   646	 * self-correcting: a dropped/garbled prior update can't leave a step wrong, the
   647	 * next snapshot re-establishes truth. Returns an EMPTY map when no snapshot exists
   648	 * (small models on the assist path never emit it, or none yet) — the UI then falls
   649	 * back to the honest status chip rather than a lying empty checklist. Never inferred
   650	 * from action counts. */
   651	export function deriveBuildProgress(
   652	  events: AgentEvent[],
   653	  status?: ConversationStatus,
   654	): Map<number, StepState> {
   655	  const progress = new Map<number, StepState>();
   656	  // The latest plan supersedes prior ones; only count snapshots after it (a re-plan
   657	  // starts a fresh checklist). Capture its step COUNT to bound the snapshot.
   658	  let latestPlanIdx = -1;
   659	  let latestRev = -1;
   660	  let nSteps = 0;
   661	  events.forEach((e, i) => {
   662	    if (e.kind === "plan" && e.revision >= latestRev) {
   663	      latestRev = e.revision;
   664	      latestPlanIdx = i;
   665	      nSteps = Array.isArray(e.steps) ? e.steps.length : 0;
   666	    }
   667	  });
   668	  // Declarative: the LAST update_plan_progress action wins — it's the full picture.
   669	  let latest: AgentEvent | null = null;
   670	  events.forEach((e, i) => {
   671	    if (i < latestPlanIdx) return;
   672	    if (e.kind === "action" && e.tool_call?.tool_name === "update_plan_progress") latest = e;
   673	  });
   674	  // Apply the declarative snapshot IF one exists — but do NOT early-return when it's
   675	  // absent. A SMALL model (the NL-done-at-finish tier, e.g. local Qwen) never calls
   676	  // update_plan_progress, so `latest` stays null; a `return` here skipped the FINISHED
   677	  // reconciliation below and left every step unchecked on a build that actually finished
   678	  // (the live #3 bug on Qwen). Fall through so terminal reconciliation runs regardless.
   679	  const steps = ((latest as AgentEvent | null)?.tool_call?.arguments?.steps ?? []) as Array<{
   680	    index: number;
   681	    state: string;
   682	  }>;
   683	  if (Array.isArray(steps)) {
   684	    for (const s of steps) {
   685	      const idx = Number(s?.index);
   686	      const st = String(s?.state);
   687	      // Bound to the CURRENT plan's range. A malformed or stale index (e.g. index 99
   688	      // on a 3-step plan, or a leftover from a longer prior plan) must NOT create a
   689	      // phantom step or inflate the done-count into a lying checklist (a snapshot of
   690	      // [{index:99,state:"done"}] would otherwise render "1/3" with every real step
   691	      // pending). Out-of-range / non-integer indices are dropped.
   692	      if (!Number.isInteger(idx) || idx < 1 || (nSteps > 0 && idx > nSteps)) continue;
   693	      if (st === "done" || st === "active" || st === "pending") progress.set(idx, st as StepState);
   694	    }
   695	  }
   696	  // Terminal reconciliation:
   697	  //  • FINISHED — the build passed the real finish gate, so the deliverable is
   698	  //    complete: EVERY plan step reads done. This also stops a stale early snapshot
   699	  //    from showing e.g. "2/3" (with no Done signal) after the build actually
   700	  //    finished — the model isn't required to send a final 100%-done snapshot.
   701	  //  • STUCK/ERROR/IDLE (stopped without completing) → "active" reads as stalled.
   702	  if (status === "FINISHED" && nSteps > 0) {
   703	    for (let i = 1; i <= nSteps; i++) progress.set(i, "done");
   704	  } else if (status === "STUCK" || status === "ERROR" || status === "IDLE") {
   705	    for (const [idx, st] of progress) {
   706	      if (st === "active") progress.set(idx, "stalled");
   707	    }
   708	  }
   709	  return progress;
   710	}
   711	
   712	/** The live activity signal — what the agent is doing RIGHT NOW between events.
   713	 * Without true token streaming, the UI would otherwise show a static "Working"
   714	 * label that feels frozen during slow local-model turns (5-30s for Qwen 27B).
   715	 * This selector inspects the event tail and the status to produce a precise
   716	 * label for what the user is waiting on. */
   717	export type LiveSignal =
   718	  | { kind: "idle" }
   719	  | { kind: "thinking_about_user_message"; preview: string }
   720	  | { kind: "tool_executing"; tool_name: string; detail?: string }
   721	  | { kind: "composing_next_step" }
   722	  | { kind: "starting" }
   723	  | { kind: "waiting_for_you"; label: string };
   724	
   725	export function deriveLiveSignal(
   726	  events: AgentEvent[],
   727	  status: ConversationStatus,
   728	): LiveSignal {
   729	  // Cluster 6: liveness is no longer RUNNING-only. During gate/await states the
   730	  // bar narrates what the agent is waiting ON, so the surface never reads as

exec
/bin/bash -lc "nl -ba frontend/src/lib/buildTrace.replay.test.ts | sed -n '1,260p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	/**
     2	 * W9 — Frontend event-store REPLAY tests.
     3	 *
     4	 * Design principle (the whole point of W9): the test's INPUT is a real event-stream
     5	 * fixture and its assertion is on the real derived OUTPUT — no mocking of the deriver
     6	 * internals. Load a JSON fixture, cast to AgentEvent[], feed to the production selector,
     7	 * assert on what the selector returns.
     8	 *
     9	 * Historical bug this suite locks in (#3): a finished small/local-model build showed 0/4
    10	 * plan steps done because `deriveBuildProgress` early-returned an empty map when there was
    11	 * NO `update_plan_progress` snapshot, skipping the FINISHED→all-done reconciliation.
    12	 * The fix is in `buildTrace.ts`; these tests are the permanent guard.
    13	 *
    14	 * Run: npx vitest run src/lib/buildTrace.replay.test.ts
    15	 */
    16	
    17	import { describe, expect, it } from "vitest";
    18	import type { AgentEvent, ConversationStatus } from "@/types/agent";
    19	import { deriveBuildProgress, deriveActivity, deriveLiveSignal } from "@/lib/buildTrace";
    20	
    21	// Canonical minimized fixtures — each is a small hand-written event array plus an
    22	// `expect` block and a `_why` note.  Load and cast: the derivers must tolerate
    23	// fixtures that omit optional fields (that's intentional — minimal is durable).
    24	import noPlanProgressFinished from "../../e2e-full/fixtures/no_plan_progress_finished.json";
    25	import runningThenError from "../../e2e-full/fixtures/running_then_error.json";
    26	import toolErrorVisible from "../../e2e-full/fixtures/tool_error_visible.json";
    27	import finishedWithPartialPlanUpdates from "../../e2e-full/fixtures/finished_with_partial_plan_updates.json";
    28	import cancelledRequest from "../../e2e-full/fixtures/cancelled_request.json";
    29	
    30	/** Cast a fixture's raw events array to the real AgentEvent type.
    31	 *  Fixtures are minimized and may omit optional EventBase fields — that is
    32	 *  intentional.  The derivers must be robust to sparse inputs. */
    33	function asEvents(fixture: { events: unknown[] }): AgentEvent[] {
    34	  return fixture.events as unknown as AgentEvent[];
    35	}
    36	
    37	// ---------------------------------------------------------------------------
    38	// Fixture: no_plan_progress_finished.json
    39	// Bug #3: small/local model (Qwen) finished without ever calling
    40	// update_plan_progress → deriveBuildProgress returned empty map → 0/4 shown.
    41	// ---------------------------------------------------------------------------
    42	
    43	describe("no_plan_progress_finished — #3 regression (small-model 0/4 after FINISHED)", () => {
    44	  const evts = asEvents(noPlanProgressFinished);
    45	  const status = noPlanProgressFinished.final_status as ConversationStatus;
    46	  const { nsteps } = noPlanProgressFinished.expect;
    47	
    48	  it("deriveBuildProgress: ALL steps reconcile to done on FINISHED with no snapshot", () => {
    49	    // THE critical assertion: no update_plan_progress was emitted, but the build
    50	    // FINISHED → the terminal reconciliation path MUST check off every step.
    51	    // Pre-fix: returned Map{} (size 0); post-fix: Map{1:"done", 2:"done", 3:"done", 4:"done"}.
    52	    const p = deriveBuildProgress(evts, status);
    53	    expect(p.size).toBe(nsteps);
    54	    for (let i = 1; i <= nsteps; i++) {
    55	      expect(p.get(i)).toBe("done");
    56	    }
    57	  });
    58	
    59	  it("deriveLiveSignal: FINISHED → idle (no active/spinning signal on a done build)", () => {
    60	    const sig = deriveLiveSignal(evts, status);
    61	    expect(sig.kind).toBe("idle");
    62	  });
    63	
    64	  it("deriveBuildProgress returns empty map while still RUNNING (no premature checks)", () => {
    65	    // The fix must NOT leak into the running state — only a real FINISHED reconciles.
    66	    const p = deriveBuildProgress(evts, "RUNNING");
    67	    expect(p.size).toBe(0);
    68	  });
    69	});
    70	
    71	// ---------------------------------------------------------------------------
    72	// Fixture: running_then_error.json
    73	// A RUNNING build that transitions to ERROR via a sandbox file-not-found.
    74	// ---------------------------------------------------------------------------
    75	
    76	describe("running_then_error — terminal ERROR is surfaced correctly", () => {
    77	  const evts = asEvents(runningThenError);
    78	  const status = runningThenError.final_status as ConversationStatus;
    79	  const { stalled_step_index, failed_action_id, failed_action_error } = runningThenError.expect;
    80	
    81	  it("deriveLiveSignal: ERROR → idle (not a confusingly active signal)", () => {
    82	    const sig = deriveLiveSignal(evts, status);
    83	    expect(sig.kind).toBe("idle");
    84	  });
    85	
    86	  it("deriveBuildProgress: active step becomes stalled on ERROR (honest about halted work)", () => {
    87	    const p = deriveBuildProgress(evts, status);
    88	    expect(p.get(stalled_step_index)).toBe("stalled");
    89	  });
    90	
    91	  it("deriveActivity: the errored action appears as status=failed", () => {
    92	    const items = deriveActivity(evts, null, status);
    93	    const item = items.find((i) => i.id === failed_action_id);
    94	    expect(item).toBeDefined();
    95	    expect(item?.status).toBe("failed");
    96	  });
    97	
    98	  it("deriveActivity: the error text is in expandable.error (visible to the user on drill-down)", () => {
    99	    const items = deriveActivity(evts, null, status);
   100	    const item = items.find((i) => i.id === failed_action_id);
   101	    expect(item?.expandable?.error).toBe(failed_action_error);
   102	  });
   103	});
   104	
   105	// ---------------------------------------------------------------------------
   106	// Fixture: tool_error_visible.json
   107	// An agent_error event (tool call crashed) must surface in deriveActivity as
   108	// status=failed with the error text accessible in expandable.error.
   109	// Distinct from a success=false observation — agent_error means the tool
   110	// itself threw, not merely returned a non-zero exit code.
   111	// ---------------------------------------------------------------------------
   112	
   113	describe("tool_error_visible — agent_error reflected in activity feed", () => {
   114	  const evts = asEvents(toolErrorVisible);
   115	  const status = toolErrorVisible.final_status as ConversationStatus;
   116	  const { action_id, expected_error } = toolErrorVisible.expect;
   117	
   118	  it("deriveActivity: the shell action with agent_error has status=failed", () => {
   119	    const items = deriveActivity(evts, null, status);
   120	    const item = items.find((i) => i.id === action_id);
   121	    expect(item).toBeDefined();
   122	    expect(item?.status).toBe("failed");
   123	  });
   124	
   125	  it("deriveActivity: the error text is in expandable.error", () => {
   126	    const items = deriveActivity(evts, null, status);
   127	    const item = items.find((i) => i.id === action_id);
   128	    expect(item?.expandable?.error).toBe(expected_error);
   129	  });
   130	
   131	  it("deriveActivity: the action is still present in the feed (not silently dropped)", () => {
   132	    const items = deriveActivity(evts, null, status);
   133	    // At least one item; the failed shell action must not be swallowed.
   134	    const shellItems = items.filter((i) => i.expandable?.tool_name === "shell");
   135	    expect(shellItems.length).toBeGreaterThanOrEqual(1);
   136	  });
   137	
   138	  it("deriveLiveSignal: ERROR → idle", () => {
   139	    const sig = deriveLiveSignal(evts, status);
   140	    expect(sig.kind).toBe("idle");
   141	  });
   142	});
   143	
   144	// ---------------------------------------------------------------------------
   145	// Fixture: finished_with_partial_plan_updates.json
   146	// A capable model emitted a partial snapshot (1 done, 1 active, 1 pending)
   147	// but NO final 100%-done snapshot before FINISHED.  All steps must still
   148	// reconcile to done — the stale partial snapshot must not leave "1/3" in UI.
   149	// ---------------------------------------------------------------------------
   150	
   151	describe("finished_with_partial_plan_updates — stale partial snapshot + FINISHED → all done", () => {
   152	  const evts = asEvents(finishedWithPartialPlanUpdates);
   153	  const status = finishedWithPartialPlanUpdates.final_status as ConversationStatus;
   154	  const { nsteps } = finishedWithPartialPlanUpdates.expect;
   155	
   156	  it("deriveBuildProgress: all steps done even though last snapshot was partial (not 1/3)", () => {
   157	    const p = deriveBuildProgress(evts, status);
   158	    expect(p.size).toBe(nsteps);
   159	    for (let i = 1; i <= nsteps; i++) {
   160	      expect(p.get(i)).toBe("done");
   161	    }
   162	  });
   163	
   164	  it("deriveLiveSignal: FINISHED → idle", () => {
   165	    const sig = deriveLiveSignal(evts, status);
   166	    expect(sig.kind).toBe("idle");
   167	  });
   168	
   169	  it("deriveBuildProgress during RUNNING: partial snapshot is applied as-is (not prematurely all-done)", () => {
   170	    // Re-derive with RUNNING to confirm terminal reconciliation is status-gated.
   171	    const p = deriveBuildProgress(evts, "RUNNING");
   172	    // step 1 was in the snapshot as "done"
   173	    expect(p.get(1)).toBe("done");
   174	    // step 2 was "active" — must NOT be done while still RUNNING
   175	    expect(p.get(2)).not.toBe("done");
   176	  });
   177	});
   178	
   179	// ---------------------------------------------------------------------------
   180	// Fixture: cancelled_request.json
   181	// User cancelled a build mid-run (loop stops; status → IDLE).
   182	// ---------------------------------------------------------------------------
   183	
   184	describe("cancelled_request — mid-run cancellation (IDLE)", () => {
   185	  const evts = asEvents(cancelledRequest);
   186	  const status = cancelledRequest.final_status as ConversationStatus;
   187	  const { stalled_step_index, done_step_index } = cancelledRequest.expect;
   188	
   189	  it("deriveLiveSignal: IDLE → idle (no spinning signal on a halted build)", () => {
   190	    const sig = deriveLiveSignal(evts, status);
   191	    expect(sig.kind).toBe("idle");
   192	  });
   193	
   194	  it("deriveBuildProgress: the active step becomes stalled (honest about unfinished work)", () => {
   195	    const p = deriveBuildProgress(evts, status);
   196	    expect(p.get(stalled_step_index)).toBe("stalled");
   197	  });
   198	
   199	  it("deriveBuildProgress: the already-done step stays done (cancel does not regress done steps)", () => {
   200	    const p = deriveBuildProgress(evts, status);
   201	    expect(p.get(done_step_index)).toBe("done");
   202	  });
   203	});

exec
/bin/bash -lc "nl -ba frontend/src/lib/e2eBridge.ts | sed -n '1,220p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	/**
     2	 * E2E bridge — exposes a non-enumerable `window.__DISCO_E2E__` getter for the
     3	 * evidence harness.  ONLY active in Vite development mode (`import.meta.env.DEV`)
     4	 * or when the `disco_e2e=1` localStorage opt-in is set.  Never exposes secrets,
     5	 * tokens, or full application state — navigational and run-lifecycle metadata only.
     6	 *
     7	 * evidence-harness-campaign.md W6
     8	 */
     9	
    10	/** The currently visible surface / route segment. */
    11	export type Surface =
    12	  | "search"
    13	  | "build"
    14	  | "agent"
    15	  | "deep_research"
    16	  | "settings"
    17	  | "history"
    18	  | "projects"
    19	  | "activity"
    20	  | "share"
    21	  | "imported"
    22	  | null;
    23	
    24	/**
    25	 * The state snapshot returned by `window.__DISCO_E2E__`.
    26	 *
    27	 * - `schemaVersion` — always 1; bump on breaking changes.
    28	 * - `surface`       — the currently visible surface.
    29	 * - `conversationId`— the active conversation id, or null when on a non-conversation route.
    30	 * - `runStatus`     — the live conversation status string (e.g. "RUNNING"), or null.
    31	 * - `route`         — current `window.location.pathname`.
    32	 */
    33	export interface DiscoE2EState {
    34	  schemaVersion: 1;
    35	  surface: Surface;
    36	  conversationId: string | null;
    37	  runStatus: string | null;
    38	  route: string;
    39	}
    40	
    41	/**
    42	 * Defines `window.__DISCO_E2E__` as a live (non-enumerable, configurable)
    43	 * getter that calls `getState()` on every access, so the harness always reads
    44	 * the **current** navigational + run state without a stale closure.
    45	 *
    46	 * Activation:
    47	 * - Always active in Vite `DEV` mode (development server).
    48	 * - Also active in production builds when `localStorage.disco_e2e === '1'`.
    49	 * - Inert otherwise — no property is defined on `window`.
    50	 *
    51	 * Safe to call multiple times: idempotent after the first successful install.
    52	 */
    53	export function installE2EBridge(getState: () => DiscoE2EState): void {
    54	  const active =
    55	    import.meta.env.DEV ||
    56	    (typeof localStorage !== "undefined" &&
    57	      localStorage.getItem("disco_e2e") === "1");
    58	  if (!active) return;
    59	  if (typeof window === "undefined") return;
    60	  // Idempotent — don't clobber an already-installed getter.
    61	  if ("__DISCO_E2E__" in window) return;
    62	
    63	  Object.defineProperty(window, "__DISCO_E2E__", {
    64	    get: getState,
    65	    configurable: true,
    66	    enumerable: false,
    67	  });
    68	}

exec
/bin/bash -lc 'rg -n "deriveLiveSignal|deriveActivity|deriveBuildProgress|BuildProgress|finished|FINISHED|AWAITING|ERROR|STUCK|PAUSED" frontend/src/lib/buildTrace.ts frontend/src/lib/buildTrace.replay.test.ts frontend/e2e-full -S' in /var/home/dylan/projects/disco
 succeeded in 0ms:
frontend/src/lib/buildTrace.replay.test.ts:9: * Historical bug this suite locks in (#3): a finished small/local-model build showed 0/4
frontend/src/lib/buildTrace.replay.test.ts:10: * plan steps done because `deriveBuildProgress` early-returned an empty map when there was
frontend/src/lib/buildTrace.replay.test.ts:11: * NO `update_plan_progress` snapshot, skipping the FINISHED→all-done reconciliation.
frontend/src/lib/buildTrace.replay.test.ts:19:import { deriveBuildProgress, deriveActivity, deriveLiveSignal } from "@/lib/buildTrace";
frontend/src/lib/buildTrace.replay.test.ts:24:import noPlanProgressFinished from "../../e2e-full/fixtures/no_plan_progress_finished.json";
frontend/src/lib/buildTrace.replay.test.ts:27:import finishedWithPartialPlanUpdates from "../../e2e-full/fixtures/finished_with_partial_plan_updates.json";
frontend/src/lib/buildTrace.replay.test.ts:38:// Fixture: no_plan_progress_finished.json
frontend/src/lib/buildTrace.replay.test.ts:39:// Bug #3: small/local model (Qwen) finished without ever calling
frontend/src/lib/buildTrace.replay.test.ts:40:// update_plan_progress → deriveBuildProgress returned empty map → 0/4 shown.
frontend/src/lib/buildTrace.replay.test.ts:43:describe("no_plan_progress_finished — #3 regression (small-model 0/4 after FINISHED)", () => {
frontend/src/lib/buildTrace.replay.test.ts:48:  it("deriveBuildProgress: ALL steps reconcile to done on FINISHED with no snapshot", () => {
frontend/src/lib/buildTrace.replay.test.ts:50:    // FINISHED → the terminal reconciliation path MUST check off every step.
frontend/src/lib/buildTrace.replay.test.ts:52:    const p = deriveBuildProgress(evts, status);
frontend/src/lib/buildTrace.replay.test.ts:59:  it("deriveLiveSignal: FINISHED → idle (no active/spinning signal on a done build)", () => {
frontend/src/lib/buildTrace.replay.test.ts:60:    const sig = deriveLiveSignal(evts, status);
frontend/src/lib/buildTrace.replay.test.ts:64:  it("deriveBuildProgress returns empty map while still RUNNING (no premature checks)", () => {
frontend/src/lib/buildTrace.replay.test.ts:65:    // The fix must NOT leak into the running state — only a real FINISHED reconciles.
frontend/src/lib/buildTrace.replay.test.ts:66:    const p = deriveBuildProgress(evts, "RUNNING");
frontend/src/lib/buildTrace.replay.test.ts:73:// A RUNNING build that transitions to ERROR via a sandbox file-not-found.
frontend/src/lib/buildTrace.replay.test.ts:76:describe("running_then_error — terminal ERROR is surfaced correctly", () => {
frontend/src/lib/buildTrace.replay.test.ts:81:  it("deriveLiveSignal: ERROR → idle (not a confusingly active signal)", () => {
frontend/src/lib/buildTrace.replay.test.ts:82:    const sig = deriveLiveSignal(evts, status);
frontend/src/lib/buildTrace.replay.test.ts:86:  it("deriveBuildProgress: active step becomes stalled on ERROR (honest about halted work)", () => {
frontend/src/lib/buildTrace.replay.test.ts:87:    const p = deriveBuildProgress(evts, status);
frontend/src/lib/buildTrace.replay.test.ts:91:  it("deriveActivity: the errored action appears as status=failed", () => {
frontend/src/lib/buildTrace.replay.test.ts:92:    const items = deriveActivity(evts, null, status);
frontend/src/lib/buildTrace.replay.test.ts:98:  it("deriveActivity: the error text is in expandable.error (visible to the user on drill-down)", () => {
frontend/src/lib/buildTrace.replay.test.ts:99:    const items = deriveActivity(evts, null, status);
frontend/src/lib/buildTrace.replay.test.ts:107:// An agent_error event (tool call crashed) must surface in deriveActivity as
frontend/src/lib/buildTrace.replay.test.ts:118:  it("deriveActivity: the shell action with agent_error has status=failed", () => {
frontend/src/lib/buildTrace.replay.test.ts:119:    const items = deriveActivity(evts, null, status);
frontend/src/lib/buildTrace.replay.test.ts:125:  it("deriveActivity: the error text is in expandable.error", () => {
frontend/src/lib/buildTrace.replay.test.ts:126:    const items = deriveActivity(evts, null, status);
frontend/src/lib/buildTrace.replay.test.ts:131:  it("deriveActivity: the action is still present in the feed (not silently dropped)", () => {
frontend/src/lib/buildTrace.replay.test.ts:132:    const items = deriveActivity(evts, null, status);
frontend/src/lib/buildTrace.replay.test.ts:138:  it("deriveLiveSignal: ERROR → idle", () => {
frontend/src/lib/buildTrace.replay.test.ts:139:    const sig = deriveLiveSignal(evts, status);
frontend/src/lib/buildTrace.replay.test.ts:145:// Fixture: finished_with_partial_plan_updates.json
frontend/src/lib/buildTrace.replay.test.ts:147:// but NO final 100%-done snapshot before FINISHED.  All steps must still
frontend/src/lib/buildTrace.replay.test.ts:151:describe("finished_with_partial_plan_updates — stale partial snapshot + FINISHED → all done", () => {
frontend/src/lib/buildTrace.replay.test.ts:152:  const evts = asEvents(finishedWithPartialPlanUpdates);
frontend/src/lib/buildTrace.replay.test.ts:153:  const status = finishedWithPartialPlanUpdates.final_status as ConversationStatus;
frontend/src/lib/buildTrace.replay.test.ts:154:  const { nsteps } = finishedWithPartialPlanUpdates.expect;
frontend/src/lib/buildTrace.replay.test.ts:156:  it("deriveBuildProgress: all steps done even though last snapshot was partial (not 1/3)", () => {
frontend/src/lib/buildTrace.replay.test.ts:157:    const p = deriveBuildProgress(evts, status);
frontend/src/lib/buildTrace.replay.test.ts:164:  it("deriveLiveSignal: FINISHED → idle", () => {
frontend/src/lib/buildTrace.replay.test.ts:165:    const sig = deriveLiveSignal(evts, status);
frontend/src/lib/buildTrace.replay.test.ts:169:  it("deriveBuildProgress during RUNNING: partial snapshot is applied as-is (not prematurely all-done)", () => {
frontend/src/lib/buildTrace.replay.test.ts:171:    const p = deriveBuildProgress(evts, "RUNNING");
frontend/src/lib/buildTrace.replay.test.ts:189:  it("deriveLiveSignal: IDLE → idle (no spinning signal on a halted build)", () => {
frontend/src/lib/buildTrace.replay.test.ts:190:    const sig = deriveLiveSignal(evts, status);
frontend/src/lib/buildTrace.replay.test.ts:194:  it("deriveBuildProgress: the active step becomes stalled (honest about unfinished work)", () => {
frontend/src/lib/buildTrace.replay.test.ts:195:    const p = deriveBuildProgress(evts, status);
frontend/src/lib/buildTrace.replay.test.ts:199:  it("deriveBuildProgress: the already-done step stays done (cancel does not regress done steps)", () => {
frontend/src/lib/buildTrace.replay.test.ts:200:    const p = deriveBuildProgress(evts, status);
frontend/src/lib/buildTrace.ts:20:   * message addressed to the human (BP-05's release valve: "finished WITHOUT
frontend/src/lib/buildTrace.ts:112:export function deriveActivity(
frontend/src/lib/buildTrace.ts:310:      // (BP-05 release valve: the run finished WITHOUT a clean browser
frontend/src/lib/buildTrace.ts:373: * 3. `deliverable` events — the agent declared a finished served artifact via `serve`;
frontend/src/lib/buildTrace.ts:410:      // Source (3): the agent declared a finished served artifact. Add only when
frontend/src/lib/buildTrace.ts:545:/** The latest finished-artifact handoff the agent declared via `serve` (a
frontend/src/lib/buildTrace.ts:602: * (FINISHED/STUCK/ERROR — anything that means "the loop stopped before this step
frontend/src/lib/buildTrace.ts:631:  // the agent started a step and the loop stopped before it finished. Show that
frontend/src/lib/buildTrace.ts:634:    status === "FINISHED" || status === "STUCK" || status === "ERROR" || status === "IDLE";
frontend/src/lib/buildTrace.ts:651:export function deriveBuildProgress(
frontend/src/lib/buildTrace.ts:676:  // update_plan_progress, so `latest` stays null; a `return` here skipped the FINISHED
frontend/src/lib/buildTrace.ts:677:  // reconciliation below and left every step unchecked on a build that actually finished
frontend/src/lib/buildTrace.ts:697:  //  • FINISHED — the build passed the real finish gate, so the deliverable is
frontend/src/lib/buildTrace.ts:700:  //    finished — the model isn't required to send a final 100%-done snapshot.
frontend/src/lib/buildTrace.ts:701:  //  • STUCK/ERROR/IDLE (stopped without completing) → "active" reads as stalled.
frontend/src/lib/buildTrace.ts:702:  if (status === "FINISHED" && nSteps > 0) {
frontend/src/lib/buildTrace.ts:704:  } else if (status === "STUCK" || status === "ERROR" || status === "IDLE") {
frontend/src/lib/buildTrace.ts:725:export function deriveLiveSignal(
frontend/src/lib/buildTrace.ts:734:  if (status === "AWAITING_PLAN_APPROVAL")
frontend/src/lib/buildTrace.ts:736:  if (status === "AWAITING_USER_DECISION")
frontend/src/lib/buildTrace.ts:738:  if (status === "AWAITING_USER_QUESTION")
frontend/e2e-full/support/schema.ts:24:  TOOL_ERROR = "TOOL_ERROR",
frontend/e2e-full/support/schema.ts:30:  VALIDATION_ERROR = "VALIDATION_ERROR",
frontend/e2e-full/fixtures/README.md:17:| `no_plan_progress_finished.json` | FINISHED | **#3 regression** — small/local model (e.g. Qwen) finished without ever calling `update_plan_progress`; `deriveBuildProgress` pre-fix returned an empty map and showed 0/4 steps done on a build that actually completed. The terminal-reconciliation path MUST set all steps to `done`. |
frontend/e2e-full/fixtures/README.md:18:| `running_then_error.json` | ERROR | A RUNNING build transitions to ERROR via a `SandboxFileNotFoundError`. Guards: `deriveLiveSignal` must return `idle` on ERROR (not a confusingly active signal); the active progress step must become `stalled`; the errored action must appear as `status=failed` in `deriveActivity`. |
frontend/e2e-full/fixtures/README.md:19:| `tool_error_visible.json` | ERROR | An `agent_error` event (tool call crashed, not just returned non-zero) for a `shell` action must surface in `deriveActivity` as `status=failed` with the error text in `expandable.error`. Distinct from a `success=false` observation — pre-gap had no dedicated test for the `agent_error` path. |
frontend/e2e-full/fixtures/README.md:20:| `finished_with_partial_plan_updates.json` | FINISHED | A capable-model build where `update_plan_progress` was called mid-build (step 1 done, step 2 active, step 3 pending) but NO final 100%-done snapshot was sent before FINISHED. `deriveBuildProgress` MUST reconcile all steps to `done` — the stale partial snapshot must not leave the UI showing "1/3 done" after a successful build. |
frontend/e2e-full/fixtures/README.md:21:| `cancelled_request.json` | IDLE | A build cancelled mid-run (user hit Cancel; loop stops; status → IDLE). `deriveLiveSignal` must return `idle`; the active step must become `stalled`; the already-done step must stay `done`. |
frontend/e2e-full/fixtures/README.md:26:`no_plan_progress_finished.json` and contains additional fixtures consumed by
frontend/e2e-full/fixtures/README.md:31:| `no_plan_progress_finished.json` | W9 frontend replay (this directory) |
frontend/e2e-full/fixtures/README.md:40:  "final_status": "FINISHED" /* | "ERROR" | "IDLE" | ... */,
frontend/e2e-full/fixtures/cancelled_request.json:47:  "_why": "A build cancelled mid-run (user hit Cancel; the loop stops and status → IDLE). Guards: (1) deriveLiveSignal returns idle — there is no active signal on a halted build; (2) the active step (index 2) becomes stalled so the UI is honest about unfinished work rather than showing a spinning blue icon; (3) the already-done step (index 1) stays done."
frontend/e2e-full/fixtures/finished_with_partial_plan_updates.json:57:      "status": "FINISHED"
frontend/e2e-full/fixtures/finished_with_partial_plan_updates.json:60:  "final_status": "FINISHED",
frontend/e2e-full/fixtures/finished_with_partial_plan_updates.json:65:  "_why": "A partial update_plan_progress snapshot (step 1 done, step 2 active, step 3 pending) followed by FINISHED with NO final 100%-done snapshot. deriveBuildProgress(events,'FINISHED') MUST reconcile ALL 3 steps to done. Guards against the '1/3' stale-snapshot lie after a build that actually finished — the capable-model path where the model sent a mid-build snapshot but not a final one."
frontend/e2e-full/fixtures/tool_error_visible.json:37:      "status": "ERROR"
frontend/e2e-full/fixtures/tool_error_visible.json:40:  "final_status": "ERROR",
frontend/e2e-full/fixtures/tool_error_visible.json:45:  "_why": "An agent_error event for a shell action must surface in deriveActivity as status=failed with the error text accessible in expandable.error. Pre-gap: no dedicated test confirmed that agent_error events (as opposed to failed observations) correctly reach the activity feed as failed items. This distinction matters: agent_error means the tool call itself crashed, not just returned a non-zero exit code."
frontend/e2e-full/fixtures/running_then_error.json:48:      "status": "ERROR"
frontend/e2e-full/fixtures/running_then_error.json:51:  "final_status": "ERROR",
frontend/e2e-full/fixtures/running_then_error.json:58:  "_why": "A build that transitions RUNNING → ERROR via a sandbox file-not-found. Guards: (1) deriveLiveSignal returns idle on ERROR (not a confusingly active signal); (2) an active progress step becomes stalled so the UI is honest about halted work; (3) the errored action appears as status=failed in deriveActivity with the error text in expandable.error."
frontend/e2e-full/fixtures/no_plan_progress_finished.json:73:      "status": "FINISHED"
frontend/e2e-full/fixtures/no_plan_progress_finished.json:76:  "final_status": "FINISHED",
frontend/e2e-full/fixtures/no_plan_progress_finished.json:81:  "_why": "Small/local model finished without ever emitting update_plan_progress. deriveBuildProgress(events,'FINISHED') MUST reconcile all 4 steps to done. Pre-fix it returned an empty map (0/4)."

codex
Frontend replay is closer to the right shape for bug #3: fixture in, production selector out. The main limitation is it is still selector-only; it does not mount the real PlanPanel/Activity UI or assert rendered counts.
exec
/bin/bash -lc "nl -ba frontend/src/lib/buildTrace.ts | sed -n '725,820p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   725	export function deriveLiveSignal(
   726	  events: AgentEvent[],
   727	  status: ConversationStatus,
   728	): LiveSignal {
   729	  // Cluster 6: liveness is no longer RUNNING-only. During gate/await states the
   730	  // bar narrates what the agent is waiting ON, so the surface never reads as
   731	  // frozen between turns or while a gate is open.
   732	  if (status === "WAITING_FOR_CONFIRMATION")
   733	    return { kind: "waiting_for_you", label: "Waiting for you to approve an action" };
   734	  if (status === "AWAITING_PLAN_APPROVAL")
   735	    return { kind: "waiting_for_you", label: "Waiting for you to review the plan" };
   736	  if (status === "AWAITING_USER_DECISION")
   737	    return { kind: "waiting_for_you", label: "Waiting for your decision" };
   738	  if (status === "AWAITING_USER_QUESTION")
   739	    return { kind: "waiting_for_you", label: "The agent asked you a question" };
   740	  if (status !== "RUNNING") return { kind: "idle" };
   741	  // Walk the tail backward to classify what we're waiting on. Skip noise
   742	  // events (environment reminders) — they're meta, not the live signal.
   743	  for (let i = events.length - 1; i >= 0; i--) {
   744	    const e = events[i];
   745	    if (e.kind === "status") continue;
   746	    if (e.kind === "message" && e.source === "environment") continue;
   747	    if (e.kind === "message" && e.source === "user") {
   748	      // Just received a user message → the model is reading + composing a reply.
   749	      const text = (e.message?.content ?? "").slice(0, 80);
   750	      return { kind: "thinking_about_user_message", preview: text };
   751	    }
   752	    if (e.kind === "action" && e.tool_call) {
   753	      // Action emitted but no observation yet → tool is executing.
   754	      // (If a later observation/error existed, we'd have hit it first walking back.)
   755	      const tc = e.tool_call;
   756	      return {
   757	        kind: "tool_executing",
   758	        tool_name: tc.tool_name,
   759	        detail:
   760	          tc.tool_name === "shell"
   761	            ? String(tc.arguments.command ?? "")
   762	            : tc.tool_name.startsWith("file_")
   763	              ? String(tc.arguments.path ?? "")
   764	              : undefined,
   765	      };
   766	    }
   767	    if (e.kind === "observation" || e.kind === "agent_error") {
   768	      // Last event was a tool result; model is composing its next step.
   769	      return { kind: "composing_next_step" };
   770	    }
   771	    if (e.kind === "message" && e.source === "agent") {
   772	      // Agent just spoke; another step is in progress.
   773	      return { kind: "composing_next_step" };
   774	    }
   775	  }
   776	  // No prior signal → we just kicked off; model is reading the goal.
   777	  return { kind: "starting" };
   778	}
   779	
   780	/** Cluster 6: aggregate plan progress for a glanceable "N of M" + bar. Built
   781	 * from the same progress Map the PlanPanel already has. */
   782	export function planProgressSummary(
   783	  totalSteps: number,
   784	  progress: Map<number, StepState>,
   785	): { done: number; total: number; fraction: number } {
   786	  let done = 0;
   787	  for (const st of progress.values()) if (st === "done") done += 1;
   788	  const total = Math.max(totalSteps, 0);
   789	  return { done, total, fraction: total > 0 ? done / total : 0 };
   790	}

codex
The e2e bridge is deliberately small and low-risk, but it can be active in all Vite dev sessions, not just tests. I’ll quickly check where it is installed and whether the control attributes are stable/unique enough for the click harness.
exec
/bin/bash -lc "nl -ba frontend/src/App.tsx | sed -n '160,215p'" in /var/home/dylan/projects/disco
exec
/bin/bash -lc "nl -ba frontend/e2e-full/support/discoClick.ts | sed -n '1,280p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   160	  );
   161	  useEffect(() => {
   162	    if ((location.state as { seedTask?: string } | null)?.seedTask) {
   163	      navigate(location.pathname, { replace: true, state: null });
   164	    }
   165	    // eslint-disable-next-line react-hooks/exhaustive-deps
   166	  }, []);
   167	  return (
   168	    <AgentSurface
   169	      resumeCid={cid ?? null}
   170	      seedTask={seedRef.current}
   171	      seedContext={seedContextRef.current}
   172	    />
   173	  );
   174	}
   175	
   176	/** Resume an existing Deep Research run from /deep/:cid — opens the Deep
   177	 * Research surface pinned to a specific conversation id. The WS subscription
   178	 * replays history-then-live, so all derived state (plan, progress, report)
   179	 * recovers naturally. */
   180	function ResumeDeepResearch() {
   181	  const { cid } = useParams<{ cid: string }>();
   182	  return <DeepResearchSurface resumeCid={cid ?? null} />;
   183	}
   184	
   185	/**
   186	 * App composition root: server-state provider (TanStack Query) → router → mode
   187	 * context → the shell, which frames the routed views. The main surface (/) follows the
   188	 * mode slider (Search vs Build); /history and /settings are placeholders until their
   189	 * prompts land. Unknown routes fall back to the main surface.
   190	 */
   191	export default function App() {
   192	  return (
   193	    <QueryClientProvider client={queryClient}>
   194	      <Router>
   195	        <ModeProvider>
   196	          <ToastProvider>
   197	          {/* W6: evidence-harness bridge — inert in production without opt-in */}
   198	          <E2EBridgeMounter />
   199	          <Routes>
   200	            <Route element={<Shell />}>
   201	              <Route index element={<MainSurface />} />
   202	              <Route path="activity" element={<ActivityView />} />
   203	              <Route path="history" element={<HistoryView />} />
   204	              <Route path="projects" element={<ProjectsView />} />
   205	              <Route path="build/:cid" element={<ResumeProject />} />
   206	              <Route path="agent/:cid" element={<ResumeAgent />} />
   207	              <Route path="imported/:cid" element={<ImportedRunView />} />
   208	              <Route path="deep/:cid" element={<ResumeDeepResearch />} />
   209	              <Route path="share/:token" element={<ShareView />} />
   210	              <Route path="settings" element={<SettingsView />} />
   211	              <Route path="*" element={<Navigate to="/" replace />} />
   212	            </Route>
   213	          </Routes>
   214	          </ToastProvider>
   215	        </ModeProvider>

 succeeded in 0ms:
     1	/**
     2	 * discoClick — click wrapper with full evidence provenance.
     3	 *
     4	 * Records before-screenshot, accessible name/role/selector/route/disabled,
     5	 * performs the click, takes an after-screenshot, then optionally waits for
     6	 * declared backend evidence (WS frame type, HTTP status, event-log entries).
     7	 * Every call appends a record to ui-actions.jsonl.
     8	 *
     9	 * evidence-harness-campaign.md W12
    10	 */
    11	
    12	import * as fs from "node:fs";
    13	import * as path from "node:path";
    14	import type { Locator, Page } from "@playwright/test";
    15	import type { Recorder } from "./recorder";
    16	
    17	// ---------------------------------------------------------------------------
    18	// Types
    19	// ---------------------------------------------------------------------------
    20	
    21	/** Options controlling backend-evidence assertions after a click. */
    22	export interface DiscoClickOpts {
    23	  /**
    24	   * WS frame type to wait for (matched against `payload.type` inside
    25	   * websockets.jsonl, written by wsRecorder).  e.g. "plan_approved".
    26	   */
    27	  expectWs?: string;
    28	  /**
    29	   * HTTP response status code the next page response must carry.
    30	   * Uses `page.waitForResponse` set up BEFORE the click.
    31	   */
    32	  expectStatus?: number;
    33	  /**
    34	   * Event `event_type` values that must appear in the conversation event log
    35	   * at  GET /conversations/{cid}/events  within the poll timeout.
    36	   * Requires `agentBaseUrl` + `cid`.
    37	   */
    38	  expectEvents?: string[];
    39	  /** Agent-server root, e.g. "http://127.0.0.1:8000". Required for expectEvents. */
    40	  agentBaseUrl?: string;
    41	  /** Conversation ID to poll for event-log evidence. Required for expectEvents. */
    42	  cid?: string;
    43	  /** Optional override for the ui_action_id (defaults to a timestamp+counter slug). */
    44	  actionId?: string;
    45	}
    46	
    47	/** One record appended to ui-actions.jsonl per discoClick call. */
    48	export interface UiActionRecord {
    49	  schema: "ui-action";
    50	  ui_action_id: string;
    51	  ts_before: string;
    52	  ts_after: string;
    53	  accessible_name: string;
    54	  role: string;
    55	  selector: string;
    56	  /** Page URL at time of click. */
    57	  route: string;
    58	  disabled: boolean;
    59	  /** Path relative to the run folder. */
    60	  screenshot_before: string;
    61	  /** Path relative to the run folder. */
    62	  screenshot_after: string;
    63	  backend_evidence: BackendEvidence;
    64	}
    65	
    66	interface BackendEvidence {
    67	  ws_type_waited?: string;
    68	  ws_found?: boolean;
    69	  status_waited?: number;
    70	  status_found?: boolean;
    71	  events_waited?: string[];
    72	  events_found?: boolean;
    73	}
    74	
    75	// ---------------------------------------------------------------------------
    76	// Internal helpers
    77	// ---------------------------------------------------------------------------
    78	
    79	let _counter = 0;
    80	
    81	function makeActionId(): string {
    82	  return `action-${Date.now()}-${(++_counter).toString().padStart(4, "0")}`;
    83	}
    84	
    85	/**
    86	 * Poll `websockets.jsonl` until a WS frame whose `payload.type` equals
    87	 * `frameType` appears, or until `timeoutMs` elapses.
    88	 *
    89	 * The file is written synchronously by wsRecorder so polling it is safe.
    90	 */
    91	async function pollWsType(
    92	  filePath: string,
    93	  frameType: string,
    94	  timeoutMs = 10_000,
    95	): Promise<boolean> {
    96	  const deadline = Date.now() + timeoutMs;
    97	  while (Date.now() < deadline) {
    98	    if (fs.existsSync(filePath)) {
    99	      const text = fs.readFileSync(filePath, "utf-8");
   100	      for (const line of text.split("\n")) {
   101	        if (!line.trim()) continue;
   102	        try {
   103	          const obj = JSON.parse(line) as {
   104	            event?: string;
   105	            payload?: Record<string, unknown>;
   106	          };
   107	          if (
   108	            (obj.event === "framesent" || obj.event === "framereceived") &&
   109	            obj.payload?.["type"] === frameType
   110	          ) {
   111	            return true;
   112	          }
   113	        } catch {
   114	          // malformed line — skip
   115	        }
   116	      }
   117	    }
   118	    await new Promise<void>((r) => setTimeout(r, 200));
   119	  }
   120	  return false;
   121	}
   122	
   123	/**
   124	 * Poll GET `url` (newline-delimited JSON) until every event type in `types`
   125	 * appears as `event_type` (or `type`) in at least one record.
   126	 */
   127	async function pollEventLog(
   128	  url: string,
   129	  types: string[],
   130	  timeoutMs = 15_000,
   131	): Promise<boolean> {
   132	  const remaining = new Set(types);
   133	  const deadline = Date.now() + timeoutMs;
   134	  while (Date.now() < deadline) {
   135	    try {
   136	      const res = await fetch(url);
   137	      if (res.ok) {
   138	        const text = await res.text();
   139	        for (const line of text.split("\n")) {
   140	          if (!line.trim()) continue;
   141	          try {
   142	            const obj = JSON.parse(line) as Record<string, unknown>;
   143	            const t = obj["event_type"] ?? obj["type"];
   144	            if (typeof t === "string") remaining.delete(t);
   145	          } catch {
   146	            // skip malformed line
   147	          }
   148	        }
   149	        if (remaining.size === 0) return true;
   150	      }
   151	    } catch {
   152	      // network error — keep polling
   153	    }
   154	    await new Promise<void>((r) => setTimeout(r, 500));
   155	  }
   156	  return false;
   157	}
   158	
   159	// ---------------------------------------------------------------------------
   160	// Public API
   161	// ---------------------------------------------------------------------------
   162	
   163	/**
   164	 * Click `locator` with full click-level evidence provenance.
   165	 *
   166	 * Steps:
   167	 *  1. Before-screenshot saved to screenshots/before-{actionId}.png
   168	 *  2. Read accessible name, role, selector slug, current URL, disabled state
   169	 *  3. If `opts.expectStatus` given — arm `page.waitForResponse` BEFORE click
   170	 *  4. Perform `locator.click()`
   171	 *  5. After-screenshot saved to screenshots/after-{actionId}.png
   172	 *  6. Resolve any declared backend-evidence watchers (WS / HTTP status / event log)
   173	 *  7. Append {@link UiActionRecord} to ui-actions.jsonl
   174	 */
   175	export async function discoClick(
   176	  page: Page,
   177	  recorder: Recorder,
   178	  locator: Locator,
   179	  opts: DiscoClickOpts = {},
   180	): Promise<void> {
   181	  const actionId = opts.actionId ?? makeActionId();
   182	  const screenshotsDir = path.join(recorder.runDir, "screenshots");
   183	  fs.mkdirSync(screenshotsDir, { recursive: true });
   184	
   185	  // -------------------------------------------------------------------------
   186	  // 1 + 2: before-click evidence
   187	  // -------------------------------------------------------------------------
   188	  const tsBefore = new Date().toISOString();
   189	  const beforeFile = `before-${actionId}.png`;
   190	  await page.screenshot({
   191	    path: path.join(screenshotsDir, beforeFile),
   192	    fullPage: false,
   193	  });
   194	
   195	  // Accessible name: aria-label → text content → empty string
   196	  const ariaLabel = await locator.getAttribute("aria-label");
   197	  const textContent = ariaLabel ?? (await locator.textContent())?.trim() ?? "";
   198	
   199	  // Role: role attribute → tag name (via structural evaluate — no DOM lib needed)
   200	  const roleAttr = await locator.getAttribute("role");
   201	  const tagName = await locator.evaluate(
   202	    (el: { tagName: string }) => el.tagName.toLowerCase(),
   203	  );
   204	  const role = roleAttr ?? tagName;
   205	
   206	  // Selector slug: data-testid → data-disco-control → aria-label → tag name
   207	  const testId = await locator.getAttribute("data-testid");
   208	  const discoControl = await locator.getAttribute("data-disco-control");
   209	  const selector = testId ?? discoControl ?? ariaLabel ?? tagName;
   210	
   211	  const route = page.url();
   212	
   213	  // Disabled state
   214	  const disabledAttr = await locator.getAttribute("disabled");
   215	  const ariaDisabled = await locator.getAttribute("aria-disabled");
   216	  const disabled = disabledAttr !== null || ariaDisabled === "true";
   217	
   218	  // -------------------------------------------------------------------------
   219	  // 3: arm HTTP-status watcher BEFORE the click so responses aren't missed
   220	  // -------------------------------------------------------------------------
   221	  const backendEvidence: BackendEvidence = {};
   222	  let statusPromise: Promise<boolean> | undefined;
   223	  if (opts.expectStatus !== undefined) {
   224	    const expectedStatus = opts.expectStatus;
   225	    backendEvidence.status_waited = expectedStatus;
   226	    statusPromise = page
   227	      .waitForResponse((res) => res.status() === expectedStatus, {
   228	        timeout: 15_000,
   229	      })
   230	      .then(() => true)
   231	      .catch(() => false);
   232	  }
   233	
   234	  // -------------------------------------------------------------------------
   235	  // 4: perform the click
   236	  // -------------------------------------------------------------------------
   237	  await locator.click();
   238	
   239	  // -------------------------------------------------------------------------
   240	  // 5: after-click screenshot
   241	  // -------------------------------------------------------------------------
   242	  const tsAfter = new Date().toISOString();
   243	  const afterFile = `after-${actionId}.png`;
   244	  await page.screenshot({
   245	    path: path.join(screenshotsDir, afterFile),
   246	    fullPage: false,
   247	  });
   248	
   249	  // -------------------------------------------------------------------------
   250	  // 6: resolve backend-evidence watchers
   251	  // -------------------------------------------------------------------------
   252	
   253	  // HTTP status
   254	  if (statusPromise !== undefined) {
   255	    backendEvidence.status_found = await statusPromise;
   256	  }
   257	
   258	  // WS frame type — poll websockets.jsonl (written by wsRecorder)
   259	  if (opts.expectWs !== undefined) {
   260	    backendEvidence.ws_type_waited = opts.expectWs;
   261	    const wsFile = path.join(recorder.runDir, "websockets.jsonl");
   262	    backendEvidence.ws_found = await pollWsType(wsFile, opts.expectWs);
   263	  }
   264	
   265	  // Event-log polling
   266	  if (opts.expectEvents !== undefined && opts.expectEvents.length > 0) {
   267	    backendEvidence.events_waited = opts.expectEvents;
   268	    if (opts.agentBaseUrl !== undefined && opts.cid !== undefined) {
   269	      const base = opts.agentBaseUrl.replace(/\/$/, "");
   270	      const url = `${base}/conversations/${opts.cid}/events`;
   271	      backendEvidence.events_found = await pollEventLog(url, opts.expectEvents);
   272	    } else {
   273	      // Can't poll without base + cid
   274	      backendEvidence.events_found = false;
   275	    }
   276	  }
   277	
   278	  // -------------------------------------------------------------------------
   279	  // 7: append evidence record
   280	  // -------------------------------------------------------------------------

exec
/bin/bash -lc 'rg -n "data-disco-control" frontend/src | sort' in /var/home/dylan/projects/disco
 succeeded in 0ms:
frontend/src/components/QueryInput.tsx:110:          data-disco-control="send-message"
frontend/src/components/build/AgentStatusBar.tsx:190:            data-disco-control="resume"
frontend/src/components/build/AgentStatusBar.tsx:206:            data-disco-control="stop"
frontend/src/components/build/AgentStatusBar.tsx:230:              data-disco-control="kill-confirm"
frontend/src/components/build/AgentStatusBar.tsx:251:            data-disco-control="kill"
frontend/src/components/build/AlternativesGate.tsx:62:              data-disco-control="pick-alternative"
frontend/src/components/build/AlternativesGate.tsx:97:            data-disco-control="pick-alternative"
frontend/src/components/build/AskPanel.tsx:77:          data-disco-control="answer-question"
frontend/src/components/build/BuildSurface.tsx:96:        data-disco-control="upload-files"
frontend/src/components/build/ClarifyPanel.tsx:140:          data-disco-control="submit-clarify"
frontend/src/components/build/ConfirmationPanel.tsx:78:          data-disco-control="reject-action"
frontend/src/components/build/ConfirmationPanel.tsx:86:          data-disco-control="approve-action"
frontend/src/components/build/DeliverablePanel.tsx:107:          data-disco-control={isApp ? "open-app" : "download-artifact"}
frontend/src/components/build/DeliverablePanel.tsx:65:          data-disco-control="export-manifest"
frontend/src/components/build/DeliverablePanel.tsx:94:          data-disco-control="download-artifact"
frontend/src/components/build/PlanPanel.tsx:238:                  data-disco-control="revise-plan"
frontend/src/components/build/PlanPanel.tsx:257:                data-disco-control="approve-plan"
frontend/src/components/build/SteerInput.tsx:45:        data-disco-control="steer"
frontend/src/components/research/DeepBoundedNotice.tsx:99:                data-disco-control="retry-exhaustive"
frontend/src/components/research/DeepResearchSurface.tsx:206:                    data-disco-control="steer-research"
frontend/src/components/research/DeepResearchSurface.tsx:213:                <button type="button" onClick={r.stop} data-disco-control="stop" className={CTRL_BTN}>
frontend/src/components/research/DeepResearchSurface.tsx:217:                <button type="button" onClick={r.kill} data-disco-control="kill" className={KILL_BTN}>
frontend/src/components/research/DeepResearchSurface.tsx:226:                <button type="button" onClick={r.resume} data-disco-control="resume" className={CTRL_BTN}>
frontend/src/components/research/DeepResearchSurface.tsx:230:                <button type="button" onClick={r.kill} data-disco-control="kill" className={KILL_BTN}>
frontend/src/components/research/DeepResearchSurface.tsx:238:              <button type="button" onClick={r.retry} data-disco-control="retry" className={CTRL_BTN}>
frontend/src/components/research/DeepResearchSurface.tsx:248:                  data-disco-control="export-report-md"
frontend/src/components/research/DeepResearchSurface.tsx:264:                  data-disco-control="export-report-pdf"
frontend/src/components/research/DeepResearchSurface.tsx:284:                  data-disco-control="export-report-docx"
frontend/src/components/research/NeedMoreCard.tsx:287:              data-disco-control="export-report-md"
frontend/src/components/research/NeedMoreCard.tsx:319:              data-disco-control="export-report-pdf"
frontend/src/components/research/NeedMoreCard.tsx:375:              data-disco-control="export-report-docx"
frontend/src/components/research/NeedMoreCard.tsx:471:              data-disco-control="generate-audio"
frontend/src/components/research/NeedMoreCard.tsx:496:              data-disco-control="generate-audio"
frontend/src/components/research/NeedMoreCard.tsx:637:            data-disco-control="export-audio"
frontend/src/components/research/NeedMoreCard.tsx:840:          data-disco-control="build-deck"
frontend/src/components/research/ReportFollowUp.tsx:70:          data-disco-control="follow-up"
frontend/src/views/HistoryView.tsx:147:              data-disco-control="import-share"
frontend/src/views/HistoryView.tsx:268:                          data-disco-control="delete-conversation"

exec
/bin/bash -lc "nl -ba frontend/e2e-full/support/uiInventory.ts | sed -n '1,300p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	/**
     2	 * uiInventory — enumerate, classify, and track UI controls on a Playwright page.
     3	 *
     4	 * `enumerateControls(page)` queries every interactive element and classifies
     5	 * each into one of five kinds:
     6	 *
     7	 *   backend-command  Has data-disco-control OR matches known action names →
     8	 *                    causes an HTTP/WS call.  ONLY these require four-truth
     9	 *                    backend evidence; the coverage gate fires only for them.
    10	 *   navigation       <a href="..."> — routes the user elsewhere.
    11	 *   form-input       input / textarea / select / [role=textbox] — captures data.
    12	 *   local-ui-only    Visible interactive element with no known backend effect
    13	 *                    (tabs, toggles, disclosure triggers, etc.).
    14	 *   disabled         Has [disabled] or [aria-disabled=true]; skipped in coverage.
    15	 *
    16	 * `HitMap` tracks which controls were seen vs. clicked.
    17	 * `assertCoverage(allowlist)` fails if any enabled+visible backend-command
    18	 * control was discovered but never clicked (unless allowlisted with a reason).
    19	 *
    20	 * evidence-harness-campaign.md W12
    21	 */
    22	
    23	import type { Page } from "@playwright/test";
    24	
    25	// ---------------------------------------------------------------------------
    26	// Types
    27	// ---------------------------------------------------------------------------
    28	
    29	/** Classification of a UI control. */
    30	export type ControlKind =
    31	  | "backend-command"
    32	  | "navigation"
    33	  | "form-input"
    34	  | "local-ui-only"
    35	  | "disabled";
    36	
    37	/** One enumerated control on a surface. */
    38	export interface Control {
    39	  /** URL path at enumeration time, e.g. "/build/abc123". */
    40	  route: string;
    41	  /** Surface derived from the route, e.g. "build", "home", "settings". */
    42	  surface: string;
    43	  /** ARIA role attribute or HTML tag name. */
    44	  role: string;
    45	  /** Best accessible name: aria-label → text content → empty. */
    46	  name: string;
    47	  /** Whether the control is interactive (not disabled). */
    48	  enabled: boolean;
    49	  /** Whether the control is currently visible in the viewport. */
    50	  visible: boolean;
    51	  /** data-testid attribute, or null. */
    52	  testId: string | null;
    53	  /**
    54	   * Stable identifier: data-disco-control → data-testid → kebab(name+role).
    55	   * Used as the key in HitMap.
    56	   */
    57	  controlId: string;
    58	  kind: ControlKind;
    59	}
    60	
    61	// ---------------------------------------------------------------------------
    62	// Classification helpers
    63	// ---------------------------------------------------------------------------
    64	
    65	/**
    66	 * Heuristic action-name patterns that strongly imply a backend call.
    67	 * Matched against the lowercased text content / aria-label.
    68	 */
    69	const BACKEND_COMMAND_PATTERNS: RegExp[] = [
    70	  /\bsubmit\b/,
    71	  /\bsend\b/,
    72	  /\bsearch\b/,
    73	  /\bapprove\b/,
    74	  /\breject\b/,
    75	  /\bcancel\b/,
    76	  /\bdelete\b/,
    77	  /\bremove\b/,
    78	  /\bsave\b/,
    79	  /\bcreate\b/,
    80	  /\bnew\b/,
    81	  /\bstart\b/,
    82	  /\bstop\b/,
    83	  /\bpause\b/,
    84	  /\bresume\b/,
    85	  /\bconfirm\b/,
    86	  /\brun\b/,
    87	  /\bexport\b/,
    88	  /\bdownload\b/,
    89	  /\bupload\b/,
    90	  /\bimport\b/,
    91	  /\bgenerate\b/,
    92	  /\bbuild\b/,
    93	  /\bdeploy\b/,
    94	  /\bpublish\b/,
    95	  /\bconnect\b/,
    96	  /\bdisconnect\b/,
    97	  /\brefresh\b/,
    98	  /\breload\b/,
    99	  /\bkill\b/,
   100	  /\breplan\b/,
   101	  /\bshare\b/,
   102	  /\brevoke\b/,
   103	];
   104	
   105	/**
   106	 * Classify a raw element into a {@link ControlKind}.
   107	 *
   108	 * Priority:
   109	 *  1. disabled / aria-disabled → "disabled"
   110	 *  2. has data-disco-control → "backend-command"
   111	 *  3. <a href="..."> → "navigation"
   112	 *  4. input / textarea / select / role=textbox → "form-input"
   113	 *  5. role=tab / role=radio / role=switch → "local-ui-only" (state-only)
   114	 *  6. name matches BACKEND_COMMAND_PATTERNS → "backend-command"
   115	 *  7. fallback → "local-ui-only"
   116	 */
   117	function classifyKind(
   118	  tagName: string,
   119	  role: string,
   120	  name: string,
   121	  discoControl: string | null,
   122	  href: string | null,
   123	  isDisabled: boolean,
   124	): ControlKind {
   125	  if (isDisabled) return "disabled";
   126	  if (discoControl !== null) return "backend-command";
   127	  if (tagName === "a" && href !== null) return "navigation";
   128	  if (
   129	    tagName === "input" ||
   130	    tagName === "textarea" ||
   131	    tagName === "select" ||
   132	    role === "textbox"
   133	  ) {
   134	    return "form-input";
   135	  }
   136	  if (role === "tab" || role === "radio" || role === "switch") {
   137	    return "local-ui-only";
   138	  }
   139	  const lower = name.toLowerCase();
   140	  if (BACKEND_COMMAND_PATTERNS.some((p) => p.test(lower))) {
   141	    return "backend-command";
   142	  }
   143	  return "local-ui-only";
   144	}
   145	
   146	/** Derive a surface name from a URL path. */
   147	function surfaceFromRoute(route: string): string {
   148	  try {
   149	    const pathname = new URL(route).pathname;
   150	    const segment = pathname.split("/").filter(Boolean)[0];
   151	    return segment ?? "home";
   152	  } catch {
   153	    return "unknown";
   154	  }
   155	}
   156	
   157	/** Build a stable controlId from the available attributes. */
   158	function makeControlId(
   159	  discoControl: string | null,
   160	  testId: string | null,
   161	  name: string,
   162	  role: string,
   163	): string {
   164	  if (discoControl !== null && discoControl.length > 0) return `disco:${discoControl}`;
   165	  if (testId !== null && testId.length > 0) return `tid:${testId}`;
   166	  const slug = `${name}-${role}`
   167	    .toLowerCase()
   168	    .replace(/[^a-z0-9]+/g, "-")
   169	    .replace(/^-|-$/g, "")
   170	    .slice(0, 60);
   171	  return `gen:${slug || "unnamed"}`;
   172	}
   173	
   174	// ---------------------------------------------------------------------------
   175	// Raw element shape returned from page.evaluate / locator.evaluate
   176	// ---------------------------------------------------------------------------
   177	
   178	interface RawAttrs {
   179	  tagName: string;
   180	  roleAttr: string | null;
   181	  ariaLabel: string | null;
   182	  dataTestId: string | null;
   183	  discoControl: string | null;
   184	  disabledAttr: boolean;
   185	  ariaDisabled: string | null;
   186	  href: string | null;
   187	}
   188	
   189	// ---------------------------------------------------------------------------
   190	// enumerateControls
   191	// ---------------------------------------------------------------------------
   192	
   193	/**
   194	 * CSS selector covering all interactive elements the harness cares about.
   195	 * Deduplication is handled after enumeration via controlId.
   196	 */
   197	const CONTROL_SELECTOR = [
   198	  "button",
   199	  "a[href]",
   200	  "input:not([type='hidden'])",
   201	  "textarea",
   202	  "select",
   203	  "[role='button']",
   204	  "[role='switch']",
   205	  "[role='radio']",
   206	  "[role='tab']",
   207	  "[role='menuitem']",
   208	  "[role='textbox']",
   209	  "[data-testid]",
   210	  "[data-disco-control]",
   211	].join(", ");
   212	
   213	/**
   214	 * Enumerate all interactive controls on `page` and classify each.
   215	 *
   216	 * Skips duplicate controlIds (keeps the first occurrence).
   217	 * Does NOT filter by visibility here — callers should check `ctrl.visible`.
   218	 */
   219	export async function enumerateControls(page: Page): Promise<Control[]> {
   220	  const route = page.url();
   221	  const surface = surfaceFromRoute(route);
   222	
   223	  const locators = await page.locator(CONTROL_SELECTOR).all();
   224	
   225	  const seen = new Set<string>();
   226	  const controls: Control[] = [];
   227	
   228	  for (const loc of locators) {
   229	    // Single evaluate call per element to minimise round-trips
   230	    const attrs: RawAttrs = await loc.evaluate((rawEl) => {
   231	      // Cast through unknown to avoid needing DOM lib in tsconfig
   232	      const el = rawEl as unknown as {
   233	        tagName: string;
   234	        getAttribute(name: string): string | null;
   235	        hasAttribute(name: string): boolean;
   236	      };
   237	      return {
   238	        tagName: el.tagName.toLowerCase(),
   239	        roleAttr: el.getAttribute("role"),
   240	        ariaLabel: el.getAttribute("aria-label"),
   241	        dataTestId: el.getAttribute("data-testid"),
   242	        discoControl: el.getAttribute("data-disco-control"),
   243	        disabledAttr: el.hasAttribute("disabled"),
   244	        ariaDisabled: el.getAttribute("aria-disabled"),
   245	        href: el.getAttribute("href"),
   246	      };
   247	    });
   248	
   249	    const isDisabled =
   250	      attrs.disabledAttr || attrs.ariaDisabled === "true";
   251	
   252	    const role = attrs.roleAttr ?? attrs.tagName;
   253	    const textContent = (await loc.textContent())?.trim() ?? "";
   254	    const name = attrs.ariaLabel ?? textContent;
   255	    const controlId = makeControlId(
   256	      attrs.discoControl,
   257	      attrs.dataTestId,
   258	      name,
   259	      role,
   260	    );
   261	
   262	    // Skip duplicates (same controlId already collected)
   263	    if (seen.has(controlId)) continue;
   264	    seen.add(controlId);
   265	
   266	    const visible = await loc.isVisible();
   267	
   268	    const kind = classifyKind(
   269	      attrs.tagName,
   270	      role,
   271	      name,
   272	      attrs.discoControl,
   273	      attrs.href,
   274	      isDisabled,
   275	    );
   276	
   277	    controls.push({
   278	      route,
   279	      surface,
   280	      role,
   281	      name,
   282	      enabled: !isDisabled,
   283	      visible,
   284	      testId: attrs.dataTestId,
   285	      controlId,
   286	      kind,
   287	    });
   288	  }
   289	
   290	  return controls;
   291	}
   292	
   293	// ---------------------------------------------------------------------------
   294	// HitMap
   295	// ---------------------------------------------------------------------------
   296	
   297	/**
   298	 * Tracks which controls were enumerated (seen) vs. actually clicked (hit).
   299	 *
   300	 * Feed every `enumerateControls` result through `record()`.

codex
The control IDs are not globally unique by design, and duplicates are common (`resume`, `stop`, `kill`, `generate-audio`, etc.). That is workable if scoped by route/surface, but the current inventory deduplicates only by controlId, so duplicate visible controls on the same page can be dropped from coverage.
exec
/bin/bash -lc "nl -ba frontend/e2e-full/support/discoClick.ts | sed -n '278,360p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   278	  // -------------------------------------------------------------------------
   279	  // 7: append evidence record
   280	  // -------------------------------------------------------------------------
   281	  const record: UiActionRecord = {
   282	    schema: "ui-action",
   283	    ui_action_id: actionId,
   284	    ts_before: tsBefore,
   285	    ts_after: tsAfter,
   286	    accessible_name: textContent,
   287	    role,
   288	    selector,
   289	    route,
   290	    disabled,
   291	    screenshot_before: path.join("screenshots", beforeFile),
   292	    screenshot_after: path.join("screenshots", afterFile),
   293	    backend_evidence: backendEvidence,
   294	  };
   295	  recorder.write("ui-actions.jsonl", record);
   296	}

exec
/bin/bash -lc "nl -ba frontend/e2e-full/support/uiInventory.ts | sed -n '300,430p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   300	 * Feed every `enumerateControls` result through `record()`.
   301	 * Call `hit(controlId)` after each `discoClick`.
   302	 * Call `assertCoverage(allowlist)` at end-of-suite to enforce the coverage gate.
   303	 */
   304	export class HitMap {
   305	  private readonly _seen = new Map<string, Control>();
   306	  private readonly _clicked = new Set<string>();
   307	
   308	  /** Record a discovered control. Safe to call multiple times with the same id. */
   309	  record(control: Control): void {
   310	    if (!this._seen.has(control.controlId)) {
   311	      this._seen.set(control.controlId, control);
   312	    }
   313	  }
   314	
   315	  /** Record a set of controls (convenience for the array from enumerateControls). */
   316	  recordAll(controls: Control[]): void {
   317	    for (const c of controls) this.record(c);
   318	  }
   319	
   320	  /** Mark a control as exercised by a click. */
   321	  hit(controlId: string): void {
   322	    this._clicked.add(controlId);
   323	  }
   324	
   325	  /** All controls discovered so far. */
   326	  get allSeen(): Control[] {
   327	    return [...this._seen.values()];
   328	  }
   329	
   330	  /** All clicked control IDs. */
   331	  get allClicked(): ReadonlySet<string> {
   332	    return this._clicked;
   333	  }
   334	
   335	  /**
   336	   * Fail if any enabled, visible `backend-command` control was discovered
   337	   * but never clicked, unless its controlId appears in `allowlist`.
   338	   *
   339	   * @param allowlist  Map from controlId → reason string (human-readable).
   340	   * @throws {Error}   Listing every un-exercised control with its surface/name.
   341	   */
   342	  assertCoverage(allowlist: Record<string, string> = {}): void {
   343	    const failures: string[] = [];
   344	    for (const [id, ctrl] of this._seen) {
   345	      if (ctrl.kind !== "backend-command") continue;
   346	      if (!ctrl.enabled || !ctrl.visible) continue;
   347	      if (id in allowlist) continue;
   348	      if (!this._clicked.has(id)) {
   349	        failures.push(
   350	          `  [${ctrl.surface}] ${id}  name="${ctrl.name}"  role=${ctrl.role}`,
   351	        );
   352	      }
   353	    }
   354	    if (failures.length > 0) {
   355	      throw new Error(
   356	        `Coverage gate: ${failures.length} backend-command control(s) discovered but never exercised:\n${failures.join("\n")}`,
   357	      );
   358	    }
   359	  }
   360	}

exec
/bin/bash -lc "rg -n \"expectEvents|events_found|event_type|/events|discoClick\\(|assertCoverage|HitMap|controlId\" frontend/e2e-full -S" in /var/home/dylan/projects/disco
 succeeded in 0ms:
frontend/e2e-full/scenarios/click-provenance.smoke.spec.ts:13: *  5  event-log rows appended      → conversations/<cid>/events.jsonl (live backend)
frontend/e2e-full/scenarios/click-provenance.smoke.spec.ts:35:  HitMap,
frontend/e2e-full/scenarios/click-provenance.smoke.spec.ts:63:  hitMap: HitMap,
frontend/e2e-full/scenarios/click-provenance.smoke.spec.ts:100:    const hitMap = new HitMap();
frontend/e2e-full/scenarios/click-provenance.smoke.spec.ts:106:    // Every control must have a non-empty controlId and a valid kind.
frontend/e2e-full/scenarios/click-provenance.smoke.spec.ts:115:      expect(ctrl.controlId.length).toBeGreaterThan(0);
frontend/e2e-full/scenarios/click-provenance.smoke.spec.ts:139:          : page.locator(`[data-disco-control="${clickTarget.controlId.replace("disco:", "")}"]`);
frontend/e2e-full/scenarios/click-provenance.smoke.spec.ts:144:        !clickTarget.controlId.startsWith("disco:")
frontend/e2e-full/scenarios/click-provenance.smoke.spec.ts:155:      await discoClick(page, recorder, locator, {
frontend/e2e-full/scenarios/click-provenance.smoke.spec.ts:156:        // No expectWs / expectStatus / expectEvents for the smoke run;
frontend/e2e-full/scenarios/click-provenance.smoke.spec.ts:161:      hitMap.hit(clickTarget.controlId);
frontend/e2e-full/support/assertions.ts:23:  event_type?: string;
frontend/e2e-full/support/assertions.ts:103: * Fetches  GET /conversations/{cid}/events  from `agentBaseUrl`.
frontend/e2e-full/support/assertions.ts:108: * @param expected      Ordered list of event_type strings that must appear.
frontend/e2e-full/support/assertions.ts:119:  const url = `${base}/conversations/${cid}/events`;
frontend/e2e-full/support/assertions.ts:132:    (r) => (r["event_type"] ?? r["type"] ?? "") as string,
frontend/e2e-full/support/uiInventory.ts:16: * `HitMap` tracks which controls were seen vs. clicked.
frontend/e2e-full/support/uiInventory.ts:17: * `assertCoverage(allowlist)` fails if any enabled+visible backend-command
frontend/e2e-full/support/uiInventory.ts:55:   * Used as the key in HitMap.
frontend/e2e-full/support/uiInventory.ts:57:  controlId: string;
frontend/e2e-full/support/uiInventory.ts:157:/** Build a stable controlId from the available attributes. */
frontend/e2e-full/support/uiInventory.ts:195: * Deduplication is handled after enumeration via controlId.
frontend/e2e-full/support/uiInventory.ts:216: * Skips duplicate controlIds (keeps the first occurrence).
frontend/e2e-full/support/uiInventory.ts:255:    const controlId = makeControlId(
frontend/e2e-full/support/uiInventory.ts:262:    // Skip duplicates (same controlId already collected)
frontend/e2e-full/support/uiInventory.ts:263:    if (seen.has(controlId)) continue;
frontend/e2e-full/support/uiInventory.ts:264:    seen.add(controlId);
frontend/e2e-full/support/uiInventory.ts:285:      controlId,
frontend/e2e-full/support/uiInventory.ts:294:// HitMap
frontend/e2e-full/support/uiInventory.ts:301: * Call `hit(controlId)` after each `discoClick`.
frontend/e2e-full/support/uiInventory.ts:302: * Call `assertCoverage(allowlist)` at end-of-suite to enforce the coverage gate.
frontend/e2e-full/support/uiInventory.ts:304:export class HitMap {
frontend/e2e-full/support/uiInventory.ts:310:    if (!this._seen.has(control.controlId)) {
frontend/e2e-full/support/uiInventory.ts:311:      this._seen.set(control.controlId, control);
frontend/e2e-full/support/uiInventory.ts:321:  hit(controlId: string): void {
frontend/e2e-full/support/uiInventory.ts:322:    this._clicked.add(controlId);
frontend/e2e-full/support/uiInventory.ts:337:   * but never clicked, unless its controlId appears in `allowlist`.
frontend/e2e-full/support/uiInventory.ts:339:   * @param allowlist  Map from controlId → reason string (human-readable).
frontend/e2e-full/support/uiInventory.ts:342:  assertCoverage(allowlist: Record<string, string> = {}): void {
frontend/e2e-full/support/discoClick.ts:34:   * Event `event_type` values that must appear in the conversation event log
frontend/e2e-full/support/discoClick.ts:35:   * at  GET /conversations/{cid}/events  within the poll timeout.
frontend/e2e-full/support/discoClick.ts:38:  expectEvents?: string[];
frontend/e2e-full/support/discoClick.ts:39:  /** Agent-server root, e.g. "http://127.0.0.1:8000". Required for expectEvents. */
frontend/e2e-full/support/discoClick.ts:41:  /** Conversation ID to poll for event-log evidence. Required for expectEvents. */
frontend/e2e-full/support/discoClick.ts:72:  events_found?: boolean;
frontend/e2e-full/support/discoClick.ts:125: * appears as `event_type` (or `type`) in at least one record.
frontend/e2e-full/support/discoClick.ts:143:            const t = obj["event_type"] ?? obj["type"];
frontend/e2e-full/support/discoClick.ts:175:export async function discoClick(
frontend/e2e-full/support/discoClick.ts:266:  if (opts.expectEvents !== undefined && opts.expectEvents.length > 0) {
frontend/e2e-full/support/discoClick.ts:267:    backendEvidence.events_waited = opts.expectEvents;
frontend/e2e-full/support/discoClick.ts:270:      const url = `${base}/conversations/${opts.cid}/events`;
frontend/e2e-full/support/discoClick.ts:271:      backendEvidence.events_found = await pollEventLog(url, opts.expectEvents);
frontend/e2e-full/support/discoClick.ts:274:      backendEvidence.events_found = false;
frontend/e2e-full/support/artifactCollector.ts:32:  { file: "events.jsonl", urlPath: "/conversations/{cid}/events" },

exec
/bin/bash -lc "nl -ba frontend/e2e-full/scenarios/click-provenance.smoke.spec.ts | sed -n '1,230p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	/**
     2	 * click-provenance.smoke.spec.ts — W12 smoke spec.
     3	 *
     4	 * Demonstrates that discoClick produces all 10 evidence records for one
     5	 * interaction and that uiInventory enumerates + classifies the home-screen
     6	 * controls correctly.
     7	 *
     8	 * The 10 evidence records (from evidence-harness-campaign.md):
     9	 *  1  UI control discovered        → controls-discovered.json
    10	 *  2  UI action performed          → ui-actions.jsonl entry
    11	 *  3  HTTP/WS frame caused         → network.jsonl (passive listener fires on navigate)
    12	 *  4  conversation_id captured     → conversations/<cid>/ folder (live backend only)
    13	 *  5  event-log rows appended      → conversations/<cid>/events.jsonl (live backend)
    14	 *  6  state transition observed    → conversations/<cid>/state.final.json (live backend)
    15	 *  7  DISCO_INSPECT trace          → conversations/<cid>/inspect-trace.json (live backend)
    16	 *  8  sandbox/tool side effect     → workspace-manifest.json (live backend)
    17	 *  9  final UI renders result      → screenshots/after-*.png
    18	 * 10  artifact saved               → dossier folder with all evidence files
    19	 *
    20	 * Records 4–8 require a live backend workflow; in this smoke run they are
    21	 * verified structurally (the infrastructure to produce them is wired up and
    22	 * the correct helper functions are importable + callable).
    23	 *
    24	 * Run on VM 201 by the orchestrator — NOT locally (Playwright OOMs on workstation).
    25	 *
    26	 * evidence-harness-campaign.md W12
    27	 */
    28	
    29	import * as fs from "node:fs";
    30	import * as path from "node:path";
    31	import { expect, test } from "../fixtures/discoHarness";
    32	import { discoClick } from "../support/discoClick";
    33	import {
    34	  enumerateControls,
    35	  HitMap,
    36	  type Control,
    37	} from "../support/uiInventory";
    38	import {
    39	  expectEventSequence,
    40	  expectInspectTrace,
    41	  expectArtifactReadable,
    42	} from "../support/assertions";
    43	
    44	// ---------------------------------------------------------------------------
    45	// Helpers
    46	// ---------------------------------------------------------------------------
    47	
    48	/** Write controls to controls-discovered.json in the run folder. */
    49	function writeControlsDiscovered(
    50	  runDir: string,
    51	  controls: Control[],
    52	): void {
    53	  fs.writeFileSync(
    54	    path.join(runDir, "controls-discovered.json"),
    55	    JSON.stringify(controls, null, 2) + "\n",
    56	    "utf-8",
    57	  );
    58	}
    59	
    60	/** Write hit-map snapshot to controls-hit.json in the run folder. */
    61	function writeControlsHit(
    62	  runDir: string,
    63	  hitMap: HitMap,
    64	): void {
    65	  fs.writeFileSync(
    66	    path.join(runDir, "controls-hit.json"),
    67	    JSON.stringify(
    68	      {
    69	        seen: hitMap.allSeen.length,
    70	        clicked: [...hitMap.allClicked],
    71	        backend_command_seen: hitMap.allSeen.filter(
    72	          (c) => c.kind === "backend-command",
    73	        ).length,
    74	      },
    75	      null,
    76	      2,
    77	    ) + "\n",
    78	    "utf-8",
    79	  );
    80	}
    81	
    82	// ---------------------------------------------------------------------------
    83	// Test
    84	// ---------------------------------------------------------------------------
    85	
    86	test(
    87	  "click-provenance smoke — all 10 evidence records, home-screen control inventory",
    88	  async ({ page, recorder }) => {
    89	    // -----------------------------------------------------------------------
    90	    // Navigate to home (record 3: network.jsonl captures the HTTP requests)
    91	    // -----------------------------------------------------------------------
    92	    await page.goto("/");
    93	
    94	    // -----------------------------------------------------------------------
    95	    // Record 1: enumerate + classify home-screen controls
    96	    // -----------------------------------------------------------------------
    97	    const controls = await enumerateControls(page);
    98	    writeControlsDiscovered(recorder.runDir, controls);
    99	
   100	    const hitMap = new HitMap();
   101	    hitMap.recordAll(controls);
   102	
   103	    // The home screen must expose at least one discoverable control.
   104	    expect(controls.length).toBeGreaterThan(0);
   105	
   106	    // Every control must have a non-empty controlId and a valid kind.
   107	    const validKinds = new Set([
   108	      "backend-command",
   109	      "navigation",
   110	      "form-input",
   111	      "local-ui-only",
   112	      "disabled",
   113	    ]);
   114	    for (const ctrl of controls) {
   115	      expect(ctrl.controlId.length).toBeGreaterThan(0);
   116	      expect(validKinds.has(ctrl.kind)).toBe(true);
   117	    }
   118	
   119	    // -----------------------------------------------------------------------
   120	    // Record 2 + 9: discoClick on a visible navigation or command control
   121	    //
   122	    // Strategy: prefer a navigation link (safe, no backend call needed) so
   123	    // the smoke spec is self-contained even without a live backend.  Falls back
   124	    // to the first visible enabled control of any kind.
   125	    // -----------------------------------------------------------------------
   126	    const clickTarget =
   127	      controls.find(
   128	        (c) =>
   129	          (c.kind === "navigation" || c.kind === "backend-command") &&
   130	          c.visible &&
   131	          c.enabled,
   132	      ) ?? controls.find((c) => c.visible && c.enabled);
   133	
   134	    if (clickTarget !== undefined) {
   135	      // Locate the element by its most stable available attribute
   136	      let locator =
   137	        clickTarget.testId !== null
   138	          ? page.getByTestId(clickTarget.testId)
   139	          : page.locator(`[data-disco-control="${clickTarget.controlId.replace("disco:", "")}"]`);
   140	
   141	      // Final fallback: accessible role + name
   142	      if (
   143	        clickTarget.testId === null &&
   144	        !clickTarget.controlId.startsWith("disco:")
   145	      ) {
   146	        locator =
   147	          clickTarget.name.length > 0
   148	            ? page.getByRole(
   149	                clickTarget.role as Parameters<typeof page.getByRole>[0],
   150	                { name: clickTarget.name },
   151	              )
   152	            : page.locator(clickTarget.role).first();
   153	      }
   154	
   155	      await discoClick(page, recorder, locator, {
   156	        // No expectWs / expectStatus / expectEvents for the smoke run;
   157	        // a live backend workflow test (W13) adds those.
   158	        actionId: "smoke-click-01",
   159	      });
   160	
   161	      hitMap.hit(clickTarget.controlId);
   162	    }
   163	
   164	    // -----------------------------------------------------------------------
   165	    // Write hit-map snapshot
   166	    // -----------------------------------------------------------------------
   167	    writeControlsHit(recorder.runDir, hitMap);
   168	
   169	    // -----------------------------------------------------------------------
   170	    // Record 10: write dossier (timeline.md + index.html + manifest.json)
   171	    // -----------------------------------------------------------------------
   172	    recorder.dossier();
   173	
   174	    // -----------------------------------------------------------------------
   175	    // Assertions on the evidence folder (records 1, 2, 9, 10)
   176	    // -----------------------------------------------------------------------
   177	
   178	    // Record 1: controls-discovered.json
   179	    const discoveredPath = path.join(recorder.runDir, "controls-discovered.json");
   180	    expect(fs.existsSync(discoveredPath)).toBe(true);
   181	    const discovered = JSON.parse(
   182	      fs.readFileSync(discoveredPath, "utf-8"),
   183	    ) as Control[];
   184	    expect(discovered.length).toBeGreaterThan(0);
   185	
   186	    // Record 2: ui-actions.jsonl has at least one entry
   187	    const uiActionsPath = path.join(recorder.runDir, "ui-actions.jsonl");
   188	    if (clickTarget !== undefined) {
   189	      expect(fs.existsSync(uiActionsPath)).toBe(true);
   190	      const uiActionsText = fs.readFileSync(uiActionsPath, "utf-8");
   191	      const uiActions = uiActionsText
   192	        .split("\n")
   193	        .filter((l) => l.trim())
   194	        .map((l) => JSON.parse(l) as { schema: string; ui_action_id: string });
   195	      expect(uiActions.length).toBeGreaterThan(0);
   196	      expect(uiActions[0]?.schema).toBe("ui-action");
   197	      expect(uiActions[0]?.ui_action_id).toBe("smoke-click-01");
   198	    }
   199	
   200	    // Record 9: before + after screenshots exist
   201	    if (clickTarget !== undefined) {
   202	      const screenshotsDir = path.join(recorder.runDir, "screenshots");
   203	      expect(fs.existsSync(screenshotsDir)).toBe(true);
   204	      const shots = fs.readdirSync(screenshotsDir);
   205	      const beforeShot = shots.find((f) => f.startsWith("before-smoke-click-01"));
   206	      const afterShot = shots.find((f) => f.startsWith("after-smoke-click-01"));
   207	      expect(beforeShot).toBeDefined();
   208	      expect(afterShot).toBeDefined();
   209	    }
   210	
   211	    // Record 3: network.jsonl was written by the passive listener (navigate → requests)
   212	    const networkPath = path.join(recorder.runDir, "network.jsonl");
   213	    expect(fs.existsSync(networkPath)).toBe(true);
   214	    const networkLines = fs
   215	      .readFileSync(networkPath, "utf-8")
   216	      .split("\n")
   217	      .filter((l) => l.trim());
   218	    expect(networkLines.length).toBeGreaterThan(0);
   219	
   220	    // Record 10: dossier files
   221	    expect(fs.existsSync(path.join(recorder.runDir, "manifest.json"))).toBe(true);
   222	    expect(fs.existsSync(path.join(recorder.runDir, "timeline.md"))).toBe(true);
   223	    expect(fs.existsSync(path.join(recorder.runDir, "index.html"))).toBe(true);
   224	
   225	    // controls-hit.json
   226	    expect(
   227	      fs.existsSync(path.join(recorder.runDir, "controls-hit.json")),
   228	    ).toBe(true);
   229	
   230	    // -----------------------------------------------------------------------

codex
There is another concrete bug in `discoClick`: event polling treats `/conversations/{cid}/events` as newline-delimited JSON, but the backend returns a JSON object with an `events` array. Any `expectEvents` check will time out false even when the event is present.
exec
/bin/bash -lc "nl -ba frontend/e2e-full/scenarios/click-provenance.smoke.spec.ts | sed -n '230,330p' && nl -ba frontend/e2e-full/support/assertions.ts | sed -n '90,155p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   230	    // -----------------------------------------------------------------------
   231	    // Structural proof: records 4–8 infrastructure is importable + callable
   232	    //
   233	    // These helpers will throw on a real call without a live backend; here we
   234	    // only verify they are correctly typed and exported.  Live assertions are
   235	    // added in W13 scenario specs.
   236	    // -----------------------------------------------------------------------
   237	    expect(typeof expectEventSequence).toBe("function");
   238	    expect(typeof expectInspectTrace).toBe("function");
   239	    expect(typeof expectArtifactReadable).toBe("function");
   240	
   241	    // -----------------------------------------------------------------------
   242	    // Control-kind breakdown logged for human inspection
   243	    // -----------------------------------------------------------------------
   244	    const byKind: Record<string, number> = {};
   245	    for (const c of controls) {
   246	      byKind[c.kind] = (byKind[c.kind] ?? 0) + 1;
   247	    }
   248	    // Write a summary so the timeline includes it
   249	    recorder.write("ui-actions.jsonl", {
   250	      schema: "control-kind-summary",
   251	      route: page.url(),
   252	      total: controls.length,
   253	      by_kind: byKind,
   254	      ts: new Date().toISOString(),
   255	    });
   256	  },
   257	);
    90	    );
    91	  }
    92	  return res.text();
    93	}
    94	
    95	// ---------------------------------------------------------------------------
    96	// Public API
    97	// ---------------------------------------------------------------------------
    98	
    99	/**
   100	 * Assert that the conversation event log contains all `expected` event types,
   101	 * appearing in the given order (subsequence, not contiguous).
   102	 *
   103	 * Fetches  GET /conversations/{cid}/events  from `agentBaseUrl`.
   104	 * The endpoint may return newline-delimited JSON or a JSON array; both are handled.
   105	 *
   106	 * @param cid           Conversation / request ID.
   107	 * @param agentBaseUrl  Agent-server root, e.g. "http://127.0.0.1:8000".
   108	 * @param expected      Ordered list of event_type strings that must appear.
   109	 * @throws {Error}      If any expected type is missing or out of order.
   110	 */
   111	export async function expectEventSequence(
   112	  cid: string,
   113	  agentBaseUrl: string,
   114	  expected: string[],
   115	): Promise<void> {
   116	  if (expected.length === 0) return;
   117	
   118	  const base = stripTrailingSlash(agentBaseUrl);
   119	  const url = `${base}/conversations/${cid}/events`;
   120	  const text = await fetchText(url);
   121	
   122	  // Tolerate both JSONL and JSON-array responses
   123	  let rows: EventRow[];
   124	  const trimmed = text.trim();
   125	  if (trimmed.startsWith("[")) {
   126	    rows = JSON.parse(trimmed) as EventRow[];
   127	  } else {
   128	    rows = parseJsonl(trimmed);
   129	  }
   130	
   131	  const eventTypes = rows.map(
   132	    (r) => (r["event_type"] ?? r["type"] ?? "") as string,
   133	  );
   134	
   135	  let cursor = 0;
   136	  const missing: string[] = [];
   137	  for (const want of expected) {
   138	    let found = false;
   139	    while (cursor < eventTypes.length) {
   140	      if (eventTypes[cursor] === want) {
   141	        cursor++;
   142	        found = true;
   143	        break;
   144	      }
   145	      cursor++;
   146	    }
   147	    if (!found) missing.push(want);
   148	  }
   149	
   150	  if (missing.length > 0) {
   151	    throw new Error(
   152	      `expectEventSequence [cid=${cid}]: missing event type(s) in sequence: ${missing.join(", ")}\n` +
   153	        `Observed types (${eventTypes.length}): ${eventTypes.join(", ")}`,
   154	    );
   155	  }

codex
The smoke Playwright spec explicitly says records 4–8 are only “importable + callable,” so it does not prove backend evidence yet. That is fine as a smoke, but it should not be counted as closing the evidence gap.
exec
/bin/bash -lc "nl -ba packages/agent-server/src/disco/agent_server/routes/conversations.py | sed -n '185,245p' && nl -ba packages/agent-server/src/disco/agent_server/routes/ws.py | sed -n '1,140p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   185	        # Same sandbox-liveness overlay as the WS state frame (bp-13): the HTTP
   186	        # surface (agentLive fallback, polling clients, the live specs) must
   187	        # tell the same suspended/active story as the socket.
   188	        if runtime is not None:
   189	            sstate = runtime.sandbox_state(conversation_id)
   190	            if sstate is not None:
   191	                state.extras["sandbox"] = sstate
   192	            # Surface the autonomous flag so the UI can badge the conversation.
   193	            if runtime.is_autonomous(conversation_id):
   194	                state.extras["autonomous"] = True
   195	            # ALWAYS emit assist (True or False) — the badge must reflect the CURRENT
   196	            # tier. Emitting it only-when-true let a switch to standard leave the
   197	            # frontend's preserved-on-absent value stuck on "Assist".
   198	            state.extras["assist"] = runtime.is_assist(conversation_id)
   199	        result = state.model_dump(mode="json")
   200	        # BP-15: overlay the real sandbox backend name so the UI shows the live tier.
   201	        if runtime is not None:
   202	            sbackend = runtime.sandbox_backend_name()
   203	            if sbackend is not None:
   204	                result["sandbox_backend"] = sbackend
   205	        return result
   206	
   207	    @router.get("/conversations/{conversation_id}/workspace/{path:path}")
   208	    async def workspace_file(conversation_id: str, path: str) -> Response:
   209	        """Serve immutable workspace images (screenshots + plots) from the sandbox.
   210	        Allowlist: .pmx/screenshots/ and .pmx/plots/ ONLY — never user code.
   211	        No sandbox / file absent / path outside allowlist → 404 (never 403)."""
   212	        norm = posixpath.normpath(path)
   213	        if posixpath.isabs(norm) or norm.startswith(".."):
   214	            raise HTTPException(status_code=404)
   215	        if not any(norm.startswith(pfx) for pfx in _WORKSPACE_PREFIXES):
   216	            raise HTTPException(status_code=404)
   217	        if runtime is None:
   218	            raise HTTPException(status_code=404)
   219	        session = runtime.live_session(conversation_id)
   220	        data = None
   221	        if session is not None:
   222	            try:
   223	                data = await session.read_file(norm)
   224	            except Exception:
   225	                pass
   226	
   227	        if data is None:
   228	            project_store_method = getattr(runtime, "project_store", None)
   229	            if project_store_method is not None:
   230	                ps = project_store_method()
   231	                if ps is not None:
   232	                    store_path = ps.path_for(conversation_id)
   233	                    if store_path is not None:
   234	                        disk_path = store_path / "snapshot" / norm
   235	                        # Prevent traversal out of snapshot
   236	                        try:
   237	                            disk_path = disk_path.resolve()
   238	                            snap_base = (store_path / "snapshot").resolve()
   239	                            if disk_path.is_file() and disk_path.is_relative_to(snap_base):
   240	                                data = disk_path.read_bytes()
   241	                        except Exception:
   242	                            pass
   243	
   244	        if data is None:
   245	            raise HTTPException(status_code=404)
     1	"""WebSocket routes — the conversation event/state stream (§7.1–7.4) and the
     2	read-only research-answer stream (Stage 4)."""
     3	
     4	from __future__ import annotations
     5	
     6	import asyncio
     7	import contextlib
     8	from typing import Any, cast
     9	
    10	from disco.core import (
    11	    WSClientFrame,
    12	    WSServerFrame,
    13	)
    14	from disco.core.store.sqlite import SqliteEventStore
    15	from disco.retrieval.local_encoders import EncoderUnavailable
    16	from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
    17	from pydantic import ValidationError
    18	
    19	from ..runtime import ConversationRuntime
    20	from ._common import _context_message, _user_message
    21	
    22	
    23	async def _handle_frame(
    24	    store: SqliteEventStore,
    25	    websocket: WebSocket,
    26	    conversation_id: str,
    27	    frame: WSClientFrame,
    28	    runtime: ConversationRuntime | None = None,
    29	) -> None:
    30	    """Map a client frame to the event log. Appended messages are echoed back to
    31	    every subscriber (incl. this socket) as `event` frames — the log is truth.
    32	    A message KICKS the loop (Stage 2) so a real answer streams back."""
    33	    if frame.type == "ping":
    34	        await websocket.send_json(WSServerFrame(type="pong").model_dump(mode="json"))
    35	    elif frame.type in ("send_message", "steer", "inject_source", "confirm", "reject") and (
    36	        store.conversation_origin(conversation_id) == "imported"
    37	    ):
    38	        # Imported (untrusted, read-only) conversations refuse every revive path —
    39	        # the WS is one of them (the easy-to-miss kick site). Refuse, don't kick.
    40	        # `WSServerFrame.error` is typed `dict[str, Any] | None` but the wire
    41	        # contract here is a free-form reason string — cast to keep the
    42	        # runtime value byte-identical while satisfying the type checker.
    43	        await websocket.send_json(
    44	            WSServerFrame(
    45	                type="error", error=cast("dict[str, Any]", "imported_read_only")
    46	            ).model_dump(mode="json")
    47	        )
    48	    elif frame.type == "send_message" and frame.content is not None:
    49	        # R3: an optional large `context` (e.g. a full DR report) is stored as a
    50	        # HIDDEN ENVIRONMENT message FIRST, then the short visible user message —
    51	        # so the model receives the report while the history shows only the
    52	        # one-line "Make slides for …" instead of the whole report dumped inline.
    53	        if frame.context:
    54	            await store.append(conversation_id, _context_message(frame.context))
    55	        await store.append(conversation_id, _user_message(frame.content))
    56	        if runtime is not None:
    57	            runtime.kick(conversation_id)
    58	    elif frame.type == "steer" and frame.steer_text is not None:
    59	        # D3: when a DR run is in flight for this cid, route the steer into the
    60	        # DR queue instead of kicking the agent loop. The engine drains the queue
    61	        # at each section boundary → the steer becomes a new gather leg.
    62	        # OFF-path (no DR run active): falls through to the original agent-loop kick.
    63	        if runtime is not None and conversation_id in runtime._dr_steer:
    64	            runtime._dr_steer[conversation_id].append(frame.steer_text)
    65	        else:
    66	            await store.append(conversation_id, _user_message(frame.steer_text, steer=True))
    67	            if runtime is not None:
    68	                runtime.kick(conversation_id)
    69	    elif frame.type == "inject_source" and frame.inject_source_text is not None:
    70	        # D3: inject a plaintext snippet into the DR run's corpus mid-run.
    71	        # The text is converted immediately to a Passage so the engine's
    72	        # pop_injected_sources callback can return typed Passage objects.
    73	        # URL → Passage extraction is a follow-up (async, requires extraction
    74	        # provider); only plaintext is accepted in v1.
    75	        if runtime is not None and conversation_id in runtime._dr_injected_sources:
    76	            import hashlib as _hashlib
    77	
    78	            from disco.retrieval.models import Passage as _RetrievalPassage
    79	
    80	            text = frame.inject_source_text.strip()
    81	            passage_id = "injected-" + _hashlib.sha256(text.encode()).hexdigest()[:12]
    82	            passage = _RetrievalPassage(
    83	                id=passage_id,
    84	                text=text,
    85	                source_url="user-injected",
    86	                source_title="User-injected source",
    87	            )
    88	            runtime._dr_injected_sources[conversation_id].append(passage)
    89	    elif frame.type == "confirm" and runtime is not None:
    90	        # Approve the pending action: execute exactly it, then resume (Build gate).
    91	        await runtime.confirm(conversation_id)
    92	    elif frame.type == "reject" and runtime is not None:
    93	        # Deny the pending action: record denial, resume without executing.
    94	        await runtime.reject(conversation_id)
    95	    elif frame.type == "approve_plan" and runtime is not None:
    96	        # Approve the pending plan: flip to execution mode and start building.
    97	        await runtime.approve_plan(conversation_id)
    98	    elif frame.type == "request_plan" and runtime is not None:
    99	        # (Re-)enter plan mode with the user's instruction (first plan or re-plan).
   100	        await runtime.request_plan(conversation_id, frame.content or "")
   101	    elif frame.type == "pick_alternative" and runtime is not None and frame.option_id is not None:
   102	        # User chose one of the agent's proposed alternatives (after 4+ failures).
   103	        await runtime.pick_alternative(conversation_id, frame.option_id)
   104	    elif frame.type == "pause" and runtime is not None:
   105	        # WALK-18 — cooperative pause: sets a flag the loop observes at its next
   106	        # step boundary and lands PAUSED (unlike `cancel`, no lock contention with
   107	        # the in-flight model step). `resume` re-kicks. Hard stop is kill.
   108	        await runtime.pause(conversation_id)
   109	    elif frame.type == "cancel" and runtime is not None:
   110	        # Cooperative stop (the hard kill is POST /conversations/{id}/kill).
   111	        await runtime.cancel(conversation_id)
   112	    elif frame.type == "resume" and runtime is not None:
   113	        # Continue a stopped/incomplete run — re-points at resume_conversation, the
   114	        # same mode-agnostic path the HTTP POST /resume route uses.
   115	        await runtime.resume_conversation(conversation_id)
   116	
   117	
   118	def make_ws_router(
   119	    store: SqliteEventStore, runtime: ConversationRuntime | None
   120	) -> APIRouter:
   121	    router = APIRouter()
   122	
   123	    @router.websocket("/ws/conversations/{conversation_id}")
   124	    async def conversation_ws(
   125	        websocket: WebSocket,
   126	        conversation_id: str,
   127	        last_seq: int = Query(default=0),
   128	    ) -> None:
   129	        await websocket.accept()
   130	        if runtime is not None:
   131	            runtime.on_connect(conversation_id)
   132	
   133	        # (1) On connect: one state snapshot, then replay events after last_seq,
   134	        #     then live — all via the store's subscribe (history-then-live).
   135	        state = await store.get_state(conversation_id)
   136	        # Overlay sandbox liveness so the UI can show "suspended" vs "active" badge.
   137	        if runtime is not None:
   138	            sstate = runtime.sandbox_state(conversation_id)
   139	            if sstate is not None:
   140	                state.extras["sandbox"] = sstate

codex
The event-route mismatch is worth citing precisely: both `discoClick` and `expectEventSequence` parse JSONL/array, while the route returns an object wrapper. I’m locating the route definition line for the final review.
exec
/bin/bash -lc "rg -n \"@router.get\\(\\\"/conversations/\\{conversation_id\\}/events|async def .*events|return \\{\\\"events\\\"\" packages/agent-server/src/disco/agent_server/routes/conversations.py packages/agent-server/src/disco/agent_server/routes -S" in /var/home/dylan/projects/disco
 succeeded in 0ms:
packages/agent-server/src/disco/agent_server/routes/conversations.py:173:    @router.get("/conversations/{conversation_id}/events")
packages/agent-server/src/disco/agent_server/routes/conversations.py:174:    async def get_events(
packages/agent-server/src/disco/agent_server/routes/ws.py:156:        async def pump_events() -> None:
packages/agent-server/src/disco/agent_server/routes/conversations.py:173:    @router.get("/conversations/{conversation_id}/events")
packages/agent-server/src/disco/agent_server/routes/conversations.py:174:    async def get_events(
packages/agent-server/src/disco/agent_server/routes/_common.py:158:async def _declared_artifacts(store: SqliteEventStore, conversation_id: str) -> set[str]:

exec
/bin/bash -lc "nl -ba packages/agent-server/src/disco/agent_server/routes/conversations.py | sed -n '165,184p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   165	                    "reason": "not_finished",
   166	                    "status": state.execution_status.value,
   167	                },
   168	            )
   169	        stored = await store.append(conversation_id, _user_message(body.content))
   170	        runtime.kick(conversation_id)
   171	        return {"event_id": stored.id, "seq": stored.seq, "followup": True}
   172	
   173	    @router.get("/conversations/{conversation_id}/events")
   174	    async def get_events(
   175	        conversation_id: str,
   176	        after_seq: int | None = Query(default=None),
   177	        limit: int = Query(default=100),
   178	    ) -> dict:
   179	        page = await store.paginate(conversation_id, after_seq=after_seq, limit=limit)
   180	        return page.model_dump(mode="json")
   181	
   182	    @router.get("/conversations/{conversation_id}/state")
   183	    async def get_state(conversation_id: str) -> dict:
   184	        state = await store.get_state(conversation_id)

codex
I’m checking CI last, especially whether the PR tier actually runs the boundary/validator tests and whether the VM tier exercises live artifact bytes or just smoke structure.
exec
/bin/bash -lc "nl -ba .github/workflows/evidence-harness.yml | sed -n '1,260p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	name: evidence-harness
     2	
     3	# The evidence-harness gates (campaign: docs/disco-evidence-harness-campaign.md). Two tiers,
     4	# matching the synthesized plan:
     5	#
     6	#  - pr-evidence (GitHub-hosted, REQUIRED, deterministic, NO browser): the fast input/output
     7	#    contracts that the ~1800 mocked-middle unit tests miss — boundary contracts (W1/W2/W3/W10),
     8	#    the frontend event-store REPLAY suite (W9, catches the plan-progress bug with no browser),
     9	#    the cheap artifact validators (W14a), and the disco-verify runner unit tests (W15).
    10	#
    11	#  - nightly-vm (self-hosted VM 201, advisory): the heavy/real tier — Playwright full specs
    12	#    (W11/W12), live disco-verify scenarios (W13), and render-it heavy validators (W14b).
    13	#    Runs on the dedicated browser host (Playwright OOMs the memcapped workstation), so it is
    14	#    scheduled / manual, never on the PR critical path.
    15	
    16	on:
    17	  push:
    18	  pull_request:
    19	  schedule:
    20	    - cron: "0 7 * * *" # nightly VM tier
    21	  workflow_dispatch:
    22	
    23	jobs:
    24	  pr-evidence:
    25	    name: pr-evidence (boundary + replay + validators, no browser)
    26	    runs-on: ubuntu-latest
    27	    steps:
    28	      - uses: actions/checkout@v4
    29	
    30	      - name: Install uv
    31	        uses: astral-sh/setup-uv@v5
    32	      - name: Set up Python
    33	        run: uv python install 3.13
    34	      - name: Sync Python deps
    35	        run: uv sync
    36	
    37	      # Heavy/cheap artifact validators shell out to these — install them so the
    38	      # validator tests exercise the real tools, not just their guard paths.
    39	      - name: System tools for artifact validators
    40	        run: sudo apt-get update && sudo apt-get install -y poppler-utils ffmpeg
    41	
    42	      - name: Boundary contracts (W1/W2/W3/W10)
    43	        run: uv run pytest packages -m boundary_contract -p no:cacheprovider
    44	
    45	      - name: Artifact validators + disco-verify runner (W14a/W15)
    46	        run: |
    47	          uv run pytest \
    48	            packages/tools/tests/test_artifact_validators.py \
    49	            packages/tools/tests/test_deck_validators.py \
    50	            packages/tools/tests/test_heavy_validators.py \
    51	            packages/agent-server/tests/test_disco_verify_runner.py \
    52	            packages/core/tests/test_evidence_schema.py \
    53	            packages/agent-server/tests/integration/test_scripted_smoke.py \
    54	            packages/agent-server/tests/test_debug_evidence_route.py \
    55	            -p no:cacheprovider
    56	
    57	      - name: Frontend event-store replay (W9 — no browser)
    58	        run: |
    59	          cd frontend
    60	          npm ci
    61	          npx vitest run src/lib/buildTrace.replay.test.ts
    62	
    63	  nightly-vm:
    64	    name: nightly-vm (Playwright + live scenarios + heavy validators)
    65	    # The VM 201 evidence host registered as a self-hosted runner with this label.
    66	    # Skipped on normal PRs/pushes; runs on the nightly schedule or manual dispatch.
    67	    if: github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'
    68	    runs-on: [self-hosted, vm201]
    69	    continue-on-error: true
    70	    steps:
    71	      - uses: actions/checkout@v4
    72	
    73	      - name: Install uv
    74	        uses: astral-sh/setup-uv@v5
    75	      - name: Sync Python deps
    76	        run: uv sync
    77	
    78	      # Requires the workstation dev servers (5173/8000) reachable on this host's
    79	      # localhost (the reverse SSH tunnel). The full Playwright config + live
    80	      # disco-verify scenarios produce dossiers under test-record/.
    81	      - name: Evidence-harness Playwright specs (W11/W12)
    82	        run: |
    83	          cd frontend
    84	          npm ci
    85	          npx playwright install firefox chromium
    86	          npx playwright test --config e2e-full/full.config.ts
    87	
    88	      - name: Live disco-verify scenarios (W13)
    89	        run: uv run python -m disco.agent_server.verify.scenarios_run || true
    90	
    91	      - name: Upload evidence dossiers
    92	        if: always()
    93	        uses: actions/upload-artifact@v4
    94	        with:
    95	          name: evidence-dossiers
    96	          path: |
    97	            frontend/test-record/e2e-full/**
    98	            test-record/**

exec
/bin/bash -lc "nl -ba pyproject.toml | sed -n '1,180p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	# Workspace root for the Platform (codename: disco).
     2	#
     3	# This is a *virtual* uv project: it is never built or installed itself
     4	# (`tool.uv.package = false`). It exists to (a) declare the workspace members
     5	# and (b) hold the shared dev toolchain so a single `uv sync` provisions the
     6	# whole monorepo. Each real package lives under packages/* and is its own
     7	# separately-versioned distribution, mirroring the four-layer topology in
     8	# basis-of-design.md §5 (core -> tools -> agent-server -> app-server).
     9	[project]
    10	name = "disco-workspace"
    11	version = "0"
    12	description = "Self-hosted Research & Agent platform — workspace root (not a distributable)."
    13	requires-python = ">=3.12"
    14	dependencies = []
    15	
    16	[tool.uv]
    17	package = false
    18	
    19	[tool.uv.workspace]
    20	members = ["packages/*"]
    21	
    22	[dependency-groups]
    23	# Shared dev toolchain for every member package.
    24	dev = [
    25	    "pytest>=8.2",
    26	    "pytest-asyncio>=0.24",
    27	    "ruff>=0.6",
    28	    # Architecture fitness functions (CI/pre-commit): import-linter enforces the
    29	    # package layering in .importlinter; pair with scripts/check_arch_budget.py
    30	    # (size caps) + scripts/gen_arch_diagram.py (the generated container diagram).
    31	    "import-linter>=2.0",
    32	    "basedpyright>=1.39",  # Python static type-checking (strict: zero errors tree-wide, no baseline)
    33	    "httpx>=0.27",  # Starlette TestClient (WebSocket + REST testing, headless)
    34	    "hypothesis>=6.155.2",
    35	    # BP-08 process backend: ProcessKernel launches the host `python3` kernelspec
    36	    # via jupyter_client.AsyncKernelManager — ipykernel provides that kernelspec.
    37	    # Without it every host-side kernel test fails at launch (the container image
    38	    # installs its own ipykernel in deploy/sandbox/Dockerfile).
    39	    "ipykernel>=6.29",
    40	    # BP-08 acceptance 2(e): the plots integration test renders a real matplotlib
    41	    # figure on the PROCESS backend (the container image stays matplotlib-free —
    42	    # the decided option in deploy/sandbox/Dockerfile).
    43	    "matplotlib>=3.9",
    44	    "numpy>=2.0",
    45	    # BP-04 browser daemon: the host-side integration test runs the REAL
    46	    # _browser_daemon.py as a subprocess, which imports playwright and drives
    47	    # Chromium (one-time `playwright install chromium` fetches the browser).
    48	    # The container image installs its own in deploy/sandbox/Dockerfile.
    49	    "playwright>=1.45",
    50	]
    51	
    52	[tool.pytest.ini_options]
    53	# Async tests (the EventStore is async) run without per-test decorators.
    54	asyncio_mode = "auto"
    55	testpaths = ["packages"]
    56	markers = [
    57	    "integration",
    58	    # W7: fast input/output boundary CONTRACTS (W1 sandbox-error typing, W2 task supervision,
    59	    # W3 slides/artifact, W10 guardrails). Selected into the fast PR tier: `-m boundary_contract`.
    60	    "boundary_contract",
    61	]
    62	addopts = "-q"
    63	
    64	[tool.ruff]
    65	line-length = 100
    66	target-version = "py312"
    67	
    68	[tool.ruff.lint]
    69	# Sensible defaults: pyflakes, pycodestyle, isort, bugbear, pyupgrade.
    70	select = ["E", "F", "I", "B", "UP"]
    71	# UP042 (prefer enum.StrEnum) is intentionally disabled: the event-state
    72	# contract's normative code defines enums as `(str, Enum)`, and we mirror it
    73	# verbatim so the source stays a faithful transcription of the spec.
    74	ignore = ["UP042"]
    75	
    76	[tool.pyright]
    77	include = [
    78	    "packages/core/src",
    79	    "packages/retrieval/src",
    80	    "packages/tools/src",
    81	    "packages/agent-server/src",
    82	    "packages/app-server/src",
    83	]
    84	exclude = ["**/__pycache__", "**/tests", "**/.venv", "archived God files"]
    85	pythonVersion = "3.13"
    86	typeCheckingMode = "standard"
    87	reportMissingImports = true

exec
/bin/bash -lc 'rg -n "boundary_contract|test_disco_verify_runner|test_deck_validators|test_artifact_validators|test_heavy_validators|buildTrace.replay|e2e-full|scenarios_run|validate_pptx|disco-verify|pytest|vitest|playwright" .github/workflows/evidence-harness.yml pyproject.toml frontend/package.json packages/*/pyproject.toml' in /var/home/dylan/projects/disco
 succeeded in 0ms:
packages/tools/pyproject.toml:26:browser = ["playwright"]
packages/agent-server/pyproject.toml:37:# Installed into the venv, so it runs in the compose image as `disco-verify` (or
packages/agent-server/pyproject.toml:39:disco-verify = "disco.agent_server.verify:main"
packages/agent-server/pyproject.toml:40:pmx-verify = "disco.agent_server.verify:main"  # deprecated alias — use disco-verify
pyproject.toml:25:    "pytest>=8.2",
pyproject.toml:26:    "pytest-asyncio>=0.24",
pyproject.toml:46:    # _browser_daemon.py as a subprocess, which imports playwright and drives
pyproject.toml:47:    # Chromium (one-time `playwright install chromium` fetches the browser).
pyproject.toml:49:    "playwright>=1.45",
pyproject.toml:52:[tool.pytest.ini_options]
pyproject.toml:59:    # W3 slides/artifact, W10 guardrails). Selected into the fast PR tier: `-m boundary_contract`.
pyproject.toml:60:    "boundary_contract",
frontend/package.json:13:    "test": "vitest run",
frontend/package.json:14:    "test:watch": "vitest",
frontend/package.json:15:    "test:e2e": "playwright test",
frontend/package.json:16:    "test:e2e:update": "playwright test --update-snapshots"
frontend/package.json:47:    "@playwright/test": "^1.60.0",
frontend/package.json:61:    "playwright": "^1.60.0",
frontend/package.json:66:    "vitest": "^3.2.4"
.github/workflows/evidence-harness.yml:9:#    the cheap artifact validators (W14a), and the disco-verify runner unit tests (W15).
.github/workflows/evidence-harness.yml:12:#    (W11/W12), live disco-verify scenarios (W13), and render-it heavy validators (W14b).
.github/workflows/evidence-harness.yml:43:        run: uv run pytest packages -m boundary_contract -p no:cacheprovider
.github/workflows/evidence-harness.yml:45:      - name: Artifact validators + disco-verify runner (W14a/W15)
.github/workflows/evidence-harness.yml:47:          uv run pytest \
.github/workflows/evidence-harness.yml:48:            packages/tools/tests/test_artifact_validators.py \
.github/workflows/evidence-harness.yml:49:            packages/tools/tests/test_deck_validators.py \
.github/workflows/evidence-harness.yml:50:            packages/tools/tests/test_heavy_validators.py \
.github/workflows/evidence-harness.yml:51:            packages/agent-server/tests/test_disco_verify_runner.py \
.github/workflows/evidence-harness.yml:61:          npx vitest run src/lib/buildTrace.replay.test.ts
.github/workflows/evidence-harness.yml:80:      # disco-verify scenarios produce dossiers under test-record/.
.github/workflows/evidence-harness.yml:85:          npx playwright install firefox chromium
.github/workflows/evidence-harness.yml:86:          npx playwright test --config e2e-full/full.config.ts
.github/workflows/evidence-harness.yml:88:      - name: Live disco-verify scenarios (W13)
.github/workflows/evidence-harness.yml:89:        run: uv run python -m disco.agent_server.verify.scenarios_run || true
.github/workflows/evidence-harness.yml:97:            frontend/test-record/e2e-full/**

exec
/bin/bash -lc "for f in packages/core/pyproject.toml packages/tools/pyproject.toml packages/agent-server/pyproject.toml frontend/package.json; do printf '\\n### %s\\n' \""'$f"; nl -ba "$f" | sed -n '"'1,160p'; done" in /var/home/dylan/projects/disco
 succeeded in 0ms:

### packages/core/pyproject.toml
     1	# The `core` package — "the brain": no server, no UI, importable library
     2	# (basis-of-design.md §5). Phase 0 implements only the Event & State spine
     3	# defined by event-state-contract.md; later subsystems (loop, memory condenser,
     4	# LLM router, security) land here in subsequent phases.
     5	[project]
     6	name = "disco-core"
     7	version = "0.1.0"
     8	description = "Event & State spine + agent core for the disco platform."
     9	requires-python = ">=3.12"
    10	dependencies = [
    11	    "pydantic>=2.7",
    12	    "httpx>=0.27",
    13	    "cryptography>=42",  # Fernet for encrypting stored secrets at rest (OpenRouter key)
    14	]
    15	
    16	[build-system]
    17	requires = ["hatchling"]
    18	build-backend = "hatchling.build"
    19	
    20	# src-layout PEP 420 namespace package: `disco` is a shared namespace
    21	# (no __init__.py) so sibling packages — disco.tools, .agent_server,
    22	# .app_server — can join it later without a rename. Point the wheel at the
    23	# namespace dir and hatch packages everything beneath it.
    24	[tool.hatch.build.targets.wheel]
    25	packages = ["src/disco"]

### packages/tools/pyproject.toml
     1	# The `tools` package — the action space (BoD §5).
     2	[project]
     3	name = "disco-tools"
     4	version = "0.1.0"
     5	description = "Tool/action space for the disco platform."
     6	requires-python = ">=3.12"
     7	dependencies = [
     8	    "disco-core",
     9	    # MCP client (RP-05) — Model Context Protocol v1 (stateless, per-conversation-pool).
    10	    # Pin <2: v2 is unreleased + breaking; the 2026-07-28 RC moves to a stateless core.
    11	    "mcp>=1.27.2,<2",
    12	    # Docker SDK — the gVisor backend drives the daemon over the local socket OR
    13	    # Docker-over-SSH (the [ssh] extra makes ssh:// importable; use_ssh_client=True
    14	    # still authenticates via the system ssh, e.g. keyless Tailscale SSH).
    15	    "docker[ssh]>=7",
    16	    "podman>=5",  # Podman SDK — the Podman backend drives the rootless socket native-remote
    17	    "jupyter_client",
    18	    "websockets",
    19	    "openpyxl>=3.1",
    20	    # C3: native editable PPTX renderer (real text boxes, NOT image-per-slide).
    21	    # BSD-3-Clause; no transitive non-permissive deps.
    22	    "python-pptx>=1.0.2",
    23	]
    24	
    25	[project.optional-dependencies]
    26	browser = ["playwright"]
    27	
    28	[build-system]
    29	requires = ["hatchling"]
    30	build-backend = "hatchling.build"
    31	
    32	[tool.hatch.build.targets.wheel]
    33	packages = ["src/disco"]
    34	
    35	[tool.uv.sources]
    36	disco-core = { workspace = true }

### packages/agent-server/pyproject.toml
     1	# The `agent-server` package — per-conversation runtime (BoD §5). In Phase 0 it
     2	# exposes the §7 wire layer (WebSocket event/state streaming + reconnect/replay)
     3	# and the §7.5 REST surface over the core EventStore. No agent loop yet.
     4	[project]
     5	name = "disco-agent-server"
     6	version = "0.1.0"
     7	description = "Per-conversation runtime: WebSocket + REST over the core event log."
     8	requires-python = ">=3.12"
     9	dependencies = [
    10	    "disco-core",
    11	    "disco-retrieval",
    12	    "disco-tools",  # the Build surface composes the agent toolset + sandbox here
    13	    "fastapi>=0.110",
    14	    "python-multipart>=0.0.12",
    15	    "uvicorn[standard]>=0.30",  # [standard] pulls the websockets impl the WS routes need
    16	    "websockets>=12.0",
    17	    "cronsim>=2.6",
    18	    "weasyprint>=63.0",  # RP-07: PDF export (md → HTML → WeasyPrint)
    19	    "markdown>=3.5",  # RP-07: faithful md → HTML for the PDF path (tables/lists/etc.)
    20	]
    21	
    22	[project.optional-dependencies]
    23	# RP-09 in-process Kokoro TTS — OPTIONAL so the lean core install stays lean. Rides
    24	# the onnxruntime already pulled by fastembed (deliberately NOT torch). This extra is
    25	# on the MEMBER package, so install it with
    26	# `uv sync --package disco-agent-server --extra tts` (NOT a bare `uv sync
    27	# --extra tts`, which errors — the workspace root has no `tts` extra). Weights
    28	# download on first use.
    29	tts = [
    30	    "kokoro-onnx>=0.4",
    31	    "lameenc>=1.7",
    32	    "numpy>=1.26",
    33	]
    34	
    35	[project.scripts]
    36	# `disco verify` — verify a self-hoster's configured model actually drives the loop.
    37	# Installed into the venv, so it runs in the compose image as `disco-verify` (or
    38	# `python -m disco.agent_server.verify`).
    39	disco-verify = "disco.agent_server.verify:main"
    40	pmx-verify = "disco.agent_server.verify:main"  # deprecated alias — use disco-verify
    41	
    42	[build-system]
    43	requires = ["hatchling"]
    44	build-backend = "hatchling.build"
    45	
    46	[tool.hatch.build.targets.wheel]
    47	packages = ["src/disco"]
    48	
    49	[tool.uv.sources]
    50	disco-core = { workspace = true }
    51	disco-retrieval = { workspace = true }
    52	disco-tools = { workspace = true }

### frontend/package.json
     1	{
     2	  "name": "disco-web",
     3	  "private": true,
     4	  "version": "0.1.0",
     5	  "type": "module",
     6	  "description": "Research Answer UI — the Research surface front-end (BoD §13.3).",
     7	  "scripts": {
     8	    "dev": "vite",
     9	    "build": "tsc -b && vite build",
    10	    "typecheck:build": "tsc -p tsconfig.build.json --noEmit",
    11	    "preview": "vite preview",
    12	    "lint": "eslint .",
    13	    "test": "vitest run",
    14	    "test:watch": "vitest",
    15	    "test:e2e": "playwright test",
    16	    "test:e2e:update": "playwright test --update-snapshots"
    17	  },
    18	  "dependencies": {
    19	    "@fontsource-variable/fraunces": "^5.1.0",
    20	    "@fontsource-variable/jetbrains-mono": "^5.1.0",
    21	    "@fontsource-variable/newsreader": "^5.2.10",
    22	    "@fontsource-variable/schibsted-grotesk": "^5.1.0",
    23	    "@radix-ui/react-dialog": "^1.1.0",
    24	    "@radix-ui/react-dropdown-menu": "^2.1.16",
    25	    "@radix-ui/react-tabs": "^1.1.0",
    26	    "@radix-ui/react-tooltip": "^1.1.0",
    27	    "@tanstack/react-query": "^5.59.0",
    28	    "@univerjs/core": "^0.25.0",
    29	    "@univerjs/design": "^0.25.0",
    30	    "@univerjs/engine-render": "^0.25.0",
    31	    "@univerjs/sheets": "^0.25.0",
    32	    "@univerjs/sheets-ui": "^0.25.0",
    33	    "@univerjs/ui": "^0.25.0",
    34	    "chart.js": "^4.4.0",
    35	    "class-variance-authority": "^0.7.0",
    36	    "clsx": "^2.1.0",
    37	    "lucide-react": "^0.460.0",
    38	    "react": "^19.0.0",
    39	    "react-dom": "^19.0.0",
    40	    "react-markdown": "^10.1.0",
    41	    "react-router-dom": "^7.16.0",
    42	    "remark-gfm": "^4.0.1",
    43	    "tailwind-merge": "^2.5.0"
    44	  },
    45	  "devDependencies": {
    46	    "@eslint/js": "^9.39.4",
    47	    "@playwright/test": "^1.60.0",
    48	    "@tailwindcss/vite": "^4.0.0",
    49	    "@testing-library/jest-dom": "^6.5.0",
    50	    "@testing-library/react": "^16.0.0",
    51	    "@testing-library/user-event": "^14.5.0",
    52	    "@types/node": "^22.19.19",
    53	    "@types/react": "^19.0.0",
    54	    "@types/react-dom": "^19.0.0",
    55	    "@vitejs/plugin-react": "^4.3.0",
    56	    "eslint": "^9.13.0",
    57	    "eslint-plugin-react-hooks": "^5.0.0",
    58	    "eslint-plugin-react-refresh": "^0.4.14",
    59	    "globals": "^15.11.0",
    60	    "jsdom": "^25.0.0",
    61	    "playwright": "^1.60.0",
    62	    "tailwindcss": "^4.0.0",
    63	    "typescript": "^5.6.0",
    64	    "typescript-eslint": "^8.10.0",
    65	    "vite": "^6.0.0",
    66	    "vitest": "^3.2.4"
    67	  }
    68	}

codex
The workflow claims live scenarios/heavy validators in nightly, but the command masks live-scenario failure with `|| true` and there is no separate heavy-validator step. That makes the most important tier advisory even inside the advisory job.
exec
/bin/bash -lc "nl -ba packages/agent-server/src/disco/agent_server/verify/__init__.py | sed -n '1,140p' && nl -ba packages/agent-server/src/disco/agent_server/verify/__main__.py | sed -n '1,140p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	"""disco-verify — two things in one namespace:
     2	
     3	1. **Setup verification** (original ``verify.py``): a small battery of capability
     4	   checks run against the persisted config and live endpoints to prove a configured
     5	   model can actually drive the agent loop (``check_config``, ``check_completion``,
     6	   ``check_tool_calling``, ``check_grounding``, ``run_checks``).
     7	
     8	2. **API-first scenario runner** (W15, new): drives the running app through its
     9	   HTTP/WS API, waits for terminal state, locates deliverables, runs validators,
    10	   and writes a redacted evidence dossier (``run_scenario``, ``Scenario``,
    11	   ``VerifyResult``, ``AbstractVerifyClient``, ``HttpVerifyClient``).
    12	
    13	The two sub-systems are independent; this ``__init__`` re-exports both so
    14	``from disco.agent_server import verify; verify.check_config(...)`` and
    15	``from disco.agent_server.verify import run_scenario`` both work.
    16	
    17	Note: the original ``verify.py`` flat module is shadowed by this package once the
    18	``verify/`` directory exists. Its content is preserved verbatim in
    19	``_setup_checks.py`` and re-exported here so existing callers are unaffected.
    20	"""
    21	
    22	from __future__ import annotations
    23	
    24	# ---- setup-verification (original verify.py content, preserved in _setup_checks) ---
    25	from ._setup_checks import (  # noqa: F401  (public re-export)
    26	    Check,
    27	    Status,
    28	    _build_runtime,
    29	    _render,
    30	    check_completion,
    31	    check_config,
    32	    check_grounding,
    33	    check_tool_calling,
    34	    main,
    35	    run_checks,
    36	)
    37	
    38	# ---- API-first scenario runner (W15) ----------------------------------------
    39	from .runner import AbstractVerifyClient, HttpVerifyClient, run_scenario  # noqa: F401
    40	from .schema import Scenario, VerifyResult  # noqa: F401
    41	
    42	__all__ = [
    43	    # setup-verification
    44	    "Check",
    45	    "Status",
    46	    "check_config",
    47	    "check_completion",
    48	    "check_grounding",
    49	    "check_tool_calling",
    50	    "main",
    51	    "run_checks",
    52	    # W15 runner
    53	    "AbstractVerifyClient",
    54	    "HttpVerifyClient",
    55	    "Scenario",
    56	    "VerifyResult",
    57	    "run_scenario",
    58	]
     1	"""``python -m disco.agent_server.verify`` entry point.
     2	
     3	Preserved when the flat ``verify.py`` module became the ``verify/`` package (W15): the
     4	self-host setup-check CLI (``disco-verify`` / ``python -m disco.agent_server.verify``) still
     5	runs the same ``main()``.
     6	"""
     7	
     8	from __future__ import annotations
     9	
    10	from ._setup_checks import main
    11	
    12	if __name__ == "__main__":
    13	    main()

codex
I’m doing one last pass over sandbox exception typing and filesystem readers to separate “sound but narrow” from actual leaks. Then I’ll synthesize the findings by priority.
exec
/bin/bash -lc "nl -ba packages/core/src/disco/core/loop/file_state.py | sed -n '130,150p' && nl -ba packages/core/src/disco/core/loop/view_render.py | sed -n '80,100p' && nl -ba packages/core/src/disco/core/loop/recitation.py | sed -n '260,278p' && nl -ba packages/core/src/disco/core/loop/observe.py | sed -n '108,122p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   130	        treated as "not stale" (the snapshot will handle the missing case).
   131	
   132	        This is a pure DETECTION scan; it does NOT update the tracker.
   133	        """
   134	        stale: list[str] = []
   135	        for path in working_set:
   136	            snap = self._snaps.get(path)
   137	            if snap is None:
   138	                continue  # never shown → "never-shown", not "stale"
   139	            try:
   140	                raw = await asyncio.wait_for(
   141	                    sbx.read_file(path),  # type: ignore[attr-defined]
   142	                    timeout=_STALE_READ_TIMEOUT_S,
   143	                )
   144	            except (FileNotFoundError, TimeoutError, PermissionError, OSError):
   145	                continue  # cannot read → treat conservatively as not stale
   146	            disk_sha = hashlib.sha256(raw).hexdigest()
   147	            if disk_sha != snap.sha:
   148	                stale.append(path)
   149	        return stale
   150	
    80	    would block pause/steer/cancel). Raises nothing. Extracted from
    81	    workspace_snapshot_message to keep that coordinator under the size cap."""
    82	    try:
    83	        raw = await asyncio.wait_for(sbx.read_file(path), timeout=_WS_READ_TIMEOUT_S)
    84	    except TimeoutError:
    85	        # Degrade to the snapshot collected so far. The sandbox isn't healed
    86	        # here (a pure projection step); the model's NEXT real action goes
    87	        # through _execute_and_observe, which detects the dead sandbox.
    88	        return _SNAPSHOT_READ_HALT
    89	    except FileNotFoundError:
    90	        # E4 (T8) — deleted between the action and this read. Surface it (the
    91	        # model can re-decide whether to re-create); don't silently lose it.
    92	        omitted_notes.append(f"[file gone: {path}]")
    93	        return None
    94	    except PermissionError:
    95	        # E4 (T8) — sandbox denied the read. Note it so the model knows the path
    96	        # is in its working set but the snapshot can't show it.
    97	        omitted_notes.append(f"[unreadable: permission: {path}]")
    98	        return None
    99	    except (IsADirectoryError, NotADirectoryError, OSError) as e:
   100	        # E4 (T8) — a directory slipped into the working set. Note + skip; any
   260	        """
   261	        sbx: Sandbox | None = getattr(self._loop.executor, "sandbox", None)
   262	        if sbx is None:
   263	            return
   264	        path = self._loop._MEMORY_PATH
   265	        # Read existing content (if any). A missing file means a fresh mirror — but
   266	        # first fall back to the legacy .pmx/ path so a pre-rename workspace's facts
   267	        # migrate forward into the new file on the next write.
   268	        try:
   269	            existing = (await sbx.read_file(path)).decode("utf-8", errors="replace")
   270	        except (FileNotFoundError, NotADirectoryError):
   271	            try:
   272	                existing = (
   273	                    await sbx.read_file(self._loop._LEGACY_MEMORY_PATH)
   274	                ).decode("utf-8", errors="replace")
   275	            except Exception:  # noqa: BLE001 — no legacy mirror either: fresh start
   276	                existing = ""
   277	        except Exception:  # noqa: BLE001 — read flakiness: start clean
   278	            existing = ""
   108	            if bn.startswith(".disco-spill-"):
   109	                _add(name)
   110	
   111	        # 3. the standing-memory mirror if present (.disco/, or legacy .pmx/).
   112	        for memory_path in (".disco/MEMORY.md", ".pmx/MEMORY.md"):
   113	            try:
   114	                await sbx.read_file(memory_path)
   115	                _add(memory_path)
   116	                break
   117	            except (FileNotFoundError, NotADirectoryError):
   118	                continue
   119	            except Exception:  # noqa: BLE001 — a dead sandbox just skips the pointer
   120	                break
   121	
   122	        return candidates

exec
/bin/bash -lc "rg -n \"read_file\\(|except \\(FileNotFoundError|except FileNotFoundError|except \\(FileNotFoundError, NotADirectoryError\\)|except \\(FileNotFoundError, TimeoutError, PermissionError, OSError\\)\" packages/core/src/disco/core/loop packages/agent-server/src/disco/agent_server packages/tools/src/disco/tools/builtin packages/tools/src/disco/tools/projects -S" in /var/home/dylan/projects/disco
 succeeded in 0ms:
packages/tools/src/disco/tools/projects/archive.py:61:    async def read_file(self, path: str) -> bytes: ...
packages/tools/src/disco/tools/projects/archive.py:146:                data = await sandbox.read_file(child)
packages/core/src/disco/core/loop/observe.py:114:                await sbx.read_file(memory_path)
packages/core/src/disco/core/loop/observe.py:117:            except (FileNotFoundError, NotADirectoryError):
packages/core/src/disco/core/loop/recitation.py:269:            existing = (await sbx.read_file(path)).decode("utf-8", errors="replace")
packages/core/src/disco/core/loop/recitation.py:270:        except (FileNotFoundError, NotADirectoryError):
packages/core/src/disco/core/loop/recitation.py:273:                    await sbx.read_file(self._loop._LEGACY_MEMORY_PATH)
packages/core/src/disco/core/loop/file_state.py:141:                    sbx.read_file(path),  # type: ignore[attr-defined]
packages/core/src/disco/core/loop/file_state.py:144:            except (FileNotFoundError, TimeoutError, PermissionError, OSError):
packages/tools/src/disco/tools/builtin/_deck_patch.py:292:            raw_bytes = await ctx.sandbox.read_file(args.deck_file)
packages/tools/src/disco/tools/builtin/_deck_patch.py:350:                data = await ctx.sandbox.read_file(f"{stem}_img_{i}.png")
packages/core/src/disco/core/loop/view_render.py:83:        raw = await asyncio.wait_for(sbx.read_file(path), timeout=_WS_READ_TIMEOUT_S)
packages/core/src/disco/core/loop/view_render.py:89:    except FileNotFoundError:
packages/tools/src/disco/tools/builtin/live_view.py:97:    except (FileNotFoundError, subprocess.TimeoutExpired):
packages/tools/src/disco/tools/builtin/live_view.py:124:    except FileNotFoundError:
packages/core/src/disco/core/loop/boundaries.py:88:    async def read_file(self, path: str) -> bytes: ...
packages/tools/src/disco/tools/builtin/files.py:245:        data = await ctx.sandbox.read_file(args.path)
packages/tools/src/disco/tools/builtin/files.py:250:        # ctx.sandbox.read_file() calls inside _gated_write and each mutator's
packages/tools/src/disco/tools/builtin/files.py:347:            old_text = (await ctx.sandbox.read_file(args.path)).decode("utf-8", errors="replace")
packages/tools/src/disco/tools/builtin/files.py:414:            existing = await ctx.sandbox.read_file(args.path)
packages/tools/src/disco/tools/builtin/files.py:537:        text = (await ctx.sandbox.read_file(args.path)).decode("utf-8", errors="replace")
packages/tools/src/disco/tools/builtin/files.py:604:        text = (await ctx.sandbox.read_file(args.path)).decode("utf-8", errors="replace")
packages/tools/src/disco/tools/builtin/files.py:679:        text = (await ctx.sandbox.read_file(args.path)).decode("utf-8", errors="replace")
packages/tools/src/disco/tools/builtin/files.py:760:        text = (await ctx.sandbox.read_file(args.path)).decode("utf-8", errors="replace")
packages/agent-server/src/disco/agent_server/report_export.py:712:    return await sandbox.read_file("_export.docx")
packages/agent-server/src/disco/agent_server/routes/conversations.py:223:                data = await session.read_file(norm)
packages/agent-server/src/disco/agent_server/routes/files.py:70:            return await session.read_file(norm)
packages/agent-server/src/disco/agent_server/routes/files.py:128:                    existing_bytes += len(await session.read_file(f"uploads/{fname}"))

exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/builtin/files.py | sed -n '230,265p' && nl -ba packages/tools/src/disco/tools/executor.py | sed -n '145,170p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   230	            "Read a UTF-8 text file from the workspace, with 1-based LINE NUMBERS. "
   231	            "For large files pass `offset` (1-based start line) + `limit` (line "
   232	            "count) to read a slice. Prefer `file_edit` (pass the exact text you see "
   233	            "as `old`) for targeted changes; the line numbers also let you target "
   234	            "`file_replace_lines`, but re-read right before each line edit since they "
   235	            "shift after every change."
   236	        ),
   237	        args_model=FileReadArgs,
   238	        needs=_FS,
   239	        runs_in="sandbox",
   240	        read_only=True,  # observes only — safe for the planner
   241	    )
   242	
   243	    async def run(self, args: FileReadArgs, ctx: ToolContext) -> ToolOutcome:
   244	        assert ctx.sandbox is not None  # sandbox tools always receive an instance
   245	        data = await ctx.sandbox.read_file(args.path)
   246	        # F1 — set the read-since-write bit for this path so a subsequent
   247	        # file_write is allowed (the happy path: read → write). Applies to ALL
   248	        # tiers (not gated on ctx.assist) — the thrash root-cause hits capable
   249	        # models too, and the guard must be symmetric. The internal
   250	        # ctx.sandbox.read_file() calls inside _gated_write and each mutator's
   251	        # own read do NOT go through this method, so they do NOT set the bit —
   252	        # only an explicit model-issued file_read counts as grounding evidence.
   253	        _conv_state(ctx.conversation_id)["read_since_write"].add(_canonical(args.path))
   254	        text = data.decode("utf-8", errors="replace")
   255	        lines = text.splitlines()
   256	        total = len(lines)
   257	        start = max((args.offset or 1) - 1, 0)
   258	        if start >= total and total > 0:
   259	            return ToolOutcome(
   260	                success=True,
   261	                content=f"[lines {start + 1}-{total} of {total} — offset past end of file]",
   262	            )
   263	        # F7 — pressure-aware head-only. Fires ONLY when:
   264	        #   - ctx.assist is on (the weak-model tier the gate exists to protect),
   265	        #   - the caller did NOT pass an explicit offset/limit (a targeted read
   145	                "invalid_arguments",
   146	                f"arguments for {call.tool_name!r} failed validation",
   147	                validation_errors=[dict(err) for err in e.errors()],  # ErrorDetails -> plain dict
   148	                expected_schema=tool.definition.args_model.model_json_schema(),
   149	            )
   150	
   151	        # 3. build context (sandbox handle + scoped capabilities; NO secrets)
   152	        ctx = await self._build_context(tool.definition)
   153	
   154	        # 4. execute with a timeout; map every failure mode to a failed ToolResult
   155	        try:
   156	            outcome = await asyncio.wait_for(tool.run(args, ctx), timeout=ctx.timeout_s)
   157	        except TimeoutError:
   158	            return self._fail(call, "timeout", f"exceeded {ctx.timeout_s}s")
   159	        except CapabilityDenied as e:
   160	            return self._fail(call, "denied", f"capability not granted: {e}")
   161	        except SandboxError as e:
   162	            return self._fail(call, "sandbox_error", str(e))
   163	        except Exception as e:  # noqa: BLE001 — the tool's own failure is an observation
   164	            return self._fail(call, "execution_error", str(e))
   165	
   166	        return ToolResult(
   167	            call_id=call.call_id,
   168	            tool_name=call.tool_name,
   169	            success=outcome.success,
   170	            content=outcome.content,

exec
/bin/bash -lc "PYTHONPATH=packages/core/src:packages/tools/src python - <<'PY'
from disco.tools.sandbox.base import SandboxFileNotFoundError, SandboxError
print(SandboxFileNotFoundError.__mro__)
e=SandboxFileNotFoundError('x')
print(isinstance(e, SandboxError), isinstance(e, FileNotFoundError), isinstance(e, OSError), e.args)
PY" in /var/home/dylan/projects/disco
 exited 1 in 601ms:
Traceback (most recent call last):
  File "<stdin>", line 1, in <module>
  File "/var/home/dylan/projects/disco/packages/tools/src/disco/tools/__init__.py", line 20, in <module>
    from .builtin import build_default_registry
  File "/var/home/dylan/projects/disco/packages/tools/src/disco/tools/builtin/__init__.py", line 29, in <module>
    from .sheets import SheetsTool
  File "/var/home/dylan/projects/disco/packages/tools/src/disco/tools/builtin/sheets.py", line 15, in <module>
    import openpyxl
ModuleNotFoundError: No module named 'openpyxl'

codex
For W1 specifically, `SandboxSession.read_file()` does not erase the typed missing-file exception; it only converts `SandboxUnavailableError`. The missed cases are other filesystem errors and validation coverage, not the main missing-file path.
exec
/bin/bash -lc 'rg -n "class GatewayKernel|exec_shell|read_file|SandboxError|FileNotFoundError|raise " packages/tools/src/disco/tools/sandbox/kernel.py | head -n 160' in /var/home/dylan/projects/disco
 succeeded in 0ms:
20:from .base import SandboxError
106:        raise NotImplementedError
110:        raise NotImplementedError
114:        raise NotImplementedError
118:        raise NotImplementedError
122:        raise NotImplementedError
314:class GatewayKernel(KernelSession):
399:        raise SandboxError(
680:                # fresh one. Never raise from here: a broken inner shouldn't

exec
/bin/bash -lc "nl -ba packages/tools/src/disco/tools/sandbox/kernel.py | sed -n '314,430p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   314	class GatewayKernel(KernelSession):
   315	    """Transport for container backends: reaches jupyter_kernel_gateway via WebSockets."""
   316	
   317	    def __init__(self, sandbox: Any, sessions: Any) -> None:
   318	        self._sandbox = sandbox
   319	        self._sessions = sessions
   320	        self._kernel_id: str | None = None
   321	        self._ws: Any | None = None
   322	        self._seq = 0
   323	        self._url: str | None = None
   324	        self._session_id = str(uuid.uuid4())
   325	        # Per-sandbox gateway auth token (stable + re-derivable — see
   326	        # _gateway_auth_token). Sent on every gateway HTTP request and on the WS
   327	        # connect, and handed to the gateway at launch via --auth_token.
   328	        self._token = _gateway_auth_token(str(getattr(sandbox, "id", "default")))
   329	        self._auth_headers = {"Authorization": f"token {self._token}"}
   330	
   331	    def _ws_with_token(self, ws_url: str) -> str:
   332	        """Append the gateway token as a query param (version-agnostic across
   333	        websockets releases vs threading per-connect header kwargs)."""
   334	        sep = "&" if "?" in ws_url else "?"
   335	        return f"{ws_url}{sep}token={self._token}"
   336	
   337	    async def _ensure_gateway(self) -> str:
   338	        """Start the gateway lazily in tmux and wait for ready."""
   339	        # Check if already running (port 8899)
   340	        from ._container import INTERNAL_PORTS
   341	        port = next(iter(INTERNAL_PORTS)) # 8899
   342	        
   343	        # Resolve host mapping
   344	        mapping = None
   345	        if hasattr(self._sandbox, "internal_port_mapping"):
   346	            mapping = self._sandbox.internal_port_mapping(port)
   347	        
   348	        if not mapping:
   349	            # Not a container or not published? For process backend it shouldn't reach here.
   350	            # But just in case, if it's local, we might want localhost:8899
   351	            mapping = ("localhost", port)
   352	
   353	        self._url = f"http://{mapping[0]}:{mapping[1]}"
   354	        
   355	        # Try to reach it
   356	        import httpx
   357	        try:
   358	            async with httpx.AsyncClient() as client:
   359	                res = await client.get(f"{self._url}/api", timeout=1.0, headers=self._auth_headers)
   360	                if res.status_code == 200:
   361	                    return self._url
   362	        except Exception:
   363	            pass
   364	
   365	        # Not running -> start in tmux session '__kernel'. exec_dir=None means the
   366	        # container tmux default dir (WORKDIR /workspace), so kernels spawned by the
   367	        # gateway inherit the workspace as cwd — user code's relative paths resolve
   368	        # against the same tree the file API serves.
   369	        # --auth_token requires every caller (REST + WS) to present the token;
   370	        # without it the gateway is an unauthenticated RCE endpoint on whatever
   371	        # interface the port is published to. The token is hex (shell-safe).
   372	        cmd = (
   373	            f"jupyter kernelgateway --KernelGatewayApp.api=kernel_gateway.jupyter_websocket "
   374	            f"--KernelGatewayApp.auth_token={self._token} "
   375	            f"--ip 0.0.0.0 --port {port}"
   376	        )
   377	        await self._sessions.exec("__kernel", cmd, None)
   378	        
   379	        # Poll /api until ready. The gateway start is CPU-bound; on a saturated
   380	        # box (local-LLM inference + the build agent competing for cores) it can
   381	        # take well over 30s, so an autonomous build would forfeit on a
   382	        # slow-but-fine start. Budget generously + env-tunable
   383	        # (DISCO_KERNEL_GATEWAY_START_S, default 120s; PMX_ legacy honored).
   384	        budget_s = int(
   385	            os.environ.get("DISCO_KERNEL_GATEWAY_START_S")
   386	            or os.environ.get("PMX_KERNEL_GATEWAY_START_S")
   387	            or "120"
   388	        )
   389	        async with httpx.AsyncClient() as client:
   390	            for _ in range(max(1, budget_s)):
   391	                try:
   392	                    res = await client.get(f"{self._url}/api", timeout=1.0, headers=self._auth_headers)
   393	                    if res.status_code == 200:
   394	                        return self._url
   395	                    await asyncio.sleep(1.0)  # up but not 200 yet — wait, don't tight-loop
   396	                except Exception:
   397	                    await asyncio.sleep(1.0)  # not up yet (connection refused) — wait
   398	
   399	        raise SandboxError(
   400	            f"jupyter kernel gateway failed to start within {budget_s}s "
   401	            f"(set DISCO_KERNEL_GATEWAY_START_S higher if the box is heavily loaded)"
   402	        )
   403	
   404	    async def start(self) -> None:
   405	        url = await self._ensure_gateway()
   406	        import httpx
   407	        async with httpx.AsyncClient() as client:
   408	            res = await client.post(f"{url}/api/kernels", headers=self._auth_headers)
   409	            res.raise_for_status()
   410	            self._kernel_id = res.json()["id"]
   411	
   412	        # Connect WS
   413	        import websockets
   414	        ws_url = url.replace("http://", "ws://") + f"/api/kernels/{self._kernel_id}/channels"
   415	        self._ws = await websockets.connect(self._ws_with_token(ws_url))
   416	        
   417	        # Setup memory limit
   418	        setup_cell = (
   419	            "import resource; "
   420	            "resource.setrlimit(resource.RLIMIT_AS, (4 << 30, 4 << 30))"
   421	        )
   422	        await self.execute(setup_cell, timeout_s=10)
   423	
   424	    async def execute(self, code: str, *, timeout_s: int) -> KernelResult:
   425	        if not self._ws:
   426	            await self.start()
   427	        assert self._ws is not None  # start() always populates _ws
   428	        ws = self._ws
   429	
   430	        msg_id = str(uuid.uuid4())

codex
I’m doing a narrow check on the Playwright artifact collector now. The API runner misses bytes; if the browser harness collects them correctly, that still leaves the PR/nightly API path weak but reduces the total blast radius.
exec
/bin/bash -lc "nl -ba frontend/e2e-full/support/artifactCollector.ts | sed -n '1,260p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	/**
     2	 * artifactCollector — fetch app truth endpoints after a meaningful action.
     3	 *
     4	 * Calls the agent-server's per-conversation endpoints and saves the responses
     5	 * under  conversations/<cid>/  in the run folder.  All 404s are tolerated
     6	 * silently (e.g. /api/debug/trace/{cid} is inert without DISCO_INSPECT=1).
     7	 * Network errors are recorded to artifact-collector-errors.jsonl and skipped.
     8	 *
     9	 * evidence-harness-campaign.md W11
    10	 */
    11	
    12	import * as fs from "node:fs";
    13	import * as path from "node:path";
    14	import type { Recorder } from "./recorder";
    15	
    16	// ---------------------------------------------------------------------------
    17	// Endpoint table
    18	// ---------------------------------------------------------------------------
    19	
    20	interface Endpoint {
    21	  /** Filename to write under conversations/<cid>/. */
    22	  file: string;
    23	  /** URL path.  Occurrences of {cid} are replaced before fetching. */
    24	  urlPath: string;
    25	}
    26	
    27	/**
    28	 * Truth endpoints called after each meaningful interaction.
    29	 * Mirrors the list in evidence-harness-campaign.md §"Three-layer harness".
    30	 */
    31	const ENDPOINTS: Endpoint[] = [
    32	  { file: "events.jsonl", urlPath: "/conversations/{cid}/events" },
    33	  { file: "state.final.json", urlPath: "/state" },
    34	  { file: "inspect-trace.json", urlPath: "/api/debug/trace/{cid}" },
    35	  { file: "workspace-manifest.json", urlPath: "/api/projects/{cid}/manifest" },
    36	];
    37	
    38	// ---------------------------------------------------------------------------
    39	// Public API
    40	// ---------------------------------------------------------------------------
    41	
    42	/**
    43	 * Fetch all truth endpoints for {@link cid} from {@link agentBaseUrl} and
    44	 * write each successful response to  conversations/<cid>/  in the run folder.
    45	 *
    46	 * @param cid           Conversation / request ID to query.
    47	 * @param agentBaseUrl  Root URL of the agent-server, e.g. "http://127.0.0.1:8000".
    48	 * @param recorder      Active Recorder for this run.
    49	 */
    50	export async function collectArtifacts(
    51	  cid: string,
    52	  agentBaseUrl: string,
    53	  recorder: Recorder,
    54	): Promise<void> {
    55	  recorder.noteConversation(cid);
    56	  const cidDir = path.join(recorder.runDir, "conversations", cid);
    57	
    58	  for (const ep of ENDPOINTS) {
    59	    const urlPath = ep.urlPath.replace("{cid}", cid);
    60	    const url = `${agentBaseUrl.replace(/\/$/, "")}${urlPath}`;
    61	
    62	    try {
    63	      const res = await fetch(url);
    64	      if (!res.ok) {
    65	        // 404 expected when endpoint is gated or conversation doesn't exist yet.
    66	        if (res.status !== 404) {
    67	          recorder.write("artifact-collector-errors.jsonl", {
    68	            cid,
    69	            url,
    70	            status: res.status,
    71	            ts: new Date().toISOString(),
    72	          });
    73	        }
    74	        continue;
    75	      }
    76	      const text = await res.text();
    77	      fs.writeFileSync(path.join(cidDir, ep.file), text, "utf-8");
    78	    } catch (err: unknown) {
    79	      // Network errors (ECONNREFUSED, timeout, etc.) — record and skip.
    80	      recorder.write("artifact-collector-errors.jsonl", {
    81	        cid,
    82	        url,
    83	        error: err instanceof Error ? err.message : String(err),
    84	        ts: new Date().toISOString(),
    85	      });
    86	    }
    87	  }
    88	}

exec
/bin/bash -lc "nl -ba frontend/e2e-full/support/assertions.ts | sed -n '155,260p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
   155	  }
   156	}
   157	
   158	// ---------------------------------------------------------------------------
   159	
   160	/** Options for {@link expectInspectTrace}. */
   161	export interface InspectTraceOpts {
   162	  /**
   163	   * Minimum number of routing decisions the trace must contain.
   164	   * Defaults to 1.  Set to 0 to skip this check.
   165	   */
   166	  minRoutingDecisions?: number;
   167	  /**
   168	   * Strings that must appear somewhere in the serialized span names.
   169	   * Each string is tested with `includes()` against every `span.name`.
   170	   */
   171	  spansInclude?: string[];
   172	}
   173	
   174	/**
   175	 * Assert that the DISCO_INSPECT router/span trace for `cid` meets minimum
   176	 * quality thresholds.
   177	 *
   178	 * Fetches  GET /api/debug/trace/{cid}  from `agentBaseUrl`.
   179	 * This endpoint is only populated when the server runs with DISCO_INSPECT=1;
   180	 * a 404 is treated as an assertion failure (not silently skipped) so that
   181	 * the harness catches misconfigured environments.
   182	 *
   183	 * @param cid              Conversation / request ID.
   184	 * @param agentBaseUrl     Agent-server root.
   185	 * @param opts             Thresholds; see {@link InspectTraceOpts}.
   186	 * @throws {Error}         If the trace doesn't meet the specified thresholds.
   187	 */
   188	export async function expectInspectTrace(
   189	  cid: string,
   190	  agentBaseUrl: string,
   191	  opts: InspectTraceOpts = {},
   192	): Promise<void> {
   193	  const base = stripTrailingSlash(agentBaseUrl);
   194	  const url = `${base}/api/debug/trace/${cid}`;
   195	  const trace = await fetchJson<InspectTrace>(url);
   196	
   197	  const minDecisions = opts.minRoutingDecisions ?? 1;
   198	
   199	  // Routing decisions check
   200	  if (minDecisions > 0) {
   201	    const decisions = trace["routing_decisions"] ?? [];
   202	    const count = Array.isArray(decisions) ? decisions.length : 0;
   203	    if (count < minDecisions) {
   204	      throw new Error(
   205	        `expectInspectTrace [cid=${cid}]: expected >= ${minDecisions} routing decision(s), got ${count}`,
   206	      );
   207	    }
   208	  }
   209	
   210	  // Span inclusion check
   211	  if (opts.spansInclude !== undefined && opts.spansInclude.length > 0) {
   212	    const spans = trace["spans"];
   213	    const spanNames: string[] = Array.isArray(spans)
   214	      ? spans.map((s) => {
   215	          const sp = s as { name?: unknown };
   216	          return typeof sp.name === "string" ? sp.name : "";
   217	        })
   218	      : [];
   219	
   220	    const missing: string[] = [];
   221	    for (const want of opts.spansInclude) {
   222	      if (!spanNames.some((n) => n.includes(want))) {
   223	        missing.push(want);
   224	      }
   225	    }
   226	    if (missing.length > 0) {
   227	      throw new Error(
   228	        `expectInspectTrace [cid=${cid}]: span(s) not found: ${missing.join(", ")}\n` +
   229	          `Observed spans: ${spanNames.join(", ")}`,
   230	      );
   231	    }
   232	  }
   233	}
   234	
   235	// ---------------------------------------------------------------------------
   236	
   237	/**
   238	 * Assert that an artifact at `artifactPath` inside the conversation workspace
   239	 * is readable — i.e. the server returns a non-empty 2xx response.
   240	 *
   241	 * Fetches  GET /workspace/{artifactPath}  from `agentBaseUrl`.
   242	 * (Adjust the URL template to match whatever the app actually exposes.)
   243	 *
   244	 * @param cid            Conversation / request ID (used in error messages).
   245	 * @param agentBaseUrl   Agent-server root.
   246	 * @param artifactPath   Workspace-relative path, e.g. "report.pdf".
   247	 * @throws {Error}       If the artifact is missing or empty.
   248	 */
   249	export async function expectArtifactReadable(
   250	  cid: string,
   251	  agentBaseUrl: string,
   252	  artifactPath: string,
   253	): Promise<void> {
   254	  const base = stripTrailingSlash(agentBaseUrl);
   255	  // Try the evidence bundle endpoint first (W5); fall back to direct workspace path.
   256	  const evidenceUrl = `${base}/api/debug/evidence/${cid}`;
   257	  const workspaceUrl = `${base}/workspace/${artifactPath}`;
   258	
   259	  // Prefer the workspace path for direct artifact checks
   260	  const res = await fetch(workspaceUrl);

exec
/bin/bash -lc "nl -ba frontend/e2e-full/scenarios/smoke.full.spec.ts | sed -n '1,260p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	/**
     2	 * smoke.full.spec.ts — W11 smoke spec.
     3	 *
     4	 * ONE test that:
     5	 *   1. Opens the app root through the reverse-tunnel (http://127.0.0.1:5173).
     6	 *   2. Records network/WS/console automatically via the discoHarness fixture.
     7	 *   3. Takes a screenshot.
     8	 *   4. Writes the dossier.
     9	 *   5. Asserts the page title is non-empty and the dossier folder was produced
    10	 *      with all three required files (manifest.json, timeline.md, index.html).
    11	 *
    12	 * This spec is designed to run on VM 201 by the orchestrator; it cannot run
    13	 * on the workstation due to the memory cap on Playwright processes.
    14	 *
    15	 * evidence-harness-campaign.md W11
    16	 */
    17	
    18	import * as fs from "node:fs";
    19	import * as path from "node:path";
    20	import { expect, test } from "../fixtures/discoHarness";
    21	
    22	test("smoke — open home page and produce a dossier", async ({
    23	  page,
    24	  recorder,
    25	}) => {
    26	  // -------------------------------------------------------------------------
    27	  // 1. Navigate
    28	  // -------------------------------------------------------------------------
    29	  await page.goto("/");
    30	
    31	  // -------------------------------------------------------------------------
    32	  // 2. Record UI action
    33	  // -------------------------------------------------------------------------
    34	  recorder.write("ui-actions.jsonl", {
    35	    action: "navigate",
    36	    url: page.url(),
    37	    ts: new Date().toISOString(),
    38	  });
    39	
    40	  // -------------------------------------------------------------------------
    41	  // 3. Screenshot into the run folder
    42	  // -------------------------------------------------------------------------
    43	  const screenshotsDir = path.join(recorder.runDir, "screenshots");
    44	  fs.mkdirSync(screenshotsDir, { recursive: true });
    45	  await page.screenshot({
    46	    path: path.join(screenshotsDir, "home.png"),
    47	    fullPage: false,
    48	  });
    49	
    50	  // -------------------------------------------------------------------------
    51	  // 4. Write dossier (before assertions so we can assert on the files)
    52	  // -------------------------------------------------------------------------
    53	  recorder.dossier();
    54	
    55	  // -------------------------------------------------------------------------
    56	  // 5. Assertions
    57	  // -------------------------------------------------------------------------
    58	
    59	  // UI truth: the app loaded a non-empty page title.
    60	  const title = await page.title();
    61	  expect(title.length).toBeGreaterThan(0);
    62	
    63	  // Dossier truth: the run folder and all three required files exist.
    64	  expect(fs.existsSync(recorder.runDir)).toBe(true);
    65	  expect(
    66	    fs.existsSync(path.join(recorder.runDir, "manifest.json")),
    67	  ).toBe(true);
    68	  expect(
    69	    fs.existsSync(path.join(recorder.runDir, "timeline.md")),
    70	  ).toBe(true);
    71	  expect(
    72	    fs.existsSync(path.join(recorder.runDir, "index.html")),
    73	  ).toBe(true);
    74	});

exec
/bin/bash -lc "nl -ba frontend/e2e-full/fixtures/discoHarness.ts | sed -n '1,240p'" in /var/home/dylan/projects/disco
 succeeded in 0ms:
     1	/**
     2	 * discoHarness — Playwright test fixture that wires Recorder + wsRecorder.
     3	 *
     4	 * Provides a `recorder` fixture to every spec that imports from this module.
     5	 * The recorder:
     6	 *   - Starts before the test body (a unique run folder is created).
     7	 *   - Captures network requests/responses, console messages, page errors, and
     8	 *     WebSocket frames throughout the test.
     9	 *   - Calls recorder.dossier() in teardown to ensure the run folder is always
    10	 *     complete even if the test crashes before an explicit dossier() call.
    11	 *
    12	 * Usage in specs:
    13	 *   import { test, expect } from "../fixtures/discoHarness";
    14	 *
    15	 * evidence-harness-campaign.md W11
    16	 */
    17	
    18	import { test as base } from "@playwright/test";
    19	import { Recorder } from "../support/recorder";
    20	import { attachWsRecorder } from "../support/wsRecorder";
    21	
    22	// ---------------------------------------------------------------------------
    23	// Fixture types
    24	// ---------------------------------------------------------------------------
    25	
    26	export interface DiscoHarnessFixtures {
    27	  /** Active Recorder for the current test run. */
    28	  recorder: Recorder;
    29	}
    30	
    31	// ---------------------------------------------------------------------------
    32	// Extended test object
    33	// ---------------------------------------------------------------------------
    34	
    35	export const test = base.extend<DiscoHarnessFixtures>({
    36	  recorder: async ({ page }, provide, testInfo) => {
    37	    // Derive a deterministic, filesystem-safe run-id from the test title.
    38	    const safeTitle = testInfo.title
    39	      .toLowerCase()
    40	      .replace(/[^a-z0-9]+/g, "-")
    41	      .replace(/^-|-$/g, "")
    42	      .slice(0, 60);
    43	    const ts = new Date()
    44	      .toISOString()
    45	      .replace(/[^0-9T]/g, "")
    46	      .slice(0, 15);
    47	    const recorder = new Recorder(`${ts}-${safeTitle}`);
    48	
    49	    // Wire WS recording (must be before navigation).
    50	    attachWsRecorder(page, recorder);
    51	
    52	    // Record HTTP request/response pairs (url + status; no bodies to avoid secrets).
    53	    page.on("request", (req) => {
    54	      recorder.write("network.jsonl", {
    55	        event: "request",
    56	        method: req.method(),
    57	        url: req.url(),
    58	        ts: new Date().toISOString(),
    59	      });
    60	    });
    61	
    62	    page.on("response", (res) => {
    63	      recorder.write("network.jsonl", {
    64	        event: "response",
    65	        status: res.status(),
    66	        url: res.url(),
    67	        ts: new Date().toISOString(),
    68	      });
    69	    });
    70	
    71	    // Record browser console output.
    72	    page.on("console", (msg) => {
    73	      recorder.write("console.jsonl", {
    74	        type: msg.type(),
    75	        text: msg.text(),
    76	        ts: new Date().toISOString(),
    77	      });
    78	    });
    79	
    80	    // Record uncaught page errors.
    81	    page.on("pageerror", (err) => {
    82	      recorder.write("page-errors.jsonl", {
    83	        message: err.message,
    84	        ts: new Date().toISOString(),
    85	      });
    86	    });
    87	
    88	    // Hand the recorder to the test body.
    89	    await provide(recorder);
    90	
    91	    // Teardown: guarantee the dossier is written even if the test body crashed.
    92	    recorder.dossier();
    93	  },
    94	});
    95	
    96	// Re-export expect so specs can import both from this single module.
    97	export { expect } from "@playwright/test";

codex
The Playwright helpers have several endpoint-shape mismatches: state, events, artifact URLs, and trace span field names. Since the current smoke only checks helper importability, those mismatches are not caught.
exec
/bin/bash -lc 'rg -n "function E2EBridgeMounter|installE2EBridge|__DISCO_E2E__|runStatus|conversationId|surface" frontend/src/App.tsx frontend/src -S | head -n 160' in /var/home/dylan/projects/disco
 succeeded in 0ms:
frontend/src/App.tsx:12:import { installE2EBridge, type DiscoE2EState, type Surface } from "@/lib/e2eBridge";
frontend/src/App.tsx:33: * Mounts the `window.__DISCO_E2E__` metadata bridge (W6).  Derives surface +
frontend/src/App.tsx:34: * conversation-id from the router location; runStatus is null at the app level
frontend/src/App.tsx:35: * (surface components can extend via the shared stateRef if needed later).
frontend/src/App.tsx:38:function E2EBridgeMounter() {
frontend/src/App.tsx:48:  const conversationId =
frontend/src/App.tsx:56:  let surface: Surface = null;
frontend/src/App.tsx:57:  if (routePrefix === "build") surface = "build";
frontend/src/App.tsx:58:  else if (routePrefix === "agent") surface = "agent";
frontend/src/App.tsx:59:  else if (routePrefix === "deep") surface = "deep_research";
frontend/src/App.tsx:60:  else if (routePrefix === "imported") surface = "imported";
frontend/src/App.tsx:61:  else if (routePrefix === "share") surface = "share";
frontend/src/App.tsx:62:  else if (pathname === "/activity") surface = "activity";
frontend/src/App.tsx:63:  else if (pathname === "/history") surface = "history";
frontend/src/App.tsx:64:  else if (pathname === "/projects") surface = "projects";
frontend/src/App.tsx:65:  else if (pathname === "/settings") surface = "settings";
frontend/src/App.tsx:67:    surface =
frontend/src/App.tsx:73:    surface,
frontend/src/App.tsx:74:    conversationId,
frontend/src/App.tsx:75:    runStatus: null,
frontend/src/App.tsx:80:    surface,
frontend/src/App.tsx:81:    conversationId,
frontend/src/App.tsx:82:    runStatus: null,
frontend/src/App.tsx:87:    installE2EBridge(() => stateRef.current);
frontend/src/App.tsx:88:    // installE2EBridge is idempotent; run once on mount.
frontend/src/App.tsx:95:/** The main surface follows the active top-level mode (the slider): Search → the
frontend/src/App.tsx:96: * grounded research surface; Build → the agent surface (software framing); Agent →
frontend/src/App.tsx:97: * the same surface, general-task framing. */
frontend/src/App.tsx:105:/** Resume an existing Build project from /build/:cid — opens the Build surface
frontend/src/App.tsx:106: * pinned to a specific conversation id (the project surface's reopen path). */
frontend/src/App.tsx:144: * ResumeProject, agent framing. The stored surface stays "agent"; resume never
frontend/src/App.tsx:148: * surface (task framing, not software-dev), so ResumeAgent must capture the
frontend/src/App.tsx:177: * Research surface pinned to a specific conversation id. The WS subscription
frontend/src/App.tsx:187: * context → the shell, which frames the routed views. The main surface (/) follows the
frontend/src/App.tsx:189: * prompts land. Unknown routes fall back to the main surface.
frontend/src/vite-env.d.ts:17:  __DISCO_E2E__?: import("./lib/e2eBridge").DiscoE2EState;
frontend/src/App.tsx:12:import { installE2EBridge, type DiscoE2EState, type Surface } from "@/lib/e2eBridge";
frontend/src/App.tsx:33: * Mounts the `window.__DISCO_E2E__` metadata bridge (W6).  Derives surface +
frontend/src/App.tsx:34: * conversation-id from the router location; runStatus is null at the app level
frontend/src/App.tsx:35: * (surface components can extend via the shared stateRef if needed later).
frontend/src/App.tsx:38:function E2EBridgeMounter() {
frontend/src/App.tsx:48:  const conversationId =
frontend/src/App.tsx:56:  let surface: Surface = null;
frontend/src/App.tsx:57:  if (routePrefix === "build") surface = "build";
frontend/src/App.tsx:58:  else if (routePrefix === "agent") surface = "agent";
frontend/src/App.tsx:59:  else if (routePrefix === "deep") surface = "deep_research";
frontend/src/App.tsx:60:  else if (routePrefix === "imported") surface = "imported";
frontend/src/App.tsx:61:  else if (routePrefix === "share") surface = "share";
frontend/src/App.tsx:62:  else if (pathname === "/activity") surface = "activity";
frontend/src/App.tsx:63:  else if (pathname === "/history") surface = "history";
frontend/src/App.tsx:64:  else if (pathname === "/projects") surface = "projects";
frontend/src/App.tsx:65:  else if (pathname === "/settings") surface = "settings";
frontend/src/App.tsx:67:    surface =
frontend/src/App.tsx:73:    surface,
frontend/src/App.tsx:74:    conversationId,
frontend/src/App.tsx:75:    runStatus: null,
frontend/src/App.tsx:80:    surface,
frontend/src/App.tsx:81:    conversationId,
frontend/src/App.tsx:82:    runStatus: null,
frontend/src/App.tsx:87:    installE2EBridge(() => stateRef.current);
frontend/src/App.tsx:88:    // installE2EBridge is idempotent; run once on mount.
frontend/src/App.tsx:95:/** The main surface follows the active top-level mode (the slider): Search → the
frontend/src/App.tsx:96: * grounded research surface; Build → the agent surface (software framing); Agent →
frontend/src/App.tsx:97: * the same surface, general-task framing. */
frontend/src/App.tsx:105:/** Resume an existing Build project from /build/:cid — opens the Build surface
frontend/src/App.tsx:106: * pinned to a specific conversation id (the project surface's reopen path). */
frontend/src/App.tsx:144: * ResumeProject, agent framing. The stored surface stays "agent"; resume never
frontend/src/App.tsx:148: * surface (task framing, not software-dev), so ResumeAgent must capture the
frontend/src/App.tsx:177: * Research surface pinned to a specific conversation id. The WS subscription
frontend/src/App.tsx:187: * context → the shell, which frames the routed views. The main surface (/) follows the
frontend/src/App.tsx:189: * prompts land. Unknown routes fall back to the main surface.
frontend/src/views/HistoryView.tsx:12: * the backend. Open routes to the main surface; delete is destructive and gated
frontend/src/views/HistoryView.tsx:59:          <span className="h-3 w-2/3 animate-pulse rounded-full bg-surface-2" />
frontend/src/views/HistoryView.tsx:60:          <span className="h-2 w-24 animate-pulse rounded-full bg-surface-2" />
frontend/src/views/HistoryView.tsx:70:    <div className="flex flex-col items-center gap-inline rounded-card border border-hairline bg-surface-1 p-major text-center">
frontend/src/views/HistoryView.tsx:162:          <div className="flex items-center gap-inline rounded-control border border-hairline bg-surface-1 px-inline py-hair focus-within:border-hairline-strong">
frontend/src/views/HistoryView.tsx:180:            className="flex flex-col items-center gap-inline rounded-card border border-hairline border-l-2 border-l-warn bg-surface-1 p-body text-center"
frontend/src/views/HistoryView.tsx:209:                    {/* open → the conversation's OWN surface, READ-ONLY (the route
frontend/src/views/HistoryView.tsx:210:                        loads it via /deep|build/:cid; the surface subscribes + replays
frontend/src/views/HistoryView.tsx:218:                            : c.surface === "deep_research"
frontend/src/views/HistoryView.tsx:220:                              : c.surface === "agent"
frontend/src/views/HistoryView.tsx:222:                                : c.surface === "build"
frontend/src/views/HistoryView.tsx:234:                        {c.surface === "deep_research" && (
frontend/src/views/HistoryView.tsx:239:                        {c.surface === "build" && (
frontend/src/views/HistoryView.tsx:244:                        {c.surface === "agent" && (
frontend/src/views/ActivityView.dashboard.test.tsx:3: * schedule-run history surface.
frontend/src/views/ActivityView.dashboard.test.tsx:8: *   2. Completion (running list goes empty) updates BOTH surfaces — the
frontend/src/views/ActivityView.dashboard.test.tsx:11: *   3. The schedule-run history surface ("Recent scheduled runs") renders the
frontend/src/views/ActivityView.dashboard.test.tsx:16: * All three surfaces consume the EXISTING activity feed
frontend/src/views/ActivityView.dashboard.test.tsx:41:      surface: "build",
frontend/src/views/ActivityView.dashboard.test.tsx:48:      surface: "agent",
frontend/src/views/ActivityView.dashboard.test.tsx:113: *  surfaces against the SAME `["activity"]` query data. */
frontend/src/views/ActivityView.dashboard.test.tsx:124:                <Route index element={<div>Home surface</div>} />
frontend/src/views/ActivityView.dashboard.test.tsx:126:                <Route path="*" element={<div>Home surface</div>} />
frontend/src/views/ActivityView.dashboard.test.tsx:167:    //     surfaces the count pill from the same data. Scope to the
frontend/src/views/ActivityView.dashboard.test.tsx:176:    // (3) Both running tasks are listed, with their titles, surfaces, and
frontend/src/views/ActivityView.dashboard.test.tsx:183:    // surface, both honest about what the runtime actually reported.
frontend/src/views/ActivityView.dashboard.test.tsx:187:    // lives, and clicks open the correct surface, not a generic 404).
frontend/src/views/ActivityView.dashboard.test.tsx:189:    // ModeSlider's "build" radio option (same string, different surface).
frontend/src/views/ActivityView.dashboard.test.tsx:228:    //     NOT clear the audit history. The schedule-run history surface
frontend/src/views/ActivityView.dashboard.test.tsx:234:  it("schedule-run history surface: every ScheduleRunRecord in the feed is rendered, with the coalesced flag", () => {
frontend/src/views/ActivityView.dashboard.test.tsx:240:    // surface — it lists EVERY ScheduleRunRecord from the activity feed's
frontend/src/views/ActivityView.dashboard.test.tsx:250:    //    title — proves the surface is per-record, not per-schedule.
frontend/src/views/ActivityView.dashboard.test.tsx:252:    // 3) The coalesced flag from the feed surfaces as a "coalesced" badge
frontend/src/views/ActivityView.dashboard.test.tsx:258:  it("schedule-run history is updated live: a new run appended to the feed appears in the surface", async () => {
frontend/src/views/ActivityView.dashboard.test.tsx:268:    // surface re-renders against the new feed (one shared query → no
frontend/src/views/ActivityView.dashboard.test.tsx:289:    // The previous rows are still there (the surface is append-only /
frontend/src/views/ActivityView.dashboard.test.tsx:307:        surface: "build" as const,
frontend/src/views/ActivityView.dashboard.test.tsx:345:  it("the dashboard's running-task buttons open the correct surface (build/agent/deep) when clicked", async () => {
frontend/src/views/ActivityView.dashboard.test.tsx:349:    // mount the destination surfaces here (they have their own large test
frontend/src/views/ActivityView.dashboard.test.tsx:360:    // surface chip. Click it — MemoryRouter pushes the route; we don't
frontend/src/views/ShareView.tsx:123:    <div className="border-b border-hairline bg-surface-1 px-body py-inline">
frontend/src/views/ShareView.tsx:135:          {bundle.surface ?? "build"}
frontend/src/views/ImportedRunView.tsx:77:    <div className="border-b border-hairline bg-surface-1 px-body py-inline">
frontend/src/views/ActivityView.tsx:7:/** Where a running task opens — the surface that produced it. */
frontend/src/views/ActivityView.tsx:8:function surfacePath(t: RunningTask): string {
frontend/src/views/ActivityView.tsx:9:  if (t.surface === "deep_research") return `/deep/${t.id}`;
frontend/src/views/ActivityView.tsx:10:  if (t.surface === "agent") return `/agent/${t.id}`;
frontend/src/views/ActivityView.tsx:11:  if (t.surface === "build") return `/build/${t.id}`;
frontend/src/views/ActivityView.tsx:37: * scheduled-run history. Read-only; clicking a running task opens its surface
frontend/src/views/ActivityView.tsx:65:            className="flex flex-col items-center gap-inline rounded-card border border-hairline border-l-2 border-l-warn bg-surface-1 p-body text-center"
frontend/src/views/ActivityView.tsx:104:                        onClick={() => navigate(surfacePath(t))}
frontend/src/views/ActivityView.tsx:117:                            {t.surface && (
frontend/src/views/ActivityView.tsx:119:                                {t.surface === "deep_research" ? "deep" : t.surface}
frontend/src/views/ProjectsView.tsx:2: * Projects — the persistent Build-workspace library, a SEPARATE surface from
frontend/src/views/ProjectsView.tsx:4: * vs. resumable build workspaces). Reuses the History surface's list-and-reopen
frontend/src/views/ProjectsView.tsx:7: * files-missing badge that surfaces graceful-failure honestly).
frontend/src/views/ProjectsView.tsx:34:          <span className="h-3 w-2/3 animate-pulse rounded-full bg-surface-2" />
frontend/src/views/ProjectsView.tsx:35:          <span className="h-2 w-24 animate-pulse rounded-full bg-surface-2" />
frontend/src/views/ProjectsView.tsx:51:    <div className="flex flex-col items-center gap-inline rounded-card border border-hairline bg-surface-1 p-major text-center">
frontend/src/views/ProjectsView.tsx:70:    <div className="flex flex-col items-center gap-inline rounded-card border border-hairline bg-surface-1 p-major text-center">
frontend/src/views/ProjectsView.tsx:96:          navigate(project.surface === "agent" ? `/agent/${project.id}` : `/build/${project.id}`)
frontend/src/views/ProjectsView.tsx:181:          <div className="flex items-center gap-inline rounded-control border border-hairline bg-surface-1 px-inline py-hair focus-within:border-hairline-strong">
frontend/src/views/ProjectsView.tsx:199:            className="flex flex-col items-center gap-inline rounded-card border border-hairline border-l-2 border-l-warn bg-surface-1 p-body text-center"
frontend/src/views/settings.test.tsx:166:    // isolation tiers are surfaced at the point of choice
frontend/src/views/settings.test.tsx:171:  it("surfaces the local → tighter-confirmation coupling when local is selected", async () => {
frontend/src/views/history.test.tsx:28:    // The other owner's conversation must never surface.
frontend/src/shell/NavRail.tsx:33:  // Projects is its OWN surface — distinct from History (ephemeral research) and
frontend/src/shell/NavRail.tsx:58:      className="flex h-full flex-col gap-section border-r border-hairline bg-surface-1 py-section"
frontend/src/shell/NavRail.tsx:109:                  // "New" is the home/start affordance: reset the surface to the
frontend/src/shell/NavRail.tsx:111:                  // surface they were last on (e.g. a Build). Other items navigate
frontend/src/shell/NavRail.tsx:122:                      ? "bg-surface-2 text-accent"
frontend/src/shell/NavRail.tsx:123:                      : "text-text-muted hover:bg-surface-2 hover:text-text",
frontend/src/shell/NavRail.tsx:155:            "flex w-full items-center gap-inline rounded-control px-inline py-inline font-ui text-[0.82rem] text-text-muted transition-colors hover:bg-surface-2 hover:text-text",
frontend/src/shell/NavRail.test.tsx:11:// Mock the mode context so we can assert "New" resets the surface to Search.
frontend/src/shell/NavRail.test.tsx:29:  it("resets the surface to the default search mode when 'New' is clicked", () => {
frontend/src/shell/ModeSlider.tsx:18: * surface framed for software), and Agent (the same machinery, general-task
frontend/src/shell/ModeSlider.tsx:29:      className="inline-flex items-center rounded-full border border-hairline bg-surface-1 p-px"
frontend/src/shell/ModeSlider.tsx:48:              active && "bg-surface-2 text-text",
frontend/src/shell/Shell.test.tsx:19:              <Route index element={<div>Home surface</div>} />
frontend/src/shell/Shell.test.tsx:22:              <Route path="*" element={<div>Home surface</div>} />
frontend/src/shell/Shell.test.tsx:66:    expect(screen.getByText("Home surface")).toBeInTheDocument();
frontend/src/shell/mode.ts:7: * Search (grounded research), Build (the agent surface framed for software), and
frontend/src/shell/mode.ts:12: * CHILD LEVEL (the in-chat scope control): a per-mode option set surfaced by ONE
frontend/src/shell/mode.ts:15: * Agent → a single Standard scope (the surface doesn't render a scope control).
frontend/src/shell/mode.ts:28:  { id: "build", label: "build", dormant: false }, // the agent surface, software framing
frontend/src/shell/mode.ts:66:  // Agent has no meaningful sub-scopes (it's "do a task"), and the surface never
frontend/src/shell/mode.test.ts:7:describe("mode model — agent surface", () => {
frontend/src/shell/ModeProvider.tsx:6: * the main surface (selector). No browser storage, per the data discipline.
frontend/src/components/QueryInput.tsx:30:   *  Research surface for Depth/Recency/Iterative so selecting DR doesn't add a

context compacted
codex
**Findings**

- **P0: `disco-verify` does not validate real delivered artifact bytes.** Validators only run when `Path(path).exists()` in the runner process cwd, so live workspace/sandbox artifacts are silently skipped: [runner.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/verify/runner.py:355). The runner has no artifact download/read method in its client abstraction: [runner.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/verify/runner.py:55), despite the real endpoint existing at [files.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/routes/files.py:222). Deck validation is also not invoked at all; only pdf/audio/sheet validators are wired: [runner.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/verify/runner.py:360).

- **P0: the procedural deck regression can still pass live verification.** The runner only detects `procedural_image_provider` from `image_generate` observations: [runner.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/verify/runner.py:333). Slides choose and stage image backends internally: [slides.py](/var/home/dylan/projects/disco/packages/tools/src/disco/tools/builtin/slides.py:387), [_slides_pipeline.py](/var/home/dylan/projects/disco/packages/tools/src/disco/tools/builtin/_slides_pipeline.py:603). A regression that embeds `pil-procedural` through the slides pipeline would not emit that observation and would not be caught unless the actual `.pptx` is downloaded and inspected.

- **P0: hangs can pass when no expected terminal status is configured.** `poll_until_terminal()` returns the last state on timeout instead of a timeout failure: [runner.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/verify/runner.py:184). Then `expected_status is None` makes any status acceptable: [runner.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/verify/runner.py:512). The test currently locks in that behavior: [test_disco_verify_runner.py](/var/home/dylan/projects/disco/packages/agent-server/tests/test_disco_verify_runner.py:509). `RUNNING` and `AWAITING_*` after timeout must fail.

- **P0: the missing-file scenario and tests contradict each other.** The exported scenario expects `FINISHED`: [scenarios.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/verify/scenarios.py:52), but tests expect/pass `ERROR`: [test_disco_verify_runner.py](/var/home/dylan/projects/disco/packages/agent-server/tests/test_disco_verify_runner.py:452), [test_disco_verify_runner.py](/var/home/dylan/projects/disco/packages/agent-server/tests/test_disco_verify_runner.py:535). Either CI should currently fail, or this coverage is stale/not running.

- **P0/P1: the Playwright evidence helpers use wrong API shapes.** `/events` returns a page object with an `events` array: [conversations.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/routes/conversations.py:173), but `discoClick` parses it like JSONL/top-level events: [discoClick.ts](/var/home/dylan/projects/disco/frontend/e2e-full/support/discoClick.ts:123). Assertions have the same shape bug: [assertions.ts](/var/home/dylan/projects/disco/frontend/e2e-full/support/assertions.ts:122). Artifact checks use `/workspace/{path}` instead of `/conversations/{cid}/artifacts/{path}`: [assertions.ts](/var/home/dylan/projects/disco/frontend/e2e-full/support/assertions.ts:249). State collection omits the conversation id: [artifactCollector.ts](/var/home/dylan/projects/disco/frontend/e2e-full/support/artifactCollector.ts:31). Trace assertions check `name`, but spans are emitted as `span`: [obs.py](/var/home/dylan/projects/disco/packages/core/src/disco/core/obs.py:67), [assertions.ts](/var/home/dylan/projects/disco/frontend/e2e-full/support/assertions.ts:211).

- **P1: CI gives false confidence for the live harness.** Nightly is advisory: [evidence-harness.yml](/var/home/dylan/projects/disco/.github/workflows/evidence-harness.yml:63). Live scenario failures are explicitly ignored with `|| true`: [evidence-harness.yml](/var/home/dylan/projects/disco/.github/workflows/evidence-harness.yml:88). The workflow claims heavy validators, but the nightly path does not actually install/run LibreOffice/poppler rendering.

- **P1: defaulting slides to `pptx` is a behavior break.** The default is now `pptx`: [slides.py](/var/home/dylan/projects/disco/packages/tools/src/disco/tools/builtin/slides.py:62). Markdown/no-goal callers now go down the Marp path and fail when Marp is absent instead of falling back to HTML: [slides.py](/var/home/dylan/projects/disco/packages/tools/src/disco/tools/builtin/slides.py:575). That may be intentional, but it is not compatibility-neutral.

- **P1: W1 fixes missing-file reads, but not read errors generally.** Multiple inheritance here is probably sound enough because `SandboxError` is pure Python and `FileNotFoundError` is the only builtin layout exception: [base.py](/var/home/dylan/projects/disco/packages/tools/src/disco/tools/sandbox/base.py:52). But `raise_read_error()` only maps “no such file”: [base.py](/var/home/dylan/projects/disco/packages/tools/src/disco/tools/sandbox/base.py:74). Permission, directory, and malformed-path read failures remain bare `SandboxError`, while loop code still catches only `FileNotFoundError`/`OSError`: [view_render.py](/var/home/dylan/projects/disco/packages/core/src/disco/core/loop/view_render.py:82), [file_state.py](/var/home/dylan/projects/disco/packages/core/src/disco/core/loop/file_state.py:139). `looks_like_not_found()` is brittle substring parsing: [base.py](/var/home/dylan/projects/disco/packages/tools/src/disco/tools/sandbox/base.py:62).

- **P1: W1 guardrails mostly inspect text, not behavior.** The guardrail regex only catches very specific source patterns: [test_sandbox_read_guardrail.py](/var/home/dylan/projects/disco/packages/tools/tests/test_sandbox_read_guardrail.py:20). The behavioral test exercises only the process sandbox: [test_sandbox_exception_contract.py](/var/home/dylan/projects/disco/packages/tools/tests/test_sandbox_exception_contract.py:61). Container/podman/gvisor typed-read behavior is still largely untested.

- **P1: task supervision is best-effort.** `_on_run_task_done()` schedules terminalization with `asyncio.create_task()` and drops it if the loop is shutting down/no loop exists: [runtime.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/runtime.py:1450). Tests sleep and observe the happy path rather than awaiting or forcing store/loop-shutdown failures: [test_task_supervision.py](/var/home/dylan/projects/disco/packages/agent-server/tests/integration/test_task_supervision.py:73). Excluding `IDLE` from concluded statuses is correct for surfacing an IDLE crash.

- **P1: artifact validators can crash instead of returning failures.** External binaries are called without `FileNotFoundError` handling: [artifact_validators.py](/var/home/dylan/projects/disco/packages/tools/src/disco/tools/verify/artifact_validators.py:11), [artifact_validators.py](/var/home/dylan/projects/disco/packages/tools/src/disco/tools/verify/artifact_validators.py:39). Corrupt `.pptx` can raise `BadZipFile`: [artifact_validators.py](/var/home/dylan/projects/disco/packages/tools/src/disco/tools/verify/artifact_validators.py:135). `_looks_procedural()` is a weak heuristic: [artifact_validators.py](/var/home/dylan/projects/disco/packages/tools/src/disco/tools/verify/artifact_validators.py:75). It will false-positive simple flat charts with diagonal lines and false-negative antialiased/recompressed placeholders.

- **P2: redaction is incomplete as a general sanitizer.** `redact()` recurses only through `dict` and `list`: [schema.py](/var/home/dylan/projects/disco/packages/core/src/disco/core/evidence/schema.py:79). Tuples, sets, dataclasses, Pydantic models, non-dict mappings, and secret-looking values under neutral keys can leak unless callers serialize first.

- **P2: the new evidence route is mostly safe, but the old trace route is not redacted.** The evidence endpoint is gated and redacted: [debug.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/routes/debug.py:61). Path traversal appears covered by `ProjectStore` id validation. But `/api/debug/trace/{conversation_id}` still returns raw trace snapshots: [debug.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/routes/debug.py:45).

**Coverage Gaps / False Confidence**

- W15 tests lean on a fake client instead of the real HTTP/artifact boundary, so they miss route shape, artifact download, auth, and workspace path issues.
- W11 smoke tests mostly prove helper imports, not that helpers correctly verify backend evidence. `discoClick` records `status_found/ws_found/events_found` but does not fail when expected evidence is missing: [discoClick.ts](/var/home/dylan/projects/disco/frontend/e2e-full/support/discoClick.ts:253).
- UI inventory deduplicates by `controlId`, so duplicate visible controls collapse into one coverage hit: [uiInventory.ts](/var/home/dylan/projects/disco/frontend/e2e-full/support/uiInventory.ts:262), [uiInventory.ts](/var/home/dylan/projects/disco/frontend/e2e-full/support/uiInventory.ts:304).
- Validators cover pdf/audio/sheet/deck partially. App/site deliverables, standalone images, docx/text/markdown reports, zipped project outputs, and multi-file artifacts have little or no semantic validation.
- W8 is a useful real router/agent boundary test, but it does not cover real HTTP/WS or non-process sandbox backends.

**Priorities**

- **P0:** make timeout/nonterminal states fail; fix scenario expectation mismatch; make `disco-verify` download/read actual artifacts and run deck validation; fix Playwright helper endpoint shapes.
- **P1:** remove `|| true`/advisory-only masking for the live job you care about; add behavioral sandbox read tests for container/podman/gvisor; harden validators to return findings instead of raising; decide whether `pptx` default break is acceptable.
- **P2:** broaden redaction, redact or remove the legacy trace route, improve UI inventory uniqueness, and add validators for non-deck deliverable classes.
tokens used
266,614
**Findings**

- **P0: `disco-verify` does not validate real delivered artifact bytes.** Validators only run when `Path(path).exists()` in the runner process cwd, so live workspace/sandbox artifacts are silently skipped: [runner.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/verify/runner.py:355). The runner has no artifact download/read method in its client abstraction: [runner.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/verify/runner.py:55), despite the real endpoint existing at [files.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/routes/files.py:222). Deck validation is also not invoked at all; only pdf/audio/sheet validators are wired: [runner.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/verify/runner.py:360).

- **P0: the procedural deck regression can still pass live verification.** The runner only detects `procedural_image_provider` from `image_generate` observations: [runner.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/verify/runner.py:333). Slides choose and stage image backends internally: [slides.py](/var/home/dylan/projects/disco/packages/tools/src/disco/tools/builtin/slides.py:387), [_slides_pipeline.py](/var/home/dylan/projects/disco/packages/tools/src/disco/tools/builtin/_slides_pipeline.py:603). A regression that embeds `pil-procedural` through the slides pipeline would not emit that observation and would not be caught unless the actual `.pptx` is downloaded and inspected.

- **P0: hangs can pass when no expected terminal status is configured.** `poll_until_terminal()` returns the last state on timeout instead of a timeout failure: [runner.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/verify/runner.py:184). Then `expected_status is None` makes any status acceptable: [runner.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/verify/runner.py:512). The test currently locks in that behavior: [test_disco_verify_runner.py](/var/home/dylan/projects/disco/packages/agent-server/tests/test_disco_verify_runner.py:509). `RUNNING` and `AWAITING_*` after timeout must fail.

- **P0: the missing-file scenario and tests contradict each other.** The exported scenario expects `FINISHED`: [scenarios.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/verify/scenarios.py:52), but tests expect/pass `ERROR`: [test_disco_verify_runner.py](/var/home/dylan/projects/disco/packages/agent-server/tests/test_disco_verify_runner.py:452), [test_disco_verify_runner.py](/var/home/dylan/projects/disco/packages/agent-server/tests/test_disco_verify_runner.py:535). Either CI should currently fail, or this coverage is stale/not running.

- **P0/P1: the Playwright evidence helpers use wrong API shapes.** `/events` returns a page object with an `events` array: [conversations.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/routes/conversations.py:173), but `discoClick` parses it like JSONL/top-level events: [discoClick.ts](/var/home/dylan/projects/disco/frontend/e2e-full/support/discoClick.ts:123). Assertions have the same shape bug: [assertions.ts](/var/home/dylan/projects/disco/frontend/e2e-full/support/assertions.ts:122). Artifact checks use `/workspace/{path}` instead of `/conversations/{cid}/artifacts/{path}`: [assertions.ts](/var/home/dylan/projects/disco/frontend/e2e-full/support/assertions.ts:249). State collection omits the conversation id: [artifactCollector.ts](/var/home/dylan/projects/disco/frontend/e2e-full/support/artifactCollector.ts:31). Trace assertions check `name`, but spans are emitted as `span`: [obs.py](/var/home/dylan/projects/disco/packages/core/src/disco/core/obs.py:67), [assertions.ts](/var/home/dylan/projects/disco/frontend/e2e-full/support/assertions.ts:211).

- **P1: CI gives false confidence for the live harness.** Nightly is advisory: [evidence-harness.yml](/var/home/dylan/projects/disco/.github/workflows/evidence-harness.yml:63). Live scenario failures are explicitly ignored with `|| true`: [evidence-harness.yml](/var/home/dylan/projects/disco/.github/workflows/evidence-harness.yml:88). The workflow claims heavy validators, but the nightly path does not actually install/run LibreOffice/poppler rendering.

- **P1: defaulting slides to `pptx` is a behavior break.** The default is now `pptx`: [slides.py](/var/home/dylan/projects/disco/packages/tools/src/disco/tools/builtin/slides.py:62). Markdown/no-goal callers now go down the Marp path and fail when Marp is absent instead of falling back to HTML: [slides.py](/var/home/dylan/projects/disco/packages/tools/src/disco/tools/builtin/slides.py:575). That may be intentional, but it is not compatibility-neutral.

- **P1: W1 fixes missing-file reads, but not read errors generally.** Multiple inheritance here is probably sound enough because `SandboxError` is pure Python and `FileNotFoundError` is the only builtin layout exception: [base.py](/var/home/dylan/projects/disco/packages/tools/src/disco/tools/sandbox/base.py:52). But `raise_read_error()` only maps “no such file”: [base.py](/var/home/dylan/projects/disco/packages/tools/src/disco/tools/sandbox/base.py:74). Permission, directory, and malformed-path read failures remain bare `SandboxError`, while loop code still catches only `FileNotFoundError`/`OSError`: [view_render.py](/var/home/dylan/projects/disco/packages/core/src/disco/core/loop/view_render.py:82), [file_state.py](/var/home/dylan/projects/disco/packages/core/src/disco/core/loop/file_state.py:139). `looks_like_not_found()` is brittle substring parsing: [base.py](/var/home/dylan/projects/disco/packages/tools/src/disco/tools/sandbox/base.py:62).

- **P1: W1 guardrails mostly inspect text, not behavior.** The guardrail regex only catches very specific source patterns: [test_sandbox_read_guardrail.py](/var/home/dylan/projects/disco/packages/tools/tests/test_sandbox_read_guardrail.py:20). The behavioral test exercises only the process sandbox: [test_sandbox_exception_contract.py](/var/home/dylan/projects/disco/packages/tools/tests/test_sandbox_exception_contract.py:61). Container/podman/gvisor typed-read behavior is still largely untested.

- **P1: task supervision is best-effort.** `_on_run_task_done()` schedules terminalization with `asyncio.create_task()` and drops it if the loop is shutting down/no loop exists: [runtime.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/runtime.py:1450). Tests sleep and observe the happy path rather than awaiting or forcing store/loop-shutdown failures: [test_task_supervision.py](/var/home/dylan/projects/disco/packages/agent-server/tests/integration/test_task_supervision.py:73). Excluding `IDLE` from concluded statuses is correct for surfacing an IDLE crash.

- **P1: artifact validators can crash instead of returning failures.** External binaries are called without `FileNotFoundError` handling: [artifact_validators.py](/var/home/dylan/projects/disco/packages/tools/src/disco/tools/verify/artifact_validators.py:11), [artifact_validators.py](/var/home/dylan/projects/disco/packages/tools/src/disco/tools/verify/artifact_validators.py:39). Corrupt `.pptx` can raise `BadZipFile`: [artifact_validators.py](/var/home/dylan/projects/disco/packages/tools/src/disco/tools/verify/artifact_validators.py:135). `_looks_procedural()` is a weak heuristic: [artifact_validators.py](/var/home/dylan/projects/disco/packages/tools/src/disco/tools/verify/artifact_validators.py:75). It will false-positive simple flat charts with diagonal lines and false-negative antialiased/recompressed placeholders.

- **P2: redaction is incomplete as a general sanitizer.** `redact()` recurses only through `dict` and `list`: [schema.py](/var/home/dylan/projects/disco/packages/core/src/disco/core/evidence/schema.py:79). Tuples, sets, dataclasses, Pydantic models, non-dict mappings, and secret-looking values under neutral keys can leak unless callers serialize first.

- **P2: the new evidence route is mostly safe, but the old trace route is not redacted.** The evidence endpoint is gated and redacted: [debug.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/routes/debug.py:61). Path traversal appears covered by `ProjectStore` id validation. But `/api/debug/trace/{conversation_id}` still returns raw trace snapshots: [debug.py](/var/home/dylan/projects/disco/packages/agent-server/src/disco/agent_server/routes/debug.py:45).

**Coverage Gaps / False Confidence**

- W15 tests lean on a fake client instead of the real HTTP/artifact boundary, so they miss route shape, artifact download, auth, and workspace path issues.
- W11 smoke tests mostly prove helper imports, not that helpers correctly verify backend evidence. `discoClick` records `status_found/ws_found/events_found` but does not fail when expected evidence is missing: [discoClick.ts](/var/home/dylan/projects/disco/frontend/e2e-full/support/discoClick.ts:253).
- UI inventory deduplicates by `controlId`, so duplicate visible controls collapse into one coverage hit: [uiInventory.ts](/var/home/dylan/projects/disco/frontend/e2e-full/support/uiInventory.ts:262), [uiInventory.ts](/var/home/dylan/projects/disco/frontend/e2e-full/support/uiInventory.ts:304).
- Validators cover pdf/audio/sheet/deck partially. App/site deliverables, standalone images, docx/text/markdown reports, zipped project outputs, and multi-file artifacts have little or no semantic validation.
- W8 is a useful real router/agent boundary test, but it does not cover real HTTP/WS or non-process sandbox backends.

**Priorities**

- **P0:** make timeout/nonterminal states fail; fix scenario expectation mismatch; make `disco-verify` download/read actual artifacts and run deck validation; fix Playwright helper endpoint shapes.
- **P1:** remove `|| true`/advisory-only masking for the live job you care about; add behavioral sandbox read tests for container/podman/gvisor; harden validators to return findings instead of raising; decide whether `pptx` default break is acceptable.
- **P2:** broaden redaction, redact or remove the legacy trace route, improve UI inventory uniqueness, and add validators for non-deck deliverable classes.
CODEX_DONE exit=0
