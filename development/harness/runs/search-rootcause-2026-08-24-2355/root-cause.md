# Deep Research reproduction and root cause

## Reproduction

Four live `standard_deep` reports were run through the public research protocol:
two repetitions each of the Late Bronze Age Collapse and 2026 solid-state-battery
questions. The batch allowed three simultaneous reports. Every search, bounded
result set, extraction outcome, visible model request/response, report, and inspect
snapshot was captured. The corrected cassette replay passes all four reports.

| Run | Search context | Queries | Degraded | Clean zero | Raw hits | Extraction OK / failed | Words | Sources | Search thrash | Draft latency |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| 01 | concurrent wave | 51 | 26 | 3 | 153 | 92 / 40 | 4,397 | 56 | no-progress 3 | 121,633 ms |
| 02 | concurrent wave | 50 | 23 | 4 | 155 | 86 / 39 | 5,668 | 61 | no-progress 3 | 114,535 ms |
| 03 | concurrent wave | 63 | 27 | 8 | 195 | 115 / 47 | 4,518 | 63 | no-progress 4 | 100,698 ms |
| 04 | mostly single-report | 42 | 4 | 2 | 250 | 132 / 63 | 4,994 | 57 | none | 678,856 ms |

`Degraded` means SearXNG returned HTTP 200 and zero hits while naming Yahoo as an
unresponsive or suspended engine. `Clean zero` means no raw hits and no recorded
provider degradation.

## Proven search root cause

The dominant no-progress streaks were provider outages misclassified as query
misses.

- The first concurrent wave had 76 degraded searches out of 164 (46.3%), compared
  with only 15 candidate clean misses (9.1%).
- Failures were synchronized: whole three-query rounds across different reports
  returned zero together, followed by whole rounds recovering together. That
  pattern does not follow query meaning.
- All affected responses identified Yahoo as unresponsive or suspended. A direct
  healthy check showed that Disco's default SearXNG response contained results only
  from Yahoo.
- The agent received `yield_reason: no_hits` for these degraded responses and was
  instructed to pivot the query. It therefore spent later turns reformulating a
  provider outage instead of waiting or retrying the provider.
- The model did not repeat exact queries. In the first wave, 160 of 164 planned
  queries were rewritten before issue, and the thrash detector found unique-query
  work rather than literal loops.
- Under mostly single-report load, degradation fell to 4 of 42 searches (9.5%).
  Run 04 found 250 raw hits with 21 fewer queries than run 03 and had no search
  thrash.

A separate nine-request burst succeeded while Yahoo was healthy. The failure is
therefore intermittent under sustained batch activity/upstream engine state, not a
deterministic nine-request ceiling.

## Secondary extraction cause

Primary and specialist sources fail extraction more often than accessible blogs and
vendor pages. Across the first three runs, 293 of 419 extraction attempts succeeded
and 126 failed. Concentrated failures included ScienceDirect, ResearchGate,
Springer, Cambridge, JSTOR, Nature, Argonne, and government/PDF sources.

Direct reproduction showed:

- IEA and Argonne pages/PDFs can return a Crawl4AI HTTP 500 whose response says a
  Cloudflare JavaScript challenge blocked the crawl.
- A ScienceDirect URL returned HTTP 307; the extraction result was rejected rather
  than followed.
- Toyota's primary newsroom page and an accessible secondary analysis extracted
  successfully when called directly.
- One mixed `extract_many` request returned a request-level HTTP 500, causing the
  current adapter to mark every URL in that batch failed. Later repetitions returned
  per-URL partial success, so this batch-wide failure is real but intermittent.

This creates a source-quality selection effect: authoritative sources disappear
from the evidence pool while easier-to-crawl secondary pages remain. The reports
can pass citation resolution while supporting field-wide or precise technical
claims with weak sources.

## Separate writer stall

Run 04's report draft took 678,856 ms; the other drafts took about 101-122 seconds.
The provider timeout is 180 seconds and the router may retry a transient failure five
times without previously emitting per-attempt evidence. The latency is strongly
consistent with three 180-second timeouts followed by a successful fourth call, but
the old trace cannot prove that retrospective inference. It proves only that one
writer operation was silent for 678,856 ms and eventually succeeded.

Provider-attempt start/end events are now captured separately from visible model
I/O, including call ordinal, latency, safe error class, retry scheduling,
cancellation, and incomplete streams. Timeout cleanup fetches the inspect snapshot,
so an in-flight `started` attempt is retained even when the outer harness times out.

## Harness defect found during reproduction

The original live batch was falsely reported as failed because the harness searched
recursively for any nested `status: error`; an individual URL extraction error was
mistaken for terminal run status. The classifier now accepts only actual top-level
error/lifecycle frames. Replaying the untouched cassettes after that fix passes all
four reports and all report invariants.

## Behavior fixes recommended next

1. Treat a zero-result response with provider degradation as provider trouble:
   retry the same query with bounded backoff/concurrency control, then use a tested
   alternate engine. Do not tell the model it was a semantic no-hit.
2. Isolate extraction failures per URL. Split a request-level batch failure and add
   a safe primary-source/PDF/redirect fallback before discarding the source.
3. Gate load-bearing claims on source authority and citation entailment, not merely
   citation resolution. Preserve upstream sources over pages summarizing them.
4. Use the new provider-attempt trace to measure the writer before changing timeout
   or retry policy; distinguish a genuinely slow response from repeated transient
   attempts.

