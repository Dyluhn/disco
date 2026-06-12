# Disco Build UX — Interaction & Visual Gap Analysis

> Produced 2026-06-07 by a 19-agent adversarial workflow (8 UI-mapping agents +
> 10 pillar-comparison agents + synthesis). Scope: the Build (agent) surface at
> `frontend/src/components/BuildSurface.tsx` and its children. Every claim
> verified against actual source; file:line citations are load-bearing.
> Companion to `manus-gap-analysis.md` (the backend/loop analysis).

---

## 1. Executive summary

The Build surface is **architecturally sound and emotionally dead**. The data
plumbing, the event reducer, the type discipline, the plan gate, the steering
protocol — all real, all correct. What's missing is everything that makes an
agentic builder *feel* like a live collaborator instead of a CI job: a thing you
can watch render, a turn-taking conversation, anchored progress, and a payoff at
the end. "Nearly broken vs Manus" is the precise gap between *correct wiring* and
*legible, alive interaction*.

### INTERACTION failures (how you interface with the build) — the real damage

1. **No live preview, ever, in the shipped path — and the dead "Preview" tab
   advertises the absence.** `getPreview()` hard-returns `{available:false}`
   offline (`api/agent.ts:73-74`), the tab carries a permanent "soon" pill
   (`ExecutionCanvas.tsx:152`), the body is a centered grey paragraph
   (`ExecutionCanvas.tsx:122-138`). The feature that makes Manus magical is a tab
   the user is invited to click into emptiness. **Critical.**

2. **The deliverable handoff is an anticlimax.** At FINISHED the preview hook
   *deactivates* (`ExecutionCanvas.tsx:92` — `active` excludes FINISHED), so the
   pane goes *more* dead at completion. The payoff is a quiet prose card
   (`BuildSurface.tsx:159-179`) + a hairline "Export" that drops a blind
   `{cid}.zip` (`projects.ts:91-112`). No live URL, no shareable link, no
   manifest. The `Project` type has no `url`/`deployment_url` field
   (`types/project.ts:23-27`). **Critical.**

3. **The conversation is one-directional — the agent can narrate but cannot ask.**
   No "agent needs your answer" status in the enum (`types/agent.ts:8-17`). An
   `ask_user` question is demoted to a muted-grey `agent_message` row
   (`ActivityFeed.tsx:31-32,150-153`) identical to routine narration,
   `attention:false`. The user doesn't realize they were asked; status stays
   "Working." Half the turn-taking silently does nothing. **High.**

4. **You can't gently stop. You can only nuke.** The only stop control is a red
   Kill that tears down the sandbox + revokes capabilities (`api/agent.ts:78-81`),
   one un-guarded click, no confirm, no "killing…" feedback
   (`AgentStatusBar.tsx:77-91`). The graceful `cancel()` verb *exists and is
   exposed* (`useBuildStream.ts:172`, `types/agent.ts:191`) but is **wired to no
   button**. "Paused" is a rendered status (`AgentStatusBar.tsx:14`) with no
   control to enter it — a phantom affordance. **High.**

5. **Progress + "now" signals are correct but positioned where they disappear.**
   The plan checklist and `LiveSignalBar` both live *inside* the single
   `overflow-y-auto` feed container (`BuildSurface.tsx:133-158`), neither sticky,
   nothing auto-scrolls. On a long run, "what's left" and "what's happening now"
   both scroll off the edges. **No aggregate progress** anywhere — no "3 of 7,"
   no bar (grep-clean). **High.**

### VISUAL / POLISH gaps (the look, the sense of life during a run)

- **Statically handsome, dynamically frozen.** The only animating element,
  `LiveSignalBar`, returns `null` unless status is exactly `RUNNING`
  (`buildTrace.ts:330`, `LiveSignalBar.tsx:17`). The status spinner is also
  RUNNING-only (`AgentStatusBar.tsx:61`). During the 5–30s thinking turn between
  events — and during *every* gate/await state — no spinner, no narrator. Reads
  as hung.
- **No token streaming + no auto-scroll + no entrance animation.** Whole events
  dump all-at-once below the fold; the existing `pmx-rise`/`settle` keyframes
  (`theme.css`) are unused on the build surface.
- **The right pane sits on a permanently-empty "soon" placeholder** as its most
  prominent third tab.

---

## 2. The interaction gaps, prioritized

### 2.1 — Live interactive preview from the first scaffold — CRITICAL
- **Manus:** a live, continuously-updating render from the first file + a visual
  design-edit mode.
- **Us:** artifact surfaces only as raw code (`ExecutionCanvas.tsx:20-56`) + raw
  shell output (`:58-85`). Render lives in a third tab that's never the default
  (`:164-170`), carries a permanent "soon" pill (`:152,186-190`), and the iframe
  renders only when `getPreview` → `available:true` (offline hard-returns false,
  `api/agent.ts:73-74`). Even live, only once a dev server boots mid-run, polled
  every 4s (`useBuildPreview.ts:16-17`) — never "from the beginning." No
  design-edit mode exists.
- **Fix:** (1) Stop advertising the dead tab — hide/disable until available
  (`ExecutionCanvas.tsx:149-203`). (2) Ship a client-side `srcdoc` render from the
  written files (`deriveFiles`, `buildTrace.ts:183-199`) — no backend. (3) Make
  Preview the default tab once a renderable artifact exists (`:165-170`). (4)
  Scope design-edit mode as its own milestone.

### 2.2 — Deliverable handoff (live URL + artifacts + summary) — CRITICAL
- **Manus:** completion posts summary + ZIP + code links + public deployment URL.
- **Us:** three decoupled hollow channels — prose card (`BuildSurface.tsx:159-179`),
  blind `{cid}.zip` export (`projects.ts:91-112`), and the "soon"-gated Preview tab
  whose hook deactivates at FINISHED (`ExecutionCanvas.tsx:92`). No URL field on
  `Project` (`types/project.ts:23-27`). The moment of payoff is where the UI goes
  quietest.
- **Fix:** build a `DeliverablePanel` hero at FINISHED: (1) "View live site" on a
  *persistent* URL (add `deployment_url` to `Project` + payload); (2) keep the
  preview hook active in FINISHED/STUCK (`ExecutionCanvas.tsx:92`); (3) Export as
  an artifact card (manifest from `deriveFiles`); (4) structured completion
  summary; (5) auto-switch Inspector to Preview on FINISHED.

### 2.3 — The chat as a genuine two-way conversation (the agent can ask) — HIGH
- Narration half is excellent (`buildTrace.ts:43-61`, thoughts verbatim
  `ActivityFeed.tsx:170-176`). The **ask half is dysfunctional**: no blocking
  status (`types/agent.ts:8-17`); `ask_user` → plain `agent_message`,
  `attention:false` (`buildTrace.ts:163-169`), grey + indistinguishable from
  narration; only reply path is the redirect-framed `SteerInput`.
- **Fix:** real gate mirroring `ConfirmationPanel`/`AlternativesGate`
  (`BuildSurface.tsx:186-195`): (1) add `AWAITING_USER_QUESTION` status; (2)
  `AskPanel` alertdialog with a dedicated answer box on `send_message`. Interim:
  `attention:true` for `ask_user` (`buildTrace.ts:168`) + relabel the steer box to
  "Answer the agent" when a question is the tail.

### 2.4 — Control affordances (stop / pause / in-command) — HIGH
- Strong redirect, crude stop, no pause. Red Kill is destructive + un-guarded
  (`AgentStatusBar.tsx:77-91`, `api/agent.ts:78-81`); the graceful `cancel()` verb
  is defined + exposed but wired to no button (`types/agent.ts:191`,
  `useBuildStream.ts:172`); `PAUSED` is renderable with no control to enter it.
- **Fix:** (1) wire `cancel()` to a visible "Stop" in the header (non-destructive
  halt); (2) Kill confirm + "Stopping…" `isPending`; (3) resolve the phantom
  Pause; (4) put the primary stop in the persistent header.

### 2.5 — Progress + coherence (where are we / what's left) — HIGH
- Machinery real (`PlanPanel.tsx:15-27,97-122`; `LiveSignalBar`
  `buildTrace.ts:326-368`), but legibility breaks: plan + feed share one scroll
  container, no sticky (`BuildSurface.tsx:133-155`); no aggregate signal; live bar
  at the bottom of the same scroll box and RUNNING-only.
- **Fix (positioning on already-correct data):** (1) hoist/`sticky` the
  `PlanPanel`; (2) add a "done/total" counter + thin bar in the header
  (`PlanPanel.tsx:62-77`); (3) pin `LiveSignalBar` in the footer; (4) animate
  `AgentStatusBar` for all active states.

### 2.6 — Mid-run steering + NL iteration — HIGH (PARTIAL)
- Steer protocol correct (`SteerInput.tsx:11-17`, `useBuildStream.ts:173-181`),
  but: no preview to steer against; `SteerInput` scrolls out of reach; only
  acknowledgment is your own grey echo.
- **Fix:** (1) visible agent-side ack + ensure steer echo is the tail so the live
  bar shows "Reading your message…"; (2) pin `SteerInput`, auto-scroll on
  user/steer events; (3) the deeper fix is live preview (2.1) — until then
  relabel steering "redirect the agent," not "edit the live site."

### 2.7 — Plan review + approval as the front door — MEDIUM (PARTIAL)
- Real gate (`useBuildStream.ts:213`, `PlanPanel.tsx:53-122`), but review is
  approve-or-prose-only (static `<li>`, no edit/reorder), Revise clears the
  textarea while showing the stale plan, and the submit→plan-arrival window is
  dead (`deriveLiveSignal` idle for non-RUNNING, `buildTrace.ts:330`).
- **Fix:** (1) "drafting your plan…" signal + skeleton PlanPanel while plan is
  null; (2) "re-planning" dimmed-stale state on Revise (keep the revision text);
  (3) make the plan adjustable (per-step skip/reorder batched into `request_plan`).

### 2.8 — Async cloud execution + notifications + replay — HIGH (PARTIAL)
- Replay works (`App.tsx:26-29` → `useBuild.ts:22-26`), but: Build has no session
  persistence (Deep Research stashes to localStorage and auto-resumes,
  `useDeepResearch.ts:25-46`; `useBuild` keeps cid only in React state); zero
  notification infra; no WS reconnect (`api/agent.ts:102-103`).
- **Fix:** (1) port Deep Research's session stash to `useBuild` + restore Build
  mode on reload; (2) completion `Notification` + `document.title` badge on
  FINISHED/STUCK/ERROR; (3) WS reconnect with backoff (replay de-dupes by id, so
  it's safe) — reserve `ErrorState` for after N failures.

---

## 3. Visual / polish gaps (dead vs alive)

Statically polished, dynamically frozen. Typography, spacing, hairlines,
resizable split, icons are refined. The problem is liveness:

- **Liveness is RUNNING-gated and mostly absent** (`buildTrace.ts:330`,
  `LiveSignalBar.tsx:17`, `AgentStatusBar.tsx:61`). → Narrate WAITING/AWAITING/
  STUCK; pulse the indicator for all active states.
- **No token streaming + no auto-scroll.** No `scrollIntoView`/ref-follow anywhere
  in any build component. → Add a bottom-sentinel ref + `useEffect` on
  `activity.length` (near-bottom guard); same in `TerminalPane`. **Single
  highest-leverage polish fix.**
- **No entrance animation** — `pmx-rise`/`settle` keyframes unused. → Apply to new
  feed rows.
- **Passive canvas** — no auto-scroll, no "currently executing" highlight linking
  feed↔canvas. → Auto-scroll Terminal; flash matching row on `tool_executing`.
- **Dead "soon" placeholder tab** → hide until available.
- **No "running computer" header signal** → add a live pulse dot + elapsed-step
  counter to the Inspector header (`ResizableSplit.tsx:167-179`).

---

## 4. What we already do well (don't rebuild)

- **Resizable/collapsible/persisted split** (`ResizableSplit.tsx`) — production-grade.
- **Honest plan-first gate** (`PlanPanel.tsx` + `useBuildStream.ts:213`) — real
  blocking state, no false affordance.
- **Unified chat-and-action feed** (`deriveActivity`, `buildTrace.ts:70-173`) —
  interleaved with optimistic echo; thought verbatim never truncated.
- **Expandable drill-down** (`ExpandableDetail`, `ActivityFeed.tsx:43-108`) —
  auto-opens failed actions.
- **Real event-derived Files + Terminal** (`buildTrace.ts:183-239`) — no extra backend.
- **Strict type/role discipline + data-flow gating** — components read only
  `useBuild()`, never fetch.
- **Honest progress reconciliation** (`derivePlanProgress`, `buildTrace.ts:288-312`)
  — stalled steps never lie with a spinning icon.

The bones are excellent. This is a *surfacing and liveness* problem, not a rewrite.

---

## 5. The single highest-leverage UX change

**Ship a client-side `srcdoc` live preview from the first file write, make it the
default Inspector tab, and keep it alive through FINISHED with a persistent "View
live site" link.**

It closes both *critical* pillars (2.1 preview, 2.2 handoff) and unblocks the
*high* one that depends on them (2.6 visual steering). It needs **no backend** for
the first increment — `deriveFiles` already has the file contents
(`buildTrace.ts:183-199`); pipe HTML/CSS/JS into an iframe `srcdoc`, re-render on
each `file_write`. It converts the right pane from "a wide empty panel with a tab
that admits it does nothing" into the literal thing the user came for — their
site, rendering, updating, clickable. Everything else makes the build *legible
and in-command*; this is the one change that makes it *feel like Manus*.

---

## 6. Sequenced roadmap

Cheap-and-loud first, then the critical preview/handoff arc, then depth. (S ≈
hours, M ≈ 1–2 days, L ≈ several days.)

| # | Change | Effort | Files |
|---|--------|--------|-------|
| 1 | **Auto-scroll feed + Terminal** (bottom-sentinel ref + near-bottom guard) | **S** | `BuildSurface.tsx:133-158`, `ExecutionCanvas.tsx:58-85` |
| 2 | **Pin `LiveSignalBar` + `SteerInput` outside the scroll container** | **S** | `BuildSurface.tsx:133-218` |
| 3 | **Liveness state-agnostic** (narrate WAITING/AWAITING/STUCK; pulse for all active) | **S** | `buildTrace.ts:326-367`, `AgentStatusBar.tsx:61`, `LiveSignalBar.tsx` |
| 4 | **Wire `cancel()` to a "Stop" button** + Kill confirm/`isPending` + resolve phantom Pause | **S** | `AgentStatusBar.tsx:38-94`, `BuildSurface.tsx:98` |
| 5 | **Aggregate progress + sticky plan** ("done/total" + bar; hoist/collapse) | **S–M** | `PlanPanel.tsx:62-122`, `BuildSurface.tsx:121-155` |
| 6 | **Hide/disable the dead Preview tab** until `data.available` | **S** | `ExecutionCanvas.tsx:149-203` |
| 7 | **Port Deep Research session stash to `useBuild`** + completion `Notification`/title badge | **M** | `useBuild.ts`, `App.tsx:19-22`, `useBuildStream.ts` |
| 8 | ★ **Client-side `srcdoc` live preview from first file write; default tab; cross-pane flash** | **M–L** | `ExecutionCanvas.tsx:87-203`, new srcdoc renderer over `deriveFiles` |
| 9 | **`DeliverablePanel` hero at FINISHED** (persistent URL, manifest export, summary, auto-switch) | **L** | `BuildSurface.tsx:159-179`, `ExecutionCanvas.tsx:92`, `types/project.ts:23-27`, `projects.ts:91-112` |
| 10 | **Ask-gate** (`AWAITING_USER_QUESTION` + `AskPanel`; interim `attention:true` + relabel) | **M** | `types/agent.ts:8-17`, `buildTrace.ts:163-169`, `BuildSurface.tsx:186-210`, new `AskPanel.tsx` |
| 11 | **Plan front-door polish** (skeleton/"drafting…"; "re-planning" state; visible steer ack) | **M** | `PlanPanel.tsx:45-122`, `buildTrace.ts:330`, `useBuildStream.ts:183-188` |
| 12 | **WS reconnect with backoff** + "reconnecting…" banner | **M** | `api/agent.ts:86-114`, `useBuildStream.ts` |
| 13 | **Adjustable plan** + **visual design-edit mode** (net-new pillars; own milestones) | **L** each | `PlanPanel.tsx:97-122`; new design-edit surface over the preview iframe |

Items 1–6 are a day or two and convert "feels broken" into "feels alive and
in-command." Item 8 is the magic. Items 9–13 close the remaining critical/high
pillars to genuine Manus parity.
