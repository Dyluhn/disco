# disclaude campaign — status write-up (2026-06-29)

Autonomous, heartbeat-driven execution of the Parallel Harness Addendum. Every PR runs the
binding loop: **plan → Codex (gpt-5.5) read-only plan review → revise to APPROVE →
implement → tests → Codex code review → revise to APPROVE → commit/push → heartbeat ledger.**
Branch `disclaude/experimental-…`; bare remote `~/projects/disclaude.git`.

## 1. Position

| Phase | State |
|---|---|
| Compliance repair pass (P7 accept, heartbeat audit, P1A/P1B reclassification) | ✅ done |
| **P8** Semantic Direct Manipulation (A semantic-refs · B data-disco metadata · C frontend resolver · D edit oracles) | ✅ COMPLETE |
| **P9** TweakSpec & Owner Controls (A schema · B `.disco/tweaks.json` IO · C typed `app_set_tweak`+seed · D TweakPanel UI) | ✅ COMPLETE |
| **P1B-LIVE-1** product-evidence TS bridge + `_SLICE_FIELDS` hardening | ✅ accepted |
| **P1B-LIVE-2** scenario_runner + dossier writer | ✅ accepted |
| **P1B-LIVE-3a** MiniMax relay → repo + unit test | ✅ accepted |
| **P1B-LIVE-3b** the live build run | ⚠️ **in progress — driver proven, build paused (see §5)** |
| P10 → P17 | gated behind P1B-LIVE green |

**12 PRs Codex-gated and pushed this session**, plus the live stack bring-up. The MiniMax
driver is proven to build real software (§4); Product Harness is **not** marked complete
because no run has yet reached a clean `FINISHED` + passing `classify_dossier` with UI
evidence (§5).

## 2. Snags Codex declined (REVISE) and the fixes

Codex caught real defects on almost every PR. The high-value ones:

### P8A — Semantic reference model
- **Plan REVISE ×2:** loose `dict` locators → typed discriminated-union locators; ambiguity
  modeled as a *target kind* → modeled as a *resolution outcome*; unpinned `context` → a
  typed `SemanticReferenceContext`.
- **Code REVISE ×2:** (1) incomplete `ReferenceResolution` invariant; slides/sections indexed
  by *tuple position* instead of explicit ordinal; cross-tier ambiguity masking; loose
  negative-ordinal regex; `IndexedLocator(index=-1)` constructible. (2) **gpt-5.5 ran the code**
  and found `target_id` collided across kinds (`section:notes` vs `comment_anchor:notes` →
  same string), so dedup collapsed two distinct targets → resolved instead of AMBIGUOUS. Fix:
  kind-qualified identity.
- Earlier live bug: `normalize_screen_label` turns `"slide 5"`→`"slide-5"`, and the negative
  regex matched the separator `-5` as a "negative". Fixed by detecting negatives on the raw
  phrase.

### P8D — Targeted/manual-edit oracles
- **Code REVISE:** a present-but-non-dict evidence slice (`{"targeted_edit": []}`) looked
  *absent* → SKIP → a malformed run could classify PASS. Plus `str()`/bool coercion masked
  wrong-typed fields (`isinstance(True, int)` is `True`). Fix: explicit `ABSENT` vs `MALFORMED`
  sentinels + strict no-coercion type guards.

### P9A — TweakSpec schema
- **Code REVISE:** `affects` paths weren't lexically validated (`affects=(" ",)` grounded a
  tweak); TEXT/COLOR counted *duplicate* affects toward the "≥2 fields" rule; a `set` input
  for palette colors bypassed canonicalization (pydantic coerces `set`→`tuple` *after* the
  before-validator). Also pydantic coerced `min=False`→`0.0`, so bool bounds slipped the type
  check. Fixes: validate+dedupe affects, reject non-list/tuple palette input, reject bool
  bounds in the before-validator.

### P9C — typed `app_set_tweak`
- **Plan REVISE:** making the tool require a TweakSpec would create a **governed-but-
  unauthorable false affordance** (nothing wrote `.disco/tweaks.json`). Fix folded into the
  same PR: `app_create` now **seeds** a real default TweakSpec, so the tool is usable
  end-to-end. Plus `no_app` precedence and migrating the two untyped tests.

### P9D — TweakPanel UI
- **Code REVISE:** `isMalformed()` checked only `=== undefined`, blind to runtime `null`
  numeric bounds → `<input value={null}>`. Fix: `notFiniteNum` (rejects null/undefined/NaN/
  Infinity). Plus the vitest asserted against `SLICE_KEYS` itself (circular).

### P1B-LIVE-1 — product-evidence bridge
- **Plan REVISE ×3:** the schema + oracle contract *already existed* in P1A — a new schema
  would be a forbidden duplicate. Reframed to the missing **TS evidence bridge**, and gpt-5.5
  found a **latent P1A bug**: `_SLICE_FIELDS["export"]` declared only `requested` while the
  oracle adjudicates `download_present`/`download_bytes`, so a `bool download_bytes` evaded
  validation. Hardened (build-and-harden).
- **Code REVISE:** `buildProductEvidence` iterated `SLICE_KEYS` while parity guarded only
  `SLICE_FIELDS` (two sources) → a dropped slice key would silently lose captured evidence.
  Fix: `SLICE_FIELDS` is the typed single source, `SLICE_KEYS` derived; test asserts against
  the *input* not the constant under test.

### P1B-LIVE-2 — scenario_runner + dossier
- **Plan REVISE:** my plan claimed proofs the SKIP-by-default oracles wouldn't give (provider
  enforcement is opt-in; absent slices SKIP). Fix: scenarios materialize the full classifier
  contract + required-slice enforcement (missing → INVALID_RUN, not PASS-via-SKIP).
- **Code REVISE:** an artifact named `product-evidence.json` could overwrite the strict-
  validated core file, and an artifact *label* `events` collided with the manifest's core
  label → the classifier would read the artifact *as* the event log (defeating the hash
  lock). Fix: namespace artifacts under `artifacts/` path + `artifact:` label prefix.

### P1B-LIVE-3a — MiniMax relay
- **Code REVISE:** `create_app` imported deps *before* the key check (fail-fast →
  ModuleNotFoundError); the package `__init__` eagerly imported `disco`, so the "dep-light"
  claim was false (proven by a subprocess test with only the repo root on `PYTHONPATH`); SSE
  returned `200` regardless of the upstream status. All fixed.

**Recurring theme:** the same defect class kept reappearing in new costumes — *two sources of
truth that can drift*, and *guards that test `=== undefined` but not runtime `null`*, and
*claimed invariants with no test that fails when the claim is false*. gpt-5.5 twice caught
bugs only by **executing** the code, not reading it.

## 3. Deviations from the plan (all flagged + gate-approved)

| Plan said | Did instead | Why |
|---|---|---|
| `tools/appkit/semantic_metadata.py` (P8B) | `core/appkit/semantic_metadata.py` | the renderer is in core; core can't import tools — single source must live in core |
| `harness/oracles/` (P8D) | `harness/build_soak/oracles/` | that's the real zero-opinion oracle layer (OracleResult + failure_codes) |
| `tools/appkit/tweaks_io.py` (P9B) | `tools/builtin/tweaks_io.py` | there is no `disco.tools.appkit` package; next to its consumers |
| new `product_evidence_schema.py` (P1B-LIVE-1) | reuse the existing P1A `product_evidence.py` + a TS bridge | schema already existed; avoid duplication |
| `e2e-live/support/productEvidence.ts` | `src/lib/harness/productEvidence.ts` | vitest **excludes** `e2e-live/**`; needed a discoverable home |
| "check for an OpenRouter build driver" | **MiniMax** via the existing `minimax_relay.py` | Dylan directive; matches the P17 constraint |

## 4. What's proven (live, with evidence)

- **Full stack live:** relay `:8080` (200) + agent-server `:8000` (`/health` ok, model =
  `driver-minimax`) + **podman 5.8.2** with the `disco-sandbox:base` image.
- **MiniMax-M3 builds real software:** an autonomous build produced **"The Corner Cup"**
  coffee-shop site — `index.html` (4341 B, real semantic HTML) + `styles.css` (6114 B) +
  `preview_start` + `verify_web_app`. Rendered via Playwright/Firefox; **screenshot delivered
  to Dylan**. Evidence in `test-record/p1blive3b/`.
- **Direct MiniMax, zero OpenRouter (P17-clean):** `relay.jsonl` shows every call →
  `host=api.minimaxi.chat, model=MiniMax-M3`. The provider-ledger is sourced from the relay
  so it shows the *real upstream host*, not the localhost relay.

## 5. Issues that surfaced (and their disposition)

1. **MiniMax-M3 malforms `update_plan_progress`** — sends `steps:['']` (list of empty strings)
   instead of `[{index, state}]` → 5 validation rejections → the no-progress breaker **paused**
   the build *after* the deliverable existed. ⇒ no clean `FINISHED` terminal ⇒ `classify_dossier`
   can't pass ⇒ **Product Harness NOT complete.** This is the next gated PR: repair/ignore a
   malformed `steps` arg and don't let plan-progress validation spam pause a build that already
   has a deliverable. (Open.)
2. **Relay FastAPI 422 (live-caught):** `from __future__ import annotations` stringized the
   proxy's `request: Request` (lazily imported) → FastAPI treated it as a query param. Removed
   the future-import. **Fixed + committed.**
3. **Latent P1A `_SLICE_FIELDS` gap:** export download fields weren't type-validated. **Fixed.**
4. **MiniMax endpoint `.com` 401 trap:** the coding-plan token 401s on `api.minimaxi.com` but
   works on `api.minimaxi.chat` / `api.minimax.io` (OpenAI-compat). Saved to memory.
5. **GPU memory leak (earlier in the session):** orphaned amdgpu VRAM/GTT after a ROCm hang;
   cleared on the desktop recovery. Saved to memory.
6. **Codex reviews time out near 460 s:** mitigated with `timeout 560` + tighter prompts.
7. **Local llama-server + Pi down** the whole session — the original reason the build driver
   was blocked; resolved by switching to MiniMax via the relay.

## 6. Next

The gated build-loop robustness PR (issue #1) → re-run to a clean `FINISHED` → the
UI/Playwright product-harness (`build-artifact-runtime-smoke.spec.ts`) → `classify_dossier`
PASS + PreviewPane screenshots → only then is Product Harness complete and P10 unlocked.
P17's soak stays reserved for **MiniMax-M3 via the direct MiniMax API only**.
