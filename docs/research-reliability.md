# Deep Research: reading, retries, and recovery

The September 2026 fresh-install investigation found provider-control omissions,
missing reading activity, and a checkpoint gap before the first draft. The
fixes share the existing router, progress stream, and research checkpoint owner.

- Ollama DeepSeek v4.1 Flash forwards explicit non-thinking intent as
  `reasoning_effort: none`. GLM 5.3/5.3 Flash requests `low`, reflecting their
  lightweight reasoning policy. Other providers keep their existing mappings.
  These are request controls, not a guarantee about a provider's internal work.
- Reading calls publish waiting and actual output activity, with source and
  part identity. Retry backoff is a separate state, visible with inspect off.
  The frontend retains those facts when replaying a conversation after reconnect.
- The stream idle deadline advances on content, reasoning, tool arguments, or
  completion markers. Empty lines, keepalives, role-only frames, and usage-only
  frames cannot keep a silent generation alive. Slow, productive local models
  retain progress-based timeouts. Buffered JSON has a bounded wait.
- Transient HTTP errors retain `Retry-After` (seconds or HTTP date), bounded at
  five minutes. Both router entry points use it and keep backoff cancellable.
  Explicit structured hard-quota codes are terminal; an unclassified 429 still
  receives normal transient retries. The original CommandCode response body
  was unavailable, so its precise quota/rate-limit cause remains unknown.
- The reader saves charged calls and completed chunks through the existing
  durable checkpoint/event owner, before a draft exists. Resume reuses matching
  source hashes and starts at the next unfinished chunk. Interrupted calls do
  not reset the allowance. Older checkpoints still load; a saved draft/review
  is never replaced by an earlier reading boundary.
- Source coverage and output capacities are retained. Provider-reported usage
  and measured latency are recorded with the notes. Reading remains sequential;
  the fix does not add pressure to rate-limited endpoints or lower evidence
  capacity to obtain a faster result.

The captured Ollama transport fixtures are in
`packages/core/tests/fixtures/ollama-20260922`. The live reading check used two
retained sources from the affected session: DeepSeek completed in 40.99 seconds
with 76 accepted findings; GLM completed in 30.27 seconds with 64. All four reads
completed, with validated source offsets, activity events, and reading checkpoints.
This is a small live check, not a general latency benchmark or proof against
future provider outages. The private session data and full test logs remain in
the workstation evidence directory, outside the repository.

Focused regression coverage includes raw SSE stalls and productive controls,
header propagation and both router retry paths, cancellation during backoff,
source-change invalidation, partial reading resume, durable checkpoint round-trip,
legacy writer recovery, frontend event replay, and the Compose preflight. Firefox
renders the actual progress component from captured live reading events.

For installation, use `deploy/compose/disco-compose` with rootless Podman. It
selects Compose v2 or later and validates the file before startup. On Ubuntu
24.04 install `docker-compose-v2`; the old `docker-compose` v1 package cannot
parse the project's Compose file. The wrapper also rejects a stale explicit
provider override before any container starts.
