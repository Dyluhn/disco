> **SUPERSEDED / HISTORICAL (as of 2026-07-07).** June-era runthru-v2 root-cause + fix plan, frozen mid-execution.
> Historical only. Not a source of current status or operating instructions.

# Dylan runthru-v2 — root cause + fix plan (2026-06-20)

Frustrated live test (slow/broken build). Root-caused by 6 parallel investigations (5 tracers + codex),
ALL from real evidence in `disco.db` + `/tmp/disco-agent.log` (conv ids cited). Convergent. Every fix below
ENDS with **visual proof** (screenshot or real trace from the running app) — no "fixed" without it.

## Codex review round 1 (SHIP-WITH-FIXES) — corrections folded in (authoritative amendments)
- **NEW ROOT-5 (codex MISSED-catch): `slides_generate` ignores the conversation model override.** The structured
  pipeline resolves its own LLM from `ConfigStore().model_for(AGENT_DRIVER)` (`_slides_pipeline.py:51-77`), so even
  after ROOT-1 the deck is authored by the DEFAULT (local) model while the loop runs DeepSeek. → thread the
  conversation's effective model/router into `slides_generate` (and check other tool-internal LLM calls — audio/
  sheets pipelines may have the same). Required for #5/#7.
- **ROOT-1 mechanism corrected:** there is NO `PATCH /conversations/{cid}`; settings apply only at creation
  (`agent.ts:54`, `conversations.py:47`). The loop CACHES `_model_override` at first kick (`runtime.py:1030-1063`),
  so changing it later won't affect a live loop. → FIX = **add a settings-patch endpoint, await it on the
  pre-created cid, THEN kick** (don't recreate the cid — uploads live on it). Also: **DR is already mostly fixed**
  by R1/R9 (it recreates preCid on settings change + submits current settings on the fallback path,
  `useDeepResearch.ts:74-165`); its only residual is the null/pending attach window + upload survival (E4) — NOT a
  "defaults on mount" drop. The "defaults on mount" drop is BUILD-only (`useBuild.ts:36-90`).
- **#3 sub-fixes were WRONG — redesign:** do NOT set `plan_step.read_only=False` (it's genuinely informational/no
  mutation — architectural regression). The F9 false-dedup is NOT the arg-summary int-drop (dedup equality compares
  FULL args); it's that `{index,state}` is **dict-equal across plan revisions** → step 1 of revision-2 dedups
  against step 1 of revision-1. → FIX: make F9's dedup key **plan-revision-aware** (or exclude `plan_step` from F9).
  Done-conditions are **advisory and only run AFTER the model calls `plan_step(done)`** (`plan_conditions.py:37`) —
  they are NOT an auto-complete net today, and not every step has one (DR steps don't, `deep_research_service.py:393`).
  → The real auto-complete = **emit a synthetic/system `plan_step(idx,done)` ActionEvent when a step's work is
  verifiably complete** (the UI `buildTrace.ts:605` + finish gate `signals.py:219` already consume plan_step
  actions), so the checkmark never depends on the model remembering. Define "verifiably complete" via done-condition
  where present; elsewhere keep the nudge.
- **#4 preview is not purely the `/workspace/` URL:** the backend static preview already serves the first
  `index.html`'s directory (`session.py:418-487`). So ALSO fix `deriveSrcDoc` subpath resolution
  (`buildTrace.ts:441-461` indexes full-path + basename only → `css/app.css` misses) AND PreviewPane mode is
  init-once so it won't switch to live after the proxy appears (`PreviewPane.tsx:200-203`) AND surface the canonical
  preview URL so the agent stops pointing at `/workspace/`.
- **#5 theme: NO silent coercion** (masks generator bugs). Coerce ONLY known legacy aliases (e.g. `neutral`→
  `neutral-light`); otherwise FAIL validation + retry with the EXACT allowed enum listed. Editor needs
  canvas-relative sizing (`ElementBox.tsx:216` is viewport `vw`).
- **#6 do NOT enable anchored_edit on local Qwen** (config `config.py:316` + `test_vision_caps.py:582` assert it
  lacks it; `file_str_replace` is capability-gated by design `registry.py:157`). Use the EXISTING line-edit tools
  (`file_edit`/`file_replace_lines`, available to all) + a read-before-rewrite policy + ROOT-1 (the picked model has
  anchored edit). Benchmark before ever flipping Qwen's capability.
- **#7 `/agent` needs SEED PLUMBING:** `ResumeAgent` drops router-state seedTask/seedContext (unlike ResumeProject)
  and `AgentSurface` takes no seed props (`App.tsx:43-85`, `AgentSurface.tsx:13`). → add seedTask/seedContext to the
  agent path + a deck-first prompt/artifact-mode, or routing to /agent lands on the surface without starting the job.
- **ROOT-2 needs SHARED canonicalization, not prompt-only:** file tools expect workspace-relative paths
  (`files.py:188`); process sandbox cwd=workspace + rejects absolute `/workspace/...` (`process.py:48-68`); container
  maps `workspace/foo`→`/workspace/workspace/foo` (`_container.py:267`). → normalize BOTH `/workspace/foo` and
  `workspace/foo` at the file-tool + done-condition + preview boundaries; keep old-snapshot `workspace/...` compat.
- **Sequencing:** Wave 1 also adds the backend settings-patch endpoint + `/agent` seed propagation; Wave 2 does path
  canonicalization before preview/done-condition tests; Wave 3 builds the real plan-progress event (not advisory).

## The 4 systemic roots (fixing these collapses most of the 9 issues)

**ROOT-1 — eager "pre-create cid on mount" drops post-mount user choices.** Both `useBuild.ts` and
`useDeepResearch.ts` pre-create a conversation on mount with DEFAULT settings (`modelOverride:null`), then at
submit reuse that stale `preCid` and `return` BEFORE sending the user's current pick. Causes: **model override
ignored** (A1 — picked DeepSeek, ran local Qwen), the **iterative toggle** (R1), the **attach button
vanishing** (E4, a regression from R1's `setPreCid(null)` flush), DR leader. Evidence: `conv_9787…`,
`conv_043…` absent from `disco.db.overrides.json`; all their inference POSTs went to local `192.168.1.231:18080`.
→ FIX (corrected per review — there is NO patch route today, and the loop caches the model at first kick): **add a
settings-patch endpoint, `await` it on the pre-created cid to set the current override/autonomous, THEN send the
kick.** Do NOT recreate the cid (uploads live on it). DR is already mostly fixed (recreates preCid on settings
change + submits current settings on fallback) — its only residual is the attach pending-window (E4); the
"defaults on mount" drop is BUILD-only. Attach stays mounted (pending state), never unmounted on null. (`useBuild.ts:36,78`, `useDeepResearch.ts:74,96`, `NeedMoreCard.tsx:740`, `conversations.py:26`,
`runtime.py:1030`.)

**ROOT-2 — doubled `workspace/` path breaks preview AND step auto-completion.** The build agent writes to
`workspace/index.html` *inside a sandbox whose cwd is ALREADY the workspace*, so files land at
`workspace/workspace/…`. The preview server serves the workspace as root, so the agent pointing the preview at
`/workspace/` → **404 → blank preview** (C1, reported 3×); and plan-step **done-conditions check root paths that
don't match `workspace/…` → never auto-complete** (part of the 5× step bug). Evidence: `conv_9787…` writes
`workspace/css/…`; seq168 `status 404`; log `GET /workspace/ 404`. → FIX: stop the doubled prefix — make the
build prompt/path contract write to the workspace ROOT (no `workspace/` prefix), OR normalize the prefix in the
preview URL + done-condition path resolution. Canonical preview URL surfaced to the agent.

**ROOT-3 — brand fonts load in NONE of the three render paths (3 different reasons).** (a) HTML: `@font-face`
uses `src: url('file:///…/Fraunces.ttf')` — browsers BLOCK `file://` from an `http://` page → slides render in
fallback fonts (D3). (b) PDF: `font_face_css()` uses CSS4 variable-range `font-weight: 100 900` which WeasyPrint
REJECTS ("Ignored 'font-weight:100 900', invalid value" ×5 in the log) → system-font substitution → the
**glitchy/broken iterative PDF** (E1; worse for iterative because its section titles are 240–288-char verbatim
sub-questions that overflow without Fraunces metrics). (c) PPTX: `run.font.name="Fraunces"` but the TTF isn't
installed in the OS → viewer substitutes. → FIX: (a) base64-inline the font bytes as data-URIs in
`brand/css.py font_face_css()` (works for both browser-HTTP and WeasyPrint); (b) single-value `font-weight` per
`@font-face` (WeasyPrint-safe); (c) embed fonts in the PPTX or document the install requirement.
(`core/brand/css.py:58-71`, `_pptx_render.py:619,806`.)

**ROOT-4 — the model-override drop forces the WEAKEST model to do the hardest job.** Because A1 dropped the
DeepSeek pick, the build ran on local Qwen 27B Q5, which: (i) is denied `anchored_edit` in config → can ONLY
`file_write` whole files (`registry.py`: `_WEAK_TIER_ADVERTISED = AGENT_TOOLS - {file_str_replace}`); (ii) takes
20–93 s/turn; (iii) unreliably calls `plan_step(done)`. So ROOT-1's fix is ALSO the biggest lever on slowness
(C3 — 19 of 28 min in a browser→full-rewrite loop), full-rewrites (C2), and step-marking. Evidence: `conv_9787…`
37 `file_write`, 0 targeted edits, `windows.css` rewritten ×9.

---

## Per-issue fixes (all 9) → wave

| # | Issue (×N = times reported) | Fix | Root |
|---|---|---|---|
| 1 | **Model override ignored** ×3 — ran Qwen not picked DeepSeek | ROOT-1: send current override at submit + deck handoff passes the effective pick (not `null`/stale last-selected) | R-1 |
| 2 | **Plan approval skipped** | Split "auto-kick seeded task" from "auto-approve plan": the deck handoff must NOT set conversation `autonomous=true` (or make auto-approve an explicit opt-in). User sees + approves the plan. (`NeedMoreCard.tsx:740`→drop the `true`; `engine.py:832`) | — |
| 3 | **Plan steps never mark off** ×5 | **(see corrected design in the amendments above — these supersede)** (a) make F9's dedup key **plan-revision-aware** (or exclude `plan_step` from F9) — do NOT set `read_only=False`; (b) **emit a synthetic/system `plan_step(idx,done)` ActionEvent** when a step is verifiably complete (UI + finish gate already consume plan_step actions) so the checkmark never depends on the model; (c) define "verifiably complete" via done-condition where present, nudge elsewhere; (d) ROOT-4 (capable model marks reliably) | R-2/4 |
| 4 | **Site preview blank** ×3 | ROOT-2 (path) + default PreviewPane to LIVE mode when the dev server is up (not the broken srcdoc) + fix `deriveSrcDoc` relative-subpath resolution (`buildTrace.ts:445`) as fallback | R-2 |
| 5 | **Slides HTML-fallback + broken editor + fonts** | (a) enumerate ALL 8 valid themes in `_CAPABLE_SYSTEM`/`_WEAK_SYSTEM`/`_RETRY_MSG`; coerce ONLY known legacy aliases (`neutral`→`neutral-light`), else FAIL + retry with the exact enum (NO silent coercion — it masks generator bugs) (`_slides_pipeline.py:129,164`); (b) ROOT-3 fonts; (c) **ROOT-5: slides_generate must use the conversation's effective model**; (d) deck editor: render the real deck (iframe the rendered HTML or load brand CSS + structure into `SlideCanvas`; fix `vw`→**canvas-relative** sizing) (`_deck_schema.py:1130`, `ElementBox.tsx:216`, `DeckEditor.tsx`) | R-3/5 |
| 6 | **Agent too slow / full rewrites** (~20min) | ROOT-4 (run picked model — it HAS anchored edit) + push the EXISTING line-edit tools (`file_edit`/`file_replace_lines`, available to all tiers) via a **read-before-rewrite policy** (existing large file → require targeted edit) + a progress gate on the browser→edit loop. **Do NOT enable `anchored_edit` on local Qwen** (config+tests assert it lacks it; benchmark first) (`registry.py:62,157`, `files.py:303` description, `engine.py`) | R-4 |
| 7 | **Deck handoff → /build not /agent** | Route report→slides to the AGENT surface: `createBuildConversation(null,"agent",true→false)` + `navigate('/agent/${cid}')` → task-agent framing + deck-first workflow (not "you are a software dev" + port-8000 preview) (`NeedMoreCard.tsx:740`, `App.tsx`) | — |
| 8 | **Iterative PDF broken + verbose labels** | PDF: ROOT-3(b) weasyprint font-weight + remove the duplicated inline+stylesheet CSS injection (`report_export.py:258,455`). Labels: decouple the iterative sub-question's QUERY from its LABEL — set the label to `Iterating on section "{sec.title}"` (search) / `Rewriting section "{sec.title}"`; pass the claim text as the search query only (`deep_research/engine.py:793`, `gather.py:258`, `synthesis.py:340`, `deepResearchTrace.ts:276`) | R-3 |
| 9 | **Attach button** (splash footer + DR vanish) | Standard: move `UploadComposer` from `footer`→`extraControls` (inline, like DR) (`ResearchSurface.tsx:77`). DR: keep the attach mounted in a pending/disabled state while preCid re-creates instead of unmounting on null (`DeepResearchSurface.tsx:143`, `useDeepResearch.ts:96`) — ties to ROOT-1 | R-1 |

## Honest note on #3 (the 5× ask)
It is NOT a one-line fix and I won't pretend it is. It's 4 stacked causes (weak model forgets `plan_step(done)`;
F9 mis-dedup + arg-summary collision confuse it; done-condition auto-complete broken by ROOT-2; weakest model
running due to ROOT-1). The durable fix is (c)+(e): **auto-complete a step when its done-condition verifiably
passes**, so a checkmark never depends on the model remembering — plus the F9/arg-summary code fixes and ROOT-1/2.
After the fix the acceptance test is explicit: a real multi-step build ends with EVERY completed step checked in
the UI (screenshot), proven on BOTH a capable model AND local Qwen.

## Build order (waves; each ends with VISUAL PROOF)
1. **ROOT-1 settings-patch-then-kick + #1 + #9 + #2 + #7** (build hook + NEW settings-patch endpoint + `/agent` seed plumbing + routing) — unblocks running the picked model + approval + agent surface.
2. **ROOT-2 shared workspace-path canonicalization** (#4 preview + #3 done-conditions) — preview renders, done-conditions resolve.
3. **#3 plan-step** (revision-aware F9 + synthetic `plan_step(done)` progress event — NOT read_only=False) — steps check off without the model.
4. **ROOT-3 fonts (data-URI + single-weight) + ROOT-5 slides-model + #5 slides** (theme enum/legacy-coerce + editor canvas-sizing) + **#8 iterative PDF/labels**.
5. **#6 speed** (line-edit policy + read-before-rewrite + loop gate — NOT Qwen anchored-edit).
Each fix: implement → real-app reproduce-broken-then-working → **screenshot/trace attached** → gpt-5.5 diff review → commit.

## Acceptance (visual proof required, per issue)
1 model: build trace shows POSTs to the picked OpenRouter model, not local. 2: plan-approval gate appears on the deck handoff. 3: every done step checked in the UI (capable + Qwen). 4: a built site RENDERS in the preview (screenshot). 5: deck is real (renderer≠fallback) + fonts visible + editor shows the real slide. 6: a build completes in a fraction of the time with targeted edits. 7: deck handoff lands on /agent. 8: iterative PDF opens clean in Firefox (screenshot) + labels read "Iterating on section X". 9: attach inline on splash + stays put in DR.
