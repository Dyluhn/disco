# RP wave 3–4 dispatch-readiness recon (2026-06-11)

Four parallel Explore passes turned plan §RP-07/08/09/11 into surgical manifests.
**Collision facts below are ORCHESTRATOR-VERIFIED against the code**, correcting
the scouts where they guessed. rp-05c currently owns (uncommitted): `engine.py`,
`tools/executor.py`, `tools/registry.py`, `tools/mcp/tool_search.py`,
`agent-server/runtime.py`.

## Verified seam reality (the parallelism ceiling)

- A new builtin tool is only callable if its name is in the `AGENT_TOOLS`
  frozenset at `packages/tools/src/disco/tools/registry.py:53`
  (`agent_scope()` → `in_scope()` filters by it). So **every new tool touches
  registry.py** — rp-05c-owned. RP-11's "zero collision" claim is FALSE.
- Backend feature endpoints funnel through `agent-server/runtime.py` (new
  methods) + `agent-server/app.py` (new routes). RP-07 and RP-08 both need BOTH
  → they collide with rp-05c AND with each other. Agent-server backend work is
  **serial on the seam**.
- Frontend halves touch distinct files (`deepResearch.ts`, `grounded.ts`,
  `blocks.tsx`, `*Surface.tsx`) → genuinely parallel, zero seam contact.

## Dispatch order (post rp-05c commit)

| Order | Backend collides on | FE-only files (parallelizable) | Wave |
|-------|--------------------|--------------------------------|------|
| RP-07 export | runtime.py, app.py | deepResearch.ts, DeepResearchSurface.tsx, useDeepResearch.ts | 3 |
| RP-08 scheduler | runtime.py, app.py, sqlite.py, events.py | ScheduleSection.tsx, scheduleNL.ts | 3 |
| RP-09 audio | registry.py (+new builtin files) | (blocks/deliverable render) | 4 |
| RP-11 sheets | registry.py (+new builtin files) | grounded.ts, blocks.tsx | 4 |

**Parallel structure once the seam frees:** one backend order on runtime/app at a
time (serial); frontend halves of the others fan out concurrently; new-tool
builtin modules (rp-09/rp-11) run parallel except the one-line `AGENT_TOOLS` edit
(batch them, or land sequentially).

---

## RP-08 — Scheduled tasks (wave 3)

**Locked:** cronsim (NOT croniter/APScheduler); ONE asyncio loop in app lifespan;
runs append to the SAME conversation; run-once-coalesced missed-run policy;
persist depth/model_override in the schedule row; next-3-runs confirm card; NL
parse RFC-2445 → dateparser → never-silent.

**Manifest:** CREATE `agent-server/schedule.py` (ScheduleManager), `schedule_models.py`,
tests `test_schedule_coalesce.py` + `test_schedule_e2e.py`, FE `ScheduleSection.tsx`
+ `scheduleNL.ts`. MODIFY `core/events.py` (ScheduleEvent/ScheduleRunEvent),
`core/store/sqlite.py` (schedules + schedule_runs tables), `agent-server/runtime.py`
(lifecycle + create/list/delete methods — NEW methods, seam-collision with rp-05c),
`agent-server/app.py` (4 routes), `agent-server/pyproject.toml` (cronsim).

**Integration points (verified seams):** lifespan wiring at `app.py:99-128` next to
`idle_sweep_task`; runner pattern = `_idle_sweep_loop()` at `runtime.py:1654-1664`;
event-append seam = the `reconcile_orphaned_runs` pattern `runtime.py:763-813,1190-1212`;
depth/model_override dicts at `runtime.py:366-367,981-989`; store schema `sqlite.py:46-54`.

**Acceptance:** 2-min recurring schedule → two runs append to the SAME conversation
event stream; kill server between runs → restart → exactly ONE coalesced catch-up
(not 2+); DST-boundary unit tests via cronsim; confirm-card screenshot.

**Open Qs for the brief:** one-shot editability; manual RRULE input vs NL-only;
confirm-card modal vs inline.

---

## RP-07 — Report export PDF/DOCX (wave 3)

**Locked:** pandoc (system binary, subprocess — lighter than pypandoc) + WeasyPrint
(~150-200MB); NO LaTeX/wkhtmltopdf; rides the BP-08/BP-04 VM-201 image rebuild
(CODE builds now, live-acceptance needs the image). Current export = client-side md
only at `frontend/src/api/deepResearch.ts:66-127`.

**Manifest (code now):** CREATE `agent-server/report_export.py` (md/pdf/docx
serializers; port `serializeReportToMarkdown` from deepResearch.ts:88-127), tests
`test_report_export.py`. MODIFY `agent-server/runtime.py` (`export_report()` method —
seam-collision), `agent-server/app.py` (`POST /api/conversations/{cid}/report/export?fmt=`
~line 575, mirrors share_export at app.py:975), `agent-server/pyproject.toml`,
FE `deepResearch.ts` + `DeepResearchSurface.tsx` + `useDeepResearch.ts` (fmt param +
3 download options). IMAGE-ONLY (note, don't build): `deploy/sandbox/Dockerfile`
(pandoc + libpango + weasyprint layer after the Chromium block).

**Acceptance:** live deep-research run → download PDF + DOCX → open both → screenshot;
markdown export still byte-identical; missing report → 404, bad fmt → 400; SendUserFile
the PDF.

**Ambiguity:** no StandardReportEvent exists — endpoint is generic over ReportEvent;
document the assumption (deep reports only today).

---

## RP-09 — Audio overviews (wave 4)

**Locked:** Kokoro via Speaches (NOT Piper); voices **af_heart + af_bella** (Dylan
ratified — binding over any am_michael in the plan body; FLAG the discrepancy);
NO SSML (mixer inserts 300-600ms inter-turn silence); JSON turn-script
`[{speaker,text}]`, validate + 1 retry; 100-200 tok/turn; MP3 + transcript as
DeliverableEvent. ZERO TTS code in tree today.

**Manifest:** CREATE `tools/builtin/audio_overview.py`, `tools/builtin/_audio_mixer.py`,
`agent-server/audio_config.py` (VOICE_A/B constants + SPEACHES_URL env), tests
`test_audio_overview_tool.py` + `test_audio_overview_integration.py`. MODIFY
`tools/builtin/__init__.py` (register), **`tools/registry.py` AGENT_TOOLS** (add
`audio_overview` — seam-collision), optional `app.py` Speaches health-check.

**Speaches contract:** `POST ${SPEACHES_URL}/v1/audio/speech` {text,voice_id,format:mp3},
30s/turn timeout, fail-soft if offline. NOT a local-GPU dependency (separate service).

**Acceptance:** deep report → audio generated → MP3 + transcript delivered → manual
listen check; mock-Speaches unit tests for schema/silence/chunk bounds.

---

## RP-11 — Sheets (wave 4)

**Locked:** openpyxl (write FORMULAS not values) + formula whitelist (SUM/AVERAGE/IF/
VLOOKUP/INDEX/MATCH/DATE… reject IMPORTXML/INDIRECT); Univer viewer (Luckysheet EOL);
values UNVALIDATED — UI flags honestly; defer LibreOffice recalc. Mirror RP-03's
chart-block end-to-end wiring (commit 8217e8e).

**Manifest:** CREATE `tools/builtin/sheets.py` (SheetsTool → .xlsx via openpyxl,
`ToolOutcome(artifacts=[path])`), tests `test_sheets.py`. MODIFY `tools/builtin/__init__.py`,
**`tools/registry.py` AGENT_TOOLS** (add `sheet_generate` — seam-collision), FE
`grounded.ts` (`kind:"sheet"` in AnswerBlock union) + `blocks.tsx` (Univer iframe +
download), `frontend/package.json` (@univer/core,@univer/sheets).

**Verified:** artifacts→DeliverableEvent flow already exists at `engine.py:2230` (no
engine change). Decided scope: **agent-side tool only** (sheets need model-authored
formulas + workspace write, unlike research-streamed chart blocks).

**Acceptance:** real build emits .xlsx with live formulas → opens in LibreOffice
(manual) → Univer render screenshot; unit tests for round-trip/formula-preservation/
whitelist.
