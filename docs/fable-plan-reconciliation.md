# Fable planning corpus — reconciliation vs current code (2026-06-13)

Each Fable planning doc reconciled item-by-item against the LIVE codebase (not the
doc's own status text). DONE / PARTIAL / OPEN with file:line evidence. Produced by
parallel review agents + verified. Accounts for this session's shipped work
(file-state snapshot, A autonomous, B verify-cap, C plan_step-cap, D edit-guards,
Fix B/C live model probe) and post-handoff work (StuckDetector, LLMSummarizingCondenser
with window-fraction + recitation + pinned plan/knowledge/datasource, finish tool,
file_read offset/limit, microcompact).

Validation status of this session's fixes: **Gemma E4B 8/8 no-clobber; Qwen 27B 5×5
all PASS; 120b clean sweeps.** Edit guards test-locked (`test_edit_guards.py`).
Autonomous off-is-off proven (`test_autonomous_mode.py`).

---

## doc: manus-gap-analysis.md (GAPS A–H + 12-item roadmap)

**Mostly DONE since handoff + this session.**
- DONE: GAP B (finish tool / prose≠done, `agent.py:204-227`), GAP D (plan+knowledge+datasource
  pinned + recitation tail, `view.py:127-222`), GAP F (`<error_handling>` ladder + temp jitter,
  `prompts.py:215-222`, `agent.py:68-70`), GAP H (KV: sort_keys + prompt_cache_key + Anthropic
  cache_control, `openai_provider.py:173,235-267`). Roadmap 1,2,3,4,6,10,12 DONE.
- PARTIAL: GAP A (condenser caps at 24k/32k working budget despite window-fraction — `view.py:494-499`;
  keep_recent=6 counts EVENTS not tool-turns), GAP C (file_read offset/limit + workspace snapshot
  DONE; **auto-spill of large obs to disk NOT done**), GAP E (serve marker exists, **deploy_preview
  absent** `builtin/__init__.py:64`), GAP G (Knowledge/Datasource events exist+pinned, but
  **DatasourceEvent has no emission path**; skills still in system prompt). Roadmap 7,8,9,11 PARTIAL.
- OPEN: Roadmap 5 (deploy_preview + serve/process tool).

**Top OPEN:** deploy_preview (HIGH, ~1h), auto-spill large obs to disk (HIGH), keep_recent in
tool-turns not events (MED), DatasourceEvent emission path (MED), reversible-compaction tier (MED).

---

## doc: manus-gap-analysis-addendum.md (§2 new findings, §3 blueprints)

- DONE: 2.1 `<file_rules>` prompt block (`prompts.py:223-240`) + `file_append` tool; 2.2 notify/ask
  partition + affirmative finish (`engine.py:502-517`, `agent.py:204-227`); 2.3A DENY tier
  (`analyzers.py:87-111`); 2.3B egress proxy wired (`gvisor.py:186-258`) **but tagged HARDWARE-UNVERIFIED**;
  2.6 temperature jitter; 3.1 S1–S4 compaction cascade (dynamic budget, snip, microcompact, structured
  summarizer); 3.6 sort_keys + cache_control + cached_tokens read; 3.7 circuit-breaker escalation.
- PARTIAL: 2.4 visual self-verify (`verify=static/app` HTTP checks exist; **no headless-browser/screenshot
  gate**); 3.1-S5 hard_reset is a normal summarize, **not a filesystem-pointer-only flush**; 3.3 0.0.0.0
  bind mentioned but no `<deploy_rules>` block; 3.4B `remember`→KnowledgeEvent done **but no `.pmx/MEMORY.md`
  persistence** (dies on hard reset).
- OPEN: shell-tool + file_write descriptions not de-steered (one-liners); 2.4 `verify_app` standalone tool;
  2.5 image generation + binary-safe write; 2.6/3.5 **serialization jitter + nudge-pool variants**;
  3.3 detached `serve`/`start_server` tool + pre-expose self-test; **3.6-2 mode-boundary KV-cache break
  ("do this FIRST")**; 3.6-1 rolling transcript cache markers (#3/#4); 3.6-4 cache-write cost tracking;
  §4 plan-step verify predicates.

**Top OPEN:** 3.6-2 mode-boundary cache break (P1, pure-local correctness), egress HARDWARE-UNVERIFIED
live test (P1 security), 3.1-S5 pointer-only flush (P2), MEMORY.md persistence (P3), serialization jitter (P4).

---

## doc: agent-architecture-rebuild-plan.md (B1–B9 + DR isolate)

- DONE: §1 keep-architecture; Part A roll-back of read-counter bandaids (all 6 symbols gone); this
  session's snapshot+A+B+C+D+FixB/C; B1 observation masking (`view.py:284-338`); B3 structured tail
  variation (`view.py:353-373`); B4 keep-errors-in (`view.py:320`); B5 KV discipline (`openai_provider.py`,
  `test_view_kv_stability.py`); B8 persistent IPython kernel (`kernel.py`, minor: no idle-cull, RLIMIT_AS
  not cgroup).
- PARTIAL: B2 (kernel spills >2k to `.outputs/`, broader CodeAct-default not enforced); B6 recitation
  **fires EVERY step** (plan says on-drift/cadence — Manus: constant rewrite wastes ~⅓ actions);
  B9 prefill masking (infra done, only PLANNING prefill behind `PMX_PLAN_PREFILL=1` env, default OFF);
  §4 DR fan-out (concurrent gather, but shared router — not true isolated sub-contexts).
- OPEN: **B7 — verification-gated completion via FRESH-CONTEXT evaluator (zero code today, highest
  leverage)** — an external DoD spec the agent can't edit + a separate read-only evaluator agent at
  finish. This is the structural fix for "self-grades in a contaminated context → false done" (exactly
  the 120b verify-thrash observed).

**Top OPEN:** B7 fresh-context evaluator (P1), B6 recitation-on-cadence (P2 cheap), B9 full lifecycle
masking (P3), DR isolated sub-contexts (P4), B8 idle-kernel cull (P5).

---

## Consolidated highest-leverage OPEN items (across the 3 docs)
1. **B7 fresh-context evaluator** — the root fix for false-"done"/verify-thrash. Zero code today.
2. **3.6-2 mode-boundary KV-cache break** — single stable system prompt; pure-local correctness win.
3. **deploy_preview / detached serve tool** (GAP E / 3.3) — agent can build a site but can't show it.
4. **Auto-spill large obs + MEMORY.md persistence** (GAP C / 3.4B / S5) — filesystem-as-memory residue.
5. **Recitation on cadence not every step** (B6) — wastes ~⅓ of actions.
6. **Serialization jitter + nudge-pool** (2.6/3.5) — anti-self-imitation (temperature half done).
7. **Egress allowlist live VM test** (2.3B) — close the HARDWARE-UNVERIFIED gap.

(next-fix-set RP plan, release-execution, manus-ui-gap, decomplexity-wave reconciliations: in progress.)
