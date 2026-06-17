# Dylan's Walkthru — 6-17-26

> This file has three parts:
> 1. **Section 1 — Verbatim notes.** Dylan's session walkthrough, copied EXACTLY,
>    not one word changed. Do not edit this section.
> 2. **Section 2 — Root-cause breakdown.** Per-issue investigation with file:line
>    evidence (added after the fact, separate from the verbatim notes).
> 3. **Section 3 — Consolidated search list.** The actionable items, folded into
>    the standing "stuff we need to search/fix" register.

---

## Section 1 — Verbatim notes (DO NOT EDIT — copied exactly)

during my session, here are the issues i noticed. while doing a basic search, the markdown text that the model outputted was not formatted. i saw all the blocks of markdown code in there, then when the report finished, the markdown style actually appled. Next, when you ask a deep research question and submit it, your question takes the place at the top as the title, but until the plan appears, it is blank. we should add a tasteful loading signal here. also, during my deep research, i selected deepseek v4 as the driver. i noticed that my blackbox mini pc was spinning up quite significantly. i thought i selected the bundled options so im confused- ill need to check this to verify but this may be an issue. also, on one of the subsections, this notice on source disagreement is a little bit wierd. "one medium analysis" isnt something that should be making it to the ui. not sure what the bracketed numbering is either, looks like perhaps a trace or identifier that got surfaced. Sources disagree. One Medium analysis argues that the traditional accuracy-versus-latency dichotomy is obsolete, advocating instead for a multi-dimensional view incorporating model size, deployment cost, and development time [[bce679_p0]]. also, this one is a little bit wierd too." Sources disagree. However, the sources leave important tensions unresolved.". for the generate audio option at the bottom of the report, if the models are downloading or doing whatever you said the first time setup is, there should be a notice so the user isnt just stuck waiting and wondering why its taking forever. Ideally this would show an actual percentage or something. its a long wait, even for a kickass pc like mine. for the audio overview, it seems like the default path is for podcast style audio- and honestly im impressed. this isnt bad dude, seriously. i think when clicking generate audio, you should have a popup that has the choice for a podcast style (note this as alpha, or not fully expanded on yet), while the other option should be something like a single speaker (the first speaker chosen out of the two in the settings model should be the defualt- this needs to be noted in the UI as well for the settings page). The single voice audio overview should take the report, and break it down. honestly discuss the gaps but go over the report. an honest report essentially based off the report. but i really have to say, this shit is coming together man. good job. one nitpick, is i wish the audio player that pops up would be more in line with our UI choices- it looks out of place and blocky/clunky. final parts for the deep research surface. the follow up option still does not work. we still need to figure that out. you said you worked on this, but maybe it has to do with the time gap between me checking all the functions and asking the follow up last, after a few minutes. not sure. we need to fix this one and put it to bed. Wait a second- it was actually doing something. it prodduced a report that i asked for. okay so this actually points to something else, that we need to add a section below the follow up portion whenever its active, stating what the model is actually doing. it looks frozen, but it is not. also, similarly, before it produced this follow up output, the loading bar at the top for the original plan was spinning again. these need to be decoupled. lastly, for the follow up, its another issue of the markdown not being formatted correctly. the response is legible, but its covered with markdown formatting plaintext and didnt convert even after finishing. also when the export, generate etc card appears after the follow up, the export doesnt contain the follow up text. need to work on that as well. for any generation or exporting after a follow up, the first popup should probably ask the user whether they want to include the follow up. this needs to be variable, and have the option to choose how many follow ups to include. it needs to scale and be modular i guess. then after that prpopup, it should produce the regular action, like the popup for markdown, pdf, etc. and tactually hook and grab the follow up in those exports or generation for the audio. additionally, i asked a second follow up just to confirm, that yes, the loading at the very top near the plan section triggers whenever a follow up is asked. and also to confirm, in the settings, the encoders are selected as bundled. you should check out whats going on there to see if the settings is lying. my mini pc was spinning up significantly. to be fair, it could be due to searxng running on my mini pc though, with crawl4ai. woah, just noticed another bug. i went to the settings then back to deep research to ask a question. immediately, right off the bat, the text follow up: provide a report on the current global shipping trade appeared. the follow up is listing my exact first search. the deep research hasnt even started yet. thats wierd. another data point, i swapped to the local bundled crawler and my mini pc no longer spins up, so the crawl4ai may very well be the cause of my mini pc spinning up. it looks like th ebunded method works for this. we should probably not reffence duckduckgo here specifically though for copyright reasons, maybe just ddgs instead. and in this report, again, the sources disagree flag is wiered and doesnt make sens in the context. okay, im testing build out now.  - okay so for build, a few things. first, after the model tries to serve it up and verify its running, thats when the manifest button and the open button appear. however the manifest button doesnt do anything until the plan is actually completed. we are surfacing this card too early. also, the open button directed be to a local host converstions conv_ path /preivew/ that doesnt actually pull up anything and says preview not avaliable. however the preview tab on the actual application is fine. the good news is that the export option works after the first iteration at least. -- okay i just noticed something. it looks like for some reason we got rid of the tool where llms can make surgical edits on files instead of reqriting the whole thing. i vaguealy remember that being a thing that we should have implemented for small models, which means the assist option should be the only thing carrying that. if im off there, let me knwo. -- okay so im on my second iteration here, and its just trying to debug a black screen in a game i asked it to make. good god dude, the redundancy is awful, and it keeps looping. it looks like we are almost forcing a loop. what the hell is going on here? its painful man, and it burns up tokens. this is on deepseek v4 flash, a model that should absolutely be good enough. the issue is whatever harness we have it in right now. aloso, whenver you click paude, and you send a message to steer, it doesnt actually pause until you send that message. then when i tried to press resume, nothing happened. so its a pause, which doesnt immediately work, followed by a steer, followed by a resume which doesnt work. -- okay so the agent surface now. i asked it to generate slides and it made html slides. that tells me it still wont use the actual pptx or slides tool for some reaso. the good news is, the agent produced that html and it popped up in my chat with an option to download it. i downloaded and it pulled up correctly, good work there. but this is definitely something that should appear in the artifacts tab for agent mode. -- for all my interactions you should look over the traces as well to see what is going on. finally, when an agent compelted a task, a screenshot in a borwser, which now works so good job on that, i tried the schedule thing that pops up at the bottom of the pager. the syntax is very specific. its wierd and doesnt work well. we should rework that to be better.

---

## Section 2 — Root-cause breakdown

Each issue traced to code with file:line evidence by parallel read-only
investigators (2026-06-17), plus corroboration from the live event log (`disco.db`)
and the persisted config (`disco-config.json` + its pre-fix backup). Every item is
tagged **CONFIRMED** (mechanism read directly in source) or **HYPOTHESIS**.

Legend for the surfaces: DR = Deep Research, BUILD/AGENT share machinery
(`_BUILD_LIKE_SURFACES = {"build","agent"}` in `runtime.py`).

### A. Deep Research — streaming render & surface state

**A1 — Streaming markdown shows as raw plaintext (basic search), formats only on finish. CONFIRMED.**
- Root cause: `frontend/src/components/AnswerDocument.tsx:40-45` renders the in-flight
  block's accumulated tokens as bare `{streamingText}` inside a plain `<p>` — no markdown
  parse. On completion `hooks/useResearchStream.ts:45-54` moves it into `blocks[]`, where
  `blocks/BlockView.tsx` renders structured/styled. The flip is the `token`→`block` frame
  transition (`useResearchStream.ts:38-54`).
- Note: the DR *report* itself does NOT stream raw text (it shows `SectionSkeleton` pulse
  bars then pops in fully rendered via `<Markdown>` at `DeepReportView.tsx:180`). The raw
  markdown the user saw on the DR surface is the **follow-up** render path — see A3/B.
- Fix: in `AnswerDocument.tsx:40-45` render `streamingText` through `<Markdown>` (react-markdown
  tolerates partial markdown) and append the caret after it.

**A2 — DR title appears but the area below is blank with no loader until the plan arrives. CONFIRMED.**
- Root cause: `frontend/src/components/research/DeepResearchSurface.tsx:115-302`. After
  `submit()`, the header H1 renders `r.query` immediately (`:120-127`), but EVERY block below
  is gated on `r.plan` being present (plan gate `:244`, progress strip `:264`, report `:295`).
  `plan` stays null until the decompose step emits a PlanEvent (`useDeepResearchStream.ts:175`),
  several seconds later. Nothing renders for the "started, RUNNING, plan still null" window.
- The exact gap state: `r.started === true && r.plan === null && r.status === "RUNNING"`.
- Fix: add a "Planning the research…" loader branch between the header (`:233`) and the plan
  gate, gated on that state; reuse the `Loader2` spin already imported at `:28`.

**A3 — Follow-up answer markdown never formats (even after finishing). CONFIRMED.**
- Root cause: `DeepResearchSurface.tsx:326-336` renders the assistant follow-up answer as
  `<div className="whitespace-pre-wrap …">{m.message.content}</div>` — raw text, never passed
  through `<Markdown>`. Completely separate path from the report (`DeepReportView.tsx:180,218`).
  No renderer → stays raw permanently.
- Fix: render through `<Markdown>` (import it — currently not imported in that file); build the
  `answer` for `[[id]]` citation resolution the same way `DeepReportView` does
  (`asGroundedAnswer(report, query)`).

**A4 — Stale "Follow-up: <prev query>" appears before research starts (Settings→DR round-trip). CONFIRMED (derivation bug); the "persisted across surfaces" theory DISPROVEN.**
- Root cause: `hooks/useDeepResearch.ts:178-186`. Follow-ups are derived as
  `events.filter((e.seq ?? 0) > reportSeq && role∈{user,assistant})` with
  `reportSeq = stream.report?.seq ?? -1`. When no report exists yet, `reportSeq = -1`, so the
  run's OWN initiating user query (seq ≥ 0 > -1) is misclassified as a "follow-up." The panel
  (`DeepResearchSurface.tsx:317`) is gated only on `followUps.length > 0`, not on report-present.
- There is NO zustand/persisted store holding follow-up text (verified) — it is re-derived from
  the live event stream, so the "stale" text is the new run's own query mislabeled.
- Contributing: `useDeepResearchStream.ts:145-147` bails `if (!session) return;` BEFORE
  dispatching `reset`, so a session→null transition doesn't clear the reducer's events.
- Fix: (1) bail follow-up derivation to `[]` when `stream.report == null` (don't use the `-1`
  fallback); (2) gate the panel on `r.report && r.status === "FINISHED"` (match `NeedMoreCard`
  at `:344`); (3) dispatch `reset` when `session === null` in `useDeepResearchStream.ts`.

### B. Deep Research — follow-up engine & export

Follow-up data flow: `NeedMoreCard`/`ReportFollowUp.onAsk` → `r.followUp(q)`
(`useDeepResearchStream.ts:171-173`) → WS `send_message` → `POST /conversations/{cid}/messages`
(`conversations.py:63-71`) → `runtime.kick` → `_maybe_run_deep_research`
(`deep_research_service.py:206-263`, Phase-3 `_has_fresh_user_message` branch line 262) →
`_follow_up_deep_research` (`deep_research_service.py:592-739`). A dedicated `/followup` route
exists (`conversations.py:73-98`) but the frontend uses `send_message`; both converge on `kick`.

**B1 — Follow-up looks frozen (no activity indicator) though it IS working. CONFIRMED.**
- Root cause: `_follow_up_deep_research` emits ZERO progress events — only
  `StatusEvent(RUNNING, detail="follow_up")` (`deep_research_service.py:623-626`), then ONE
  blocking, **non-streaming** `router.complete()` (line 700; no-passages branch line 634), then
  the answer `MessageEvent` (line 708-714), then `StatusEvent(FINISHED, detail="follow_up_complete")`.
  No `ActionEvent`/`ObservationEvent`, no token stream. The frontend's only progress renderers
  (`deriveLiveTrace`/`deriveStats`, `deepResearchTrace.ts:268-362`) are driven by exactly those
  missing action/observation events → nothing to show. There is no follow-up status component.
- Fix: (backend) emit coarse phase events during follow-up (e.g.
  `ActionEvent(tool_name="phase", {...})`, or switch line 700/634 to `router.stream_complete`);
  (frontend) add a dedicated follow-up status section below `ReportFollowUp`/`NeedMoreCard`,
  driven by the follow-up `StatusEvent` + new phase events — independent of `DeepProgressStrip`.

**B2 — The top original-plan loading bar re-spins whenever a follow-up is asked (must decouple). CONFIRMED.**
- Root cause: the original-plan progress UI and the follow-up share the SINGLE conversation
  `status` field. The follow-up's `StatusEvent(RUNNING, detail="follow_up")` is collapsed by the
  reducer (`useDeepResearchStream.ts:80-91`) to `r.status = "RUNNING"`, discarding the
  `detail="follow_up"` discriminator. `DeepProgressStrip` (always mounted once a plan exists,
  `DeepResearchSurface.tsx:264-272`) keys every loader purely on `status === "RUNNING"`
  (`DeepProgressStrip.tsx:65-72,81`; `deepResearchTrace.ts:343,356`). Confirmed by Dylan: a 2nd
  follow-up re-triggered the top plan loader each time.
- Fix: track a separate `followUpStatus` in the reducer keyed on
  `StatusEvent.detail ∈ {follow_up, follow_up_complete}` (don't overwrite main `status`); gate
  `DeepProgressStrip` on "plan run active," not raw `status==="RUNNING"`; drive B1's new indicator
  from `followUpStatus`.

**B3 — Export/generate after a follow-up omits the follow-up text. CONFIRMED.**
- Root cause: every export/audio serializer consumes ONLY the `ReportEvent`:
  frontend `serializeReportToMarkdown(report)` (`deepResearch.ts:155-194`); server
  `export_report` fetches `reports[-1]` (`deep_research_service.py:741-774`) →
  `report_export.serialize_markdown(report)` (`report_export.py:30-72`; PDF/DOCX derive from that
  markdown); audio `generate_report_audio(report, …)` (`report.py:80-127`). Follow-up turns exist
  only as separate `MessageEvent`s (`seq > report.seq`), surfaced as `r.followUps`
  (`useDeepResearch.ts:178-186`) but never passed into any serializer.
- Fix: extend serializers to accept follow-up turns; server side gather post-report user/assistant
  `MessageEvent`s (skip `<system-reminder>`/`<reground-anchors>`) and append a "Follow-up Q&A"
  section. NOTE: `serialize_markdown` (py) and `serializeReportToMarkdown` (ts) are verbatim-ported
  byte-for-byte — they must change together.

**B4 — (FEATURE) Pre-export "include the follow-up(s)? how many?" popup, modular/scalable. Seam mapped.**
- Triggers: top-bar export `DeepResearchSurface.tsx:162-225` → `exportReportByFmt`
  (`useDeepResearch.ts:145-165`); `ExportModal` in `NeedMoreCard.tsx:103-340`; audio
  `AudioSection.generate` (`NeedMoreCard.tsx:351-362`) → `requestReportAudio` (`deepResearch.ts:126-146`).
- Data model: one follow-up turn = a post-report `MessageEvent` (user question / assistant answer,
  alternating in event order); already flattened to `r.followUps` (`useDeepResearch.ts:178-186`).
  No pair grouping today — "how many" = count of post-report `role:"user"` entries.
- Build: new `IncludeFollowUpsModal` opened BEFORE `ExportModal`/audio when `followUps.length>0`,
  binding to grouped Q/A pairs with a count/individual-toggle selector; thread the selection
  (selected seqs) through `serializeReportToMarkdown`/`exportReportByFmt`/`requestReportAudio` →
  server `export_report`/`report_audio`/`serialize_markdown` reconstruct selected turns from the log.
- Files: `NeedMoreCard.tsx`, `DeepResearchSurface.tsx`, `useDeepResearch.ts`, `deepResearch.ts`
  (+ new modal); backend `deep_research_service.py`, `routes/report.py`, `report_export.py`,
  `report_audio.py`.

### C. Deep Research — synthesis text quality ("Sources disagree" notice)

The notice is built from `ReportSection.disputed_notes` (sentences the synthesizer extracts from
the section body and the UI surfaces in a callout). Three compounding defects:

**C1 — Raw `[[bce679_p0]]` citation id leaks into the rendered notice ("looks like a trace id"). CONFIRMED.**
- Root cause: `frontend/src/components/research/DeepReportView.tsx:166-177` (line 174) renders
  `{real.disputed_notes.join(" ")}` as RAW JSX text. The section *body* one line down (`:180`)
  goes through `<Markdown answer={answer}>` → `CitedText` which resolves `[[id]]` via
  `CITE = /\[\[(\w+)\]\]/g` (`blocks/CitedText.tsx:6,68-82`). The callout bypasses that resolver,
  so `[[bce679_p0]]` prints verbatim. Same leak on export: `deepResearch.ts:166-167`
  (`_Conflicts noted: …_`). Upstream `_extract_disputed_notes` (`synthesis.py:249-260`) returns
  sentences verbatim and does NOT strip `[[id]]` — even though `coherence_pass` (`synthesis.py:468`)
  proves the strip (`re.sub(r"\[\[[\w-]+\]\]","",lead)`) is known and just missing here.
- Fix: render the callout through `CitedText` (the `answer` is in scope at `:180`); strip/resolve
  on export too.

**C2 — "One Medium analysis" — sources attributed by publishing platform reach the UI. CONFIRMED.**
- Root cause: `_SECTION_PROMPT` rule 2 (`synthesis.py:170-180`) tells the model to attribute
  claims; it attributes by platform ("One Medium analysis argues…"). `_extract_disputed_notes`
  (`synthesis.py:254-260`) lifts that sentence verbatim. Nothing forbids naming the platform or
  rewrites it.
- Fix: add a prompt line forbidding referring to a source by its publishing platform; scrub in
  extraction.

**C3 — Vacuous disagreement sentences ("…leave important tensions unresolved."). CONFIRMED.**
- Root cause: the extraction cue regex (`synthesis.py:255-259`) has a `however[, ].+sources?`
  branch that matches ANY hedging sentence, plus the UI hardcodes the "Sources disagree." prefix
  (`DeepReportView.tsx:173`) → tautologies. Prompt rule 4 (`synthesis.py:185-189`,
  "FOREGROUND TENSION AND UNCERTAINTY") encourages the hedging; the regex has no signal filter.
- Fix: drop the loose `however…sources?` branch; require a real conflict cue AND a `[[id]]`
  citation; reject short/hedge-only notes; make the "Sources disagree." label conditional.

### D. Audio overview

Flow: `AudioSection` button (`NeedMoreCard.tsx:366`) → `requestReportAudio(cid)`
(`deepResearch.ts:126`, single blocking POST) → `routes/report.py:80 report_audio()` →
`report_audio.py:199 generate_report_audio` (LLM turn-script → per-turn synth → mix → mp3) →
`tts_local.synthesize` → `_KokoroEngine.__init__` → `_fetch` (~300MB `urllib.urlretrieve`) →
artifact served at `report.py:129` → raw `<audio>` at `NeedMoreCard.tsx:399`.

**D1 — First-run model download (~300MB) shows no progress; UI just hangs. CONFIRMED.**
- Root cause: `tts_local.py:66 urllib.request.urlretrieve(url, tmp)` is called with NO
  `reporthook` → no byte progress. The frontend already HAS a `progress` field
  (`AudioState.generating.progress`, rendered `NeedMoreCard.tsx:380`) but it is hardcoded to `0`
  at `:352` → the `progress>0` branch is dead code. Download runs off-loop
  (`asyncio.to_thread`, `tts_local.py:109`) so uvicorn isn't blocked, but the HTTP response is
  withheld until the WHOLE pipeline finishes. A per-conversation ephemeral WS progress channel
  EXISTS (`store.publish_ephemeral` → `pump_ephemeral`, `ws.py:129`, the watch-it-write bus) but
  `report_audio.py`/`tts_local.py` carry no `cid`/`store` ref — though the route handler
  (`report.py:80`) has both.
- Fix: pass a `reporthook` to `urlretrieve`; thread a progress callback + `cid` from `report.py`
  down to `_fetch`; publish `tts_progress` ephemeral frames; drive the existing `progress` render
  from them. At minimum surface a static "Downloading voice model (~300 MB, first run only)…".

**D2 — Audio player is a raw native `<audio controls>`, clunky/out of place. CONFIRMED.**
- Root cause: the ONLY player is a bare native element at `NeedMoreCard.tsx:399`
  (`<audio src=… controls className="h-8 max-w-[16rem]" />`). `controls` draws the browser's
  default media chrome (grey pill in Firefox) which can't take Tailwind tokens; no CSS targets
  `audio`/`::-webkit-media-controls`. Every sibling uses the design system (`rounded-control`,
  `border-hairline`, `bg-surface-1`, `font-ui`, `text-text`, `accent`).
- Fix: build a custom `AudioPlayer.tsx` (hidden `<audio ref>` + custom play/pause, seek bar,
  elapsed/total) styled with the design tokens; replace the bare element.

**D3 — (FEATURE) Podcast-vs-single-speaker choice popup + single-voice mode. Seam mapped; no single-voice path exists today.**
- Two-host podcast is hardcoded everywhere: prompt `_TURN_SCRIPT_PROMPT` (`audio_overview.py:63`,
  "two-host audio script… Host A… Host B"); schema `Turn.speaker` `pattern=^(A|B)$`
  (`audio_overview.py:44`); validator requires ≥2 turns + A/B (`:150,:158`); synth maps A→`voice_a`,
  B→`voice_b` (`report_audio.py:298`); transcript labels "Host A/B". Config `TtsSettings`
  (`core/llm/config.py:119`: `voice_a`,`voice_b`,…) has no mode field. Settings UI
  `settings/AudioSection.tsx:234-255` has Host A/Host B voice inputs with no "single-speaker
  default" note.
- Build (recommend per-request popup choice, no config migration): add `mode:"podcast"|"single"`
  to `report_audio()` (`report.py:80`) + `generate_report_audio` (`report_audio.py:199`); single
  mode = a new walkthrough/breakdown prompt (honestly covers gaps, not a dialogue), relax the
  validator/schema to accept an all-"A" script — the existing A→`voice_a` mapping already makes
  the FIRST configured voice the single-speaker default. IMPORTANT: artifact path is fixed
  `audio_overview.mp3` with an idempotency short-circuit (`report_audio.py:224,232`) → the two
  modes WILL collide; cache key must include the mode. Frontend: replace the single button with a
  Radix dialog (pattern exists as `ExportModal`) offering "Podcast style (ALPHA)" vs
  "Single speaker"; pass `mode` through `requestReportAudio`. Settings: note `voice_a` = single
  default at `AudioSection.tsx:236`. Mirror `mode` in the `audio_overview.py` tool to keep
  tool+server in lock-step.

### E. Providers / settings honesty ("is Settings lying?" — it is NOT)

Single resolution path: `build_live_retrieval` (`retrieval/live.py:416-484`), called only from
`DeepResearchService._research` (`deep_research_service.py:135-171`, used by BOTH research and
deep_research). It loads persisted config (`_config_store.load()`) and passes every field through
EXPLICITLY (`live.py:158-169`). Config is JSON at `disco-config.json` (`config_store.py:38-39`).
The app-server Settings GET reads the SAME store and maps 1:1 (`mappers.py:175-207`).
**Displayed config == persisted config == effective config.** Precedence: persisted JSON is
authoritative; env vars + hardcoded LAN defaults are DEAD on the live path (they only fire when a
passed value is None/empty, and the caller always passes real values). An env var CANNOT silently
override a "bundled" UI selection here.

**E1 — Driver = deepseek v4 blamed for mini-PC spin. RED HERRING / CONFIRMED NOT the cause.**
- `or-deepseek-deepseek-v4-pro` has `base_url=https://openrouter.ai/api/v1` (remote OpenRouter);
  the driver selection only sets `default_model`/the pill (`config.py:224-232`). A remote LLM call
  cannot spin a LAN box. Other roles (rag/rewriter/summarizer) are all `or-gpt-oss-120b-free`
  (also OpenRouter) — no local llama-server engaged.

**E2 — Encoders show "bundled" but mini-PC spun. CONFIRMED: encoders were NOT the culprit; Settings was truthful.**
- `encoders.remote: false` in the live config AND the prior backup. With `remote=False`,
  `build_live_retrieval` (`live.py:477-483`) builds IN-PROCESS `FastEmbed*` (ONNX/CPU); the remote
  encoder URLs are only used on the `remote=True` branch (`live.py:472-476`). Display mapper
  `_encoders_from` (`mappers.py:175-182`) shows the true bool. No divergence.

**E3 — crawl4ai is the real spin cause (Dylan confirmed by switching to bundled). CONFIRMED: it was PERSISTED config, not a default.**
- Shipped DEFAULTS are bundled/keyless: `SearchSettings.provider="ddgs"`,
  `ExtractionSettings.provider="local"` (`config.py:150,161`). But Dylan's persisted
  `disco-config.json.bak-1781588447` (mtime 2026-06-16) had `search.provider="searxng"`
  (`http://192.168.1.202:8888`) and `extraction.provider="crawl4ai"` (`http://192.168.1.237:11235`,
  crawl4ai's default port) — carried over from his earlier manual mini-PC setup. `_make_search`/
  `_make_extraction` (`live.py:392-413`) dispatch on the provider string → hit the LAN boxes → spin.
  The current file (mtime 2026-06-17 03:28) shows his fix: `search.provider="ddgs"`,
  `extraction.provider="local"` → bundled in-process → spin stops. Matches his report exactly.
- LATENT GOTCHA: the current file STILL carries the stale `base_url` values
  (`192.168.1.202:8888`, `192.168.1.237:11235`) — inert while provider is bundled, but if he ever
  re-selects searxng/crawl4ai in the UI, those persisted URLs resolve again.
- The whole illusion: "bundled" was true for **Encoders** (one Settings section) while
  **Data Sources** had Self-hosted Crawl4AI/SearXNG selected (`DataSourcesSection.tsx:29-36`,
  tier "selfhost"). Two separate sections, easy to conflate.
- Fix/hygiene: when provider flips to a bundled tier, null the persisted `base_url` (in
  `save_search`/`save_extraction`); optionally surface effective endpoints per section or warn
  when a non-bundled provider points at a LAN host.

**E4 — Don't name "DuckDuckGo" in the UI (copyright) → use "ddgs". CONFIRMED, exactly one user-visible string.**
- `frontend/src/components/settings/DataSourcesSection.tsx:29` —
  `label: "Bundled — DuckDuckGo"`. Change to `"Bundled — ddgs"`. Everything else
  ("duckduckgo" in `bundled_providers.py`, `config.py`, dtos comments) is internal-only
  (comments/docstrings); the wire id is already `ddgs` and logs already say "ddgs search".

### F. Build surface

**F1 — Manifest/Open card surfaced too early; Manifest button does nothing until plan completes. CONFIRMED.**
- Root cause: `DeliverablePanel` (`frontend/src/components/build/DeliverablePanel.tsx`, rendered
  `BuildSurface.tsx:367-379`) appears whenever `deriveDeliverable(visibleEvents)` is non-null
  (`buildTrace.ts:435-449`), which is true the instant ANY `DeliverableEvent` exists. But the
  `serve` tool emits that event mid-run, BEFORE the affirmative `finish`
  (`turn_control.py:630 handle_serve`, `engine.py:1151`). Manifest button →
  `GET /api/projects/{cid}/manifest` (`api/projects.ts:129`) requires a persisted project record
  written only when a build run ENDS (`runtime.py:1268`) → mid-run returns 404
  (`routes/projects.py:97-102`) → dead button.
- Fix: gate the panel on `b.status === "FINISHED"` (when the snapshot/manifest record also exists),
  not on mere presence of a `DeliverableEvent`.

**F2 — "Open" button → `…/conversations/conv_…/preview/` says "preview not available", but the in-app Preview TAB works. CONFIRMED (two divergent routes).**
- Embedded Preview tab (works): `PreviewPane.tsx:92` uses `previewHostUrl(cid, port)` → origin-true
  subdomain `http://{cid8}-{port}.localhost:8000/` (`api/client.ts:68`) → `HostPreviewProxyMiddleware`
  (`host_proxy.py`) whose resolver calls `runtime.wake_for_preview` (`preview_service.py:56-88`) —
  it REMATERIALIZES a suspended sandbox.
- Open button (broken): `BuildSurface.tsx:372` → `…/conversations/{cid}/preview-app/` → DEPRECATED
  path route (`routes/preview.py:33-43`, marked "DC-01: hostname proxy is canonical") → resolves via
  `runtime.preview_upstream` → `port_upstream` (`preview_service.py:45-54`) which reads ONLY
  `_executors.get(cid)` — a live in-memory executor, NO wake. After the run ends and the sandbox
  auto-suspends, there's no executor → 503 "preview not available". (PreviewPane's own
  "Open in new tab" link at `:102` has the same bug.)
- Fix: point Open at `previewHostUrl(cid, PREVIEW_PORT)` (same as the tab), OR make the
  `preview_app`/`port_app` path routes resolve through `wake_for_preview`.

**F3 — Surgical line-edit tool "removed"? CONFIRMED STILL PRESENT — Dylan's recollection is wrong.**
- `file_edit` (`files.py:383`), `file_replace_lines` (`files.py:458`), `file_insert_lines`
  (`files.py:531`) all exist, are instantiated (`builtin/__init__.py:77-79`), and are in
  `AGENT_TOOLS` (`registry.py:68,70,71`) — always in scope for build/agent, NOT assist-gated.
  "assist" is a weak-model tier (`_common.py:85-87`) that only tweaks `file_read` budgets and the
  F6 patch-spiral "switch to full rewrite" directive (`stuck.py:11-17`); it does not carry the
  surgical tools (those are unconditional). No fix needed — informational.

**F4 — Build loop redundancy / forced looping, burns tokens on deepseek v4 flash (capable model → it's the harness). HYPOTHESIS (strong, evidence-backed) + trace-corroborated.**
- Mechanism: every loop-breaker keys on a FAILURE signal or an IDENTICAL repeat; none detect
  "successful actions making no visual progress" — exactly the black-screen-game case.
  - Stuck detector (`loop/stuck.py:51-52`): byte/content-identical action→observation cycles,
    threshold 3. A capable model writes a DIFFERENT edit each turn → never content-equal → never fires.
  - Circuit breaker (`turn_control.py:307-321`): counts `count_recent_failures` (failed obs /
    `AgentErrorEvent`). A black screen comes from SUCCESSFUL tool calls (writes apply, dev server
    returns 200, `verify="app"` only checks the server responds — `finish.py:102-112`) → zero
    failure events → never trips.
  - Finish/DoD/verify caps (`finish.py:50,59`) reset on any passing verdict and release after 3.
  - F6 patch-spiral detector (the one semantic no-progress signal) is assist-gated (`stuck.py:11-17`)
    and assist defaults OFF for capable cloud models → disabled on deepseek v4 flash.
  - Backstop is `max_iterations`, default **500** (`engine.py:461`).
- Trace corroboration (`disco.db`, build convo `conv_303536044bf4…`, 376 events): 167 actions /
  **160 observations / only 7 agent_errors** — i.e. mostly SUCCESSFUL, varied actions, almost no
  failures, so the failure-keyed breakers never engage and it grinds toward the 500-iter ceiling.
- Fix: add a semantic no-progress detector that is failure-independent and NOT assist-gated — trip
  when the same `verify`/screenshot symptom recurs across N varied edits (screenshot-hash /
  served-DOM diff); lower the practical ceiling for the build/iterate path; make the circuit
  breaker also consider "repeated identical verify outcome," not just tool failures.

**F5 — Pause doesn't take effect until a steer message is sent; Resume does nothing. CONFIRMED.**
- No real pause is wired. `engine.pause()`/`engine.resume()` exist (`engine.py:1491-1499`) but are
  DEAD CODE (zero callers); `ws.py:78` literally says "pause: … wired with the UI later" — there is
  NO `pause` frame handler. The only stop is the cooperative `cancel` (Stop button →
  `ControlOps.cancel` → `engine.py:1501-1506`), observed only at the between-steps checkpoint
  (`engine.py:1054-1073`). The turn body holds `self._lock` across the entire model step
  (`engine.py:1056→1124`), and `cancel()` must take that same lock to emit IDLE → it can't land
  until the in-flight step finishes (≈ when you've given up and typed a steer message).
- Resume: `kick()` is idempotent (`runtime.py:1222-1224`: returns if a non-done task exists). After
  a cooperative cancel the prior task is often still finishing its step, so `resume_conversation`
  (`resume_service.py:370-375`) calls `kick`, which no-ops over the stale-but-alive task → nothing
  spawns. Also resume legality requires PAUSED or IDLE-with-unfinished-plan
  (`resume_service.py:345-350`) while the frontend `canResume` (`useBuildStream.ts:299-303`) is
  looser → button can 409 silently.
- Fix: wire a genuine pause — `pause` frame handler in `ws.py` + `ControlOps.pause` →
  `engine.pause()`, and check a cooperative pause flag/`asyncio.Event` INSIDE the driver step (not
  only at the lock-held checkpoint) so it lands mid-step; for resume, await/clear the cancelled task
  (or have `kick` replace a cancel-pending task) before re-kicking, and align backend resume
  legality with the frontend `canResume`.

### G. Agent surface

**G1 — Asked for slides → agent hand-rolled HTML instead of the pptx/slides tool. CONFIRMED mechanism (prompt steering), HYPOTHESIS that it's why this model chose HTML.**
- The tool EXISTS and is in scope: `slides_generate` / `SlidesTool` (`builtin/slides.py:304-320`,
  Marp → html always, pdf/pptx when Marp+Chromium present), in `AGENT_TOOLS` (`registry.py:88`),
  advertised + callable. NOT missing.
- Root cause: prompt steering. The EXECUTION driver prompt `_EXECUTION_DRIVER_PROMPT`
  (`core/llm/prompts.py:187-246`) funnels all authoring to file tools ("file_write, file_edit…"
  line 190; `<file_rules>` line 237) and NEVER mentions `slides_generate`/`sheet_generate`. The only
  prose surfacing is the PLANNING-only capability block (`prompts.py:399-416`, appended at `:467`),
  framed as "available after approval." At execution time the deck tool is in the schema list with
  zero steering → a model asked for slides defaults to `file_write` HTML.
- Fix: add an explicit line to `_EXECUTION_DRIVER_PROMPT` (and the small-model variant): "For
  presentations use `slides_generate`; for spreadsheets `sheet_generate`; do NOT hand-author
  HTML/CSV when a generator exists." Optionally a deck/"slides" intent heuristic.

**G2 — Agent-produced file downloads in chat but is absent from the Artifacts tab. CONFIRMED (frontend enumeration gap, not backend/cid).**
- `ArtifactsPane` (`AgentCanvas.tsx:194-203`) renders ONLY `deriveFiles(events)`. `deriveFiles`
  (`buildTrace.ts:305-329`) ingests ONLY `action` events with `tool_name ∈
  {file_write, file_append, file_edit}`. Files reach the user via THREE disjoint paths —
  (1) file_write actions → Artifacts tab; (2) sandbox-tool `observation` artifacts
  (slides/sheet structured output, parsed `buildTrace.ts:155-198` → SlidesBlock/SheetBlock with
  download); (3) `deliverable` events from `serve()` (`buildTrace.ts:438`). The tab consumes only
  (1). So a deck/sheet from a sandbox tool, a served HTML handoff, or anything written via
  `shell`/`code_exec` is downloadable in chat but never enumerated as an artifact. Backend route
  `GET /conversations/{cid}/artifacts/{path}` (`routes/files.py:164`) and `cid` threading are fine.
- Fix: broaden `deriveFiles` (`buildTrace.ts:305`) to also harvest file artifacts from
  `observation` events (slides/sheet `structured.filename`) and `deliverable` events, unioned by
  path with the file_write set (the data is already parsed nearby at `:155-198`).

**G3 — Post-task schedule affordance: syntax too specific/weird, doesn't work well. CONFIRMED.**
- The whole path is CRON despite the `rrule` name (`schedule_models.py:3,26-33`: the field stores a
  5-field cron expr, cronsim-parsed at `schedule.py:42-61`). The brittle input parser is
  `parseScheduleNL` (`frontend/src/lib/scheduleNL.ts:216-260`): a fixed table of ~13 ANCHORED
  regexes (`NL_PATTERNS` `:107-206`). Only exact phrasings match ("every day at 9am" yes;
  "9am daily"/"weekdays at 9"/"8:30pm" → fall through to the error at `:254`). Three competing input
  formats (cron, RFC-2445-ish, NL) are accepted but the backend only understands cron; the field is
  misleadingly named `rrule` everywhere; the placeholder even shows raw cron (`ScheduleSection.tsx:276`).
  `looksLikeCron` (`scheduleNL.ts:24`) checks only charset/field-count, not ranges → invalid cron
  passes the frontend and fails later at backend preview.
- Fix: preset buttons emitting known-good cron ("Daily 9am", "Weekdays", "Weekly Mon", "Hourly") so
  users never type syntax; add a real NL→cron pass (or a small parser) behind the free-text box;
  tighten client-side cron validation to mirror cronsim ranges; rename the user-facing concept to
  "cron schedule" (or hide cron behind presets). Build on the existing next-3-runs preview card
  (`ScheduleSection.tsx:198-217` + `/api/schedules/preview`).

---

## Status tracker (live, updated as items land)

Legend: ☐ not started · ◐ code-complete + all gates green + committed · ✅ live-verified in the app.

- ✅ WALK-05 — ddgs label (live: HAS_DUCKDUCKGO=false) · commit b48f5bf
- ✅ WALK-21 (settings note) — single-speaker default note (live-verified) · commit b48f5bf
- ✅ WALK-02 — planning loader (live: "Planning the research…" caught in the gap) · b48f5bf
- ✅ WALK-03 — citation-leak fixed (live: finished report rawCiteMarkers=[], "Disputed 1"
  note renders to [n]) · b48f5bf
- ✅ WALK-04 — follow-up markdown render (live: follow-up answered, no raw markers) · b48f5bf
- ✅ WALK-11 — plan-loader decouple (live: planLoaderRespun=false on follow-up) · b48f5bf
- ◐ WALK-01 stream markdown · WALK-08 stale-follow-up (committed b48f5bf; live DR confirmed
  no stale follow-up appeared pre-report — WALK-08 implicitly verified)
- ◐ WALK-06 disputed-notes scrub · WALK-07 base_url hygiene (committed b48f5bf)
- ◐ WALK-09 deliverable-card gate · WALK-10 Open-button wake · WALK-16 artifacts tab
  (committed b48f5bf; live build run pending)
- ◐ WALK-13 TTS download notice · WALK-14 AudioPlayer · WALK-21 mode popup
  (committed b48f5bf; live audio run pending)
- ◐ WALK-15 slides steering · WALK-17 schedule presets (committed b48f5bf; live pending)
- ☐ WALK-12 follow-up progress (Wave 2) · WALK-18 pause/resume (Wave 1b engine) ·
  WALK-19 no-progress breaker (Wave 1b engine) · WALK-20 export-include-follow-up (Wave 2)

Closed-by-investigation (no work): F3 surgical tools present; E1/E2 driver+encoders not the spin.

---

## Section 3 — Consolidated search list additions

Folded into the standing register (`docs/remaining-items-consolidated-2026-06-15.md`). Grouped
quick-wins vs. larger work; **P0** = breaks/confuses a core flow, **P1** = quality/UX, **P2** =
feature.

### Quick wins (small, localized, high signal-to-effort)
- **WALK-01 (P1)** Stream markdown live in basic search — `AnswerDocument.tsx:40-45` render via `<Markdown>`. [A1]
- **WALK-02 (P1)** Render DR follow-up answers through `<Markdown>` — `DeepResearchSurface.tsx:326-336`. [A3]
- **WALK-03 (P0)** Stop the `[[id]]` citation leak in the "Sources disagree" callout — wrap `DeepReportView.tsx:174` in `CitedText`; strip on export `deepResearch.ts:167`. [C1]
- **WALK-04 (P1)** DR "Planning…" loader for the submit→first-plan gap — `DeepResearchSurface.tsx` ~`:233`. [A2]
- **WALK-05 (P1)** Rename UI "Bundled — DuckDuckGo" → "Bundled — ddgs" — `DataSourcesSection.tsx:29`. [E4]
- **WALK-06 (P1)** Tighten `_extract_disputed_notes` — drop the `however…sources?` branch, require a conflict cue + `[[id]]`, reject hedge-only notes; conditional "Sources disagree." label; forbid platform-name attribution in `_SECTION_PROMPT`. — `synthesis.py:170-189,249-260`. [C2,C3]
- **WALK-07 (P1)** Null the persisted `base_url` when a provider flips to a bundled tier (search/extraction) — kills the latent LAN-spin re-trigger; `save_search`/`save_extraction`. [E3 hygiene]
- **WALK-08 (P0)** Fix stale "Follow-up: <query>" before report — bail follow-up derivation to `[]` when `report==null`, gate panel on FINISHED, dispatch reset on `session===null`. — `useDeepResearch.ts:178-186`, `useDeepResearchStream.ts:145-147`, `DeepResearchSurface.tsx:317`. [A4]
- **WALK-09 (P0)** Gate the build Deliverable card on `b.status==="FINISHED"` — `BuildSurface.tsx:367-379`/`buildTrace.ts:435-449`. [F1]
- **WALK-10 (P0)** Route the build "Open" button through the waking subdomain/`wake_for_preview` instead of the deprecated `preview-app` path — `BuildSurface.tsx:372`, `routes/preview.py:33-43`. [F2]

### Medium (one subsystem, a few files)
- **WALK-11 (P1)** Decouple the follow-up indicator from the plan loader — separate `followUpStatus` keyed on `StatusEvent.detail`; gate `DeepProgressStrip` on "plan run active." [B2]
- **WALK-12 (P1)** Emit follow-up progress events + a dedicated follow-up status section ("what the model is doing") — backend `_follow_up_deep_research` phase events + frontend component. [B1]
- **WALK-13 (P1)** First-run TTS download progress — `reporthook` on `urlretrieve` + ephemeral `tts_progress` frames + drive the existing (dead) `progress` render; min. a static "downloading model…" notice. [D1]
- **WALK-14 (P1)** Custom `AudioPlayer.tsx` on the design system; replace the native `<audio>` — `NeedMoreCard.tsx:399`. [D2]
- **WALK-15 (P1)** Steer the agent to artifact tools at execution time — add `slides_generate`/`sheet_generate` to `_EXECUTION_DRIVER_PROMPT` — `prompts.py:187-246`. [G1]
- **WALK-16 (P1)** Broaden `deriveFiles` to enumerate `observation`/`deliverable` artifacts in the Artifacts tab — `buildTrace.ts:305-329`. [G2]
- **WALK-17 (P1)** Schedule UX — preset buttons (known-good cron) + client-side cronsim-range validation + rename to "cron"; optional NL→cron — `scheduleNL.ts`, `ScheduleSection.tsx`. [G3]

### Larger / engine-risk (design first)
- **WALK-18 (P0, ENGINE)** Real pause/steer/resume — wire a `pause` frame + `ControlOps.pause` + cooperative pause flag checked INSIDE the driver step; fix the idempotent-`kick`-over-stale-task resume drop; align resume legality with frontend `canResume`. — `ws.py:78`, `engine.py:1491-1499,1054-1073`, `runtime.py:1222-1224`, `resume_service.py:345-375`. [F5]
- **WALK-19 (P0, ENGINE)** Semantic no-progress breaker for build/iterate — failure-independent, NOT assist-gated; trip on recurring verify/screenshot symptom across N varied edits; lower the build-path iteration ceiling (default `max_iterations=500`). — `loop/stuck.py`, `loop/turn_control.py:307-321`, `engine.py:461`. [F4]
- **WALK-20 (P2, FEATURE)** Export/audio after follow-up: include follow-up text + a pre-action "include which/how many follow-ups?" modular popup, then the normal export/audio popup; thread selection into all serializers. — frontend `NeedMoreCard`/`DeepResearchSurface`/`useDeepResearch`/`deepResearch.ts` (+ new modal); backend `deep_research_service.py`/`report.py`/`report_export.py`/`report_audio.py`. [B3,B4]
- **WALK-21 (P2, FEATURE)** Audio: podcast-vs-single-speaker popup (Podcast = ALPHA) + single-voice "honest walkthrough" mode using `voice_a` as default; mode-aware cache key; Settings note that voice_a is the single default. — `report_audio.py`, `audio_overview.py`, `report.py`, `NeedMoreCard.tsx`, `settings/AudioSection.tsx`. [D3]

### Closed by investigation (no work)
- **F3** Surgical line-edit tools (`file_edit`/`file_replace_lines`/`file_insert_lines`) are present and wired for any model — Dylan's "removed" recollection is incorrect.
- **E1/E2** The deepseek-v4 driver and the encoders were NOT the mini-PC spin cause; Settings was truthful. Cause was persisted crawl4ai/searxng (E3), already fixed by switching to bundled.

