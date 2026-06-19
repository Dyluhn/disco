# Surgical plan — Cluster D: polish / search / attach / live

Status: BUILD-READY SPEC (no code changed by this doc). Author: autonomous session
2026-06-18. Scope: the five "polish / search / attach / live" items —
F1 (keep-searching), G1/DR-4 (initial-box attach), DR mid-run steer/inject,
RP-09 live-audio acceptance, noVNC live browser (#53–57).

Sources mined + **verified against current code** (every `file:line` below was
re-read this session): `docs/remaining-items-consolidated-2026-06-15.md`,
`docs/dylans-walkthru-6-17-26.md`, `docs/disco-direction-and-decisions-6-17-26.md`
§11 (DR-3 recency DONE, DR-4 attach), `docs/design-novnc-live-browser.md`.

Conventions (from CLAUDE.md): four fitness gates (`scripts/check_arch_budget.py`,
`uv run lint-imports`, `gen_arch_diagram.py --check`, `uv run basedpyright`);
unit tests via `.venv/bin/python3 -m pytest -m "not integration"`; **visual
evidence mandatory** for any UI change (Firefox/Playwright screenshot of it
working in the real app + `SendUserFile`); real-sample harnesses only.

> CAUTION on line numbers: captured 2026-06-18. Re-locate the symbol (Serena
> `find_symbol`) and confirm behaviour before editing — the symbol + behaviour is
> authoritative, the line is a hint.

---

## D1 — F1: keep-searching on no-answer (basic research)

**ID:** F1 · roadmap §11.6 / issue #1.

**Current status (verified):** NOT STARTED. The basic-research answer pipeline is
`stream_research_answer` in
`packages/retrieval/src/disco/retrieval/streaming.py:340-442`. It is strictly
linear: discovery (`:363`) → extraction with a single wider-net retry on empty
(`:371-377`) → rerank (`:392`) → constrained streamed generation (`:397-415`) →
structure+verify (`:420-421`). There is **no reformulate-and-re-search** anywhere.
If generation returns prose with **zero supported claims**, the pipeline still
emits a `final` frame and finishes — the "up but not grounding" failure. The only
existing retries are inside extraction (`:374`) and the per-query rewriter lives in
the *engine* path (`engine.py:54-60`, `RouterQueryRewriter.rewrite` at
`ranking.py:104-111`) — `stream_research_answer` does **not** take or use a
rewriter today (`research_stream` wires it at
`deep_research_service.py:214-230` without one).

**Root / approach:** Add a bounded outer loop around the
discover→extract→rerank→generate→verify body, keyed on the **no-answer signal**.
The signal is computed *after* `_verify_claims`: `supported = sum(1 for c in claims
if c["verdict"] == "supported")`. If `supported == 0` **and** we have rounds left,
reformulate the query with `RouterQueryRewriter`, re-run discovery+extraction with
the new query (skipping URLs already extracted), and regenerate. **OFF-path
guarantee:** when round 0 already yields ≥1 supported claim, the function takes the
exact bytes/frames it does today — the retry block is unreachable, so the
non-empty case is byte-identical.

**The no-answer signal (precise):** `supported == 0` after `_verify_claims`
(`streaming.py:421`). Do NOT key on `len(claims)==0` alone — an answer of all
`unsupported`/`neutral` claims is equally "no grounded answer" and must also
retry. (`unsupported_count` already exists at `:430`.)

**Files:line — the change:**
- `packages/retrieval/src/disco/retrieval/streaming.py`
  - **Signature:** add `rewriter: QueryRewriter | None = None` and
    `max_research_rounds: int = 1` to `stream_research_answer` (`:340-354`).
    `max_research_rounds=1` (no extra rounds) keeps every existing caller/test
    byte-identical until the wiring below opts in; bound is **≤2** total reformulate
    rounds (so cap the param at 2 server-side).
  - **Refactor (surgical, not a rewrite):** extract the existing body steps 1–5
    (`:362-421`, discovery→verify) into a private `async def _answer_once(query,
    *, exclude_urls: frozenset[str], ...) -> dict | _Empty` that returns the
    assembled `answer` dict + the working `by_id`/`top`/`all_hits`/`claims` OR an
    error sentinel. The current `:362-436` becomes: loop `for round in range(1 +
    bounded_rounds)`, call `_answer_once`, compute `supported`; if `supported>0` or
    `rewriter is None` or last round → break and emit exactly the existing `final`
    + `state:finished` frames (`:435-436`). Keep all token frames streaming on the
    FINAL round only (don't stream a doomed first draft to the user — buffer round
    N's tokens, emit them only when that round is accepted; on a retried round emit
    a lightweight `{"type":"phase","phase":"reformulating"}` progress frame so the
    UI shows life, mirroring DR's phase events).
  - **Reformulation:** between rounds call `new_qs = await
    rewriter.rewrite(query, n=1)`; use `new_qs[0]` if non-empty else keep `query`.
    Pass `exclude_urls = {h.url for h in all_hits_so_far}` into discovery's
    candidate set so round 2 fetches *different* sources (dedupe against round-1
    URLs before extraction).
- `packages/agent-server/src/disco/agent_server/deep_research_service.py:214-230`
  (`research_stream`): pass `rewriter=RouterQueryRewriter(self._rt._router_now())`
  and `max_research_rounds=2` into `stream_research_answer`. (`RouterQueryRewriter`
  is already imported at `:428` for DR; reuse it.)

**Reformulation prompt:** reuse the EXISTING `QUERY_REWRITER` role prompt that
`RouterQueryRewriter.rewrite` already issues (`ranking.py:104-111`) — do NOT invent
a new prompt; n=1 single reformulation is exactly its contract. (If a no-answer
*hint* helps, optionally prepend the original query with "Previous search found no
usable sources; rephrase for different sources:" only on round≥1 — keep this OFF
unless a live run shows the bare rewrite under-diversifies.)

**New files:** none.

**Tests** (`packages/retrieval/tests/`, new `test_streaming_keep_searching.py`):
1. round-0 has ≥1 supported claim → `rewriter.rewrite` is **never called**;
   frame sequence is byte-identical to a no-rewriter run (golden-compare the
   emitted frame list). *(OFF-path parity guard.)*
2. round-0 verifies 0 supported claims, round-1 (after reformulate) verifies ≥1 →
   `rewrite` called exactly once; final answer is round-1's; a `reformulating`
   phase frame was emitted; round-0 tokens were NOT emitted.
3. both rounds verify 0 supported → emits the round-1 `final` (best-effort) and
   stops; `rewrite` called ≤2 times total (cap honoured, no loop).
4. `max_research_rounds=1` (default) → never reformulates regardless of signal
   (proves the param gates it; protects all existing callers/tests).

**Acceptance:** non-UI (streaming logic). Prove via a real `/ws/research` run
against a deliberately-obscure query that 0-grounds on the first pass and
self-recovers on reformulation; capture the frame log. (No screenshot required —
the frontend already renders `phase`/`final` frames.)

**Risk:** LOW-MED. The only real risk is accidentally changing the non-empty path;
the test-1 golden-frame parity guard + the `max_research_rounds=1` default
neutralise it. Cost bound: ≤2× search/extract on the *rare* no-answer path only.

**Dependencies:** none (independent; schedule alongside DR-3 per §11.1). Touches
only `streaming.py` + one wiring line — no layering/diagram impact.

---

## D2 — G1 / DR-4: attach files in the INITIAL box

**ID:** G1 / DR-4 F1+F2+F3 · roadmap §11.5 · WALK/G1.

**Current status (verified):** PARTIAL. `UploadComposer` exists and works
*mid-conversation on the Build/Agent surface only*:
- Component: `frontend/src/components/build/BuildSurface.tsx:18` (a paperclip +
  drag-drop that POSTs to `uploadFiles(cid, files)`); the button is **disabled when
  `cid` is null** (`:92`, `:25` early-returns when `!cid`).
- Mounted once, gated on a live cid:
  `frontend/src/components/BuildSurface.tsx:415` —
  `{b.cid && b.status !== "ERROR" && <UploadComposer cid={b.cid} />}`. It is in the
  *running* surface, not the empty/initial state.
- Upload route: `packages/agent-server/src/disco/agent_server/routes/files.py:40`
  `upload_files` → `runtime.upload_session(cid)` (`:62`) → `session.write_file(
  "uploads/<name>", data)` (`:121`) → `runtime.store_upload` (`:122`) → emits ONE
  `DatasourceEvent` (`:138-152`). Uploads land in the sandbox `uploads/` dir, NOT
  the vector store — they are **files the agent can read**, not retrieval Passages.
- cid creation is on-submit: Build/Agent POST `/conversations` only when the user
  sends (`agent.ts:55-66` `createBuildConversation`; `useBuild.ts:35-44` sets the
  cid in the create `onSuccess`). DR creates its cid on submit too
  (`useDeepResearch.ts:79`). **Basic research is STATELESS — no cid at all**: it
  streams over `/ws/research` (`ws.py:185`, `runtime.research_stream` →
  `deep_research_service.py:199`), sending one query frame
  (`frontend/src/api/research.ts:105-123`).
- Corpus/Passage plumbing already exists: `Passage.corpus_id`
  (`retrieval/models.py:36`), `RetrievalRequest.corpus_ids`
  (`models.py:55`), `DefaultRetrievalEngine._corpus_passages`
  (`engine.py:81-89`, embeds the query, queries each namespace), and
  `VectorStore.ingest(corpus_id, *, owner_id, docs)`
  (`vectorstore.py:42,82-90`). `CreateConversationBody` (`_common.py:74-91`)
  already carries `recency_window` (the DR-3 precedent for a submit-time field).

So three gaps remain, exactly the §11.5 F1/F2/F3 split:

**F1 — render UploadComposer in the empty/initial state (needs a cid first).**
- **Root:** the empty state has no cid, and `UploadComposer` is null-gated. Two
  options; choose **pre-create-on-mount** (lower-risk than threading a pending
  session through three surfaces):
  - On surface mount of Build/Agent/DR, eagerly POST `/conversations` (reuse
    `createBuildConversation` / the DR create) to obtain a cid *before* the first
    message, store it as the session's `cid`, and render `<UploadComposer
    cid={cid} />` in the initial QueryInput cluster. Submit then **kicks the
    existing cid** instead of creating a new one (Build's `useBuild` already
    supports a resume-style "cid already set" path at `:28-29`; reuse it so submit
    becomes kick-not-create when a pre-created cid exists).
  - Guard against orphan conversations: a pre-created-but-never-submitted cid must
    be reaped. Mark it `pending` (no USER message) and let the existing
    idle-suspend / empty-conversation cleanup drop it (there is already an
    idle-suspend path — `test_idle_suspend.py`). Add a test that an abandoned
    pre-created cid carries no events and is not listed in history.
- **Files:**
  - `frontend/src/components/ResearchSurface.tsx:55-79` (empty-state cluster),
    `frontend/src/components/research/DeepResearchSurface.tsx:115-140`
    (DR initial box, already hosts `RecencySelector` at `:132`), and the
    Build/Agent empty state (`components/BuildSurface.tsx`, the QueryInput cluster
    above `:415`). Add `<UploadComposer cid={preCreatedCid} />` next to each
    initial `QueryInput`.
  - `frontend/src/hooks/useBuild.ts`, `useDeepResearch.ts`, and the research hook
    (`useResearch.ts` / `useResearchStream.ts`): add an eager pre-create effect +
    a "submit kicks the existing cid" branch.
- **Basic-research wrinkle (must flag):** `/ws/research` has **no cid**, so its
  uploads have nowhere to live and F3 below cannot reuse the sandbox `uploads/`
  path. For research, pre-create a lightweight conversation cid the same way and
  pass it on the WS query frame (add `conversation_id` + `corpus_ids` to the
  `/ws/research` open frame at `research.ts:113-122` and to `research_stream`'s
  signature at `deep_research_service.py:199-208`). This is the larger sub-change;
  if descoping, ship F1+F2 for Build/Agent/DR first and gate research-attach behind
  F3.

**F2 — uploaded files → Passages (corpus_id) → cited in the DR report.**
- **Root:** uploads today are sandbox files, not retrievable Passages. Ingest them
  into the vector store under a per-conversation corpus and thread that corpus into
  the run's `RetrievalRequest.corpus_ids`.
- **Files:**
  - `routes/files.py:121-152`: after `session.write_file`/`store_upload`, for the
    **v1 text types only** (`.txt`/`.md`/`.csv` — see scope), parse the bytes into
    `ExtractedDoc`/`Passage`s (chunk on paragraphs/rows) and call
    `vector_store.ingest(corpus_id=f"upload:{cid}", owner_id=<owner>, docs=[...])`.
    Set `corpus_id` on each Passage (the field already exists, `models.py:36`).
    Keep the existing single `DatasourceEvent` emit (`:138`).
  - Thread the corpus into retrieval: DR's `DeepResearchRun` already holds a
    `vector_store` + `_namespace = conversation_id`
    (`deep_research/engine.py:113,117`) and gather legs run `RetrievalRequest`s.
    Add the upload corpus id to each leg's `RetrievalRequest.corpus_ids` so
    `_corpus_passages` (`engine.py:81-89`) folds uploaded passages into the
    candidate set alongside web hits (`engine.py:99-100`). The uploaded passages
    then rank + cite through the identical path as web passages (no separate
    citation code).
  - `_common.py` `CreateConversationBody`: no new field needed if the corpus id is
    derived as `upload:{cid}`; the run discovers it because uploads were ingested
    under that deterministic namespace. (Alternatively add `corpus_ids: list[str]`
    to the body, mirroring `recency_window`.)
- **PDF note (v1-only):** PDF extraction is explicitly OUT of v1 (§11.5 "no
  PDF/binary parsing in v1"). The upload route must **reject** non-text types for
  ingestion (still allow them as sandbox files for Build/Agent, but do NOT ingest →
  they won't be cited). Surface a clear toast ("PDF attach as a research source is
  coming soon — saved to uploads/ for the agent to read").

**F3 — basic-research seed_passages.**
- **Root:** basic research (`stream_research_answer`) takes `search`/`extraction`/
  `reranker` but no seed corpus. Feed the uploaded passages as seeds that join the
  reranked working set.
- **Files:** `streaming.py:340-392`: add `seed_passages: list[Passage] =
  frozenset()`/`()` param; after extraction (`:373-377`) extend `passages` with the
  seeds before rerank (`:392`) so they compete for `top_k`. Wire from
  `research_stream` (`deep_research_service.py:220`) by loading the
  `upload:{cid}` corpus for the research cid. OFF-path: empty seeds → byte-identical.

**Scope v1:** plaintext / markdown / csv only. No PDF/binary parsing.

**New files:** a small upload-parsing helper (e.g.
`packages/agent-server/src/disco/agent_server/uploads_ingest.py` — text→Passages
chunker) so `files.py` stays a route. No new frontend components (reuse
`UploadComposer`).

**Tests:**
- Backend: attach a `.md` at task start → after a DR run, a cited passage in the
  ReportEvent has `corpus_id == upload:{cid}` (F2). Basic-research run with a seed
  passage that out-ranks web noise → the seed appears in `final.passages` (F3).
  Non-text upload → not ingested, sandbox file still written, toast reason returned
  (PDF-deferral). Pre-created-but-abandoned cid → no events, not in history (F1).
- Frontend (vitest): empty-state renders `data-testid="upload-composer"` enabled
  (cid present) on Research/Agent/DR; submit kicks the pre-created cid (no second
  POST `/conversations`).

**Acceptance (visual — MANDATORY):** Firefox/Playwright screenshots of (a) the
paperclip in the *empty* initial box on each of Research / Agent / DR; (b) a DR
report whose citations include the attached `.md` (chip resolves to the upload);
(c) a basic-research answer grounded on the attached file. `SendUserFile` each.

**Risk:** MED — upload→ingest plumbing + the basic-research statelessness wrinkle
+ orphan-cid lifecycle. De-risked by reusing the existing corpus/Passage path
(no new citation code) and the `recency_window` precedent for submit-time fields.

**Dependencies:** the existing upload/Passages infra (`files.py`, `vectorstore.py`,
`engine._corpus_passages`). Independent of F1/RP-09; per §11.1 this is **P2, LAST**
in Track B. Sequence after the cheap wins.

---

## D3 — DR mid-run steer / inject sources

**ID:** roadmap §2.1#4 / remaining-items "Mid-run DR steer / inject sources".

**Current status (verified):** Stop/resume-at-section-boundary EXISTS; steer +
inject-source mid-run do NOT.
- The run lifecycle: `DeepResearchRun` (`deep_research/engine.py:86`), `run()`
  (`:411-476`). The section boundary is in `_drain_and_synthesize`
  (`:317-409`): legs are drained **serially**, each section is synthesized and
  appended *immediately* (`:403`) — a durable checkpoint — then a `section_done`
  event is emitted (`:405-408`). `should_cancel` is polled at the **top of each
  sub-question iteration** (`:350-354`); when true it sets `bounded_by="stopped"`,
  cancels still-running legs, and returns the partial report.
- Control wiring: `should_cancel` = `flag.is_set` where `flag` is the shared
  `runtime._cancel_flags[cid]` (`deep_research_service.py:562-567`), the SAME flag
  used by kill/cancel/resume. Resume re-enters `run()` with `resume_sections/
  resume_passages/resume_all_hits` from the prior partial ReportEvent
  (`engine.py:417-435`, `deep_research_service.py:543-570`). The WS `steer` frame
  exists but only for the agent loop: `ws.py:34` appends a steer USER message + kicks
  — DR's `research_stream`/`_execute_deep_research` never reads it.

**Root / approach:** There is exactly ONE safe interruption point — the
sub-question boundary in `_drain_and_synthesize` (`:349-350`), the same point
`should_cancel` already uses. Fold steer/inject in **there**, not mid-leg:
- **Steer (re-prioritise / add a sub-question):** at the boundary, drain a
  per-cid **pending-steer queue** (a new `runtime._dr_steer[cid]: list[str]`,
  populated by the WS `steer` frame when `surface == "deep_research"`). A steer
  string becomes either (a) a NEW `SubQuestion` appended to `pending` /
  `gather_tasks` (start a fresh gather leg via `_start_gather_tasks` for it), or
  (b) a re-order hint applied to the not-yet-drained tasks. Emit a `phase`/
  `observation` event so the steer is visible (mirror the memory-bounded
  transparency emit at `engine.py:189-201`).
- **Inject-source (fold in a user URL/file mid-run):** at the boundary, drain a
  `runtime._dr_injected_sources[cid]: list[str|Passage]`. A URL → run it through
  the injected `ExtractionProvider` → Passages; a file → the F2 upload corpus.
  Add those Passages to the **next** section's candidate set (and to the run's
  carried passages so they can be cited). This naturally composes with DR-4's
  upload corpus (`upload:{cid}`) — an injected file IS an upload.

**Files:line — the change:**
- `packages/retrieval/src/disco/retrieval/deep_research/engine.py`
  - `_drain_and_synthesize` (`:349-372`): after the `should_cancel` poll
    (`:350-354`) and before/after `await task` (`:364`), call two injected
    optional callbacks — `pop_steers: Callable[[], list[str]] | None` and
    `pop_injected: Callable[[], list[Passage]] | None` — passed down from `run()`.
    A returned steer → append a new `SubQuestion` + start its leg; injected
    passages → stash for the next `synthesize_section` call (extend the leg's
    candidate set / carried passages).
  - `run()` (`:411-460`): thread `pop_steers`/`pop_injected` through to
    `_drain_and_synthesize`. Keep both `None` by default → **byte-identical**
    off-path.
- `packages/agent-server/src/disco/agent_server/deep_research_service.py`
  - `_execute_deep_research` (`:390-570`): build the per-cid steer/inject queues
    on the runtime (like `_cancel_flags` at `:562`), pass their `pop` closures into
    `run.run(...)` (`:566-570`).
  - `research_stream` is the basic path; the steer queues live on the DR exec path
    only.
- `packages/agent-server/src/disco/agent_server/routes/ws.py:34` and `:48-51`: when
  the conversation surface is `deep_research`, route the `steer` frame into
  `runtime._dr_steer[cid]` (and a new `inject_source` frame into
  `_dr_injected_sources[cid]`) instead of (or in addition to) the agent-loop kick.
- Frontend: a "Steer" input + an "Add source" affordance in
  `DeepResearchSurface.tsx` while `status` is running (near the existing Stop/Resume
  controls). Send a `steer`/`inject_source` WS frame. **No false affordance** — only
  render these while a run is actually in-flight and the WS is open.

**New files:** none (queues are runtime dicts; UI reuses existing input patterns).

**Tests:**
- Engine: a run with `pop_steers` returning one steer at the first boundary →
  `gather_tasks` grows by one leg, the extra section appears in the report,
  off-path (both `None`) is byte-identical (golden report compare). `pop_injected`
  → the injected passage is citable in a later section.
- Service/WS: a `steer` frame on a `deep_research` conversation lands in
  `_dr_steer[cid]` and is consumed at the next boundary (not mid-leg).

**Acceptance (visual):** Firefox screenshot of a live DR run where a mid-run steer
adds a visible new section and an injected URL appears as a cited source; capture
the event timeline. `SendUserFile`.

**Risk:** **MED-HIGH (ENGINE).** This touches the DR run loop's serial
drain — the exact machinery WALK-18 flagged as fragile (`engine.pause()` dead
code; idempotent kick drops resume). Mitigations: (1) only act at the existing
`should_cancel` boundary — never mid-leg, preserving the "completed section =
durable checkpoint" invariant; (2) both hooks default `None` → byte-identical
off-path, guarded by a golden-report test; (3) no new lock acquisition inside the
loop body (CLAUDE.md event-sourcing invariant — the loop runs under `self._lock`).

**Dependencies:** composes with DR-4 (D2) for inject-file (shares the `upload:{cid}`
corpus). Independent of F1/RP-09. Schedule after DR-4 since inject-source reuses
its ingest path.

---

## D4 — RP-09: live audio acceptance

**ID:** #16/#21 / RP-09 · remaining-items "RP-09 live audio acceptance".

**Current status (verified):** the SUBSYSTEM is SHIPPED; only a **live
mixer-robustness acceptance** remains.
- In-process Kokoro TTS: `agent_server/tts_local.py` (`_KokoroEngine`, pinned
  v1.0 ONNX, SHA-verified download, idle-TTL unload, `SAMPLE_RATE = 24000`).
- Server-side pipeline: `agent_server/report_audio.py:337` `generate_report_audio`
  (turn-script → per-turn synth → `mix_pcm` → single `encode_mp3` →
  cid-scoped cache, atomic `.part` rename `:493-498`, content-hash cache key
  `:381-389`). Modes `podcast` (2-voice) / `single` (`:373-374`).
- Mixer: `tools/builtin/_audio_mixer.py` — `mix_pcm` (`:98`, resamples
  rate-mismatched turns, drops empty turns, single-turn = no phantom silence) +
  `encode_mp3` (`:141`, empty/None-safe). The doc-comment (`:5-8`) records the
  original bug (44.1 kHz silence vs 24 kHz Kokoro → decoder glitches) the PCM-mix
  fixed.
- Route + Settings + frontend: `routes/report.py:247` `POST
  /conversations/{cid}/report/audio?mode=podcast|single` → typed 503/502/404; the
  Settings → Audio toggle + audio-overview UI are wired (C2).
- **Existing tests are SYNTHETIC, not live:** `test_audio_mixer_robustness.py`
  explicitly states "No live TTS service is required — every turn is a synthesized
  numpy sine" (`:22`). It covers empty/single/short/clipping/rate-mismatch
  (`:132-324`). `test_audio_overview_three_modes.py` + `test_report_audio.py`
  fake `_call_llm`/synthesis. **Nothing exercises real Kokoro → real mixer → a
  playable file.** That is the open rung.

**Root / approach:** This is an ACCEPTANCE pass (+ any robustness fixes it
surfaces), NOT a new subsystem. Build a real-sample harness that drives the SHIPPED
pipeline with **real Kokoro output** and asserts the mixed MP3 is correct and
playable across the dimensions sine-waves can't catch (real variable-length
segments, voice switches, prosody-driven amplitude, real sample counts).

**Files:line — the change:** none to production code unless the harness surfaces a
defect. The pipeline under test is `report_audio.generate_report_audio` +
`_audio_mixer.{mix_pcm,encode_mp3}` + `tts_local._KokoroEngine`.

**New files:** a live, **integration-marked** harness
`packages/agent-server/tests/test_report_audio_live.py` (marker `integration` so
it runs only in CI's advisory/e2e job + locally, never the required unit gate —
real Kokoro download is heavy). It:
1. forces `provider="bundled"` (real Kokoro), downloads/loads the pinned model
   once (`tts_local`), and runs `generate_report_audio` on 2–3 **captured real
   ReportEvents** (varying section counts: a 1-section minimal report, a typical
   4–5 section report, a long report) in BOTH `mode="podcast"` and
   `mode="single"`.
2. Asserts robustness on each output: the MP3 decodes
   (`_audio_mixer.mp3_duration_seconds` exists, `:179`); duration ≈ Σ(turn
   durations) + (N−1)·`SILENCE_MS_DEFAULT` within tolerance; no NaN/clip wrap;
   `single` mode produces no "Host B" voice switch; podcast alternates A/B; the
   atomic `.part` file is gone and the final mp3+transcript both exist; the
   content-hash cache returns byte-identical on a second call.
3. Sends the rendered MP3 + transcript via `SendUserFile` for human ear-check.

**"Robust" means (acceptance bar):** for every (report × mode) combination the
endpoint returns a single decodable MP3 whose duration matches the turn count, with
correct voice assignment, no decoder glitches at the 24 kHz↔resample boundary, no
clipping artefacts, and a deterministic cache hit on re-request — and a human
confirms it's listenable (no chipmunk/garble) from the `SendUserFile` artifacts.

**Live acceptance steps:** (1) run the harness locally with real Kokoro (first run
downloads ~300 MB); (2) `SendUserFile` the podcast + single MP3s of a real report;
(3) drive the actual UI: open a finished DR report, press Audio Overview in both
modes, confirm playback in the custom AudioPlayer (WALK-14), screenshot it
playing; (4) if any robustness defect surfaces (e.g. a real long turn exposes a
buffer issue `mix_pcm` mishandles), fix it in `_audio_mixer.py` with a new
unit-level regression test built from the captured real PCM.

**Tests:** the live harness above (integration-marked) + any regression unit test
distilled from a real defect (so the required unit gate keeps it green
deterministically, per the sine-wave pattern).

**Acceptance (visual + audio):** Firefox screenshot of the audio overview playing
in the report UI (both modes) + the MP3/transcript artifacts via `SendUserFile`.

**Risk:** LOW. No production change unless a real defect appears; the live harness
is additive and integration-gated (never blocks the required CI job).

**Dependencies:** none. This is a **cheap win** — schedule with F1.

---

## D5 — noVNC live browser (#53–57) — LARGE

**ID:** #53–57 / E6 v2 · `docs/design-novnc-live-browser.md` (a 5-phase design
ALREADY EXISTS). This section TIGHTENS that design against current code; it does
not re-spec from scratch.

**Current status (verified against code):** PLANNED, not started. The design's
referenced files all exist and its assumptions mostly hold — with **three stale
references to correct**:
- `deploy/sandbox/Dockerfile` (verified) installs python/node/playwright-chromium/
  marp/pandoc/build tools — **no Xvfb / x11vnc / noVNC / websockify** today. P1 is
  net-new image layers, as the design says.
- The browser daemon launches **headless**:
  `tools/builtin/_browser_daemon.py:36` —
  `self.browser = self.playwright.chromium.launch(headless=True,
  args=["--no-sandbox"])`, viewport 1280×800 (`:37`). P2's "headed on `DISPLAY`"
  is a real change to this exact line (gate it: headed only when the live tier is
  requested; default stays headless+screenshots).
- Preview proxy plumbing exists and is reusable:
  `agent_server/host_proxy.py:15` `PREVIEW_HOST_RE =
  ^(?P<cid8>[0-9a-f]{8})-(?P<port>\d{2,5})\.localhost...`; it **enforces
  `port in USER_PORTS`** (`:72-73`). `runtime.port_upstream(cid, port)`
  (`:1498`) + `preview_upstream` (`:1492`) + `wake_for_preview` (`:1501`) are the
  resolver hooks. **STALE in the design:** it says "reuse the `USER_PORTS` set /
  `PREVIEW_PORT` machinery" — but `USER_PORTS` is a fixed curated set
  `frozenset({8000, 3000, 5173, 8080, 5000, 4321})`
  (`tools/sandbox/_container.py:42`) with **no noVNC port**. P3 MUST add a
  dedicated `NOVNC_PORT` (e.g. 6080) to `USER_PORTS` (or a parallel allowlist the
  host-proxy also accepts) — the proxy will 4xx an unknown port otherwise. Update
  the design's "reuse USER_PORTS as-is" wording.
- Frontend target verified: `frontend/src/components/build/AgentCanvas.tsx`
  `BrowserPane` (`:73`) renders the per-action screenshot reel
  (`screenshotPaths`, `:22-34`; the "frame-by-frame reel, not a live video" copy at
  `:115-116`). The "Live" toggle + iframe slot in here, as the design says.

**Root / approach:** Follow the design's locked defaults (lazy headed-on-Xvfb on
first browser use; VNC bridge lazy on Live-open; view-only first; no WM /
`--kiosk`; local/podman first, gVisor deferred). The tightened phase plan:

- **P1 — sandbox image** (`deploy/sandbox/Dockerfile`): add a late layer (keep the
  expensive playwright layer cached) installing `xvfb x11vnc novnc websockify`
  (+ `openbox` ONLY if a smoke test shows window-mapping breaks `--kiosk`).
  Rebuild + retag `disco-sandbox:base`; smoke test: start `Xvfb :1`, launch
  chromium headed on it, `x11vnc -localhost`, `websockify`/noVNC serves
  `vnc.html`. Assert image size delta ≤ ~+250 MB. **~½–1 day.**
- **P2 — lazy daemon live service** (`tools/builtin/_browser_daemon.py` +
  a new `tools/builtin/live_view.py`): make the browser launch headed on
  `DISPLAY=:1` when the live tier is requested (`:36` gate); add an idempotent
  `ensure_live()` that starts `Xvfb :1`, ensures the headed browser, starts
  `x11vnc` bound to **127.0.0.1 only**, and `websockify`/noVNC on `NOVNC_PORT`.
  Mirror `BrowserState.start`'s idempotent health-check pattern (`:33-44`). Teardown
  on idle (reuse the sandbox idle-suspend path — `test_idle_suspend.py`). **~1 day.**
- **P3 — agent-server route + proxy wiring**: `GET
  /conversations/{cid}/browser/live-url` → lazily `ensure_live()` →
  return `port_upstream(cid, NOVNC_PORT)`-derived auth-gated proxy URL
  (`runtime.py:1498`). **Add `NOVNC_PORT` to `USER_PORTS`**
  (`_container.py:42`) so `host_proxy.py:72-73` admits it; per-conv jail is the
  existing `{cid8}-{port}` owner-scoping. Return **503 + typed reason** when
  `enable_live_browser` is OFF in Settings. **~½ day.**
- **P4 — frontend Live toggle + Settings gate**: a "Live" toggle next to the
  screenshot reel in `AgentCanvas.tsx` `BrowserPane` (`:73`); when on, `GET
  .../browser/live-url` and render `<iframe src="vnc.html?autoconnect=1&
  view_only=1&...">`. Default stays the reel (no false affordance: only show
  "Live" when `enable_live_browser` is on). Add the `enable_live_browser` Settings
  toggle (default OFF), mirroring the existing audio/vision toggles. **~½ day.**
- **P5 — live + jail/security acceptance** on a real sandbox backend
  (local/podman): open Live, watch the agent navigate; assert x11vnc is
  **loopback-bound** (not network-reachable), another owner's cid8 cannot reach the
  iframe (per-conv jail), `view_only=1` blocks input, idle teardown frees the
  stack. Firefox screenshots/video of the live view + the jail-denial. **~½ day.**

**D7 gVisor prerequisite (FLAG):** the design's default-4 defers gVisor because the
egress-allowlist proxy must admit `NOVNC_PORT` — that is the **D7 egress work**
(see `perpleximanus-egress-proxy` memory). local/podman ship first; gVisor live is a
clean follow-up, NOT a blocker, but must be called out so it isn't assumed working
on the gVisor host.

**New files:** `tools/builtin/live_view.py` (the lazy desktop/VNC service); a
frontend Settings field; tests below. No new proxy — rides `host_proxy.py`.

**Tests:** P1 image smoke (CI image-build job); P2 `ensure_live` idempotency +
loopback-bind assertion (integration-marked — needs a real sandbox); P3 route
returns 503 when disabled / a proxy URL when enabled + `NOVNC_PORT ∈ USER_PORTS`
gate test; P4 vitest: Live toggle hidden when Setting off, iframe `view_only=1` +
no `allow-same-origin` mismatch; P5 the live acceptance (e2e-live runner only).

**Acceptance (visual):** Playwright **video** (not just a frame) of the agent
driving a headed browser in the Live iframe + a screenshot of the cross-owner jail
denial. `SendUserFile`.

**Risk:** **LARGE / MED-HIGH.** Spans the sandbox image, a new daemon service, a
proxy-allowlist change (security-sensitive — `USER_PORTS` is the exposure
boundary), and frontend. Security invariants (loopback-bind, per-conv jail,
view-only) are load-bearing — P5 must prove them, not assume them. Image-size +
RAM (the OOM history) bound the lazy-start design.

**Dependencies:** independent of D1–D4; D7 egress for the gVisor path only. Ship
**last**, as a separate multi-day effort.

---

## Ordered build sequence (this cluster)

Cheap, independent wins first; the ENGINE-touching and LARGE items last.

1. **D1 — F1 keep-searching** (LOW-MED; one file + one wiring line; golden-frame
   OFF-path guard). *Schedule with DR-3 per §11.1.*
2. **D4 — RP-09 live audio acceptance** (LOW; additive integration harness, no
   prod change unless a real defect surfaces). *Cheap win; parallel to D1 —
   disjoint files.*
3. **D2 — G1/DR-4 attach** (MED; reuses the corpus/Passage path; basic-research
   statelessness + orphan-cid lifecycle are the real work). *§11.1 P2, LAST in
   Track B. F2's upload corpus is a prerequisite for D3's inject-source.*
4. **D3 — DR mid-run steer/inject** (MED-HIGH, ENGINE; act only at the existing
   `should_cancel` boundary; both hooks default-None → byte-identical). *After D2
   (inject-file reuses the `upload:{cid}` corpus).*
5. **D5 — noVNC live browser** (LARGE; 5 phases, security-sensitive proxy change,
   gVisor deferred behind D7). *Separate multi-day effort, last.*

Every UI-affecting item (D2, D3, D4, D5) ends with Firefox/Playwright visual
evidence via `SendUserFile` and passes the four fitness gates + the relevant unit
suites before it's called done.
