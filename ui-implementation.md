# The Full Run — UI Build Prompts

The complete, sequenced set of build prompts for the perpleximanus UI. Five prompts, in dependency order. Each "PROMPT" block is paste-able to the build agent as one task; everything else is for you. This run **supersedes** the earlier single answer-UI hand-off — Prompts 1 and 3 cover and extend it.

---

## How to use this run

**Order (dependencies flow downward — don't reorder):**
1. **Design System** — the token substrate everything else consumes.
2. **Application Shell** — the hideable nav, the frame, mode awareness; the container the views live in.
3. **Main Interaction Surface** — the answer document + the input control cluster (model pill, mode selector, thinking toggle). Refines the existing answer UI and fixes its gaps.
4. **Settings** — the model-assignment matrix, skills, MCP.
5. **History** — the conversation library.

**The five gaps I flagged earlier, and where each is fixed:**
- *Typography — four roles, a distinct reading face for the answer body (the #1 lever):* **Prompt 1** (defined) → **Prompt 3** (applied to body).
- *Hover source-card occludes the answer text:* **Prompt 3**.
- *Empty state too empty / no identity:* **Prompt 3** (+ identity tokens from 1, frame from 2).
- *Answer body line-height slightly loose:* **Prompt 1** (token) → **Prompt 3** (applied).
- *Submit button too low-contrast, reads as disabled:* **Prompt 3** (input-as-card + solid submit).

**Shared DNA — every prompt assumes all of this; it is not repeated per-prompt:**

- *Philosophy:* the interface is a **"Scandinavian subway map" — clean, ruthlessly functional, ultimately invisible; a vessel for facts, not a brand.** The reconciliation that governs this whole run: **quiet in appearance, rich in function.** Restraint is about *decoration* (no gradient cards, no ambient color, hairlines not shadows) — NOT about withholding controls. The app has real navigation, model/mode controls, and settings; they are simply *quiet, well-organized, and legible* rather than loud. Do not use "minimalism" as an excuse to omit a control the user asked for.
- *Dark-mode first.* Dark is the **primary** design target — a near-absolute black base, the surface ladder built via subtle lightness shifts + hairline (1px, low-contrast) borders, never heavy shadows or glassmorphism. Light mode must be equally deliberate, but design dark first. The existing dark mode (clean near-black) is the right direction — build on it.
- *Chroma carries meaning, never decoration.* Color appears only on **actionable or verifiable** elements — citations, verification states, the primary action, interactive affordances. A desaturated muted blue for links (never reflex/Google blue). If an element isn't actionable or carrying meaning, it's neutral.
- *Stack:* React + TypeScript, Vite, **TanStack Query** for all server state, **Tailwind** + a **Radix/shadcn-class** primitive layer styled to the design system.
- *Data-flow discipline (binding, the architectural side of "not vibe-coded"):* UI components → TanStack Query **hooks** → a **data-access layer** (`src/api`) → the API. **A component never calls `fetch` or the API directly — that is a defect.** Query hooks `use[Resource]`, mutations `use[Action]`.
- *Build against fixtures first, then wire live.* Build each surface against realistic fixtures (include the unhappy paths — weak/unsupported claims, paywalled/blocked sources, provider errors, empty lists). Then wire the real data/stream. Token frames in streaming are ephemeral typewriter rendering; the final event is the source of truth — reconcile to it.
- *No browser storage* (localStorage/sessionStorage) — keep state in React/query cache.
- *Standing rules for every prompt:* stay strictly in the prompt's lane (don't build the next prompt's surface); write component tests (vitest-class) covering streaming/empty/loading/error/failure states; `npm` build + lint clean; accessible Radix primitives (keyboard/focus/contrast); and **report back** with the design decisions you made, screenshots/description in both themes, and any spec ambiguity you had to interpret (flag it, don't silently diverge).

**After each prompt (you):** look at it in both themes at real content length; trust your eye over the green test suite (tests prove function, not premium feel); check the lane was respected and the data-flow discipline held; bring me the agent's design decisions and ambiguity flags so I can write a verbatim patch if needed. Expect 2–3 taste-iteration passes per surface — that's the work, not a failure.

---

## PROMPT 1 — The Design System

> Builds the token substrate and proves it on the existing answer surface. Fixes the typography and line-height gaps. Everything downstream consumes this.

**PASTE:**

You are building the **design system** for perpleximanus — the token foundation every other surface will consume — and proving it by applying it to the existing answer surface. **Build only the system + its application to what already exists; do not build new screens, nav, or settings.** Read `basis-of-design.md` §13.7 (design language) first. Honor the Shared DNA you were given (dark-first, quiet-but-functional, chroma-on-meaning).

**Deliver a token system** as CSS variables / Tailwind theme tokens, structured so every later surface references tokens, never hard-coded values:

1. **Typography — four distinct roles, rem-based (never px). This is the highest-leverage part; choose with real intent.**
   - **Display** — a characterful serif, used for the wordmark and the answer's H1 (the question). The current serif wordmark is the right instinct — make it a deliberate, named role and extend it to the answer title so an answer reads like an article with a serif headline.
   - **UI / workhorse** — a clean grotesk for nav, labels, tabs, buttons, controls. Broad glyph coverage to avoid fallback-font layout shift.
   - **Reading** — a **separate face for the long-form answer body, distinct from the UI grotesk.** This is the single biggest "clean → designed" lever and it's currently missing (body and UI share one face). Given "document-grade / vessel for facts," strongly consider a **reading-optimized serif** for the body (it makes the answer feel like a publication and pairs with the serif display) — or a reading-optimized grotesk clearly distinct from the UI face. Pick deliberately and tell me which and why.
   - **Mono** — `JetBrains Mono` or `Fira Code` for code.
   - **NOT** Inter / Roboto / Arial / system fonts, and **not** Space Grotesk. Distinctive faces true to "calm, precise, invisible."
   - **Line-height tokens:** UI/labels tight at **1.3–1.4**; long-form body relaxed at **~1.55** (the current body runs slightly loose — land it near 1.55).

2. **Spacing scale** — rem on a 16px base, applied via flex/grid `gap` where possible: `0.25` / `0.5` / `1` / `~1.5` / `~1.9`. **Border-radius `~0.5rem`** — soft, not pill/bubbly. Use the CSS `:has()` selector for content-dependent padding rather than JS layout math.

3. **Color — dark-first, two complete themes.** Near-absolute black base in dark; clean near-white in light. The **surface ladder**: elevation via subtle lightness shifts + **hairline borders only — never shadows or glassmorphism**. Links: **desaturated muted blue**, not reflex blue. Define a small set of **meaning colors** (citation/link, verification-supported, verification-weak, verification-unsupported/failure, primary-action) — these are the *only* places chroma appears. Everything else is a neutral from the surface/text ramp.

4. **Motion tokens** — minimal, latency-masking. Define the **signature time-to-first-token micro-interaction** (a small, on-brand constructing/processing motion that replaces a generic spinner — the one memorable, restrained moment). Plus gentle transition tokens for streaming text and card reveals. No gratuitous animation.

**Prove it:** re-apply the tokens to the **existing answer surface** so the four type roles are visibly distinct (especially the new reading face on the body), the line-height lands near 1.55, the palette/surface-ladder/muted-blue-links are token-driven, and both themes render deliberately. This is a refactor-to-tokens of what exists, not a redesign of its layout.

**Anti-patterns (automatic fail):** generic AI fonts; Space Grotesk; purple-on-white; chroma as decoration; shadows/glassmorphism for depth; px instead of rem; body and UI sharing one face.

**Done when:** a documented token system exists; the answer surface renders through it in both themes with four visibly-distinct type roles and the corrected line-height; no surface value is hard-coded. **Report** the exact faces you chose (and why they fit "calm, precise, invisible"), the palette, and the TTFT micro-interaction you designed.

---

## PROMPT 2 — The Application Shell

> The hideable nav, the layout frame, mode awareness, theme toggle. The container every view lives in.

**PASTE:**

You are building the **application shell** for perpleximanus — the persistent frame that hosts every view — and **nothing inside the views themselves** beyond placeholders. Consume the design system (Prompt 1). Honor the Shared DNA (quiet-but-functional, dark-first, data-flow discipline).

**Build:**

1. **A hideable left nav rail.** Collapsible (a clear toggle; remember collapsed/expanded within the session via React state, not browser storage). Items: **New** (start a query), **History**, **Spaces** (present but visibly **dormant/disabled** with a quiet "coming soon" affordance — the surface exists, the feature lands later), **Settings**. Quiet styling — hairline separation, the UI grotesk, chroma only on the active item. When collapsed, icon-only with accessible labels; when expanded, icon + label. Keyboard navigable.

2. **The layout frame** — a main content region beside the rail that hosts the active view via **client-side routing** (e.g., React Router): routes for the main surface (`/`), history (`/history`), settings (`/settings`). Build the views as **labeled placeholders** for now (Prompt 3/4/5 fill them) — the shell's job is the frame and navigation, not the contents.

3. **A persistent active-mode indicator** (legibility — you must always know what the system is doing). A quiet, always-visible indicator of the current mode — **Search / Build / Deep Research** — in the frame (e.g., top of the main region). For now it reflects/defaults to **Search**; Build and Deep Research are shown as **available-but-dormant** options (visibly present, clearly not-yet-active). The *selector control* itself lives in the main surface (Prompt 3); this is the **indicator** that the mode is X.

4. **Theme toggle** — relocate/standardize the existing light/dark toggle into the shell chrome (it currently floats top-right). Persist within session via React state.

5. **Responsive behavior** — the rail collapses gracefully on narrow viewports (overlay/drawer rather than squeezing content). The frame must not cause layout shift when the rail toggles.

**Data-flow:** any data the shell needs (e.g., a future user/owner context) flows through hooks → data layer, never direct calls. For now most shell state is local UI state.

**Anti-patterns (automatic fail):** a non-collapsible rail; the rail squeezing content instead of overlaying on mobile; chroma on inactive nav items; shadows for the rail's separation (use a hairline); layout shift on toggle; building the actual view contents (that's later prompts).

**Done when:** the rail collapses/expands cleanly, routing switches between placeholder views, the active-mode indicator is always visible and reads "Search," dormant items (Spaces, Build, Deep Research) are visibly present-but-inactive, both themes render, and it's responsive with no layout shift. **Report** the nav structure and how dormant-vs-active is communicated.

---

## PROMPT 3 — The Main Interaction Surface

> The answer document + the input control cluster. Refines the existing answer UI, fixes its remaining gaps, and adds the model/mode/thinking controls. The heart of the app.

**PASTE:**

You are building the **main interaction surface** — the query input with its control cluster, and the answer document — inside the shell's `/` route. **Build only this surface; not settings, not history, not the agent/build or deep-research views.** Consume the design system (Prompt 1) and the shell (Prompt 2). The answer surface mostly **already exists** — this is refinement + the control cluster, not a rebuild. Honor the Shared DNA.

**A. Fix these specific gaps in the existing answer surface:**

1. **The hover source-card must not occlude the answer text.** Currently it floats over and cuts off the prose mid-sentence. Offset/reposition it (and/or dim-but-don't-cover the body) so the corroborating snippet is readable *without* hiding the content it annotates. This is a legibility regression at the exact moment of verification — fix it.
2. **The input becomes a generous card, not a thin bar.** Give it real height and internal padding (study Perplexity's input card geometry). It should read as a substantial surface, elevated via the surface ladder (lightness + hairline), not a skinny strip.
3. **The submit control becomes the primary action** — a solid, high-contrast filled control (uses the primary-action meaning color). It currently reads as disabled. It must look like the obvious way to send.
4. **Apply the reading face** (Prompt 1) to the answer body, with line-height ~1.55. The body must be visibly distinct from the UI grotesk.

**B. The empty state (pre-first-query) — fill it with calm identity.** Currently it's a near-empty void with just a bar, which reads unfinished. Add: the **wordmark** (display serif), the **input card** with its control cluster (below), and a few **quiet, outlined example-query pills** (icon + label, restrained — like Manus's category pills, NOT gradient marketing cards). It should feel intentional and calm, reflecting "vessel for facts" — not empty, not busy.

**C. The input control cluster (quiet but functional — this is where the user's controls live):**
- **A model "leader" pill** — selects, by hand, the model that **leads this conversation** (overriding the settings default for this conversation only). Tapping it opens a picker of available models, **grouped local vs. overflow**, showing **cost next to each** ("free" for local, "$/Mtok" for OpenRouter) — the picker is cost-legible so the user chooses with spend in view. The selection is **absolute** (the system uses exactly that model; there is no automatic routing). Show the current leader on the pill.
- **A mode selector** — Search / Build / Deep Research. **Search is active; Build and Deep Research are present but visibly dormant/disabled** (they light up in later phases). Selecting Search keeps the surface in research mode. This selector drives the shell's active-mode indicator (Prompt 2).
- **A "Think" toggle** — a per-conversation reasoning-effort control (off / on). Wire it to a **per-conversation flag passed to the backend**; the backend's full handling is **deferred/placeholder for now** (its exact effect depends on the chosen models — mark it clearly as a wired-but-not-yet-fully-implemented control; it must not pretend to do more than it does). Surface it quietly in the cluster.
- The cluster is **quiet** — these controls are small, hairline-bordered, chroma only where actionable. Do not turn the input into a busy toolbar; organize the controls cleanly (a compact row beneath/within the input card).

**D. Preserve and keep working everything the existing answer surface already does right:** the document layout (H1 question as title, section headers, sticky "On this page" ToC), inline `[n]` citations anchored to claims, the dual-tab **All Searched / Cited** source panel with **re-scope controls** (filter domains / drop weak / re-synthesize), structured components (syntax-highlighted code, callout modules, tables), verification states on claims (supported/weak/unsupported via meaning colors), explicit failure rendering (paywalled/blocked sources shown, not dropped), follow-up pills, and Stop Generating. Render all of it through the new tokens.

**E. Provider errors surface cleanly.** When a model/provider returns an error (the model can't perform the request), render the **real error content** to the user legibly — never a generic "something failed," never swallowed. This is a distinct state from a *source* failing.

**F. Streaming with ZERO layout shift** — pre-allocate the source-panel region and stream structured components into rigid containers; tables with predictable sizing; `<pre>` expands via CSS transition, not abrupt reflow. Reconcile token frames to the final event. Demonstrate the TTFT micro-interaction (Prompt 1) as the loading state.

**Build against fixtures first** (including weak/unsupported claims, paywalled/blocked sources, a provider-error case, and an empty state), then wire the WebSocket stream and the retrieval data via hooks → data layer.

**Anti-patterns (automatic fail):** the hover card covering body text; a thin-bar input; a low-contrast submit; body and UI sharing a face; a busy toolbar input; gradient/marketing pills; layout shift while streaming; an end-of-document reference dump without inline anchors; a cramped chat column for long answers; `fetch` in a component; swallowing a provider error into a generic failure.

**Done when:** all five gaps are fixed; the control cluster (leader pill with cost-legible picker, mode selector with dormant Build/Deep Research, Think toggle wired-but-marked) works; the empty state feels calm and identified; everything the surface already did right still works through the new tokens; streaming has zero observable layout shift; provider errors render cleanly; both themes deliberate. **Report** the control-cluster design, how the cost-legible picker reads, and how you marked the Think toggle's deferred backend.

---

## PROMPT 4 — Settings

> The model-assignment matrix (the absolute, manual model story), plus the skills and MCP configuration surfaces.

**PASTE:**

You are building the **Settings** view (the shell's `/settings` route) — **only settings; no other surface.** Consume the design system and shell. Honor the Shared DNA (quiet-but-functional, data-flow discipline). Settings is dense by nature — keep it *organized and quiet*, not decorated.

**Build three sections:**

1. **The model-assignment matrix (the core).** Model selection in this app is **absolute and manual — there is no automatic routing.** Provide:
   - A **default primary model** selector (the model that leads conversations unless the main-screen pill overrides it).
   - An **explicit per-role selector** for every other function: **RAG answerer, query rewriter, summarizer, NLI verifier.** Each role gets its own dropdown.
   - For each selector, show the model's **declared/detected capabilities** (e.g., vision, long-context, tool-calling — as metadata/badges) and its **cost** ("free" for local, "$/Mtok" for OpenRouter), so the user assigns with capability and spend in view.
   - These assignments are **absolute** — the system uses exactly what's assigned; nothing overrides them automatically. Present it as a clean matrix (role × assigned model + capability/cost metadata), not a busy form.
   - Writes flow through a mutation hook → data layer → the router config. (Capabilities shown here are **advisory metadata + fail-loud at runtime**; the UI does not predict or block — it informs the assignment.)

2. **Skills configuration.** A surface to **view, enable/disable, and configure skills** (reusable capability modules). **Scaffold the surface now** — list with enable toggles and a config affordance per skill — backed by fixtures; mark it clearly as **wiring-pending** (the skills subsystem lands in a later phase). It must look complete and intentional, not pretend to control things that aren't wired.

3. **MCP connections.** A surface to **add, view, and configure MCP server connections** (external tool servers) — name/URL/status per connection, an add affordance. **Scaffold now** against fixtures, marked **wiring-pending** (the MCP client populates in a later phase).

**Anti-patterns (automatic fail):** implying automatic routing exists; a model selector that hides cost; a busy/cluttered form instead of a clean matrix; controls that pretend to be wired when they're scaffolds (mark deferred surfaces honestly); `fetch` in a component.

**Done when:** the model matrix lets the user set a default primary + every per-role assignment with capability/cost visible, and assignments are absolute; skills and MCP surfaces are present, clean, and honestly marked wiring-pending; writes go through hooks → data layer; both themes deliberate. **Report** how the matrix reads, how cost/capability are surfaced per assignment, and how you marked the deferred (skills/MCP) surfaces.

---

## PROMPT 5 — History

> The conversation library, backed by the existing ownership-scoped data layer.

**PASTE:**

You are building the **History** view (the shell's `/history` route) — **only history.** Consume the design system and shell. Honor the Shared DNA (data-flow discipline).

**Build:**
- A **list of past conversations**, ownership-scoped (the backend exposes a list-conversations call that filters by owner; consume it through a query hook → data layer — never a direct call). Each row: the conversation's title/first-question, a timestamp, and a quiet affordance to **open** it (routes to the main surface with that conversation loaded) and to **delete** it (with a confirm — deletion is destructive).
- **Search/filter** over the list (client-side filtering of fetched conversations is fine to start).
- **Empty state** — a calm "no conversations yet" reflecting the philosophy, not a blank panel.
- **Loading and error states** — first-class (skeleton rows while loading; a legible error if the fetch fails).
- Quiet styling — hairline-separated rows, UI grotesk, chroma only on actionable affordances (open/delete).

**Anti-patterns (automatic fail):** a direct API/`fetch` call from a component; returning cross-owner conversations (the list must be owner-scoped); no confirm on delete; a blank/empty void instead of a calm empty state; loud row styling.

**Done when:** the owner-scoped conversation list renders with open + (confirmed) delete, search/filter works, empty/loading/error states are first-class, data flows through hooks → data layer, and both themes are deliberate. **Report** the list/row design and the empty-state treatment.

---

## Closing note (for you, not the agent)

This run takes the UI from its current ~50% to the full application frame: a real design system, a navigable shell with mode awareness, the refined answer surface with the manual-model control cluster, the settings matrix that makes the model story absolute and legible, and history. It deliberately **defers** (visibly, honestly) the Build/Agent surface, Deep Research view, and the live wiring of skills/MCP — those are present-but-dormant so the frame is complete and they slot in as their backends land.

The model story is fully settled across this run: **absolute manual selection (settings matrix + main-screen leader pill), cost-legible everywhere, no automatic routing, provider errors surfaced cleanly.** The dormant intelligent-routing code stays commented in the backend per the lobotomy patch — nothing in this UI run resurrects it.

Build them in order. After each, the calibration loop is the same one that's worked all along: look with your own eye (tests prove function, not feel), confirm the lane held and the data-flow discipline held, and bring me the agent's design decisions and any ambiguity flags so I can write a verbatim patch if the design language needs tightening.
