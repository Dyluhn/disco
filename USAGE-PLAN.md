# USAGE-PLAN — who does what after the bake-off

Routing implemented 2026-06-10 from the decided conclusions in `bakeoff/SUMMARY.md`.
Each row cites its bake-off evidence. Estimates are of MY prior involvement removed,
graded honestly in both directions — not inflated to please, not deflated to
self-preserve.

## Job-by-job

### 1. Diff verification ("is this change real / not a bandaid / not a stub")
- **Who now:** Claude, unchanged. Cheap models may only pre-filter
  (`scripts/diff-prefilter.sh` → `review-queue/prefilter/*.leads.txt`, hard-labeled
  UNVERIFIED).
- **Why:** RESULTS-verifier.md — DeepSeek/MiniMax/Qwen all **0/3** on the project's
  three canonical bad patterns; failure mode was *confident wrongness*, and two
  models produced the identical false catch (so cheap-ensemble voting is dead too).
  No cheap CAUGHT/MISSED label gates anything, anywhere.
- **Escalation to me:** everything. Pre-filter leads arrive as hints in my context.
- **Involvement removed: ~10%.** The leads genuinely speed up claim-vs-diff
  cross-checking on big commits (the one task all three cheap models did reliably),
  but the verdict work — cross-hunk reasoning, cause-vs-symptom — is untouched and
  was always the expensive part.

### 2. Screenshot triage
- **Who now (REWIRED 2026-06-10):** Gemini 3 Flash, with **Sonnet (headless claude)**
  as mandatory quota fallback (`scripts/screenshot-triage.sh`). The MiniMax-via-pi
  fallback is REVOKED — no paid OpenRouter models via pi. Judged against an explicit
  expected-state string written by the spec author, never "does this look ok".
- **Why:** RESULTS-vision.md — Flash **4/4** (MiniMax also went 4/4, retained as data
  only), catching the two adversarial cases (caption-vs-blank contradiction,
  NOT-WIRED banner vs decoy "Connected" dots).
- **Escalation to me** (`review-queue/screenshots/`): FAIL verdict, LOW confidence,
  malformed output, or PASS-with-no-visible-cues. Auto-passes are logged, not shown.
- **Involvement removed: ~80% of screenshot reviewing.** I previously viewed every
  shot; now I see escalations plus whatever I choose to spot-check. Honest caveat,
  stated not softened: final visual sign-off has historically caught things
  checklists missed (CONTEXT.md §5 failure mode 1), and the bake-off sample was 4
  curated shots. The control that keeps this safe is the *expected-state string* —
  it is authored by whoever wrote the spec (me), and a lazy one-liner there degrades
  the whole cut. SendUserFile of final per-order evidence screenshots to Dylan is
  unchanged — that rule was never about me.

### 3. Live-spec drafting vs iteration
- **Who now (REWIRED 2026-06-10):** Gemini Flash drafts from a brief via pointer
  prompt, fallback gpt-oss-120b:free (`scripts/spec-draft.sh`, template
  `agent-projects/spec-briefs/TEMPLATE.md`); five deterministic gates reject drafts
  that would mechanically false-fail BEFORE a ~25-minute live run is spent. I keep
  the iterate-against-reality loop and final sign-off. (MiniMax was the bake-off's
  strongest drafter but is paid-via-pi — REVOKED; the gates carry its lessons.)
- **Why:** RESULTS-spec.md — all three candidates false-failed every real passing
  run on a fact (`shell` vs `shell_exec`) that only live iteration had surfaced.
  Drafting is delegable; convergence against reality is not.
- **Escalation to me:** every gated-pass draft, for live iteration; every gate
  failure is a brief-or-draft fix, also mine (cheap retry first).
- **Involvement removed: ~30% of spec work.** Drafting is the smaller half;
  BP-02's spec took 6 live iterations and that loop is intact by evidence, not
  sentiment.

### 4. Orchestration plumbing
- **Who now:** bash (`scripts/orchestrate.sh` + `orders.yaml`). Dispatch with
  timeout + stall-kill, artifact existence/freshness verification, pi
  stdout-salvage, manifest-driven collision refusal and per-order staging. Zero
  model calls; on any failure it stops with a written reason and never improvises.
- **Why:** SUMMARY.md routing; CONTEXT.md §4 — polling heartbeats, by-hand staging
  plans, and log salvage were pure-Claude overhead with no judgment content.
- **Escalation to me:** every reason file in `.orchestrate/stop/`, every salvage
  flag in `.orchestrate/flags/` (a salvaged artifact is a worker CLAIM, not a
  deliverable). Judgment calls — what to dispatch, deviation decisions, brief
  authoring — were never plumbing and stay mine.
- **Involvement removed: ~70% of coordination overhead.** Worker babysitting
  (watch loops, freshness checks, salvage, staging arithmetic) is gone; what
  remains is reading reason files and deciding.

## What stays load-bearing on me (by evidence, not preference)
Diff/work verification (0/3, RESULTS-verifier.md) · spec iteration + sign-off
(3/3 candidates false-fail real runs, RESULTS-spec.md) · surgical-brief authoring
and architectural/deviation judgment (untested in the bake-off — no cheap model has
demonstrated either) · the escalation queues the cuts produce.

## Net effect, honestly
Weighting my prior time across these activities (verification-heavy, per
CONTEXT.md §6): this removes roughly **30–40% of my total involvement**, dominated
by the screenshot cut (highest volume) and the death of polling/salvage/staging
overhead. It does NOT cut the single most expensive activity — evidence
verification and live-spec convergence — because the bake-off measured exactly
that and the cheap models failed it. Re-test that frontier when a materially
stronger cheap model appears; the bake-off harness in `bakeoff/` is reusable
as-is.
