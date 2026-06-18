# Driver / provider reliability + loose ends — plan (2026-06-18)

The remaining open items surfaced during today's runthrough + fix work. The big
one is the OpenRouter free-pool driver flakiness (it cost three acceptance runs
and would read as a hang to a user); the rest are small hardening notes.

## §0 — What we surfaced today that is NOT yet fixed

1. **OpenRouter free-pool driver flakiness (HIGH).** Default driver
   `or-gpt-oss-120b-free` (`openai/gpt-oss-120b:free`). OpenRouter's free pool
   routes tool-calling requests to the **Chutes** provider, which rejects API-key
   auth with *"No cookie auth credentials found."* To a user this looks like a
   hang/error on Build/Agent/Deep-Research.
2. **The requery hint is misleading (MED).** A provider-level rejection is
   currently surfaced to the model as *"the provider rejected your request — check
   tool names / JSON / parameters and try again"* — blaming the model for a
   routing problem it didn't cause, and retrying the **same** provider.
3. **`_router_now` silently ignores a passed `config=` (LOW, footgun).** It always
   `self._config_store.load()`s from `disco-config.json`, so an in-process
   `ConversationRuntime(config=...)` is dead for routing — cost real debugging time.
4. **DOCX/MD dark mode (LOW, optional).** The new Light/Dark export toggle is
   PDF-only; DOCX uses a light `reference.docx`.

## §1 — Root cause (grounded, with cites)

- **No provider-routing plumbing exists.** `OpenAIProvider._payload`
  (`openai_provider.py:273-328`) hardcodes the body keys; there is **no**
  `body["provider"]`, no `allow_fallbacks`, no extra-body passthrough.
  `CompletionRequest` (`types.py:90-128`) has **no** field to carry provider prefs.
- **Wrong error class → wrong recovery.** `_raise_typed`
  (`openai_provider.py:366-385`) maps a 4xx that isn't 401/403/429/5xx to a generic
  `LLMError`. Chutes' *"No cookie auth credentials found"* is a 4xx with no `auth`
  err-type → generic `LLMError`.
- **The requery can't change routing.** `driver.py:447-462` catches `LLMError`,
  requeries ≤2× by appending a *user hint* to the messages, then `raise`s. It
  re-issues the **same** model/provider — so a provider-level failure recurs and
  the run ENDs in ERROR. (`LLMTransientError` is the only path that backs off, and
  it PAUSEs "driver-unavailable" after 3 tries — `_DRIVER_RETRY_BACKOFFS_S =
  (10,30,90)`.)
- **No free→paid failover to lean on.** The intelligent-routing overflow ladder is
  **dormant** since v1.2 (`config.py:221-243`); `model_for()` returns a single key,
  no escalation. The only live fallback is vision escalation (one hard-coded model).
- **Catalogue has reliable alternatives.** `disco-config.json`: paid OpenRouter
  entries already exist — `or-deepseek-deepseek-v4-flash` ($0.20/M out),
  `or-gemini-3-flash` ($0.30/M), `driver-overflow` = claude-3.5-sonnet ($15/M) — and
  local `driver-local` (Qwen 27B, $0). Paid `openai/gpt-oss-120b` (non-`:free`)
  tool-calls cleanly (verified today, provider WandB).

## §2 — Fix design

Two independent layers + one config decision. **P1+P2 are the robust,
model-agnostic engineering fix** (make ANY OpenRouter model survive provider
roulette). **P3 is the product/cost decision** (what the default driver should be).
P4/P5 are cheap loose ends.

### P1 — OpenRouter provider-preference injection (engineering, HIGH value)
Give every OpenRouter request a sane `provider` routing preference so it stops
landing on a cookie-only/param-incompatible backend.
- **`types.py`**: add `CompletionRequest.provider_prefs: dict[str, Any] | None = None`.
- **`openai_provider._payload`**: when the target is OpenRouter (provider name /
  base_url contains `openrouter`), merge a provider block into the body:
  `body["provider"] = {"require_parameters": True, "allow_fallbacks": True, **(req.provider_prefs or {})}`.
  - `require_parameters: true` → OpenRouter only routes to providers that support
    the params we send (tools/response_format) — this alone steers OFF the free
    Chutes path for tool-calling. `allow_fallbacks: true` → if one provider 4xxs,
    OpenRouter tries the next instead of failing.
  - Source the default block from config (a new optional
    `RouterConfig.openrouter_provider_prefs` or per-entry field) so it's tunable
    without code — default `{require_parameters, allow_fallbacks}`.
- **Gating:** only for OpenRouter entries; local/llama.cpp/OpenAI payloads stay
  byte-identical (they'd reject an unknown `provider` key).
- **Files:** `types.py`, `openai_provider.py`, `config.py` (+ the entry/RouterConfig
  field). **Risk:** LOW-MED (additive body key, gated on provider).

### P2 — Classify + recover provider rejections correctly (MED value)
- **`openai_provider._raise_typed`**: detect a provider-routing/availability
  rejection (e.g. message contains "no cookie auth", "no allowed providers",
  "no instances available", or err_type ~ provider) and raise a NEW
  `LLMProviderUnavailable` (subclass of `LLMTransientError` so it inherits the
  back-off path) — OR keep `LLMError` but special-case in the driver.
- **`driver.py` requery path**: when the rejection is provider-level, do NOT append
  the "check your tool names/JSON" hint (the model did nothing wrong). Instead
  **re-issue the same request with an escalated `provider_prefs`** (e.g. force
  `allow_fallbacks: true`, or `ignore: ["Chutes"]`) — a routing retry, not a
  model-blaming retry. Keep the ≤2 cap.
- **Files:** `errors.py` (new exception, optional), `openai_provider.py`,
  `driver.py`. **Risk:** MED (touches the driver retry loop — unit-test the
  classification + that a provider rejection triggers a routing retry, not a hint).

### P3 — "No default — use whatever I picked last" (sticky model pick)
**Dylan's call (2026-06-18): there should be no hardcoded default driver; a new
conversation should use whatever model the user picked last.** Today a pick is
stored PER-CONVERSATION only (`runtime.set_model_override(cid, key)` →
`conversations.py:45`); a new conversation falls back to `RouterConfig.default_model`
(`or-gpt-oss-120b-free`). There is no persisted "last pick".

Design:
- **Persist a global "last selected model"** in the settings store (a single
  catalogue key, per owner). Update it whenever the user picks a model (extend
  `set_model_override` / the pick path to also write `last_selected_model`).
- **Seed new conversations from it.** When a conversation is created WITHOUT an
  explicit `model_override` (`conversations.py` / `ws.py:212` / `schedules.py`),
  seed `model_override` from `last_selected_model`; `default_model` becomes the
  fallback ONLY before any pick has ever been made (fresh install).
- **Frontend:** the model pill defaults to `last_selected_model` (fetch it with the
  catalogue), so the UI shows the sticky choice. (Persist server-side, NOT
  localStorage — consistent with the lifecycle work that killed the localStorage
  trap.)
- This makes the "default" emergent (= the last pick) instead of a config constant,
  and it composes cleanly with P1/P2 (the chosen model is whatever it is; P1/P2
  just make any OpenRouter choice reliable).
- **Files:** settings store (new `last_selected_model`), `runtime.set_model_override`,
  the conversation-create paths, frontend model-pill default + a GET for the value.
  **Risk:** LOW-MED (a persisted preference + seeding; no routing-engine change).

### P4 — `_router_now` config footgun (LOW, cheap)
When `ConversationRuntime(config=...)` is passed but `_router_now` will reload from
disk anyway, emit a one-time WARNING ("an in-process config= was provided but
routing reloads disco-config.json; use config_store= or DISCO_CONFIG"). Or honor
the injected config for routing outside tests. **Files:** `runtime.py`. **Risk:** LOW.

### P5 — DOCX dark mode (OPTIONAL)
If wanted: generate a dark `reference.docx` variant and pick it on `mode=dark`.
Default non-goal unless Dylan asks — most people read DOCX in their editor's theme.

## §3 — Acceptance (live, real)
- **P1/P2 headline:** drive a real Build on `or-gpt-oss-120b-free` end-to-end (the
  exact thing that ERRORed today) and assert it completes with **zero** "No cookie
  auth"/provider ERRORs across N runs — reuse `verify_replan_acceptance.py` but
  point `DISCO_CONFIG` at the *free* default (no paid override) to prove the
  hardening, not a workaround.
- Unit: `_payload` emits `provider` only for OpenRouter; provider rejection →
  routing retry (not a model hint); local payload unchanged.
- Gates: the four fitness gates + agent-server/core suites.

## §4 — Build order
P4 (1-liner) → P1 (provider prefs) → P2 (classification + routing retry) → live
acceptance on `:free` → P3 (sticky last-picked model) → P5 only if asked.
P1+P2 are the core engineering win; P3 is the UX Dylan asked for; the rest are small.

## §5 — Already fixed today (not part of this plan)
- **Dark PDF "center rectangle on white" (`8c99650`).** Surfaced 2026-06-18 after
  the dark-mode export shipped: WeasyPrint only painted the body background inside
  the page content box, leaving 1in white margins (and the margin header/footer
  rendered light-on-white). Fixed by `@page{ background:var(--bg) }` — full-bleed in
  both modes, verified with full-page screenshots (corners + center match) + a
  regression test. (My miss: I had pixel-sampled the center, not the margins.)
