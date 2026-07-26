# Self-Reviews

**Status: APPEND-ONLY.** One entry per hourly review. Never edit or delete a
past entry.

An hourly review is **not a checkpoint and not permission to stop.** After
writing one, continue immediately with the next campaign action.

## Required shape

Every entry must answer every field. A timestamp is not a review — the
PostToolUse hook validates these fields structurally and will not advance the
schedule until each is answered.

```text
## Review <N> — <local time> / <UTC time>

Source fingerprint: sha256:<scripts/source_fingerprint.py --which source --quiet>
Work completed since prior review:
Evidence that it actually worked:
What went well and why:
What went rough / consumed time or tokens:
Immediate process or technical correction:
Recent fixes reviewed together:
Repeated pattern detected? (yes/no):
  # if yes:
  - shared earliest broken invariant:
  - structural product/harness remedy:
  - signal that would recognize it earlier next time:
  - existing/new regression that protects it:
  - why the remedy remains target-neutral and flexible:
Overhardening check:
  - observed failure or authoritative contract requiring each open item:
  - any theoretical tail to drop:
Next action:
```

A pattern is promoted to [`RELIABILITY-PATTERNS.md`](./RELIABILITY-PATTERNS.md)
only with evidence across at least two fixes/failures, or one demonstrated
cross-cutting mechanism.

---

## Review 1 — 2026-07-25 21:58 CDT / 2026-07-26T02:58:00Z

Source fingerprint: sha256:bd1b1bba209e6b9362df1e69e360b0dd6d8b56087b147bfa72eeae81a63c8b98

Work completed since prior review: First review of the campaign; covers Epic 0
from session start. Verified the entire handoff state against the brief rather
than assuming it (branch, HEAD `271af2e6`, `f55efb03` ancestry, the three active
product paths, empty Serena memories, absent AGENTS.md, external archive
manifest). Reconciled §5 architecture vocabulary against real code. Authored the
nine-file governance surface, `scripts/source_fingerprint.py`,
`scripts/check_governance_seal.py`, five `.claude/hooks/*` scripts, and
`.claude/settings.json`. Created and fail-closed verified the Ollama-only GLM 5.2
OpenCode launcher.

Evidence that it actually worked: `sha256sum -c MANIFEST.sha256` → 33/33 OK,
exit 0. Seal gate exercised across its full state machine — exit 3 (no manifest)
→ 1 (rebaseline refused without env) → 0 (rebaselined) → 0 (verify) → 1 (drift,
named exactly) → 0 (restored). Guard mutation matrix: 11/11 mutation spellings
denied (Edit, Write, `>>`, `>`, `sed -i`, `tee`, `rm`, `mv`, `cp`,
`git checkout`, `truncate`, `python -c`, basename-only), 5/5 legitimate
operations allowed with no false positives. Review mechanism proven on real
wall-clock: interval set to 60 s at 21:56:48, not due at 21:56:28, due at
21:57:09 with injection fired, source mutation denied while overdue, ledger
write still allowed, bare-timestamp review rejected naming all 10 missing
fields with the schedule not advancing. `opencode debug config` assertion
passed: only `ollama-cloud` enabled, `opencode-go` disabled,
`baseURL=https://ollama.com/v1`, key present and never printed.

What went well and why: Verifying the hook schema against the installed binary
(2.1.220) instead of from memory paid for itself immediately — it confirmed
`permissionDecision`, `additionalContext`, the `Stop` `decision/reason` shape,
`stop_hook_active`, and match-all matcher semantics, so the hooks worked on
first run with no schema guesswork. Reconciling §5 against code before sealing
caught that `DeliveryShape`/`PreviewModality` do not exist as types; sealing
those names would have made a frozen boundary false on day one.

What went rough / consumed time or tokens: Two self-inflicted defects, both
found only because I ran adversarial controls rather than trusting the happy
path. (1) The `sed -i` guard pattern matched only `"sed -i"`, so the target path
fell outside `match.group(0)` and the check silently failed open. (2) The
completion sentinel matched its own documentation inside a fenced code block, so
the ledger reported the contract SATISFIED at Epic 0 — the Stop gate would have
let me stop immediately.

Immediate process or technical correction: Every mutator regex now spans to the
end of its shell segment so the path is always inside the tested match. The
completion check now strips fenced blocks, ignores indented/quoted lines, and
requires an exact standalone line; both a negative control (documentation
mention) and a positive control (real assertion) are proven.

Recent fixes reviewed together: the `sed -i` fail-open and the sentinel false
affordance.

Repeated pattern detected? (yes/no): yes
  - shared earliest broken invariant: a check whose *scope* is narrower than the
    claim it is trusted to make. The regex tested 2 characters and concluded
    "no protected path"; the substring test read documentation and concluded
    "contract satisfied". In both cases the mechanism reported a stronger
    negative/positive than its evidence supported.
  - structural product/harness remedy: a predicate must be evaluated over the
    full span of the thing it judges — the whole shell segment, the whole line
    in assertion position — and citation contexts (fenced, indented, quoted)
    must be excluded before a claim is read as an assertion.
  - signal that would recognize it earlier next time: any guard or gate that has
    never been run against a deliberately hostile input. Both defects appeared
    on the first adversarial control and neither was visible in normal use.
  - existing/new regression that protects it: the guard mutation matrix (11 deny
    / 5 allow) and the sentinel negative+positive control pair, both re-runnable.
  - why the remedy remains target-neutral and flexible: it constrains only how a
    predicate is *scoped*, adding no framework-, model-, or scenario-specific
    knowledge; the guard stays heuristic by design and the hash gate remains the
    authority.

Overhardening check:
  - observed failure or authoritative contract requiring each open item: the
    guard matrix entries each correspond to a real shell spelling that would
    have mutated a sealed file; the sentinel controls correspond to an observed
    false SATISFIED reading; the seal state machine corresponds to the brief's
    explicit mutation-proof requirement.
  - any theoretical tail to drop: yes — dropped. I did not pursue exotic shell
    evasions (base64-decoded writes, `exec` redirection, editor subprocesses).
    The brief explicitly forbids turning this into an adversarial shell-parser
    project, and `check_governance_seal.py` catches drift regardless of spelling.

Next action: set the review interval to 3,600 s, reconcile root `CLAUDE.md` into
a bootstrap pointing at the governance authority order, then commit only the
context/governance paths — never the three active product paths.

## Review 2 — 2026-07-25 23:00 CDT / 2026-07-26T04:00:00Z

Source fingerprint: sha256:b94c9002bbc19bd0bdead6cdeeca7ef32d1ed2398778857ce4188e58c78da4b0

Work completed since prior review: Closed Epic 0 and committed it as `32a09957`
(85 paths, three active product paths excluded by explicit pathspec). Proved hook
wiring end-to-end with two real sessions. Found and fixed a defect I had
introduced in the Stop gate. Opened Epic 1 and closed three of its named gaps:
browser-evidence horizon binding, the mutable-head fallback, and the
authority-race fence (adding resume and agent-view supersession to the existing
user-turn and run-intent checks, unified into one choke point). Then found and
fixed a defect in the pre-existing dirty Ruling-2 code that would have made the
freeze useless in production.

Evidence that it actually worked: full `harness/build_soak/tests/` suite exit 0
(~890 passed, 1 skipped) after every change. `test_freeze_before_kill.py` 11
passed. The committed `test_progressing_hardcap_freeze_order.py` reproduction
still 3 passed. Two real `claude -p` sessions attempted to mutate sealed files
via Edit/Write and via Bash and were blocked by the PreToolUse hook in the
model's own words, with the seal exit 0 and both files untouched afterwards. The
new real-path positive test asserts the freeze lands, names the exact immutable
version (`version_seq`, `tree_digest`), preserves `index.html` byte-exact and the
PNG byte-exact across the kill, retains
`RUN_TIMEOUT_WHILE_PROGRESSING`, and issues exactly one kill.

What went well and why: Two methods paid for themselves repeatedly. First,
running adversarial controls instead of trusting the happy path — every single
defect this session was found by a deliberate negative control, never by normal
use. Second, the revert-check: after fixing the raw-row bug I re-broke it on
purpose and confirmed the new test fails with exactly the production symptom
(`FREEZE_TIMEOUT != frozen`) while all 11 fake-based tests still pass. That
turned "I wrote a test" into "I proved this test protects this fix".

What went rough / consumed time or tokens: Three things. (1) My e2e probe
sessions ran with `--dangerously-skip-permissions` and made uninstructed writes
in the campaign worktree — rewriting CAMPAIGN-STATUS.md, stripping
`.serena/project.yml` comments, and creating a 212-line `scripts/glm_delegate.py`
built against a *local* Ollama endpoint, contradicting the mandated
`ollama-cloud` route. All reverted. (2) The `authority-race` GLM delegation cost
1.13M tokens and produced a report I had largely superseded by reading the code
directly. (3) I burned 27,793 tokens on an invalid negative control that could
not have failed, because `glm-run.sh` re-exports the very variable I was trying
to poison.

Immediate process or technical correction: Probe sessions now run read-only or in
a disposable copy — never with write permission in the campaign worktree.
Delegations get a narrower scope and a stated token expectation; anything I can
answer by reading one file, I read. A negative control must be checked for
whether it can actually fail before I trust its result.

Recent fixes reviewed together: the `sed -i` fail-open, the completion-sentinel
false affordance, the unbounded Stop gate, and the raw-row freeze bug.

Repeated pattern detected? (yes/no): yes
  - shared earliest broken invariant: a consumer read a *shape* the producer
    never emits. `freeze_progressing_workspace` read `status`, `trigger`,
    `version_seq`, `run_intent_id` and `agent_view_id` straight off raw SQLite
    rows, where only `seq`/`kind`/`source` are real columns and everything else
    lives inside an unparsed JSON `payload` string. Every one of those reads
    returned None, so the freeze would have reported FREEZE_TIMEOUT on 100% of
    real runs. `normalize_event`'s own docstring records a prior P0 false-negative
    with the identical shape — "a row with top-level `kind` had its payload
    dropped" — so this is the second occurrence, not an anecdote.
  - structural product/harness remedy: exactly one canonical normalizer, applied
    at every boundary where persisted rows enter logic. `normalize_events` was
    already used at four other sites in this adapter; the freeze was the sole
    path that skipped it.
  - signal that would recognize it earlier next time: a test whose fixture is
    hand-built flat dicts while the production source is a database row. The
    fake modelled a shape the real producer never emits, so it could not fail.
  - existing/new regression that protects it: the real-path positive test, which
    drives run_once against real SQLite rows and a real ProjectStore version.
    Proven by revert-check to fail on the bug while the fakes stay green.
  - why the remedy remains target-neutral and flexible: it adds no new contract
    and no scenario-specific knowledge — it routes one existing consumer through
    the normalizer the codebase already treats as canonical.

Overhardening check:
  - observed failure or authoritative contract requiring each open item: the
    horizon clip and the workspace-source registration each trace to a specific
    named gap and an observed loss (counted seeds 460004/460005); the resume and
    agent-view fences are explicit acceptance items; the subordinate-verdict test
    protects an acceptance item about not laundering the primary verdict.
  - any theoretical tail to drop: yes — dropped one. I removed a test asserting
    that an event with no usable `seq` is excluded by the horizon clip: the
    schema enforces `NOT NULL` on `events.seq`, so the durable log cannot produce
    one. The three-line defensive branch stays because it is fail-closed and free;
    asserting an impossible state does not.

Next action: finish Epic 1's remaining acceptance — the symlink and mutable-head
fail-closed identity checks around collection, and explicit confirmation that the
product pause/kill APIs and kill semantics are unchanged — then commit Epic 1 as
one coherent package and move to Epic 2.

## Review 3 — 2026-07-26 00:29 CDT / 2026-07-26T05:29:08Z

Source fingerprint: sha256:af81ec6224cc878852446ce08bade0cde7a1e05ed3973f0da4f22f11bfa62baf

Work completed since prior review: Finished the in-flight Epic-3 one-horizon
slice and committed it (`fad36450`). Corrected the stale status snapshot the
owner flagged. Then executed the owner-authorized acceleration insertion in
full: Package A fast qualification profile (`4ec9bf30`), Package C Semgrep audit
with a reasoned refusal to admit any rule (`d0d70649`), and Package B failure
capsules with an honest unsupported-live-replay boundary (`2fc8645c`). Worktree
clean at every boundary.

Evidence that it actually worked: one-horizon slice — core and agent-server
suites exit 0, revert-check fails with the exact intended message. Package A —
20 tests, F0 exit 0 (~900 tests), and the load-bearing test drives the real
`_build_soak_result` over a *passing* qualification batch and proves it finds
nothing to count. Package B — 6 tests against the real `run_once` hard-cap path
with a real `ProjectStore` version, six distinct tamper cases each refused.
Across all: `ruff check harness/build_soak/` All checks passed, `basedpyright`
0/0/0, full build-soak suite exit 0.

What went well and why: Running eight narrow GLM inventories in parallel was a
genuine step change. The Package-A inventory found the promotion reader's
`rglob("batch-summary.json")` discovery, which is the single fact the whole
structural-exclusion design turns on — I would have found it eventually by
reading, but far later and at much higher context cost. Cost calibration also
became legible: the narrowest delegation cost 47k tokens and the broadest 1.23M,
for comparable usefulness.

What went rough / consumed time or tokens: I was asked directly whether I was
using subagents to save context, and the honest answer was no — one delegation,
poorly scoped, whose result I then superseded by reading the code myself. I had
flagged that cost in Review 2 and *did not change the behaviour*, which is the
worse failure: noticing a problem and continuing anyway. Separately, an early
negative control for the GLM launcher was invalid (the launcher re-exports the
variable I tried to poison), costing 27,793 tokens and proving nothing.

Immediate process or technical correction: delegate inventory work by default
now, in parallel, with numbered questions, a hard line budget, and a
file:line-or-NOT-FOUND requirement. Verify every load-bearing claim myself
before building on it — which is exactly what caught that `run.py:1190` is
*correct* despite looking like the P11 bug.

Recent fixes reviewed together: the one-horizon return, the injectable batch
summary name, and the capsule boundary rule.

Repeated pattern detected? (yes/no): yes
  - shared earliest broken invariant: all three are the same shape — a
    *discovery or hand-off mechanism that reads by convention rather than by
    contract*. The promotion reader discovers evidence by globbing a filename;
    `_materialize_current_view` handed forward an event list by convention that
    it matched the View; a capsule would have "found" the workspace by trusting
    an event's word for the bytes. In each case the consumer had no way to tell
    a correct producer from an incorrect one.
  - structural product/harness remedy: make the contract carried, not inferred.
    Return the horizon with the View; make the promotion-visible filename an
    explicit parameter so a non-promoting lane structurally cannot produce it;
    make the capsule re-verify the immutable version and every restored byte
    rather than trusting the naming.
  - signal that would recognize it earlier next time: any consumer that finds
    its input by pattern (glob, name, "latest", "newest by mtime") rather than
    by being handed an identity. Ask what happens when something *else*
    legitimately produces that pattern.
  - existing/new regression that protects it: the one-horizon revert-check; the
    `_build_soak_result` non-ingestion test plus its leak-back negative control;
    the tampered-immutable-version restore refusal.
  - why the remedy remains target-neutral and flexible: each is a parameter or a
    return value, not a policy engine, and all three defaults are unchanged, so
    no existing caller behaves differently.

Overhardening check:
  - observed failure or authoritative contract requiring each open item: the
    horizon fix traces to the documented k460000 double-condensation; the
    summary-name seam traces to the promotion reader's actual discovery code,
    read directly; every capsule refusal maps to a named Package-B acceptance
    item.
  - any theoretical tail to drop: yes, two dropped. No Semgrep rule was admitted
    — the candidate's correct and incorrect forms are syntactically identical,
    so any rule would flag a legitimate positive control. And live focused
    replay was not built, because it would require a new product API,
    fabricated internal state, or destroying the original verdict.

Next action: resume Epic 2 — the durable typed host-authored runtime constraint
contract, with the process-backend host-signal prohibition as its first typed
producer — then the remaining Epic-3 acceptance work (raw DSML/tool-markup
rejection, one bounded repair, truthful deterministic fallback), then Epics 4-7
in governed order.

## Review 4 — 2026-07-26 01:39 CDT / 2026-07-26T06:39:35Z

Source fingerprint: sha256:e03c5be62b53603448315b572ac7dbd2a30faaa8860e0c6ca5815614cf52049d

Work completed since prior review: Epic 2 implemented and committed (typed
runtime constraint, its producer, and the deterministic loop proof). Epic 3
completed (summary rejection, one bounded repair, truthful fallback). Epic 4
found blocked on infrastructure and the blocker recorded. Epic 5 taken first:
both Ruff gates brought green tree-wide, all three architecture-budget
regressions this branch introduced decomposed, the gate rebaselined and proven
still sensitive, frontend Vitest / typecheck / Vite build / G11 all green, and
the Export Track-1 verifier launched.

Evidence that it actually worked: `test_runtime_constraints.py` 17 passed,
`test_summary_validation.py` 19 passed, `packages/core` + `packages/tools` +
`packages/agent-server` exit 0 (9,020 tests, 0 failure markers, 1 skip). All
four architecture fitness gates exit 0, plus tool schemas, both Ruff gates and
the governance seal. Frontend: Vitest 175 files / 1146 tests passed, typecheck
exit 0, Vite build exit 0, G11 exit 0. Budget-gate sensitivity proven by probe:
`View.of` 255 → 258 exits 1, restored exits 0.

What went well and why: attribution before action, repeatedly. Measuring every
budget violation against `f55efb03` turned "26 failures" into "23 inherited, 3
ours", which converted an apparently campaign-scale refactor into three small,
tractable fixes plus a documented decision. The same habit caught that the live
stack on 8000/8800 belongs to *other checkouts* — running Epic 4 against it
would have filed another worktree's behaviour as this candidate's evidence.

What went rough / consumed time or tokens: three self-inflicted errors, all
caught by re-running the gate rather than by review. My first `StuckDetector`
extraction moved one member of a mutually-referential cluster and broke three
tests. My first rebaseline *replaced* the allowlist instead of merging it,
silently dropping legitimate allowances and turning 23 violations into 10
different ones. And a name-based regex damaged two unrelated entries because two
symbols share each name across files. Each was cheap to catch and would have been
expensive to miss.

Immediate process or technical correction: when a symbol belongs to a cluster,
move the whole cluster; when editing a keyed table, merge rather than regenerate;
when matching by name, match on the qualifying path too. And re-run the gate
after every mechanical edit, not at the end of a batch.

Recent fixes reviewed together: the three decompositions, the budget rebaseline,
and lifting Epic 2's helper out of `execute_and_observe`.

Repeated pattern detected? (yes/no): yes
  - shared earliest broken invariant: **a mechanical edit applied by name rather
    than by identity.** The regex that matched `sed -i` without its path, the one
    that rewrote `run`/`__init__` in the wrong file, and the extraction that took
    one member of a cluster are the same mistake: a transformation keyed on a
    fragment that does not uniquely identify its target.
  - structural product/harness remedy: key mechanical edits on the full identity —
    (path, symbol), not symbol; the whole cluster, not one member; merge into a
    keyed structure rather than regenerate it.
  - signal that would recognize it earlier next time: any edit performed by regex
    or string replace over a namespace where the key can repeat. Ask "how many
    things does this pattern match?" before applying, not after.
  - existing/new regression that protects it: the budget gate itself now has a
    proven sensitivity probe, and the gate was what caught all three errors.
  - why the remedy remains target-neutral and flexible: it is a rule about how I
    edit, not about product behaviour; nothing in the codebase constrains a
    future target.

Overhardening check:
  - observed failure or authoritative contract requiring each open item: every
    decomposition traces to a specific symbol this branch pushed over a cap; the
    rebaseline traces to a gate that was red at stable-main and therefore blind;
    the Ruff work traces to an explicit Epic 5 gate.
  - any theoretical tail to drop: yes — I did **not** decompose the 23 inherited
    violations. `ConversationRuntime` at 4567 and `reduce_progress` at 522 against
    a 200 cap are campaign-scale refactors with real regression risk, and doing
    them inside a preflight would be the opposite of the smallest general
    solution. Recorded as roadmap work with the debt annotated in the source.

Next action: start a Disco stack from THIS checkout on non-conflicting ports so
Epic 4's context diagnostics measure the candidate's own bytes, while the Export
Track-1 verifier and the two GLM review streams finish.
