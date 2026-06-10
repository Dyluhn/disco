# BP-00 bench record — mmproj on the 27B driver

## Phase V1 — feasibility (2026-06-10, no serving changes)

**Result: PASS — exact-match projector exists in the EXACT source repo.**

- Serving model: `/var/home/dylan/models/Qwen3.6-27B-UD-Q5_K_XL.gguf`, sourced from
  `unsloth/Qwen3.6-27B-MTP-GGUF` (per `~/run-llama-server-27b.sh` header: "MTP head
  is in the unsloth Qwen3.6-27B-MTP-GGUF repo this file came from").
- That repo ships three projectors (HF tree API, 2026-06-10):

  | file | LFS sha256 | size |
  |---|---|---|
  | mmproj-BF16.gguf | `05353347512982ee62317b9d8c89372bc815f4b4043580e7ef3ad411ec1a1cd3` | 931,146,304 |
  | **mmproj-F16.gguf** | `eacf610d1ee4bd5ed0197a0777dd8f4fceb8eefa27009067c7d496cb68fbde45` | 927,607,360 |
  | mmproj-F32.gguf | `fdc443e974cad1f61c45af1cfd5580855855ddce0d6c14cc500a5714c486ac1d` | 1,842,940,480 |

- Picked **F16** (Vulkan backend: BF16 path is second-class on RDNA4; F32 doubles
  VRAM for no measured quality gain on projectors).
- Downloaded to `/var/home/dylan/models/mmproj-Qwen3.6-27B-F16.gguf`; local
  `sha256sum` = `eacf610d1ee4bd5ed0197a0777dd8f4fceb8eefa27009067c7d496cb68fbde45`
  — **matches the repo LFS oid bit-for-bit**.
- Identical mmproj set also present in `unsloth/Qwen3.6-27B-GGUF` (same oids) —
  projector is shared across the MTP and plain repos, confirming it targets the
  Qwen3.6-27B base, not a different size/revision.

## Phase V2 — bench gate (2026-06-10)

Profiles: `~/run-llama-server-27b.sh` (baseline) vs `~/run-llama-server-27b-vision.sh`
(identical + `--mmproj /var/home/dylan/models/mmproj-Qwen3.6-27B-F16.gguf`).
Switch mechanism extended: `switch-model.sh 27b-vision`; text-only revert stays one
command away (`switch-model.sh 27b`). Bench = `bench_v2.py` (this dir): live
llama-server `/completion`, 6-prompt standard mix (code ×2, prose, JSON tool-call,
plan, ~3K-token long-ctx), 256-tok gens, temp 0, 2 rounds; warm-round (r2) numbers
are the comparison; all stats from the server's own `timings` (incl.
`draft_n`/`draft_n_accepted`), VRAM/GTT from host sysfs (card1, R9700).

### Same-day baseline vs vision profile (warm round)

| profile | tg t/s | draft accept | pp t/s (mean) | peak VRAM | peak GTT |
|---|---|---|---|---|---|
| baseline (27b) | 50.6 | 68.6% | 171.9 | 26,387 MiB (80.9%) | 1,824 MiB |
| vision (27b + mmproj F16) | 50.3 | 68.2% | 130.1 | 27,513 MiB (84.3%) | 1,746 MiB |

Raw JSON: `bench-baseline.json` / `bench-vision.json`.

### Criteria

| # | criterion | result |
|---|---|---|
| a | `/props` → `"vision": true`, server healthy after `switch-model.sh 27b-vision` | **PASS** |
| b | MTP intact: tg −0.6% (50.3 vs 50.6), accept −0.4pt (68.2 vs 68.6) — identical per-prompt accept rates; long-ctx prefill 850.9 vs 878.3 t/s (−3.1%). All ≪ 10% bar. (Small-prompt pp deltas are fixed-overhead noise on ~40-token prefills.) | **PASS** |
| c | 128K ctx (KV preallocated at load) + 14,851-token prompt + 1 image: peak 27,515 MiB VRAM (84.3%), GTT flat ~1.77 GiB, no OOM, no GTT swap; correct fused answer | **PASS** |
| d | image turn end-to-end (`image_turn_v2.py`): real 1280×800 BP-04-daemon screenshot, model read header **"PMX Status Dashboard"** + all three card labels/numbers (42, 668, 50 t/s) exactly; MTP active during image-conditioned decode (49.3 t/s, 66% accept) | **PASS** |
| e | 30-min mixed-traffic soak (`soak_v2.py`: tool-JSON / image / code / long-ctx / image+ctx cycled; InvocationID watched per request): **215/215 OK over 1804s, zero failures, zero restarts** (InvocationID stable), peak VRAM 27,518 MiB byte-flat, GTT 1,769 MiB flat; per-workload means 51.1–55.4 t/s with MTP accept intact throughout | **PASS** |

### Soak log tail (criterion e, full log: `soak.log`, verdict: `soak.json`)

```
[1364s] req163 code       55.3 t/s accept=350/447 vram=27515 gtt=1769 OK
[1376s] req164 long-ctx   51.7 t/s accept=416/544 vram=27515 gtt=1769 OK
[1390s] req165 image+ctx  51.7 t/s accept=482/650 vram=27515 gtt=1769 OK
[1393s] req166 tool-json  54.3 t/s accept=105/135 vram=27515 gtt=1769 OK
[1396s] req167 image      51.1 t/s accept=115/165 vram=27515 gtt=1769 OK
...
SOAK PASS  (215/215, duration 1804s, invocation_unchanged=true,
            peak vram 27518 MiB, peak gtt 1769 MiB)
```

**V2 verdict: ALL FIVE CRITERIA PASS — the mmproj-enabled profile is cleared for
serving. V3 (driver vision wiring) may proceed.**

### V2 findings worth keeping

- **mmproj VRAM cost: +1,126 MiB** at load (F16 projector ≈ 0.93 GB file + buffers).
  Headroom at 128K: ~5.1 GiB free — comfortably below the ~95% kwin ring-reset zone.
- **Image embeddings prompt-cache**: a repeated screenshot turn showed
  `cache_n=1036, prompt_n=4` — agent loops re-sending the same screenshot don't
  re-pay the ~1.9s mmproj encode (~513–560 t/s effective image-prefill when cold).
- **Honesty probe (accidental)**: a glyph-less screenshot (local Playwright-Chromium
  on Fedora Atomic renders NO text — host quirk; `pmx-sandbox:base` on VM-201 has
  proper Debian fonts and is the real rendering path) was described by the model
  exactly as blank ("no numbers visible") — no hallucinated content. The gate
  screenshot was therefore generated by the daemon INSIDE pmx-sandbox:base under
  gVisor (`bp00-shot-remote.sh` pattern, exec-pipes because `docker cp` into a
  running runsc container lags the gofer view).
- Qwen3.6 templates default to thinking: vision answers need `max_tokens` ≥ ~700 or
  the budget is consumed by `reasoning_content` (content arrives empty at 200).
