# Gap Analysis Addendum: Concrete Blueprints from Claude Code / Replit / Devin / Lovable / Base44 Research

> Produced 2026-06-07. ADDENDUM to `manus-gap-analysis.md` (backend/loop) and
> `manus-ui-gap-analysis.md` (UI/interaction). Assumes the reader has both docs
> open. Triage source: two deep-research reports on Manus + Claude Code + Replit
> + Devin + Lovable + Base44, run through per-pattern code triage. File:line
> citations are to `packages/.../disco/...` as noted, matching the base docs.
>
> This addendum does NOT restate the gaps. It records (a) patterns the base docs
> never named, and (b) concrete implementation recipes for gaps the base docs
> identified but left abstract.

---

## 1. What the new research changes

It overturns nothing in the two gap docs and **corrects two overclaims**; mostly
it supplies the concrete HOW the base docs deferred. The single most consequential
finding is a *correctness* one, not an enhancement: the egress allowlist is
**modeled but dead at the network layer** (`base.py:56-62` `egress_allowed()` is
never translated to any iptables/proxy rule; `gvisor.py:152`/`podman.py:245`/
`local.py:68` do a binary `none`/`bridge` choice), so `tool-sandbox-contract.md:36,87`
promises a deny-by-default guarantee the code does not deliver. The two doc
overclaims to fix: (1) ~~"true CodeAct / Manus-faithful" — `code_exec` was
*stateless one-shot*, file-state only, no persistent interpreter~~ **RESOLVED
(2026-06-09):** `code_exec` Python cells now share state across calls via a
namespace-serialization harness (`system.py`, `_CODEACT_RUNNER_SRC`) — names
defined in one cell are in scope in the next, like a notebook kernel. With `dill`
present even functions/imports carry; without it, picklable data values carry
(pickle fallback); unserializable handles never carry (by design). Not a live
in-process kernel (the sandbox exposes only write_file + exec_shell), but the
across-cell *statefulness* the claim implied is now real + tested against the
process sandbox. Node remains one-shot (documented). (2) the `AlternativesEvent`
docstring (`events.py:393-399`)
describes a 4-strike circuit breaker via a `propose_alternatives` tool that **does
not exist** (grep finds it only in two docstrings). Everything else is additive HOW.

---

## 2. Genuinely new findings (NOT in the existing gap docs)

### 2.1 Forbid shell-based file edits; steer to dedicated file APIs (Manus `<file_rules>`)
**Maps to: none.** Severity medium; status partial.

**What it is.** Raw-shell string escaping (heredocs, `>`/`>>`, `sed -i`, `tee`)
triggers severe escape-sequence hallucination in LLMs on quotes/`$`/backticks/newlines.
Manus explicitly prohibits bash file edits and routes all create/read/append/replace
through dedicated file tools.

**What our code does.** The *structural* half is already good: `file_read`/`file_write`/
`file_list`/`file_edit` exist (`packages/tools/.../builtin/files.py`; `FileWriteTool:41-56`
full-content write, `FileEditTool:88-108` first-occurrence replace with a not-found
failure outcome at `:100-105`), both returning `artifacts`. The intent is even
documented (`files.py:1-6` docstring: "NOT shell redirection"). But the **model-facing
surface does not express the preference**: the shell tool description is neutral
("Run a shell command inside the sandbox", `system.py:63`), the driver prompt lists
tools flat with no ordering (`prompts.py:134-137`), there is no `<file_rules>` block
anywhere, and there is no `file_append` (so `>>` is the only path to append).

**Why it matters.** The corruption failure mode is silent and data-destroying; the
fix is near-zero-cost steering of tools we already built.

**Blueprint.** Prompt-first captures ~90%:
1. **Prompt** — insert a `<file_rules>` paragraph into `_EXECUTION_DRIVER_PROMPT`
   (`prompts.py:134`): "FILE EDITS GO THROUGH FILE TOOLS. `file_write` to create/overwrite,
   `file_edit` (old/new) to change part. Do NOT modify files with shell — no
   `cat <<EOF`, no `>`/`>>`, no `sed`/`awk`/`tee` in-place. Shell is for commands
   (installs, builds, tests, git). Raw-shell file writing corrupts on quotes, `$`,
   backticks, newlines." Mirror one line into `_PLANNING_DRIVER_PROMPT`.
2. **Tool descriptions** (always present even under truncation) — `system.py:63` →
   "Run a shell command (installs, builds, tests, git). NOT for creating/editing
   files — use file_write/file_edit"; prepend `file_write` desc (`files.py:44`)
   with "Preferred way to author file content — avoids shell-escaping corruption."
3. Add **`file_append`** (new `FileAppendTool` in `files.py` mirroring `FileWriteTool`)
   to close the one legitimate reason to reach for `>>`.
4. **Optional hard guard** — in `ShellTool.run` (`system.py:70`) regex-detect in-band
   file authoring (`<<\s*['"]?\w+`, ` > `, ` >> `, `sed -i`, `tee `) and return a soft
   failure nudging file_write/file_edit. Purely additive — APIs already exist.

### 2.2 notify (non-blocking) vs ask (blocking) message-tool partition
**Maps to: GAP B + UI 2.3.** Severity high; status partial. (Also a concrete HOW for a
known gap — see §3.2; listed here because the `notify` half is a genuinely missing tool.)

The **blocking `ask_user`** half is fully built (`engine.py:128-185` virtual tool,
appended in execution mode `:386-395`, intercepted and never executed, halts at
`AWAITING_USER_DECISION`, emits `AlternativesEvent` with options `:1091-1102` or a
halting MessageEvent without `:1103-1124`; frontend recognizes the status `types/agent.ts:14-15`).
The **non-blocking `notify`** half does **not exist as a tool** (grep for notify/non_blocking
in `packages/*.py` is empty). Progress is carried implicitly on every action's `thought`
field plus the dangerous mid-run prose-reply convention (`prompts.py:142-147`), and the
GAP B root bug is still live: `agent.py:105-108` returns `finished=True` unconditionally
for any tool-less turn. Recipe in §3.2.

### 2.3 OS-level safety hooks: PreToolUse hard-deny + egress-interception proxy
**Maps to: none.** Severity high; status partial.

**What it is.** Claude Code enforces safety at the OS layer, not via prompt: a
PreToolUse regex that **denies** independent of LLM compliance, and an egress
interception proxy that triggers per-domain human approval.

**What our code does — already strong.** We have a tiered, honest OS isolation layer
(`isolation.py:48-90`: gVisor/runsc adversarial-safe, Podman-remote shared-kernel,
local/runc, unknown→weakest fail-safe `_FALLBACK:83`; `gvisor.py` hard-fails without
runtime `:98-108`, never pulls images `:110-124`, injects no host env `environment={}` `:159`).
We have a real PreToolUse-equivalent: `RuleBasedAnalyzer` (`security/analyzers.py:38-100`)
with always-on `_SHELL_HIGH` regexes (sudo, mkfs, dd, fork-bomb, `nc -e`, `curl|sh`,
iptables, `_destructive_rm:78-84`); the LLM analyzer is additive-only and forbidden from
lowering the verdict (`LLMBasedAnalyzer:187-192`, `EnsembleAnalyzer.max_risk:256`). Gate
runs before OS exec (`engine.py:1134-1165`). We even **beat** Claude Code's flat model
with isolation→confirmation coupling (`isolation.py:40-44` `recommended_confirmation()`:
weaker tier ⇒ strictly tighter gate).

**The two real gaps.**
- **A. No hard deny.** Even a HIGH `_SHELL_HIGH` match only routes to human confirm
  (`engine.py:1151` `should_confirm`). No verdict refuses outright; `reject` appears
  in `security-analyzer-contract.md:212` only as an event the agent sees, not an OS block.
- **B. Egress allowlist unenforced (the load-bearing finding).** `egress_allowed()`
  (`base.py:56-62`) is dead code at the network layer; `sealed()` (`_container.py:32-33`)
  is true only with NO allowlist AND no NETWORK cap, so any previewable/NETWORK box gets
  **full bridge** networking. On shared-kernel tiers this is a real exfil surface.

**Blueprint.**
1. **Egress-interception proxy** (fills dead `egress_allow`): run a sidecar on a
   per-instance Docker network; point guest `network_mode` at it; set `HTTP_PROXY`/
   `HTTPS_PROXY`/`http_proxy` in the guest env (`gvisor.py:159`, currently `{}`). Sidecar
   ACL = `spec.egress_allow`, wiring `egress_allowed(host)` (`base.py:59`) as the live
   predicate. Non-allowlisted domain ⇒ hold connection, emit an approval event →
   `WAITING_FOR_CONFIRMATION` (`engine.py:1155`, carry an egress payload instead of an
   action id); approve adds host to live ACL, deny returns 403. This is notify-vs-ask
   applied to the network.
2. **Hard-deny verdict**: add a `DENY` tier above HIGH (raw-device writes `>/dev/sd*`,
   fork bombs, mkfs) refused at `engine.py:1151` before `should_confirm` — emit a refusal
   ActionEvent the agent sees (`security-contract.md:212`) and never execute, regardless
   of policy or LLM. Makes the regex a true OS-level block.
3. **expose_port self-test** before handing a URL (see §3.3).

### 2.4 Visual self-verification before finish/deploy
**Maps to: none directly** (tangential to GAP E, GAP F, UI 2.1/2.2 — but those are about
SHOWING the human, not the AGENT verifying). Severity high; status **missing**.

**What it is.** The agent navigates its own built routes in a headless browser, captures
screenshots + console errors + exit codes, SEES its own output, and gates completion on it
(Replit `web_application_feedback_tool`; Manus mandatory headless self-test before deploy).

**What our code does.** Nothing. No headless/screenshot/console capability anywhere
(grep chromium|chrome|playwright|puppeteer|screenshot|console_log is empty). The only
web-reach tool is `curl -sL` reduced through a deterministic HTML quarantine parser —
never renders, never executes JS, never screenshots (`builtin/browser.py:195-212`,
`_quarantine:109-123`). Exit codes are captured but passively (`system.py:35-52` into
`ToolOutcome.structured`); nothing enforces a check. The preview path is human-facing only
and never returns to the agent (`expose_port` URL `_container.py:194-210` → proxy
`runtime.py:953-985` → iframe `ExecutionCanvas.tsx:91-130`). The FINISHED gate verifies
process completeness only (`_has_productive_work ~488-501`, plan-step-done `~531-564`),
never output correctness. `deploy_preview` is an unimplemented stub (`registry.py:63`).

**Blueprint.** New builtin `verify_app` tool (`builtin/`), `Capability.SHELL+DISPLAY`,
`runs_in="sandbox"`: (a) ensure dev server up on `PREVIEW_PORT=8000` (reuse the static
server `gvisor.py:27-33`); (b) headless-render a route in-sandbox (`chromium --headless
--screenshot=/workspace/_verify.png --dump-dom http://localhost:8000/<route>` or Playwright
via code_exec) + capture console messages; (c) return structured `{exit_code, console_errors[],
screenshot_path, dom_title}` mirroring `system.py:_exec_outcome`. **Surface the screenshot
back to the AGENT** as an ObservationEvent (the missing "agent sees its own output" channel).
Gate completion: extend the FINISHED transition (`~488-564`) with "for buildable artifacts,
a passing `verify_app` since last write is required, else refuse→STUCK/auto-continue."
Prompt: add a verify phase to `_EXECUTION_DRIVER_PROMPT`. **Cheap MVP** with no chromium:
degrade to curl exit-code + HTTP status + non-empty DOM title via the existing `_quarantine`
path — and **flag it honestly** as not visual verification (no false affordance).

### 2.5 On-the-fly image/asset generation (Lovable dimension-gated diffusion)
**Maps to: none.** Severity medium; status **missing**.

**What it is.** A first-class DESIGN capability producing real visual assets (logos, hero
images, OG images, favicons, palettes) for the generated site, with model-by-dimension
selection (Lovable: flux.schnell for <1000px UI elements, flux.dev for hero/full-screen).

**What our code does.** No image generation. Builtins (`builtin/__init__.py:31-56`) are
text/file/search only. The file tools are **UTF-8-text-only and cannot write binary**:
`FileWriteTool` does `args.content.encode("utf-8")` (`files.py:52`), `FileReadTool` does
`decode("utf-8", errors="replace")` (`files.py:33`), `FileWriteArgs.content: str` (`files.py:38`).
The router roles are all text/cross-encoder (no diffusion role). Closest existing concepts
are BoD §12.8 visual-iteration and §13.7 design tokens — about styling rendered HTML, not
generating original assets.

**Blueprint.**
1. New `generate_image(prompt, width, height, purpose)` tool (new `builtin/media.py`),
   EGRESS+FILESYSTEM cap, `runs_in="sandbox"`.
2. Dimension-gated model selection: `max(w,h) < 1000` ⇒ fast (flux.schnell-class, ~4 steps);
   else ⇒ high-fidelity (flux.dev-class, ~28 steps). New 5th router role (BoD §15.2), route
   to a self-hosted diffusion endpoint else overflow to a hosted API (§15.3).
3. **Prerequisite — binary-safe write path** (current tools block this): add
   `file_write_binary(path, content_b64)` OR write directly via `ctx.sandbox.write_file(path,
   raw_bytes)` (the sandbox layer already takes bytes at `files.py:53`; only the public surface
   is UTF-8-clamped). Return path in `ToolOutcome.artifacts`.
4. Wire into the §12.8 critic loop (render → screenshot → vision-critique → regenerate),
   scope edits via version history (§13.5) so regen never clobbers manual tweaks.
5. Palette derivation = cheap deterministic dominant-color extraction snapped to §13.7
   60-30-10, not an LLM call. **Security seam:** image-gen egress through the deny-by-default
   allowlist (§17.4, see §2.3); treat the generated asset as untrusted (SVG can carry script)
   and sanitize before any preview iframe.

### 2.6 Controlled structural noise injection to break self-imitation loops
**Maps to: GAP F.** Severity medium; status divergent. (Concrete HOW for a known gap — see §3.5;
the *serialization-jitter* half is genuinely new and not named in GAP F.)

Our pipeline is maximally deterministic (agent temp 0.0 `agent.py:77-87`/`types.py:95`; pure
per-event templates `view.py:29-33`, `events.py:248-278`), and StuckDetector merely HALTS on
repetition (`engine.py:804-807`) — give up, never escape. GAP F named the temperature half; the
new addition is the **serialization-jitter** half and an **escape-instead-of-halt** recipe (§3.5).

---

## 3. Concrete blueprints for KNOWN gaps

### 3.1 GAP A — the 5-shaper compaction cascade is the missing tier ordering
We run exactly ONE shaper (the lossiest) plus a reactive emergency, with none of the cheap
upstream layers. GAP A said "add a compaction tier before summarization" but treated it as two
tiers; the research gives the **ordering** and the cheap intermediate layers it never named.
Build a pipeline in `_materialize_view` (`engine.py`, before view build ~`:837`) running
cheapest-and-most-lossless first:

- **S1 Budget Reduction** (REPLACES GAP A fix 1): live formula, not static literals. Today
  thresholds are constructor literals `max_tokens=24_000, hard_max_tokens=32_000` (`view.py:166-167`)
  wired with bare defaults `LLMSummarizingCondenser()` (`runtime.py:446`) — a 200k-window model
  still condenses at 32k. Pull `context_window` from the routed model's `CapabilityProfile`
  (`config.py:163,215` carry 131_072/200_000), set `soft=0.65*ctx`, `hard=0.80*ctx`, lower
  `keep_recent`/`min_forget` as spend climbs, pass into the condenser at `runtime.py:446`. Pure
  arithmetic, no model call. Also fix the naive trigger estimate `sum(len(m.content))//4`
  (`engine.py:364-366`) which ignores tool-call JSON/schemas/system prompt.
- **S2 Snip** (NEW, not in GAP A): cap each tool observation at ingestion, before the View
  (`events.py` rendering for code_exec/shell/extract) — head+tail with `[snipped N lines — re-run
  / file_read for full]`. Deterministic, reversible (bytes still on disk; dovetails GAP C).
  Currently the only `max_chars` lives in retrieval (`live.py:140,206` 16k; `synthesis.py:95` ~600),
  not on agent-context observations.
- **S3 Microcompact** (NEW): no-model pass dropping turns with no durable state change (no file
  write, no plan_step, error superseded by a later success), replaced with a one-line tombstone.
  This is what the condenser's contiguous-middle `min_forget` guard (`view.py:209-213`) structurally
  cannot express (it never forgets interior no-ops).
- **S4 Context Collapse**: our existing `LLMSummarizingCondenser` (`view.py:146-227`, keep_head=1/
  keep_recent=6) — but fires only on the residue after S1–S3, upgraded to GAP A fix 3: structured
  schema (files-touched / decisions / plan-status / open-threads / tried-and-failed), excluding PlanEvent.
- **S5 Auto-Compact**: make `_hard_reset` (`engine.py:846-855`, today just re-runs the summarizer on
  caught `LLMContextWindowExceeded`) a real flush retaining ONLY persistent file-memory pointers
  (PATH list + pinned plan from GAP D) — wire to the GAP C filesystem-as-memory substrate.

**Net value beyond GAP A:** the cascade SHAPE — three cheap deterministic layers (S1 dynamic
thresholds, S2 Snip, S3 Microcompact) that run BEFORE any model-call summarization, making the lossy
collapse the last resort instead of the only resort. S1 and S2 are a few hours each and reclaim most
of the win with no model call.

### 3.2 GAP B + UI 2.3 — notify/ask partition + affirmative completion
Replace the single overloaded "tool-less prose = ask OR done" channel with a strict two-tool
partition, mirroring the `ask_user` pattern that already exists:
1. Add a **`notify_user`** virtual tool alongside `ask_user` in the append list (`engine.py:386-395`),
   schema `{message, optional attachments:[path]}`. Intercept like `ask_user` (`engine.py:1091`) but
   emit `MessageEvent(source=AGENT)` and **CONTINUE** (no StatusEvent, no halt) — the explicit
   non-blocking path the dead prose branch at `engine.py:1042` was groping toward.
2. **Make completion affirmative:** stop treating tool-less turns as finished — `agent.py:105-108`
   must return `finished=False` for prose-only (routes to the genuine no-op continue at `engine.py:1042`),
   and require an explicit **`finish`/`done`** virtual tool. notify carries progress without ending;
   finish ends; ask halts. Removes the `prompts.py:142-147` "reply with no tool call" land-mine GAP B
   flagged as actively dangerous.
3. Rewrite `_EXECUTION_DRIVER_PROMPT` (`prompts.py:134-162`): "DEFAULT to notify_user for progress and
   mid-run answers — never stops the run. ask_user ONLY when genuinely blocked. Call finish only when
   every step is done."
4. **Frontend:** notify → ordinary narration (`attention:false`, `buildTrace.ts:168`); ask → `attention:true`
   + dedicated AWAITING status — now justified because the two are tool-distinguishable at the wire level.

### 3.3 GAP E + UI 2.1/2.2 — 0.0.0.0 bind + proxy + mandatory pre-expose self-test
We have proxy + bind halves, not self-test. Proxy is arguably better than Manus's random public domain:
single tailnet origin (`agent_server/app.py:130-144` → `runtime.preview_upstream` `runtime.py:953-963` →
`session.expose_port` `_container.py:193-210`), never a raw container port to the browser. Bind 0.0.0.0
is *implicit* only (auto static server `gvisor.py:27-34` binds 0.0.0.0; Docker publishes that one port
`gvisor.py:143,153`) — nothing tells the agent to bind ITS OWN dev server (vite/next) to 0.0.0.0, so
`npm run dev` binds 127.0.0.1 and the proxy can't reach it. Self-test is MISSING; the browser tool is
text-only (no render/screenshot/console); `deploy_preview` is a stub; shell has a hard timeout ceiling
(`system.py:26,72`) so a real `npm run dev` gets killed.

Three additions:
1. **Bind rule as prompt contract**: add `<deploy_rules>` — "any dev server MUST bind `0.0.0.0:8000`
   (`vite --host 0.0.0.0 --port 8000`, `next dev -H 0.0.0.0 -p 8000`, `python -m http.server 8000 --bind
   0.0.0.0`), never localhost." Infra already publishes 0.0.0.0:8000 (`gvisor.py:143`); only the model
   instruction is missing.
2. **Long-running `serve`/`start_server` tool**: runs detached (`nohup CMD >/tmp/devserver.log 2>&1 &`),
   records PID in a per-conversation process registry, returns immediately (bypasses the timeout ceiling),
   tails `/tmp/devserver.log` so boot errors surface.
3. **Mandatory pre-expose self-test** (the new HOW): before done, (a) `preview_check` — backend `httpx`
   GET against `preview_upstream + "/"`, assert 200 + first-bytes; (b) headless render via the §2.4
   `verify_app` capability for layout/JS-error verification. Sequence: write files → serve (0.0.0.0:8000,
   detached) → preview_check 200 → headless self-verify → surface the persistent proxy URL as the
   deliverable. Wire that existing persistent proxy URL (`runtime.preview`) into the DeliverablePanel
   `deployment_url` (UI 2.2) rather than inventing a deploy.

### 3.4 GAP G (+ GAP C) — lazy/path-scoped skills + a capped agent-written MEMORY.md
Our Skills are the OPPOSITE of lazy: `render_skills_for_prompt` (`skills.py:185-202`) concatenates EVERY
enabled skill's FULL body into one block, built per request (`runtime.py:245`) and prepended unconditionally
to BOTH prompts (`prompts.py:191-203`). 64KB per-body cap (`skills.py:84`), no aggregate cap. The MEMORY.md
analog is absent — Skills are hand-edited via the Settings UI only (`skill_store.save` reached only from
`config_state.py:350`, never the loop). `file_read` still dumps whole files (no offset/limit), no `.pmx/`
spill (GAP C items 1-2 open).

Two recipes:
- **(A) Lazy/path-scoped injection** — add an optional `scope` frontmatter glob to the Skill model
  (`skills.py:36-45`). In `render_skills_for_prompt`, two tiers: ALWAYS inject a one-line MANIFEST per
  skill (name+description, ~15 tokens, the CLAUDE.md table-of-contents), inject the FULL body ONLY when
  scope matches the step's touched path/intent. Wire the trigger at `runtime.py:245` via
  `render_skills_for_prompt(skills, active_paths=...)`. Converts N-full-bodies-every-turn into
  1-manifest + only-relevant-bodies.
- **(B) MEMORY.md as a capped, agent-written, condensation-immune KnowledgeEvent** — agent appends durable
  facts (API schema, house-style) to `.pmx/MEMORY.md` via `file_write`; per request load only first
  25KB/200 lines into the prompt as a pinned, condensation-exempt KnowledgeEvent (the GAP G event type,
  now given a backing store + cap + load policy); deeper recall goes through `file_read(offset/limit)` —
  which makes **GAP C item 1 (offset/limit) a HARD PREREQUISITE**. The 25KB-first-page + search-the-rest
  split bounds startup cost while keeping unbounded learned memory addressable, and closes the
  "API schema learned mid-run → condensed to lossy prose → hallucinated endpoint" hazard.

### 3.5 GAP F — noise injection + temperature + escape-instead-of-halt
GAP F named only the temperature half (AGENT_DRIVER temp 0.3-0.5, summarizer 0.0). Add:
1. **Structural jitter layer** (new): deterministic-but-varying serialization seed = `hash(conversation_id,
   event.seq)` selecting from 3-4 equivalent phrasings for the wrapper text in `ObservationEvent`/
   `AgentErrorEvent` (`events.py:248-278`) and the system-reminder nudges (`engine.py:823-831`). Keep
   `View.of` pure (same events → same messages, `view.py:29-33`) by threading the seed deterministically
   and varying only NEW tail tokens — preserves the KV-cache friendliness GAP H wants.
2. **Escalating noise on stuck** (replaces bare halt): make StuckDetector graded. At repeat-count n-1
   (`stuck.py:27-33`), BEFORE halting: bump AGENT_DRIVER temp for the next single step (0.0→0.7), inject a
   varied reframe reminder, rotate the serialization seed for the repeated observation. Only if the escape
   step still `event_content_eq`-repeats do we emit `StatusEvent(STUCK)` (`engine.py:806`). Turns the
   give-up gate into a loop-break.
3. Make the existing constant nudges (`engine.py:816-835`) draw from a 3-variant pool keyed on fire-count,
   so a re-fired reminder isn't itself a self-imitation seed.

### 3.6 GAP H — explicit prompt caching, and kill the mode-boundary cache break
Our assembly is structurally cache-friendly (pure append-only `View.of` `view.py:25-92`, no timestamps in
the prompt path) but emits ZERO cache markers and actively breaks the prefix at the PLANNING↔EXECUTION
boundary (different prompt strings `prompts.py`; tools array mutated `_tools_for_step` `engine.py:380-396`).
`openai_provider._payload` (`:138-165`) has no `cache_control`/`prompt_cache_key`; `json.dumps` lacks
`sort_keys=True` (`:130`) so tool-call args can drift key order; cost is hardcoded `0.0` and cached_tokens
never parsed.
1. **Cache-breakpoint placement** (Anthropic max 4 `cache_control:{type:ephemeral}`): mark (a) end of
   system block, (b) end of tools array (tools precede messages in the prefix), (c) last message of the
   stable head, (d) a rolling marker on the second-to-last turn (incremental caching of the growing
   transcript — Devin/Claude Code). Thread via a new optional field on `CompletionRequest` (`types.py:85`),
   consumed in the provider — not hardcoded.
2. **Kill the mode-boundary break** (do this FIRST, independently — pure-local correctness win even before
   any markers): make the system prompt a SINGLE stable superset for both modes, move the mode signal into
   an appended `<system-reminder>`/`current_mode` line; and STOP filtering tools by mode (`engine.py:382-394`)
   — expose the full small stable tool set every turn (Manus "mask, don't remove"), gate planning/execution
   in the prompt. Tool removal is the single most expensive invalidation.
3. **Deterministic serialization**: `sort_keys=True` at `openai_provider.py:130` and anywhere ActionEvent
   re-serializes args.
4. **Measure**: parse `prompt_tokens_details.cached_tokens` / `cache_read_input_tokens` +
   `cache_creation_input_tokens` in `_to_response` (`:192-208`) into new `TokenUsage` fields; price cache-read
   ~0.1x, cache-write ~1.25x. Without this a silently-broken prefix is invisible except on the bill.

### 3.7 GAP F + GAP D + UI 2.3 — harness-enforced 3-strike circuit breaker
None of our halt paths is a "tried N distinct approaches → ask the human" escalation; the one documented as
such is **unwired**. StuckDetector fires only on byte-identical repetition (`stuck.py:89-96` needs 3 identical
cycles) — three DIFFERENT failures never trip it, and on a hit it freezes (`StatusEvent(STUCK)`,
`engine.py:805-807`) reopened only by manual user input. `ask_user` is a clean blocking escalation but 100%
model-discretionary; its own description tells the model to call it "after 2-3 distinct approaches"
(`engine.py:142-146`) yet the harness never triggers it. `propose_alternatives` exists ONLY in two docstrings
(`events.py:189,396`) — no tool, no counter (the contract lies). `consecutive_tool_errors` is computed
(`engine.py:351-361`) but advisory-only. Net: 3 distinct failures + a passive model = grind to
`max_iterations=500` (`engine.py:795-802`) → bare ErrorEvent.

GAP F said "add an `<error_handling>` ladder to the PROMPT" (trusts a local Qwen to self-escalate). This
research says **hard-code the breaker in the LOOP**:
- **(A)** Pure detector beside StuckDetector — `consecutive_distinct_failures(recent)`: walk back from the
  tail counting `AgentErrorEvent` not separated by a successful ObservationEvent or USER message; reuse
  `_after_last_user_message` (`stuck.py:35`) so a fresh instruction resets. Counts DISTINCT failures.
  Threshold = 3, as `StuckThresholds.distinct_failures: int = 3` (`stuck.py:27`).
- **(B)** In `engine.py` between stuck-check (`:805`) and View build (`:837`): if `distinct_failures >= 3`
  and the model didn't itself emit ask_user/propose_plan_update this episode, the harness synthesizes the
  escalation (don't freeze, don't silently continue), two-tier:
  - Tier 1 (3): inject ONE `<system-reminder>` naming the 3 failed approaches (the `AgentErrorEvent.error`
    strings) + "you've tried N distinct approaches; call ask_user(question, options) or propose_plan_update"
    (mirrors `_EXECUTION_NUDGE` `engine.py:107`).
  - Tier 2 (4th, still failing): harness STOPS waiting — builds the `AlternativesEvent` itself and halts at
    `AWAITING_USER_DECISION`, finally wiring `events.py:393`'s documented "failed 4 times → loop asks." Build
    options from the distinct failed ActionEvents via the existing `AlternativeOption` shape (`engine.py:451`)
    so the user gets clickable next-steps, not a freeze.
- **(C)** Caps the GRIND path at ~4 with a handoff instead of `max_iterations=500`.
- **(D)** Resolve the phantom: ship `propose_alternatives` as a real tool OR correct the `AlternativesEvent`
  docstring (`events.py:393-399`) — today it's a no-false-affordances violation at the contract layer.
- **UI:** the `AWAITING_USER_DECISION` this now reaches needs the AskPanel/blocking status UI 2.3 already
  specs — this research is what makes that gate actually FIRE on failure.

### 3.8 GAP E — CodeAct is one-shot, not persistent (overclaim correction + recipe)
> **RESOLVED 2026-06-09** (different mechanism than the recipe below). Rather than add an `exec_code`
> kernel primitive to every backend, Python `code_exec` now wraps each cell in a namespace-serialization
> runner (`system.py` `_CODEACT_RUNNER_SRC`): restore the prior namespace → run the cell → persist the
> still-serializable names. dill carries functions/imports; pickle fallback carries data; unserializable
> handles never carry. Across-cell statefulness is real + tested (`test_code_exec_python_state_persists_across_cells`).
> The kernel-primitive recipe below is kept as the path to a *live* interpreter if file-mediated state proves limiting.

Correct first: GAP E (`manus-gap-analysis.md:148-150`) and §3 (`:209`) assert "true CodeAct / Manus-faithful";
code proves it's stateless one-shot, file-state only. `CodeExecTool.run` (`system.py:92-99`) writes
`_codeact.{ext}` and runs `{interp} {fname}` via `exec_shell`, which spawns a FRESH subprocess per call
(`process.py:63-87`) that exits — in-memory variable state is discarded. The workspace filesystem persists
(`session.py:32-129`) but no live interpreter; `SandboxInstance` (`base.py:64-86`) has no REPL/kernel primitive
(grep kernel|jupyter|ipython|REPL is empty). Genuine: full native library access (`python-node-base` image
`base.py:48`) with file-mediated state.

Persistent-kernel recipe fitting the Protocol seam:
1. Add ONE additive method to `SandboxInstance` (`base.py:64`): `async def exec_code(self, language, code, *,
   timeout_s) -> ExecResult`, default-None like `expose_port` so existing backends are unaffected.
2. Back it with a long-lived interpreter per session: python via `ipython kernel` (or a thin `python -u` driver
   reading code blocks over a pipe) launched ONCE at first `exec_code`, PID held in `ProcessSandboxInstance`,
   each snippet fed to the SAME process so `df = pd.read_csv(...)` in call 1 is visible in call 2; node via a
   persistent `node --experimental-repl-await` REPL over stdin.
3. Route `CodeExecTool` (`system.py:92`) to `ctx.sandbox.exec_code(...)` when available, falling back to today's
   write-file+`python3 file` path when the backend returns None.
4. `SandboxSession._resilient` already re-creates on death (`session.py:96-111`) — extend to re-launch the
   kernel and surface the existing "state lost, generation N" signal so the agent KNOWS its variables vanished.
5. Keep `_GRACE_S=5` (`system.py:23`) so a hung cell returns `timed_out` cleanly. Net new: cross-call variable
   persistence (load-once/query-many dataframes, incremental scraping, live API clients) — the half of Manus
   CodeAct we actually lack.

---

## 4. Where we already match or beat the field

- **Completion gate is Manus-style forced continuation, not refuse-and-stall** (and the base doc already
  records this as "done well"). On a finished-but-incomplete turn, `engine.py:969` injects an environment
  reminder and `continue`s (`:973-1001`), capped at 3 per user message (`:325`, reset at the last USER
  MessageEvent `:511-527`), landing a clean FINISHED/`partial_plan` (`:1024-1030`) — explicitly NOT STUCK
  (`:1004-1006`). **The one upgrade the research adds:** Manus verifies COMPUTATIONALLY against the
  environment; ours verifies only self-attested `plan_step(idx,'done')` checkboxes — the same model that
  wants to stop controls the truth source. Bolt per-step **verify predicates** onto the existing gate
  (no new event types): extend `propose_plan` with optional `{"verify": {"kind":"file_exists","path":...}}`
  / `{"kind":"expose_port","expect_status":200}` / `{"kind":"shell_exit_zero","cmd":...}`; in
  `_plan_is_incomplete` (`:530-565`) a step is done IFF `plan_step(idx,'done')` was emitted AND the predicate
  re-passes against the live workspace; reuse the auto-continue cascade verbatim but name the FAILED predicate
  in the reminder. Predicate eval is stateless/log-derivable, no model call — same discipline as
  `_plan_step_lag_signal`. This closes the mark-done-and-bail hole.
- **OS-isolation tier is honest and tiered, and isolation→confirmation coupling beats Claude Code's flat model**
  (`isolation.py:40-90`; see §2.3). The PreToolUse-equivalent regex floor is injection-resistant
  (`analyzers.py`, `EnsembleAnalyzer.max_risk:256`). We need hard-deny + egress enforcement, not the floor.
- **Blocking `ask_user` is fully built and faithful** (`engine.py:128-185`, halts at `AWAITING_USER_DECISION`)
  — we lack only the non-blocking `notify` twin and the harness trigger.
- **Immutable append-only log + pure deterministic `View.of`** (`view.py:25-92`) already gives us Manus's
  "stable timestamp-free prefix" property by construction — we just don't yet emit cache markers (§3.6).
- **Richer plan-tracker than Replit's flat checklist** (typed steps, 4-state glyphs incl. a synthesized
  "stalled" state Replit's 2-glyph scheme can't express, `PlanPanel.tsx:15-27`, `buildTrace.ts:286-309`), plus
  a self-auditing lag nudge Replit lacks (`engine.py:568-633`). The cheap adds from Replit's `report_progress`:
  a presentational `done/total` counter + thin bar in the PlanPanel header (data already in scope,
  `PlanPanel.tsx:36,98`, ~6 lines), a FINISHED→"Done — what's next?" turn-hand-back in the footer signal (not a
  checklist decoration — it's the turn-taking signal, reuse the ask_user surface), and tighten the prompt from
  "3-7 steps" to "3-5 capstones" (`prompts.py:125`) — but keep the renderer UNCAPPED so a 6-step plan still
  shows all 6 (capping would hide our honest stalled-state design).

---

## 5. Revised top-3 priorities

1. **Enforce egress + add hard-deny (§2.3).** This is the only finding that fixes a *false guarantee*:
   `tool-sandbox-contract.md:36,87` promises deny-by-default per-task egress, but `egress_allowed()` is dead
   code and any previewable/NETWORK box gets full bridge networking — a real exfil surface on shared-kernel
   tiers. Ship the egress-interception sidecar (wire `egress_allow` → proxy ACL, force `HTTP(S)_PROXY` in the
   guest env at `gvisor.py:159`, route novel domains to the existing `WAITING_FOR_CONFIRMATION` ask) and the
   `DENY` verdict tier before `should_confirm` (`engine.py:1151`). Security correctness outranks any UX win,
   and the regex floor + isolation tiers we already have make this an incremental wire-up, not a rebuild.

2. **The S1+S2 compaction layers + cache de-mutation (§3.1, §3.6 step 2).** These are the highest leverage-to-
   effort items and they compound. S1 (dynamic budget from `context_window`) + S2 (Snip at ingestion) are
   pure-arithmetic / deterministic, no model call, a few hours each, and reclaim most of the context win while
   making the lossy summarizer the last resort. Doing GAP H step 2 FIRST (single stable system prompt + stop
   mutating the tools array) is a pure-local correctness improvement that shrinks and stabilizes the prefix
   even before any cache markers exist — and is the prerequisite that makes the rest of GAP H worth anything
   (cross-references roadmap item 12). Together these directly attack "how context survives," the thing the
   backend doc says we are most wrong about.

3. **notify/ask partition + affirmative finish + harness circuit breaker (§3.2, §3.7).** One coherent
   turn-taking overhaul: add `notify_user`, flip `agent.py:105-108` to `finished=False` for prose, require an
   explicit `finish` tool, and hard-code the 3-strike distinct-failure breaker that auto-fires the `ask_user`
   gate we already built. This removes the GAP B "tool-less prose = done" land-mine, ends the grind-to-500
   failure mode with a real human handoff, and lights up the `AWAITING_USER_DECISION` UI (UI 2.3) on actual
   failures — finally making the blocking gate fire on its own instead of only when the model volunteers.
   Visual self-verification (§2.4) and the verify-predicate gate (§4) are the natural fourth, layering output
   correctness on top of this turn-taking spine once it lands.
