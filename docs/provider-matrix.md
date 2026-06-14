# Provider & model matrix

"What model should I run?" Disco is provider-neutral: it drives the agent loop
through an OpenAI-compatible LLM router, not a hardcoded vendor. This page is the honest
guidance on *which* model to point it at. For the *how* (the three self-host configs),
see [`self-host.md#models`](./self-host.md#models) — this page does not duplicate it.

> The strategic wedge of this project is **reliability on local / open-weight models**.
> The reliability work was engineered around the **24–32B class** (the roadmap calls it
> "engineered-for-27B"). Smaller models *run* the loop; they do not *match* the bigger
> ones. This page mirrors that honesty and does not claim otherwise.

## 1. The requirement

Any endpoint that satisfies all of:

- **OpenAI-compatible** (`/v1/chat/completions`). Each distinct backend is one provider
  entry with a `base_url`; the router talks to it through `OpenAIProvider`.
- **Reliable tool-calling.** The agent loop is tool-call-driven; a model that emits
  malformed or unreliable tool calls will stall the loop. `TOOL_CALLING` is a first-class
  capability requirement in the router (`Requirement.TOOL_CALLING`).
- For the **vision** path only: an endpoint serving a vision model (mmproj). Optional —
  guarded behind `Requirement.VISION` / `PMX_DRIVER_VISION=1`; unset routes vision work
  elsewhere or skips it.

The router assigns a model per **role** (`ModelRole`): `AGENT_DRIVER` (the loop's
tool-calling/planning brain), `RAG_ANSWERER` (grounded synthesis), `QUERY_REWRITER`,
`SUMMARIZER` (condensation), and `NLI_VERIFIER` (citation entailment — a cross-encoder,
not a chat model). One capable driver can serve every chat role; the cheaper roles
(rewriter/summarizer) are where a smaller second model earns its keep.

## 2. VRAM tiers (rough, honest)

The reliability target is the 24–32B class. Everything below it trades capability for
footprint — the trade is real, not marketing.

| Class | Verdict | Use it for |
|---|---|---|
| **~4B** (e.g. the bundled Qwen3-4B Q4) | **Works — demo-grade.** Proves the whole loop keyless on a CPU-only box. | Trying it out; short, simple loops. Not real agent work. |
| **~14B** | **Usable.** The lower end of "tolerable" for grounded answers + short tool loops. | Light Research; simple Build/Agent tasks. Expect degradation on long horizons. |
| **24–32B** (e.g. Qwen3 30–35B-A3B / a 27B dense) | **The reliability target the harness was engineered around.** | The recommended floor for real Build/Agent + Deep Research. |
| **Frontier via OpenRouter** | **Best — but not local.** | Hardest steps where local quality ceilings bite; keyed overflow. |

The basis-of-design states the capability floor directly: *"a robust agentic loop wants
the Llama-3.1/3.3-70B or Qwen3-30–35B class; sub-20B is viable only for short, simple
loops"* (basis-of-design §12), and for the driver: *quantize Q4_K_M / Q5_K_M, **never
below Q4***.

### Approximate VRAM by size × quant

**Rough order-of-magnitude only** — actual usage depends on quant variant, KV-cache
size, context length, and your runtime. Treat these as "will it roughly fit," not a spec.
Add headroom for the KV cache at long context (the driver runs large context windows).

| Model size | Q4 (~4.5 bpw) | Q5 (~5.5 bpw) | Q8 (~8.5 bpw) |
|---|---|---|---|
| ~4B | ~2.5–3 GB | ~3–3.5 GB | ~4.5–5 GB |
| ~14B | ~8–9 GB | ~10–11 GB | ~15–16 GB |
| ~27–32B | ~16–20 GB | ~20–24 GB | ~32–36 GB |

These are *weights-only* approximations; budget several more GB for KV at the 32K–128K
context the driver uses. (The self-host doc's footprint note puts the bundled 4B at
~2.6 GB on disk / ~5–6 GB resident with runtime overhead — consistent with the table.)
No measured throughput/accuracy numbers are asserted here; verify yours (see §5).

## 3. Pointing the app at each provider

All three are switched in `.env` and then owned by the Settings UI. Full instructions
live in [`self-host.md#models`](./self-host.md#models); in brief:

1. **Bundled (default).** `COMPOSE_PROFILES=bundled-llm` runs a llama.cpp server with
   `PMX_LLM_HF=unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M`. Keyless, CPU-only-friendly,
   demo-grade. The driver seed points at `http://llm:8080/v1`.
2. **Bring your own endpoint** (recommended for real work). Drop `bundled-llm` from
   `COMPOSE_PROFILES` and set `PMX_DRIVER_BASE_URL` to your OpenAI-compatible server —
   e.g. ollama or a native llama.cpp at `http://host.docker.internal:11434/v1`. Keep GPU
   serving (Vulkan/ROCm/CUDA) native on the host; the container just points at it. The
   `PMX_DRIVER_*` vars (`PMX_DRIVER_MODEL_ID`, `PMX_DRIVER_CTX`, optional
   `PMX_DRIVER_VISION=1`) seed the catalogue **only on first run** — after that the
   Settings → Models matrix owns it.
3. **OpenRouter.** Leave the bundled model off, paste an OpenRouter key in
   **Settings → Models → OpenRouter** (encrypted at rest, never in `.env`), and assign
   the roles. The app-server proxies the public OpenRouter model catalogue.

## 4. The honest caveat: smaller models degrade on the full agent loop

Every comparable OSS project either warns that non-frontier models "significantly
degrade" or carries issue threads of 14–32B models stalling the loop (release-roadmap
§"field-wide gaps"). Disco's whole bet is to push that floor down with a harness
engineered around local-model weaknesses — but it does not erase it:

- A **4B** runs the loop end-to-end and is genuinely useful as a keyless demo, but will
  spiral or stall on long, multi-step agent tasks.
- The **24–32B class** is where tool-calling + planning + multi-turn stability become
  "good enough" for real Build/Agent and Deep Research work — it is the target, not a
  guarantee for *every* model in that size.
- The **OpenRouter overflow** exists precisely because local quality ceilings bite on the
  hardest ~20% of steps (basis-of-design §"working thesis").

Do not read "reliable on local models" as "every local model is reliable." Read it as
"the system is engineered so a *good* 24–32B model can drive it."

## 5. Verify your own model

The repository does **not** ship measured per-model benchmark scores, and this page
deliberately invents none. A given model's reliability on *your* hardware and quant is an
empirical question — so confirm it directly:

```bash
docker compose exec agent-server python -m disco.agent_server.verify
# from a checkout: make verify   (add ARGS=--quick to skip live grounding)
```

`disco verify` runs a battery against *your* configured driver and prints a per-capability
PASS/FAIL/SKIP table:

- **config** — a driver resolves (assignment or `default_model`).
- **completion** — the endpoint is reachable and returns text (catches a wrong
  `base_url` / unserved model — with the provider's verbatim error).
- **tool-calling** — the model emits a structured tool call when asked. This is the gate:
  a model that can't tool-call cannot drive the loop, however fluent its prose.
- **grounding** — the full research pipeline returns a cited answer (SKIPs cleanly with no
  internet; this is a capability smoke test, not a faithfulness score).

It exits non-zero if any check fails. For a graded faithfulness *score* over a versioned
corpus, that's the eval harness (`make eval`), separate from this go/no-go check.

There is **no measured score in-repo** for any model — pick from the tier table above,
start at the 24–32B class for real work, and confirm yours with `disco verify`.
