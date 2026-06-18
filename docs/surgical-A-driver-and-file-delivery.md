# Surgical plan A — Driver/provider reliability + File-delivery

Branch: `build-surface-recovery-ux`. **Build-ready, no code in this doc — specs only.**
Every claim re-verified against current code (line numbers below are live as of this
writing; the older `docs/disco-driver-reliability-plan-6-18-26.md` matched). Honor the
four fitness gates (`uv run basedpyright`, `uv run lint-imports`,
`uv run python scripts/check_arch_budget.py`, `uv run python scripts/gen_arch_diagram.py --check`),
unit tests via `.venv/bin/python3 -m pytest -m "not integration"`, real-sample harnesses,
mandatory Firefox visual evidence for UI, and no class > 800 / func > 200 LOC.

---

# AREA 1 — Driver / provider reliability

## Grounding (re-verified)

- `packages/core/src/disco/core/llm/openai_provider.py`
  - `_payload` is **L251–328**. Body keys are hardcoded (`model`, `messages`,
    `temperature`, `stream`, optional `max_tokens`/`response_format`/`tools`/
    `chat_template_kwargs`/`stream_options`/`prompt_cache_key`). **No `provider` key,
    no extra-body passthrough.** The only provider-conditional shaping today is
    `_is_anthropic(model)` → `_mark_anthropic_cache` (L326–327).
  - `_raise_typed` is **L366–385**: context-overflow → `LLMContextWindowExceeded`;
    401/403 or `"auth" in err_type` → `LLMAuthError`; content_filter/451 →
    `LLMContentFiltered`; 429 or ≥500 → `LLMTransientError`; **everything else (incl.
    a Chutes "No cookie auth credentials found" 4xx, which has no `auth` err_type) →
    generic `LLMError`** (L385).
  - Provider identity available inside the instance: `self.name` (L181) and
    `self._base` (base_url, L180). `_is_small_assist_default` already keys on
    `"openrouter" in base_url` (`runtime_settings.py:155`) — reuse that idiom.
- `packages/core/src/disco/core/llm/types.py` `CompletionRequest` is **L90–128** —
  **no provider-prefs / extra-body field.** Frozen pydantic model.
- `packages/core/src/disco/core/llm/errors.py`: `LLMError` (L17) → `LLMTransientError`
  (L26) is the only back-off-eligible subclass; `LLMContextWindowExceeded`/`LLMAuthError`/
  `LLMContentFiltered`/`NoEligibleModel`/`BudgetExceeded` (L41–58).
- `packages/core/src/disco/core/loop/driver.py`
  - `_DRIVER_RETRY_BACKOFFS_S = (10.0, 30.0, 90.0)` at **L57**.
  - Retry loop **L322–462**. The request is built in `agent.step` (driver passes
    `attempt=attempts+1` at L349). `except LLMTransientError` (L422) backs off ≤3×
    then PAUSEs `driver-unavailable` (L440–446). `except LLMError as e` (L447) is the
    requery path: ≤2× appends a *"provider rejected … check tool names, JSON,
    parameters"* user hint (L453–460) and retries the **same** routing, then `raise`s
    (L462). It cannot change provider/routing.
- `packages/core/src/disco/core/loop/agent.py` `step` (**L83–146**) is where
  `CompletionRequest(...)` is constructed (**L117–140**). This is the single seam to
  thread `provider_prefs` into the request.
- `packages/agent-server/src/disco/agent_server/runtime.py`
  - `_router_now` is **L627–684**; it always `self._config_store.load()`s (L643) and
    ignores any in-process `config=` (the P4 footgun). A `pick` reassigns every
    generative role (L651–653).
  - `set_model_override` delegator at **L858–859** → `runtime_settings.py`
    `set_model_override` (**L61–67**): writes `self._rt._model_override[cid]` and
    `_save_overrides()` to the `_override_path` JSON sidecar. **There is no persisted
    "last pick"** — a new conv with no override falls to `RouterConfig.default_model`.
- `RouterConfig` (`config.py`): `default_model` **L211**; `model_for` precedence
  override > assignment > default at **L235–243**; overflow ladder DORMANT **L221–233**.
- Conversation-create paths that set the override:
  - `routes/conversations.py:45` `runtime.set_model_override(cid, body.model_override)`
    inside `create_conversation` (L31–64).
  - `routes/ws.py:212,220` reads `body.get("model_override")` and passes it to the
    create helper.
  - `routes/schedules.py:38` passes `model_override=body.model_override`.
  - `CreateConversationBody.model_override` at `routes/_common.py:83`.
- Frontend model pill: `ModelLeaderPill` (`value: string|null`, null → "use Settings
  default" = `assignments.default_model`, L19/34/36) and `build/BuildModelPicker`
  (`effectiveId = value ?? data?.default`, L27–28). Catalogue/assignments via
  `useModels`/`useAssignments` → app-server `GET /api/models`, `/api/models/assignments`
  (`app-server/routes/models.py:23,34`).

## OpenRouter `provider` body object — API confirmation (docs knowledge)

OpenRouter's chat-completions accepts a top-level **`provider`** object (Provider
Routing). Relevant keys, all correct as used here:
- `require_parameters: true` — only route to upstreams that support **all** request
  params we send (`tools`, `response_format`). This is what steers tool-calling **off**
  the free Chutes path (Chutes doesn't satisfy our tool-calling params under API-key
  auth → it's excluded, not rejected).
- `allow_fallbacks: true` (default true) — if the first upstream errors, try the next
  rather than failing the call.
- `ignore: ["Chutes", ...]` — hard-exclude named providers (string display names).
- `order: [...]`, `sort: "throughput"|"price"|"latency"`, `data_collection`,
  `quantizations` also exist; we use only the three above. The object is a top-level
  sibling of `model`/`messages`, so it merges cleanly into the existing body dict.

This is a real, current API. Non-OpenRouter OpenAI-compatible servers (llama.cpp, vLLM,
OpenAI) would **reject an unknown `provider` key** → the change MUST be gated on
OpenRouter.

---

## P1 · OpenRouter provider-preference injection

- **ID:** P1
- **Root cause / approach:** Free-pool OpenRouter routes tool-calls to Chutes, which
  rejects API-key auth. We have zero plumbing to express a routing preference. Add an
  optional, OpenRouter-gated `provider` block so OpenRouter only lands on upstreams that
  support our params and will fall back on error.
- **Files:line + change:**
  1. `types.py` `CompletionRequest` (after L123, before `request_id`): add
     `provider_prefs: dict[str, Any] | None = None` with a docstring (VOLATILE; merged
     into the OpenRouter `provider` body only; ignored by every non-OpenRouter adapter).
     `Any` is already imported (L14).
  2. `openai_provider.py` `_payload` (insert after the tools block, ~L294, before the
     `chat_template_kwargs` logic): a new private helper call. Add
     `OpenAIProvider._is_openrouter(self) -> bool` returning
     `"openrouter" in self._base.lower() or "openrouter" in self.name.lower()`. In
     `_payload`, when `self._is_openrouter()`:
     `body["provider"] = {"require_parameters": True, "allow_fallbacks": True, **(req.provider_prefs or {})}`.
     Caller-supplied `req.provider_prefs` wins on key collision (e.g. an escalation can
     set `ignore`). Local/OpenAI payloads are **byte-identical** (the branch never
     fires).
  3. `config.py` (optional, tunable-without-code): add
     `RouterConfig.openrouter_provider_prefs: dict[str, Any] | None = None` (near the
     budget fields ~L216). When set, `_router_now`/the router seeds
     `req.provider_prefs` defaults from it; the hardcoded
     `{require_parameters, allow_fallbacks}` in `_payload` remains the floor. **Decision
     below** — can ship P1 without this and add later.
- **New files:** none.
- **Tests** (`packages/core/tests/llm/`):
  - `_payload` emits `body["provider"]` **only** when base_url/name contains
    `openrouter`; absent for a localhost/llama.cpp provider (assert key not in body).
  - Default block equals `{"require_parameters": True, "allow_fallbacks": True}` when
    `provider_prefs` is None; a caller `provider_prefs={"ignore": ["Chutes"]}` merges
    (all three keys present).
  - **Byte-identical guard:** a non-OpenRouter `_payload(...)` is `==` to the
    pre-change payload for an identical request (golden dict).
- **Acceptance:** see P1/P2 shared live run below.
- **Risk:** LOW–MED. Additive body key, provider-gated. Only risk is a future non-OR
  base_url that contains the literal "openrouter" — acceptable; the gate is the same one
  already trusted in `runtime_settings.py:155`.
- **Dependencies:** none (foundation for P2).

## P2 · Classify + recover provider rejections (routing retry, not a model hint)

- **ID:** P2
- **Root cause / approach:** A provider-routing rejection is currently a generic
  `LLMError` → the driver blames the model ("check your JSON") and retries the same
  upstream → recurs → run ENDs in ERROR. Classify provider-availability rejections and,
  on retry, change **routing** (escalate `provider_prefs`) instead of nagging the model.
- **Error-classification logic** (`openai_provider.py` `_raise_typed`, insert a branch
  **before** the final `raise LLMError` at L385): treat as provider-level when the
  lowercased message OR err_type contains any of:
  `"no cookie auth"`, `"no allowed providers"`, `"no instances available"`,
  `"no endpoints found"`, `"provider returned error"`, `"requires moderation"` (the
  observed OpenRouter/Chutes phrasings), or err_type startswith `"provider"`. Raise a
  **new `LLMProviderUnavailable(LLMTransientError)`** (add in `errors.py` after L39).
  Subclassing `LLMTransientError` means it *inherits the back-off path for free* — but
  see the driver split below; we want a routing change, not a blind same-provider sleep.
- **Routing-retry behavior** (`driver.py`): add a dedicated
  `except LLMProviderUnavailable` **above** the `except LLMTransientError` (L422) so it
  is caught first (Python matches in order; the subclass MUST precede the base).
  Behavior, capped at the existing `requery`/attempts budget:
  - On first/second occurrence, re-issue the **same** request with **escalated
    `provider_prefs`** — start `{"allow_fallbacks": True}`, escalate to
    `{"allow_fallbacks": True, "ignore": ["Chutes"]}` on the second. This threads
    through `agent.step`: add a `provider_prefs: dict | None = None` kwarg to
    `agent.step` (agent.py:83) forwarded into `CompletionRequest(... provider_prefs=...)`
    (agent.py:117). The driver holds an escalation var alongside `attempts`.
  - Do **NOT** append the "check tool names / JSON / parameters" user hint (the model
    did nothing wrong). No transient_message is added on this path.
  - After the cap, fall through to the existing PAUSE (`driver-unavailable`) rather than
    a model-blaming ERROR — a provider outage is recoverable on resume.
- **Files:** `errors.py` (new exception), `openai_provider.py` (classification),
  `agent.py` (forward `provider_prefs` kwarg), `driver.py` (new except arm +
  escalation). Keep `driver.py`'s loop under its 200-LOC cap — the new arm is ~12 lines;
  if it pushes the function over, extract the escalation into a small
  `_escalated_provider_prefs(n: int) -> dict` module helper.
- **New files:** none.
- **Tests:**
  - Unit (provider): each trigger substring → `LLMProviderUnavailable`; a generic 4xx
    with an unrelated message still → plain `LLMError` (no over-capture).
  - Unit (driver, real loop with a fake provider that raises
    `LLMProviderUnavailable` once then succeeds): assert (a) the retry carries escalated
    `provider_prefs` (capture the second `CompletionRequest`), (b) **no** "check your
    JSON" hint was appended to messages, (c) the run completes. Mirror the existing
    `test_inspect_trace.py` real-loop pattern.
  - Regression: a plain `LLMError` still takes the L447 requery-with-hint path (existing
    behavior unchanged).
- **Acceptance (shared P1+P2, live, real):** drive a real Build end-to-end on the
  **free** default `or-gpt-oss-120b-free` via `verify_replan_acceptance.py` with
  `DISCO_CONFIG` pointed at the free default and **no paid `model_override`**. Assert the
  run reaches FINISHED with **zero** "No cookie auth"/provider ERROR events across N≥3
  runs (this is the exact failure that cost three runs on 2026-06-18). Capture the
  `DISCO_INSPECT` trace (`GET /api/debug/trace/{cid}`) showing the OpenRouter request
  carried the `provider` block. The four gates + `packages/core` +
  `packages/agent-server` suites green.
- **Risk:** MED — touches the driver retry loop. Mitigated by ordering the except arms
  correctly (subclass first) and the real-loop unit test.
- **Dependencies:** P1 (the `provider_prefs` field + `_is_openrouter` gate).

## P3 · Sticky last-picked model ("no hardcoded default")

- **ID:** P3
- **Root cause / approach:** A pick is stored per-conversation only; a new conversation
  falls to `RouterConfig.default_model`. Dylan's call: a new conversation should use
  **whatever the user picked last**. Persist a single global "last selected model" and
  seed new conversations from it; `default_model` becomes the fallback **only before any
  pick has ever been made**.
- **Persistence (where stored):** add to the `RuntimeSettings` sidecar family
  (`runtime_settings.py`) a new single-value store mirroring the existing JSON-sidecar
  pattern (`_load_overrides`/`_save_overrides` at L38–59):
  - New sidecar path `self._rt._last_model_path` declared on `ConversationRuntime`
    alongside `_override_path` (search `runtime.py:464` `_load_overrides()` init block
    and the path declarations near it — add the path next to `_override_path`).
  - `RuntimeSettings.get_last_selected_model() -> str | None` and
    `set_last_selected_model(model_id: str | None)` (atomic temp-file replace like the
    others). Stored as `{"owner": "<id>", "model": "<key>"}` or, simplest for the
    single-owner deployment, a bare `{"model": "<key>"}`. Per-owner keying is the
    forward-compatible shape (matches `DEFAULT_OWNER_ID`).
  - Add delegators on `ConversationRuntime` (the test-suite + routes call on the runtime,
    per the file's convention).
- **Write path:** extend `set_model_override` (runtime_settings.py:61) — when a non-None
  `model_id` is set for a conversation, **also** `set_last_selected_model(model_id)`.
  (The pick path is the create-time override; this captures every explicit pick.)
- **Seed path (the create routes):** when a conversation is created **without** an
  explicit `model_override`, seed it from `get_last_selected_model()`:
  - `routes/conversations.py:45` — `model = body.model_override or runtime.last_selected_model()`;
    `runtime.set_model_override(cid, model)`.
  - `routes/ws.py:212` — same `or` fallback before `model_override=...` at L220.
  - `routes/schedules.py:38` — same fallback. **Decision:** schedules may intentionally
    want a frozen model; seed only when the schedule's `model_override` is None (keep an
    explicit schedule pick authoritative).
  - Guard: only seed if the last-pick key still exists in `cfg.models` (fail-safe to
    `default_model`, mirroring `_router_now`'s "unknown keys ignored" at L651).
- **GET endpoint:** add `GET /api/models/last-selected` (app-server `routes/models.py`,
  near L23) returning `{"model": "<key>|null"}` — OR surface it inside the existing
  `/api/models/assignments` DTO as an added `last_selected` field (fewer round-trips for
  the pill). **Decision below.** Note the value lives in agent-server's runtime sidecar;
  app-server reads config, so either (a) app-server proxies agent-server, or (b) expose
  it from agent-server (`routes/models.py` there) and have the pill fetch it from the
  agent base. Recommend **(b)** — agent-server owns the runtime/sidecar; the pill already
  hits the agent base for builds.
- **Frontend:** `ModelLeaderPill` (L34) / `BuildModelPicker` (L27): change the
  "effective default" from `assignments.default_model` to
  `lastSelected ?? assignments.default_model`. Add a `useLastSelectedModel` query hook
  (`hooks/useModels.ts`) + `api/models.ts` fetch. The pill keeps `value=null` meaning
  "use the sticky/default"; the displayed label and the seeded build both reflect the
  last pick. Persist **server-side only** (no localStorage — consistent with the
  lifecycle work that killed the localStorage trap).
- **New files:** none (hook + api fn live in existing files).
- **Tests:**
  - Unit: `set_model_override(cid, "or-gemini-3-flash")` writes the last-selected
    sidecar; a fresh `RuntimeSettings.get_last_selected_model()` returns it (round-trip +
    restart-survives via re-load).
  - Unit/route: create a conversation with `model_override=None` after a prior pick →
    the new conv's effective override equals the last pick (assert via the
    `/state`/router resolution); with no prior pick ever → falls to `default_model`.
  - Unit: an unknown persisted last-pick key is ignored → `default_model`.
  - Frontend vitest: pill renders the last-selected label when the hook returns one,
    else the assignments default.
- **Acceptance (live + visual):** pick a non-default model on a Build conversation;
  create a **brand-new** conversation; confirm the model pill shows the last pick and the
  `DISCO_INSPECT` trace shows that model driving the new run. **Firefox screenshot** of
  the new-conversation pill showing the sticky pick + `SendUserFile`.
- **Risk:** LOW–MED — a persisted preference + seeding, no routing-engine change.
- **Dependencies:** independent of P1/P2 (composes cleanly — the sticky model is just
  whatever it is; P1/P2 make any OpenRouter choice reliable).

## P4 · `_router_now` config-injection footgun (cheap)

- **ID:** P4
- **Root cause:** `_router_now` (runtime.py:643) always reloads `disco-config.json`, so a
  `ConversationRuntime(config=...)` passed in-process is dead for routing — it cost real
  debugging time. (`_injected_router` at L641 IS honored; a `config=` is not.)
- **Change:** at the top of `_router_now`, if the runtime was constructed with an
  in-process `config=` that is **not** wired into `_config_store`, emit a **one-time**
  `logging.warning` ("an in-process config= was provided but routing reloads
  disco-config.json; use config_store= or DISCO_CONFIG"). Guard with a
  `self._warned_config_ignored` bool so it fires once. (Do NOT silently honor the
  injected config — the reload-per-request is load-bearing for live Settings edits.)
- **Files:** `runtime.py` (`__init__` flag + `_router_now` guard).
- **Tests:** unit — constructing with the ignored `config=` and calling `_router_now`
  twice logs the warning exactly once (caplog).
- **Acceptance:** unit + gates.
- **Risk:** LOW. Pure observability.
- **Dependencies:** none. **Build this first (1-liner-ish, de-risks the rest).**

## P5 · DOCX dark mode — OUT OF SCOPE here

Deferred (optional, non-goal unless Dylan asks). Not part of this plan.

---

# AREA 2 — File delivery (agent → user-downloadable file in the chat feed)

## Current state (re-verified) — what exists and where it breaks

**The backend per-file download route already exists and is correct:**
- `GET /conversations/{cid}/artifacts/{path}` — `routes/files.py:164–212`
  (`artifact_file`). Jails: (1) path must be in `_declared_artifacts` (event log), (2)
  extension in `_ARTIFACT_TYPES` (`_common.py:48–60`: xlsx/pptx/pdf/html/md/mp3/wav/png/
  jpg/jpeg/csv), (3) traversal-normalized + host-path resolve-jail. Serves from the live
  sandbox first, falling back to the host `ProjectStore` snapshot (so a FINISHED run with
  a reaped sandbox still serves). Always `Content-Disposition: attachment`.
- `_declared_artifacts` (`_common.py:125–162`) DOES include
  **`DeliverableEvent` with `artifact_kind=="files"` → `e.path`** (L160–161), plus
  sheet/slides/image/audio tool outputs. So the contract for "agent declares a file →
  it's reachable" is wired **at the route level**.

**The deliverable primitive exists:**
- `DeliverableEvent` — `events.py:559–592`. `artifact_kind: "app"|"files"`, `path`,
  `title`, `deployment_url`. Emitted by the `serve` tool via
  `turn_control.handle_serve` (`turn_control.py:726–780`): for `kind="files"` it appends
  a `DeliverableEvent(artifact_kind="files", path=...)`.

**Where it breaks (the EXACT gaps):**

1. **Build/Agent: the deliverable card downloads the WHOLE PROJECT ZIP, not the file.**
   `DeliverablePanel` (`build/DeliverablePanel.tsx`) for `kind!=="app"` calls
   `onDownload` → `BuildSurface.tsx:381` `download.mutate(b.cid)` →
   `useDownloadProject` (`hooks/useProjects.ts:34`) → `downloadProject(cid)`
   (`api/projects.ts:95`) → `GET /api/projects/{cid}/download` (a **zip of the entire
   workspace**, `routes/projects.py:63`). So an agent that hands off a single
   `report.pdf` gives the user a whole-project zip, and only if `ProjectStore`
   persistence is configured (else 404). The correct per-file route
   (`/conversations/{cid}/artifacts/{path}`) is **never called from the deliverable
   card** — the panel ignores `deliverable.path`.

2. **The deliverable card is not in the conversation feed and only shows on FINISHED.**
   `BuildSurface.tsx:371–372` renders `DeliverablePanel` pinned under the feed and only
   when `b.status === "FINISHED"` (WALK-09 gate). There is **no per-message "here is a
   file" card inline in the feed**, and nothing during a run.

3. **Research surface: blocks render with NO download (cid not threaded).**
   `ResearchSurface.tsx:121` renders `<AnswerDocument blocks=… />` **without a `cid`
   prop**. `AnswerDocument` (L25/38) forwards `cid` to `BlockView` → `SheetBlock`/
   `SlidesBlock`, whose download is gated `{cid && <SheetDownload …/>}`
   (`SheetBlock.tsx:54`, `SlidesBlock.tsx:83`). With `cid` undefined the block is a
   dead preview — no false affordance, but also **no download**. (This is the
   "honest-but-unreachable" state called out in the block docstrings.)

4. **Deep Research closing card: no file-delivery, no cid threaded to the report.**
   `DeepResearchSurface.tsx:354–360` renders `<DeepReportView … />` **without `cid`**,
   even though `r.cid` IS available on the surface (used by `NeedMoreCard` at L421–426).
   `DeepReportView` (`research/DeepReportView.tsx:196`, `Props` L20) has no `cid` and
   renders sections as prose via `SectionView`/`Markdown`/`CitedText` (no `BlockView`),
   so any block-type artifact a DR run produces has no download. DR's only export today
   is the **report→PDF/DOCX/MD** export modal (the File/FileText/FileType icons,
   DeepResearchSurface.tsx:28) — there is **no "agent emitted a file" handoff** on the DR
   surface at all. This is the prerequisite that blocks the DR-closing-card→agent-handoff
   product idea.

**Net:** Dylan is right. The plumbing to make a single agent-emitted file downloadable
exists at the route + event layer, but the **frontend never wires a per-file download
into the conversation feed** — Build downloads a whole-project zip, Research/DR don't
thread `cid` to the renderers, and there's no first-class "file card" in the feed.

## Work items

### F1 · Build/Agent: wire the deliverable card to the per-file artifact route

- **ID:** F1
- **Root cause / approach:** The deliverable card has `deliverable.path` and a real
  per-file route exists; stop downloading the whole-project zip for `kind="files"`.
- **Files:line + change:**
  - `frontend/src/lib/buildTrace.ts` `DeliverableView` (L453) + `deriveDeliverable`
    (L467–470): already carries `kind`/`path`/`deploymentUrl`; confirm `path` is exposed
    (add if missing).
  - `frontend/src/components/build/DeliverablePanel.tsx` (L30–33, L74–84): for
    `kind === "files"`, render the Download as an `<a href download>` pointing at
    `${agentHttpBase()}/conversations/${cid}/artifacts/${encodeURI(path)}` — reuse the
    EXACT `SheetDownload`/`SlidesDownload` anchor idiom (`ActivityFeed.tsx:75,117`), not a
    new fetch. Requires threading `cid` into the panel (currently only `onDownload`).
  - `frontend/src/components/BuildSurface.tsx:381`: pass `cid` to the panel; keep
    `onExportManifest`/`download` (whole-project zip) as a **secondary** action ("Download
    all / .zip"), not the primary file action.
  - Backend: confirm the file extension is in `_ARTIFACT_TYPES` — if a deliverable
    `path` is a **directory** (allowed by the event doc), the per-file route 404s. **Add
    a directory case**: when `DeliverableEvent.artifact_kind=="files"` and `path` is a
    dir, the panel falls back to the project-zip download (or a future per-dir zip route).
    Spec: detect dir vs file by extension presence; prefer files for the first-class
    card.
- **New files:** none.
- **Tests:** vitest — `DeliverablePanel` with `kind="files"` + `cid` renders an `<a
  download href=…/artifacts/<path>>`; with a directory path falls back to the zip
  action; `kind="app"` unchanged (opens preview). Backend test already covers
  `artifact_file` serving a DeliverableEvent-declared path — add one asserting a
  `serve(kind="files", path="report.pdf")` makes `report.pdf` reachable and a non-declared
  sibling 404s.
- **Acceptance (live + visual):** run a real Build where the agent writes a file and
  `serve(kind="files", path=…)`s it; on FINISHED, click Download on the card → the single
  file downloads (not a zip). **Firefox screenshot** of the card + the downloaded file +
  `SendUserFile`.
- **Risk:** LOW. Reuses the proven route + anchor component.
- **Dependencies:** none.

### F2 · First-class "agent emitted a file" card **inline in the conversation feed**

- **ID:** F2
- **Root cause / approach:** A handoff should appear **in the feed as it happens**, not
  only as a bottom panel on FINISHED. Render a download card as a feed item whenever a
  `DeliverableEvent(kind="files")` (or a sheet/slides/image/audio declared artifact)
  lands.
- **Contract:** no new event needed — `DeliverableEvent` already carries
  `title`+`path`+`artifact_kind`. `buildTrace.ts` already turns `deliverable` events into
  feed entries (L312, L348). Extend the feed item for a `kind="files"` deliverable to
  carry `{title, path}` and render a `FileDownload` card (a generalization of
  `SheetDownload`/`SlidesDownload` that takes an arbitrary `{filename,title}` and the
  `cid`).
- **Files:line + change:**
  - `frontend/src/lib/buildTrace.ts:348` (`deliverable` branch): emit an `ActivityItem`
    with an `expandable.file = {filename: path, title}` for `artifact_kind==="files"`.
  - `frontend/src/components/build/ActivityFeed.tsx`: add an exported `FileDownload`
    component (sibling of `SheetDownload`/`SlidesDownload`, L67/109) — same `<a download
    href=…/artifacts/{encodeURI(filename)}>` shape, generic icon (lucide `File`). Render
    it in the feed-item switch (near L339–346) when `item.expandable.file` is present and
    a `conversationId` is held.
  - Keep the bottom `DeliverablePanel` (F1) as the FINISHED capstone; F2 is the live,
    per-event card. (No duplication: the panel is the "final result" summary; feed cards
    are the running handoffs.)
- **New files:** none (extend ActivityFeed).
- **Tests:** vitest — a `deliverable` event with `kind="files"` produces a feed item with
  a download anchor to the artifacts route; `kind="app"` does not (still the open-preview
  affordance). buildTrace unit test for the new `expandable.file` derivation.
- **Acceptance (live + visual):** Build run; as the agent `serve`s a file mid-run, a
  download card appears **in the feed**; clicking downloads the file. **Firefox
  screenshot** + `SendUserFile`.
- **Risk:** LOW–MED. New feed affordance; keep it strictly cid-gated (no false
  affordance).
- **Dependencies:** F1 (the `FileDownload` anchor idiom).

### F3 · Thread `cid` into the Research + Deep-Research report renderers

- **ID:** F3
- **Root cause / approach:** Block downloads are correct but unreachable on
  Research/DR because `cid` isn't threaded. Thread it so any sheet/slides/image artifact
  in a research or DR answer is downloadable — and so DR can carry a file handoff.
- **Files:line + change:**
  - `frontend/src/components/ResearchSurface.tsx:121`: pass `cid={r.cid}` to
    `<AnswerDocument>` (confirm the research hook exposes a conversation id; if the
    single-pass research stream lacks a stable cid, surface it from the WS/conv create —
    **open decision** noted below).
  - `frontend/src/components/research/DeepReportView.tsx`: add `cid?: string|null` to
    `Props` (L20) and thread it into `SectionView` → wherever blocks render. Today
    `SectionView` renders prose only; if DR begins emitting block artifacts (image-gen,
    sheets), `BlockView` must receive `cid`. Minimum change: accept+forward `cid`.
  - `frontend/src/components/research/DeepResearchSurface.tsx:354`: pass `cid={r.cid}`
    (already available, used at L425) to `<DeepReportView>`.
- **New files:** none.
- **Tests:** vitest — `AnswerDocument`/`DeepReportView` with a `cid` render the block
  download anchor; without `cid` render the honest no-download preview (existing
  behavior). Snapshot the DR report with a block artifact + cid.
- **Acceptance (live + visual):** a research/DR run that produces a downloadable block →
  the in-block download appears and works. **Firefox screenshot** + `SendUserFile`.
- **Risk:** LOW. Pure prop threading; the download component is already proven.
- **Dependencies:** none (independent of F1/F2; shares the route).

### F4 · Deep-Research closing-card file handoff (the DR→agent-handoff prerequisite)

- **ID:** F4
- **Root cause / approach:** DR has no "here is a file" handoff. Add a first-class
  deliverable/download to the DR closing card so a DR run (or a DR→agent handoff) can
  hand the user a file (report artifact, generated asset).
- **Contract:** reuse `DeliverableEvent(kind="files")` + the per-file artifacts route +
  the F2 `FileDownload` card. DR already emits a `ReportEvent`; for any file the DR loop
  produces, emit a `DeliverableEvent(kind="files", path=…)` (server-side, in the DR
  finish path — locate the DR report-finalize in `retrieval`/the deep-research loop;
  emit alongside `ReportEvent`). Then `_declared_artifacts` makes it reachable
  automatically.
- **Files:line + change:**
  - Backend: emit `DeliverableEvent(artifact_kind="files", …)` where the DR loop
    finalizes a file artifact (the DR report-assembly/finish path — grep the
    deep-research loop for `ReportEvent` emission; co-locate the deliverable emission).
  - Frontend: in `DeepResearchSurface.tsx` near the NeedMoreCard block (L421+), render
    the F2 `FileDownload` card(s) for `kind="files"` deliverables on the DR conversation
    (derive from the DR events, cid = `r.cid`).
- **New files:** none.
- **Tests:** unit (backend) — a DR run that produces a file emits a
  `DeliverableEvent(kind="files")` and the artifacts route serves it. vitest — the DR
  closing area renders a `FileDownload` for it.
- **Acceptance (live + visual):** a DR run that yields a file → a download card on the DR
  closing card → clicking downloads it. **Firefox screenshot** + `SendUserFile`.
- **Risk:** MED — touches the DR finalize path; gate the emission so it only fires for
  real file artifacts (no empty handoffs, mirroring `handle_serve`'s no-op guards at
  turn_control.py:752–774).
- **Dependencies:** F2 (the `FileDownload` card), F3 (cid on DR).

---

# Ordered build sequence

1. **P4** — `_router_now` warning (de-risk debugging, ~1 fn).
2. **P1** — OpenRouter `provider_prefs` field + gated `_payload` injection.
3. **P2** — `LLMProviderUnavailable` classification + driver routing-retry; **live
   acceptance on `:free`** with no paid override (the headline proof).
4. **P3** — sticky last-picked model (settings store + create-path seeding + GET + pill).
5. **F1** — deliverable card → per-file artifacts route (Build).
6. **F2** — inline file-download card in the conversation feed.
7. **F3** — thread `cid` into Research + DR report renderers.
8. **F4** — DR closing-card file handoff (depends on F2+F3).

Driver P1+P2 are the core engineering win; P3 the UX Dylan asked for. File-delivery F1
unblocks the most with the least code (route already exists); F2–F4 build the
first-class "agent hands you a file in chat" that gates image-gen / DR-handoff / podcast.

# Open decisions

- **P1 config field:** ship the hardcoded `{require_parameters, allow_fallbacks}` floor
  now, or add `RouterConfig.openrouter_provider_prefs` for code-free tuning? (Recommend:
  hardcoded floor + the field as a later, additive follow-up.)
- **P3 GET surface:** new `GET /api/models/last-selected` on **agent-server** (owns the
  sidecar) vs adding `last_selected` to the app-server assignments DTO (one fewer
  round-trip but cross-server). Recommend agent-server.
- **P3 schedules seeding:** seed scheduled-conversation model from the last pick, or keep
  a schedule's model frozen at creation? (Recommend: only seed when the schedule has no
  explicit `model_override`.)
- **F1 directory deliverables:** `DeliverableEvent(kind="files")` `path` may be a
  directory. First-class card targets single files; dir handoffs fall back to the
  project zip — or add a per-directory zip route? (Recommend: file-first now, dir-zip
  later.)
- **F3 research cid:** confirm the single-pass Research stream exposes a stable
  conversation id to thread; if not, surface it from the conv-create/WS handshake.
