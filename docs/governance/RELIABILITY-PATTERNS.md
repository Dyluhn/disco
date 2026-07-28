# Reliability Patterns

**Status: APPEND-ONLY.** Evidence-backed recurring bug patterns and their
structural remedies.

## Admission rule

A pattern is admitted only with **evidence across at least two fixes/failures,
or one demonstrated cross-cutting mechanism.**

Do not turn a single anecdote into a framework. Do not record theoretical
hardening ideas here — this is a ledger of what actually broke, more than once.

For each pattern prefer **one structural choke point** plus **one regression
that detects the whole family**. Do not add a keystone configuration table for
every framework or model.

---

## Standing patterns (carried in as already evidenced)

These were evidenced during earlier phases of this program. They are recorded
here as the working set an hourly review checks new fixes against.

### P1 — Lifecycle teardown ordered before durable evidence capture

**Shape.** A run is torn down (killed, stopped, cleaned) before the evidence
that proves what it did is durably captured. The evidence is then gone or
describes a state that no longer exists.

**Earliest broken invariant.** Evidence must be bound to exact event-bound
immutable state *before* the authority that produced it is dismantled.

**Structural remedy.** Freeze first, then tear down: establish the durable
marker and the immutable version, bind evidence at or before that horizon, and
only then run the ordinary teardown path.

**Signal to recognise it earlier.** A teardown path that runs before any
`*Version`/`PAUSED`-style durable marker is appended.

---

### P2 — Mutable head substituted for exact event-bound immutable state

**Shape.** Collection reads "latest", the ProjectStore head, or a symlink
instead of the exact immutable version an event named. The result looks
plausible and is silently wrong.

**Earliest broken invariant.** Evidence must reference the exact immutable
version bound to the accepted event horizon
([BOUNDARIES §7](./ARCHITECTURE-BOUNDARIES.md)).

**Structural remedy.** Identity/digest checked around collection; symlink and
mutable-head fallback **fail closed** rather than substituting.

**Signal to recognise it earlier.** Any read path that can resolve to a head
pointer when an exact version is available.

---

### P3 — Model-facing context loses a stable host capability fact

**Shape.** A host refusal or capability constraint is stated once, then dropped
by condensation. The model repeats the forbidden operation because, from its
view, it never happened.

**Earliest broken invariant.** Host authority must survive context handling
([BOUNDARIES §9](./ARCHITECTURE-BOUNDARIES.md)).

**Structural remedy.** Host-authored **typed** runtime constraints with stable
key, scope, lifetime/expiry, bounded guidance, and a usable alternative —
carried independently of prose summarization.

**Signal to recognise it earlier.** The same normalized error recurring after a
condensation boundary.

---

### P4 — Provider protocol / tool markup accepted as ordinary prose

**Shape.** Raw DSML or tool-call protocol markup from the provider is stored as
though it were a summary, then replayed to the model as context.

**Earliest broken invariant.** Protocol output is not prose
([BOUNDARIES §9](./ARCHITECTURE-BOUNDARIES.md)).

**Structural remedy.** Structurally validate summarizer output; reject protocol
markup; allow one bounded repair; fall back to a truthful host-generated summary
that says what it omitted.

**Signal to recognise it earlier.** Persisted summary text containing
tool-call/protocol delimiters.

**Caution.** Ordinary HTML, JSX, and shell/code snippets are legitimate content.
A rejection rule that eats them is overhardening.

---

### P5 — Returned view and event list describe different horizons

**Shape.** A builder appends condensation and rebuilds while the materialization
path can still return pre-build events. The same span is then condensed twice.

**Earliest broken invariant.** One response, one horizon
([BOUNDARIES §8](./ARCHITECTURE-BOUNDARIES.md)).

**Structural remedy.** The returned view and its events share a single
post-condensation horizon by construction.

**Signal to recognise it earlier.** Condensation count rising faster than
distinct spans.

---

### P6 — Harness cleanup/config not using the same environment as startup

**Shape.** Startup uses one environment; cleanup or config resolution uses
another. Resources look released but are not, or config diffs appear that were
never intended.

**Earliest broken invariant.** Truth measurement must use the same environment
as the thing it measures.

**Structural remedy.** One environment contract shared by startup, cleanup, and
config binding; a config diff proves the sole intended difference.

**Signal to recognise it earlier.** A leak or config discrepancy that only
appears in the harness, never in product use.

---

### P7 — A host fallback or false classification masks the original verdict

**Shape.** A secondary failure (timeout, cleanup error, subordinate gate)
overwrites or launders the primary verdict, so the real cause disappears from
the record.

**Earliest broken invariant.** The primary verdict is authoritative; subordinate
facts are recorded *alongside* it, never in place of it.

**Structural remedy.** Subordinate outcomes are recorded as subordinate, with
the primary verdict retained verbatim.

**Signal to recognise it earlier.** A verdict field whose value can be
overwritten by a later, lesser failure.

---

### P8 — Stale campaign/test authority overriding current product contracts

**Shape.** An old plan, matrix, work order, or handoff is treated as current
status, and work is done against a target that no longer exists.

**Earliest broken invariant.** Authority order
([README](./README.md)): current code + tests + evidence beats every document.

**Structural remedy.** One finite authority surface; everything else explicitly
marked history; stale counting authorities re-authored against the actual final
bytes before anything counts.

**Signal to recognise it earlier.** A document being cited for *status* rather
than for design rationale.

---

## Campaign-observed patterns

### P9 — A predicate evaluated over a narrower span than the claim it supports

**Evidence (two instances, 2026-07-25, both in the governance tooling itself).**

1. The seal guard's shell-mutation regex `\bsed\b[^|;]*-i` matched only the text
   `"sed -i"`. The target path fell outside `match.group(0)`, so the guard asked
   "does this two-character match contain a protected path?", correctly answered
   no, and **failed open** on `sed -i … ENGINEERING-STANDARDS.md`.
2. The campaign completion sentinel was checked with a substring test. The
   status ledger *documents* the sentence that must eventually appear, inside a
   fenced code block — so the gate read its own documentation as the assertion
   and reported the contract **SATISFIED at Epic 0**, which would have permitted
   an immediate voluntary stop.

**Shared earliest broken invariant.** A predicate reported a stronger result than
its evidence supported, because it was evaluated over a *smaller* span (2
characters) or a *larger* span (the whole file including citations) than the
claim required.

**Structural remedy.** Evaluate a predicate over exactly the span the claim is
about: the whole shell segment when asking "does this command touch path P"; a
single non-quoted, non-fenced line when asking "does this document *assert* S".
Citation contexts — fenced, indented, block-quoted — are excluded before a claim
is read as an assertion.

**Signal to recognise it earlier.** A guard or gate that has never been run
against a deliberately hostile input. Both defects were invisible in normal use
and both surfaced on the *first* adversarial control.

**Regression that protects it.** The guard mutation matrix (11 deny / 5 allow,
including `sed -i`, `tee`, `truncate`, `git checkout`, `python -c`, and
basename-only spellings) and the sentinel negative+positive control pair.

**Why it stays target-neutral.** It constrains only how a predicate is *scoped*.
It adds no framework-, model-, or scenario-specific knowledge, and the seal guard
remains deliberately heuristic — `check_governance_seal.py` stays the authority.

---

### P10 — An enforcement mechanism with no termination bound

**Evidence (one demonstrated cross-cutting mechanism, 2026-07-25).** The `Stop`
hook refused to let a session end whenever the campaign completion contract was
unmet. That condition is true *by definition* until the final epic closes, so
**every** session started in this repository became unstoppable — including
unrelated one-shot invocations with nothing to do with the campaign. Observed as
`claude -p` hanging on a trivial prompt; parking `.claude/settings.json` made it
return instantly.

**Earliest broken invariant.** A guard must bound its own refusals. An
enforcement rule whose stop condition can never be met during normal operation is
a denial of service on all future work, not a guardrail.

**Structural remedy.** Bound consecutive refusals (here:
`MAX_CONSECUTIVE_BLOCKS = 3`, then allow with a loud message), and reset the
counter once a stop is allowed so the next genuine attempt is refused just as
firmly. The intent — defeat a premature hand-back — is fully served by an
*emphatic* refusal; it never required an *infinite* one.

**Signal to recognise it earlier.** Any always-on gate whose release condition is
a project milestone rather than a per-invocation fact. Ask: "what does this do to
a session that is not the one I am thinking about?"

**Regression that protects it.** The bounded-refusal sequence test: block, block,
block, allow, then re-armed on the next attempt.

**Why it stays target-neutral.** It is a property of the guard's control flow,
not of any product behaviour, and it leaves the refusal semantics unchanged.

**Related.** This is the mirror image of [P7](#p7--a-host-fallback-or-false-classification-masking-the-original-verdict):
P7 is a mechanism that says too little (a verdict laundered away); P10 is a
mechanism that says the same true thing forever, until saying it is the failure.

---

### P11 — A consumer reads a shape the producer never emits

**Evidence (two instances).**

1. *Prior, recorded in code.* `normalize_event`'s docstring documents a P0
   false-negative in which "a row with top-level `kind` had its payload dropped,
   hiding `detail`/`tool_call`/`revision` from every predicate."
2. *This campaign, 2026-07-25.* `freeze_progressing_workspace` read `status`,
   `trigger`, `version_seq`, `run_intent_id`, and `agent_view_id` **directly off
   raw SQLite rows**. `collect_events()` returns
   `{seq, kind, source, id, created_at, payload}` where `payload` is an unparsed
   JSON string — only `seq`/`kind`/`source` are real columns. Every one of those
   reads returned `None`, so the pre-kill freeze would have reported
   `FREEZE_TIMEOUT` on **100% of real runs** while claiming to preserve evidence.

**Earliest broken invariant.** Persisted rows are not domain events. A predicate
must consume the canonical normalized form, not the storage form.

**Structural remedy.** Exactly one canonical normalizer, applied at every
boundary where persisted rows enter logic. `normalize_events` was already used at
four other call sites in the same adapter; the freeze was the only path that
skipped it. The remedy is routing, not a new contract.

**Signal to recognise it earlier — this is the important half.** The defect was
invisible to **eleven** passing unit tests, because their fake `collect_events`
returned hand-built **flat dicts**: a shape the real producer never emits. A fake
that is more convenient than the real source cannot fail the way production
fails. Watch for any fixture whose event shape differs from what the real query
returns.

**Regression that protects it.** A real-path test driving `run_once` against
actual SQLite rows and a real `ProjectStore` version. Its protective value was
**proven by revert-check**: with the bug reintroduced it fails with exactly the
production symptom (`FREEZE_TIMEOUT != frozen`), while all eleven fake-based
tests stay green.

**Why it stays target-neutral.** It adds no product behaviour and no
scenario-specific knowledge — one existing consumer now uses the normalizer the
codebase already treats as canonical.

**Standing practice this earns.** When a fix matters, **re-break it on purpose**
and confirm the new test fails for the right reason. That converts "I wrote a
test" into "I proved this test protects this fix", and it is what exposed the
blindness of the fake-based suite here.

### P12 — A fail-closed refusal names the rule but not the state it judged

**Evidence (two instances, both in the harness's own evidence chain).**

1. *F-21 (2026-07-27).* Capture-authority refusals at non-FINISHED lifecycle
   boundaries reported only that capture was denied; establishing WHICH boundary
   state had been consulted took **three diagnosis passes** before the fix
   (`c595d289`) could even be aimed.
2. *F-26 (2026-07-28, cert10 `p4_ff_react_steer`).* The browser-evidence
   collector refused `.pmx/screenshots/0001-navigate.png` with a payload of
   exactly `{conversation_id, path}`. A later provider-free replay of immutable
   version `003-bc7ae1f7396f` at `horizon_seq=321` found all nine referenced
   screenshots and succeeded. Both observations were correct, and the dossier
   could not reconcile them — the refusal never recorded which workspace
   directory, version, horizon, or manifest it had judged. A wrong mechanism
   ("manifest built from declared paths only") survived a full session because
   the evidence needed to falsify it was structurally absent.

**Earliest broken invariant.** A refusal at an authority boundary is itself
evidence, and evidence must be attributable: any error that consulted versioned
state (workspace, immutable version, manifest, event horizon) must carry that
state's identity in its payload. A rule citation without the judged state makes
every future occurrence equally undiagnosable and any proposed fix
unfalsifiable from the dossier.

**Structural remedy.** Enrich the refusal payload at the point of judgment with
the identity actually consulted — workspace dir, manifest entry counts, and the
referenced paths under judgment (`02ef30c0`); same shape as the existing
`SnapshotNotReadyError` payload, which already names seal/digest/count facts.

**Signal to recognise it earlier.** Any `raise` at a boundary that read
versioned or snapshotted state whose payload names only the missing thing plus
an id. If a live failure and a later replay of "the same" state could disagree
without the payload telling you which state each saw, the payload is too thin.

**Regression that protects it.**
`harness/build_soak/tests/test_missing_screenshot_names_the_state_judged.py` —
hermetic, asserts the refusal carries the workspace it judged.

**Why it stays target-neutral.** Payload enrichment only: no threshold,
ordering, acceptance, or classification change; the refusal fails closed
identically for every target and scenario shape.

### P13 — A terminal bound to a version authority it never verified, disclosed too late to act

**Evidence (three members: two counted-lane failures on qualified
candidates, one qualification-pilot failure — the third generalizes the
family from version authority to any terminal-boundary resource assumed
rather than bound).**

1. **F-26 (pause side, cert10).** The soak's PAUSED work terminal was judged
   against the *finish* seal — an authority a PAUSED run can never produce —
   so honest paused work surfaced as INVALID_RUN evidence gaps. Fixed by
   `collect_paused_workspace`: the boundary's evidence now comes from the
   pause-sealed immutable version the product actually created (`8f249c7a`).
2. **F-27 (finish side, counted trial 590005).** The model symlinked root
   paths; sandbox wrote them, the done-condition checker rejected them (the
   model honestly re-planned but left the inert links), build + live web
   verification resolved them, and the FINISHED terminal was accepted — then
   the strict final seal refused the content, and the only disclosure landed
   POST-terminal (seq 305), where nobody could act. The finished build was
   unattributable and unexportable, and the harness classifier mapped the
   product's refusal to the same rerunnable INVALID_RUN as snapshot lag.
3. **F-28 (session side, qualification pilot seed 620108).** A workspace
   restore 1.7 s after FINISHED acquired the just-terminal executor's
   session while the teardown was racing it; the apply stage died mid-flight,
   the product logged nothing, the client saw a bare 503 whose body the
   harness did not retain — and the identical restore replayed moments later
   succeeded. The assumed resource was a live SESSION rather than a version,
   but the invariant broken is the same: a post-terminal operation trusted a
   moments-old authority instead of binding or deterministically reacquiring
   it at the point of use. Fixed by a bounded reacquire-once through the
   existing wake/create chain (never the same failed object), loud logging
   on every failed restore, and verbatim retention of the product's error
   body in harness evidence.

**Shared earliest broken invariant.** A terminal state was accepted (or
adjudicated) under a version authority that was never bound at that boundary
— and the constraint that voided the terminal reached the model only after
the last point it could act.

**Structural remedy.** (a) Every terminal binds to the version authority it
claims: PAUSED work reads its pause seal; an affirmative FINISH is gated by a
dry-run of the *same* snapshot walker + skip rule the final seal will apply
(one content model, zero drift), with the refusal naming the exact blocking
entries and remedy while the model can still act. At the two finish-battery
sites (finalize, completed-via-notify) a bounded cap (3) releases loudly
(`unsealed_release`) so no legitimate build is caged — measured: 3 refusals →
1 release → FINISHED. At the browserless valve a refusal instead degrades to
the pre-existing safe PAUSE/actionless terminal (the per-run-segment streak
reset makes the cap unreachable there; the route to a valid terminal is
preserved either way). (b) The product
discloses a refused seal as TYPED evidence (`persistence_failure.kind =
"seal_incomplete_content"`), and the harness adjudicates product-vs-lag from
that product-owned disclosure — never a private re-derivation, never one
bucket for both.

**Signal to recognise it earlier.** Any acceptance path whose authority check
runs only after the acceptance is durable; any refusal surfaced only
post-terminal; any classifier that maps "the product refused" and "the
evidence is late" to the same rerunnable outcome.

**Regression that protects it.** `test_finish_seal_gate.py` (7: red→green on
the live 590005 signature, cap release, controls incl. probe-error anti-cage
and forced-terminal bypass), the browserless-valve pair in
`test_bug6_actionless_honest_finish.py` (a blocking probe never lands the
valve's FINISHED; a sealable probe leaves it byte-identical), the
typed-disclosure pair in `test_final_workspace_commit.py`, the harness split
tests (both directions + superseded-refusal + a recovery/pause version cut
never supersedes a refused finish seal), and the F-26 paused-terminal
suites on the same bytes.
