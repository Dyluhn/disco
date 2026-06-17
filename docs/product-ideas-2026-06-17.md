# Product directions — captured 2026-06-17 (Dylan)

Raw product thinking from Dylan, saved verbatim-in-intent for later planning. Not
yet scheduled. Several items share a **hard prerequisite** (file delivery), called
out first.

---

## 0. CROSS-CUTTING PREREQUISITE — the agent must be able to hand users files via chat

Dylan: *"I don't think the agent can actually hand users files thru the chat.
That would be kinda necessary for a lot of these things. Like you couldn't even
download slides if we did figure out how to make them."*

- This blocks #1 (images), #2 (agent-made slides), #4 (podcast video) — every
  idea below that produces a deliverable the user must receive.
- Today there IS an in-block download (`SheetBlock`/`SlidesBlock`) and a build
  `DeliverablePanel` + artifact route, but the D12 audit found the in-block
  download has **no production caller** (cid never threaded on the research
  surface). So "agent emits a file → user downloads it from the conversation"
  is not reliably wired end-to-end.
- **Action:** make a first-class "agent delivers a file into the conversation
  feed, user clicks to download" primitive that works on Build/Agent AND on the
  Deep Research closing card. This is the gating dependency — do it first.

---

## 1. Image generation as a real agent + build TOOL (+ Settings CRUD, local & paid)

Today `image_generate` is a procedural-PIL placeholder (geometric patterns).
Dylan wants it to be a **real tool the agent and build surfaces call**:
- **Build:** website backgrounds, hero images, asset generation.
- **Agent:** images for slides, or whatever the task needs.

Requirements:
- The diffusion model is **addable in Settings, behind the provider-key CRUD we
  just built** (the `ProviderKeysSection` + the provider-config pattern: pick a
  provider, set base_url / api_key_env / model; the key lives encrypted).
- Must accept **both local and paid** options.

### Researched provider landscape (the "look it up" — starting point, verify before building)
The cleanest design mirrors the existing 3-tier (bundled / self-host / paid),
adding an **image provider category**:

- **Bundled (current):** procedural PIL — keep as the keyless placeholder, but
  RELABEL it honestly in the tool description ("procedural, not photorealistic").
- **Self-host (local):**
  - **ComfyUI** — de-facto local standard, HTTP API (`/prompt` + websocket), runs
    SDXL/FLUX/SD3 on a local GPU. Most flexible; API is graph-based (needs an
    adapter, not OpenAI-shaped).
  - **Automatic1111 / Forge (SD WebUI)** — `POST /sdapi/v1/txt2img`, simpler REST.
  - **`diffusers` in-process** — the repo already has a `DiffusersBackend` slot
    left empty by design; needs torch + weights + a GPU. Heaviest to bundle.
  - **LocalAI** — exposes an **OpenAI-images-compatible** `/v1/images/generations`,
    so it can reuse the paid adapter below (a nice unification).
- **Paid:**
  - **OpenAI Images** (`gpt-image-1`, DALL·E 3) — `POST /v1/images/generations`.
  - **Stability AI** (Stable Image / SD3) — own REST API.
  - **Replicate** — unified API hosting FLUX/SDXL/etc.
  - **fal.ai** — fast hosted diffusion (FLUX), API.
  - **Together AI** — FLUX endpoints.
  - **Black Forest Labs (FLUX)** — direct or via fal/replicate.

**Design lever:** the OpenAI `/v1/images/generations` shape is the best
"compatible boundary" — it covers paid OpenAI AND LocalAI (and others that mimic
it), so one adapter handles a lot; add a **ComfyUI adapter** for the dominant
local path. So: `bundled | openai-images-compatible | comfyui` covers most of the
space, each configured via the Settings provider CRUD with an encrypted key.

---

## 2. Deep Research closing card → hand off to AGENT mode (with a pre-approved plan)

When a Deep Research report is done, the bottom card (currently MD/PDF/DOCX export
+ audio + follow-up) should also offer **agent-mode actions** on the report:
"Make slides," and the other tools relevant to a report.

- For **slides**: the action auto-feeds the agent a **PRE-APPROVED plan** with the
  Deep Research report attached as context. The plan is broken into the concrete
  agent steps to **make and deliver the PowerPoint** (outline → deck build via the
  slides tool → render → deliver the file to the user).
- Generalize: any closing-card action = "seed a Build/Agent conversation with the
  report as context + a pre-approved, task-specific plan, skip the plan-approval
  gate (it's pre-approved), run to a delivered artifact."
- Depends on #0 (file delivery) for the user to actually get the deck.

---

## 3. NEW "Iterative mode" toggle in the chat box (use the NLI/claim-accuracy data)

A separate toggler (next to scope/think) for **iterative refinement**, with a
dropdown for **up to 5 iterations**. The insight: we already produce **honest
per-claim NLI verdicts** (supported / weak / unsupported) — that's untapped signal
for *targeted* improvement.

Mechanism (Dylan's framing):
- It is NOT another full Deep Research. The **weak parts** of the report become a
  **focused plan**.
- The claims that aren't fully supported get **iterated on specifically**: pull
  fresh web searches for the weak-sourced items; the main agent iterates on the
  **weak sections only, plus the conclusion and the header**.
- Open design problem Dylan flagged: **how to do this without making the report
  feel choppy or contradictory** when only some sections are rewritten. (Candidate
  approaches to explore: re-synthesize the weak sections in-place against new
  sources, then a light coherence pass over conclusion+intro only; preserve the
  strong sections verbatim; track which sentences changed.)
- This is the highest-novelty idea — it turns the grounding verdicts from a
  *display* feature into a *control signal* for self-improvement.

---

## 4. Audio export → a REAL two-host podcast (NotebookLM-style), as an agent task

Dylan suspects the current audio overview (if it even works end-to-end) does NOT
produce a NotebookLM-style two-host conversational podcast.
- Make it an **agent task**: an option on the report closing card where the agent
  produces an actual podcast (potentially a podcast *video*).
- This is bigger than the current `report_audio` two-voice TTS — it's a scripted,
  multi-turn, produced piece. Likely: agent writes a real dialogue script →
  multi-voice TTS → mix → (optional) simple video track → deliver the file.
- Depends on #0 (file delivery).
- First: verify whether the existing two-voice `report_audio` pipeline actually
  works live (it's RP-09, flagged as live-acceptance-open).

---

## 5. Benchmarks before launch (REQUIREMENT)

Dylan: *"we need to have benchmarks before we launch."*
- A real benchmark/eval suite is a **launch gate**, not optional.
- There's an existing `eval-as-a-feature` / `disco verify` surface to build on,
  but a proper benchmark set (research grounding accuracy, agent task success,
  latency, cost) is needed before v0.1.

---

## Dependency summary
- **#0 file delivery** gates #1, #2, #4.
- **#2, #4** are "seed an agent conversation with a pre-approved plan + report
  context" — same primitive, two payloads.
- **#3** stands alone and is the most novel (NLI verdicts → targeted re-research).
- **#1** rides the Settings provider-key CRUD already shipped.
- **#5** is a launch gate.
