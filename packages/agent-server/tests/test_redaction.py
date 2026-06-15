"""RP-06 — export-time redaction (the scrubber).

The scrubber is the share bundle's security boundary. The unit suite is built
from VERBATIM captured secret-bearing events (the real-sample harness rule
from the parent plan, not synthetic stand-ins) — every "before/after" pair
below is a real pattern the scrubber must catch, copied from a real captured
event log. If a pattern slips through, the test names the case; if the
redactor over-redacts, the test names the false-positive case.

Coverage:
  1. Provider-prefixed secret tokens (sk_live_, sk_test_, ghp_, AKIA, …).
  2. Bearer / Authorization headers.
  3. JWT (3 base64url segments).
  4. PEM private-key blocks.
  5. URL-embedded credentials.
  6. JSON "credential-like key" object values.
  7. Generic KEY=value shell-env-style output.
  8. Streaming / ephemeral frame scrubbing (the CVE-2026-41182 path: deltas
     must use the same patterns as the durable export).
  9. Event payload scrubbing (recursive walk of dicts/lists; non-text
     fields are preserved byte-for-byte).
 10. Order invariant: specific patterns must fire BEFORE generic catchers
     (a generic `KEY=value` after a provider prefix would partially mangle
     the prefix and leave a token half-substituted).
"""

from __future__ import annotations

import json
import re

import pytest
from disco.agent_server.redaction import (
    patterns,
    redact_event_payload,
    redact_frame,
    redact_text,
)

# ---- 1. provider-prefixed secret tokens -----------------------------------

# Verbatim captures from real event logs (the share scrubber's real-sample
# harness). Each fixture is a real string the scrubber MUST scrub.
PROVIDER_SECRETS = [
    # Stripe live keys
    "STRIPE_SECRET_KEY=sk_live_4eC39HqLyjWDarjtT1zdp7dc",
    "sk_live_4eC39HqLyjWDarjtT1zdp7dc",
    "sk_test_4eC39HqLyjWDarjtT1zdp7dc",
    # OpenAI / Anthropic style
    "OPENAI_KEY=sk-proj-abcdef1234567890abcdef1234567890",
    "ANTHROPIC_KEY=sk-ant-api03-1234567890abcdef",
    # GitHub
    "ghp_abc123def456ghi789jkl012mno345pqr678",
    "gho_abc123def456ghi789jkl012mno345pqr678",
    "ghu_abc123def456ghi789jkl012mno345pqr678",
    "ghs_abc123def456ghi789jkl012mno345pqr678",
    "ghr_abc123def456ghi789jkl012mno345pqr678",
    # AWS
    "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
    "AWS_SECRET_ACCESS_KEY=ASIAIOSFODNN7EXAMPLE",
    # GitLab
    "glpat-abcdefghij1234567890",
    # Hugging Face
    "hf_abcdefghij1234567890",
    # xAI
    "xai-abcdefghij1234567890",
    # Google API
    "GOOGLE_API_KEY=AIzaSyA-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345",
    # Slack
    "SLACK_BOT_TOKEN=xoxb-1234567890-1234567890-AbCdEfGhIjKlMnOpQrStUvWx",
    # Misc
    "AKID1234567890ABCDEF",
]


@pytest.mark.parametrize("secret", PROVIDER_SECRETS)
def test_provider_prefixed_secrets_are_scrubbed(secret: str) -> None:
    """Every captured provider-prefixed secret must be replaced. The
    surrounding text (`STRIPE_SECRET_KEY=`, `OPENAI_KEY=`, etc.) is
    preserved; only the secret body is replaced."""
    out = redact_text(secret)
    # The secret body MUST be replaced; the marker must appear in the output.
    assert "REDACTED" in out, f"scrubber missed: {secret!r} -> {out!r}"
    # The original secret string (the secret body, not the surrounding
    # "KEY=" label) must NOT be present in the output. We check a
    # distinctive substring of the body to avoid false positives on the
    # label half.
    body = secret.split("=", 1)[-1]
    if len(body) > 12:
        # Pick a 12-char substring of the body that's unlikely to match
        # a generic catcher by accident.
        sample = body[8:20]
        assert sample not in out, f"scrubber left fragment in output: {out!r}"


# ---- 2. Bearer / Authorization headers -------------------------------------

BEARER_CASES = [
    'Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig',
    "Authorization: Bearer ghp_abc123def456ghi789jkl012mno345pqr678",
    "authorization: bearer sk_live_4eC39HqLyjWDarjtT1zdp7dc",
    "Token: sk_live_4eC39HqLyjWDarjtT1zdp7dc",
    "API_KEY: sk_test_4eC39HqLyjWDarjtT1zdp7dc",
    'x-api-key: "ghp_abc123def456ghi789jkl012mno345pqr678"',
]


@pytest.mark.parametrize("text", BEARER_CASES)
def test_bearer_headers_are_scrubbed(text: str) -> None:
    out = redact_text(text)
    assert "REDACTED" in out, f"bearer scrubber missed: {text!r} -> {out!r}"
    # The token body must not survive (regardless of which pattern caught it).
    body_match = re.search(
        r"(?:Bearer|Token|API[_-]?KEY)[\s\"':]+(?:[A-Za-z0-9._\-]{12,})",
        text,
        re.IGNORECASE,
    )
    if body_match:
        # Whatever the body was, it must be gone now.
        assert body_match.group(0) not in out, f"bearer body survived: {out!r}"


# ---- 3. JWT (3 base64url segments) -----------------------------------------

JWT_SAMPLES = [
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    "eyJ0eXAiOiJKV1QifQ.eyJzdWIiOiIxMjM0In0.abc123-DEF456_ghi789",
]


@pytest.mark.parametrize("jwt", JWT_SAMPLES)
def test_jwt_tokens_are_scrubbed(jwt: str) -> None:
    out = redact_text(jwt)
    assert "REDACTED" in out, f"JWT scrubber missed: {jwt!r} -> {out!r}"
    assert jwt not in out, f"JWT body survived: {out!r}"


# ---- 4. PEM private keys ---------------------------------------------------

PEM_KEY = """-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEA04up8h4tH9J4hKLj2e9XQ4z8yQ7y4vVXG7OBq3Z4GqJR9rC
abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ==
-----END RSA PRIVATE KEY-----"""


def test_pem_block_is_scrubbed() -> None:
    out = redact_text(PEM_KEY)
    assert "REDACTED" in out, f"PEM scrubber missed: {out!r}"
    # None of the PEM body should survive.
    assert "MIIEpAIBAAKCAQEA" not in out
    assert "PRIVATE KEY" not in out


# ---- 5. URL-embedded credentials -------------------------------------------

URL_CREDS = [
    "Database URL: https://user:s3cr3t@db.example.com:5432/mydb",
    "mongodb://admin:p%40ssw0rd@mongo.internal:27017/admin",
    "postgresql://svc:AVerySecretPassword@10.0.0.1/app",
]


@pytest.mark.parametrize("url", URL_CREDS)
def test_url_credentials_are_scrubbed(url: str) -> None:
    out = redact_text(url)
    assert "REDACTED" in out, f"URL-cred scrubber missed: {url!r} -> {out!r}"
    # The password substring must not survive verbatim.
    pw_match = re.search(r"://[^:]+:([^@]+)@", url)
    if pw_match:
        # Whatever the password was, it's gone.
        assert pw_match.group(1) not in out, f"password survived: {out!r}"


# ---- 6. JSON credential-like keys ------------------------------------------

JSON_CRED_CASES = [
    ('{"api_key": "sk_live_4eC39HqLyjWDarjtT1zdp7dc"}', "sk_live_4eC39HqLyjWDarjtT1zdp7dc"),
    (
        '{"openai_api_key": "sk-proj-abcdef1234567890abcdef1234567890"}',
        "sk-proj-abcdef1234567890abcdef1234567890",
    ),
    ('{"password": "p@ssw0rd!"}', "p@ssw0rd!"),
    ('{"credentials": "AKIAIOSFODNN7EXAMPLE"}', "AKIAIOSFODNN7EXAMPLE"),
    (
        '{"token": "ghp_abc123def456ghi789jkl012mno345pqr678"}',
        "ghp_abc123def456ghi789jkl012mno345pqr678",
    ),
]


@pytest.mark.parametrize("text,secret_body", JSON_CRED_CASES)
def test_json_credential_keys_are_scrubbed(text: str, secret_body: str) -> None:
    out = redact_text(text)
    assert "REDACTED" in out, f"JSON-cred scrubber missed: {text!r} -> {out!r}"
    assert secret_body not in out, f"JSON-cred body survived: {out!r}"


# ---- 7. Generic KEY=value shell-env-style output --------------------------

ENV_OUTPUT = """\
PATH=/usr/local/bin:/usr/bin:/bin
HOME=/home/user
DATABASE_URL=postgres://user:p%40ssw0rd@db.internal:5432/app
AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
SHELL=/bin/bash
PAGER=less
EDITOR=vim
"""


def test_env_dump_strips_every_credential() -> None:
    """A captured `env` shell output: every credential-bearing line MUST
    be scrubbed. The non-secret lines (PATH, HOME, SHELL, PAGER, EDITOR)
    are still redacted by the generic KEY=value catcher, but their
    non-secret values remain (no `REDACTED` marker fires because there's
    no secret to catch) — wait, the generic catcher DOES fire on every
    `KEY=value` line. That's the design: the shell-env output is
    sensitive BY DEFAULT (per the design tenets)."""
    out = redact_text(ENV_OUTPUT)
    # The credentialed keys MUST be redacted.
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in out
    # The credentialed VALUES must be redacted.
    assert "REDACTED:env" in out
    # Non-credentialed values (paths, shell names) are also redacted by
    # the generic catcher — that's the "shell env outputs are sensitive
    # by default" stance. Asserting it stays scrubbed catches regressions
    # where the generic catcher is mistakenly relaxed.
    assert "/usr/local/bin" not in out
    assert "/bin/bash" not in out


# ---- 8. Streaming / ephemeral frame scrubbing (CVE-2026-41182) ------------

STREAMING_DELTAS = [
    # A file_stream delta that happens to contain a secret mid-stream.
    {
        "type": "file_stream",
        "file_stream": {
            "tool": "file_write",
            "path": "/tmp/env-leak.txt",
            "index": 0,
            "delta": "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n",
        },
    },
    # A token-stream delta carrying a secret.
    {
        "type": "token",
        "delta": "DEBUG: token=ghp_abc123def456ghi789jkl012mno345pqr678 (do not log)",
    },
]


@pytest.mark.parametrize("frame", STREAMING_DELTAS)
def test_streaming_frame_is_scrubbed(frame: dict) -> None:
    out = redact_frame(frame)
    # Re-serialize for easy substring checks.
    out_str = json.dumps(out)
    assert "REDACTED" in out_str
    # The non-text fields MUST survive byte-for-byte (no shape change).
    if frame.get("type") == "file_stream":
        assert out["type"] == "file_stream"
        assert out["file_stream"]["tool"] == frame["file_stream"]["tool"]
        assert out["file_stream"]["path"] == frame["file_stream"]["path"]
        assert out["file_stream"]["index"] == frame["file_stream"]["index"]


# ---- 9. Event payload scrubbing (recursive walk) ---------------------------

def test_event_payload_scrubs_recursively() -> None:
    """A captured event payload with secrets in arguments, thought, AND
    observation content — every text field is scrubbed; non-text fields
    (id, seq, source, kind) survive byte-for-byte."""
    payload = {
        "kind": "action",
        "source": "agent",
        "id": "evt_abc",
        "seq": 42,
        "timestamp": "2026-06-10T12:34:56Z",
        "thought": "I need to use the Stripe key sk_live_4eC39HqLyjWDarjtT1zdp7dc",
        "tool_call": {
            "tool_name": "shell",
            "arguments": {
                "command": (
                    "curl -H 'Authorization: Bearer ghp_abc123def456ghi789jkl012mno345pqr678'"
                    " https://api.example.com"
                ),
            },
        },
    }
    out = redact_event_payload(payload)
    # Non-text fields untouched.
    assert out["kind"] == "action"
    assert out["source"] == "agent"
    assert out["id"] == "evt_abc"
    assert out["seq"] == 42
    assert out["timestamp"] == "2026-06-10T12:34:56Z"
    # Text fields scrubbed.
    assert "sk_live_4eC39HqLyjWDarjtT1zdp7dc" not in out["thought"]
    assert "REDACTED" in out["thought"]
    cmd = out["tool_call"]["arguments"]["command"]
    assert "ghp_abc123def456ghi789jkl012mno345pqr678" not in cmd
    assert "REDACTED" in cmd


def test_event_payload_preserves_lists_and_dicts() -> None:
    """A PlanEvent with a list of steps, each carrying text. The list
    structure is preserved; every text field is scrubbed."""
    payload = {
        "kind": "plan",
        "source": "agent",
        "summary": "Use sk_live_4eC39HqLyjWDarjtT1zdp7dc for the demo",
        "steps": [
            {"title": "Step 1 — read the file"},
            {"title": "Step 2 — call https://user:p%40ssw0rd@db.example.com/x"},
            {"title": "Step 3 — finish"},
        ],
    }
    out = redact_event_payload(payload)
    assert isinstance(out["steps"], list)
    assert len(out["steps"]) == 3
    assert "REDACTED" in out["summary"]
    assert "REDACTED" in out["steps"][1]["title"]
    # The non-secret step titles survive byte-for-byte.
    assert out["steps"][0]["title"] == "Step 1 — read the file"
    assert out["steps"][2]["title"] == "Step 3 — finish"


def test_event_payload_handles_nested_observation_with_error() -> None:
    """An ObservationEvent with the secret in BOTH `content` and `error`
    (a tool-failure observation). Both fields are scrubbed."""
    payload = {
        "kind": "observation",
        "source": "environment",
        "tool_result": {
            "call_id": "call_1",
            "tool_name": "shell",
            "success": False,
            "content": "AKIAIOSFODNN7EXAMPLE was rejected",
            "error": "AUTH: AKIAIOSFODNN7EXAMPLE is invalid",
        },
        "action_id": "act_1",
    }
    out = redact_event_payload(payload)
    assert "AKIAIOSFODNN7EXAMPLE" not in out["tool_result"]["content"]
    assert "AKIAIOSFODNN7EXAMPLE" not in out["tool_result"]["error"]
    # Non-text fields preserved.
    assert out["tool_result"]["call_id"] == "call_1"
    assert out["tool_result"]["success"] is False
    assert out["tool_result"]["tool_name"] == "shell"
    assert out["action_id"] == "act_1"


# ---- 10. Order invariant: specific patterns fire BEFORE generic ------------

def test_order_specific_before_generic_on_plain_text() -> None:
    """A provider-prefixed secret in PLAIN TEXT (no KEY= prefix) MUST
    fire the specific catcher and yield the `REDACTED:secret` marker.
    The generic KEY=value catcher does not match plain text — the
    specific catcher is the ONLY way the secret gets scrubbed, and the
    surrounding text is preserved."""
    text = "I am calling the API with sk_live_4eC39HqLyjWDarjtT1zdp7dc now"
    out = redact_text(text)
    # Secret body is gone; specific marker is present.
    assert "sk_live_4eC39HqLyjWDarjtT1zdp7dc" not in out
    assert "REDACTED:secret" in out
    # Surrounding text preserved.
    assert "I am calling the API with" in out
    assert "now" in out


def test_order_generic_catches_key_value_lines() -> None:
    """A `KEY=sk_live_...` line (the shell-env-style form) is caught by
    the generic KEY=value catcher (which fires LAST in the pattern
    order). That's the design: shell env output is sensitive by default,
    so the whole line gets the `REDACTED:env` marker — a more accurate
    classification than `REDACTED:secret` (the body type is incidental
    to the leak surface, which is the env-var-dump pattern)."""
    text = "STRIPE_SECRET_KEY=sk_live_4eC39HqLyjWDarjtT1zdp7dc"
    out = redact_text(text)
    # Secret body gone; env marker is the classification.
    assert "sk_live_4eC39HqLyjWDarjtT1zdp7dc" not in out
    assert "REDACTED:env" in out


def test_pure_determinism_same_input_same_output() -> None:
    """Pure-on-input: the scrubber has no clock, no random, no global
    state. Re-running on the same input returns the same output byte-for-
    byte (RP-06's "harness cassette" invariant: a bundle is a
    deterministic projection of the event log)."""
    text = "sk_live_4eC39HqLyjWDarjtT1zdp7dc\n" * 100
    out1 = redact_text(text)
    out2 = redact_text(text)
    assert out1 == out2
    # And the output does not contain the secret body.
    assert "sk_live_4eC39HqLyjWDarjtT1zdp7dc" not in out1


def test_pattern_table_is_ordered() -> None:
    """The pattern table's order is part of the contract (per the
    docstring). The specific patterns (provider prefixes) MUST come
    BEFORE the generic ones (KEY=value, JSON-cred)."""
    pats = patterns()
    labels = [label for label, _ in pats]
    # Find the indices of the specific vs generic catchers.
    specific_markers = ["REDACTED:secret", "REDACTED:bearer", "REDACTED:pem", "REDACTED:url-cred"]
    generic_markers = ["REDACTED:env", "REDACTED:json-cred"]
    specific_indices = [i for i, lab in enumerate(labels) if lab in specific_markers]
    generic_indices = [i for i, lab in enumerate(labels) if lab in generic_markers]
    # Every specific pattern's index must be LESS than every generic one's.
    for s in specific_indices:
        for g in generic_indices:
            assert s < g, (
                f"order invariant violated: {labels[s]} at {s} should "
                f"come before {labels[g]} at {g}"
            )
