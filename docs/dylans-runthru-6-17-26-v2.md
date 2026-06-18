# Dylan Runthru — 6-17-26 — Version 2

> Structure mirrors v1:
> 1. **Section 1 — Verbatim notes.** Copied EXACTLY, not one word changed. Do not edit.
> 2. **Section 2 — Root-cause breakdown.** Per-issue, with file:line + trace evidence.
> 3. **Section 3 — Plan.** Sequenced, with the open-questions (separate agent vs build,
>    OSS pptx harvest) called out.

---

## Section 1 — Verbatim notes (DO NOT EDIT — copied exactly)

okay, next runthru time. for my first action, i performed a basic search on small <1b llms. the sources didnt have any info, so the answer basically just said the provided sources dont answer it. im glad that it isnt hallucinating info, but also, thats just a waste if no real answer is given. why would we not just continue searching until a source is given, and it can answer the question at least. its pointless right now. Sidenote, we should tune the system prompts for all the models invloved in search and deep research, the ones that make sub questions and the main driver, to include a function where it injects the current time and date and to specifically look for the latest info on a topic. we just need to push the result towards recency. next i ran a small deep research report. many of the things here are fixed, so good work. for the audio overview, i noticed something a little bit off- the audio report was given special characters that dont align to natural language. there was a small section there where the transcript had given the tts something about the y axis, but it have it "*Y* aixs" - this makes sense for emphasis when reading it, but that shouldnt be coming over to the tts. we should create a filter here to prevent this, and other things that could reasonably make it in to the transcript. This should apply to both the single voice and podcast styke approach. Additionally, in all the TTS stuff, any acronym (LLM, API, etc) needs to be defined fully the first time its referenced in the overview (specifically, it doesnt need to define it every time, but standard language dichotomy demands that it should be defined at least once, the first time its mentioned, so that everyone is on the same page. it would be annopying to have it fully defined every time though). next, for the follow up section, i do notice that the fixes i requested are implemented, and they work well. however, the follow up answer includes bracketed text like "238afc_p11" inline. im not sure what that is, but it needs work as that text shouldnt appear in the ui like that. also, for the follow up section, the export option now gives you the choice to include follow ups- but critically, it doesnt actually export the follow up. it is still the same. what gives? thats a half baked fix. update on this- the pdf section does actually include the follow ups. the markdown doesnt. youll need to verify the docx actually does too. additionally, the audio overview is identical to the first iteration. updated follow ups arent pushing there either. BIG FINDING - for the issue with the markdown not exporting the most recent follow ups as well as the main report, this issue is ONLY IN THE BOTTOM EXPORT! at the top of the page, when i export by markdown, it is indeed updated. so it looks like this just wasnt fully fleshed out. for the docx, i just verified that it does not include any updated follow ups. i installed libre office and it does not show follow ups. also, the formatting is kinda messy. this is something we should clean up, same with pdf. use a nice, standardized format that looks clean and minimalistic. also, for the audio overview, pdf, and docx, every export, regardless of which button i press, should have the personalized name attached to the downloaded/exported file. right now, many of them are just conv_, and the audio is just a default name. it needs to be personalized to the conversation and context of the actual report. this is mostly just polished, but its what seperates the cheap vibecoded apps from what we are building here. okay, on to the build surface. it looks like now the preview page is broken. i see some text on there from the app but virtually none of the actual visual design. also, the steps arent getting checked off still. also, we should put in the system prompt for the build agent to steer away from monolithic files. also, there is something going on that still loops the agent from checking files, verifying the build, checking the files again, verifying again, saying it needs to verify the same file over and over, going back to testing the site again etc... the testing portion needs to happen in one swoop at the end, and if issues surface bring it back to build. not just a constant bouncing between. it also continually keeps verifying all the files are up on the disk, then saying "now let me read x file", what is that? the looping here is insane. we still have some serious issues with our build harness that makes this so painful. if you need to, look in to what openhands or the other open source references - but dont revert the assisted function we are only using in small models. good god man, it keeps saying all of it is there, then saying let me read the truncated whatever, or let me look at file x or y, then reading it, then testing, then reading over and over and over again. look at the actual trace for this and spin up a subagent opus to root cause this. use two infact to form a consensus. this build issue is persistent and basically breaks the whole thing. the preview issue is also a big one, and it sucks man. also, the live server still does not show the actual result, it still says preview not avaliable. good god man, why does it continually verify then go to test, then back to verify, then back to test, then back over and and over and over agian. OKAY, THIS IS INFURTIATING, IM GOING TO AGENT MODE NEXT. FIRST THING I NOTICE, THE BUILD, AGENT, AND RESEARCH MODE DO NOT HAVE THE ABILITY TO ATTACH FILES IN THE ACTUAL INITIAL CHAT BOX. THATS A FEATURE WE ARE MISSING. I KNOW THERES ATTACHEMENTS FOR BUILD IN THE STEER OR PLAN STEPS, BUT WE ALSO DONT KNOW IF THAT EVEN WORKS OR GETS INJECTED TO THE AGENT IN A WAY IT CAN READ IT. FOR DEEP RESEARCH, THE ATTACH SHOULD BE MORE LIKE SOURCES THAT IT MUST LOOK OVER FOR THE RESEARCH, AND INCLUDE IT IF IRELEVANT IN THE REPORT. FOR THE REST OF THEM, THEY SHOULD BE REFERENCES OR WHATEVER IS RELEVANT FOR THESE SURFACES TO MAKE THE ATTACHED FILES USEFUL. for the agent mode, it produced html slides - i thought we had it worked out to make actual pptx or actual google slides? what happened? the powerpoint being fully html works, but its mis-sized and the viewpoert on my browser doesnt show the full thing, it looks like it got clipped. it also just looks bad. this needs to be at least good looking for all the artifacts it makes; manus outputs some really good looking slide decks. but its not just the slide decks, but whatever is  the agent is capable of producing also, for the artifacts, the html file it outputted is just a white screen in the artifacts. it should be manipulatable, not just static, and to staert, it should actually render man additionally, the artifacts page shows the file itself, but it says 0b for zero bytes, this is probably the same disease affecting the white screen. so an update on the pptx slides, after i steered the agent it looks like it downloaded some python script for making pptx. which is cool, but this suffers from the same disease of build mode where the loop doesnt contribute towards actual progress. this is something that should be basic man. it should be able to make a slide deck in a few minutes, on the first prompt. its not just the slides either, thats just what im poking at. theres some significant issues here.  also, the ootx it is making in these drafts is just a white slide with one slide only that says less than 15500 chars elided - already applied; use file_read for content. i dont have the patience to let this play out, but this is a big feature that just sucks and takes forever, and never actually works. annoying. look into the traces and spin up subagents to root cause this. use minimax where you can, and verify the root cause. this should not be this painful. if we need to actually seperate the agent surface from the build surface, just say that man.  the thing with powerpoints or any actual creation of these artifacts is that ywe are leaving it to open to the llm deciding everything. there should probably just be a barebones option it can write to and edit, instead of leaving the whole thing open to interpretation. check and see if any opewn source progreams on line do this, or if theres any open source ai powered pptx or google slides creator with a good mit or apache license that we can harvest from here. research what the other programs are doing. we have made a lot of progress but we still arent there on this stuff. it needs deep attention and care to fix. save these verbatim notes as dylan runthru 6-17-26 vversion 2. then build the plan to figure this all out.

---

## Section 2 — Root-cause breakdown

Traced by 8 parallel read-only investigators (incl. **two independent Opus agents that
reached consensus** on the build loop) against the code AND the live `disco.db` traces.
Tags: **CONFIRMED** (mechanism read in source + trace) / **HYPOTHESIS**.

### A. THE KEYSTONE — `_snip_args` corrupts executed tool args (CONFIRMED, both Opus agents)

**Not F8** (my first guess was wrong). The culprit is the **ungated** `_snip_args`
shaper: `packages/core/src/disco/core/events.py:287-294`, dispatched from
`ActionEvent.to_llm_message` at `events.py:251`, firing on **any** tool string arg
> `_ARG_SNIP_CHARS=1500` (events.py:284), **for every model, every tool, ungated by the
assist tier** (so it hits even capable models with assist off).

Mechanism = an **echo loop**, not a direct data path: `_snip_args` renders a prior big
arg in *history* as `"<{N} chars elided — already applied; use file_read for the content>"`.
The weak model then **copies that exact placeholder string** into a *new* tool call. The
new ActionEvent is stored with `arguments=<marker>` (~70 chars, never re-elided) and
**dispatched to the executor**. Trace proof (`conv_06eb76f3…`): seq 81 writes real 12,417-char
md → seq 91 `slides_generate(markdown="<12,417 chars elided…>")` (the exact 12,417 count is
echoed) → garbage 1-slide deck; seq 176 `file_write(content="<11,995 chars elided…>")` →
**obs "wrote 72 bytes" — the placeholder overwrote the 12 KB deck (data loss)** → seq 185
`file_read` returns the placeholder which literally says "use file_read for the content"
→ re-read loop. There is **no execution-time guard** rejecting a placeholder-as-argument.

**This single bug breaks slides, file-writes, artifacts, and drives much of the read loop.**
Fix (keystone): at the tool-executor boundary, reject/rehydrate any arg matching the
elision-marker regex (for `file_write`/`file_edit`: rehydrate from the last confirmed full
value via `_f8_confirmed_file_writes` or disk; for `slides_generate`/others: emit an
AgentError telling the model to `file_read` and resupply). Reword the marker so it points to
the in-context **WORKSPACE snapshot** (not "use file_read") and is self-evidently a
system note never to be copied. Keep elision on history (context savings preserved).

### B. Build read-thrash loop (CONFIRMED, both Opus agents)

- **RC2 — snapshot too small for the project.** The A8 workspace snapshot caps at
  `_WS_MAX_FILES=8` / `_WS_PER_FILE_CHARS=6000` / `_WS_TOTAL_CHARS=16000`
  (`view_render.py:41-43`). A 13-file project overflows it every turn → 5 files omitted +
  big files truncated each turn, and the truncation/omission markers literally instruct
  "file_read this path" (`view_render.py:195,232`). Result (`conv_6483…`): 82 file_reads,
  `windows.js` re-read 8-24×. Fix: scale the snapshot (cap by total chars, not a hard 8) +
  short-circuit a `file_read` whose current content is already in this turn's snapshot.
- **RC3 — no breaker sees redundant *reads*.** Zero stuck/no-progress events fired in 234
  events. `repeated_verify_no_progress` (stuck.py:334) only counts *edits* vs probe
  outcomes; a read-heavy loop has no edits → never trips. Patterns 1/4 need byte-identical
  adjacency / A-B-A-B, defeated by offset/path variation. Fix: add a read-thrash breaker
  (same path read ≥N× with no intervening mutation / content already in snapshot).
- **RC4 — "steps not checked off"** is downstream lag: `plan_step` completion works, but
  step 1 sat "active" from seq 18→163 behind the read-thrash; plus plan-state thrash
  (re-marking done steps). Fix mostly falls out of RC2/RC3; add an illegal-transition warn.
- **RC5 — no anti-monolith / single-verify-pass steering.** `<file_rules>` (prompts.py:236-253)
  only explains how to *edit* big files. Fix: add "author many small focused files (<~300
  lines); BUILD first then VERIFY in one pass at the end — the workspace snapshot is
  authoritative, don't re-read files you just wrote" to `_EXECUTION_DRIVER_PROMPT` (+ `_SMALL`).

### C. Agent surface / artifacts (CONFIRMED)

- **Slides HTML-not-pptx + "white slide saying chars elided":** `slides_generate` IS reached
  (WALK-15 steering worked — 3 calls in the trace) but calls 2 & 3 received the elided
  placeholder as markdown (Keystone A) → Marp rendered the placeholder as a 1-slide deck,
  `success=True`. Fix: guard `args.markdown` for the elided pattern in `slides.py` → explicit
  failure. (Editable pptx is "v2"; today pptx is image-per-slide — see slides architecture.)
- **Artifacts "0 B" + white screen** (`buildTrace.ts` + `AgentCanvas.tsx`): slides/served
  artifacts live server-side; `deriveFiles` records `content=""` → 0 B; `deriveSrcDoc`
  returns `""` (not null) → `"" != null` → iframe renders `srcdoc=""` → blank. The download
  route always sets `Content-Disposition: attachment` so an iframe `src` would download, not
  render. Fixes: (a) `deriveSrcDoc`: `if(!entry.content) return null`; (b) add an
  `?inline=true` artifact route (no attachment header) + point the ArtifactsPane iframe at it
  → real interactive Marp preview (Marp HTML is self-contained + keyboard-navigable).
  "Manipulatable" beyond Marp's built-in nav needs per-slide content in the structured payload.
- **Deck quality:** the **Marp** path is already good (113 KB self-contained, `uncover` theme,
  16:9). The **fallback** renderer (`slides.py:_fallback_html`) is poor (vertical scroll, no
  16:9, no nav). Fixes: fixed 16:9 + scale JS for the fallback; iframe needs `aspect-ratio:16/9`.
- **Separate Agent from Build? RECOMMENDATION: NO.** Both share `BuildAgent`/`_compose_build_loop`
  (runtime.py:690); differences are cosmetic (prompt flavor, egress, skills). Instead add an
  **`artifact_mode` flag** to `_compose_build_loop`: swap `BlastRadiusConfirm`→`NeverConfirm`,
  `OperatingMode.PLANNING`→`INTERACTIVE`, restrict tools to artifact tools (slides/sheet/
  image/file/search) — a ~5-line gated change that gives "research → make artifact in 2-5
  steps, no plan gate" without duplicating a whole surface.

### D. Deep Research exports (CONFIRMED)

- **S1 — bottom MD ≠ top MD:** the NeedMoreCard ExportModal non-FSA branch (Firefox!) calls
  `exportReportAsMarkdown(report)` WITHOUT `followUpSeqs` (`NeedMoreCard.tsx:154-159`). Top bar
  threads them; the bottom non-FSA path drops them. Fix: thread followUpSeqs in that branch.
- **S2 — DOCX excludes follow-ups in 4 layers** (frontend `fmt!=="docx"` guards ×2 +
  `routes/report.py:119` `fmt in ("md","pdf")` + `_render_docx_in_sandbox` no follow_ups arg)
  even though `serialize_docx` accepts them. Fix: add docx at all 4 layers.
- **S3 — audio always identical:** `AudioModeDialog` is only mounted in the `audio.status==="idle"`
  branch (`NeedMoreCard.tsx:555-565`); once a deck/audio is "done", clicking Audio sets
  `modeOpen=true` but the dialog isn't rendered → request silently dropped → old audio served.
  Plus a cache-key bug: empty `follow_ups` list → `fu_suffix=""` → same filename → cache hit.
  Fix: render `AudioModeDialog` unconditionally; reset audio→idle on a new request.
- **S4 — messy formatting:** PDF uses `nl2br` (report_export.py:118) → spurious `<br>`; DOCX
  pandoc has no `--reference-doc` → Office defaults. Fix: drop nl2br + clean print CSS; bundle
  a clean `reference.docx`.
- **S5 — filenames:** server `Content-Disposition` uses `report-{cid}` (routes/report.py:199),
  non-FSA download uses `report-${cid}` (deepResearch.ts:116), audio hardcoded
  `"audio-overview.mp3"` (NeedMoreCard.tsx:588), audio cache name internal. Fix: derive a slug
  from `report.query` for all four (FSA paths already do this correctly).

### E. TTS hygiene (CONFIRMED)

- **Raw markdown to TTS** (`*Y*`): no normalization between `turn.text` and synthesis
  (`audio_overview.py:437/442`, `report_audio.py:352/359`). Fix: add `_normalize_for_tts()`
  (strip md links→text, `[[id]]`/`[n]` citations, `**`/`*`/`_`/backticks, headings, bullets;
  collapse whitespace) applied at both synth call sites; covers single + podcast.
- **Acronyms undefined:** prompt-only fix — add "expand each acronym in full on first mention,
  short form after" to `_TURN_SCRIPT_PROMPT` and `_SINGLE_SCRIPT_PROMPT` (audio_overview.py).
- **Follow-up citation leak `238afc_p11`:** one-char bug — `deep_research_service.py:701` labels
  passages with single `[{pid}]` while instructing `[[id]]`; the LLM copies single brackets,
  which the `[[…]]` CITE regex (Markdown.tsx) doesn't catch → leaks. Fix: `[[{pid}]]`.

### F. Search quality (CONFIRMED)

- **No keep-searching on a non-answer:** basic research is linear search→extract→generate→return
  (`streaming.py`), no non-answer detection, no reformulate-retry (only an extraction-failure
  retry). Fix: after generation, if 0 grounded claims (or all unsupported) → reformulate the
  query and re-search, bounded ≤2 rounds (≤3 total searches).
- **No recency steering:** zero date injection anywhere (6 prompt sites: `_answer_prompt`,
  decompose `_PROMPT_TEMPLATE`, `_GAP_PROMPT`, `_SECTION_PROMPT`, `_COHERENCE_PROMPT`,
  grounding `_prompt`); ddgs supports `timelimit="d/w/m/y"` and SearXNG `time_range`, neither
  passed. Fix: inject current date at prompt-build time + a recency directive at each site;
  default ddgs `timelimit` for research queries.

### G. Attach files (CONFIRMED — feature gap, not a bug)

- Build/Agent steer-attach **works end-to-end** (UploadComposer → `POST /conversations/{cid}/files`
  → sandbox `uploads/` + condensation-immune `DatasourceEvent` → `file_read`; 51 tests). It's
  just **absent from the INITIAL chat box** (UploadComposer only renders after `cid` exists).
  DR + basic research have **zero** attach UI.
- Seam: pre-create the `cid` on surface mount (backend already supports upload-before-kick via
  lazy pending session) → render UploadComposer in the empty state. DR: read uploads → chunk to
  `Passage`s (`corpus_id="user_upload"`) → upsert into the run's vector store (files-as-sources,
  cited). Basic: add `seed_passages` through `stream_research_answer`. PDFs need an extractor
  (v1: accept plaintext/md/csv only).

### H. Slides architecture (research — DECISION REQUIRED)

OSS survey + Presenton (Apache-2.0). The highest-quality pattern (and exactly Dylan's
"barebones schema it writes into") is **constrained typed slide-schema JSON → deterministic
`python-pptx` adapter filling a designer-built .pptx Slide Master → pptx + pdf(LibreOffice) +
html**. Harvest: `slide-deck-ai` (MIT, the schema), `pptx-template` (Apache, the adapter),
`python-pptx` (MIT, can load+edit real templates). **Presenton** (Apache) is a full FastAPI+Next
service that ALSO does image-gen + data reports + themes, but generates slides via markdown
(weaker than strict schema). Options A (Presenton sidecar) / B (lean python-pptx schema tool) /
C (hybrid: B for slides + Presenton sidecar for images/reports). See §3.

---

## Section 3 — Plan

**Keystone insight:** fixing **A (`_snip_args`)** un-breaks slides, file-writes, artifacts, AND
much of the build loop at once. Everything else is sequenced behind it. All engine fixes are
ADDITIVE (preserve the small-model assist kit). Each item ships through the constitution
(basedpyright 0, lint-imports, arch budget, vitest/pytest) + live verification.

### WAVE 0 — Keystone (unblocks everything) — P0, engine
- **K1.** Execution-time guard: reject/rehydrate any tool arg containing an elision/snapshot
  marker, before dispatch (files.py write/edit rehydrate from last-confirmed/disk; others →
  AgentError). Reword the markers to point to the workspace snapshot, never "use file_read".
  *Single highest-value fix in the whole runthru.*

### WAVE 1 — Build loop health — P0, engine (after K1)
- **L1.** Read-thrash breaker (redundant successful reads / content-in-snapshot). [RC3]
- **L2.** Scale the workspace snapshot (total-char budget; stop truncation markers reading as
  "re-read me") + short-circuit reads already satisfied by the snapshot. [RC2]
- **L3.** Prompt steering: small-focused-files + "verify once at the end, snapshot is
  authoritative" in `_EXECUTION_DRIVER_PROMPT` (+ `_SMALL`); plan-state illegal-transition warn. [RC4/RC5]

### WAVE 2 — Artifacts render + lightweight artifact mode — P1
- **A1.** `slides_generate` elided-arg guard (slides.py). [redundant safety under K1]
- **A2.** Artifacts white-screen/0B: `deriveSrcDoc` null fix + `?inline=true` artifact route +
  ArtifactsPane iframe → real interactive preview.
- **A3.** Fallback renderer 16:9 + scale JS; iframe aspect-ratio. (Manus-quality.)
- **A4.** `artifact_mode` flag on `_compose_build_loop` (NeverConfirm + INTERACTIVE + artifact
  toolset) — the "don't separate the surface, add a lean artifact path" answer.

### WAVE 3 — Slides architecture — **DECISION: A / B / C** (recommend C)
- **B-path:** new `slides_schema` tool — LLM emits typed JSON (layout|title|bullets|image) →
  Pydantic validate → `python-pptx` fills a designer 16:9 Slide Master → pptx+pdf+html. Build a
  clean light + dark theme master. Replaces image-per-slide with editable decks.
- **A-path:** Presenton FastAPI as an optional sidecar (compose), LLM=our gpt-oss-120b, called by
  the slides/report tools. Also unlocks image-gen (product idea #1) + data reports.
- **C (recommend):** B for slide quality + A sidecar for images/data-reports.

### WAVE 4 — Deep Research polish — P1
- **E-cluster (TTS):** `_normalize_for_tts` filter + acronym prompt + the 1-char follow-up
  citation fix (`[[pid]]`).
- **D-cluster (exports):** bottom-MD follow-ups (S1), DOCX follow-ups ×4 layers (S2), audio
  regenerate/mode-dialog (S3), PDF/DOCX clean formatting (S4), personalized filenames ×4 (S5).

### WAVE 5 — Search quality — P1
- **F1.** Keep-searching-until-answered loop (non-answer detect → reformulate → re-search, ≤2).
- **F2.** Current-date injection + recency directive at all 6 prompt sites + ddgs `timelimit`.

### WAVE 6 — Attach files (feature) — P2
- **G1.** Pre-create-cid pattern → UploadComposer in the initial box (build/agent).
- **G2.** Deep Research files-as-sources (uploads → Passages → vector store, cited).
- **G3.** Basic research `seed_passages`. (PDF extractor later.)

### Open decisions for Dylan
1. **Slides architecture: A / B / C?** (recommend **C**.)
2. **Separate Agent from Build?** Investigation says **no — use the `artifact_mode` flag (A4)**.
   Confirm you're OK with that instead of a separate surface.
3. Execution order: the plan above is the recommended order (keystone → loop → artifacts →
   slides → DR polish → search → attach). Confirm or re-prioritize.

---

## Section 4 — Dylan's confirmations + nuances (SOURCE OF TRUTH, 2026-06-17)

Claude restated every flagged issue (comprehension check); Dylan confirmed each and added
nuance. This section is authoritative for what each issue means and any added direction.
(Numbering follows the runthru order; #3 and #32 were initially skipped by Claude — restored.)

- **#1 (search dead-ends on no-answer) — YES.** Keep searching until a real answer exists.
- **#2 (recency steering) — YES.** PLUS evaluate **last30days-skill** (github.com/mvanhorn/last30days-skill,
  MIT) — adapt its *function* (native recent-mode per source + date window; v2: entity-first
  resolver), **maybe as a toggle**. Don't port the 12-platform scraping; build a Recency toggle on
  ddgs `timelimit`/SearXNG `time_range` + date injection.
- **#3 (acknowledgment: "many things fixed, good work") — positive, not an issue.** Restored for completeness.
- **#4 (markdown/special chars to TTS) — YES.** Filter, both single + podcast.
- **#5 (acronym expand on first mention) — YES.**
- **#6 (`238afc_p11` leak in follow-up) — YES.**
- **#7-10 (export include-follow-ups half-baked; only bottom MD broken; docx missing; audio stale) — YES, exactly.**
- **#11 (clean PDF/DOCX format) — YES, BIGGER:** **template it** — a **modular, branded export
  template** that handles follow-ups + big reports, which the exporter just fills in. Make it
  **uniquely ours, stylistically.** (Scope grows from "tidy CSS" → "design Disco's own report template system.")
- **#12 (personalized export filenames) — YES.**
- **#13 (preview renders text, no visual design) — YES.**
- **#14 (steps not checked off) — YES, persistent, "really needs a good root cause."** → The
  unbiased reviewers FOUND it: C18 `file_exists` resolves against a None `workspace_path` →
  checks the agent-server CWD, not /workspace → false "missing" on every step (`plan_conditions.py:163-197`).
- **#15 (anti-monolith steering) — YES.** Theory: a multi-file project is **easier for the agent
  to manage**. (Note the irony the reviewers found: this correct steer tipped the 13-file project
  past the 8-file snapshot cap → the fix is to SCALE the snapshot, not discourage multi-file.)
- **#16-19 (the build read/verify/test loop) — YES. "Root-cause the HARDEST, stop letting it
  slide. This is a BREAKING issue."** → Root-caused by 2 unbiased Opus reviewers (consensus) in
  `docs/build-harness-review-6-17-26.md §7`: context-mgmt thrash + breakers blind to read loops +
  broken browser/vision + false plan-checks.
- **#18 (harvest from OpenHands/OSS) — YES.** Web research allowed, **but conclusions must be
  concretely verifiable** (no hand-wavy harvesting).
- **#22/#23 (no attach in initial box; unsure steer-attach works) — YES.** (Reviewer found
  steer-attach DOES work end-to-end; just missing from the initial box.)
- **#24/#25 (attach semantics: DR=sources-to-cite, others=references) — YES.**
- **#26 (HTML slides not real pptx) — YES.**
- **#27 (artifacts must look good, Manus-tier) — YES; "maybe our recent gold mine (Presenton) solves this."**
- **#28/#29 (artifact white screen / 0 bytes) — YES.**
- **#30/#32 (pptx loop disease + far too slow) — YES.** #32 distinct: **the whole artifact feature
  "sucks and takes forever and never actually works"** — should make a deck in minutes on the first prompt.
- **#31 (slide content = "chars elided" placeholder) — YES.** This is the `_snip_args` slides bug.
- **#33 (separate Agent from Build?) — YES, "I just want to know if that's part of our disease."**
  → ANSWER: **No.** The build loop is context-management + breaker-blindness, not surface coupling.
  Recommendation stands: add an `artifact_mode` flag (NeverConfirm+INTERACTIVE), don't split surfaces.
- **#34 (constrain artifact gen to a barebones schema) — YES, BUT NEEDS RESEARCH.** Dylan: "we've
  previously solved issues by making them LESS deterministic; not sure if more-deterministic
  (constrained schema) is the viable solution here — investigate." → So the constrained-schema vs
  freer-generation choice is an **open research question, not a settled decision.** Validate
  empirically (which produces better decks with our models) before committing.
- **#35 (research OSS pptx generators) — YES.** (Done: Presenton + slide-deck-ai + pptx-template +
  python-pptx; see runthru §2.H.)

## Section 6 — MASTER PLAN (draft, built from 5 research lanes + 3 Opus plan-builders + 2-reviewer build consensus)

Research digest: `docs/runthru-v2-research-digest.md`. Build root-cause: `docs/build-harness-review-6-17-26.md §7`.
Three independent tracks; build-harness gates the artifact track; Deep Research is fully parallel.

### Sequencing overview
```
TRACK A — BUILD HARNESS (the breaking issue, engine-risk)        ── highest priority
  K1  _snip_args execution-guard (slides/artifact corruption gate)   [small, gates Track C]
  Phase 0  regression harness (pure-fn metric tests) + macOS golden fixture
  Phase A  6 PARALLEL quick-wins (stop the bleed)
  Phase B  durable redesign (sequenced after A proves it)
TRACK B — DEEP RESEARCH POLISH (parallel, low engine-risk)       ── 2nd worker, independent
  DR-1 correctness one-liners + export/audio fixes
  DR-2 branded export template      DR-3 recency toggle      DR-4 attach files
TRACK C — ARTIFACTS & SLIDES (gated by K1 + Phase A)             ── after the bleed stops
  Phase 0  constrained-vs-free EXPERIMENT (gates the schema)  [parallel-OK]
  Phase 1 slides v2  Phase 2 render/preview  Phase 3 artifact_mode  Phase 4 image-gen  (Phase 5 sidecar = optional de-risk)
```
**The one hard cross-track rule:** K1 (the `_snip_args` execution-guard) must merge before any Track-C
artifact path ships — the elision bug corrupts every new artifact identically (it's what put
"<N chars elided>" on the slide). K1 is small; do it first.

### TRACK A — BUILD HARNESS (full detail from PB-1)
- **K1 — `_snip_args` execution-guard** (`core/events.py:287-294` + tool executor + `files.py`): reject/
  rehydrate any tool arg containing the elision marker before dispatch; reword the marker to point to
  the in-context snapshot (never "use file_read"). Gates Track C. *S, MED risk.*
- **Phase 0 — regression harness** (`core/tests/loop/test_build_loop_regression.py` + fixtures, optional
  golden from `docs/evidence/macos-build-trace-6-17-26.txt`): encode the acceptance metric table so the
  loop death is provable without a live model. *M, no risk.*
  - **Acceptance metric table (macOS-clone):** file_read 82→**<20**; max reads/path 15→**≤3**;
    "haven't-seen/truncated" thoughts 91→**<10**; browser 30s timeouts ≥4→**0**; false plan "NOT met"
    many→**0**; events ~374(noop)→**<150 via finish()**; preview text/503→**CSS-bearing 200**.
- **Phase A — quick-wins (parallel; A1 shares a file with B1):**
  - A1 snapshot: budget-by-chars not 8-file cap; never evict deliverable files; stop "re-read me"
    markers; align the 6000/7000/8000 caps so an ~8KB file is "fully seen". (`view_render.py`, `files.py`,
    `view.py`) *M, MED.*
  - A2 read-aware breaker: new `repeated_read_no_edit` (hash tool+args, NOT thought; file-specific to
    dodge the wait-loop FP) → escalate-then-halt. (`stuck.py`, `turn_control.py`, `engine.py` call-site) *M, MED-HIGH.*
  - A3 un-gate F9 read-dedup for all tiers. (`dedup.py` caller) *S, LOW-MED.*
  - A4 C18 done-condition: resolve `file_exists` via the sandbox FS, not the agent-server CWD (the
    `None workspace_path` bug); also fixes the DoD gate skip. (`plan_conditions.py`, `finish.py`,
    `base.py`/`process.py` Protocol) *M, MED.*
  - A5 browser: text/CSS-selector clicks (not only `[data-pmx-index]`) + short timeout. (`browser.py`,
    `_browser_daemon.py`) *M, none.*
  - A6 preview: serve the subdirectory app (detect index.html dir) + keep alive after finish. (`session.py`,
    `preview_service.py`, `preview.py`) *M, none.*
  - A7 prompts: anti-monolith + single-verify-pass + vision-aware verify mandate. (`prompts.py`) *S, none.*
- **Phase B — durable redesign (sequenced):**
  - B1 structural-map + diff-since-last-step context (Aider/SWE-agent/OpenHands pattern; ~1-2k tokens
    regardless of project size); supersedes A1. (`new workspace_map.py`, `view_render.py`) *L, HIGH — behind a flag, A/B vs A1.*
  - B2 architectural finish-gate (OpenHands; one consolidated "build→verify once→back to build" check;
    interlock the existing caps). (`finish.py`) *M-L, MED-HIGH.*
  - B3 vision for verify (auto-advertise VISION for vision-capable drivers and/or route screenshots to
    the existing `vision_escalation_model` seam). (`config.py`, routing, `browser.py`) *M, LOW-MED.*
- **Live acceptance:** re-run `build a simple macosx clone` + the no-monolith steer on the disco-live
  runner; assert the metric table + a Playwright screenshot of the rendered macOS design.

### TRACK B — DEEP RESEARCH POLISH (full detail from PB-2)
- **DR-1 (ship first):** D1 follow-up citation 1-char fix (`deep_research_service.py:701` `[pid]`→`[[pid]]`);
  C1 `_normalize_for_tts` at both synth sites (single+podcast; transcript stays raw); C2 acronym-first-
  mention prompt; B1 bottom-card MD follow-ups via the server endpoint; B2 DOCX follow-ups at all 4
  layers; B3 audio mode-dialog-render + content-hash cache key; B4 personalized filenames ×4.
- **DR-2 export template** (after DR-1's shared-file edits): A1 bundle OFL fonts (Fraunces/Schibsted
  Grotesk/Newsreader); A2 `_build_pdf_html(report, follow_ups)` from the model + drop `nl2br` + print-CSS
  (cover/running header/numbered sections/follow-up page/sources appendix/TOC>10/2-col>30) +
  FontConfiguration; A3 styled `reference.docx` + pandoc `--reference-doc`. MD byte-parity preserved.
- **DR-3 recency toggle** (parallel, 2nd worker): E1 `time_filter` on the SearchProvider contract +
  ddgs `timelimit`/SearXNG `time_range`; E2 thread `recency_window` run→gather→request; E3 create-body +
  UI `RecencySelector` next to depth; E4 date+recency prompt injection (decompose=highest ROI). OFF =
  byte-identical.
- **DR-4 attach files** (P2, last): F1 pre-create-cid → UploadComposer in the initial box (build/agent/DR);
  F2 DR files-as-sources (uploads→Passages corpus_id=user_upload→vector store, cited); F3 basic-research
  seed_passages. v1 = plaintext/md/csv only.
- *(Note: "keep-searching-on-no-answer" #1/F1 is a separate small search wave — bounded reformulate-retry
  in `streaming.py` — tracked but not bundled here.)*

### TRACK C — ARTIFACTS & SLIDES (full detail from PB-3; gated by K1 + Phase A)
- **Phase 0 — #34 experiment (GATE, parallel-OK):** `harness/slides_experiment/` 15-20 prompts ×
  {free-form / rigid / loose-hybrid} × {gpt-oss-120b, weakest local}; score overflow+LLM-judge+blind
  human → `docs/slides-experiment-verdict.md`. Freezes the Phase-1 schema. *M, low.*
- **Phase 1 — slides v2 (loose-schema → native python-pptx):** add `python-pptx` dep; `_deck_schema.py`
  (loose typed `Deck/Slide`, layout_hint never rejected); `slides_generate` v2 `deck` path (keep markdown
  fallback); `_pptx_render.py` (~400 LOC: light+dark themes reusing the OFL set, 6-8 layouts ported from
  Presenton's general templates → real editable text boxes); PDF via LibreOffice-in-sandbox + self-
  contained 16:9 HTML; model-tier-aware prompt (`<artifact_tools>` one-pass for gpt-oss-120b, rigid
  example for small). *L, MED.* Image-per-slide is replaced by **native editable PPTX**.
- **Phase 2 — render/preview fixes:** `deriveSrcDoc` `if(!entry.content)return null` (white-screen);
  suppress "0 B"; `?inline=true` artifact route (html-only allowlist, no attachment header,
  sandbox=allow-scripts NO allow-same-origin); point ArtifactsPane iframe at it (real interactive
  preview); keyboard-nav = the "manipulable" v1. *M, LOW-MED.*
- **Phase 3 — artifact_mode flag (#33 answer, NO surface split):** `runtime._artifact_mode` +
  `set_artifact_mode`; branch `_compose_build_loop` → NeverConfirm + INTERACTIVE + `artifact_scope()`
  (slides/sheet/image/file/search, no shell/browser/plan-gate); `CreateConversationBody.artifact_mode` +
  explicit Agent-surface toggle. *S-M, LOW.*
- **Phase 4 — image-gen backends (product idea #1):** drop Presenton's provider-pluggable image service
  into the EXISTING `ImageBackend` seam (`image_gen.py`): keyless PIL/ComfyUI default + paid
  Pexels/Pixabay/DALL-E/Gemini via the existing `/api/secrets` CRUD; wire `Slide.image_prompt`→image. *M, MED.*
- **Phase 5 — optional Presenton sidecar de-risk:** 1-2 day throwaway compose pointed at gpt-oss-120b to
  sanity-check deck quality before committing Phase 1; never merged to prod. *S, low.*
- **Acceptance:** agent asks for a 5-slide deck → native editable `.pptx` (opens in PowerPoint/LibreOffice)
  + `.pdf` + 16:9 `.html` that renders + keyboard-navigates in the Artifacts tab; Firefox screenshot.

---

## Section 5 — Re-evaluation (what Dylan's answers changed)

- **Build-loop diagnosis: UNCHANGED, reconfirmed.** #14/#16-19 add severity, not new facts. The
  consensus fix set stands (snapshot scaling → read-aware breaker → F9-for-all → browser/vision →
  C18 → subdir preview). #14's "persistent, needs a real root cause" is now ANSWERED (C18 bug).
- **Exports (#11): scope GREW** — from "clean formatting" to "design a modular, branded Disco
  report template the exporter fills in (follow-ups + big reports)." Wave 4 export item is now a
  template-system task, not a CSS tidy.
- **Slides (#34): decision is now RESEARCH-GATED.** Do NOT assume constrained-schema. Add a research
  step: empirically compare constrained-schema vs freer generation (and Presenton's markdown
  approach) for deck quality with our models, THEN pick. Wave 3 gains a prerequisite spike.
- **Search (#1/#2): EXPANDED** — add a last30days-inspired Recency toggle (date window + native
  recent-mode search), beyond the plain date-injection. Wave 5 grows.
- **#33 ANSWERED:** no surface split; `artifact_mode` flag.
- Everything else: confirmed as-is; plan unchanged.

