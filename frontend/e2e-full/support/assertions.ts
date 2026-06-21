/**
 * assertions — four-truth helpers for the Disco evidence harness.
 *
 * These helpers fetch the app's own truth endpoints and assert on the results.
 * They are the "wire / event-log / output truth" layers of the FOUR-truths model;
 * UI truth is handled by Playwright's native expect() in the spec files.
 *
 *   expectEventSequence   — event-log truth: expected event types appear in order
 *   expectInspectTrace    — router/span truth: DISCO_INSPECT trace meets minimums
 *   expectArtifactReadable — output truth: an artifact path returns a non-empty body
 *
 * All functions throw (with a descriptive message) on assertion failure so they
 * integrate naturally into Playwright test bodies alongside `expect()`.
 *
 * evidence-harness-campaign.md W12
 */

// ---------------------------------------------------------------------------
// Internal types (shapes returned by the app's endpoints)
// ---------------------------------------------------------------------------

interface EventRow {
  /** The real discriminator field emitted by the agent-server (events.py §kind). */
  kind?: string;
  /** Legacy fallback names — kept for forward-compat with any future shape changes. */
  event_type?: string;
  type?: string;
  [key: string]: unknown;
}

interface InspectTrace {
  routing_decisions?: unknown[];
  /** Spans emitted by obs.log_span — each record carries `span` (the name), not `name`. */
  spans?: Array<{ span?: string; [key: string]: unknown }>;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function stripTrailingSlash(url: string): string {
  return url.replace(/\/$/, "");
}

/**
 * Fetch `url` with optional `headers`.  Returns the parsed JSON body.
 * Throws a descriptive error if the response is not 2xx.
 */
async function fetchJson<T = unknown>(
  url: string,
  headers: Record<string, string> = {},
): Promise<T> {
  const res = await fetch(url, { headers });
  if (!res.ok) {
    throw new Error(
      `assertFetch: ${url} returned HTTP ${res.status} ${res.statusText}`,
    );
  }
  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Assert that the conversation event log contains all `expected` event types,
 * appearing in the given order (subsequence, not contiguous).
 *
 * Fetches  GET /conversations/{cid}/events  from `agentBaseUrl`.
 * The endpoint may return newline-delimited JSON or a JSON array; both are handled.
 *
 * @param cid           Conversation / request ID.
 * @param agentBaseUrl  Agent-server root, e.g. "http://127.0.0.1:8000".
 * @param expected      Ordered list of `kind` values that must appear in sequence
 *                      (e.g. `["status","action","observation","status"]`).
 *                      These are the `EventKind` discriminator values serialized
 *                      by the agent-server — see events.py `EventKind` enum.
 * @throws {Error}      If any expected type is missing or out of order.
 */
export async function expectEventSequence(
  cid: string,
  agentBaseUrl: string,
  expected: string[],
): Promise<void> {
  if (expected.length === 0) return;

  const base = stripTrailingSlash(agentBaseUrl);
  const baseUrl = `${base}/conversations/${cid}/events`;

  // GET /conversations/{cid}/events returns a JSON object:
  //   { events: EventRow[], next_cursor: number | null }
  // Collect all pages via next_cursor pagination.
  const allRows: EventRow[] = [];
  let nextCursor: number | null = null;
  let isFirstPage = true;
  do {
    const pageUrl: string = isFirstPage
      ? baseUrl
      : `${baseUrl}?after_seq=${nextCursor as number}`;
    isFirstPage = false;
    type EventPage = { events?: EventRow[]; next_cursor?: number | null };
    const page: EventPage = await fetchJson<EventPage>(pageUrl);
    const events = Array.isArray(page.events) ? page.events : [];
    allRows.push(...events);
    nextCursor =
      typeof page.next_cursor === "number" ? page.next_cursor : null;
  } while (nextCursor !== null);

  // The real discriminator is `kind` (events.py — serialized by pydantic model_dump).
  // `event_type` / `type` are fallback aliases kept for forward-compat only.
  const eventTypes = allRows.map(
    (r) => (r["kind"] ?? r["event_type"] ?? r["type"] ?? "") as string,
  );

  let cursor = 0;
  const missing: string[] = [];
  for (const want of expected) {
    let found = false;
    while (cursor < eventTypes.length) {
      if (eventTypes[cursor] === want) {
        cursor++;
        found = true;
        break;
      }
      cursor++;
    }
    if (!found) missing.push(want);
  }

  if (missing.length > 0) {
    throw new Error(
      `expectEventSequence [cid=${cid}]: missing event type(s) in sequence: ${missing.join(", ")}\n` +
        `Observed types (${eventTypes.length}): ${eventTypes.join(", ")}`,
    );
  }
}

// ---------------------------------------------------------------------------

/** Options for {@link expectInspectTrace}. */
export interface InspectTraceOpts {
  /**
   * Minimum number of routing decisions the trace must contain.
   * Defaults to 1.  Set to 0 to skip this check.
   */
  minRoutingDecisions?: number;
  /**
   * Strings that must appear somewhere in the serialized span names.
   * Each string is tested with `includes()` against every `span.name`.
   */
  spansInclude?: string[];
}

/**
 * Assert that the DISCO_INSPECT router/span trace for `cid` meets minimum
 * quality thresholds.
 *
 * Fetches  GET /api/debug/trace/{cid}  from `agentBaseUrl`.
 * This endpoint is only populated when the server runs with DISCO_INSPECT=1;
 * a 404 is treated as an assertion failure (not silently skipped) so that
 * the harness catches misconfigured environments.
 *
 * @param cid              Conversation / request ID.
 * @param agentBaseUrl     Agent-server root.
 * @param opts             Thresholds; see {@link InspectTraceOpts}.
 * @throws {Error}         If the trace doesn't meet the specified thresholds.
 */
export async function expectInspectTrace(
  cid: string,
  agentBaseUrl: string,
  opts: InspectTraceOpts = {},
): Promise<void> {
  const base = stripTrailingSlash(agentBaseUrl);
  const url = `${base}/api/debug/trace/${cid}`;
  const trace = await fetchJson<InspectTrace>(url);

  const minDecisions = opts.minRoutingDecisions ?? 1;

  // Routing decisions check
  if (minDecisions > 0) {
    const decisions = trace["routing_decisions"] ?? [];
    const count = Array.isArray(decisions) ? decisions.length : 0;
    if (count < minDecisions) {
      throw new Error(
        `expectInspectTrace [cid=${cid}]: expected >= ${minDecisions} routing decision(s), got ${count}`,
      );
    }
  }

  // Span inclusion check
  if (opts.spansInclude !== undefined && opts.spansInclude.length > 0) {
    const spans = trace["spans"];
    const spanNames: string[] = Array.isArray(spans)
      ? spans.map((s) => {
          // obs.log_span emits {"span": name, "event": "start"|"end", ...}
          // The field is `span`, NOT `name`.
          const sp = s as { span?: unknown };
          return typeof sp.span === "string" ? sp.span : "";
        })
      : [];

    const missing: string[] = [];
    for (const want of opts.spansInclude) {
      if (!spanNames.some((n) => n.includes(want))) {
        missing.push(want);
      }
    }
    if (missing.length > 0) {
      throw new Error(
        `expectInspectTrace [cid=${cid}]: span(s) not found: ${missing.join(", ")}\n` +
          `Observed spans: ${spanNames.join(", ")}`,
      );
    }
  }
}

// ---------------------------------------------------------------------------

/**
 * Assert that an artifact at `artifactPath` inside the conversation workspace
 * is readable — i.e. the server returns a non-empty 2xx response.
 *
 * Fetches  GET /workspace/{artifactPath}  from `agentBaseUrl`.
 * (Adjust the URL template to match whatever the app actually exposes.)
 *
 * @param cid            Conversation / request ID (used in error messages).
 * @param agentBaseUrl   Agent-server root.
 * @param artifactPath   Workspace-relative path, e.g. "report.pdf".
 * @throws {Error}       If the artifact is missing or empty.
 */
export async function expectArtifactReadable(
  cid: string,
  agentBaseUrl: string,
  artifactPath: string,
): Promise<void> {
  const base = stripTrailingSlash(agentBaseUrl);
  // Real endpoint: GET /conversations/{cid}/artifacts/{path}
  // (files.py:222 — jailed to declared artifacts, extension-allowlisted).
  const artifactUrl = `${base}/conversations/${cid}/artifacts/${artifactPath}`;
  // Evidence bundle for richer diagnostics when DISCO_INSPECT=1.
  const evidenceUrl = `${base}/api/debug/evidence/${cid}`;

  const res = await fetch(artifactUrl);
  if (!res.ok) {
    throw new Error(
      `expectArtifactReadable [cid=${cid}]: artifact "${artifactPath}" not readable — ` +
        `GET ${artifactUrl} returned HTTP ${res.status}\n` +
        `(evidence bundle at ${evidenceUrl} may contain more detail)`,
    );
  }

  // Check the response has a body (Content-Length > 0 or readable stream)
  const text = await res.text();
  if (text.trim().length === 0) {
    throw new Error(
      `expectArtifactReadable [cid=${cid}]: artifact "${artifactPath}" returned an empty body`,
    );
  }
}
