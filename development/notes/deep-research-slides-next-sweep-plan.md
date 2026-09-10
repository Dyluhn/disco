# Disco Quality Sweep — Master Implementation Plan

**Status:** Read-only planning complete. Implementation has not started and still requires explicit approval.

**Last updated:** 2026-08-25

This is the master plan for the next Disco quality sweep. It consolidates the completed read-only root-cause work and is organized for dependency-safe execution by parallel Luna subagents. Each numbered section is a bounded implementation slice; execute one gated wave at a time and stop if its integration gate fails. Approval of this document does not authorize work outside these slices.

## Outcome

Deep Research should keep producing the high-quality, evidence-backed reports now expected, while its controls, providers, and feedback remain honest and predictable. Slides, Workflows, Reference Packs, AppKit, model onboarding, and Build delivery should each have one reliable golden path whose UI is driven by durable runtime facts.

## Design rules

1. One intended route per outcome.
2. Funnel retries and correction toward the requested artifact; do not add silent or product-substituting alternatives. Explicit capability-limited states such as `needs_input`, `Asset only`, and labeled text/DOM-only operation are allowed only when persisted and shown truthfully.
3. A genuine provider or infrastructure outage may fail explicitly. Ordinary quality problems stay inside the correction loop.
4. UI status must be derived from persisted facts, not optimistic timers or copy.
5. Preserve authoritative source material across every generation stage.
6. Prefer small changes to existing contracts over parallel systems.
7. No fallback may silently substitute a materially worse product.

## Protected behavior from the recent Deep Research work

The implementation sweep must begin and end with regression checks for these behaviors:

- A completed research run produces an actual report with an executive summary. Verification details remain visible but cannot replace the executive summary.
- Research starts broadly, deepens where evidence warrants it, and may replace unproductive subquestions instead of becoming contractually trapped by them.
- Depth changes both research effort and expected report length within bounded limits.
- Citation numbering and displayed sources are stable and deduplicated by recognized source identity without fuzzy-title merging.
- Provider-specific reasoning or thinking fields are normalized at the provider boundary rather than leaking into report content.
- The research harness can run multiple requests concurrently and expose search queries, provider responses, model responses, event timing, retries, and detected no-progress/thrash behavior.
- SearXNG and Crawl4AI remain the configured baseline path; optional provider support must not silently alter that baseline.

These are regression requirements, not invitations to redesign those systems during this sweep.

## Confirmed root causes and planned corrections

### 1. Search source controls

**Root cause:** The UI describes source chips as additions, but any nonempty selection replaces the configured search provider. The `Web` chip specifically selects DDGS and bypasses configured SearXNG.

**Single contract:**

```text
effective search = configured Settings provider + selected additional sources
```

**Plan:**

- Remove the `Web`/DDGS chip from per-run controls.
- Relabel the remaining controls as **Additional sources**.
- Keep News, arXiv, and Semantic Scholar as optional additions.
- An empty selection means the configured provider only.
- Use the same additive composition helper anywhere the source picker is shown, including ordinary Search; only Deep Research persists the selection in its research checkpoint.
- Preserve stable URL/source-identity deduplication after federation.

### 2. Research attachments

**Root cause:** The generic uploader accepts any file and can display success even though research ingestion later ignores unsupported formats.

**Plan:**

- Show a popover or tooltip on Attach: `Accepted for research: PDF, TXT, Markdown, CSV, and HTML.`
- Apply the same allowlist to the file picker.
- Validate drag-and-drop before conversation creation or upload.
- Show an error for an unsupported file and never show a success toast for it.
- Do not broaden the ingestion formats in this sweep.

### 3. Resume fidelity

**Root cause:** Depth and recency are checkpointed, but selected additional sources exist only in process memory. A restart can silently resume with different source scope.

**Plan:**

- Add one optional `additional_sources` field to the existing research checkpoint.
- Persist normalized source IDs only.
- Interpret `null` as a legacy checkpoint and `[]` as explicitly baseline-only.
- Restore those sources through the normal dependency construction path.
- Do not persist endpoints, credentials, provider instances, or a second resume document.

### 4. Research activity card

**Root cause:** Collapse behavior is restricted to finished runs, so the growing activity feed is always expanded while research is running.

**Plan:**

- Use one expanded/collapsed state for running and finished research.
- Default it to collapsed.
- Keep current phase, elapsed time, search/source counts, and running state visible in the collapsed header.
- Give the toggle correct `aria-expanded` and `aria-controls` behavior.
- Streaming new events must not reset the user's chosen state.

### 5. Steering presentation and acknowledgment

**Root causes:**

- The steering textbox uses a visually harsher standalone border treatment instead of the established composer styling.
- The UI marks steering as sent optimistically, but the backend emits no persisted event showing that a model turn consumed it.
- A steer is currently removed from the queue before the corresponding model response parses successfully, so a malformed turn can lose it.

**Plan:**

- Reuse the existing shared input surface, hairline border, focus-within, and transparent-input treatment.
- Give every steer a client-generated ID.
- Retain pending steers across malformed or retried turns.
- Include pending steering in each attempted model turn until one returns a valid parsed response.
- Emit a persisted, provider-neutral `steer_applied` action containing the matching ID after that valid response.
- Show a spinner while pending and a green check only after the matching persisted action arrives.
- Describe the state as delivered/applied, not proof that the model semantically obeyed it.
- Do not add a timer, model self-evaluation, or new failure branch.

### 6. Report-to-Slides routing

**Root cause:** The report handoff creates a normal Agent conversation. That bypasses the deck contract and its prompt pack, adds a redundant planning model, and leaves generic shell/file tools available before and after slide generation.

**Plan:**

- Make the report button start a typed deck artifact job directly.
- Pin it to the existing deck contract and artifact mode.
- Pass the authoritative ReportEvent as the source instead of asking an outer agent to condense it into a new goal.
- Let the host invoke the existing structured slide generator through the normal executor/event log.
- After successful deterministic artifact validation, mark the job finished immediately.
- Route later edits through the deck editor and `deck_patch` contract.
- Do not give this path a generic planning, shell, or file-inspection phase.

### 7. Slide grounding

**Root cause:** The outline stage receives the source report, but the fill stage receives only the outline. It therefore invents body copy from titles and model memory.

**Plan:**

- Supply the exact authoritative report to both outline and fill stages.
- Keep the structured outline as an organization aid, never as a replacement for source context.
- Add a sentinel-fact regression proving the source reaches both model calls and the resulting authored deck.
- Do not add a second model-based fact-checking agent in the first implementation.

### 8. One first-class slide product

**Root cause:** The model-facing slide schema still advertises Markdown/Marp routes that can degrade successfully to cheap basic HTML. This creates an escape from the structured authored-deck renderer.

**Single product contract:**

```text
authoritative report
  -> structured authored deck
  -> branded renderer
  -> native PPTX + authored JSON + branded editor preview
```

**Plan:**

- Make structured authored-deck generation the only report-to-slides route.
- Use PPTX as the canonical deliverable.
- Retain authored JSON as the editable source.
- Retain branded HTML only as the first-class editor preview.
- Remove model-facing Markdown/mode selection and successful basic-HTML degradation.
- Retain the designed themed SVG/vector artwork fallback when image generation is unavailable.
- A genuine missing renderer/provider capability returns an explicit job error; it does not substitute a lesser slide product.

### 9. Provider-neutral slide model calls

**Root cause:** The inner slide pipeline posts directly to a Chat Completions-shaped endpoint, bypassing the application's provider adapter.

**Plan:**

- Inject the existing provider-neutral completion interface into the slide pipeline.
- Keep reasoning/thinking normalization, secrets, retries, response parsing, and capability handling at the provider boundary.
- Do not create a slides-specific provider abstraction.

### 10. Workflow tab truthfulness and execution

**Intended product model:** This is a plugin-style capability library for the Agent. Built-in and user-authored workflows should be concrete capabilities the Agent can invoke: each packages instructions, an input schema, a sealed tool/permission surface, and an output/completion contract. This is more executable than a skill and more task-specific than a general plugin or connector.

**Current verdict:** The subsystem is real, but the current tab is a false affordance relative to that intent. It is presently a registry/review console for sealed workflow records—mostly startup-seeded templates. Explicit Run/Schedule actions can create a genuinely tool-restricted agent loop, but ordinary Agent conversations do not consult the registry because workflow routing is disabled in the active deployment.

**Confirmed root causes:**

- Six of the seven live entries are code-seeded and automatically marked approved; they are not learned or user-created Agent capabilities.
- The normal Agent workflow router is default-off and unset in the current deployment.
- Run accepts only an instance ID. Schedule accepts only instance ID, digest, cron, and timezone. Neither path collects the workflow's required inputs or a task request.
- The sealed run prompt contains schedule metadata, output path, and stored parameters, but not the workflow card or a user goal.
- Seeded instances store empty parameters. User-authored parameterized workflows store synthetic simulation values such as `"sample"` and `1`; those fixtures are then reused as real execution inputs.
- Browser Automation and Form Fill are displayed as Approved and runnable even though their required parameters are absent and their sealed egress policy prevents them from reaching the web.
- Approval currently means a record has an approval object, not that the currently displayed compiled surface is still the surface that was reviewed or that dependencies make the workflow runnable.
- The Run endpoint waits for the sealed run to return before the UI says “Workflow run started.”
- Schedule defaults to every minute, does not collect bound task inputs, and repeated Save actions can create additional schedules instead of clearly editing one existing schedule.
- Cold resume does not reconstruct the sealed workflow identity, parameters, and phase after process restart.
- Workflow prompt packs and declared workflow skills are displayed as architecture metadata but are not mounted into the production sealed-run prompt as workflow-specific behavior.
- The catalog eagerly renders instance IDs, hashes, validation internals, full tool schemas, MCP data, policies, and a raw cron editor on every card. It is an engineering approval console presented as a user capability library.
- The default advanced creator exposes the complete platform tool inventory plus internal concepts such as MCP mounts, output path templates, verification keys, finalizers, and trust-policy flags. Users must understand the runtime architecture to create a basic capability.

**KISS product contract:**

```text
installed workflow
  = card/instructions + input schema + sealed capabilities + output contract

enabled + ready
  -> advertised to the normal Agent as one callable capability
  -> user or Agent supplies validated inputs
  -> host enters the pinned sealed workflow
  -> workflow returns its verified result/artifact to the conversation
```

**Plan:**

- Keep the real sealed executor and output gate. Do not build another workflow engine.
- Treat the tab as the Agent's capability library: built-in workflows plus workflows the user creates.
- Define one canonical `ready(workflow)` predicate covering enabled state, current approval/surface digest, dependency and connection availability, egress, tool availability, and successful compilation. The catalog, dynamic tool list, Run, Schedule, launch, and resume paths must all call it; no caller may reimplement readiness.
- Compile every Ready workflow into one stable dynamic tool definition for the normal Agent. Use only the card's short purpose for tool discovery and its parameter schema as the tool's exact call schema. A workflow that is disabled or Needs Setup must not appear in the model's tool catalog.
- Do not enable the current mandatory router behavior that tells the Agent to list workflows before every actionable request. Workflow selection should be available when relevant, not a compulsory stochastic pre-step for unrelated work.
- Use the same invocation contract whether the user clicks Run or the Agent selects the workflow. Both paths bind inputs, enter the same seal, and produce the same output.
- Convert built-ins into configure-first capabilities. Only show a workflow as Ready after required connections, egress, current approval digest, and tool availability are valid; task-specific values are collected at invocation time.
- Generate one Run form from the existing parameter schema. Pass those validated per-run values plus the workflow card as the authoritative job instruction.
- Remove synthetic fixture parameters from persisted execution state. Fixtures remain simulation-only.
- Keep scheduling as a secondary action inside a workflow's details rather than the defining feature of every card. Use the same bound inputs for schedules, replace raw every-minute defaults with an explicit scheduling step, show the human-readable cadence, and edit one identifiable schedule rather than silently duplicating it.
- Start the job asynchronously, return its conversation ID immediately, and derive queued/running/finished/error UI from persisted events.
- Separate security approval from operational readiness in the UI. `Approved` alone must never enable Run when the workflow cannot execute.
- Revalidate the pinned compiled-surface digest when launching and resuming.
- Persist enough workflow identity, parameters, and phase to reconstruct the same seal after restart.
- Build one canonical sealed brief from the workflow card, validated inputs, output contract, and outcome rules. It is the sole runtime workflow instruction. Remove or hide prompt-pack/skill claims until those fields have a real, tested runtime effect; do not introduce a second prompt hierarchy.
- Move raw tool schemas, hashes, mounts, and policies behind a collapsed **Security details** section. The primary card should resemble a capability/plugin card: purpose, Enabled state, Ready/Needs Setup state, required connections and permissions, last use, and Run. Creation produces the same kind of card as a built-in capability.

**Regular Agent invocation:**

1. During normal Agent composition, load only owner-visible workflows and publish a dynamic tool for each workflow that is enabled, approved against its current surface, dependency-complete, and successfully compiled.
2. When the Agent calls that tool, one shared invocation service validates the current digest, readiness, and exact parameters again. The tab's Run action calls this same service.
3. Persist one workflow-start event containing the instance ID, pinned definition snapshot/digest, validated parameters, and run ID. Reuse the existing event log for queued/running/finished/error and resume; add no workflow lifecycle store.
4. End the broad Agent run segment at a host-owned handoff boundary and compose a fresh sealed run segment in the **same conversation**. A fresh executor and sandbox are required because the ordinary Agent sandbox may already have broader network permissions; changing the visible tool list alone is not a truthful seal.
5. Inject one canonical brief containing the workflow card, validated inputs, output contract, and outcome rules.
6. Existing workflow finish/output gates verify the result. Completion ends this turn; the next user turn returns to ordinary Agent capabilities. `needs_input` keeps the sealed workflow active and resumable.
7. On restart, reconstruct any unmatched workflow-start fact with the pinned definition and parameters before composing the next run segment. Fail closed and visibly if its required resources are no longer available.

Do not route regular invocation through the schedule-oriented service that creates a separate pristine conversation. Reuse its sealed loop, sandbox, supervision, and verification components behind the shared invocation service.

**Simplified catalog UI:**

- Heading: **Workflows**
- Subtitle: `Reusable capabilities your Agent can use in any chat.`
- One primary action: **New workflow**
- Group compact cards under **Available to your Agent**, **Needs setup**, and **Off**.
- Each card shows only icon, name, one-sentence purpose, `Built-in` or `Yours`, truthful readiness, required app/service chips, an enable toggle, and one contextual action: **Run** or **Set up**.
- Selecting a card opens a focused detail view with **Overview**, **Runs**, **Schedule**, and **Advanced** tabs. Overview contains inputs, output, connections, plain-language permissions, and last use.
- Put exact tools, MCP mounts, policies, validation codes, digests, and raw definitions in one collapsed **Security details** section.
- Use `Ready`, `Needs setup`, and `Off` as the primary statuses. Keep `Approved` as a security fact inside details, not a claim that the capability can run.
- Do not display raw cron syntax in the catalog and never prefill an every-minute schedule.

**Simple creation flow:**

1. **Describe** — one large `What should this workflow do?` field, a few examples, and **Generate workflow**. Keep the existing natural-language drafting endpoint.
2. **Review** — one plain-language, editable summary showing what it does, when the Agent may use it, what it asks for each run, required apps/connections, actions or data it can change, and what it produces.
3. If Ready, the primary action is **Create & enable**. If dependencies are missing, it is **Save & set up**. Explicitly confirm consequential write/submit permissions.
4. A persistent **Advanced** tab retains full-fidelity power-user editing. It exposes the exact input schema, builtin tools, MCP mounts, skills, output path/format, verification checks/finalizer, write/untrusted/egress policies, compiled surface, digest, validation findings, and raw definition. Organize these into collapsible Inputs, Connections & tools, Output, Verification, Policies, and Raw definition sections; do not place them in the default path.
5. Any Advanced edit invalidates the prior compiled-surface approval, reruns configuration validation, and requires explicit review before the workflow becomes Ready again.

Replace `Simulation ok` with truthful language. Static scope/path checks are **Configuration checks**; only a real workflow execution with user-supplied inputs may be called a **Test run**.

**Focused acceptance tests:**

- Ordinary Agent mode does not claim or silently consult workflows when explicit workflow routing is disabled.
- Enabled, Ready workflows are present in the Agent's callable capability catalog; disabled or unready workflows are absent.
- An ordinary Agent can select a relevant workflow without first routing every unrelated task through `list_workflows`.
- User-clicked and Agent-selected invocations produce the same validated sealed run.
- Dynamic workflow tools have stable unique names, use the card as their description, and expose the exact workflow parameter schema.
- Invoking a workflow hands the same conversation from a broad Agent segment to a fresh sealed executor/sandbox; the broad sandbox is never reused.
- Completing a workflow restores ordinary Agent capability on the next user turn, while `needs_input` and restart preserve the active seal.
- Every card shown as Ready can complete one representative live run from the UI.
- Parameterized Run and Schedule paths reject missing/invalid input and never execute simulation fixtures.
- The workflow card and validated run inputs are both present in the sealed model request.
- Browser-based workflows cannot be marked Ready without usable egress and required inputs.
- A changed tool/MCP/skill surface invalidates readiness until the new surface is reviewed.
- Run returns a conversation immediately and its visible state matches persisted execution status.
- Saving a schedule updates the intended schedule, never creates accidental every-minute duplicates, and displays next/last run state.
- Restart-and-resume restores the exact workflow seal and parameters.
- At least one parameterized browser workflow and one artifact workflow pass live end-to-end tests using only their declared tools.
- The default catalog contains no digest, raw JSON/schema, validation code/path, MCP/finalizer terminology, or cron expression.
- The default creator initially exposes only the description step; technical settings stay collapsed.
- The Advanced tab remains reachable from both draft review and existing workflow details, round-trips every supported expert field, and clearly invalidates/revalidates readiness after edits.
- A generated review uses plain-language permissions and dependency requirements, and a newly enabled custom workflow appears once under `Yours` and becomes callable without a server restart.

### 11. Reusable Reference Packs for Build

**Corrected product intent:** This is not a request for another per-build attachment tray. A Reference Pack is a persistent, reusable collection of files that a user creates through a normal Agent task, manages in Settings, and explicitly selects when starting a Build.

```text
Agent task + attached/workspace files
  -> create a persistent Reference Pack
  -> edit or delete it in Settings
  -> explicitly select one or more packs in Build
  -> pin their bytes to that Build before the first model call
  -> Build agent inspects the pack and uses it while planning and building
```

**Current root cause:** The names exist, but the product does not. The current `ReferencePack`, `InstalledLibraryCatalog`, Starter Recipe, and Library Recipe code is isolated, in-memory Build Platform scaffolding. It has no persistent user library, Agent tool, Settings API/UI, Build picker, or runtime context path. `ConstructionRequest.reference_ids` is declared but never populated or consumed. Ordinary attachments work only as conversation resources and therefore do not create a reusable pack.

**KISS contract:**

- A pack is inert user data: `id`, owner, name, description, current revision/content digest, and an ordered set of copied files with names, media types, sizes, hashes, and readability states.
- A pack never grants tools, capabilities, network access, policy, or trust. Instruction-like text inside it is reference material, not system instruction.
- The reusable library keeps one editable current revision. It does not expose or retain a user-facing version-history system.
- When a Build binds a pack, the server makes one immutable, conversation-owned snapshot. Later Settings edits or deletion affect future Builds only; an active or resumed Build keeps its exact bytes and digest.
- Packs are never injected globally or selected automatically. Only packs explicitly chosen for that Build enter its workspace or context.
- Implement Reference Packs only. Do not activate Starter Recipes, Library Recipes, profile/category resolution, or the dormant generic library-composition stack as part of this work.

**One authority and one storage path:**

- The agent server owns the persistent, owner-scoped pack store because it already owns uploaded files, workspaces, Build conversations, and the Agent tool surface. Do not create competing app-server and agent-server writers.
- The library manifest is authoritative for reusable pack metadata and bytes. After Build binding, the immutable conversation snapshot is authoritative for that Build. `ResourceRef`, the context manifest, and `PACK.md` are transport/index views only and never independent state.
- Store a small manifest plus copied original bytes using atomic writes and existing data-directory conventions. A pack must remain valid after its source Agent conversation is removed.
- Validate ownership, workspace-relative paths, file-count/size limits, and hashes before committing. A failed multi-file save commits nothing.
- Reuse the existing ResourceRef/context-manifest and conversation snapshot mechanisms after selection instead of creating a second resume/checkpoint system.
- Use the existing `ConstructionRequest.reference_ids` as the one selected-pack identity path once it is made end to end; do not add a competing reference-selection field.

**Agent creation funnel:**

- The user attaches or creates several files in a normal Agent task and asks the Agent to make a Reference Pack from them.
- Add one Agent-only action, `create_reference_pack(name, description, files)`. It accepts only validated files in that task's workspace, copies their exact bytes to the persistent pack store, and returns a compact success card containing the pack name, file count, and a **Use in Build** action.
- Pack creation must be an explicit user-requested action. Uploading a file alone never silently adds it to a global library.
- Keep initial Agent-side mutation deliberately narrow: Agent creates a pack; Settings is the one place to rename it and add, replace, or remove files. This avoids two competing editing experiences.

**Settings UI:**

- Add a plain-language **Reference packs** section under the existing storage/library area.
- The default list shows name, one-line description, file count, total size, and last updated time. It does not expose schemas, target profiles, capability intersections, trust tiers, raw JSON, or internal digests.
- Editing supports rename, description, file inspection, add files, replace a file, and remove a file. Each file shows a truthful state: `Ready`, `Asset only`, or `Couldn't read`.
- Deletion requires confirmation and explains: future Builds can no longer select the pack; Builds that already pinned it remain intact and resumable.
- Do not add an enable toggle or a second Settings-only pack-creation flow in the first pass. The understandable creation funnel is Agent + files; Settings manages what already exists.

**Build selection and runtime funnel:**

- Add **Reference packs** to the existing collapsed Build Options area. The picker is a simple multi-select list with pack name, description, file count, and updated time. Selected packs remain visible as compact chips/count even when Options is collapsed.
- Bind the requested pack IDs and observed digests before the first Build model call, including the lazy conversation path used when files create the conversation before submit. If a pack changed or disappeared between selection and binding, block the kick with a named refresh/remove message; never silently omit it or substitute different bytes.
- On binding, copy the exact selected bytes into conversation-owned storage and record the bound IDs/digests in durable conversation state. Resume reads this snapshot, never the mutable library head.
- Materialize each selected snapshot under `references/<pack>/` with one bounded `PACK.md` index plus its original files. Put only the compact pack/index paths in normal context; do not dump every file into every prompt.
- Text, code, Markdown, CSV, HTML, and SVG remain readable through the existing file tool. PDFs receive a deterministic extracted-text companion with page markers while retaining the original. Unsupported or unreadable binaries remain available as assets with an honest state.
- Images and visually important PDF pages retain their original pixels. Add one thin, provider-neutral `reference_inspect(path, question, page?)` action that is restricted to pinned pack files and reuses the existing bounded visual-inspection broker. If no visual route is available, it returns `Asset only`; no prompt or trace may claim the pixels were inspected.
- Funnel the model toward using selected packs: the Build brief identifies them as required evidence, and the existing plan validator cannot accept `submit_plan` until the agent has read each selected `PACK.md`. An early submission receives one corrective instruction to inspect the named index and resubmit; it is not a terminal failure or alternate output path. Reuse current turn/tool receipts for this check—do not add persistent per-file read-tracking state. The agent then reads only the relevant member files on demand.
- Keep Import Project, preview Point/Discuss/Edit targets, search results, browser observations, and ordinary per-run attachments on their current contracts. They do not silently become library packs.

**Focused acceptance tests:**

- In a real Agent task, attach several files, ask the Agent to create a pack, and prove exact bytes, hashes, order, and owner-scoped persistence survive server restart and removal of the source conversation.
- A partial/invalid multi-file creation is atomic: it neither creates an incomplete pack nor reports success.
- Settings can list, rename, describe, add, replace, remove, and delete; owner isolation and traversal/size bounds are enforced.
- The Build picker sends the exact IDs/digests on both ordinary and pre-created-conversation paths, and no model call starts if binding fails.
- An unselected pack never appears in workspace, context, tools, or model input. Concurrent Builds selecting different packs never leak content across conversations.
- A selected pack is snapshotted before planning, appears once in the context index, and an early `submit_plan` is funneled back to reading `PACK.md` rather than accepted or terminated.
- Editing or deleting the library pack after Build start does not alter the active Build; a new Build sees only the current library state.
- Condensation, sandbox recreation, and process restart preserve the same bound digest, paths, and bytes.
- Live sentinel packs containing text/code, a text PDF, and an image prove that the Build agent actually uses relevant content in the artifact rather than merely receiving filenames.
- Main-model vision, dedicated vision, and no-vision configurations all exercise the same `reference_inspect` contract; the no-vision case remains truthfully `Asset only`.
- Instruction-like content inside a pack cannot widen the Build's tools, egress, policy, or completion contract.

This section records the clarified Reference Pack scope. It remains planning-only until the master plan is approved.

### 12. AppKit golden paths without visual lock-in

**Acceptance criterion:** Requiring a `DesignSpec` is acceptable; requiring a particular visual theme to obtain RBAC, authentication, database, or another trusted capability is not. AppKit already models application functionality and design separately and can re-theme a finished application. The first task is therefore to prove and, where necessary, minimally enforce that separation—not to build a parallel component system pre-emptively.

Current evidence is partial rather than conclusive. Strict AppKit still begins from a whole-site recipe, preserves some recipe layout-family constraints during design alignment, and normal Build does not reliably transmit the existing `appkit_mode`. The successful `brutalist forum with RBAC` request shows that the pieces can work together, but does not prove that the same functional core survives radically different structures and styles or that RBAC is selected without a styling cue.

**Single contract:**

```text
user goal
  -> explicit functional requirements
  -> host resolves functional AppKit capabilities and dependencies
  -> independent visual direction or sensible design default
  -> agent builds a custom UI around stable, verified functional seams
```

**KISS plan:**

- Characterize the current AppKit contract first. Generate one fixed functional application specification with the same RBAC/auth/data policy under at least three materially different `DesignSpec`s, including different layout structure—not merely color-token changes.
- Make functional requirements explicit in the approved Build/AppKit plan and resolve them independently from visual words. `RBAC` selects RBAC; `brutalist` selects a visual direction. Neither is allowed to imply the other.
- Preserve the functional application/component identity and security probes while allowing the Agent to change page hierarchy, component composition around the trusted seams, copy, layout, palette, typography, and imagery.
- Wire the existing AppKit mode through the normal Build creation path so this behavior is a real product route rather than prompt coincidence or a hidden-mode test path.
- If characterization proves that a recipe or `app_set_design` rewrites security-sensitive core code, extract only those existing core seams behind the repository's current trusted-component digest/probe/ejection contract. Do not create a second generator or a new reference format.
- Do **not** populate a parallel database/auth/RBAC component catalog merely to satisfy this goal if the existing AppKit core already remains stable under arbitrary design changes. The dormant Trusted Components stack becomes implementation work only where the characterization test proves it is needed.
- Keep one truthful ejection rule: supported design and configuration changes retain verification; editing/ejecting protected core code removes verified status and runs the existing ejection path.

**Focused acceptance tests:**

- The same functional AppKit/RBAC identity and security probes pass in brutalist, editorial, and minimal builds whose page structure and visual output are observably different.
- A forum-with-RBAC request containing no style still selects the dependency chain; a brutalist forum request containing no RBAC requirement does not silently add it.
- The approved plan declares functional requirements independently of the `DesignSpec`, and a missing/incompatible functional requirement is corrected before execution without asking for a style keyword.
- Supported design, layout, and theme edits preserve the functional verification state; modifying or ejecting protected core code invalidates it visibly.
- Live authorization tests cover anonymous, user, moderator, and admin boundaries, including direct API attempts that bypass the UI.
- Normal Build can enter the AppKit route without a hidden mode omission, styling prompt, or model-invented schema.
- A characterization test decides whether existing AppKit seams are sufficient. A separate trusted-component implementation is forbidden unless that test demonstrates a concrete missing seam.

### 13. Settings providers, credentials, and vision onboarding

**Current root causes:**

- The universal encrypted credential vault is hidden under **Models -> Providers -> Advanced provider keys**, even though Firecrawl, Tavily, Brave, image, audio, and other non-model services use it. Data Sources can select a credential reference but cannot create the credential value, so the UI sends users to an unrelated part of Settings.
- Model vision is a real tri-state fact (`vision`, `text-only`, or `unknown`), but the manual control is buried under advanced metadata. Some provider catalogs expose modalities, some do not, and missing metadata is sometimes treated as proof of text-only. Runtime probing is startup-scoped and its result is not consistently shared between the app server and Agent server. The Build picker then loses the `unknown` distinction and reacts downstream.
- Build does not actually require its main driver to have vision. A text-only tool-capable driver can work with a confirmed dedicated visual model, or can honestly use text/DOM-only behavior. The hard failure occurs when an image-bearing request reaches a model that is not vision-capable. The current UI surfaces this too late and with provider-specific remediation text.

**Single credential contract:**

```text
choose a service
  -> choose an existing credential or add one in place
  -> save only its reference in feature configuration
  -> test it beside the feature that consumes it
```

**Settings information architecture:**

- Add one top-level **Providers & Keys** group. Move model-provider connections and OpenRouter setup there under **Model providers**, and make the existing encrypted **API credentials** vault visible rather than an Advanced disclosure.
- Keep **Models** focused on model assignments, model library/capabilities, and fallback/resilience behavior.
- Use one shared **Choose or add credential** dialog from Data Sources, model providers, image generation, audio, and other paid integrations. A service supplies a friendly label and canonical default reference; power users may reveal and change the reference name.
- For example: select Firecrawl, choose **Add Firecrawl key**, enter it once, save it through the existing encrypted secret API, bind the returned reference to extraction settings, then test Firecrawl in Data Sources.
- Keep one secret store and all existing reference-based DTOs and runtime resolution. Do not add Firecrawl-, Tavily-, Brave-, or provider-specific secret endpoints. Existing arbitrary/legacy references and encrypted values must round-trip without migration.
- The central vault reports only truthful storage state: `Stored`, `Missing`, or `Locked`. Functional connection tests live beside the consuming provider or feature; remove the misleading generic model-oriented **Test key** behavior for unrelated credentials.
- Preserve existing Settings anchors as aliases so old deep links continue to land on the relocated provider controls.

**Single vision-capability contract:**

```text
explicit user setting
  > current documented live/provider fact
  > conclusive known-model fact
  > unknown
```

- Keep one backend tri-state resolver whose only internal output is `{status, provenance}`, where provenance is `user`, `live probe`, `provider`, `known model`, or `unknown`. A missing capability list is `unknown`, never proof of text-only; only explicit modalities or a conclusively nonvisual model class may resolve false.
- Preserve wire compatibility for the existing manual `vision: true | false | null` pin. Store at most that pin plus one provenance-bearing detected fact needed across restart; never persist competing resolved vision fields. Every API and runtime consumer derives the same single `{status, provenance}` value.
- Use the same **Can this model understand images?** confirmation in every add/enable path: manual model add, generic provider browser, and OpenRouter selection. If the provider or a documented live endpoint already gives a conclusive answer, show the detected state and do not ask. If it is genuinely unknown, require **Supports images** or **Text only** before saving; cancel saves nothing.
- If context window and vision are both unknown, combine them into one **Confirm model details** dialog rather than stacking popups. Expose **Image understanding** in the normal model edit form, not under Advanced metadata.
- Do not reject a useful text-only coding model. When it is assigned to Drive, visual readiness is satisfied by either that confirmed vision-capable driver or a confirmed dedicated visual model. Filter the visual-model picker to confirmed vision models and validate the same rule server-side.
- Before a Build feature that promises pixel inspection starts, preflight that visual route. If none exists, lead directly to choosing or adding a visual model; allow explicitly labeled text/DOM-only operation only where that mode is truthful. Never start optimistically and fail later with a hard-coded environment-variable message.
- Carry `vision_status` and provenance through the driver API so the Build UI distinguishes `Unknown` from `Text only`. Image-bearing requests remain fail-closed, and errors name the actual model and the relevant Settings action.
- Route every pixel-bearing operation, including verifier work, through the same confirmed isolated visual route. The main driver may remain text-only. If no confirmed visual route exists, run only the explicitly labeled text/DOM-only behavior where that behavior is truthful.
- Old `vision=null` configurations remain valid unknowns, manual pins keep precedence, older clients may omit the field, and no arbitrary test image is sent merely to detect capability.

**Focused acceptance tests:**

- Settings no longer nests general providers/keys under Models, while legacy anchors and all stored provider assignments, encrypted keys, and references survive the move and restart.
- Firecrawl, Tavily, and Brave can each add or select a credential in their own setup flow; plaintext goes only to the secret endpoint and normal config contains only the reference.
- Missing, locked, replaced, cleared, legacy-named, and self-hosted no-key configurations render and probe truthfully without leaking plaintext to API responses, logs, or disk config.
- Explicit provider modalities map to vision or text-only, absent modality metadata maps to unknown, and a provider-declared exotic vision model survives config load.
- Known models enable without a popup; an unknown model opens exactly one confirmation; cancel writes nothing; the answer persists across restart and can be changed from the ordinary edit form.
- Manual true/false wins over automatic facts, current documented probes win when no manual pin exists, and adding a model after server start does not require restart to establish a stable capability fact.
- The dedicated visual picker excludes unknown/text-only models and the server rejects stale or forged invalid assignments.
- Build distinguishes `Unknown` from `Text only`; main vision, text driver plus visual observer, and explicitly degraded text/DOM-only routes each produce truthful UI and runtime behavior.
- Direct image requests to an incapable model still fail closed with model-specific remediation, while bounded visual inspection uses only the configured isolated visual route.

### 14. Production-grade search and extraction providers

**Verified current state:** Brave and Firecrawl can complete real work with the stored credentials. Live production adapters returned Brave results, produced Firecrawl passages, and completed a Brave-to-Firecrawl retrieval run. The active application remains configured for SearXNG plus Crawl4AI, so those optional providers are not exercised by ordinary current runs. Tavily has no stored key and its runtime adapter and Settings probe use an obsolete request shape: the key is placed in the JSON body while Tavily's current contract requires `Authorization: Bearer`. Tavily must therefore be treated as not working until corrected and live-tested.

**Root cause:** The optional providers each implement their own incomplete transport behavior. Tavily and Brave collapse authentication, rate limiting, timeouts, upstream errors, malformed responses, and genuine zero results into the same empty list. Firecrawl makes one attempt per URL, uses unbounded `gather`, and can receive up to 24 simultaneous scrapes from one exhaustive research turn. Settings maintains separate probe implementations and reports HTTP 429 as green. These failures are then misreported to the research model as `no_hits` or extraction failure, causing it to change good queries and amplify a provider outage into model thrash.

Ordinary Search intentionally calls its configured search provider directly rather than entering the Deep Research retrieval/agent loop. That keeps it fast, but also means engine-level query rewriting, RRF, and Deep Research admission logic do not improve it. Preserve the one-shot product; share only the truthful provider outcome, additive federation, and stable URL-deduplication boundaries it actually needs.

**Single contract:**

```text
model asks for retrieval
  -> host executes one bounded provider operation
  -> transient transport failure is retried below the model
  -> valid empty results remain empty
  -> exhausted provider failure remains a provider failure
  -> model pivots only for evidence/relevance reasons
```

**KISS plan:**

- Add one private asynchronous HTTP execution primitive for network-backed search and extraction adapters. It owns a per-provider concurrency semaphore, one total deadline, at most three attempts, cancellation-safe exponential jitter, and bounded `Retry-After` handling. Keep endpoint, authentication, payload, and response mapping in each adapter.
- Use one fixed search deadline and one fixed extraction deadline rather than inheriting the current universal 20-second timeout or adding provider-specific timeout loops.
- Apply the primitive first to Brave, Tavily, and Firecrawl. Migrate SearXNG and Crawl4AI only where characterization proves the same primitive preserves their existing timeout and diagnostic contracts; do not rewrite the stable baseline merely for uniformity.
- Retry only connection/read/transport timeouts and `408`, `429`, `500`, `502`, `503`, and `504`. Never retry authentication, configuration, plan/quota exhaustion, other non-transient 4xx responses, malformed schema, or a valid empty response. Never sleep past the operation's total deadline.
- Keep one configured default provider and never perform implicit fallback. Additional providers run only when explicitly selected for federation; healthy members may continue while a failed member is reported as a partial outage.
- Fix Tavily to Bearer authentication and current response parsing. Move Firecrawl to one explicit current V2 scrape contract. Do not add automatic V1/V2 guessing; a genuinely required custom legacy endpoint must be an explicit Advanced setting.
- Set Firecrawl's shared in-flight ceiling to two for this sweep. Changing it requires an explicit load-test result and configuration change. Reuse the cached extraction provider across research runs so rebuilding a per-run search override cannot create a new semaphore and evade the process/account limit.
- Isolate each extraction result so one malformed or unreadable URL cannot erase successful siblings. Require usable markdown/passages before calling a scrape successful.
- Implement the existing detailed-provider boundary for Tavily and Brave and extend extraction diagnostics with the same bounded facts: provider, outcome, status category/code, attempts, latency, retry wait, result/passage count, and maximum concurrency. Never record request bodies, headers, keys, or unrestricted provider error text.
- Preserve `empty` separately from `rate_limited`, `auth`, `quota`, `timeout`, `upstream`, and `invalid_response`. After retries are exhausted, Deep Research receives a genuine provider failure; it must not emit `no_hits` or ask the model to rewrite the query.
- Make **Test connection** invoke the same production request builder/parser instead of a duplicate probe implementation. Search tests require a parsed hit and extraction tests require a usable passage. A 429 is `Authenticated, temporarily rate-limited`, not green `Ready`.
- Define `configured_sources` as construction-ready rather than key-present: the credential resolves, origin is approved, and adapter configuration compiles. Report explicit connection-test health separately; do not turn a stale health check into a second configuration gate. A requested provider may never be silently dropped.
- Implement Section 1's additive source contract after this provider boundary is stable: prepend the configured default and then add selected sources through the normal federation/deduplication path.
- Keep ordinary Search one-shot and low-latency. Route it through the same detailed provider outcome and stable per-response/federated URL deduplication, but do not add the Deep Research planner, query rewriter, or evidence loop.

**Focused acceptance tests:**

- One parametrized chaos suite covers `429 + Retry-After -> success`, `503 -> success`, timeout then success, exhaustion, cancellation during backoff, non-retryable auth/plan responses, malformed JSON/schema, and a valid empty response for every affected adapter.
- Official-contract fixtures assert Tavily Bearer auth, Brave headers/freshness/result mapping, and Firecrawl V2 request/response mapping.
- Any time, past month, and past week remain mapped and tested for every search adapter that advertises recency; an unsupported provider reports that limitation instead of silently claiming the filter was applied.
- `3 queries x 8 extraction targets` and multiple simultaneous reports never exceed the configured Firecrawl in-flight ceiling.
- A mixed extraction batch preserves successful documents when one response is malformed, blocked, empty, or exhausted.
- Settings and production use the same adapter/request mapping; 429 and incompatible 200 responses cannot render green.
- Missing credential, locked credential, unapproved origin, explicit federation, and requested-provider-not-dropped cases are covered.
- The opt-in live harness exercises both ordinary Search and Deep Research with a chosen search/extraction pair and proves `hit -> readable passage -> redacted trace` without mutating saved Settings.
- Provider outage produces a provider failure/partial-outage trace and never increments a model query-pivot counter as if the query were irrelevant.
- Ordinary Search distinguishes empty from provider failure, deduplicates stable URL variants, and does not invoke a research planner or query rewriter.

### 15. First-iteration Docker-ready and download handoff

**Definitive root cause:** The backend intentionally appends `FINISHED` before copying the sandbox into project storage. The matching finish-triggered `WorkspaceVersionEvent` with `final_seal` is appended only after the immutable workspace and project manifest have persisted. WebSocket clients therefore see a real interval where execution is finished but the deliverable is not committed.

`BuildSurface` currently enables the release query on `status === FINISHED`, and that query is keyed only by conversation ID. On a first build, `/release` can run before the project manifest exists and return `project_not_found`; a slightly later request can cache a temporary `needs_review/source_not_snapshotted` result. Production React Query has retries and window-focus refetch disabled. The later sealed workspace event changes the event stream but neither the query key nor its enabled state, so the failed or stale result remains. A second iteration toggles the status away from and back to `FINISHED`, causing a new request after the project exists. This explains both the delay and its intermittent timing.

**Single readiness contract:**

```text
execution finished
  -> immutable workspace persisted
  -> matching final seal published
  -> release assessed against that exact version
  -> Docker-ready/download card rendered
```

**KISS plan:**

- Do not reorder the backend's terminal and sealing events, add polling, or add release-query backoff. The split protects terminal durability and restart recovery.
- Add one pure frontend selector for the current committed finish: the latest `FINISHED` terminal sequence plus a finish-triggered `WorkspaceVersionEvent` whose `final_seal.terminal_seq` matches it.
- Gate all immutable finished-handoff controls—not only `SelfHostPanel`, but bound download, manifest, and committed preview actions—on that exact matching seal rather than raw `FINISHED`.
- While `FINISHED` has no matching seal, render one compact `Preparing download…` state. This is an in-progress commit state, not a failure or alternate output.
- Key the release query by conversation ID plus the seal identity: terminal sequence, version sequence, and tree digest. Each committed iteration therefore receives one fresh assessment and can never reuse a prior version's error or verdict.
- Verify that the release response's version sequence and tree digest match the selected seal before rendering or creating a bound download. A mismatch is a typed host invariant violation, never a stale card or a polling trigger; a later valid seal naturally receives a new query key.
- Reuse this selector as the shared Build/Agent immutable-handoff primitive so Slides, Workflows, Reference Packs, and AppKit do not invent their own terminal-readiness checks.
- If final sealing genuinely fails, preserve the existing honest unsealed-finalization recovery path and show its persisted state. Do not make a second iteration the recovery mechanism.

**Focused acceptance tests:**

- `FINISHED` without a matching final seal makes zero release requests and shows `Preparing download…` rather than a blank card.
- An old seal plus a new `FINISHED` terminal cannot display or request the prior release.
- A matching seal triggers exactly one release request; its version/digest match the seal, and the card plus bound download appear during the first iteration without steering or another run.
- A second iteration uses a new query identity and never flashes the prior iteration's card.
- Resume may receive `FINISHED` state before replayed events; it waits for the replayed matching seal and then renders normally.
- Restart between terminal persistence and workspace capture completes through the existing finalization journal and produces the same card.
- A live E2E records terminal sequence `T`, seal event sequence `S`, sealed version/digest, release request/result, card render, and download binding. For the bounded acceptance fixture, the first card and working bound download must appear within five seconds of the matching seal; the run also reports `S - T` finalization latency.

## Test and observability plan

### Focused contract tests

- The configured search provider remains present for every additional-source combination.
- Unsupported research attachments never upload or display success.
- Process recreation followed by resume restores source selection.
- Running research defaults collapsed, remains keyboard accessible, and preserves the user's state as events arrive.
- Steering stays pending across malformed turns and acknowledges only the matching steer after a valid response.
- A report handoff selects the deck contract and cannot invoke the generic planner or shell tools.
- Both outline and fill prompts contain a unique report fact, and the final authored deck preserves it.
- Report handoff always selects structured generation and native PPTX.
- No missing dependency can turn a PPTX request into successful basic HTML.
- No-image configuration still produces themed SVG/vector art and editable authored JSON.
- Chat-Completions-style and Responses-style providers exercise the same injected slide interface.
- Successful deck generation produces native PPTX, authored JSON, branded preview, an editor-visible artifact, and one terminal `FINISHED` transition.
- AppKit functional capability selection is independent of visual style, and verified functional seams survive supported theme/layout changes.
- Credential onboarding from each consuming Settings section writes through the one encrypted store and persists only a reference.
- Vision-known, text-only, and unknown model additions follow the same provider-neutral decision flow and produce stable capability state after restart.
- Provider transport failures remain distinguishable from valid empty results and do not provoke model query thrash.
- The first Build iteration renders its release/download handoff after the matching final seal without a second iteration.

### Reliability harness

Extend the existing **research** harness only with provider diagnostics and report-to-deck correlation. It should accept a concurrent batch of research jobs and optional deck conversions and display, per job:

- normalized request and chosen depth/source controls;
- search/extraction request summaries, provider outcome, attempts, bounded wait, concurrency, and parsed result counts;
- model call start/end, returned provider fields, parse outcome, and retry reason;
- persisted action timeline;
- slide stage transitions and artifact validation facts;
- terminal state and elapsed time;
- no-progress signals, including repeated equivalent calls and any tool activity after successful artifact validation.

The harness acceptance run executes at least five varied research requests concurrently, including explicit baseline and optional-provider pairs, then converts their reports to decks. Secrets and raw private reasoning remain redacted; only bounded provider diagnostics may be retained.

Workflow, Reference Pack, AppKit, credential, vision, and release-handoff validation stay in their focused API/UI/E2E suites. The final acceptance wave aggregates their machine-readable result manifests; it does not reimplement those systems inside the research harness. The release E2E alone records terminal sequence, matching seal, release binding, first card-render time, and download binding.

## Luna execution plan after approval

The parent owns contract decisions, shared-file wiring, integration, and acceptance. Spawn at most three `gpt-5.6-luna` implementation agents at once. Each lane begins with a failing contract test, changes only its manifest allowlist, and returns a diff/test manifest. A partial wave never advances.

The parent reserves these shared seams unless a lane manifest explicitly assigns one: core event exports/unions, `frontend/src/types/agent.ts`, Agent tool registration, `build_loop_components.py`, `build_loop_factory.py`, both top-level Build surfaces, and research-harness orchestration. Parallel lanes implement bounded modules behind frozen interfaces; the parent performs the small fan-in hunks.

### Wave 0 — immutable baseline and contract freeze

- Because the worktree is already heavily modified, capture—not reset or stash—an immutable baseline: porcelain status, binary diff, and SHA-256 for every modified/untracked file. Existing user/in-flight changes remain authoritative.
- Generate one machine-readable lane manifest per Luna task containing exact allow/deny globs, baseline hashes for pre-dirty allowed files, required test commands, per-command timeout, required services/fixtures, and output-artifact paths. A changed-path guard fails the lane if it touches anything outside its allowlist.
- Reserve every currently dirty path for the parent by default. A dirty path may be assigned to one lane only after its baseline hash is pinned and the parent confirms that lane must build on the existing content.
- Freeze the shared contracts needed by later slices: provider outcome/attempt diagnostics; `additional_sources`; steer ID/applied event; ReportEvent/deck-job and provider-neutral slide completion; canonical workflow descriptor/readiness/start event; pack create/bind and `reference_inspect`; resolved vision status/provenance; and final-seal/release binding.
- Define the protected smoke matrix run after every fan-in: Deep Research, event replay/migration, deck generation, Build handoff, frontend/backend type/lint checks, architecture/import checks, and `git diff --check`.
- Give deterministic tests isolated owner IDs, conversations, temporary data/database directories, and ports. Retry/concurrency suites use fake clocks/transports. Serialize opt-in live provider/browser/model smoke tests after fan-in; never run them in parallel against shared credentials or saved Settings.
- Run the protected matrix and one five-request concurrent research baseline before spawning implementation lanes.

### Wave 1 — independent foundations (three parallel Luna lanes)

| Lane | Bounded ownership | Delivers | Lane gate |
|---|---|---|---|
| 1A Provider boundary | paid-provider transport/adapters and their focused tests; no research-agent or Settings UI files | Tavily/Brave/Firecrawl current contracts, bounded retry/concurrency, detailed outcomes, production request-builder seam | Parametrized chaos/load suite, SearXNG/Crawl4AI regression, serialized opt-in live smoke |
| 1B Research interaction | research activity/attachment/steer modules and tests; shared event union stays parent-owned | Default-collapsed activity, honest attachment allowlist, styled steer input, pending-to-applied lifecycle | Accessibility/UI tests plus malformed/retried-turn steer retention |
| 1C Slides core | report/deck pipeline modules and tests; shared event exports and top-level Build surface stay parent-owned | Typed handoff service, source-grounded outline/fill, provider-neutral calls, structured-deck-only output | Sentinel grounding, PPTX/artifact validation, no-image SVG, provider-shape tests |

**Parent fan-in 1:** wire the frozen provider diagnostic, steer event, and deck-job contracts only in reserved files; wire Settings test routes to the production request-builder seam. Verify lane changed-path manifests, run all lane commands plus the protected smoke matrix, record an integration hash/result manifest, then run one provider and report-to-deck smoke. Wave 2 starts only if the entire gate is green.

### Wave 2 — backend product binding (three parallel Luna lanes)

| Lane | Bounded ownership | Delivers | Depends on |
|---|---|---|---|
| 2A Search controls/resume | source composition, research checkpoint, SourcePicker and ordinary-search adapter modules; no paid-provider internals | Additive sources, no Web/DDGS false affordance, restart-stable selection, one-shot ordinary Search using the same detailed provider boundary and stable dedupe | Fan-in 1 provider contract |
| 2B Workflow runtime | workflow store/compiler/readiness/invocation/resume modules and focused tests; no shared registry/loop files or catalog UI | Canonical `ready()`, validated invocation, start event payload, sealed same-conversation handoff module, restart projection | Wave 0 workflow contracts |
| 2C Reference Pack backend | pack store/routes/Agent action/snapshot/materialization/inspection modules and focused tests; no Settings or top-level Build UI | Atomic owner-scoped packs, immutable Build binding service, `PACK.md` validator seam, bounded `reference_inspect` through the existing visual broker | Wave 0 pack/inspection contracts |

**Parent fan-in 2:** wire workflow and pack actions into the reserved Agent registry/loop files, export their shared events/types, and wire the frozen Build request fields without changing the top-level UI. Run invocation/tool-scope security, restart/resume, pack isolation, ordinary/deep source matrices, every lane command, and the protected smoke matrix. Record one integration manifest before Wave 3.

### Wave 3 — truthful user surfaces (three parallel Luna lanes)

| Lane | Bounded ownership | Delivers | Depends on |
|---|---|---|---|
| 3A Sealed deliverable handoff | finish-seal selector, release hook/query, handoff subcomponents and tests; parent owns final top-level mounts | First-iteration card/download keyed to the exact seal, with `Preparing download…` during commit | Fan-in 2 final-seal contract |
| 3B Providers & Keys | Settings navigation, shared credential chooser, provider setup/test components and tests; no model-capability resolver | Top-level Providers & Keys, in-context Firecrawl/Tavily/Brave credentials, production-backed truthful tests | Fan-in 1 provider seam |
| 3C Workflow UI | workflow catalog/detail/creator/run/schedule components and UI/API tests; no runtime/loop files | Simple capability library, schema-derived run form, describe-first creator, full Advanced tab | Fan-in 2 workflow runtime |

**Parent fan-in 3:** mount the seal-gated handoff in reserved Build/Agent surfaces, connect the workflow UI to the one invocation service, and reconcile Settings anchors. Run first-iteration/replay/restart handoff E2E, workflow UI-to-sealed-run E2E, credential secret-hygiene/restart tests, every lane command, and the protected smoke matrix.

### Wave 4 — Build integration (three parallel Luna lanes)

| Lane | Bounded ownership | Delivers | Depends on |
|---|---|---|---|
| 4A Reference Pack UI | Settings pack manager, Build-options picker/binding components, and pack E2E; parent owns shared Build creation/surface hunks | Agent-created packs manageable in Settings and pinned before Build's first model call | Fan-in 2 pack backend; Fan-in 3 Build surface |
| 4B AppKit characterization | AppKit mode/planner/generator tests and modules; parent owns any shared Build creation hunk | Proven functional/design independence and only the minimal decoupling a failing characterization test requires | Frozen AppKit/Build-mode contract |
| 4C Vision onboarding | model capability resolver/DTO, model-add/edit dialog, visual picker/preflight, and focused tests | One resolved vision fact, consistent unknown confirmation, shared pixel route, truthful text/DOM-only mode | Fan-in 1 provider facts; Fan-in 2 `reference_inspect` |

**Parent fan-in 4:** apply only the reserved Build creation/surface wiring for pack selection, AppKit mode, and visual readiness. Run pack sentinel/resume/isolation E2E, three-style RBAC authorization characterization, main/dedicated/no-vision paths, every lane command, and the protected smoke matrix.

### Wave 5 — final system acceptance

- Run five concurrent Deep Research requests across the baseline and configured optional providers; inspect redacted query/model/provider traces for infrastructure-induced thrash.
- Convert all five reports through the one deck path and visually inspect PPTX, authored JSON, and editor preview.
- Run ordinary Search, Agent -> Workflow, Agent files -> Reference Pack -> Build, two visually distinct AppKit/RBAC builds, and the first-iteration release/download E2E.
- Exercise restart during research, active workflow, bound Reference Pack Build, and the `FINISHED`-before-seal interval.
- Aggregate the focused-suite result manifests; do not reproduce their logic in a new mega-harness. Run repository lint, typecheck, architecture/import checks, backend/frontend focused suites, `git diff --check`, and the protected regression set.
- The parent performs the final KISS/architecture review and rejects duplicate stores, duplicated readiness/resolution rules, provider-specific retry loops, parallel renderers, polling-based readiness, or new model exit paths before sign-off.

## Explicit non-goals

- Making every opaque binary format semantically readable in the first pass.
- Building a second checkpoint or resume system.
- Fuzzy-title source merging.
- Adding semantic steer-compliance grading.
- Creating another slide renderer, deck schema, or fallback format.
- Changing general Agent turn semantics beyond the one host-owned workflow handoff and dynamic capability tools described here.
- Silently falling back between configured search/extraction providers.
- Adding provider-specific backoff loops when the shared bounded transport policy applies.
- Polling or generic release retries to hide the `FINISHED`-before-seal race.
- Activating Starter Recipes, Library Recipes, or the dormant generic Build-library composition stack.
- Making a visual theme or recipe name the selector for authentication, RBAC, or another functional capability.
- Replacing strict AppKit's whole-app recipe mode with a second whole-app generator or populating a parallel trusted-component catalog without a failing characterization test.
- Creating service-specific secret stores or credential APIs.
- Rejecting all text-only models or pretending unknown vision capability is a conclusive answer.

## Pending additions

None. The requested sweep is consolidated and ready for adversarial review before implementation approval.
