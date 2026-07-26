> **SUPERSEDED / HISTORICAL (as of 2026-07-07).** June-era live-runthrough issue log, frozen mid-execution.
> Historical only. Not a source of current status or operating instructions.

# Dylan's runthru — 2026-06-20 (DR → slides test session)

10 issues surfaced in a live test (short Deep Research → make slides), each root-caused by a parallel tracer
(file:line evidence + minimal fix). Grouped by SHARED root cause. Status: TRACED — awaiting Dylan's go to build,
per the roadmap loop (surface → discuss → build). Fixes follow the v0.1 gate discipline (plan→gpt-5.5→verify→commit).

## ⚠ External, not a code bug (do this first): OpenRouter key TOTAL LIMIT exhausted
The concrete agent-log error behind the "stall" (R2) and the repeated-resume failures (R4) is:
`LLMAuthError [or-deepseek-deepseek-v4-pro]: Key limit exceeded (total limit)` (conv_05026ef3...). The OpenRouter
**key** has a per-key spend cap configured that's been hit — SEPARATE from the account credit balance ("I have
credits" can be true while the key's own limit is 0/exhausted). Fix on the account: raise/remove the key's limit at
openrouter.ai → settings → keys. This alone unblocks paid models (DeepSeek v4 etc.). Everything below is the Disco-
side hardening so this (and its siblings) fail GRACEFULLY and HONESTLY instead of silently/ confusingly.

---

## Cluster 1 — DR→slides handoff (the biggest UX cluster)
**R3 [High] — slides prompt fills the screen + pollutes history.** `frontend/.../research/NeedMoreCard.tsx:727-730`
builds `seedTask` = instruction **+ the ENTIRE report markdown inlined**, and that one string is BOTH the model
input AND the persisted/displayed user `MessageEvent` (`ws.py:49`; rendered verbatim `buildTrace.ts:275-288`). No
split between what the user sees and what the model receives.
→ FIX: short visible message `"Make slides for the deep research report: \"<query>\""`; pass the report as a separate
`context` field on the `send_message` WS frame, stored as a HIDDEN `EventSource.ENVIRONMENT` message (hidden in feed
by `buildTrace.ts:303-322`) BEFORE the short user message. Model sees both; history shows the one-liner.
(Files: `NeedMoreCard.tsx:727`, `ws.py:48-51`, WS frame type.)

**R6 [Med] — `slides_generate` invisible in plan mode → model refuses.** Two causes: (1) the planner correctly hides
write tools (`slides_generate` is `read_only=False`) from the PLANNING schema (`driver.py:204-221`); (2) the prose
hint that says "you'll GET slides_generate after approval" (`_AGENT_PLANNING_CAPABILITY_BLOCK`, `prompts.py:425`) is
appended ONLY for `flavor=="agent"`, but DR→slides runs in `build` flavor (`NeedMoreCard.tsx:731` → `runtime.py:708`).
So no tool in schema AND no hint it's coming.
→ FIX: move the capability-block append OUTSIDE the `if flavor=="agent"` guard (`prompts.py:480-487`) so build flavor
also gets the post-approval-tools hint; update the stale comment at `prompts.py:414`.

## Cluster 2 — Engine loop / error handling (R2+R4 SAME root + conversation)
**R2 [High] — build "literally stalled."** conv_05026ef3: plan auto-approved (seq6), first execution LLM call hit
`LLMAuthError: Key limit exceeded` (seq13) → `ErrorEvent(model_error)` → halt, **no fallback model**.
**R4 [High] — "loaded over and over, didn't work."** Same conv, each resume re-hit the same terminal auth error
(seq15-21); the loop emits a GENERIC `model_error` indistinguishable from a transient failure, so the **Resume button
stays active** and the user kept retrying a permanently-failing run.
Disco-side defects (`driver.py:497-530`): (a) the DEFECT-6 requery path retries 2× on `LLMAuthError` — useless, a bad/
capped key won't be fixed by rephrasing; (b) emits generic `model_error` for a terminal auth/budget failure.
→ FIX: (1) skip the requery for `LLMAuthError` (`driver.py:497`); (2) emit a distinct `code="auth_error"`
(`driver.py:529`); (3) frontend suppresses Resume when the latest error is `auth_error` + shows "key limit/credits"
guidance. STRETCH (recommended): on a terminal provider auth/budget error, **fall back to the configured local model**
(Qwen) instead of halting, so a capped paid key degrades to local rather than dead-stops.

## Cluster 3 — Model/key wiring (R5 latent bug + R9 visual)
**R5 [High] — paid OpenRouter model → "no credits."** `_overlay_stored_secrets` (`runtime.py:652`) injects the
decrypted key under the NEW env name `DISCO_OPENROUTER_API_KEY`, but every OpenRouter model entry in
`disco-config.json` declares the LEGACY `api_key_env: "PMX_OPENROUTER_API_KEY"`; `build_providers` (`wiring.py:128`)
resolves `environ.get("PMX_OPENROUTER_API_KEY")` → `None` (the back-compat only covers DISCO→PMX, not the reverse) →
anonymous call → paid model 402. (Masked right now because `~/disco-dev-up.sh` sources `agent.env` which sets the
`PMX_` name — but the encrypted-store path is broken without it.)
→ FIX: in `_overlay_stored_secrets`, inject the openrouter secret under BOTH `DISCO_OPENROUTER_API_KEY` AND legacy
`PMX_OPENROUTER_API_KEY` (one line). [Alt: migrate the 4 config entries to `DISCO_*` — but the overlay fix is the
robust one and aligns with the security campaign's B1 secret-ref work.]

**R9 [Med] — model override should control every surface.** It ALREADY does, server-side: a pick saves
`last_selected_model`, and every surface with `override==null` uses it (`conversations.py:26-39`, `ws.py:241-242`).
The bug is VISUAL — DR/Research pickers start at `null` and don't seed from it, so they SHOW "Default model" and look
ignored. → FIX: seed the picker from `useLastSelectedModel()` in `ResearchSurface.tsx:22` + `useDeepResearch.ts:48`.

## Cluster 4 — Frontend (R1 race + R10 reflow)
**R1 [High] — iterate toggle did nothing.** `useDeepResearch.ts:78-90`: toggling iterative ON starts a fresh
`preCreate({iterative:true})` but does NOT `setPreCid(null)`, so a submit in the in-flight window uses the STALE
preCid (created `iterative:false`); `_iterative_for(cid)` stays False; the engine's `if self._iterative:` refine loop
(`engine.py:650`) never runs → silent standard run. → FIX: `setPreCid(null)` before the new `preCreate.mutate`
(`useDeepResearch.ts:84`) so `submit()` falls through to `create.mutate({...,iterative})` carrying the live value.

**R10 [Med] — DR selection reflows the cards.** Selecting DR unmounts/remounts the whole surface
(`ResearchSurface.tsx:38-43`) and the DR controls render as a `footer` block adding ~48px → `justify-center` recenters
→ wordmark + input shift; `ExampleQueries` also vanishes. → FIX: add an `extraControls` prop to `QueryInput` and render
Depth/Recency/Iterative INLINE in the existing flex-wrap pill row (same size/register, no new row); drop the hint
paragraph; optional 150ms fade-in. (Files: `QueryInput.tsx`, `DeepResearchSurface.tsx:119-149`.)

## Cluster 5 — Slides integrity & viewing
**R7 [High — no-false-affordances] — can't tell real deck from HTML fallback.** Backend emits a `renderer` field
(`c3-brand`/`pptx-native`/`libreoffice` = real, `fallback` = degraded HTML) but the frontend DROPS it
(`buildTrace.ts:200-230`) and the stale type + `SlidesBlock.tsx:138,152` mislabel every real C3 deck as "Marp-
rendered." → FIX: thread `renderer` through (`buildTrace.ts` slides type + extraction, `grounded.ts:109` union,
`SlidesBlock.tsx`) and show HONEST labels: ⚠ visible warning on `fallback`; "structured (editable)" on C3.

**R8 [Low — dev-only] — can't view deck over the tunnel.** The deck artifact is served from `:8000` with CSP
`frame-ancestors 'self'` (`files.py:44`); the `:5173` UI iframes it cross-port → browser blocks ("permission").
Real boundary is the iframe `sandbox="allow-scripts"` (no `allow-same-origin`, `PreviewPane.tsx:420`), so frame-
ancestors here is redundant for that cross-port case. → FIX: allow the configured frontend origin —
`frame-ancestors 'self' http://localhost:5173` (NOT a blanket `*`; keep it scoped — touches security). IMMEDIATE
WORKAROUND (no code): open the deck directly at `http://localhost:8000/conversations/<cid>/artifacts/<deck>.html?inline=true`.

---

## Codex review (2026-06-20): SHIP-WITH-FIXES — proceed with tweaks
- **R4 WRONG** — "Resume stays active" is unsupported (ErrorEvent → state ERROR `state.py:139`; Resume renders only for PAUSED/IDLE `AgentStatusBar.tsx:163`). DROP the Resume-gate. Do the honest terminal-error message; the real repeat-retry path needs re-tracing separately (don't build a wrong gate).
- **R2** — classify terminal provider failures broadly (LLMAuthError, BudgetExceeded, quota/402 key-limit) + distinct `auth_error` code. Local fallback is SEPARATE/visible/capability-checked — NOT part of this minimal fix.
- **R1** — also guard the async `preCreate` onSuccess to match the request settings (older in-flight create can't overwrite the new cid), not just `setPreCid(null)`.
- **R3** — update BOTH the TS and Pydantic WS frame schemas; hidden context must NOT start with `⚠` or `User uploaded:` (else it surfaces).
- **R5** — overlay BOTH env names; update `test_secret_overlay`. There are **SIX** OpenRouter entries, not four.
- **R6** — update tests pinning build-flavor byte-identity; adjust wording for autonomous auto-approval.
- **R8** — configurable origin allowlist incl. `127.0.0.1` + tunnel origins, NOT hard-coded `localhost:5173`.
- **R9** — seed from `useLastSelectedModel()` as INITIAL only; don't clobber a user's live picker change when the async query resolves.
- **R10** — also handle `ExampleQueries` vanishing (keep it on the DR empty state or reserve equivalent space).

## Recommended build order (after Dylan approves)
1. **R5 overlay-both-names** (1 line) + **R1 iterative race** (1 line) — tiny, high-value, unblock paid models + iterate.
2. **R2/R4 auth-error handling** (skip requery + distinct code + Resume-gate + local fallback) — stops the confusing dead-retry loop.
3. **R3 DR→slides handoff** (short msg + hidden context) + **R6 plan-mode hint** — fixes the screen-filling prompt + tool refusal.
4. **R7 renderer honesty** + **R9 picker seeding** + **R10 inline controls** + **R8 CSP origin** — UI integrity + polish.
Each: plan → gpt-5.5 plan review → implement → disco gates → gpt-5.5 diff review → real-UI screenshot/verify → commit.
