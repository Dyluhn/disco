# Deep Research: reading, retries, and recovery

The September 2026 fresh-install investigation found provider-control omissions,
missing reading activity, and a checkpoint gap before the first draft. The
fixes share the existing router, progress stream, and research checkpoint owner.

- Reasoning and request extensions are configured explicitly per catalogue model
  in Settings → Models → Advanced model metadata and request options. No URL,
  provider label, or model-name match selects a thinking flag, token allowance,
  tool-call policy, continuation turn, or cache marker. Both Chat Completions and
  Responses apply the same declared reasoning options. Unconfigured options leave
  the endpoint default intact; this is not a claim that reasoning was disabled.
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

## Request options

A model's `request_policy` persists in the shared configuration and is wired to
its actual HTTP requests, including models sharing an endpoint. For example, if
an endpoint documents these fields:

```json
{
  "reasoning_enabled": {"reasoning_effort": "high"},
  "reasoning_disabled": {"reasoning_effort": "none"}
}
```

These values are examples of an explicit configuration, not universal API
capabilities. An endpoint may require a boolean, nested object, a minimum effort,
or have no switch. Supply its documented JSON. `null` means unspecified; `{}`
explicitly leaves that state's behavior unchanged. `default_reasoning` optionally
sets the default intent; request intent overrides it, and assist-repair requests
select the disabled-intent configuration. `body` contains optional extensions
common to both states; reasoning options recursively override those fields.
The policy cannot replace model identity, history, tools, stream mode or token
budgets. Credentials belong in the existing secret store.

`require_user_continuation` and `cache_control` are opt-in compatibility flags.
Serial tool requests can be declared with `body: {"parallel_tool_calls": false}`.
Old automatically selected vendor behavior is removed, without a name-based
migration table. Existing installations relying on it must explicitly configure
the options their endpoint accepts. The prior live probes establish the captured
wire fields worked on those services, not that names should select those fields.


If a request with optional reasoning settings receives a generic HTTP 400/422,
the adapter retries once without the `reasoning_enabled`/`reasoning_disabled`
overlay. It restores the underlying `body` values, retaining configured routing
constraints, prompt, tools, and token budget. No changed wire options means no
extra attempt. Authentication, context-window, filtering, and transient errors
retain their existing typed handling. A second rejection is surfaced normally.
This applies to both completion entry points and the Responses endpoint; the
existing buffered fallback for streaming rejection remains bounded as well.
Cancellation is honored, and delivered output is never replayed by this fallback.

Research activity and final response metadata record
`reasoning_control_fallback: true`; the progress strip says “retrying without
optional reasoning settings,” including after reconnect. This does not mean
reasoning is disabled, nor prove the rejected field caused the first error.
Successful acceptance alone cannot prove an endpoint honored a setting. The
fallback is local to the call, with no model-name rules or permanent capability
cache. Fixed `body` options are preserved because dropping arbitrary routing or
privacy constraints would change the operator's request.


## Final review recovery

New Quick runs allow four review decisions, including malformed and empty
responses. Two decisions are reserved for checking the revised report, so an
empty final check can recover once. Standard and Deep retain their four/five
decision limits and now reserve two for final verification as well. Old
checkpoints keep their recorded allowance; resume never replenishes calls.

After an empty visible response, the reviewer receives a concise-output
instruction with the same draft, evidence, rubric, and claim requirements.
Blank assistant messages are not replayed. This strategy is checkpointed and
carried into post-repair verification. Visible malformed replies retain their
protocol feedback. An empty response cut off at the output limit provisions
the remaining retry at the existing 48,000-token hard cap; it no longer adds
only a small increment to a capacity that produced no decision. Initial
capacity, the shared hard cap, and the single prose-repair limit remain unchanged. Exhausted or invalid reviews remain visibly unavailable or
incomplete; they cannot certify a report.

The final-check stage now reports real streamed activity and buffered-call
starts through the same review progress events as the initial review. It
uses the stage declared by the request, with no inferred activity or
model/provider-specific selection.
