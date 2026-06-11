# BP-00 — Vision: mmproj on the workstation driver (DECIDED by Dylan, 2026-06-09)

**Read `README.md` first. Requires BP-04 (screenshots exist). Supersedes the earlier
judge-role draft of this order — the judge path is now the documented FALLBACK only.**

## The decision (verbatim intent)

Dylan, 2026-06-09: *"add the mmproj to qwen 27b and adjust context and whatever else is
needed if necessary… you'll need to ensure the mmproj works with my current mtp model
and quant."* So: the driver itself (Qwen3.6-27B-UD-Q5_K_XL, llama.cpp at
192.168.1.231:18080, MTP serving, 128K ctx) becomes vision-capable. Screenshots from the
BP-04 browser go straight into the driver's context — true Manus parity, no out-of-band
judge.

Known facts: `/props` currently reports `"modalities":{"vision":false}` (no mmproj
loaded); the chat template already contains the image macros (shared with the VL
variants) — the projector is the missing piece. The workstation's serving recipe and
the switch-model profile mechanism are in `~/switch-model.sh` and the memory notes
("llama.cpp R9700 recipe", "Workstation 27B → Q5"). Serving changes must be reversible:
add a vision profile variant; never destroy the current working profile.

## Phase V1 — feasibility (no serving changes)

1. Find the official mmproj GGUF matching the EXACT base model (Unsloth's repo for the
   UD quants first, then lmstudio/community mirrors). It must be for Qwen3.6-27B's VL
   projector — a projector for a different base size/revision is a hard NO, not a
   "probably fine".
2. Verify provenance (repo, sha256) and download to the workstation's models dir.
3. If no matching projector exists anywhere: STOP. Report what exists, fall back is
   Dylan's call (the judge-role design from the superseded draft remains in git history).

## Phase V2 — the bench gate (the hard part; ALL criteria must pass)

Run on the workstation against a vision profile (`--mmproj <file>` added to the current
serving flags). Reference baseline first: re-measure the CURRENT profile the same day
(~51 t/s under MTP per the recipe notes — measure, don't assume).

| # | criterion | pass bar |
|---|---|---|
| a | server starts; `/props` → `"vision": true` | hard |
| b | MTP intact: accept-rate and t/s on the standard text bench prompts | within 10% of same-day baseline |
| c | VRAM at full 128K ctx with 1 image in context | no OOM, no swap-to-GTT collapse |
| d | image turn works end-to-end | a real 1280×800 BP-04 screenshot, sensible answer about visible content |
| e | **serving soak**: 30 min mixed traffic (tool-calls + images interleaved, scripted) | zero crashes/restarts |

Criterion (e) is non-negotiable and exists because of the documented -150mV lesson:
passing llama-bench is NOT evidence of MTP-serving stability. If (c) fails: step ctx
down 128K → 96K → 64K, re-run (b)+(c) at each step, and STOP with the tradeoff table —
the ctx reduction is Dylan's call to ratify, per his "adjust context if necessary".
If (b) or (e) fail at every viable ctx: STOP, report numbers, fallback decision returns
to Dylan.

Record everything in `test-record/bp-00/bench.md` (commands, VRAM readings, t/s tables,
soak log tail).

## Phase V3 — wiring (only after V2 passes)

1. **Core types**: `LLMMessage` (`core/events.py`) gains
   `images: list[str] | None = None` (data-URL base64). Serialization round-trips
   (extend `test_serialization.py` conventions). `openai_provider.py`: emit OpenAI
   content-parts when `images` set; error clearly if the target model lacks
   `Requirement.VISION`.
2. **Config**: the driver ModelEntry (`config.py`, driver-local) gains
   `Requirement.VISION` — gated on env `PMX_DRIVER_VISION=1` so the code ships before
   the workstation profile flips, and a non-vision driver deployment never advertises
   what it can't do.
3. **Browser observations carry the image**: when the driver has VISION, the BP-04
   `browser` observation's LLM rendering attaches the screenshot as an image part.
   **Context-cost rule (exact)**: only the MOST RECENT screenshot in the event sequence
   renders as an image; every older browser observation renders its screenshot as the
   path text only (BP-06's masking philosophy applied to images — without this, image
   KV compounds and 128K dies). Implement in the View projection next to the BP-06
   masking rules; same stability tests apply (one image→text flip per new screenshot is
   the allowed boundary change).
4. **No `vision_check` tool.** The driver sees directly. Do not build the judge tool;
   delete references to it from the prompt plans. BP-05's gate stays console-based
   (vision is additive judgment by the driver itself). BP-15 (feed thumbnails) unchanged.
5. **Prompt bullet** (BP-03 section, rendered only when the driver has VISION — follow
   the `skills_block` conditional pattern):

```
"  • You can SEE your latest browser screenshot. After navigating, look at it: "
"check layout, styling, and that the page is not blank or broken before moving on.\n"
```

## Acceptance

1. **Unit**: content-parts golden JSON; VISION gating; latest-image-only view rule
   (fingerprint stability per BP-06's invariant test pattern).
2. **Bench evidence**: the full V2 table + soak log in `test-record/bp-00/` — pasted
   into the report, not summarized.
3. **Behavioral (live driver, NOW vision-enabled)**: a build with a deliberate visual
   defect seeded (white text on white background via the task prompt's starter CSS);
   event log shows the agent identifying the defect FROM THE SCREENSSHOT (its thought
   references the visual, not console — console is clean in this scenario) and fixing
   it. Save → `test-record/bp-00/`.
4. **UI surface (live, Firefox)** — `bp-00-vision.spec.ts`: the step-3 run; feed shows
   screenshot thumbnail (BP-15) and the subsequent fixing edit; final Preview is
   readable. Screenshots: `defect-before.png`, `fixed-after.png` →
   `test-record/screenshots/bp-00/`, sent to user.

## Prohibitions

- No serving-profile changes that aren't reversible via the existing switch-model
  mechanism; the text-only profile must remain one command away.
- No judge-role/OpenRouter wiring. No mmproj from a mismatched base model.
- Bench numbers are measured same-day, both profiles, same prompts — no cached
  baselines, no llama-bench-only evidence for stability claims.
- If ANY V2 criterion fails, the failure report goes to Dylan before a single line of
  V3 is written.
