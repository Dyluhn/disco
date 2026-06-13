# disco — From Here to Release

**The gap analysis and roadmap for shipping the best open-source Manus-class platform.**
Compiled 2026-06-09 from six parallel deep audits: three first-hand code audits (build
surface, research surface, frontend/UX) and three field studies (Manus's full 2026 feature
surface from primary sources; the open-source competitive landscape with live GitHub data;
the production research-product bar across Perplexity / OpenAI / Gemini / Manus / OSS).
Companion docs: `agent-architecture-rebuild-plan.md` (the evidence-graded engineering
substrate, B1–B9), `architecture-rebuild-writeup.md`, `project-history-since-deep-research-fix.md`.

---

## 1. The strategic finding: the wedge nobody owns

The competitive research converged on one sentence:

> **The open wedge is the intersection nobody owns — Manus-grade task UX (artifacts, live
> preview, replay, lifecycle) that is *reliable on self-hosted open weights*, with a
> one-command deploy.**

The mid-2026 field, ranked by who's closest:

| Project | Has | Missing |
|---|---|---|
| **DeerFlow 2.0** (ByteDance, 70.8k★) | Full harness: sandbox, memory, skills, subagents, web UI | ByteDance-default plumbing (crawl routes through their infra), frontier-model assumptions, 16vCPU/32GB footprint, no security story |
| **Eigent** (camel-ai, 14.2k★) | Local-model polish (Ollama/vLLM first-class), parallel workforce, MCP | Desktop-only (no server/web deploy), quality still gated on frontier APIs |
| **Suna/Kortix** (19.8k★) | Most product-mature web platform: microVMs, triggers, approval gates, mobile | Pivoted to B2B "company command center"; local-LLM is an unsupported community ask; notorious self-host sprawl |
| **OpenHands** (76.3k★) | Execution-reliability gold standard, own open models | Coding-only scope; no research/artifact surface |
| **Onyx** (30.2k★) | Polish bar: web+desktop+mobile, 50+ connectors, local LLM first-class | Not an executor — no sandbox task loop, no browser automation |
| **OpenManus** (56.5k★) | Stars and mindshare | **Dormant since 2026-02**; CLI-only; defines demand, not the bar |
| **AgenticSeek** (26.5k★) | Pure local-first ideology, explicit VRAM tiers | Self-declared zero-roadmap prototype; the reliability complaints prove the demand we can serve |

**Field-wide gaps no OSS project ships** (verbatim from the landscape synthesis):
1. **Reliability on local/open models for the full agent loop** — every project either warns
   non-frontier models "significantly degrade" or has issue threads of 14–32B models stalling.
   *Nobody ships a harness engineered around local-model weaknesses.*
2. **Artifact quality parity with Manus** — slides/sheets/docs with live preview exist only in
   fragments; spreadsheets essentially absent everywhere.
3. **Replay, sharing, session portability** — Manus's shareable session replays have **no
   open-source equivalent**. Green field.
4. **Task lifecycle robustness** — suspend/resume, checkpointed research, crash survival.
   DeerFlow's memory isn't even on by default.
5. **Trust & safety engineering** — egress control essentially nonexistent in the field;
   action guards exist only in a Microsoft research prototype.
6. **Shipped eval harnesses** — no project lets a self-hoster verify their model/provider
   combo actually works.

**Why this list matters:** disco is *already differentiated on five of the six.*
We run engineered-for-27B (the rebuild plan is precisely "a harness engineered around
local-model weaknesses"). We have an event-sourced append-only log (replay/share is a
projection away, not a rebuild). We have checkpointed deep-research resume (none of the big
three research products ship it; the bar study calls it "open white space"). We have gVisor +
egress allowlist proxy + risk gates + quarantined browser content (ahead of the entire field).
We have a cassette/replay/eval harness in-repo. The identity of this release is not "another
Manus clone" — it is **the trustworthy one that actually works on your own hardware**, with
the receipts to prove it.

What we are NOT differentiated on yet: artifacts (gap #2) and the polish/feature table stakes
below. That is what this roadmap closes.

---

## 2. Where we stand — honest scorecards

Scoring: ✓ have it at or near bar · ◐ partial · ✗ absent. "Bar" = best production
implementation, per the field studies.

### 2.1 Research surface (vs the 20-item production bar)

| # | Feature | Bar-setter | Us | Note |
|---|---|---|---|---|
| 1 | Clarifying questions before research | OpenAI DR | ✗ | Pure prompting; cheap add |
| 2 | Editable research plan before run | Gemini | **✓** | Plan-approval gate with edit/revise — we already match the bar |
| 3 | Live progress transparency | Gemini/OpenAI | ✓ | DeepProgressStrip + activity feed; add "reading URL now" granularity |
| 4 | Mid-run interrupt / steer / inject sources | OpenAI (Feb 2026, unique) | ◐ | We have stop/resume at section boundary; no steer/inject yet — our checkpoint design is the hard part, already done |
| 5 | Parallel sub-question fan-out | Manus Wide Research | ✗ | Sequential `for` loop today (engine.py:174); compute-bounded, architecture supports it |
| 6 | Source quality weighting / domain restriction | OpenAI/Perplexity | ◐ | domains allow/deny exist; no credibility scoring surfaced |
| 7 | Citation granularity | Perplexity | **✓** | Sentence-level NLI per claim — *stronger mechanism than anyone*; polish the hover/card-sync |
| 8 | Contradiction surfacing | **Nobody ships it well** | ◐ | We compute entailment/contradiction + disputed_notes and barely show them. **Open differentiator sitting in our pipeline.** |
| 9 | Report structure quality | OpenAI | ✓ | Sections, exec summary, confidence per section |
| 10 | Tables/charts in reports | Gemini Ultra | ✗ | Block types defined in frontend (grounded.ts:51) — backend never emits them |
| 11 | Export formats | Perplexity | ◐ | Markdown only; PDF/DOCX missing |
| 12 | Audio overview | Gemini (unique) | ✗ | Two-voice script + Piper/Kokoro on the mini-PC — fully in reach |
| 13 | Follow-up iteration on finished report | Perplexity | ◐ | Quick research has follow-ups; Deep Research is one-shot |
| 14 | Cost/latency depth tiers | Perplexity | **✓** | quick/standard/exhaustive with honest time estimates + async |
| 15 | Project containers (files + web hybrid) | Perplexity Spaces | ✗ | No file upload anywhere in the product |
| 16 | Internal data / MCP as research source | OpenAI (2026 headline) | ✗ | MCP is a static placeholder in settings |
| 17 | Vertical depth (finance/academic) | Perplexity / scira | ✗ | Cheap keyless wins available: arxiv, SEC EDGAR, yfinance |
| 18 | Wide/horizontal N-item research | Manus only | ✗ | Pattern fully replicable at 10–30 items on our hardware |
| 19 | Security posture for agentic browsing | OpenAI Lockdown Mode | **✓** | Egress allowlist sidecar + quarantined browser — ahead of bar |
| 20 | Answer-UX micro-polish | Perplexity | ◐ | Good streaming/TTFT/sources; missing cards-slide-in-with-citation sync, favicons, snippet hovers |

**Read:** 5 at-bar (two genuinely ahead), 6 partial, 9 absent. The absences cluster in two
groups: **inputs** (files, MCP, clarifying questions) and **outputs** (charts, PDF, audio,
artifacts). The core pipeline — grounding, verification, tiers, checkpointing — is at or
above bar. This surface is closer to done than it feels.

### 2.2 Build surface (vs Manus's replicable subset)

| Capability | Manus | Us | Note |
|---|---|---|---|
| Sandboxed plan-first agent loop | ✓ | **✓** | 4 backends, risk gates, ask-gate, steering — architecture arguably cleaner |
| Live preview while building | ✓ | ✓ | Auto-served workspace + run_server + proxied preview |
| Iteration on existing work | ✓ | ✓ | Line-targeted edits + paginated reads — just proven on EE-Quest (18 in-place edits, 0 nudges) |
| **Reliability of the loop on our model** | n/a (frontier) | ◐ | The B1–B9 rebuild is this row. Everything else stacks on it. |
| Agent sees what it built (vision/QA loop) | ✓ (sandbox browser click-through QA) | ✗ | Our browser tool is curl-based; no screenshot, no pixel feedback. **Biggest single quality gap in build outcomes.** |
| File upload into task context | ✓ (PDF/CSV/XLSX/images/audio/video) | ✗ | No upload affordance anywhere |
| Artifacts: slides | ✓ (pptx templates, editable) | ✗ | |
| Artifacts: documents/reports as files | ✓ | ◐ | Markdown deliverables only |
| Artifacts: spreadsheets/data analysis + charts | ✓ (1.6 Max flagship) | ✗ | Absent across the entire OSS field too — differentiator if done |
| Deploy to hosted URL | ✓ (publish, domains, rollback) | ◐ | Preview + deployment_url field; no publish flow (Tailscale/Caddy single-box publish is feasible) |
| GitHub two-way sync | ✓ | ✗ | Agent can shell git; no first-class flow |
| Scheduled tasks | ✓ (2.0: NL recurrence, history, per-agent) | ✗ | |
| Session replay + share links | ✓ (every task, fork/take-over) | ✗ | **Event log makes this nearly free for us; no OSS equivalent exists** |
| Skills (file-based, progressive disclosure) | ✓ | ✓ | We have md skill modules; align to the open Agent Skills format for import-compat |
| Projects (instructions + knowledge + connectors) | ✓ | ◐ | Persistent workspaces exist; no instruction/knowledge container |
| Connectors / MCP | ✓ (catalog + custom HTTPS MCP) | ✗ | Settings placeholder, no client |
| Knowledge/memory across tasks | ✓ (~20 prefs, approval-gated learning) | ◐ | `remember` is per-conversation; no cross-session memory (note: controlled evidence says A/B this on the 27B — LOCA-bench showed memory tools regress weaker models) |
| Chat vs Agent mode routing | ✓ | ✗ | Every request spins the full loop; a cheap chat path is missing |
| Mobile / desktop apps | ✓ | ✗ | Responsive web partial; not chasing native this cycle |

**Explicit non-goals** (business-class moats, per the Manus study): per-session VM fleet +
100× Wide Research bursts; persistent cloud-browser fleet with shared login store; licensed
data (Similarweb/FactSet); managed hosting/MySQL/domains fleet; app-store publishing
pipelines; frontier image-edit models (Design View); payments; Telegram/Slack distribution
partnerships; multi-user Collab. None of these gate the wedge.

### 2.3 Usability / product layer

Strong already: honest error states (provider's real error verbatim), skeletons, streaming
file view with live cursor, plan gates as real alertdialogs, NotWired/coming-soon honesty,
dark/light themes, history + projects with storage-status gating.

Gaps, in rough order of user-felt pain:
1. **No file/image upload** — blocks both surfaces; single most user-visible absence.
2. **No share/replay links** — `/build/:cid` is owner-only resume, not a shareable artifact.
3. **No parallel-task awareness** — one conversation per mode, no background-task dashboard,
   no "3 tasks running" indicator. The task-as-resource backend exists; the UI doesn't show it.
4. **MCP placeholder** (flagged NotWired — correctly honest, still a gap).
5. **No onboarding** beyond example queries; no keyboard palette (Cmd+K), no full-text
   conversation search, theme not persisted, isolation tier hardcoded in UI
   (BuildSurface.tsx:44 `[GAP]`), no cost meter despite per-model prices in the catalog.
6. **Mobile**: research surface mostly responsive; build surface (ResizableSplit, inspector)
   desktop-only.

---

## 3. The roadmap

Four phases. Each phase has a theme, a gate ("done means"), and folds the standing
engineering plan (B1–B9) in as the substrate rather than a separate track. Ordering
principle: **reliability → table stakes → differentiation → release engineering.** Polish
that sits on an unreliable loop is the false-affordance failure mode at product scale.

### Phase 0 — The substrate (the standing rebuild plan, unchanged)
*Theme: make the loop trustworthy on a 27B. This IS the wedge; everything else markets it.*

1. **B1** observation masking + restorable references [C] → replaces destructive 8000-char snip
2. **B3** structured tail-variation [P], **B4** keep-errors-in [P], **B5** KV-cache discipline [P]
3. Verify read-rut gone on the EE-Quest harness → **Part A rollback** of the read-counter
   family (force-commit, nudge escalation, temperature jitter)
4. **B8** persistent IPython kernel (delete the pickle runner; flat per-cell latency test)
5. **B2** programmatic tool calling [C], **B7** verification-gated completion, **B9** prefill masking [P/H]

*Gate: 3-iteration build on a 50KB+ file with flat latency, zero nudge text in the prompt,
masking ≥ snip on our own harness. (Detail + grading: `agent-architecture-rebuild-plan.md`.)*

### Phase 0.5 — The Server Cockpit (S-series) — **runs FIRST, before B1**
*Theme: the agent and its sandbox must stop fighting. Diagnosed 2026-06-09 from the
"model kills its own port 8000" failure chain; this is a structural defect, not model
behavior, and none of B1–B9 fixes it.*

**Root cause (receipts):** port 8000 has two owners. The container's PID-1 is an
unconditional respawn loop (`gvisor.py:50`: `while true; do python3 -m http.server 8000;
sleep 1; done`) while `run_server` pkills http.server and races the respawn for the bind
(`preview.py:178`). When the agent's dev server crashes, the keepalive silently recaptures
the port and `preview_status` reports `"serving"` — a false positive (`preview.py:63` probes
only whether the port is bound, never by whom). The agent's total server visibility is two
booleans + a file count + a 20-line tail at failed startup only. The prompt prohibitions
(`prompts.py:209-222`) and the pkill hard-deny (`analyzers.py:105-110`) already exist and
do not prevent the spiral — instruction against confusion loses to missing information.

**The proven mechanism (researched 2026-06-09, triple-sourced: leaked Manus tool schemas —
two byte-identical mirrors — + leaked sandbox container source + manus.im blog; corroborated
by OpenHands tmux terminal and the E2B process API):** Manus has NO auto-serve and NO hidden
supervisor. Processes are durable resources the agent owns, via persistent named shell
sessions: `shell_exec(id, dir, cmd)` / `shell_view(id)` (live scrollback tail anytime, even
mid-run) / `shell_wait(id, s)` / `shell_write_to_process(id, input)` / `shell_kill_process(id)`
(explicit kill is SANCTIONED — ownership makes it lifecycle, not danger). Browser tools return
screenshot + indexed elements + extracted markdown + `browser_console_view`; the prompt
mandates "for web services, must first test access locally via browser"; user exposure is a
separate explicit `deploy_expose_port(port)` step. The convergent invariant across
Manus/OpenHands/E2B: (a) processes outlive the tool call, (b) cheap view-output verb always
available, (c) stdin first-class, (d) kill explicit and permitted, (e) apps verified by the
agent's own vision browser before handoff.

1. **S1 — Persistent named shell sessions** [P]. pexpect/tmux-backed sessions in the sandbox
   with `shell_exec(session, …)`, `shell_view(session)`, `shell_write(session, …)`,
   `shell_kill(session)`. A dev server is just a session. Deletes `run_server` and its
   pkill hack (`preview.py:178`).
2. **S2 — Auto-serve becomes a visible session, not PID-1** [P-adapted]. Keep the instant
   static preview (our UX strength) but run it as named session `"preview"` — viewable,
   killable, replaceable like any other. The `gvisor.py:50` respawn loop dies; the port race
   and the false `"serving"` die with it. `preview_status` → `server_status` derived from
   the session table (who owns :8000, pid, uptime, last exit + traceback tail).
3. **S3 — Environment contract** [P]. Rewrite DEPLOY/PREVIEW from prohibitions into a
   description of the machine (sessions, port ownership, verification recipe), in the
   stable prefix (B5 synergy). Retire the pkill hard-deny along with its reason.
4. **S4 — Eyes** [P]. Real sandbox browser tool with Manus's return shape as spec:
   screenshot (annotated + clean) + indexed elements + extracted text + console errors;
   screenshots routed through the vision role; surfaced in the UI inspector.
5. **S5 — Verify upgrade** [P]. Manus's rule verbatim: a web deliverable must be locally
   browser-verified (render + zero unexplained console errors) before `finish`/`serve`;
   screenshot attached as evidence.

*Gate: a build whose dev server is deliberately crashed mid-run; the agent must diagnose
from `shell_view`/`server_status`, recover (including a legitimate session kill), and finish
with a screenshot-verified app. Sequencing overall: S1–S3 → B1/B5 → Part A rollback → B8 →
S4/S5 → B2/B7/B9.*

### Phase 1 — Table stakes (close the disqualifying absences)
*Theme: the features whose absence reads "demo," in user-felt-pain order.*

1. **File upload, both surfaces.** Upload → sandbox workspace (build) / extraction →
   passages → corpus (research). PDF/CSV/XLSX/images/zip first. Unlocks bar items #15
   and half of Manus's input surface in one feature.
2. **Vision feedback for the build agent.** Headless browser (Playwright/Firefox already in
   the stack) in the sandbox: `screenshot` tool + screenshot-on-verify, images fed to a
   vision-capable role (vision model already deployed on the mini-PC; VISION enum exists
   unwired). The agent QAs what it built — Manus's click-through pattern. Biggest build-
   quality lever after Phase 0.
3. **MCP client in the agent-server.** Tools for build + retriever sources for research
   (OpenAI's 2026 headline feature; "absence is disqualifying in 2026" — landscape study).
   Un-stub the settings UI.
4. **Research input polish:** clarifying-questions round before deep research (#1, prompting
   only) + follow-up iteration on finished deep reports (#13 — persist the run's corpus,
   which the checkpoint format already carries).
5. **Export:** PDF/DOCX via pandoc/weasyprint in the sandbox image (#11), plus export of
   build deliverables stays as-is.
6. **Quick usability debt:** persist theme; live isolation tier over the wire (kill the
   BuildSurface.tsx:44 hardcode); conversation full-text search; Cmd+K palette; cost meter
   from existing per-model prices.

*Gate: a stranger can upload a CSV, get a cited deep report with a chart-free but exportable
PDF, and watch a build agent screenshot-verify its own site. All verified live with Playwright
screenshots per house rules.*

### Phase 2 — Differentiation (ship what the field doesn't have)
*Theme: convert our architectural assets into visible product features.*

1. **Session replay + share links.** A read-only share route rendering a conversation from
   its event log (projection, not new state), with step-through playback. No OSS equivalent
   exists; Manus charges for the privilege. Our event-sourced core makes this the highest
   leverage-to-effort feature in the entire matrix.
2. **Contradiction surfacing UI.** We already compute per-claim entailment/contradiction and
   disputed_notes. Surface them: "Source A contradicts Source B" callouts, per-section
   support meters, credibility-weighted source list. Nobody ships this; we'd be first.
3. **Wide-research fan-out.** Parallelize the per-sub-question gather loop (asyncio over the
   existing engine; llama.cpp parallel slots / batched rounds), then N-item horizontal mode
   ("compare 20 X") with per-item isolated context → synthesis table. 10–30 items is honest
   on our hardware; say so in the UI (tiers already model this).
4. **The artifact engine.** Charts first (sandboxed matplotlib/chart.js from real data — the
   backend finally emits those table/chart blocks the frontend already types). Then slides
   (reveal.js/Marp markdown → themed deck → PDF export), then document templates. Spreadsheets
   last (absent across the entire OSS field — even partial support differentiates).
5. **Audio overviews.** Two-voice script from a finished report → Piper/Kokoro TTS. Gemini's
   most-loved feature, trivially self-hostable on our homelab, pure delight.
6. **Scheduled tasks.** Cron-shaped recurrence on existing conversation-create APIs + run
   history. Backend task-as-resource model already supports it.
7. **Background-task dashboard.** Surface parallel running tasks (the backend already
   tracks them) — list, status, notifications.

### Phase 3 — Release engineering (the last mile that determines adoption)
*Theme: the one-command deploy and the receipts.*

1. **One-command self-host.** `docker compose up` → app + agent-server + sandbox image +
   keyless defaults (ddgs + local extraction + bundled encoders) working with zero accounts.
   The landscape study is blunt: Suna-style dependency sprawl is held against projects;
   Onyx/Vane-grade "comes up first try" is the bar. Our 3-tier keyless provider design was
   built for exactly this — make it the install story.
2. **Ship the eval harness as a user feature.** "Verify your setup": replay bundled cassettes
   against the user's model/provider combo, report pass/fail per capability. No OSS project
   does this; it converts our test infra into the trust story, and it operationalizes the
   model-dependence caveats (A/B memory on small models) honestly.
3. **Security posture doc.** Threat model, sandbox tiers honestly labeled (the UI already
   does this), egress allowlisting, prompt-injection quarantine. The field has zero audits;
   leading with a real security story is differentiation, not paperwork.
4. **Docs + gallery.** Install, provider matrix with VRAM tiers (AgenticSeek-style honesty),
   example replays (the Phase 2 share links become the demo gallery), recommended-model
   table with measured scores from the eval harness.
5. **Mobile pass** on research + history surfaces (build inspector can stay desktop-first,
   labeled), license + naming + repo hygiene, CONTRIBUTING.
6. **Optional reach:** a messaging trigger (start/observe tasks from Telegram or e-mail à la
   Mail Manus / OpenClaw) — biggest mindshare lever in the field, modest scope if it reuses
   the task APIs. Explicitly last; it's reach, not core.

---

## 4. Verification discipline (applies to every phase)

Per the house rules established this project: every feature lands with a harness built from
verbatim captured samples; UI changes end with Playwright/Firefox screenshots of the real
app; no green-test-count-as-evidence; non-wired UI is flagged red, never silently dormant;
evidence grades ([C]/[P]/[E]/[H]) carried on architectural claims; model-dependent features
(memory, fan-out quality) A/B'd on the 27B before being defaulted on.

## 5. One-paragraph summary

The research surface is nearer the production bar than it feels (5/20 at bar, two ahead);
the build surface has a sound and unusually clean skeleton but is missing the feedback loop
(vision), the inputs (files), and the artifact outputs that make Manus feel magical; the
usability layer is honest and solid but lacks the three features users notice first (upload,
sharing, parallel-task awareness). The field study says the winning identity is already ours
by architecture — reliable-on-local-weights, event-sourced, security-honest, eval-shipped —
and no incumbent (DeerFlow, Eigent, Suna, OpenHands, Onyx) occupies it. Phase 0 makes the
loop worthy of the claim; Phase 1 removes the disqualifiers; Phase 2 converts our
architecture into features nobody else has; Phase 3 makes a stranger's first 15 minutes
succeed. That is the route from here to there.
