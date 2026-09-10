/**
 * Evidence/correlation schema — TypeScript mirror of packages/core/src/disco/core/evidence/schema.py
 *
 * Keep in sync with the Python source of truth.  This file defines data shapes
 * only; no HTTP/WS wiring lives here.
 *
 * evidence-harness-campaign.md W4
 */

// ---------------------------------------------------------------------------
// Schema version
// ---------------------------------------------------------------------------

export const SCHEMA_VERSION = 1 as const;

// ---------------------------------------------------------------------------
// Error taxonomy
// ---------------------------------------------------------------------------

/** Structured classification of harness-observed failures. */
export enum ErrorTaxonomy {
  SANDBOX_MISSING_FILE = "SANDBOX_MISSING_FILE",
  SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE",
  TOOL_ERROR = "TOOL_ERROR",
  PROVIDER_AUTH = "PROVIDER_AUTH",
  PROVIDER_BUDGET = "PROVIDER_BUDGET",
  TIMEOUT = "TIMEOUT",
  UNCAUGHT_TASK_EXCEPTION = "UNCAUGHT_TASK_EXCEPTION",
  USER_CANCELLED = "USER_CANCELLED",
  VALIDATION_ERROR = "VALIDATION_ERROR",
  UNKNOWN = "UNKNOWN",
}

// ---------------------------------------------------------------------------
// Canonical evidence record
// ---------------------------------------------------------------------------

/**
 * Canonical correlation record for one harness-instrumented interaction.
 * Mirrors {@link EvidenceRecord} in schema.py exactly.
 */
export interface EvidenceRecord {
  schema_version: number;
  harness_run_id: string;
  trace_id: string | null;
  span_id: string | null;
  request_id: string | null;
  conversation_id: string | null;
  ui_action_id: string | null;
  artifact_ids: string[];
}

// ---------------------------------------------------------------------------
// Redaction
// ---------------------------------------------------------------------------

/**
 * Regex fragments matched (case-insensitively) against object key names.
 * Mirrors REDACTION_KEY_PATTERNS in schema.py.
 */
export const redactionKeyPatterns: string[] = [
  String.raw`api[_\-]?key`,
  String.raw`secret`,
  String.raw`token`,
  String.raw`password`,
  String.raw`auth(?:orization)?`,
  String.raw`cookie`,
  String.raw`bearer`,
];

const _compiledPatterns: RegExp[] = redactionKeyPatterns.map(
  (p) => new RegExp(p, "i"),
);

const _REDACTED = "***REDACTED***";

// ---------------------------------------------------------------------------
// String-content secret scrubber
// ---------------------------------------------------------------------------

/**
 * (compiled_pattern, replacement_template) pairs applied left-to-right to
 * every string value encountered during redact() traversal, independent of
 * the key that holds the string.
 *
 * Mirrors _CONTENT_SCRUB_RULES in schema.py.  Templates may use ``$1``
 * back-references to preserve non-secret prefix material.
 */
const _CONTENT_SCRUB_RULES: Array<[RegExp, string]> = [
  // 1. OpenAI / Anthropic / generic sk- prefixed keys (sk-proj-, sk-ant-, …).
  //    The ``sk-`` prefix is an unambiguous secret signal; no context needed.
  [new RegExp(String.raw`sk-[A-Za-z0-9_\-]{20,}`, "g"), _REDACTED],
  // 2. AWS IAM access key IDs: AKIA + exactly 16 uppercase alphanum chars.
  [new RegExp(String.raw`AKIA[0-9A-Z]{16}`, "g"), _REDACTED],
  // 3. Bearer token: "Bearer <token>" (any capitalisation).
  //    Matches Authorization-header values and JSON body bearer fields.
  [
    new RegExp(String.raw`Bearer\s+[A-Za-z0-9\-._~+/=]{10,}`, "gi"),
    _REDACTED,
  ],
  // 4. Authorization header with a direct long token (≥20 chars avoids
  //    falsely matching the short scheme word "Bearer" itself).
  [
    new RegExp(
      String.raw`Authorization\s*:\s*[A-Za-z0-9\-._~+/=]{20,}`,
      "gi",
    ),
    _REDACTED,
  ],
  // 5. Contextual blobs: a secret-marker keyword immediately before (≤5
  //    separator chars of whitespace, colon, or equals) a long url-safe value
  //    (≥20 chars).  The marker + separator are kept (captured in group 1);
  //    only the value is replaced.  This is deliberately conservative: the
  //    marker must be syntactically adjacent — a keyword elsewhere in the
  //    sentence does NOT trigger redaction.
  [
    new RegExp(
      String.raw`((?:api[_\-]?key|secret|token|password|bearer|authorization)[\s:=]{1,5})[A-Za-z0-9+/=_\-]{20,}`,
      "gi",
    ),
    `$1${_REDACTED}`,
  ],
];

/**
 * Replace secret-looking substrings in {@link s} with {@link _REDACTED}.
 *
 * Applied to every string value encountered during {@link redact} traversal,
 * independent of the key that holds the string.  Mirrors
 * ``_scrub_string_content()`` in schema.py.
 */
function _scrubStringContent(s: string): string {
  let result = s;
  for (const [pat, repl] of _CONTENT_SCRUB_RULES) {
    pat.lastIndex = 0; // reset before each apply — global regex is stateful
    result = result.replace(pat, repl);
  }
  return result;
}

function _keyIsSensitive(key: string): boolean {
  return _compiledPatterns.some((pat) => pat.test(key));
}

/**
 * Deep-traverse {@link obj} and replace sensitive-key values with
 * {@link _REDACTED}.
 *
 * Semantics are identical to the Python ``redact()`` function:
 * - Dict keys matched case-insensitively against {@link redactionKeyPatterns}
 *   have their entire value replaced (no nested structure preserved).
 * - Arrays are traversed element-by-element.
 * - Strings are scanned for embedded secrets by {@link _scrubStringContent}
 *   regardless of the key that contains them.
 * - Other scalars (number, boolean, null) are returned as-is.
 */
export function redact(obj: unknown): unknown {
  if (Array.isArray(obj)) {
    return obj.map(redact);
  }
  if (obj !== null && typeof obj === "object") {
    const result: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
      result[k] = _keyIsSensitive(k) ? _REDACTED : redact(v);
    }
    return result;
  }
  if (typeof obj === "string") {
    return _scrubStringContent(obj);
  }
  return obj;
}

// ---------------------------------------------------------------------------
// W3C Trace Context helpers
// ---------------------------------------------------------------------------

const _HEX32 = /^[0-9a-f]{32}$/;
const _HEX16 = /^[0-9a-f]{16}$/;

/**
 * Produce a W3C traceparent header value: ``00-<32hex>-<16hex>-01``.
 */
export function makeTraceparent(traceId: string, spanId: string): string {
  return `00-${traceId}-${spanId}-01`;
}

/**
 * Parse a W3C traceparent string into ``[traceId, spanId]``.
 * Returns ``null`` if {@link s} is not a valid traceparent.
 */
export function parseTraceparent(
  s: string,
): [string, string] | null {
  const parts = s.trim().split("-");
  if (parts.length !== 4) return null;
  const [version, traceId, spanId] = parts;
  if (version !== "00") return null;
  if (!_HEX32.test(traceId)) return null;
  if (!_HEX16.test(spanId)) return null;
  return [traceId, spanId];
}
