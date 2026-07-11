# Handover to Fable — closing out Disco v0.1

**You are Fable.** This is your onboarding + working brief for finishing Disco.
Read it fully before touching anything. It tells you where the project stands,
what's still open or half-built, the *shape* of what's left, and how to work
here. At the end there's a task that's specifically yours: after you've reviewed,
come back to Dylan with the questions that will actually let us close this out.

Branch: `disclaude/mega-campaign`. This doc was written at HEAD `2d345e4b`
(2026-07-06). Verify the current HEAD when you start — commits may have landed.

---

## 0. One boundary, non-negotiable

There is an **owner-managed track that is off-limits to you.** You will see 🚫
callout lines at certain pointers in the docs, and a folder you should not open.
**Do not read, open, investigate, or pick up anything behind those 🚫 markers.**
If a task ever seems to require crossing that boundary, **stop and ask Dylan** —
do not work around it. Everything in *this* handover is deliberately on the safe
side of that line, so you never need to. That's the only rule that isn't a
judgment call.

Everything else below is your actual scope.

---

## 1. Where the project is, in one paragraph

Disco is a self-hosted **Research + Agent** platform (one agent core over an
append-only event log, engineered to stay reliable on local/open-weight models),
and its current headline capability is a **builder**: the Agent surface generates
real, durable web apps (Cloudflare-Worker + D1 + React SPA). This sprint shipped
the **primitive framework** — a one-call "add X to my app" system — plus the first
three catalog primitives, and proved it end-to-end with a live model. The framework
is the keystone and it's *done*; adding more primitives is now mostly mechanical.
What remains is: a short list of half-finished edges, one high-value **engine bug**,
a batch of **fan-out-safe feature primitives** that ship on today's shape, a
deferred deeper catalog that waits on a bigger runtime decision, and **packaging**
for single-command self-hosting. That's the whole board.

---

## 2. Read these first (in this order)

1. **`CLAUDE.md`** (repo root) — the operating manual. Layering rule, the five
   fitness gates, how to run tests (`.venv/bin/python3 -m pytest`, **never**
   `uv run pytest`), Serena for symbol navigation, the event-sourcing invariants,
   and the visual-evidence requirement. Internalize this; it encodes the gotchas
   that cost real time.
2. **`docs/disco-project-state.md`** — the master scoreboard. Epic status, what
   shipped, the honest-edges list, the live-proof record, and the engine bug.
3. **`docs/disco-status-and-remaining.md`** — the feature catalog + remaining
   items + the packaging (Epic P) plan in prose.
4. **`docs/disco-builder-primitives-plan.md`** — the campaign plan. Useful
   sections for you: **§4** (the primitive catalog), **§6 / §10.0** (packaging
   and the as-built framework record), **§10.1** (the D1-vs-Postgres decision).
   *Skip §7 and any 🚫-marked pointer.*

Then read code, not prose, to ground yourself: the three shipped primitives are
your reference implementations (see §4 below).

---

## 3. What's already shipped (context, not your work)

You're inheriting a working system. Don't re-derive it; build on it.

| Area | State | Where |
|------|-------|-------|
| **Primitive framework (Epic F-A)** | DONE | `disco.core.appkit` — `PrimitiveDefinition` (id / tier / host_contract / spec_schema / verify / apply_spec), `register_primitive`, the `app_add_primitive` tool, host-service registry + dispatcher, per-primitive verify dispatch + the finish gate |
| **Catalog: `form` / `seo` / `collection`** | DONE, addable via `app_add_primitive` | commits `a656334c` / `055fb99a` / `172ce70d` |
| **Design directions 9 → 22 + numeric design lint (Epic A)** | DONE | `core/design/*` |
| **Build depth (Epic B)** | DONE | durable generated apps — D1 persistence surviving cold restart, related records/CRUD, client reactivity |
| **Tier-3 build guidance (Epic C)** | DONE | `core/loop/*` guidance strings |
| **Walkthrough/runthru fix batch** | DONE | the live-bug batch Dylan surfaced (OpenRouter/image-gen, research surface, export/preview honesty, exported-site MIME+zip, real visuals, honest deck fallback, instant pause, slides timeout) |
| **Live end-to-end proof** | CONFIRMED | a live model (`deepseek-v4-pro`) drove the real app and added `seo` + `form` through `app_add_primitive` autonomously; folds render correctly |

**Registered primitives in the tree:** `lead_gen`, `directory`, `records`,
`hello`, `form`, `seo`, `collection`.

**How a primitive works (the pattern you'll reuse):** a primitive declares a
`spec_schema` (the small declarative spec the *model* fills), an `apply_spec`
that folds that spec into the app's `AppSpec`, an emitter that regenerates the
app tree, and a `verify` hook. The agent picks a primitive and fills its spec;
Disco validates → folds → regenerates. Read `form`/`seo`/`collection` end-to-end
before writing a new one — they show the whole shape and the tests that gate it.

---

## 4. The general shape of what's left

Hold this mental model; it explains why some things are "now" and others are "later":

- **The framework is done, so primitives are cheap.** A new fan-out-safe primitive
  is `spec_schema` + `apply_spec` + emitter + tests — mechanical, and safe to build
  several in parallel worktrees because their emitters are disjoint.
- **There's a dual-track runtime decision (plan §10.1).** Today's apps run on the
  **Cloudflare-Worker/D1 shape**. A subset of the deeper catalog needs a
  **persistent Postgres runtime** that is explicitly *out of scope this sprint*.
  So: build what ships on D1 now; the Postgres-track items wait on a separate,
  bigger decision (flag them, don't start them).
- **Packaging is the finish line.** Epic P turns this into a single-command
  self-host. Recon is done (the compose/Dockerfiles/`.env.example` exist); it's
  hardening + gap-closing, not greenfield.
- **One engine bug outranks new features** for "does this feel finished": the
  autonomous build can't always cleanly *terminate* (see §5). Fixing that makes
  every autonomous build feel done instead of hung.

---

## 5. Half-baked / known edges (your real punch list)

These are shipped-but-not-clean, all feature/engine (no boundary-crossing). Verify
each yourself before trusting these descriptions — they're from the last session.

1. **⭐ Engine: appkit autonomous FINISH deadlock (highest value).** The primitive
   tool calls succeed, but an autonomous appkit build can't always cleanly `finish`.
   Three interacting causes (an extension of the known finish-path-drift class):
   (a) a **dictated-content false positive** — a quoted *tool argument* gets treated
   as a required copy floor, and the check looks for files (`app.js`) that don't
   exist in the Vite/TSX scaffold; (b) the **finish-verify probe** emits a `shell`
   call the appkit tool allowlist refuses, so that gate can never pass in
   appkit_mode; (c) the **execution-nudge** re-enters "work" after a "just finish"
   replan, resetting the other gates' refusal counters → deadlock. Full write-up in
   `docs/disco-project-state.md`. This blocks a clean autonomous build from
   terminating *even when the work succeeded* — fix it first.
2. **Form-folded apps are refused at deploy.** `deploy.py`'s canonical-worker check
   reconstructs the worker *without* the form emitter, so a site with an added form
   builds + verifies but is rejected at export/deploy. The deploy gate needs to
   learn the form-aware path.
3. **Forms attach to `lead_gen` apps only.** `directory` / `records` / `hello`
   refuse a form with guidance. Generalizing this is a real feature.
4. **Form `success_message` isn't editable** via `app_update_content` (it's baked
   into the component). Small, but it's a false-affordance smell — either wire it
   or make the limit explicit.
5. **`hello`-app verify quirk — unverified fix.** Pre-A3, the verifier hard-failed
   `hello` apps (expected a `schema.sql` they don't have) and runs ended STUCK.
   WO-A3 gave `hello` its own verify hook that *should* fix it — but that's
   **unverified**. Confirm with a live `hello` build before relying on it.
6. **`check_arch_budget` is red on every commit.** ~19 pre-existing god-object
   violations (`AgentLoop`, `ConversationRuntime`, `synthesize_section`, …). This
   is **baseline debt, not caused by recent work**, but it *is* a red required-CI
   gate. Know it's red so you don't mistake it for something you broke; decomposing
   the worst offenders is legitimate closing-out work if Dylan wants it.

---

## 6. Remaining feature work

### 6a. Ships on today's D1 shape — fan-out-safe, mechanical

Each = `spec_schema` + `apply_spec` + emitter + tests, following the `seo`/`form`/
`collection` pattern. Disjoint emitters ⇒ safe to build several in parallel.

| Primitive | Sketch |
|-----------|--------|
| **Analytics** | per-app site id + an embedded read-only dashboard |
| **Feature flags** | a small flag SDK + a minimal admin view |
| **Blog / RSS** | extends `seo` + `collection`; blog = a content-collection template + auto RSS |
| **Owner content-edit UI** | the rest of the content story — collections currently edit via re-apply; give owners a real edit surface |

### 6b. Needs the persistent-runtime / Postgres track first — DEFERRED

Do **not** start these; they wait on the §10.1 runtime decision. Flag them if a
request implies one.

- Migrations / ORM, search (pgvector), cache, jobs/cron
- Embedded AI / RAG over the existing router + local encoders (**the
  differentiator** — keyless, free-at-inference)
- Error-monitoring → Agent loop ("fix this prod error" as a one-click build)
- Deployment / hosting (preview → promote, custom domains)

### 6c. Packaging — Epic P (next up, recon done, not started)

Single-command self-host. Ground truth: `compose.yaml`,
`deploy/compose/Dockerfile.server` + `entrypoint.sh`, `frontend/Dockerfile`,
`deploy/sandbox/Dockerfile`, `.env.example` all exist. Ordered:

- **P1** — compose `profiles:` so bare `docker compose up` = app+agent+frontend+data;
  print the working UI URL + first-run path on boot; fix the `docs/self-html.md` →
  `docs/archive/self-host.md` broken reference. *Acceptance:* `git clone` +
  `docker compose up` on a fresh box → UI at a printed URL, zero source edits.
- **P2** — `full`/`lite` image variants around the default embedding weights
  (`full` pre-bakes them for offline RAG; `lite` makes them an optional dep +
  remote-endpoint default). Fix the `.env.example` encoder-tier default + stale
  reranker name.
- **P3** — promote `deploy/sandbox/` to a first-class deliverable; scrub
  host-specific defaults in `core/llm/config.py` (`workspace_root`, etc.) to neutral
  container-local values; rewrite the stale sandbox README.
- **P4** — trim `.env.example` to required-vs-optional; add a first-run "configure
  your model" step (the seed defaults to an empty Ollama endpoint — the #1
  out-of-box failure).
- **P5** — release hygiene: SBOM of bundled weights/deps, image-size trim + recorded
  targets, README quickstart matching P1's real commands.

### 6d. Mega-soak — Epic Z (the integration gate)

A broad live soak across all surfaces (research / build / agent / deep_research)
**and** every primitive's build-and-run path, on real models — then debug each
failure class to a clean bar rather than logging and moving on. The epics landed
individually green; only a broad soak proves they compose. This is the "is it
actually done" gate, ideally before/with packaging.

---

## 7. How to work here (the discipline that's expected)

Dylan's north star: **nothing that looks done but was never run.** Match it.

- **Prove it with a live run, not a green test count.** Cassettes/mocks prove
  "didn't break"; only a live model / real runtime / real render proves a feature
  *works*. Every change you call done names its live proof.
- **Visual evidence for any UI change** — a real Firefox screenshot of it working
  in the running app (Playwright is Firefox-only here by design; headless Chromium
  can't rasterize on this host). Attach it.
- **Run the gates before declaring done:** `uv run basedpyright` (zero errors
  tree-wide), `uv run lint-imports`, `uv run python scripts/gen_arch_diagram.py
  --check`, the relevant `pytest` packages, and `check_arch_budget` (known-red
  baseline — don't *add* violations). Exit code is truth, not the summary line.
- **Navigate with Serena, not grep**, for symbol work — names recur across the five
  packages.
- **No false affordances, no cheap workarounds, preserve originals.** Flag any gap
  loudly rather than papering over it.
- **Parallelize** — the primitive work is worktree-friendly (disjoint emitters +
  "don't touch X" fences); don't leave capacity idle.
- **In normal autonomous execution you defer, you don't block.** §8 is the one
  deliberate exception: Dylan is *asking* you to gather questions this time.

---

## 8. ⬅ YOUR DELIVERABLE: bring Dylan the questions that close this out

After you've done the reviews above — read the docs, read the three reference
primitives, run the gates to see the real baseline, and reproduced the finish
deadlock (§5.1) — **do not just start building.** First, compile the set of
questions that only Dylan can answer and that genuinely change what "done" looks
like. Present them to him as a short, prioritized list (group them, and where you
have a recommendation, lead with it).

Aim your questions at **decisions, not facts you can find in the code.** Good
territory to probe (form your own from what you actually find — don't just echo
these):

- **Definition of done.** What does "v0.1 is basically it" mean concretely — which
  of §6a / §6c / §6d are in the v0.1 line, and what's explicitly post-v0.1?
- **Sequencing / priority.** Is the §5.1 finish-deadlock bug priority-one before
  any new primitive? Does packaging (Epic P) come before or after the fan-out
  primitive batch? Before or after the mega-soak?
- **The runtime fork (§10.1).** When does the D1-vs-Postgres decision get made, and
  does anything in §6a change if a Postgres track is coming? (This gates half the
  remaining catalog.)
- **Scope cuts.** Of the §6a primitives, which actually matter for the launch story,
  and which can be dropped or deferred without hurting v0.1?
- **Packaging target.** Who is the first self-host user, and what's the minimum bar
  for P1's "clone → `docker compose up` → working UI" to count as shippable?
- **The red arch gate (§5.6).** Is decomposing the god-objects in-scope for closing
  out, or accepted debt for v0.1?
- **Verification bar.** How much live-soak (Epic Z breadth) does Dylan want before
  calling it done — a quick pass, or the full 193-run-style methodology?
- **Anything you found in review** that's ambiguous, looks half-wired, or where two
  docs disagree — surface it as a question rather than guessing.

Then wait for his answers before executing. That conversation is the point of this
handover.
