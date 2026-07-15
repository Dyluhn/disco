"""Evidence/correlation schema — evidence-harness-campaign.md W4.

Canonical data shapes for the harness. No HTTP/WS wiring here; that is a
later work item. This module is the single Python source of truth for:

  - The ``EvidenceRecord`` model stamped into event ``meta`` and harness logs.
  - The ``ErrorTaxonomy`` enum for structured failure classification.
  - ``REDACTION_KEY_PATTERNS`` + ``redact()`` for sanitising serialized bundles.
  - W3C Trace Context helpers: ``make_traceparent`` / ``parse_traceparent``.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Bump when any field is removed or its semantics change in a breaking way.
# Adding an optional field with a default does NOT require a bump.
SCHEMA_VERSION: int = 1


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


class ErrorTaxonomy(str, Enum):
    """Structured classification of harness-observed failures.

    The harness records one of these on every request that does not reach
    FINISHED, giving the dossier a machine-readable failure mode instead of a
    raw exception string.
    """

    SANDBOX_MISSING_FILE = "SANDBOX_MISSING_FILE"
    SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"
    TOOL_ERROR = "TOOL_ERROR"
    PROVIDER_AUTH = "PROVIDER_AUTH"
    PROVIDER_BUDGET = "PROVIDER_BUDGET"
    TIMEOUT = "TIMEOUT"
    UNCAUGHT_TASK_EXCEPTION = "UNCAUGHT_TASK_EXCEPTION"
    USER_CANCELLED = "USER_CANCELLED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

# Regex fragments matched (case-insensitively) against dict key names.
# A key whose normalised name matches any pattern has its value replaced with
# _REDACTED before any serialised evidence is written to disk or uploaded.
# Extend this list to cover any new secret-bearing header or config field.
REDACTION_KEY_PATTERNS: list[str] = [
    r"api[_\-]?key",  # api_key, apiKey, DISCO_API_KEY, …
    r"secret",  # secret, DISCO_SECRET_KEY, client_secret, …
    r"token",  # token, access_token, refresh_token, …
    r"password",  # password, passwd, …
    r"auth(?:orization)?",  # auth, authorization, Authorization, …
    r"cookie",  # cookie, set-cookie, Set-Cookie, …
    r"bearer",  # bearer (Authorization: Bearer …)
]

_COMPILED: list[re.Pattern[str]] = [re.compile(p, re.IGNORECASE) for p in REDACTION_KEY_PATTERNS]

_REDACTED = "***REDACTED***"

# Numeric token-COUNT telemetry fields. These key names contain "token" and so
# match a REDACTION_KEY_PATTERN, but they carry an integer *count* (in/out/cached
# tokens per model call) — not a secret. Allowlist them so the debug trace can
# MEASURE cache-hit ratio + token cost. The pass-through is intentionally narrow:
# it applies ONLY when the value is a plain number (int/float, not bool). A list
# or string under one of these keys could still hold real auth tokens, so those
# stay redacted.
_TOKEN_COUNT_KEYS: frozenset[str] = frozenset(
    {"in_tokens", "out_tokens", "cached_tokens", "tokens"}
)


def _is_token_count(key: str, value: Any) -> bool:
    """True when *key* is an allowlisted token-count field holding a plain number."""
    return (
        key.lower() in _TOKEN_COUNT_KEYS
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    )


# ---------------------------------------------------------------------------
# String-content secret scrubber
# ---------------------------------------------------------------------------

# (compiled_pattern, replacement_template) pairs applied left-to-right to
# every ``str`` value encountered during redact() traversal, regardless of
# the dict key that contains it.  Templates may use ``\1`` back-references to
# preserve non-secret prefix material.
_CONTENT_SCRUB_RULES: list[tuple[re.Pattern[str], str]] = [
    # 1. OpenAI / Anthropic / generic sk- prefixed keys (sk-proj-, sk-ant-, …).
    #    The ``sk-`` prefix is an unambiguous secret signal; no context needed.
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), _REDACTED),
    # 2. AWS IAM access key IDs: AKIA + exactly 16 uppercase alphanum chars.
    (re.compile(r"AKIA[0-9A-Z]{16}"), _REDACTED),
    # 3. Bearer token: "Bearer <token>" (any capitalisation).
    #    Matches Authorization-header values and JSON body bearer fields.
    (re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/=]{10,}", re.IGNORECASE), _REDACTED),
    # 4. Authorization header with a direct long token (≥20 chars avoids
    #    falsely matching the short scheme word "Bearer" itself).
    (
        re.compile(r"Authorization\s*:\s*[A-Za-z0-9\-._~+/=]{20,}", re.IGNORECASE),
        _REDACTED,
    ),
    # 5. Contextual blobs: a secret-marker keyword immediately before (≤5
    #    separator chars of whitespace, colon, or equals) a long url-safe value
    #    (≥20 chars).  The marker + separator are kept (captured in group 1);
    #    only the value is replaced.  This is deliberately conservative: the
    #    marker must be syntactically adjacent — a keyword elsewhere in the
    #    sentence does NOT trigger redaction.
    (
        re.compile(
            r"((?:api[_\-]?key|secret|token|password|bearer|authorization)"
            r"[\s:=]{1,5})[A-Za-z0-9+/=_\-]{20,}",
            re.IGNORECASE,
        ),
        r"\1" + _REDACTED,
    ),
]


def _scrub_string_content(s: str) -> str:
    """Replace secret-looking substrings in *s* with ``_REDACTED``.

    Applied to every ``str`` value encountered during :func:`redact` traversal,
    independent of the key that holds the string.  Applies
    :data:`_CONTENT_SCRUB_RULES` left-to-right; an earlier rule may consume
    characters before later ones see them.
    """
    for pat, repl in _CONTENT_SCRUB_RULES:
        s = pat.sub(repl, s)
    return s


def _key_is_sensitive(key: str) -> bool:
    return any(pat.search(key) for pat in _COMPILED)


def redact(obj: Any) -> Any:
    """Deep-traverse *obj* and replace sensitive-key values with ``'***REDACTED***'``.

    Sensitive keys are identified by case-insensitive match against
    ``REDACTION_KEY_PATTERNS``. The entire value (including any nested
    structure) is replaced — a nested dict under a sensitive key is not
    preserved.

    Handles arbitrarily nested dicts, lists, tuples, sets, and pydantic
    model instances (via ``model_dump()``).  Scalars are returned as-is
    (they carry no key context and are therefore safe by default).

    Container mapping:
      - ``dict``  → ``dict``  (keys preserved, values recursed)
      - ``list``  → ``list``  (items recursed)
      - ``tuple`` → ``tuple`` (items recursed; container type preserved)
      - ``set``   → ``list``  (items recursed; sets are not JSON-serializable)
      - pydantic ``BaseModel`` → ``dict`` via ``model_dump()``, then recursed
      - other objects with ``__dict__`` → ``dict`` via ``vars()``, then recursed
    """
    if isinstance(obj, dict):
        result: dict[Any, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and _key_is_sensitive(k):
                # Numeric token-count telemetry is not a secret — pass it through
                # as a number so the debug trace can measure cache hits / cost.
                if _is_token_count(k, v):
                    result[k] = v
                else:
                    result[k] = _REDACTED
            else:
                result[k] = redact(v)
        return result
    if isinstance(obj, list):
        return [redact(item) for item in obj]
    if isinstance(obj, tuple):
        return tuple(redact(item) for item in obj)
    if isinstance(obj, set):
        return [redact(item) for item in obj]
    # Pydantic models: serialize to a plain dict first, then redact that dict.
    if hasattr(obj, "model_dump") and callable(obj.model_dump):
        return redact(obj.model_dump())
    # Other user-defined instances (dataclasses, plain classes) that expose
    # their fields via __dict__. Guard with `not isinstance(obj, type)` so
    # class objects themselves are never processed as field containers.
    if not isinstance(obj, type) and hasattr(obj, "__dict__"):
        return redact(vars(obj))
    # str: scan for embedded secrets before returning.
    if isinstance(obj, str):
        return _scrub_string_content(obj)
    # int, float, bool, None, datetime, … — immutable, safe as-is.
    return obj


# ---------------------------------------------------------------------------
# W3C Trace Context helpers (§3.2)
# ---------------------------------------------------------------------------

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")


def make_traceparent(trace_id: str, span_id: str) -> str:
    """Return a W3C traceparent header value: ``00-<32hex>-<16hex>-01``.

    Both *trace_id* and *span_id* must be lowercase hex strings of the
    correct length; no validation is performed here — the caller is
    responsible for supplying valid ids (e.g. from ``uuid.uuid4().hex``).
    """
    return f"00-{trace_id}-{span_id}-01"


def parse_traceparent(s: str) -> tuple[str, str] | None:
    """Parse a W3C traceparent string into ``(trace_id, span_id)``.

    Returns ``None`` if *s* is not a valid ``00-<32hex>-<16hex>-<flags>``
    header value.  Only version ``"00"`` is accepted; unknown versions are
    treated as invalid.
    """
    parts = s.strip().split("-")
    if len(parts) != 4:
        return None
    version, trace_id, span_id, _flags = parts
    if version != "00":
        return None
    if not _HEX32.match(trace_id):
        return None
    if not _HEX16.match(span_id):
        return None
    return trace_id, span_id


# ---------------------------------------------------------------------------
# Canonical evidence record
# ---------------------------------------------------------------------------


class EvidenceRecord(BaseModel):
    """Canonical correlation record for one harness-instrumented interaction.

    This model is stamped into the ``meta`` dict of every event emitted while
    ``DISCO_E2E=1`` is set, and is stored as the root object of each
    ``ui-actions.jsonl`` entry.  It is the schema-level source of truth; HTTP
    and WS wiring that *propagates* these ids is a separate work item (W5+).

    ``frozen=True`` makes instances immutable at the type level, matching the
    convention established in ``events.py`` (``BaseEvent``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = Field(default=SCHEMA_VERSION)

    # The id assigned to this harness run (e.g. ``run_<timestamp>_<hex>``).
    # Stamped on every HTTP request and WS frame in the run so the dossier
    # can be reconstructed from any layer of the stack.
    harness_run_id: str

    # W3C Trace Context — None when the caller does not supply a traceparent.
    trace_id: str | None = None
    span_id: str | None = None

    # App-level correlation ids extracted from the HTTP response or WS payload.
    request_id: str | None = None
    conversation_id: str | None = None

    # The id of the ``disco.click()`` action that triggered this observation.
    # Links the event-log entry back to the specific UI gesture in
    # ``ui-actions.jsonl``.
    ui_action_id: str | None = None

    # Artifact ids produced or observed as a result of this interaction
    # (deliverable ids from DeliverableEvent, download filenames, …).
    artifact_ids: list[str] = Field(default_factory=list)
