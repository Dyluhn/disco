# Deep Research harness

The harness drives Deep Research through the public agent-server wire protocol
and records what an outside observer can see. It can also replay a cassette or
accept an injected JSON frame stream, so report regressions do not require a
live provider to reproduce.

From the repository root:

```bash
.venv/bin/python3 -m harness.deep_research \
  --transport live \
  --base-url http://127.0.0.1:8000 \
  --query "What are the latest innovations in AI?" \
  --depth standard_deep \
  --watch \
  --output-dir /tmp/disco-research-run
```

Replay a recorded run:

```bash
.venv/bin/python3 -m harness.deep_research \
  --transport replay \
  --cassette /tmp/disco-research-run/cassette.jsonl \
  --query "What are the latest innovations in AI?" \
  --output-dir /tmp/disco-research-replay
```

Run the default five-query acceptance corpus (the four benchmark questions
represented by the supplied reference reports plus the latest-innovations
question):

```bash
.venv/bin/python3 -m harness.deep_research \
  --transport live \
  --base-url http://127.0.0.1:8000 \
  --batch \
  --depth exhaustive \
  --watch \
  --output-dir /tmp/disco-deep-research-acceptance
```

The batch command runs with bounded concurrency (default `2`) and writes
isolated `run-01/` through `run-05/` directories. Use `--concurrency N` to
control provider pressure and `--repeat N` to sample the same corpus more than
once.
Each directory keeps an independent `report.md`, `report.json`,
`events.jsonl`, redacted `cassette.jsonl`, `summary.json`, and `summary.md`.
The root contains `batch_summary.json` and `batch_summary.md`; the manifest
records the pass/fail result and artifact paths without collapsing the
internal trace.  To run a different corpus, pass a newline-delimited file:

```bash
.venv/bin/python3 -m harness.deep_research \
  --transport replay --cassette /tmp/run/cassette.jsonl \
  --batch --queries-file development/harness/acceptance-queries.txt \
  --output-dir /tmp/disco-deep-research-acceptance
```

## Replay the writer without researching again

Every finished run saves the writer's whole input — the query, the brief, the
coverage map, the passages in pool order with full text, and the trail — as
`<data dir>/pools/<conversation id>.json` on the server (`DISCO_DATA_DIR`, else
`$XDG_DATA_HOME/disco`, else `~/.local/share/disco`). The run names it in a
`research_pool` action on the event stream, so `events.jsonl` says which pool
belongs to which run.

Point the harness at that file to write the report again on the same evidence —
the research loop is skipped entirely, so the only thing that changed between
two replays is the writer:

```bash
.venv/bin/python3 -m harness.deep_research \
  --transport live \
  --base-url http://127.0.0.1:8000 \
  --surface write_from_pool \
  --pool /path/to/pools/conv_abc123.json \
  --output-dir /tmp/disco-writer-replay
```

`--pool` takes a pool FILE (sent inline, so it need not exist on the server —
a pool captured on one machine replays on another) or the ID of a pool the
server already holds (`--pool conv_abc123`). Depth, recency and the question
come from the saved pool, not from the command line. The run produces the same
artifacts as any other — `report.md`, `events.jsonl`, `model_io.jsonl`,
`inspect.json`, `summary.json` — and its `model_io.jsonl` holds writer stages
only (`report_draft`, `report_review`, `report_rework`), never `research_turn`.

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
- `model_io.jsonl`: optional redacted visible model request/response records.
- `provider_attempts.jsonl`: optional bounded provider lifecycle records from
  `DISCO_INSPECT` (`started`/`success`/`error`, retry scheduling, latency, and
  safe provider/model labels). These rows are separate from `model_io.jsonl`
  and are not counted as search-thrash work.
- `search_io.jsonl`: bounded per-query search I/O records reconstructed from
  the stream. When the backend supplies a structured `search_io`/`retrieval`
  diagnostic, each row includes planned and issued queries, provider outcome or
  error, bounded hit metadata, extraction statuses, passage/admission counts,
  yield reason, round/turn, and latency. Older cassettes still produce an
  action-only row with `provider_outcome: "not_observed"`; the harness does
  not infer that missing responses were empty.
- `search_timeline.md`: compact operator table for the same rows.
- `inspect.json`: optional bounded `DISCO_INSPECT` trace snapshot.
- `thrash.json`: deterministic repeated-query, malformed-turn, and no-progress signals.
- `cassette.jsonl`: the redacted wire-frame seam, ready for deterministic replay.
- `summary.json`: request, telemetry, probes, bounds, errors, report, and checks.
- `summary.md`: a readable pass/fail digest followed by the rendered report.

The command exits nonzero when the provider fails or the artifact violates a
report invariant.  A Deep Research run has exactly three legitimate outcomes:

1. **A complete report** — the summary is non-empty and cited, and every
   section is present and substantive.
2. **A run ERROR** — an `ErrorEvent` plus a terminal `ERROR` status; reserved
   for genuine provider failure.
3. **A user-Stop PAUSED checkpoint** — a typed `kind == "research_checkpoint"`
   event with `sections == []` and `summary == ""`. The checkpoint event kind
   carries the resumable-state contract; it is not a finished report and is
   judged by the `stopped_checkpoint_valid` invariant.

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
- no repeated body sentences or duplicate substantive paragraphs, using the
  product's shared repetition rules.

Word count is recorded, not gated. Depth increases the research budget; the
report should be as long as the evidence and question require. Deterministic
checks establish observable structure, not factual accuracy or completeness;
those require assessment against the cited evidence and requested scope.

The checks are intentionally observable and deterministic; they do not rely on
an LLM judge.  A failed report remains available in the artifacts for
diagnosis, but the process exits nonzero. Secrets in requests and event payloads
are redacted before artifacts are written.

`summary.json` also records advisory `telemetry.quality` metrics: distinct
works, high-specificity claims backed by one work, claim concentration by
work, repeated hedge phrases, and near-duplicate body paragraphs. Batch
summaries aggregate these metrics alongside pass rate, output variance, and
the thrash-clean rate; the advisory signals do not create a second report
outcome or suppress an otherwise valid artifact.

The event stream includes the public state and event frames as well as the
harness's outbound `send_message` control. With `--inspect-model-io` and a
server started with `DISCO_INSPECT=1`, the harness also
fetches the bounded per-conversation inspect trace. This exposes visible
requests, returned text/tool calls, declared decisions, routing, and token
spans for thrash analysis; hidden reasoning is never captured.
`--watch` prints a concise redacted version of that stream while the run is live.

Search diagnostics are intentionally additive. A provider may emit a
structured `search_io` (or `retrieval`) event in the inspect trace or public
stream; it also consumes the current public envelope
`event.tool_result.structured.retrieval_trace.queries[]`. The projection
accepts aliases such as `planned_query`, `issued_query`/`post_transform_query`,
`raw_hits`/`raw_discovered_hit_count`, `extracted`,
`extraction.{attempted,success,statuses}`, `passages`/
`reranked_passage_count`, `admission_count`/`added`, `yield_reason`,
`provider_error`, and `latency_ms`. The adapter ignores free-form thoughts and
model prompts, bounds titles/URLs and hit lists, and strips credentials/secret
query parameters from URLs.
