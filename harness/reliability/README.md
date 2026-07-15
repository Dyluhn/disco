# Disco reliability campaign

This harness is the release gate for Build, Agent, Search, Settings, and clean-device behavior. It is deliberately stricter than “the test command was green”:

- every product claim maps to an executable suite in `matrix.yaml`;
- skipped or missing evidence never counts;
- live runs must retain inspect/model-routing evidence and fail on thrashing;
- one product failure taints every pass from that exact source revision;
- a fix changes the source fingerprint and restarts the post-fix streak;
- source changes during a campaign invalidate its passing results;
- clean-device credit requires distinct hardware-derived fingerprints and the exact candidate commit.

No finite suite can prove that unknown bugs are impossible. The promotion gate instead makes the residual risk explicit: the high-volume lanes require 300 consecutive post-fix passes, which puts the rule-of-three 95% upper bound for an unseen failure rate below 1%. Lower-volume, expensive edge lanes have separate targets and remain hard gates.

## Quick start

Run commands from the repository root with the repository virtual environment:

```bash
cd ~/projects/disclaude
.venv/bin/python3 -m harness.reliability.run --list
.venv/bin/python3 -m harness.reliability.run --proof hermetic --require-promotion
```

The outer runner executes independent suites concurrently. It protects 32 GiB of `MemAvailable` for the desktop by default, reserves each suite's declared memory weight, checks free disk before admission, and never kills an already-running trial merely to admit another one.

The Build soak has a second parallel layer. Its six model/build lanes share a resource pool and recheck live RAM, CPU, and disk before starting queued work. Both layers default to the same 32 GiB host floor; this is an admission threshold, not 64 GiB of additive reservation.

Useful selection examples:

```bash
# One surface
.venv/bin/python3 -m harness.reliability.run --proof live --surface build

# One suite
.venv/bin/python3 -m harness.reliability.run --suite live-search-mcp

# Validate and print a selection without running it
.venv/bin/python3 -m harness.reliability.run --proof live --dry-run
```

Do not run two full live campaign processes at once: every live suite owns a disposable
App+Agent stack on fixed campaign loopback ports. Parallelism is already inside one
campaign and inside the Build soak. The matrix never targets the operator's default
`:8000`/`:8800` services.

## Live prerequisites

The live lanes require signed seed state for the exact driver. The wrapper copies that
state into a new stack directory, enables inspect evidence, and assigns a separate
provider ledger to every suite. The runner refuses missing, malformed, underscoped, or
wrong-host/model evidence; an observed fallback is a product failure.

Set the inputs that apply to the local installation:

```bash
# Every model-driven live suite copies these into an isolated App+Agent stack.
export DISCO_RELIABILITY_SEED_CONFIG="$PWD/disco-config.json"
export DISCO_RELIABILITY_SEED_SECRETS="$HOME/.config/disco/secrets.json"
export DISCO_RELIABILITY_SEED_SECRET_KEY='the key that encrypted that secrets file'
export DISCO_RELIABILITY_SEED_APPROVALS="$PWD/disco-approved-origins.json"
export DISCO_RELIABILITY_EXPECTED_PROVIDER_HOST='your-provider.example'
export DISCO_RELIABILITY_EXPECTED_PROVIDER_MODEL='exact-wire-model-id'

# Set only when the seeded sandbox is a verified real runsc/gVisor backend.
export DISCO_RELIABILITY_GVISOR=1
```

Do not set a shared `DISCO_PROVIDER_LEDGER`; the outer runner replaces it with a
suite-private path. `MINIMAX_RELAY_LOG` remains supported by direct build-soak use.
Do not print or commit `DISCO_RELIABILITY_SEED_SECRET_KEY`; pass it through the
environment only.

Run three ordinary full waves, stopping immediately if any one fails, then make the fourth the promotion check:

```bash
.venv/bin/python3 -m harness.reliability.run --proof live --parallel-suites auto
.venv/bin/python3 -m harness.reliability.run --proof live --parallel-suites auto
.venv/bin/python3 -m harness.reliability.run --proof live --parallel-suites auto
.venv/bin/python3 -m harness.reliability.run \
  --proof live \
  --parallel-suites auto \
  --require-promotion
```

Four waves are needed because the most expensive deep-search and Agent/MCP suites contribute 25 or 30 trials per wave. Counts accumulate only for the exact same commit plus dirty-tree fingerprint.

## Targets

| Live claim family | Promotion target | Trials per full wave | Minimum clean waves |
| --- | ---: | ---: | ---: |
| Build shape matrix | 300 | 100 | 3 |
| Build revisions and mid-run steer | 100 | 100 | 1 |
| Build pause/close/resume/export/rollback/continue | 100 | 50 | 2 |
| Build and Agent multi-file preview manifest/assets | 4 | 1 | 4 |
| Agent general promised tasks | 300 | 100 | 3 |
| Agent workflow creation/current stack/MCP execution | 100 | 30 | 4 |
| Agent gVisor/noVNC lifecycle | 30 | 10 | 3 |
| Search grounding, grading, citations, follow-ups | 300 | 100 | 3 |
| Deep Search and continuation | 100 | 25 | 4 |
| Search Markdown/PDF export | 50 | 25 | 2 |
| Search audio generation and playback | 30 | 10 | 3 |
| Search through approved MCP retrieval | 30 | 10 | 3 |
| Every Settings family across restart and restore | 3 | 3 | 1 |

The Build API shape and revision matrices are assigned round-robin from `harness/build_soak/scenarios.yaml`. They cover static and multifile projects, planning, verification repair, scripts/dev servers, diagnostic flows, multiple settled revisions, and mid-run steer. The complex browser lane separately proves real preview rendering, stop/pause, app close/reopen, resume, ZIP export, revision planning, live steer, historical preview, rollback, and a successful build after rollback.

## Evidence and verdicts

Campaign output defaults to:

```text
~/.local/state/disco/reliability/
├── campaigns/campaign_.../
│   ├── campaign-summary.json
│   └── <suite-id>/
│       ├── suite.log
│       └── structured test evidence
└── state.json
```

Build-soak suite directories additionally contain one immutable dossier per conversation, including the durable event stream, workspace and preview observations, provider-ledger slice, inspect/model-routing trace, oracle results, classification, manifest, and SHA-256 evidence lock. `batch-summary.json` includes the per-run thrash verdict and trace counts.

Verdicts are intentionally distinct:

- `PASS`: all required structured evidence exists and every trial passed.
- `FAIL`: a product contract failed. The revision is tainted; fix it and rerun from zero on the new fingerprint.
- `INFRA`: the suite could not obtain a verdict, such as a missing service, ledger, browser, or resource-admission timeout. It does not count.
- `INVALID`: evidence was skipped, missing, inconclusive, undersized, or the source changed during execution. It does not count.

The campaign summary contains the cumulative promotion report for all claims selected in that run. Exit code `4` means the current wave passed but cumulative targets required by `--require-promotion` are not complete.

Never edit the source tree during a campaign. Untracked file bytes are part of the revision fingerprint, so even an uncommitted fix starts a new honest streak.

## Thrash detection

Every live Build dossier includes the Agent's inspect trace. `ThrashOracle` fails an otherwise-finished build when it observes:

- the same tool call and arguments repeated beyond the scenario limit;
- the same normalized tool error repeated beyond the limit;
- repeated hidden repair turns before an action is committed, including unknown-tool guesses, empty/prose-only responses, tool-history repair, and provider request rejection/requery cycles;
- an actionless pause;
- a product `STUCK` valve caused by repeated actions, no-ops, verification no-progress, or identical plans.

One hidden repair of a given kind is tolerated; a second identical repair fails. More than three mixed hidden repairs also fails. The thresholds live in each scenario's `assertions.thrash` block. Missing thrash policy is a skip in the low-level backward-compatible classifier, but all promotion scenarios declare the policy and the outer runner rejects skipped evidence.

The Build driver also samples the redacted inspect trace and durable tool events while each run is active. A nonterminal threshold crossing must appear in two consecutive samples (so a just-committed action waiting for its observation is not a false alarm); terminal findings are conclusive immediately. `thrash-monitor.json` is evidence-locked in every dossier and records the sample count, first detection time, event watermark, and oracle facts. Final classification reruns the oracle over the complete trace and remains authoritative.

## Clean-device campaign

Fresh-device promotion requires ten genuinely distinct machines. A human label never counts as identity; the harness hashes stable machine hardware/OS identifiers and the ledger deduplicates them.

Before running this proof:

1. Commit and publish the candidate. Fresh-device proof refuses a dirty coordinator tree.
2. Choose an older base ref and an upgrade ref that resolves exactly to the coordinator's `HEAD` commit.
3. Use a machine with no Disco containers, images, volumes, `~/.local/share/disco`, `~/.config/disco`, or occupied default test ports.
4. Make the same campaign `state.json` available sequentially to all ten machines so distinct results aggregate under one revision.

On each clean machine, from a helper checkout of the exact candidate harness:

```bash
export DISCO_FRESH_REPO_URL='your clone URL'
export DISCO_FRESH_BASE_REF='the pre-upgrade ref'
export DISCO_FRESH_UPGRADE_REF='the exact candidate ref'
export DISCO_FRESH_DRIVER_BASE_URL='https://your OpenAI-compatible endpoint/v1'
export DISCO_FRESH_DRIVER_MODEL='provider/model-id'
export DISCO_FRESH_DRIVER_API_KEY='secret-if-required'

.venv/bin/python3 -m harness.reliability.run \
  --proof fresh_device \
  --state /shared/disco-reliability/state.json \
  --out /shared/disco-reliability/campaigns
```

Use `--require-promotion` on the tenth machine. Run machines sequentially if the shared path does not provide reliable POSIX file locking.

Each device proves pristine preflight, clone, Compose build/install, first pairing, driver configuration, text/tool/Internet grounding, real Build and Deep Search, project/report export, cold restart, an in-place source upgrade, retained projects/settings, a post-upgrade edge build, and complete container/image/volume uninstall. The candidate upgrade commit must equal the commit recorded by the outer campaign.

## Resource tuning

The safe default is the requested 32 GiB desktop floor:

```bash
.venv/bin/python3 -m harness.reliability.run \
  --proof live \
  --parallel-suites auto \
  --memory-reserve-gib 32 \
  --disk-reserve-gib 10
```

`auto` uses CPU count and suite memory weights, then waits for live headroom before admitting work. A numeric `--parallel-suites` is still bounded by memory and disk. The Build runner also supports `--parallel auto` or a number, `--memory-per-worker-gib`, `--disk-per-worker-gib`, and `--cpus-per-worker` for ad hoc batches.

Do not lower the 32 GiB reserve merely to turn an admission failure green. Free resources or wait; resource exhaustion is infrastructure evidence, not product evidence.

## Failure workflow

When a campaign fails:

1. Open `campaign-summary.json` and the failed suite's `suite.log`.
2. For Build, inspect `classification.json`, the failed oracle facts, `inspect-trace.json`, and the frozen event/tool transcript.
3. Fix the product or harness contract that actually failed.
4. Run focused hermetic and one focused live reproduction.
5. Start the full post-fix streak again. The new source fingerprint prevents old passes from being reused.

Do not rerun a flaky failure until it disappears and then count the green attempt. A `FAIL` permanently taints that revision by design.
