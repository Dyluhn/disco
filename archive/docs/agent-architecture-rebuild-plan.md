# disco Agent Architecture — Research-Grounded Rebuild Plan

Supersedes `manus-aligned-loop-fixes-plan.md` (that draft contained invented mechanisms;
this one is graded against primary sources + controlled studies + live tests on our own
Qwen3.6-27B stack). Research: 3 rounds, 11 subagents, primary sources only, + empirical
hardware probes. Full source list at the end.

## Evidence grading (used on every item below — honesty up front)
- **[C] Controlled** — isolated A/B in a published study.
- **[P] Production-proven** — shipped by Manus/Anthropic/Cognition; effective in practice, not isolated in an ablation.
- **[E] Engineering-intuition** — sound, widely adopted, but no controlled isolation exists.
- **[H] Hardware-confirmed** — verified by live test against our 192.168.1.231:18080 Qwen3.6-27B server.

A blunt truth from Round 3 that governs the whole plan: **the controlled evidence does
NOT uniformly support "compaction = better."** LOCA-bench (arXiv 2602.07962) found
LLM-summarization compaction *neutral-to-negative* and memory tools *model-dependent (they
regressed weaker models)*. The two techniques with real controlled wins are (1) **keeping
large tool outputs OUT of context by design** (programmatic tool calling: +10–13pp across
models) and (2) **observation masking / restorable references** (arXiv 2508.21433: matches
or beats summarization at ~half cost, in exactly our file-re-reading regime). We build on
those two; we treat recitation/verification-gates as [P]/[E], not as proven wins.

---

## 0. Live-hardware feasibility findings (these constrain the design)
Tested against the actual Qwen3.6-27B-UD-Q5_K_XL on llama.cpp build `b165-fa8c332`:
- `tool_choice:"auto"` → **works** [H]: model emits a clean `tool_calls` array.
- `tool_choice:"required"` → **NOT enforced** [H]: model produced prose and ignored the tool. `chat_format` is `Content-only` (tool calls parsed heuristically from text, not grammar-constrained).
- `grammar` and `response_format:{type:json_schema}` on `/v1/chat/completions` → **returned empty content** [H]: not usable as-is on this build/endpoint.
- **assistant-message prefill → works** [H]: a partial `assistant` message is continued. `chat_template_caps.supports_preserve_reasoning=true`.

**Consequence:** Manus's "mask, don't remove" Specified/Required modes are achievable here
**only via assistant prefill** (the literal Manus mechanism), not via `tool_choice`/grammar.
Any masking work is gated on a small server-side decision (prefill, or re-launch with a
grammar-constrained tool template) — flagged at each relevant item.

---

## 1. What disco ALREADY gets right (keep — do not touch)
Our event-sourced design is, by luck and good taste, already aligned with several proven principles:
- **Append-only event log + View as a pure function of events** = 12-Factor #12 "stateless reducer" [P] and the substrate KV-cache stability needs (append-only context). This is a genuine asset.
- **Plan-first AgentLoop** (PLANNING → submit_plan → approval → execution) = the plan/act/verify loop [P].
- **Condensation tombstones / microcompact** = the right *shape* for compaction (don't mutate history; project over it) — matches Claude Code's "context collapse = read-time projection, underlying data not deleted" [P].
- **`c430275` plan-step scoping** and **`56526be` snapshot-on-terminal** from this session — keep (correctness + durability, not bandaids).

---

## 2. Part A — Roll back the bandaids (read-counter family)
**Evidence:** read-count limits are **not endorsed** anywhere as a quality technique — only as a blunt fail-safe; and the literature shows blunt context-surgery can *induce* the loop it's meant to stop (summary-pruning elongated trajectories, arXiv 2508.21433 §4.4) [C]. Penalizing reads makes the model *less* grounded — the opposite of the goal.

Remove (surgical, in `engine.py` — keepers `c430275`/`56526be` are interleaved, so no `git revert`):
`_READ_STREAK_LIMIT`, `_HARD_READ_NUDGE_LIMIT`, all four `_READ_NUDGE*` strings,
`_trailing_read_only_streak()`, `_read_nudges_since_user()`, `self._force_commit` (+ both
`_tools_for_step` branches), the read-streak firing block, `read_loop_break`. Keep
`_STUCK_ESCAPE_TEMP`/`in_escape` (genuine stuck-escape, unrelated to reads). Drop the 4
force-commit tests in `test_planner_gate.py`. **Sequencing:** remove only AFTER §3.B1/B2
land, so the cause is fixed before the symptom-guard is pulled. Runaway safety remains via
`max_iterations`, StuckDetector, and the circuit-breaker.

---

## 3. Part B — The proven architecture (build these)

### B1 — Observation masking + restorable references  [C — strongest controlled backing]
Replace the destructive head+tail snip at `events._OBS_SNIP_CHARS=8000` with:
- A **rolling window**: keep the last M turns' observations verbatim; for older observations, drop the body but **keep a restorable reference** (the file path + line range + a content hash / "unchanged since turn N"), not an opaque stub. Keep **all reasoning and actions** verbatim.
- Tune M empirically on our scaffold (paper used M≈10 for SWE-agent; OpenHands needed M≈58 — it's scaffold-dependent, so this is a tuning param, not a constant).
- This *is* the principled version of the "make re-reads cheap" idea — but it is **observation masking with restorable pointers**, NOT the "you already read this" read-dedup I floated earlier (which is unendorsed and stale-unsafe). If we add read-dedup at all, it must be **stateful**: invalidated by any write/move to the path (mtime/hash), returning a restorable reference, never a bare "see above."
Evidence: arXiv 2508.21433 (masking matches/beats summarization at half cost, in the observation-heavy regime we live in). Our event-sourced View is the perfect place to implement this as a render-time projection.

### B2 — Programmatic tool calling: keep big outputs OUT of context  [C — largest controlled win]
The biggest controlled win in LOCA-bench (+10–13pp across models) was *avoiding* large
intermediate tool outputs in context, via code that orchestrates tools and returns only what's
needed. For us: the CodeAct path (B8) should be the default for multi-step data work, with
results written to files and only summaries/paths returned to context. This also aligns with
Anthropic's "Code execution with MCP." Net: prefer "run code, write to file, return a path"
over "dump the tool output into the transcript."

### B3 — Structured variation in the tail  [P]
Inject small, deterministic-by-seq variation into the **surface form of newly appended**
observation/action records (rotate header phrasings, metadata ordering) to break few-shot
mimicry — the actual cause of the read-rut. **Tail only**: never vary the stable
system/tool-def prefix (that would break KV-cache, §B0). Resolves the "determinism paradox":
deterministic *prefix* (cache + replay-safety) + varied *tail* (anti-mimicry) are
complementary, applied to different regions. Evidence: Manus [P]; evidence uniformly favors
variation (not determinism) for anti-mimicry.

### B4 — Keep the wrong stuff in  [P]
Audit the View/condenser to ensure failed actions + error observations are **retained**
(at least in the recent window) rather than scrubbed — the model needs to see failures to
stop repeating them, and summary-smoothing of failures *induces* loops [C, arXiv 2508.21433].
Tool errors should be **actionable feedback**, not raw tracebacks (Anthropic tools guidance [P]).

### B5 — KV-cache discipline  [P — Manus's #1 metric]
Make the prompt prefix byte-stable and serialization deterministic:
- No volatile data (timestamps, random ordering) in the system-prompt/tool-def prefix.
- Deterministic JSON key ordering everywhere the View serializes.
- Verify our provider sends a stable prefix and that prompt/prefix caching is on for the llama.cpp server (session-consistent routing). Manus: cached vs uncached is a 10× cost gap.
This is mostly an *audit + tighten* of our existing append-only View, not new architecture.

### B6 — Recitation / goal-reinjection, on-demand  [P / E — NOT controlled]
Keep `_recitation_message` (already correctly scoped). Make it **on-demand** (inject plan
status on drift / cadence, not every step) — Manus found constant rewriting wasted ~⅓ of
actions. **Honesty:** no controlled study isolates recitation's benefit; treat as
production-intuition, not a proven win. Cheap to keep, so keep it lightweight.

### B7 — Verification-gated completion via a fresh-context evaluator  [P / E — strong rationale, not controlled]
Upgrade our `verify_app` into the Anthropic long-running-harness pattern:
- Definition-of-done = an **external spec the agent cannot edit** (e.g. a `test-results.json`
  of features, all default-FAIL), enforced by a hook that denies writing the results file
  unless evidence (screenshot/log) was read first.
- **A separate evaluator agent with no write tools** grades the diff + screenshots from a
  context that never saw the build → PASS / NEEDS_WORK. The builder cannot grade itself.
- Scope each iteration to **one feature**.
Evidence: Anthropic `cwc-long-running-agents` repo [P]; rationale (self-grading + contaminated
context → false "done") is well-documented; no isolated controlled number exists.

### B8 — Persistent IPython kernel for CodeAct (delete the pickle runner)  [P — full blueprint in hand]
Replace `_CODEACT_RUNNER_SRC` (which re-pickles the whole namespace every cell → the 41KB→346KB
quadratic hang we observed) with a **persistent Jupyter kernel per conversation**. Proven design
(AutoGen v0.2 + CodeAct both converge on it):
- Run a **Kernel Gateway inside the sandbox**; host talks HTTP (lifecycle) + one multiplexed
  **WebSocket** (messaging). State lives in the live kernel process — **zero serialization**.
- Add a `KernelSession` to the `SandboxInstance` protocol (`base.py`); implement in `process.py`
  (local subprocess gateway) and `gvisor.py` (in-container gateway). `system.py::CodeExecTool`
  routes python cells to it.
- Lifecycle: `POST /api/kernels` → WS connect + `kernel_info` handshake (don't trust HTTP-201
  as ready) → `execute_request` → collect stream/result/error, finish on **both** iopub
  `status:idle` AND shell `execute_reply` (two-flag rule) → timeout: **interrupt (SIGINT, state
  survives)** → escalate to restart/kill → restart wipes memory, rehydrate by re-running a
  bootstrap cell against workspace files.
- Resource ceiling = **sandbox cgroup** (`memory.max` + CPU quota), not the kernel. Signal the
  **process group** (subprocesses). Idle-cull kernels (CodeAct: 10-min idle, 60s sweep).
- This also fixes the `proc.kill()`-grandchild-pipe deadlock in the current `exec_shell` timeout
  path. (Full pseudocode in research notes / the persistent-kernel agent digest.)
Bonus: pairs with B2 (programmatic tool calling) — the kernel is where "run code, write file,
return path" lives.

### B9 — Action-space masking via assistant prefill  [P mechanism / H feasibility]
The legitimate replacement for force-commit, driven by **real lifecycle state, never read
counts**. Mechanism = Manus's prefill (the one confirmed working on our stack [H]):
- Keep ALL tool definitions in context always (KV-cache, §B5); constrain *selection* by
  prefilling the assistant turn: Auto = no prefill; Required = prefill up to the tool-call
  token; Specified-group = prefill up to `{"name":"<prefix>` (needs consistent tool-name
  prefixes — adopt `browser_`/`file_`/`shell_` naming).
- **Feasibility caveat [H]:** `tool_choice:required`, `grammar`, and `json_schema` do NOT work
  on our server as-configured; **prefill does**. So implement masking via prefill, OR (better
  long-term) re-launch llama.cpp with a grammar-constrained tool template (`--jinja` + a
  Hermes/Qwen tool template that sets `chat_format` to a tool-grammar mode) and revisit
  `tool_choice`. Validate the prefill-through-`--jinja`-template seam before relying on it.
- **Use sparingly** — only for genuine lifecycle states (e.g. "user just spoke → reply, don't
  act"), not as a loop-guard. This is the lifecycle-correctness upgrade, not the loop fix
  (the loop is fixed by B1/B3/B4).

---

## 4. Single-agent vs multi-agent (our two surfaces)  [C/P — resolved]
Decision rule (Anthropic, Cognition, OpenAI all reduce to one variable — *shareable context /
write-independence*):
- **Deep Research → orchestrator-worker fan-out.** Read-only, parallelizable, distilled
  returns. Anthropic measured **+90.2%** over single-agent on research, at **~15× chat token
  cost** [C], value-gated. Our deep_research already decomposes into sub-questions — this is the
  right shape; consider isolating each sub-question's exploration in its own context and
  returning only distilled findings. Lance Martin's **ISOLATE** quadrant.
- **Build → single-threaded linear agent.** Interdependent writes; parallel agents produce
  irreconcilable diffs (Cognition's exact failure case; Anthropic concedes "domains that
  require all agents to share context… are not a good fit"). Keep Build single-threaded; use
  read-only sub-agents only for *exploration/review* excursions that return summaries.
- Organizing frame for all context work: **WRITE / SELECT / COMPRESS / ISOLATE** (Lance Martin).

---

## 5. Honest caveats (do not let these get lost)
- **Memory tools can HURT a weaker model.** LOCA-bench: memory tools regressed DeepSeek while
  helping GPT-5.2/Gemini [C]. We run a 27B — any memory-tool/“agentic memory” addition must be
  A/B'd on *our* model, not assumed beneficial.
- **Compaction-by-summarization is neutral-to-negative** in the one controlled benchmark
  [C, LOCA-bench]. Prefer B1 (masking/restorable refs) and B2 (keep outputs out) over
  summarizing. Our condenser should lean on projection + references, not LLM summaries, where
  possible.
- **Observation-masking superiority is scaffold-dependent** [C]. We're SWE-agent-like
  (observation-heavy), so it should hold — but tune M and validate on our harness before betting.
- **Recitation & verification-gating are production-intuition, not controlled wins.** Ship them
  (cheap, sensible) but don't claim proof.
- **Masking via tool_choice/grammar is dead on our stack [H]** — prefill or server reconfig only.

---

## 6. Sequencing
1. **B5 (KV-cache audit) + B1 (masking/restorable refs) + B3 (variation) + B4 (keep-errors-in)** — the context substrate. Fixes the read-rut at the cause.
2. **Verify** on the real-sample EE-Quest harness that the rut is gone (the scenario that spawned the bandaid).
3. **Part A rollback** (remove read-counter) — now safe.
4. **B8 (persistent kernel)** — independent track; kills the dill-bloat hang + the timeout deadlock.
5. **B2 (programmatic tool calling default)** — leans on B8.
6. **B7 (fresh-context evaluator)** — verification gate.
7. **B9 (prefill masking)** — last; lifecycle-correctness, behind the prefill/server-config seam.
8. **Deep Research fan-out (§4)** — separate workstream.

## 7. Verification (project rules)
Real-sample harness from VERBATIM captures, injected via the common endpoint, verified in the
live UI with Playwright/Firefox before/after screenshots (green test counts are NOT evidence).
Specific gates: read-rut gone (B1–B4); 50+-stateful-cell conversation shows FLAT per-cell
latency (B8); masking solve-rate ≥ summary on our harness (B1); fresh-context evaluator catches
a deliberately-broken feature (B7). All `core`/`agent-server` tests green; new tests per item.

---

## Primary sources
- Manus context engineering: https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
- Anthropic: effective-context-engineering-for-ai-agents · writing-tools-for-agents · effective-harnesses-for-long-running-agents · building-effective-agents · built-multi-agent-research-system · code-execution-with-mcp · building-agents-with-the-claude-agent-sdk · `github.com/anthropics/cwc-long-running-agents`
- Cognition "Don't Build Multi-Agents": https://cognition.ai/blog/dont-build-multi-agents
- Lance Martin context-engineering taxonomy: https://rlancemartin.github.io/2025/06/23/context_engineering/
- 12-Factor Agents: https://github.com/humanlayer/12-factor-agents
- Observation masking [C]: https://arxiv.org/abs/2508.21433
- LOCA-bench [C]: https://arxiv.org/abs/2602.07962
- Agentic Harness Engineering [C]: https://arxiv.org/abs/2604.25850
- Lost in the Middle: https://arxiv.org/abs/2307.03172 · Context Rot: https://research.trychroma.com/context-rot
- MemGPT: https://arxiv.org/abs/2310.08560 · Reflexion: https://arxiv.org/abs/2303.11366
- CodeAct: https://arxiv.org/abs/2402.01030 · impl github.com/xingyaoww/code-act
- E2B (Manus sandbox): https://e2b.dev/blog/how-manus-uses-e2b-to-provide-agents-with-virtual-computers
- jupyter_client / AutoGen JupyterCodeExecutor / kernel_gateway (kernel blueprint sources in research notes)
- llama.cpp server + GBNF + function-calling docs; live probes against 192.168.1.231:18080 (build b165-fa8c332)
