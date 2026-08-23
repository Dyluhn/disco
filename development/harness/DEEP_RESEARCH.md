# Deep Research harness

The harness drives Deep Research through the public agent-server wire protocol
and records what an outside observer can see. It can also replay a cassette or
accept an injected JSON frame stream, so report regressions do not require a
live provider to reproduce.

From the repository root:

```bash
PYTHONPATH=development .venv/bin/python3 -m harness.deep_research \
  --transport live \
  --base-url http://127.0.0.1:8000 \
  --query "What are the latest innovations in AI?" \
  --depth standard_deep \
  --watch \
  --output-dir /tmp/disco-research-run
```

Replay a recorded run:

```bash
PYTHONPATH=development .venv/bin/python3 -m harness.deep_research \
  --transport replay \
  --cassette /tmp/disco-research-run/cassette.jsonl \
  --query "What are the latest innovations in AI?" \
  --output-dir /tmp/disco-research-replay
```

Run the default five-query acceptance corpus (the four benchmark questions
represented by the supplied reference reports plus the latest-innovations
question):

```bash
PYTHONPATH=development .venv/bin/python3 -m harness.deep_research \
  --transport live \
  --base-url http://127.0.0.1:8000 \
  --batch \
  --depth exhaustive \
  --watch \
  --output-dir /tmp/disco-deep-research-acceptance
```

The batch command runs sequentially and writes `run-01/` through `run-05/`.
Each directory keeps an independent `report.md`, `report.json`,
`events.jsonl`, redacted `cassette.jsonl`, `summary.json`, and `summary.md`.
The root contains `batch_summary.json` and `batch_summary.md`; the manifest
records the pass/fail result and artifact paths without collapsing the
internal trace.  To run a different corpus, pass a newline-delimited file:

```bash
PYTHONPATH=development .venv/bin/python3 -m harness.deep_research \
  --transport replay --cassette /tmp/run/cassette.jsonl \
  --batch --queries-file development/harness/acceptance-queries.txt \
  --output-dir /tmp/disco-deep-research-acceptance
```

Inject a JSON array of WebSocket frames with `--transport fake --frames
frames.json`. Common controls are `--depth`, `--recency`, `--model`,
`--provider`, `--surface`, and `--timeout`. The timeout defaults to the
requested depth's wall-clock budget — `quick` 600 s, `standard_deep` 1,500 s,
`exhaustive` 3,000 s — sized above the product tier budgets plus report-writing
time; `--timeout` overrides it explicitly. Live runs establish the same paired
cookie/CSRF session as the browser; Deep Research is gateless (v2) — sending
the question launches the run and the model's brief streams as its first
output. For a non-loopback server, pass its operator pairing token
with `--auth-token` or `DISCO_PAIRING_TOKEN`; it is never written to an artifact.

Each run writes:

- `report.md` and `report.json`: the normalized final artifact.
- `events.jsonl`: ordered wire events with elapsed time and classified phase.
- `cassette.jsonl`: the redacted wire-frame seam, ready for deterministic replay.
- `summary.json`: request, telemetry, probes, bounds, errors, report, and checks.
- `summary.md`: a readable pass/fail digest followed by the rendered report.

The command exits nonzero when the provider fails or the artifact violates a
report invariant.  A Deep Research run has exactly three legitimate outcomes:

1. **A complete report** — the summary is non-empty and cited, and every
   section is present and substantive. (A budget-exhausted run with an empty
   evidence pool finishes with the system-gated honest dead-end account —
   still a FINISHED report, never an error.)
2. **A run ERROR** — an `ErrorEvent` plus a terminal `ERROR` status; reserved
   for genuine provider failure.
3. **A user-Stop PAUSED checkpoint** — `bounded_by == "stopped"` with
   `sections == []` and `summary == ""`.  The harness judges this shape as a
   checkpoint: the empty summary and sections are allowed **only** when
   `bounded_by == "stopped"` (the `stopped_checkpoint_valid` invariant).

The harness judges the report artifact **as-is** and never normalizes or
manufactures structure before judging: a missing summary stays empty, missing
sections stay `[]`, and a legacy cassette that recorded no report structure is
projected without fabricated sections, summaries, or citations (its report is
tagged `legacy_replay`).  Anything else would repair the very violations the
invariants exist to detect.  The deterministic quality gates require:

- an executive summary that is substantive and free of diagnostic/research-
  process language;
- non-empty section bodies with enough prose to be substantive (a punctuation-
  only, placeholder, or thin section fails);
- a valid Markdown heading hierarchy, with no skipped levels;
- citations on every substantive section that resolve to passages with HTTP(S)
  source URLs;
- a report word count inside the requested depth tier (`quick` 1,500–2,500,
  `standard_deep` 4,000–7,000, `exhaustive` 8,000–12,000); and
- no high-rate repeated sentences or duplicate paragraphs.

The checks are intentionally observable and deterministic; they do not rely on
an LLM judge.  A failed report remains available in the artifacts for
diagnosis, but the process exits nonzero. Secrets in requests and event payloads
are redacted before artifacts are written.

The event stream includes the public state and event frames as well as the
harness's outbound `send_message` control. This makes the brief, search,
extraction, verification, writing, terminal state, bounds, and provider
errors inspectable without importing or patching product internals.
`--watch` prints a concise redacted version of that stream while the run is live.
