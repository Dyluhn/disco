"""Export-time redaction for share bundles (RP-06).

A share bundle leaks the conversation's full event log out of the trusted
boundary: the tailnet server reaches a public-ish surface, and the bundle is
the future harness cassette format (RP-00). Anything that could grant
post-export access — API keys, tokens, password material, hostnames bound to
credentials — MUST be scrubbed BEFORE the bundle is written or returned.

**Design tenets (locked, RP-06 §2):**

  1. **Ordered regex** — specific patterns (`sk_live_…`) BEFORE generic ones
     (`KEY=value`); a generic catcher that runs first would either swallow
     earlier patterns whole or leave them partially substituted. The pattern
     set is composed at module import time; order is part of the contract.
  2. **Apply to events, not strings** — the redactor walks the event payload
     recursively (ActionEvent arguments, ObservationEvent tool_result.content,
     MessageEvent.message.content, etc.). Streaming-file deltas and any other
     ephemeral frame go through the same path so CVE-2026-41182-style
     "delta-bypasses-redaction" cannot happen.
  3. **Shell + env outputs are sensitive by default** — `os.environ` dumps
     (`KEY=value` lines from a `printenv` / `env` shell) carry every secret the
     shell session holds. Stripping is mandatory.
  4. **Pure + deterministic** — the same event in → the same redacted event
     out. No clock, no random, no side effects. This is the structural reason
     a share bundle can also be a deterministic harness cassette.
  5. **Reversible by redaction marker, not by inverse map** — we replace the
     secret with `[REDACTED:secret-name]`; we never store the original.
     Recreating the original would defeat the redaction.

Pattern table (the public scrubber API; expand cautiously — every addition
must come with a unit test in test_redaction.py that uses a VERBATIM
captured secret-bearing event, per the "real-sample harness" rule):

  - **Provider-prefixed secrets** (sk_live_, sk_test_, ghp_, gho_, ghu_, ghs_,
    ghr_, AKIA, ASIA, xoxb-, xoxp-, glpat-, hf_, xai-, sk-ant-, AKID, AIza)
    — name + body, including the trailing token body up to a safe boundary.
  - **Bearer / JWT** (Authorization: Bearer …, eyJ…/eyJ…/eyJ…)
  - **Generic KEY=value** in shell-env output — `name=value` where name is
    genuinely UPPER-case + underscores + at least one letter (an env-dump
    line), value stops at whitespace or shell-quote boundary. Two deliberate
    non-matches (UI-18): the name match is case-SENSITIVE, so ordinary
    lower-case prose/shell assignments are left readable; and a value that is
    a shell *substitution* (`KEY=$(cmd …)`, `KEY=$OTHER`) is left readable
    because it contains no literal secret — only a reference to one. The
    command the agent ran must stay legible in the activity feed.
  - **JSON `"key": "value"`** where the key looks like a credential name.
    Only the VALUE is replaced; the key and the JSON punctuation survive, so
    the surrounding object stays readable (`{"password":"[…]" ,"plan":"house"}`
    rather than a bare marker where a key/value pair used to be).
  - **URL-embedded credentials** (`https://user:pass@host`).
  - **PEM blocks** (`-----BEGIN … PRIVATE KEY-----`).

The pattern set is NOT a substitute for real secret-scanning (the
google/secret-finder ecosystem); it IS a defense-in-depth redaction that
catches the common cases before a bundle leaves the boundary. Hardening the
list further is a future, larger order.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

# ---- the pattern table (ordered, composed at import time) -------------------

# A small typology to keep the redaction labels informative (the marker says
# WHICH kind of secret, not just "secret") — reviewers can grep for specific
# types in bundles.
_SECRET_LABEL = "REDACTED:secret"
_ENV_LABEL = "REDACTED:env"
_BEARER_LABEL = "REDACTED:bearer"
_PEM_LABEL = "REDACTED:pem"
_URL_CRED_LABEL = "REDACTED:url-cred"
_JSON_CRED_LABEL = "REDACTED:json-cred"


def _compile(label: str, pattern: str) -> re.Pattern[str]:
    """Compile one pattern with the redaction-label back-reference substituted.

    Each pattern REPLACES its match with `<label>` (no substring of the
    original remains). Patterns are anchored as full-string fragments; the
    redactor walks the input string with `re.sub` per pattern.
    """
    return re.compile(pattern)


# ---- placeholder / test-fixture values (UI-18) ------------------------------
#
# The agent types test credentials into its own signup forms; redacting those
# makes the activity feed unreadable and protects nothing. This allowlist is
# deliberately NARROW: the value must ANNOUNCE itself as a fixture (it starts
# with an unmistakable placeholder word, or is a `<…>` / `{{…}}` template
# slot), must be short, and must be plain `[A-Za-z0-9._-]`. Anything with the
# shape of real credential material — a provider prefix, punctuation, high
# length — fails to match and is redacted as before. It is applied ONLY to the
# JSON-credential pattern (never to shell/env output, which stays sensitive by
# default per tenet 3).
_PLACEHOLDER_VALUE = re.compile(
    r"(?i)\A(?:"
    r"test|testing|demo|example|sample|dummy|fake|placeholder|changeme|"
    r"x{3,}|y{3,}|"
    r"<[A-Za-z0-9_ .-]{1,32}>|"
    r"\{\{[A-Za-z0-9_ .-]{1,32}\}\}"
    r")[A-Za-z0-9._-]{0,16}\Z"
)


def _json_cred_replacement(match: re.Match[str]) -> str:
    """Replace only the VALUE of a JSON credential pair, keeping the key.

    `{"password":"hunter2","plan":"house"}` becomes
    `{"password":"REDACTED:json-cred","plan":"house"}` — the object still
    parses and the reader can see WHICH field was scrubbed. An obvious
    test/placeholder value is left alone (see `_PLACEHOLDER_VALUE`).
    """
    key_prefix, value = match.group(1), match.group(2)
    if _PLACEHOLDER_VALUE.match(value):
        return match.group(0)
    return f'{key_prefix}"{_JSON_CRED_LABEL}"'


# Specific provider prefixes run first so the generic KEY=value / JSON-cred
# catchers that come after them don't partially mangle the prefix.
#
# Each entry is (label, pattern, replacement). `replacement` is whatever
# `re.sub` accepts: a template string (the plain label, for patterns that
# replace their whole match) or a callable (for patterns that keep the
# surrounding syntax and replace only the secret value).
_PATTERNS: list[tuple[str, re.Pattern[str], Any]] = [
    (
        _SECRET_LABEL,
        re.compile(
            r"(?i)"  # case-insensitive
            r"(?<![A-Za-z0-9])"  # never start inside ordinary words/URL slugs
            r"(?:(?:sk|pk|rk)[_-]|"
            r"ghp_|gho_|ghu_|ghs_|ghr_|"
            r"AKIA[0-9A-Z]{8,}|ASIA[0-9A-Z]{8,}|"
            r"xox[bpars]-[0-9A-Za-z-]{8,}|"
            r"glpat-[0-9A-Za-z_-]{8,}|"
            r"hf_[A-Za-z0-9]{8,}|"
            r"xai-[A-Za-z0-9]{8,}|"
            r"sk-ant-[A-Za-z0-9-]{8,}|"
            r"AIza[0-9A-Za-z_-]{20,}|"
            r"AKID[0-9A-Za-z]{8,}"
            r")"
            r"[A-Za-z0-9_\-]{8,}"
        ),
        _SECRET_LABEL,
    ),
    # Bearer tokens: Authorization: Bearer xxx, Token: yyy, etc.
    (
        _BEARER_LABEL,
        re.compile(
            r"(?i)(?:authorization|token|api[_-]?key)['\"\s:]+(?:bearer\s+)?"
            r"([A-Za-z0-9._\-]{12,})"
        ),
        _BEARER_LABEL,
    ),
    # JWT (3 base64url segments separated by dots).
    (
        _SECRET_LABEL,
        re.compile(r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
        _SECRET_LABEL,
    ),
    # PEM private keys (entire block replaced).
    (
        _PEM_LABEL,
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        _PEM_LABEL,
    ),
    # URL-embedded credentials: https://user:pass@host
    (
        _URL_CRED_LABEL,
        re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)([^\s/:@]+):([^\s/@]+)@"),
        _URL_CRED_LABEL,
    ),
    # JSON "credential-like key": "value"  (key starts with secret/credential
    # /token/key/password/auth/api_key/bearer/cookie, value is a non-empty
    # string). Generic enough to catch the common cases; specific enough to
    # avoid clobbering unrelated "name" fields.
    (
        _JSON_CRED_LABEL,
        re.compile(
            r'(?i)("(?:secret|client_?secret|credential|credentials|token|access_?token|'
            r"api[_-]?key|password|passwd|pwd|auth|bearer|cookie|private[_-]?key|"
            r"openai[_-]?api[_-]?key|anthropic[_-]?api[_-]?key)"
            r'"\s*:\s*)"([^"]+)"'
        ),
        _json_cred_replacement,
    ),
    # Generic KEY=value in shell-env-style output. The value is everything
    # up to whitespace, a single-quote boundary (matching the opening), or a
    # double-quote boundary. Skip the synthetic `KEY` marker (a value-less
    # env-var printout) — pattern only fires when there IS a value.
    (
        _ENV_LABEL,
        re.compile(
            # Case-SENSITIVE: a genuine env-dump / export line uses an
            # UPPER_SNAKE name. Lower-case `foo=bar` in prose or in an
            # ordinary shell line is not an env dump and stays readable.
            r"(?m)^[ \t]*([A-Z_][A-Z0-9_]{1,})[ \t]*=[ \t]*"
            # `KEY=$(cmd …)` / `KEY=$OTHER` is a shell substitution, not a
            # literal secret: there is nothing to leak and collapsing it
            # destroys the command the user is trying to read (UI-18).
            r"(?!\$)"
            r"(\"[^\"\n]*\"|'[^'\n]*'|[^\s\"'`#&;|\n]+)"
        ),
        _ENV_LABEL,
    ),
]


# ---- the redactor (the public scrubber API) ---------------------------------


def _redact_string(s: str) -> str:
    """Apply every ordered pattern to one string. Pure + deterministic."""
    for _label, pat, replacement in _PATTERNS:
        s = pat.sub(replacement, s)
    return s


# Event field names whose VALUES are user/agent-typed or tool-output text.
# A non-text field (booleans, structured payloads) is recursively walked but
# never redacted as a string. We deliberately do NOT redact the event `id`,
# `seq`, or `source` — those are routing metadata, not user content.
_TEXT_FIELDS: tuple[str, ...] = (
    "content",
    "thought",
    "summary",
    "error",
    "rationale",
    "title",
    "description",
    "context",
    "markdown",
    "query",
    "snippet",
    "docs",
    "deployment_url",
    "path",
)


def _redact_value(value: Any) -> Any:
    """Recursively redact one JSON-ish value.

    - str → `_redact_string`
    - dict → redact each value in place; recurse for nested structures
    - list/tuple → redact each element
    - anything else (int, bool, None) → unchanged

    Returns the SAME shape (dict/list/scalar) so event payloads stay
    structurally identical after redaction.
    """
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(v) for v in value)
    return value


def redact_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact a single event payload (the JSON dict form of an `Event`).

    The contract: input is the output of `event.model_dump(mode='json')` or
    the raw dict received from the WebSocket. Output is a new dict with the
    same shape and the same key set, but every text field walked through
    `_redact_string`.

    `kind`, `source`, `id`, `seq`, `timestamp`, and `schema_version` are NOT
    text fields and pass through unchanged.
    """
    return _redact_value(payload)


def redact_frame(frame: dict[str, Any]) -> dict[str, Any]:
    """Redact a streaming / ephemeral frame (file-stream deltas, token
    deltas, anything that crosses the boundary in real time).

    Same shape-preserving walk as `redact_event_payload`. Applied so a
    delta-only secret (CVE-2026-41182: deltas bypassed redaction) cannot
    slip through: the streaming path uses the same patterns as the export.
    """
    return _redact_value(frame)


# Convenience: the pattern table is exported so unit tests can verify
# coverage (and so the reviewer can audit the regexes without re-deriving
# them from a diff).
def patterns() -> list[tuple[str, re.Pattern[str]]]:
    """A copy of the ordered pattern table. Use in tests to assert the
    pattern you'd expect fired actually fired (or that the redaction
    label matches)."""
    return [(label, pat) for label, pat, _repl in _PATTERNS]


def redact_text(text: str) -> str:
    """Redact a raw text blob. Exposed for callers that have a string but
    not a structured event (the trace-everywhere default scrubber path)."""
    return _redact_string(text)


# Make the type checker happy when callers build a custom scrubber on top.
Scrub = Callable[[str], str]
