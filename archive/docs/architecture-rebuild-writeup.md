# The Agent-Architecture Rebuild — Why, the Research, and the Plan

Companion narrative to `agent-architecture-rebuild-plan.md` (the graded, sequenced plan).
This document explains how we got here, what we learned, and what the plan actually changes.

---

## 1. What prompted this

During the AFK build marathon (2026-06-09), the local 27B model (Qwen3.6-27B, ≈Sonnet-4.6-
no-thinking) built a polished single-file site from scratch but **could not iterate on it** — it
read the file endlessly and never committed an edit. We fixed the genuine framework gaps
(line-targeted edits, paginated reads, a finish gate that requires a state-changing action), and
those worked. But we *also* added a family of "read-loop guards": after N reads with no edit,
nudge → escalate → and finally **withhold the read tools to force a commit** (a "force-commit"
mechanism).

You pushed back, correctly, on two grounds:
1. **"A read counter is a bad idea — why would you want the model to be less familiar with the
   status and codebase?"** Penalizing reads optimizes for the wrong thing. The model *should* be
   maximally grounded before it acts; capping reads makes its edits worse.
2. **"This doesn't look the same as what Manus is doing."** Each time I claimed a fix was
   "Manus-aligned" or "research-proven," it turned out to be a mechanism I'd invented and dressed
   up. You set the rule: **no cheap fixes; precisely what is proven and robust; rewrite the whole
   app if that's what it takes.**

That triggered exhaustive research before any more code.

## 2. The research (3 rounds, 11 subagents, primary sources only, + live hardware tests)

- **Round 1** — five parallel deep-dives: Manus's full architecture; frontier-lab agent guidance
  (Anthropic / Cognition / OpenAI / 12-Factor); code-execution state; constrained decoding / tool
  masking; long-horizon memory + anti-drift. Primary sources, verbatim mechanisms.
- **Round 2** — confirmation + thread-following: the observation-masking paper; single-vs-multi-agent
  reconciliation + the WRITE/SELECT/COMPRESS/ISOLATE taxonomy; a verified persistent-kernel
  implementation blueprint; Claude Code's context pipeline + harness ablations. Plus the first
  **live probe** of our own Qwen server.
- **Round 3** — the controlled-evidence honesty check (LOCA-bench, demand-paging, AHE) + the
  decisive **hardware feasibility tests** against `192.168.1.231:18080`.

I stopped at 3 (you authorized up to 10) because the literature **converged** — Round 2 confirmed
Round 1, Round 3 resolved the open paradoxes — and the only remaining unknowns are implementation
seams that surface in code, not papers. Running to 10 for a number would be the exact theater the
"no cheap" rule forbids; the cap is a ceiling, not a quota.

## 3. What the research overturned (where I had been wrong)

1. **"Read deduplication" is not a Manus technique and is unsafe.** No primary source endorses
   short-circuiting reads as a quality lever; it's stale-unsafe (a re-read after an edit must NOT
   return the cached value). The endorsed, **controlled-evidence** equivalent is **observation
   masking + restorable references** (arXiv 2508.21433: matches/beats LLM summarization at ~half
   cost, in exactly our file-heavy regime).
2. **The masking mechanism I proposed (`tool_choice`) is inert on our stack — proven by live test.**
   `tool_choice:"required"` does not force a call on our Qwen server (`chat_format: Content-only`);
   `grammar` and `json_schema` return empty. **Assistant prefill works** — which is *literally*
   Manus's mechanism. So masking is feasible here, but only via prefill (or a server relaunch with
   a grammar-constrained tool template).
3. **Compaction-by-summarization is controlled-NEGATIVE-to-neutral** (LOCA-bench). The controlled
   *wins* are (a) keeping large tool outputs OUT of context by design (programmatic tool calling,
   +10–13pp) and (b) observation masking. So we should not lean on LLM summaries.
4. **Memory tools can HURT a weaker model** (LOCA-bench regressed DeepSeek while helping the big
   models). We run a 27B — anything "agentic memory" must be A/B'd on our model, not assumed good.
5. **Recitation and verification-gates are production-intuition, NOT controlled wins.** Ship them
   (cheap, sensible), but don't claim proof.
6. **The stateful-CodeAct design is the wrong substrate.** It re-serializes the whole namespace
   every cell (O(N·K)); that's what made a 43-cell build *look* hung (it was thrashing on
   serialization). The proven fix is a persistent in-memory kernel — what Jupyter/E2B/AutoGen/CodeAct
   all do. State lives in the process; nothing is serialized.

## 4. The meta-lesson

Two of our deepest bugs — the pickle-every-cell runner and the read counter — are the **same mistake
in two places**: managing a hard problem with bookkeeping (serialize-and-reload; count-and-cap)
instead of removing it (keep the process alive; shape the context). Manus's whole playbook is
"fix it in the substrate": stable append-only context, files-as-memory, logit/prefill masking,
structured variation. The honest plan is therefore *lower in the stack and smaller in control-flow*
than my earlier patches — and it deletes more than it adds.

## 5. What the plan entails (`agent-architecture-rebuild-plan.md`)

Every item graded **[C]ontrolled / [P]roduction-proven / [E]ngineering-intuition / [H]ardware-confirmed.**

**Keep (already right):** event-sourced append-only log + View-as-pure-function (a real asset for
KV-cache + replay), plan-first loop, condensation-as-projection, plus this session's `c430275`
(plan-step scoping) and `56526be` (snapshot-on-terminal).

**Roll back (bandaids):** the entire read-counter family (streak limit, nudges, temperature jitter,
force-commit / withhold-read-tools) — surgically, after the cause is fixed; runaway safety stays via
`max_iterations` + StuckDetector + circuit-breaker.

**Build (the proven substrate):**
- **B1 — Observation masking + restorable references** [C]: drop old observation bodies outside a
  rolling window, keep all reasoning/actions, leave a re-readable path/hash pointer. Replaces the
  destructive 8000-char snip *and* the invented read-dedup.
- **B2 — Programmatic tool calling** [C]: keep big outputs out of context by design (run code →
  write file → return a path), the single largest controlled win.
- **B3 — Structured tail-variation** [P]: break few-shot mimicry at the source; tail only (never the
  cached prefix). Resolves the determinism paradox (deterministic prefix + varied tail).
- **B4 — Keep errors in** [P]: retain failed actions/errors as actionable feedback; summary-smoothing
  of failures provably induces loops.
- **B5 — KV-cache discipline** [P]: stable byte-prefix, deterministic serialization, prefix caching on.
- **B6 — Recitation on-demand** [P/E]: keep the plan-recitation, fire on drift not every step.
- **B7 — Verification-gated completion** [P/E]: external default-FAIL spec the agent can't edit +
  a fresh-context evaluator subagent (no write tools) that grades from a context that never saw the
  build; one feature at a time.
- **B8 — Persistent IPython kernel for CodeAct** [P]: delete the pickle runner; kernel lives in the
  sandbox (process mode: `jupyter_client` directly; gvisor: Kernel Gateway + HTTP/WS); interrupt-
  before-kill (also fixes the current timeout deadlock), file-rehydrate on restart, cgroup memory cap.
  **Highest-leverage single fix; independently testable (flat per-cell latency over 50+ cells).**
  Honest cost: adds `jupyter_client` + `ipykernel` (currently absent from the venv).
- **B9 — Action-space masking via assistant prefill** [P/H]: lifecycle-driven (never read counts),
  using the prefill mechanism confirmed working on our stack.

**Single vs multi-agent** [C/P]: Deep Research → orchestrator-worker fan-out (+90.2% on research,
~15× tokens, value-gated); Build → single-threaded (parallel writers produce irreconcilable diffs).

**Sequencing:** context substrate first (B1/B3/B4/B5) → verify the read-rut is gone on the real
EE-Quest harness → *then* pull the read-counter → B8 kernel (independent) → B2 → B7 → B9 → Deep
Research fan-out. Verification per project rules: real-sample harness, live-UI Playwright/Firefox
before/after screenshots, flat-latency kernel test, masking ≥ summary on our own harness.

## 6. Honest caveats carried into the plan
- Memory-tool benefit is model-dependent — A/B on the 27B.
- Compaction-by-summary is controlled-negative — prefer masking/programmatic.
- Observation-masking superiority is scaffold-dependent — tune the window, validate on our harness.
- Recitation/verification-gating are intuition, not proof — shipped, not oversold.
- Masking via tool_choice/grammar is dead on our stack — prefill or server reconfig only.

---

*Full graded plan + sources: `agent-architecture-rebuild-plan.md`. Work history that led here:
`project-history-since-deep-research-fix.md`.*
