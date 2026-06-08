# Perpleximanus Gap Analysis: Long-Horizon Site Building vs. Manus Architecture

> Produced 2026-06-07 by a 19-agent adversarial workflow (8 code-mapping agents +
> 10 pillar-comparison agents + synthesis), every finding grounded in actual
> source with file:line citations. Scope: the **agent-loop / backend
> architecture**. The UI/interaction-surface gap analysis lives in a separate doc.
>
> Coverage note: 1 of 8 mapping agents (View/condensation) failed to return
> structured output, but the synthesis agent independently re-read `view.py` and
> verified its citations (`24k/32k defaults, keep_head=1/keep_recent=6 at
> view.py:166-169`), so coverage held — that domain became GAP A.

File:line citations are to `packages/core/src/perpleximanus/core/` unless noted.

---

## 1. Executive summary — what we are most wrong about

We have a genuinely excellent **immutable-log spine** and an honest
**plan-completion gate**, but we are badly wrong about **how context survives a
long run**. In priority order, the things that will sink a real site build:

1. **We summarize early and lossily where Manus compacts late and reversibly.**
   `[CRITICAL]` We have *only* the destructive tier. The condenser fires at **24k
   soft / 32k hard tokens** (`view.py:166-167`) against models with **131k–200k**
   windows (`config.py:163,174,215`) — condensing at ~12–18% of the window — wired
   with **bare defaults** (`runtime.py:446`). No compaction tier exists; every
   `ObservationEvent` re-renders its full inline body every turn
   (`events.py:248-253`). On a real build you cross 24k in a handful of turns and
   the agent starts **permanently forgetting its own work** — re-reading files,
   re-creating them, contradicting earlier decisions. The single biggest coherence
   gap.

2. **A tool-less chat turn is interpreted as "the build is over."** `[HIGH, real
   bug]` `RouterAgent.step` maps *every* turn without a tool call to
   `finished=True` (`agent.py:105-108`), while the execution prompt **explicitly
   instructs the model to reply to the user mid-run as a tool-less message**
   (`prompts.py:142-147`). The system tells the model to do something the loop
   reads as completion. Unless an incomplete plan with budget remaining catches it
   (`engine.py:969-1001`), a single conversational sentence ends the run. (This is
   the talk-back feature — it is actively dangerous as written.)

3. **The filesystem is a work area, not memory.** `[HIGH]` The Manus key trick is
   entirely absent. `file_read` has no offset/limit and dumps whole files
   (`files.py:17-33`); `extract`/`search` return full blobs inline
   (`retrieval.py:33-58`); the executor explicitly **drops `artifacts`** and
   materializes full `content` (`executor.py:100-107`).

4. **The approved plan is not pinned, and never re-surfaces near the tail.**
   `[HIGH]` The `PlanEvent` gets no condensation protection (`view.py:211`) — on a
   long run it gets dissolved into a lossy summary. And it is rendered once at its
   original chronological position (`events.py:325-334`), drifting toward the HEAD
   as execution events accumulate — into the lost-in-the-middle dead zone. No
   recency recitation at all.

5. **We can build a site but cannot show it.** `[HIGH]` `deploy_preview` is
   declared (`registry.py:63`) but **unimplemented and deferred**
   (`builtin/__init__.py:35`), and every shell call dies at the 300s ceiling
   (`executor.py:90`) — a dev server survives only by undocumented `nohup &`.

Everything else (logit masking, KV-cache economics) is **low-severity and
largely latent.**

---

## 2. The gaps, prioritized

### GAP A — Context management: missing reversible compaction + premature lossy summarization `[CRITICAL]`

**Manus:** Two tiers. **Compaction** first — losslessly strip content still in
the environment (replace a 500-line file body with "saved to /src/main.py",
recoverable via re-read). Only past a *pre-rot* threshold (~128–256k) does it
fall back to lossy **summarization** of older turns into structured JSON, keeping
the last 2–3 tool turns RAW. Preference: raw > compacted > summarized.

**Us:** Only the lossy tier. `LLMSummarizingCondenser` keeps `keep_head=1` +
`keep_recent=6` raw and summarizes the middle (`view.py:146-227`). The instinct
is right, but:
- **Trigger fires catastrophically early** — 24k/32k against 131k–200k windows,
  wired bare (`runtime.py:446`).
- **Token estimate is naive char/4** summing only message `.content`
  (`engine.py:364-366`) — ignores tool-call JSON, schemas, system prompt.
- **Summary is freeform, not structured** (`summarizer.py:19-21`) — no schema
  forces file paths / plan / TODOs to survive.
- **`keep_recent=6` is EVENTS, not tool turns** — preserves only ~3 turns.

**Fix:** (1) Model-aware threshold from `context_window` (soft ~60–70%, hard
~80%). (2) Add a reversible compaction pass in `View.of` rewriting large
env-backed observations to `[compacted: 412 lines saved at src/Hero.tsx — re-read
with file_read]`. (3) Structured summarizer (files/decisions/plan-status/open-
threads), exclude the `PlanEvent`, count `keep_recent` in tool turns, fix the
token estimate.

### GAP B — "No tool call" overloaded as "finished" `[HIGH, real bug]`

**Manus:** One tool call per iteration; the terminal step is itself a special
**message tool**; "still working vs. done" is unambiguous.

**Us:** `RouterAgent` treats "no tool call" as `finished=True` unconditionally
(`agent.py:105-108`), always routing to the finish path (`engine.py:925`) — the
intended no-op path (`engine.py:1042`) is **dead code** for the real RouterAgent.
The only rescue is the auto-continue gate, and only with an incomplete plan +
budget. The prompt *mandates* a tool-less mid-run reply (`prompts.py:142-147`),
then reads it as completion.

**Fix:** Decouple. Add a real **`finish`/`done` virtual tool** so completion is
affirmative; keep prose thoughts attached to action tool calls. Until then the
`prompts.py:142-147` instruction is **actively dangerous** — rephrase so the
reply rides as the `thought` on the next tool call. (Cheap interim: return
`finished=False` for prose-only turns → routes to the genuine no-op path.)

### GAP C — Filesystem as primary external memory `[HIGH]`

**Manus:** Filesystem = true unbounded memory; the model holds only PATHS + short
descriptions; fetches on demand.

**Us:** Workspace used only as a work area. All outputs materialized inline;
executor drops `artifacts` (`executor.py:100-107`); `file_read` dumps whole files
(`files.py:17-33`); `extract`/`search` return full blobs. Note write tools
already return `artifacts=[path]` — the substrate exists; the discipline is
missing.

**Fix:** (1) `offset`/`limit`/line-range on `file_read`. (2) Auto-spill large
observations to `.pmx/observations/<id>.md`, render PATH + head + "use file_read
with offset." (3) `head`/cap on `extract`/`search`. (4) Teach the prompt the
memory contract.

### GAP D — Plan unpinned against condensation + no recency recitation `[HIGH]`

**Manus:** Structured plan injected when needed; light recitation re-emitted into
the RECENT tail to fight lost-in-the-middle.

**Us:** Plan-as-mode (acceptable divergence — not worth sub-agent machinery). But
the `PlanEvent` gets no condensation protection (`view.py:211`) and is rendered
once at a fixed log position (`events.py:325-334`), drifting toward the HEAD.
After one or two condensations the executing agent no longer sees its own ordered
steps. The completion gate still works (reads raw log), so the loop won't *lie* —
but the model grinds incoherently → STUCK/partial_plan. A grep for
`recite/remaining/resurface/tail` finds nothing.

**Fix (do together — same `view.py:211` surface):** (1) Pin the maximal-revision
`PlanEvent` + head user message against condensation. (2) Append an *ephemeral*
"objective + checklist + next pending step" block at the View tail every Nth
step, built from the existing `PlanEvent` + the done-set the loop already
computes — **no model call**.

### GAP E — Small stable tool set is solid, but site DELIVERY is capped `[HIGH]`

The foundation is Manus-faithful: 11 atomic tools, immutable registry, true
CodeAct, persistent workspace. But: (1) `deploy_preview` declared/unimplemented
(`registry.py:63`) — **the agent can build a site but cannot show it**, though
the backend can already expose `PREVIEW_PORT=8000`. (2) No long-running-process
management — 300s ceiling kills dev servers. (3) Level-2 utilities-via-shell is
implicit, undocumented.

**Fix:** Ship `deploy_preview` now (thin tool: `expose_port(PREVIEW_PORT)`→URL
artifact). Add a `serve`/process tool (detached server, log-to-file, process
registry). Document the Level-2 contract; pin/verify the base image.

### GAP F — Failure visibility strong; error policy + anti-fewshot missing `[MEDIUM]`

The strongest pillar: one `AgentErrorEvent` per failure, no hidden retries,
traces render verbatim, append-only persistence. But: (a) no explicit
`<error_handling>` ladder (verify→fix→alternate→escalate) — a local Qwen won't
reliably invent it. (b) **Temperature pinned to 0.0** (`types.py:93-95`) —
opposite of Manus's anti-fewshot jitter; a long uniform run is a near-
deterministic self-imitation chain feeding drift. (c) The summarizer doesn't
preserve "what was tried and failed."

**Fix:** Add the `<error_handling>` block; set `AGENT_DRIVER` temp 0.3–0.5 (keep
summarizer 0.0); make the summarizer preserve failed approaches.

### GAP G — Knowledge + Datasource events missing `[MEDIUM]`

The immutable-log spine is a **MATCH — arguably better-specified than Manus.**
But only 4 of 6 event roles exist. No Knowledge event (closest analog: Skills,
injected globally all-or-nothing into the prompt, unscoped). No Datasource analog
— data-API contracts reach the model only as inline observation text that
condensation later forgets. **The Datasource gap is the real long-horizon
hazard:** an API schema learned mid-run gets compressed into lossy prose → the
agent hallucinates an endpoint.

**Fix:** Add two pinned, condensation-immune event types: `KnowledgeEvent {scope,
snippet}` (move Skills off the prompt, restore per-step relevance) +
`DatasourceEvent` (durable API docs). Both exempt from condensation. Additive, no
`SCHEMA_VERSION` bump.

### GAP H — Logit masking / KV-cache economics `[LOW, latent]`

Mostly right by accident (static prompt, frozen log, deterministic tool order).
Diverges: we *remove* tools from the schema by mode (`engine.py:382-394`) rather
than masking; mode-flip mutates both prompt + tools array; no prompt caching
exists. **Low severity for local-first** — but the moment a hosted model is wired
to `AGENT_DRIVER`, the un-cached input:output ratio explodes the bill. **Do not
invest here for coherence.** Cheap hardening: stop mutating the tools array on
mode boundary, `sort_keys=True` serialization, add `cache_control` before any
hosted model.

---

## 3. What we already do well

- **The immutable-log spine** — frozen append-only typed union, gap-free seq, no
  update/delete, formal G1–G5 contracts, State+View as pure functions. **Do not
  touch it.**
- **The plan-completion gate is honest** — even when the model loses its plan, the
  loop surfaces STUCK/partial_plan rather than lying.
- **Failure visibility is the strongest pillar** — leave the machinery alone.
- **CodeAct + small-stable-set discipline is Manus-faithful.**
- **Planner-as-mode is an acceptable divergence** — the *unpinned plan* is what's
  worth fixing, not the architecture.

---

## 4. The single highest-leverage change

**Raise the condensation threshold to be model-aware AND pin the `PlanEvent`
against condensation** — GAP A(1) + GAP D(1), together at `view.py:166-211` and
`runtime.py:446`.

Every long-horizon failure mode (losing the thread, re-creating files, dissolving
the plan, grinding into STUCK) is downstream of **one root cause: a destructive
summarizer firing at ~12–18% of the window with bare defaults and no plan
protection.** It's also the cheapest high-impact change — no new tools, no model
call. This alone converts the agent from "forgets its own work after a handful of
turns" to "keeps its work and its plan for the whole build."

---

## 5. Sequenced roadmap

Effort: S ≈ hours, M ≈ 1–2 days, L ≈ several days.

| # | Change | Effort | Files |
|---|--------|--------|-------|
| 1 | Model-aware condensation threshold | **S** | `view.py:166-167`, `runtime.py:446` |
| 2 | Pin `PlanEvent` + head user msg against condensation | **S** | `view.py:211`, `view.py:39-92` |
| 3 | Fix "no tool call = finished" bug (add `finish` tool) | **M** | `agent.py:96-108`, `engine.py:925,1042`, `prompts.py:142-147` |
| 4 | Recency recitation in View (ephemeral tail block) | **M** | `view.py`, `engine.py:530-633` |
| 5 | `deploy_preview` + `serve`/process tool | **M** | `builtin/__init__.py:35`, `_container.py:193-210` |
| 6 | Filesystem-as-memory, read side (offset/limit) | **S** | `files.py:17-33`, `retrieval.py:33-58` |
| 7 | Filesystem-as-memory, spill side (auto-spill large obs) | **M** | `executor.py:100-107`, `events.py:248-253` |
| 8 | Reversible compaction tier | **L** | `view.py`, `events.py` |
| 9 | Structured summarizer + turn-based keep_recent | **M** | `summarizer.py:19-21`, `view.py:169-227` |
| 10 | `<error_handling>` ladder + agent temperature | **S** | `prompts.py:134-162`, `types.py:93-95` |
| 11 | Knowledge + Datasource events | **L** | `events.py`, `view.py`, `skills.py` |
| 12 | KV-cache hardening (before any hosted model) | **M** | `openai_provider.py:150-203` |

**Dependencies:** 1–2 are independent S-wins; do them first (the §4 highest-
leverage change). 6→7→8 form the filesystem-as-memory chain in order. 9 depends on
7/8. 12 is gated on "before wiring a hosted `AGENT_DRIVER`."
