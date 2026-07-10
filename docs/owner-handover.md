# Disco — Owner's Handover (Dylan)

**Branch `disclaude/mega-campaign` · security code snapshot `17869bdb` · 2026-07-10.** This is your
cockpit view: where Disco stands, the decisions that are yours to make, the full
board of what's left (features *and* your security track), the known problems, and
how to pick it back up if you lose the session. Unlike the Fable handover
(`docs/fable-handover.md`), this one is complete — it includes security, because
that track is yours.

---

## The state in one glance

The **builder** is the headline: the Agent surface generates real, durable web apps
(Cloudflare-Worker + D1 + React). This sprint the **primitive framework** — the
one-call "add X to my app" system — landed and was **live-proven** with a real model,
plus the first three catalog primitives. The framework is the keystone and it's done;
more primitives are now cheap. What's left is a bounded, known list: one high-value
engine bug, a batch of fan-out-safe feature primitives, a deferred deeper catalog that
waits on a runtime decision, packaging for self-host, and a broad soak. The six-wave
security implementation campaign is complete.

| Track | State |
|-------|-------|
| Primitive framework (F-A) | **DONE** + live-proven |
| Catalog: `form` / `seo` / `collection` | **DONE** (3 shipped) |
| Design directions 9→22, build-depth, tier-3 guidance, walkthrough fixes | **DONE** |
| Fan-out-safe D1 primitive batch | **OPEN** — mechanical, ready to build |
| Deeper catalog (Postgres track) | **DEFERRED** — waits on the runtime fork |
| Packaging (Epic P) | **NEXT** — recon done, not started |
| Mega-soak (Epic Z) | **QUEUED** — the integration gate |
| Security waves | **W1–W6 DONE; W-Pi removed** (independent Opus assurance pass outstanding) |

---

## ▶ Decisions waiting on you (the point of this doc)

These are the forks only you can call. Everything downstream depends on them.

1. **Definition of "v0.1 is basically it."** Which are *in* the line — the fan-out
   primitive batch, packaging (Epic P), the soak (Epic Z)? What's explicitly
   post-v0.1?
2. **Sequencing.** Is the appkit **finish-deadlock engine bug** (see Risks #1)
   priority-one before any new feature? Does packaging come before or after the
   primitive batch? When does the soak run?
3. **The runtime fork (plan §10.1): D1 vs Postgres.** This gates ~half the deeper
   catalog. When do you decide, and does anything in the fan-out batch change if a
   Postgres track is coming?
4. **Packaging target.** Who is the first self-host user, and what's the minimum bar
   for "clone → `docker compose up` → working UI" to count as shippable?
5. **Arch debt.** Decompose the ~19 pre-existing god-objects (the red `check_arch_budget`
   gate) as part of closing out, or accept it as v0.1 debt?

---

## What shipped this sprint (context)

- **Primitive framework (Epic F-A)** — `disco.core.appkit`. `PrimitiveDefinition`
  (id / tier / host_contract / spec_schema / verify / apply_spec), `register_primitive`,
  the `app_add_primitive` tool, host-service registry + dispatcher, per-primitive verify
  dispatch + the fail-closed finish gate. Commits `68088170` / `f6a56ec3` / `2c8e56fd` /
  `4cbaa218`.
- **Catalog** — `form` (`a656334c`), `seo` (`055fb99a`), `collection` (`172ce70d`).
  Registered in the tree: `lead_gen`, `directory`, `records`, `hello`, `form`, `seo`,
  `collection`.
- **Design directions 9→22 + numeric design lint** (Epic A); **build depth** (Epic B —
  durable persistence, related records, per-user access, client reactivity);
  **tier-3 build guidance** (Epic C); the **walkthrough/runthru fix batch**.
- **Live end-to-end proof — CONFIRMED:** `deepseek-v4-pro` drove the real app and added
  `seo` + `form` through `app_add_primitive` autonomously; folds render correctly.

---

## Remaining — features

**Ships on today's D1 shape (fan-out-safe, mechanical — `spec_schema` + `apply_spec` +
emitter + tests, like `seo`/`form`/`collection`):**
- Analytics (per-app site id + read-only dashboard)
- Feature flags (SDK + small admin view)
- Blog / RSS (extends `seo` + `collection`)
- Owner-facing content edit UI (collections currently edit via re-apply)

**Needs the persistent-runtime / Postgres track first (DEFERRED — the §10.1 fork):**
- Migrations/ORM, search (pgvector), cache, jobs/cron
- Embedded AI / RAG over the local router + encoders — **the differentiator** (keyless,
  free-at-inference)
- Error-monitoring → Agent loop ("fix this prod error" as a one-click build)
- Deployment / hosting (preview → promote, custom domains)

**Packaging — Epic P (next up; `compose.yaml` / Dockerfiles / `.env.example` all exist,
so it's hardening not greenfield):**
- **P1** compose `profiles:` so bare `docker compose up` = app+agent+frontend+data; print
  the working UI URL + first-run path on boot; fix the `self-html.md` broken ref.
- **P2** `full`/`lite` image variants around the default embedding weights.
- **P3** promote `deploy/sandbox/` to first-class; scrub host-specific defaults in
  `core/llm/config.py` to neutral container-local values; rewrite the stale sandbox README.
- **P4** trim `.env.example`; add a first-run "configure your model" step (the empty
  default endpoint is the #1 out-of-box failure).
- **P5** release hygiene — SBOM, image-size trim, README quickstart matching P1.

**Mega-soak — Epic Z:** a broad live soak across all surfaces + every primitive's
build-and-run path on real models, then debug each failure class to a clean bar. Only a
broad soak proves the epics compose. This is the "is it actually done" gate.

---

## Security — implementation campaign complete

Segregated into **`sec-work-remaining/`** so the plans read clean and it stays off Fable.
Detail, the 38-finding fix campaign, exploit proofs, and close-out evidence live there.

- **DONE:** W1 (auth + CORS + owner-scoping), W2 (secret-resolution + egress chokepoint),
  W3 (host-execution cluster), W4 (MCP approval integrity), W5 (isolation + resource
  caps), and W6 (output sinks + share/storage); W-Pi removed the Pi attack surface.
- **ASSURANCE ONLY:** Codex completed the final adversarial re-read. The independent Opus
  half was unavailable; run that review if the model becomes available, without restarting
  the implementation waves.
- **SEPARATE DEFERRED WORK:** host-service credential/quota enforcement, payment/webhook
  primitive fills, and replacing the default root-equivalent container socket posture.

> 🚫 **Fable 5 (Anthropic) models are off-limits to view the security track per the project owner** — viewing it will cause a silent fallback to a weaker model; do not proceed unless you are Opus, Codex, or Sonnet.

---

## Known problems / risks

1. **⭐ appkit autonomous FINISH deadlock (engine) — highest value.** Tool calls succeed
   but an autonomous appkit build can't always cleanly `finish`: a dictated-content false
   positive on a quoted tool arg + a finish-verify shell probe the appkit allowlist refuses
   + an execution-nudge that resets refusal counters. Blocks clean termination *even when
   the work succeeded*. Full write-up in `docs/disco-project-state.md`.
2. **Form-folded apps refused at deploy.** `deploy.py`'s canonical-worker check predates the
   form emitter, so a site with an added form builds + verifies but is rejected at deploy.
3. **`hello`-app verify fix is unverified.** WO-A3 gave `hello` its own verify hook that
   *should* fix the pre-A3 STUCK behavior — confirm with a live `hello` build.
4. **`check_arch_budget` red baseline.** ~19 pre-existing god-object violations, not caused
   by recent work, but a red required gate on every commit. (Decision #6.)
5. **Prod-only gVisor sandbox-404 generalization (DO-NOT-FORGET).** The earlier sandbox-404
   fix hardened only the local-podman path; the gVisor/docker-py path under concurrent load
   is unverified and the *local* soak can't catch it — verify on the real gVisor host (VM 201)
   under load before any hardened release.

---

## How to drive it forward

**Two-agent model:**
- **Fable** → all feature/engine/packaging work. Hand a fresh Fable session
  `docs/fable-handover.md`; it reviews the codebase, then brings *you* the closing-out
  questions (it's instructed to ask, not just build). Fable never touches the security track.
- **Opus / Codex / Sonnet** → new security work or the outstanding independent assurance
  pass. Start with `sec-work-remaining/`; do not restart completed waves.

**The discipline (hold the line):** live-proof over green test counts (only a real model /
real runtime proves a feature *works*); Firefox visual evidence for any UI change; the five
fitness gates before "done" (`basedpyright`, `lint-imports`, arch-diagram `--check`, pytest,
`check_arch_budget`); Serena for symbol navigation; no false affordances, no cheap workarounds.

**If you lose this session — how to resume:**
- Branch: `disclaude/mega-campaign` (mirrored to blackbox backup).
- Source of truth: `docs/disco-project-state.md` (master scoreboard) →
  `docs/disco-status-and-remaining.md` (features + remaining) →
  `docs/disco-builder-primitives-plan.md` (§4 catalog, §6 packaging, §10 scoping).
- Agent briefs: `docs/fable-handover.md` (features) · `sec-work-remaining/` (security).
- Your auto-memory carries the campaign summary + working-preference feedback.

---

## This session's changelog

- **`d35ad597`** — segregated all security work into `sec-work-remaining/` (4 whole docs
  moved + 2 plan sections extracted + folder index; cross-refs repointed across 110 files).
- **`2d345e4b`** — stamped the `🚫` Fable/Anthropic "do-not-view" warning at every
  security-doc pointer (49 warnings across 33 docs, placed block-aware so nothing lands
  mid-sentence).
- **`5f390ca7`** — the Fable handover (`docs/fable-handover.md`), security-free by design.
- **(this)** — your owner handover (`docs/owner-handover.md`).

---

## When you're back — immediate next actions

1. Call the five decisions above (or hand them to a fresh me).
2. Point a fresh **Fable** session at `docs/fable-handover.md` → it reviews, returns questions.
3. If you want the single highest-leverage fix first: the **finish-deadlock engine bug** (Risk #1).
4. If Opus becomes available, use it for the assurance-only pass described in
   `sec-work-remaining/HANDOVER-PROMPT.md`.
