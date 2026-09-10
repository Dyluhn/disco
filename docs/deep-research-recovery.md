# Deep Research recovery and evidence checking

This document describes the current implementation and its operating limits. It
does not certify a model, a completion-time promise, or report quality. The event
log remains the authority for conversation state.

## Recovery

New research executions save their state before a model turn, after a completed
turn, and before writing. A saved boundary includes the original task, accepted
steering, evidence identities and full text, coverage, pending query retries and
attempt counts, consumed source and turn budgets, and the loop cursor. Resume
preserves the original depth bounds; it does not grant a new research budget.

The Agent server writes content-addressed files below
`$DISCO_DATA_DIR/research-recovery/<conversation_id>/`:

- `passages/` contains complete evidence bodies, shared between boundaries.
- `checkpoints/` contains versioned state referring to those bodies by hash.

Files are written using same-directory temporary files, file and directory
`fsync`, and atomic replacement. A file becomes a recoverable boundary only when
its reference is appended to the conversation event log. An uncommitted file is
never selected simply because it exists or has a recent timestamp.

On server startup, an interrupted research conversation becomes paused with a
recovered checkpoint. The normal Resume control continues research from that
boundary. An interrupted writer restarts writing from the saved evidence; it
does not repeat the completed research. Draft tokens and an unfinished reviewer
or revision pass are not resumable artifacts. The final report and FINISHED
status are committed in one SQLite transaction.

Recovery verifies the version, conversation/run identity, content hashes, and
budget invariants. If the latest committed checkpoint is damaged, startup tries
an earlier committed boundary in that execution and discloses the fallback.
Work after that earlier boundary may repeat. If no committed boundary can be
read safely, the conversation becomes ERROR with an explicit recovery message;
the original files are retained for diagnosis. A legacy run whose report was
already committed before its FINISHED status is completed without writing a
second report.

An in-flight external request can repeat after a crash. Process-local search
cooldowns and elapsed starvation clocks reset on process restart; saved query
retry attempts and research budgets do not. Legacy Stop checkpoints created
before durable state was introduced lack the original counters and continue
through the existing evidence-carrying compatibility path.

## Storage and backup

Keep the Agent server's data directory persistent across process/container
restarts. A backup intended to support Resume needs both the conversation SQLite
database and its `research-recovery/` directory from the same data store. Stop
writes or use a consistent snapshot when backing up. The separate `pools/`
directory holds full writer inputs for development replays. Report Markdown or
conversation exports alone are not a replacement for these recovery files.

Full evidence can contain uploaded or private material; apply the same access
controls and retention policy as the conversation database. Content addressing
deduplicates bodies within a conversation, not across owners. Recovery does not
depend on a remote object store or a second mutable run-status database.

The normal History deletion path uses the Agent server when connected, so the
runtime cancels and drains active work before deleting its database rows and
research files. Cancellation also drains an in-flight checkpoint writer thread,
preventing it from recreating evidence after deletion. The App-server library
route forwards the authenticated deletion to the Agent server before removing
data. If the Agent cannot confirm deletion, the route returns a retryable error
rather than claiming success. A library-only source install without
`DISCO_AGENT_BASE` can delete inactive conversations locally; active conversations
require the runtime owner. Other conversations' files are preserved.

## Local evidence checker

The local checker now runs three-way natural-language inference: entailment,
contradiction, or neutral. Relevance scores are not used as entailment scores.
The implementation uses the existing ONNX CPU runtime with two inference threads
and a 512-token pair limit. It checks bounded excerpts from cited evidence; it
does not establish independent factual truth or whole-document agreement.

Pinned assets:

| Setting | Value |
| --- | --- |
| Repository | `cross-encoder/nli-deberta-v3-small` |
| Revision | `fa2804872c3b4bd748f38c0185cc85775361e735` |
| ONNX source file | `onnx/model_quint8_avx2.onnx` |
| ONNX SHA-256 | `03c2221313dc0c3eac9cec1f746d1319d33f2c2901fcce1c0f08f4daac9b6dae` |
| Other files | `config.json`, `tokenizer.json` |
| Total provisioned size | Approximately 173 MiB |

The Compose server image includes these three checksum-verified assets at
`/opt/disco-cache/nli` and sets `DISCO_NLI_MODEL_DIR` to that location. Its first
research request does not need to download the classifier.

For a source install, place the three files in a persistent model directory,
with the ONNX file named `model_quint8_avx2.onnx` beside the JSON files. Set
`DISCO_NLI_MODEL_DIR` to that directory in the Agent server environment. Without
that setting, the loader retrieves the pinned files through Hugging Face Hub on
first use. Provision them before offering offline research. Failed loading or
inference must remain visible as unavailable checks.

This English NLI model is a diagnostic aid. Its estimates are not calibrated
probabilities of truth. The report distinguishes supported, possible
contradiction, unresolved, and unavailable checks, and keeps missing evidence and
conflicting citations visible. A source with another URL is not necessarily an
independent source; mirror copies must not be treated as corroboration by a
quality evaluator.

## Validation and remaining acceptance

Deterministic tests cover serialization, budget and steering preservation,
corruption, uncommitted files, atomic replacement, and duplicate-report
prevention. Actual process-kill trials cover research, writing, and the final
SQLite transaction. They use controlled providers to make the interruption
boundary reproducible. A real Firefox Resume trial verifies the native user
path. These establish mechanics, not live-model answer quality.

Release acceptance still requires a frozen candidate and declared model/depth
profiles, fresh questions with all original attempts retained, human assessment
of report usefulness and evidence support, and observed completion times. A
successful hosted Meta run does not certify a local model or another depth tier.
