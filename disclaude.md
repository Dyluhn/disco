# disclaude.md — Disco Build Artifact Runtime Campaign (spine)

**Status:** Authoritative campaign spine (append-only after baseline).
**Owner:** Claude Code, executing autonomously inside the isolated `disclaude` experimental repo.
**Reviewer gate:** Codex (`codex exec --sandbox read-only`), plan APPROVE required before each PR is executed.
**Operating mode:** Autonomous. No user questions. Safest/narrowest assumption, document it, proceed.
**Soak driver:** MiniMax-M3 via direct MiniMax API ONLY. No OpenRouter. No other model for soak/promotion.

This file is the immutable spine. The full authoritative campaign text was supplied by Dylan
(P0..P17, the global safety gates, the PR specs). Below is the self-contained operating contract +
the append-only ledger. Do not delete old entries; append superseding notes.

---

## Non-negotiables (carried verbatim in spirit)

- **Thesis:** Disco Build becomes a host-owned artifact runtime (context → contract → prompt pack →
  starter/AppKit/BrandKit → specialized mutation tools → preview/show tools → verification finalizer →
  export/handoff → product-harness proof). Chat = control plane. Project FS = memory. Artifact
  contract = working context. Verifier = separate. Event log = audit. ContextPack = current model view.
- **Sequencing (no P2+ promotes until P0+P0B+P1 green):**
  P0 Context Runtime · P0B Lifecycle/Safety · P1 Product Harness/Oracles · P2 Contract Runtime ·
  P3 WorkflowPromptPack · P4 Specialized Mutation Tools · P5 Preview/Show/Delivery · P6 Verification
  Finalizers · P7 Starter/Brand/UI Kits · P8 Semantic Direct Manipulation · P9 TweakSpec ·
  P10 Export/Handoff · P11 Resource Import/Provenance · P12 Content/Design Discipline · P13 Deck/Doc/
  Prototype/Media · P14 Agent Ergonomics Lab/WorldSim · P15 PiKernel Product Integration ·
  P16 AppKit Return-to-Mainline · P17 Final MiniMax-M3 Soak.
- **Global gates:** no false affordances; no finish without host truth (host-owned finalizer);
  no destructive elision (recoverable excerpts w/ path+range+hash); no manual preview ownership;
  no user-installable Pi packages; Pi never gets real provider keys (local gateway, ephemeral token);
  MiniMax-direct-only soak with zero-OpenRouter proof.
- **Per-PR workflow:** read protocol + PR + deps → write plan in this file → Codex read-only review →
  revise until APPROVE → implement (parallel subagents) → tests → fix → re-test → ledger update →
  commit+push → next PR. No user approval between PRs.
- **Codex verdicts:** APPROVE | REVISE | BLOCKED_CODEX_UNAVAILABLE. If BLOCKED, fixing the Codex review
  path becomes P0 infra; do not silently bypass the gate.

---

## Ledger

### Repository Isolation Bootstrap — 2026-06-28 02:55 UTC

- source_repo: /home/dylan/projects/Disco-Pi  (chosen over /home/dylan/projects/disco because it is the
  only checkout containing build_kernel/pi_kernel.py + the Pi-kernel substrate the campaign builds on)
- experimental_repo: /home/dylan/projects/disclaude
- branch: disclaude/experimental-20260628T025508Z
- origin_remote: /home/dylan/projects/disclaude.git (local bare; gh not installed → local-remote fallback)
- source_upstream_remote: source-upstream-origin → /home/dylan/projects/Disco-Pi
- backup-mirror neutralization: Disco-Pi's `homelab` (blackbox:git-backups/disco.git) did NOT transfer
  into the clone (clone inherits source as origin only); repo_guard additionally pattern-refuses
  *git-backups* / *Disco-Pi* origins.
- repo_guard: pass (basename==disclaude AND origin not a source/backup mirror)
- git hooks: pre-commit + pre-push both run repo_guard
- heartbeat_location: .claude/heartbeat.py inside experimental repo
- main_repo_write_policy: read-only after bootstrap (no commits/pushes/campaign edits to source)
- ASSUMPTION (documented): the 3 uncommitted fixes on Disco-Pi (prompts.py inline-first,
  turn_control.py recover-don't-kill, test_cluster2_turntaking.py) are NOT carried into disclaude
  (clone copies committed history only). They remain untouched on Disco-Pi. CXT-3/CXT-5 reimplement
  context/elision behavior natively, so this is non-blocking.
