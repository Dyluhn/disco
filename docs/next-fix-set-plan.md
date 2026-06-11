# RP pack — the next structured fix set (Release Pack)

Written 2026-06-10 from the directive: *"analyze the gaps, research exhaustively to
find the right answers to those gaps, and develop a plan to fix them for the next
structured set of fixes."* Research basis: 4 repo-seam sweeps (file:line cited
throughout) + 4 external research briefs (MCP, artifacts, replay/audio/scheduling,
parallel local inference). Every mechanism below is a **locked decision** with its
evidence, not an option list — same discipline as the BP pack
(`docs/workorders/README.md` global rules apply verbatim: workers never commit,
diff review is not delegable, visual evidence mandatory, no cheap workarounds).

**Precondition: the BP campaign finishes first** (08-verify → 04 → 05 → 00 →
09/11/12 → 13 → 14/15 → 16). RP-07/RP-10 ride the same sandbox-image rebuild as
BP-08/BP-04, and RP-12 extends BP-08's kernel.

**RATIFIED 2026-06-10 — Dylan's four calls, now binding:** share links
tailnet-only; **Chromium ALWAYS in the sandbox image** (slides export ungated);
RP-04b profiling SKIPPED; audio duo **af_heart + af_bella**.

---

## 1. Gap inventory — what remains between "BP pack done" and the goal

Goal (release-roadmap.md): **Manus-grade task UX, reliable on self-hosted open
weights, one-command deploy.** BP covers Phase 0 + Phase 0.5 + the
lifecycle/upload slice of Phase 1. What it does NOT cover:

| # | Gap | Roadmap phase | Research | RP order |
|---|---|---|---|---|
| 1 | MCP client (placeholder UI only — `McpSection.tsx:8-72` NotWired) | 1 | R1+R5 | RP-05 |
| 2 | Clarifying questions before plan | 1 | repo | RP-13 |
| 3 | Deep-report follow-up turns | 1 | R3+R7 | RP-13 |
| 4 | Report export PDF/DOCX (client-side md only, `deepResearch.ts:66-127`) | 1 | R2+R6 | RP-07 |
| 5 | Usability debt (isolation hardcode `BuildSurface.tsx:44`, theme persist, Cmd+K, history search, cost meter) | 1 | repo | RP-14 |
| 6 | Tables render as raw pipes in deep reports (`DeepReportView.tsx:147-160`) | 1/2 | R2 | RP-02 |
| 7 | Chart blocks (NO chart kind exists in `grounded.ts:48-59` — roadmap was wrong that it did) | 2 | R2+R6 | RP-03 |
| 8 | Replay / share links | 2 | R3+R7 | RP-06 |
| 9 | Wide research fan-out (engine is strictly sequential, `engine.py:174`) | 2 | R4+R8 | RP-04 |
| 10 | Artifact engine: slides / sheets / docs | 2 | R6 | RP-10/11 |
| 11 | Audio overviews (ZERO TTS code in tree) | 2 | R7 | RP-09 |
| 12 | Scheduled tasks | 2 | R7 | RP-08 |
| 13 | Dashboard (status column exists, NEVER written — `store/sqlite.py:34-56`) | 2 | R3 | RP-01 |
| 14 | B-series residue: B2 partial (no write-to-file/anti-dump), B3 open (tail variation), B9 open (prefill masking) — none had BP orders | B | R4 | RP-12 |
| 15 | Phase 3 (compose deploy, eval-as-feature, security doc, gallery, mobile) | 3 | — | **next pack, not this one** |
| 16 | Process debt: real-sample harness rebuild, E3 param leak root cause, egress allowlist rollout | — | memory | RP-00 |

Phase 3 is deliberately excluded: it is packaging, and packaging a product that
still lacks the artifact/replay/MCP legs would ship the wrong thing. It becomes
the pack after this one.

---

## 2. Locked mechanism decisions, order by order

### RP-00 — Process debt (S, standing; some items fold into later orders)
- **Real-sample harness rebuild** per `memory/perpleximanus-test-harness-design.md`:
  eval runner + event-log replay + shared cassette layer on the existing
  `evaluation.py` + runtime injection seams. **Locked synergy:** the RP-06 share
  bundle IS the cassette format — one projection, two consumers. Build the
  scrubbed-export projection once, in RP-06, and the harness consumes it.
- **E3 param leak**: root-cause before RP-04 touches the engine (same files).
- **Egress allowlist proxy** (design done, 3 gotchas verified live on VM-201
  2026-06-07): rolls out as a *dependency of RP-05* — MCP HTTP servers MUST route
  through it. Not a separate order; it is RP-05 rung 0.

### RP-01 — Dashboard lite (S)
**Gap:** `conversations.status` column exists in the schema and is never written;
`ConversationSummaryDTO` lacks status; History can't show live state.
**Locked:** write-through `status` on every StatusEvent append (cache, with
`ConversationState.reconstruct()` — pure, `state.py:59` — as the fallback/repair
path on read), extend the DTO, surface status chips in History. No new runtime
machinery: runtime tracking stays the in-process dicts (`runtime.py:228-241`);
the column is a *projection*, not a source of truth.
**Gates:** live spec — start a build, History shows RUNNING without opening the
conversation, flips to FINISHED at terminal event; restart server mid-run →
reconcile_orphaned_runs path repairs the stale row. Screenshot + SendUserFile.

### RP-02 — Deep-report rich blocks (S)
**Gap:** all 5 AnswerBlock kinds have complete renderers (`blocks.tsx`, table at
168-210) but the backend never emits table; deep view splits markdown on blank
lines so tables render as raw pipes.
**Locked:** (a) ~40-line markdown-table detector in `_to_blocks()`
(`retrieval/streaming.py:109-171`) emitting the existing `table` kind — zero
frontend changes for standard research; (b) `react-markdown` + `remark-gfm`
(~10 lines, 1 dep) replacing the blank-line splitter in
`DeepReportView.tsx:147-160`.
**Gates:** behavioral run whose report contains a table; screenshot of the
rendered table in BOTH surfaces.

### RP-03 — Chart blocks (M)
**Locked:**
- New `chart` member in the AnswerBlock union (`grounded.ts:48-59`) with
  **narrow per-type JSON schemas** (bar/line/pie/scatter v1 — the GPT-Vis/DeerFlow
  pattern), **NOT raw Vega-Lite** — VegaChat (arXiv 2601.15385) measured 30.6%
  viz-error/35.1% empty-chart on unvalidated grammar output from small models.
- Backend validates with jsonschema + **one retry-with-error-trace** on failure;
  invalid after retry → degrade to table block (never a broken chart).
- Render client-side with **Chart.js 4** (67KB gzip). **No matplotlib in the
  sandbox image** — zero image cost; the Dockerfile's no-matplotlib stance
  (deploy/sandbox/Dockerfile:16) stands.
- Emission seams: deep-research synthesis prompt (charts proposed by the model as
  typed blocks) + `_to_blocks()` for standard research.
**Gates:** live build/research run that emits a real chart from real data;
schema-fuzz unit suite (malformed chart JSON → table degrade, never a crash);
screenshots.

### RP-04 — Wide research, phase 1: smart-client pipeline (M)
**The deciding finding:** MTP and `--parallel>1` are **mutually exclusive** in
llama.cpp (PR #22673 hard-errors at startup; tested on our exact model class).
Nobody in OSS (gpt-researcher/DeerFlow/STORM) parallelizes local decode — they
parallelize retrieval around a serialized LLM. So:
**Locked:**
- Keep MTP-sequential serving untouched. Restructure
  `deep_research/engine.py:174` (`for subq in pending:`) into a
  **producer/consumer pipeline**: ALL retrieval work (search, fetch, extract,
  embed, rerank) for all N sub-questions runs concurrently; LLM work
  (QUERY_REWRITER/RAG_ANSWERER synthesis) stays a **single-depth queue**;
  sub-question k+1's retrieval prefetches while k synthesizes. Expected ~1.5-2×
  wall-clock when retrieval is 30-50% of run time — instrument to confirm.
- Fix the three races the sweep found BEFORE adding concurrency:
  1. **`remaining` budget TOCTOU** (engine.py:196 — at EXHAUSTIVE caps the
     overshoot reaches ~246 passages): partition the budget N-ways upfront.
  2. **section_id collision** (`f"s{len(sections)}"`): derive from sub-question
     hash, not list length.
  3. **vector_store namespace**: per-sub-question namespace.
- First target tier: STANDARD_DEEP (6 subqs). Resume semantics (done_titles,
  engine.py:153-158) must survive — checkpoint per completed section, unchanged.
**RP-04b — REJECTED (Dylan, 2026-06-10):** the `np=4 --kv-unified
--spec-type ngram-mod` profiling session is skipped. Rationale stands on the
research: MTP⊥parallel means multi-slot gives up the 51 t/s MTP path for an
expected ~1.2-1.7× effective — the smart-client pipeline captures the win
without touching serving. Re-open only if llama.cpp ships MTP+parallel together.
**Gates:** instrumented LLM-busy vs retrieval-wait ratio before/after; A/B
wall-clock on the same 6-subq question; event-order invariants (section_done
per section, no interleaving corruption) asserted in an integration test;
killed-and-resumed mid-pipeline run completes correctly.

### RP-05 — MCP client (L — the big lever)
**Locked stack:**
- **Pin `mcp>=1.27.2,<2`** (1.27.2 = 2026-05-29; v2 is unreleased + breaking;
  the 2026-07-28 RC moves to a stateless core — do NOT couple our design to
  session-statefulness). Add `anyio>=4.4`. httpx/websockets/pydantic already
  present.
- Transports: **stdio + Streamable HTTP only.** SSE is deprecated — skip it
  entirely.
- **Static-per-session discovery** (R1's recommendation, fits the repo):
  connect/initialize the server pool at agent-server start; the per-conversation
  registry (`runtime.py:517-520 _compose_build_loop`) is built from the live
  pool at conversation start. No mid-conversation tool-list mutation; gate
  `list_changed` notifications behind re-approval.
- Naming: `mcp__<server>__<tool>`; server names short `[a-z0-9_]` (LibreChat's
  naming bugs). **Cap ~15-20 active schemas** in the 27B's context; beyond the
  cap, expose a `tool_search` meta-tool instead of raw schemas.
- Config: standard `mcpServers` map as `McpSettings` on RouterConfig
  (`config.py:139-160`) with per-server `enabled / allowed_tools / risk_tier /
  description_hash`. Secrets via the already-generic tools-layer
  `SecretsStore.get(name)` (`tools/secrets.py:68-90` broker keeps them out of
  the sandbox); generalize `core/llm/secrets.py` only if needed.
- ToolDefs: set `base_risk` **explicitly** per tool (RuleBasedAnalyzer scores
  unknown tools by NAME keywords, `analyzers.py:178-208` — never rely on it);
  `runs_in="in_process"` is a lie for MCP — stdio servers run **inside the
  gVisor sandbox**, HTTP servers route through the **egress allowlist proxy**
  (RP-00). This is the threat-model answer to R1's hardest question.
- **Security canon (every item mandatory):** hash-pin tool descriptions at
  approval, re-verify every session, human re-approval on ANY change (tool
  poisoning / rug pulls — Invariant Labs, CVE-2025-54136); fence every MCP
  result as untrusted by extending the browser `_fence()` pattern
  (`builtin/browser.py`) — CyberArk: every output channel is injectable;
  MCPTox: ~half of agents fail output-injection without fencing.
- Client lifecycle: **one task = one client lifecycle**, AsyncExitStack per
  connection, ALWAYS timeout `initialize()` (SDK #1452 hang); tolerate non-JSON
  stdout lines on stdio, capture stderr.
- **Retrieval-tier MCP** is a separate registry tier implementing the OpenAI
  `search(query)→{results:[{id,title,url}]}` / `fetch(id)→doc` contract —
  plugs into the @runtime_checkable retrieval Protocols
  (`providers.py:17-42`, wiring at `wiring.py:29-52`) so MCP sources ground
  citations exactly like built-in search.
- UI: wire `McpSection.tsx` for real (add/enable/approve/hash-diff
  re-approval); the NotWired banner comes off ONLY when the path works
  end-to-end (no false affordances).
**Gates:** real stdio server (filesystem/echo) used by a real build via gVisor;
real Streamable-HTTP server via the egress proxy; **poisoning drill** — mutate a
tool description between sessions → UI demands re-approval, run refuses until
granted; fenced-output injection drill (hostile tool result does not steer the
agent); live spec + screenshots of the MCP settings flow.

### RP-06 — Replay + share links (M)
**Locked:**
- Replay is nearly free: buildTrace selectors are **pure functions of
  AgentEvent[]** (`buildTrace.ts:299-327` et al.) — step-through =
  `events.slice(0, n)` behind a `useReplay` hook with a scrubber. Zero reducer
  changes. `GET /conversations/{cid}/events` already exists (app.py:163).
- Share = **export-time projection**, not a live door: `share_export(cid)` →
  scrubbed JSON bundle → static, self-contained viewer (claude-replay pattern) +
  `GET /share/<id>`; `share_tokens` table, base62 random, revocable. v1 is
  **read-only** (OpenHands PR #6380's replay-into-agent corner cases). Fork =
  v2, owner-only, seed-new-task-from-events≤N.
- **Redaction at export with ordered regex** (specific `sk_live_…` before
  generic `KEY=value`); shell/env/file-read outputs **sensitive by default**;
  scrub streamed deltas too (CVE-2026-41182 — deltas bypassed redaction). Manus
  itself has no redaction model; we ship one.
- The WS frame handler (`app.py:556 _handle_frame`) has no read-only mode —
  share NEVER touches the live WS; the static bundle sidesteps the whole risk.
- **The bundle doubles as the harness cassette format (RP-00).**
**Gates:** scrubber unit suite built from VERBATIM captured secrets-bearing
events (real-sample harness rule); shared link opened in a clean browser
profile replays a full real build; revocation works; screenshots.

### RP-07 — Report export PDF/DOCX (M; image decision coupled to BP-04)
**Locked:**
- Server endpoint `POST /api/conversations/{cid}/report/export?fmt=md|pdf|docx`.
  md serializer moves server-side (port of `serializeReportToMarkdown`,
  deepResearch.ts:66-127 — its own comment confirms no server path exists).
  Standard research gets export too, not just deep.
- Toolchain: **pandoc (34MB deb) + WeasyPrint (+pango libs), ≈150-200MB** in the
  sandbox image. pandoc is unavoidable for md→docx. **NO LaTeX** (4GB), **NO
  wkhtmltopdf** (archived). typst (16MB, PDF-only) optional later.
- Interim free win shipped first: `window.print()` + `@media print` CSS on the
  report views.
- Image rebuild rides the SAME VM-201 image-rebuild rung BP-08/BP-04 already
  require — one rebuild, not three.
**Gates:** real deep-research run → download PDF and DOCX → open both →
screenshots of the opened artifacts; SendUserFile the PDF itself.

### RP-08 — Scheduled tasks (M)
**Locked:**
- `schedules` + `schedule_runs` tables; **ONE asyncio scheduler loop** in the
  agent-server lifespan waking at `min(next_run_at)`, spawning ordinary tasks
  through the existing lifecycle (the `_suspend_after_grace` pattern,
  runtime.py:841-848, is the in-repo template). N workers = N schedulers is the
  classic bug — we have one process; keep it that way explicitly.
- Cron math: **cronsim** (Healthchecks' lib, Debian-cron DST semantics; Sentry
  migrated to it). **croniter is DEAD** (archived). **APScheduler 4 is alpha** —
  no. systemd timers can't be created at runtime — no.
- **Context persistence is the feature** (Manus 2.0's killer differentiator):
  a schedule points at a conversation; each run **appends as a continuation of
  the same event stream** — nearly free with our event sourcing. Cold-context
  scheduled runs produce junk; we never spawn cold.
- Persist `depth`/`model_override` IN the schedule row — `_depth` and
  `_model_override` are in-memory only (runtime.py) and die with the process.
- Missed runs: explicit policy, default **run-once-coalesced** on startup
  (reuse the reconcile_orphaned_runs startup-scan shape, runtime.py:763-813).
- NL parsing: **recurrent → RFC-2445 RRULE deterministic-first**, dateparser for
  one-shots, LLM fallback NEVER silent — and **always echo a next-3-runs
  confirmation card** before saving.
**Gates:** 2-minute recurring schedule → two runs append to the SAME
conversation visible in the UI; kill the server between runs → restart →
exactly one coalesced catch-up; DST-boundary unit tests via cronsim; confirmation
card screenshot.

### RP-09 — Audio overviews (M)
**Locked:**
- TTS = **Kokoro via Speaches** (validated by open-notebook on this homelab) —
  NOT Piper (dialogue quality). Default duo **af_heart + af_bella** (Dylan,
  2026-06-10 — the two highest-graded voices, A and A-; quality over
  gender-contrast). Blending available later if the speakers need more
  separation.
- **No SSML** (Kokoro has none): pacing via punctuation; the MIXER inserts
  300-600ms inter-turn silence; strip newlines per chunk (newline-truncation
  trap); 100-200 tokens per turn.
- Pipeline: outline pass → script pass emitting JSON `[{speaker, text}]`
  (podcastfy's conversation_config as prompt reference; validate + one retry) →
  per-turn Kokoro → concat → **MP3 artifact + transcript** in the existing
  artifact/download path. 10-min audio ≈ 1-5 min TTS on CPU; the LLM dominates —
  no GPU contention question.
**Gates:** generate audio from a real deep report; artifact downloads; SendUserFile
the MP3 + transcript; listen check is Dylan's.

### RP-10 — Slides (M-L; export UNGATED — Chromium ratified into the image)
**Locked:**
- **Marp v1** — CommonMark + `---` separators is the most constrained target a
  27B reliably hits; one binary renders HTML (iframe preview) / PDF / PPTX.
- **Chromium ships in the sandbox image unconditionally (Dylan, 2026-06-10)** —
  ~300-500MB accepted regardless of which way BP-04's browser daemon goes. PDF
  and PPTX export are therefore ON in v1. Add Chromium in the SAME VM-201 image
  rebuild that BP-08/RP-07 already require; BP-04 consumes it if its approach
  wants it.
- `marp --pptx` output is **image-based slides** — the UI labels it exactly that.
  Editable-PPTX (Suna's 3-layer python-pptx approach) is v2.
- Rejected: Slidev (too loose for 27B), pptxgenjs-direct (JS-API generation is
  exactly what small models flub).
**Gates:** real build produces a deck; iframe preview screenshot; if exports
enabled: open the PDF/PPTX, screenshot, SendUserFile.

### RP-11 — Sheets (M)
**Locked:**
- Generate with **openpyxl (0.3MB) writing FORMULAS, not computed values**
  (the Anthropic/Suna skill convention a 27B follows reliably); whitelist the
  formula function set; values the formulas would compute are **unvalidated** —
  the UI flags this honestly (defer LibreOffice headless recalc, ~400-700MB).
- View with **Univer** (Luckysheet is ARCHIVED/EOL Oct 2025).
- Suna is the only OSS agent shipping real sheets — its conventions are the
  reference implementation.
**Gates:** real build emits an .xlsx with live formulas; opens in LibreOffice on
the desktop with formulas intact (manual rung); Univer render screenshot.

### RP-12 — B-series residue (S; B2 rides BP-08's kernel)
- **B2** (kernel output discipline): write-large-outputs-to-file + anti-dump
  prompt guidance — small follow-on to BP-08's kernel wiring, same files.
- **B3** (tail variation): implement per the B-series doc — zero code exists.
- **B9** (prefill masking): nothing anywhere in the tree; small, isolated.
These three never got BP orders (confirmed by the R4 audit table) — they are
gap-closure debt from the ORIGINAL hardening series, not new scope.
**Gates:** per the B-series doc's own acceptance criteria + unit coverage.

### RP-13 — Clarifying questions + report follow-ups (M)
**Locked:**
- Clarifying questions generalize the EXISTING `AWAITING_PLAN_APPROVAL` gate
  (the one BP behavioral runs already drive via send_frame.py): a pre-plan
  `clarify` event carrying typed questions → UI prompt → answers append as
  events → planning proceeds. No new lifecycle states beyond the one pattern.
- Follow-ups on deep reports = **continuation runs on the same conversation** —
  the identical mechanism RP-08 locks for scheduled runs (event-stream append,
  context persistence). One mechanism, two features; build it once in whichever
  order lands first.
**Gates:** live spec — ambiguous prompt yields a clarify card, answer flows into
the plan; follow-up question on a finished report reuses its context (assert the
report's sources appear in the follow-up's grounding); screenshots.

### RP-14 — Usability debt sweep (S)
The hardcode at `BuildSurface.tsx:44` (isolation), theme persistence, Cmd+K
palette, history search, cost meter. Each is small; they share one order so the
sweep gets one verification pass instead of five. Cost meter reads the token
usage already in events — projection only.
**Gates:** one live spec touching all five + screenshots.

---

## 3. Execution order and why

Waves, honoring dependencies; within a wave, orders are parallel-dispatchable to
the worker fleet (allowed set: free / Gemini / Claude — pointer briefs; Sonnet
removed from the worker set per Dylan's 2026-06-10 rule).

| Wave | Orders | Rationale |
|---|---|---|
| 1 | RP-01, RP-02, RP-12, RP-14 | All S. Quick visible wins, zero deps, warm up the fleet on the new routing. |
| 2 | RP-04 (+E3 root-cause first), RP-03, RP-06 | The engine/UX core. RP-04 before any tier-cap raises; RP-06 early because its bundle unblocks the RP-00 harness cassettes. |
| 3 | RP-05, RP-07, RP-08 | The big lever (MCP) + the two that ride infra: RP-07 shares the sandbox-image rebuild, RP-05 consumes the egress proxy. |
| 4 | RP-09, RP-10, RP-11, RP-13 | Artifact expansion + interaction polish. RP-10's Chromium dependency is resolved (always in image) — only the shared image-rebuild rung orders it after wave 3. |

(RP-04b was rejected at ratification — no optional wave.)

Worker-routing note (binding, updated for the 2026-06-10 no-Sonnet rule):
implementation drafts → Gemini Flash (Pro after quota reset), Pi free models
(gpt-oss-120b:free) as fallback; spec drafts → Claude (the MiniMax spec-draft
gate is revoked with the paid-model ban); screenshot triage → Flash or
nemotron-nano-12b-v2-vl:free; diff pre-filter → gpt-oss-120b:free leads only.
Verdicts, diff review, live-spec iteration, commits: Claude. No paid OpenRouter
models via pi, ever; no Sonnet subagents.

---

## 4. Decisions — RATIFIED by Dylan 2026-06-10 (none open)

1. **Share-link exposure (RP-06): tailnet-only.** `/share/<id>` stays on the
   loopback-bound server, reached via tailscale serve like everything else.
   Internet-public (CF Access + Authelia, the Vane pattern) is a possible later
   upgrade, not in this pack.
2. **Chromium in the sandbox image: ALWAYS.** ~300-500MB accepted
   unconditionally — slides PDF/PPTX export is ungated from BP-04's browser
   approach; BP-04 consumes the same Chromium if it wants it. Goes into the one
   shared VM-201 image rebuild.
3. **RP-04b: SKIPPED.** No `np=4` ngram profiling session; the smart-client
   pipeline is the whole wide-research bet. Re-open only if llama.cpp ships
   MTP+parallel together.
4. **Audio voices (RP-09): af_heart + af_bella** — the two top-graded voices
   (quality over gender contrast). Config string; swappable anytime.

---

## 5. Post-RP candidate (requested by Dylan 2026-06-10, not yet ordered)

**Design-reference pack for the build surface** — make agent-built UIs
beautiful by default (Lovable/Manus parity). Three tiers, cheapest first:

1. **Style-preset skill pack (S):** named design specs (glass-minimal,
   editorial, dense-dashboard, brutalist, ...) as SkillStore entries injected
   via the existing `render_skills_for_prompt` seam when the build task is a
   UI. Each spec: palette + semantic tokens, type scale, spacing, component
   idioms (e.g. backdrop-filter glassmorphism recipes), anti-patterns. The
   Lovable lesson: quality comes from CONSTRAINING the model to a curated
   token vocabulary ("the design system is everything; never write custom
   styles"), not from freestyle CSS.
2. **Template scaffolds (M):** license-clean starter trees (vite+tailwind with
   tokens preconfigured; optionally shadcn/ui) shipped in the sandbox image or
   workspace-seeded; the agent copies-then-customizes instead of generating
   from zero. The Manus pattern: pick style at creation, then prompt-based
   group edits.
3. **Reference-image matching (S-M):** user drops a reference screenshot
   (BP-11 upload machinery) → vision driver (PMX_DRIVER_VISION, BP-00)
   receives it with "match this aesthetic" guidance. Mostly wiring; the
   machinery exists.

Verification rides the existing screenshot-evidence pipeline (vision triage →
pixel review). Slot after wave 4; tier 1 could ride RP-10's slides design-pack
work if convenient.

## 6. OpenHands mining notes (source-verified 2026-06-10, requested by Dylan)

Process rule going forward: before any stuck-detector / condenser / driver-
discipline order, check the All-Hands **agent-sdk** repo first (the loop
engine moved there; the main OpenHands repo is now just the app server).
Research clones: /tmp/openhands-{ref,sdk,v0}.

**Mine (they're ahead — fold into future orders):**
- Stuck-detector scenarios (sdk `conversation/stuck_detector.py`): repeated
  action-obs pairs with ID-insensitive equality, action-error loops (×3),
  alternating A-B-A-B period-2. Port scenarios 1/2/4 as additional detectors
  feeding OUR graduated valve (keep our nudge→cap→pause; theirs hard-halts).
  Candidate: wave-2+ order or DC follow-up.
- Critic finish-gate frame (`critic_mixin.py` + `AgentFinishedCritic`):
  finish scored; sub-threshold finish → followup prompt instead of finishing,
  bounded max_iterations. Generalization of our serve gate + verify-on-finish;
  swap their "git patch non-empty" for "deliverables exist in workspace".
- Weak-model survival kit (battle-tested, directly relevant to the 27B
  driver): prompt-mocked tool calling (`fn_call_converter.py`,
  `<function=name>` protocol + ICL + repair), FunctionCallValidationError
  injected back as a USER message for self-correction, corrective nudge on
  empty/reasoning-only turns (`response_dispatch.py`).
- ThinkTool (no-op reasoning-dump tool) — cheap prose-degeneration mitigation;
  tiny order candidate.
- Condenser framework reference: HARD/SOFT requirements, hard_context_reset
  retry ladder, minimum_progress ≥10% guard.

**No counterpart there (we keep our own):** graduated valve; degeneracy-
targeted condensation; wiped-sandbox resume reality-check (their sandbox
survives restarts — pure event replay suffices for them); per-state action-
space gating; meta-tool rate limiting. One string worth stealing: V0's
ERROR_ACTION_NOT_EXECUTED_ERROR phrasing for crashed-runtime messaging.

## 7. Multi-project OSS harvest survey (source-verified 2026-06-10, requested by Dylan)

Broadened process rule (Dylan): before inventing for ANY order class, survey
the open-source field — OpenHands agent-sdk (§6), Pi/pi-mono, Aider,
SWE-agent, smolagents, OpenManus, Suna. Research clones: /tmp/harvest/.
Full survey report archived in the session; ranked list below is the
actionable distillate. Problem areas: A=tool-call robustness,
B=post-restart grounding, C=deliverable verification, D=context/caching,
E=output truncation, F=status/format discipline.

**Ranked top-10 harvest list:**

| # | Mechanism (source) | Area | Effort |
|---|---|---|---|
| 1 | Arg coercion + 3-stage JSON repair before refusal — pi `validation.ts` coercion ladder + `json-parse.ts` (strict→repair→partial→repair+partial, never throws) into the driver tool-call path | A | M |
| 2 | Observation masking with batched (`polling`) boundary updates + keep/remove tags — SWE-agent `LastNObservations`; epochal masking keeps llama.cpp KV prefix stable between mask epochs | D | S/M |
| 3 | Shell spill-to-file: rolling tail buffer + full-output temp file + path in result — pi `executeShellWithCapture` (50KB threshold; tail-keep for shell, head-keep for reads) | E | S |
| 4 | Failure-feedback kit: "did you mean these actual lines" + "REPLACE lines already in file!" + "other N applied, don't re-send" — aider `apply_edits`; already-applied check counters post-restart "I already did that" | A+B | S |
| 5 | Structured checkpoint format + turn-boundary-only cut points + iterative summary update — pi `compaction.ts` (Goal/Constraints/Progress/Key Decisions/Next Steps/Critical Context); template for the reality block | B+D | M |
| 6 | Requery-outside-the-log with typed error codes + bounded retries + deliverable salvage — SWE-agent `forward_with_handling`; malformed attempts never pollute the event log's LLM view | A+C | M |
| 7 | Grammar-constrained tool calls via llama.cpp json_schema — smolagents `use_structured_outputs_internally` precedent; we own the server, reject malformed calls at decode time | A | M |
| 8 | Scheduled facts-survey re-grounding every N steps — smolagents `planning_interval`; also fire once immediately after restart | B+D | S |
| 9 | Duplicate-content stuck detector + strategy-change injection — OpenManus `is_stuck`/`handle_stuck_state`, upgrade exact-match to n-gram/format similarity | A+F | S |
| 10 | Session status enum incl. provisioning/branching states + CR-style deliverable record — Suna `project_session_status` + `change_requests` | F+C | M |

**Cross-cutting verdicts on shipped/planned choices:**
- Refusal-with-feedback: validated by all six (best: pi, aider, SWE-agent).
- Write-outputs-to-file: validated verbatim (pi; SWE-agent adds a
  prescriptive truncation marker — say WHAT TO DO next, not just "truncated").
- Graduated valve: validated (SWE-agent max_requeries→autosubmission ladder;
  aider max_reflections=3).
- Tool withholding (DC-05 re-run #5 fix): NO project does it, none
  contradicts it — genuinely ours. Pi's nearest analog: toolset changes are
  themselves logged session events (`active_tools_change`) — copy that for
  replay determinism.
- Design tension to respect: per-step observation masking vs KV-cache
  stability are in direct conflict. SWE-agent batches mask updates into
  epochs; pi avoids masking entirely (boundary-aligned compaction only).
  Pick one; never mask per-step.
- Meta-finding (Suna): a funded Manus clone deleted its own loop and runs
  OpenCode in-sandbox — supports harvest-don't-reinvent.

**Order impact:** #1/#6/#7 fold into RP-12 (weak-model FC kit) before
dispatch; #3 is the E-order blueprint; #5/#8 feed the reality-block /
re-grounding line of work; #9 feeds the valve; #10 feeds status
write-through (rp-14).

## 8. OpenCode (sst/opencode) survey addendum (source-verified 2026-06-11)

Surveyed because Suna/Kortix deleted their own loop and bet the product on
OpenCode — the strongest market signal in the field. Clone:
/tmp/harvest/opencode (HEAD bf05e8a). Verdict: **the moat is everything
AROUND the loop** (25-endpoint headless control plane + SSE; 1400 lines of
provider-quirk scar tissue in transform.ts; production hardening where every
failure degrades to feedback or escalation). The loop itself is comparable
or behind ours (mid-migration to event-sourced V2; we are natively
event-sourced). Their hard problems are NOT ours: environment assumed
durable (no wiped-sandbox story), no llama.cpp prefix-cache discipline,
no weak-model FC work beyond a qwen sampling pin.

**Tool-withholding novelty: CONFIRMED a 4th time.** All OpenCode gating is
static (agent config / model / client / permissions). No state- or
progress-dependent withholding anywhere. No contradictions with any shipped
choice.

**Steals worth adding to the harvest list (file refs in survey report):**
- Reroute-to-hidden-`invalid`-tool repair: malformed/unknown tool calls become
  an ordinary error tool_result via a registered-but-unoffered `invalid`
  tool — the turn never aborts, message invariants stay intact (A, S).
  Composes with Pi's repair ladder (#1 in §7) for RP-12.
- Anchored-summary UPDATE compaction: fixed Markdown template
  (Goal/Constraints/Progress/Key Decisions/Next Steps/Critical Context/Files)
  + merge-previous-summary instead of regenerate — long-running facts can't
  evaporate across N compactions (D, M). Same shape as Pi's #5 in §7;
  OpenCode's merge prompt is the better reference.
- Interrupted-tool reconciliation rendering: dangling running tool calls
  render as `[Tool execution was interrupted]` error tool_results + orphan
  exclusion from pending-work detection (B, S). Complement to our reality
  block for crash-mid-tool turns.
- Max-steps cap = forced TEXT-ONLY wrap-up turn (all tools off, mandated
  summary + remaining tasks) instead of a dead stop (F, S) — upgrade idea
  for our valve's cap rung.
- Doom-loop detector: 3x identical (tool name + serialized args) →
  escalate to a permission ask, not a kill (F, S).
- Finish-reason distrust: loop exit requires finish AND no pending tool-call
  parts ("some providers return 'stop' even when the message contains tool
  calls") (A/C, S) — audit our driver's exit condition for this.
- Capability-adaptive truncation hints on spill-to-file: the marker tells the
  model WHAT TO DO next, varying by whether it has a delegate tool (E, S).
- Qwen sampling pin (temp 0.55 / topP 1) — cross-check our llama.cpp
  serving defaults (A, S).
- Caution reinforced: their prune mutates rendered history in place —
  hostile to llama.cpp prefix caching; if adopted, fire only at compaction
  boundaries. Aligns with the never-mask-per-step rule.

## 9. Handoff-document design (Dylan's proposal, 2026-06-11)

Dylan independently proposed the anchored-checkpoint pattern: an agent
crafts a heavily structured handoff document before a project ends, the
next iteration recycles it (merge + prune stale), it's injected at session
start, and the model is refused access to change the structure. Ratified
direction for the reality-block upgrade, with two amendments from the
Phase-B evidence:

1. WRITE-THROUGH, not write-at-finish: crashes don't announce themselves
   (every Phase-B resume came from an unscheduled restart). The checkpoint
   updates at every plan-step transition. Sections that CAN be derived from
   the event log (plan state, deliverables, pinned facts) are
   harness-rendered, never model-authored — the model can't hallucinate
   progress into the handoff. Model fills only the narrative slots.
2. NO "first action: ask the user" — that is re-run #5's degeneration
   (ask_user-as-narration). The injected block ends with the next
   actionable step + execute-now instruction; ask_user unlocks after one
   real action. The handoff doc rendered to the HUMAN on resume is the UX
   answer to "where were we".

Template + merge prompt references: Pi compaction.ts checkpoint format
(§7 #5), OpenCode anchored-summary update (§8). Candidate order: fold into
the reality-block/re-grounding line (with §7 #8 scheduled facts-survey).
