"""Tests for disco.core.evidence.schema — W4 evidence/correlation schema.

Covers:
  1. EvidenceRecord round-trips (model_validate / model_dump)
  2. redact() — nested sensitive-key replacement, non-sensitive keys untouched
  3. make_traceparent / parse_traceparent round-trips and rejection cases
  4. ErrorTaxonomy — exhaustiveness / value shapes
"""

from __future__ import annotations

import uuid

import pytest
from disco.core.evidence.schema import (
    REDACTION_KEY_PATTERNS,
    SCHEMA_VERSION,
    ErrorTaxonomy,
    EvidenceRecord,
    make_traceparent,
    parse_traceparent,
    redact,
)
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# 1. EvidenceRecord round-trips
# ---------------------------------------------------------------------------


def _run_id() -> str:
    return f"run_{uuid.uuid4().hex[:12]}"


def test_evidence_record_minimal_round_trip() -> None:
    """Only the required field (harness_run_id) is supplied."""
    rec = EvidenceRecord(harness_run_id="run_abc123")
    dumped = rec.model_dump()
    restored = EvidenceRecord.model_validate(dumped)
    assert restored == rec
    assert restored.schema_version == SCHEMA_VERSION
    assert restored.trace_id is None
    assert restored.artifact_ids == []


def test_evidence_record_full_round_trip() -> None:
    """All fields populated; round-trip preserves values exactly."""
    run_id = _run_id()
    trace_id = uuid.uuid4().hex  # uuid4().hex = 32 lowercase hex chars
    span_id = uuid.uuid4().hex[:16]  # 16 hex
    rec = EvidenceRecord(
        harness_run_id=run_id,
        trace_id=trace_id,
        span_id=span_id,
        request_id="req_001",
        conversation_id="cid_001",
        ui_action_id="act_001",
        artifact_ids=["art_a", "art_b"],
    )
    dumped = rec.model_dump()
    restored = EvidenceRecord.model_validate(dumped)
    assert restored == rec
    assert restored.trace_id == trace_id
    assert restored.span_id == span_id
    assert restored.artifact_ids == ["art_a", "art_b"]


def test_evidence_record_schema_version_default() -> None:
    rec = EvidenceRecord(harness_run_id="run_x")
    assert rec.schema_version == 1


def test_evidence_record_extra_fields_forbidden() -> None:
    """extra='forbid' — unknown fields must raise ValidationError."""
    with pytest.raises(ValidationError):
        EvidenceRecord.model_validate({"harness_run_id": "r", "unknown_field": True})


def test_evidence_record_json_round_trip() -> None:
    """model_dump(mode='json') / model_validate round-trip stays consistent."""
    rec = EvidenceRecord(harness_run_id="run_json", artifact_ids=["x"])
    as_json_dict = rec.model_dump(mode="json")
    restored = EvidenceRecord.model_validate(as_json_dict)
    assert restored == rec


# ---------------------------------------------------------------------------
# 2. redact()
# ---------------------------------------------------------------------------


def test_redact_flat_dict_sensitive_key() -> None:
    data = {"api_key": "sk-secret-value", "model": "gpt-5"}
    result = redact(data)
    assert isinstance(result, dict)
    assert result["api_key"] == "***REDACTED***"
    assert result["model"] == "gpt-5"


def test_redact_nested_dict() -> None:
    data = {
        "config": {
            "provider": "openrouter",
            "token": "tok_abc",
            "timeout": 30,
        }
    }
    result = redact(data)
    assert isinstance(result, dict)
    assert isinstance(result["config"], dict)
    assert result["config"]["token"] == "***REDACTED***"
    assert result["config"]["provider"] == "openrouter"
    assert result["config"]["timeout"] == 30


def test_redact_nested_value_under_sensitive_key_is_fully_replaced() -> None:
    """Even a nested dict under a sensitive key is replaced, not traversed."""
    data = {"secret": {"nested_key": "should-not-appear", "another": 42}}
    result = redact(data)
    assert isinstance(result, dict)
    assert result["secret"] == "***REDACTED***"


def test_redact_list_traversed() -> None:
    data = [{"password": "hunter2", "user": "alice"}, {"normal": "value"}]
    result = redact(data)
    assert isinstance(result, list)
    assert result[0]["password"] == "***REDACTED***"
    assert result[0]["user"] == "alice"
    assert result[1]["normal"] == "value"


def test_redact_case_insensitive() -> None:
    """Keys are matched case-insensitively."""
    data = {
        "API_KEY": "upper",
        "Authorization": "Bearer tok",
        "COOKIE": "sid=abc",
        "disco_secret_key": "shhh",
        "BEARER": "raw-bearer",
    }
    result = redact(data)
    assert isinstance(result, dict)
    for k in data:
        assert result[k] == "***REDACTED***", f"expected {k} to be redacted"


def test_redact_non_sensitive_keys_untouched() -> None:
    data = {
        "model": "qwen",
        "surface": "build",
        "conversation_id": "cid_123",
        "artifact_ids": ["a", "b"],
        "count": 42,
        "enabled": True,
        "nothing": None,
    }
    result = redact(data)
    assert result == data


def test_redact_scalar_passthrough() -> None:
    assert redact("hello") == "hello"
    assert redact(42) == 42
    assert redact(None) is None
    assert redact(True) is True


def test_redact_deeply_nested() -> None:
    data = {"a": {"b": {"c": {"api_key": "deep-secret", "safe": "ok"}}}}
    result = redact(data)
    assert isinstance(result, dict)
    assert result["a"]["b"]["c"]["api_key"] == "***REDACTED***"
    assert result["a"]["b"]["c"]["safe"] == "ok"


def test_redact_list_of_lists() -> None:
    data = [["no_match", {"token": "tok"}]]
    result = redact(data)
    assert isinstance(result, list)
    assert result[0][1]["token"] == "***REDACTED***"


def test_redaction_key_patterns_non_empty() -> None:
    assert len(REDACTION_KEY_PATTERNS) >= 5, "expected at least 5 redaction patterns"


def test_redact_numeric_token_counts_pass_through() -> None:
    """CW-7: numeric token-COUNT fields are telemetry, not secrets — they match the
    "token" key pattern but must survive redaction as numbers so the debug trace can
    measure cache-hit ratio / token cost. A real secret-shaped field in the same
    span still gets redacted."""
    span = {
        "span": "agent.step",
        "in_tokens": 123,
        "out_tokens": 45,
        "cached_tokens": 67,
        "tokens": 235,
        "access_token": "sk-deadbeefdeadbeefdeadbeef0001",  # a real secret
    }
    result = redact(span)
    assert isinstance(result, dict)
    # counts pass through unchanged, as numbers
    assert result["in_tokens"] == 123
    assert result["out_tokens"] == 45
    assert result["cached_tokens"] == 67
    assert result["tokens"] == 235
    # the actual secret-bearing token field is still redacted
    assert result["access_token"] == "***REDACTED***"


def test_redact_token_count_keys_only_passthrough_numbers() -> None:
    """The allowlist is value-typed: a non-numeric value under a count key (a list
    or string that could hold real tokens) stays redacted. Bools are not counts."""
    data = {
        "tokens": ["secret-a", "secret-b"],  # a list of real tokens, NOT a count
        "in_tokens": "not-a-number",
        "cached_tokens": True,  # bool is not a count
        "out_tokens": 0,  # a genuine zero count passes through
    }
    result = redact(data)
    assert isinstance(result, dict)
    assert result["tokens"] == "***REDACTED***"
    assert result["in_tokens"] == "***REDACTED***"
    assert result["cached_tokens"] == "***REDACTED***"
    assert result["out_tokens"] == 0


# ---------------------------------------------------------------------------
# 3. make_traceparent / parse_traceparent
# ---------------------------------------------------------------------------


def _valid_trace_id() -> str:
    return uuid.uuid4().hex  # uuid4().hex is already 32 lowercase hex chars


def _valid_span_id() -> str:
    return uuid.uuid4().hex[:16]  # 16 lowercase hex chars


def test_make_traceparent_format() -> None:
    trace_id = _valid_trace_id()
    span_id = _valid_span_id()
    tp = make_traceparent(trace_id, span_id)
    assert tp == f"00-{trace_id}-{span_id}-01"


def test_parse_traceparent_round_trip() -> None:
    trace_id = _valid_trace_id()
    span_id = _valid_span_id()
    tp = make_traceparent(trace_id, span_id)
    parsed = parse_traceparent(tp)
    assert parsed is not None
    got_trace, got_span = parsed
    assert got_trace == trace_id
    assert got_span == span_id


def test_parse_traceparent_rejects_invalid_version() -> None:
    trace_id = _valid_trace_id()
    span_id = _valid_span_id()
    tp = f"01-{trace_id}-{span_id}-01"
    assert parse_traceparent(tp) is None


def test_parse_traceparent_rejects_short_trace_id() -> None:
    # 31 chars instead of 32
    trace_id = "a" * 31
    span_id = _valid_span_id()
    tp = f"00-{trace_id}-{span_id}-01"
    assert parse_traceparent(tp) is None


def test_parse_traceparent_rejects_short_span_id() -> None:
    trace_id = _valid_trace_id()
    span_id = "b" * 15  # 15 instead of 16
    tp = f"00-{trace_id}-{span_id}-01"
    assert parse_traceparent(tp) is None


def test_parse_traceparent_rejects_uppercase_hex() -> None:
    """W3C spec requires lowercase hex; uppercase is invalid."""
    trace_id = _valid_trace_id().upper()  # uppercase
    span_id = _valid_span_id()
    tp = f"00-{trace_id}-{span_id}-01"
    assert parse_traceparent(tp) is None


def test_parse_traceparent_rejects_wrong_part_count() -> None:
    assert parse_traceparent("00-abc") is None
    assert parse_traceparent("") is None
    assert parse_traceparent("not-a-traceparent-at-all-extra") is None


def test_parse_traceparent_strips_whitespace() -> None:
    trace_id = _valid_trace_id()
    span_id = _valid_span_id()
    tp = f"  00-{trace_id}-{span_id}-01  "
    parsed = parse_traceparent(tp)
    assert parsed is not None


# ---------------------------------------------------------------------------
# 4. ErrorTaxonomy exhaustiveness
# ---------------------------------------------------------------------------


def test_error_taxonomy_has_expected_values() -> None:
    """Verify the taxonomy covers the minimum required failure modes."""
    required = {
        "SANDBOX_MISSING_FILE",
        "SANDBOX_UNAVAILABLE",
        "TOOL_ERROR",
        "PROVIDER_AUTH",
        "PROVIDER_BUDGET",
        "TIMEOUT",
        "UNCAUGHT_TASK_EXCEPTION",
        "USER_CANCELLED",
        "VALIDATION_ERROR",
        "UNKNOWN",
    }
    actual = {member.value for member in ErrorTaxonomy}
    assert required <= actual, f"Missing taxonomy members: {required - actual}"


def test_error_taxonomy_is_str_enum() -> None:
    """ErrorTaxonomy members are strings (so they serialise cleanly to JSON)."""
    for member in ErrorTaxonomy:
        assert isinstance(member.value, str)
        assert member == member.value  # str-enum equality


def test_error_taxonomy_has_unknown_fallback() -> None:
    assert ErrorTaxonomy.UNKNOWN is not None
    assert ErrorTaxonomy("UNKNOWN") is ErrorTaxonomy.UNKNOWN


def test_error_taxonomy_count_at_least_ten() -> None:
    assert len(list(ErrorTaxonomy)) >= 10


# ---------------------------------------------------------------------------
# 5. redact() — tuple / set / pydantic-model robustness
# ---------------------------------------------------------------------------


def test_redact_tuple_recurses() -> None:
    """A dict with a sensitive key nested inside a tuple is redacted."""
    data = {"wrapper": ({"api_key": "tuple-secret"}, "safe-string")}
    result = redact(data)
    assert isinstance(result["wrapper"], tuple), "tuple container must be preserved"
    inner = result["wrapper"][0]
    assert isinstance(inner, dict)
    assert inner["api_key"] == "***REDACTED***"
    assert result["wrapper"][1] == "safe-string"


def test_redact_set_converted_to_list() -> None:
    """A set value is recursed and returned as a list (JSON-safe)."""
    data: dict = {"tag_set": {"alpha", "beta", "gamma"}}
    result = redact(data)
    assert isinstance(result["tag_set"], list), "set must be converted to a list"
    assert set(result["tag_set"]) == {"alpha", "beta", "gamma"}


def test_redact_pydantic_model() -> None:
    """A pydantic model instance is redacted through model_dump()."""
    from pydantic import BaseModel

    class ProviderConfig(BaseModel):
        api_key: str
        provider: str
        timeout: int

    config = ProviderConfig(api_key="sk-pydantic-secret", provider="openrouter", timeout=30)
    result = redact({"config": config})
    assert isinstance(result["config"], dict), (
        "pydantic model must be serialized to a dict before redaction"
    )
    assert result["config"]["api_key"] == "***REDACTED***"
    assert result["config"]["provider"] == "openrouter"
    assert result["config"]["timeout"] == 30


# ---------------------------------------------------------------------------
# 6. redact() — string-content secret scrubbing (P1 fix)
# ---------------------------------------------------------------------------


def test_redact_sk_key_in_string_content_neutral_key() -> None:
    """An sk-* secret embedded in a string under a neutral key is redacted."""
    secret = "sk-ant-api03-AbCdEfGhIjKlMnOpQrSt"  # 33-char sk- key
    data = {"content": f"my key is {secret}"}
    result = redact(data)
    assert isinstance(result["content"], str)
    assert secret not in result["content"], "sk- key must be scrubbed from string content"
    assert "***REDACTED***" in result["content"]
    # Non-secret leading text is preserved
    assert "my key is " in result["content"]


def test_redact_sk_proj_variant_in_string_content() -> None:
    """sk-proj- variant (OpenAI project key format) is also scrubbed."""
    secret = "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz"
    data = {"stdout": f"export OPENAI_API_KEY={secret}"}
    result = redact(data)
    assert secret not in result["stdout"]
    assert "***REDACTED***" in result["stdout"]


def test_redact_bearer_token_in_string_content() -> None:
    """'Bearer <token>' anywhere in a string value is redacted."""
    # 36-char base64url JWT header segment (real-looking)
    token = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9"
    data = {"message": f"Bearer {token}"}
    result = redact(data)
    assert isinstance(result["message"], str)
    assert token not in result["message"], "Bearer token must be scrubbed"
    assert "***REDACTED***" in result["message"]


def test_redact_bearer_token_case_insensitive() -> None:
    """'bearer' with any capitalisation is caught."""
    token = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9"
    for header in (f"bearer {token}", f"BEARER {token}", f"Bearer {token}"):
        result = redact({"h": header})
        assert token not in result["h"], f"token not scrubbed in: {header!r}"


def test_redact_aws_akia_key_in_string_content() -> None:
    """An AWS AKIA access-key ID embedded in a string is redacted."""
    akia = "AKIAIOSFODNN7EXAMPLE"  # canonical AWS example key (20 chars: AKIA + 16)
    data = {"error": f"invalid {akia} access key"}
    result = redact(data)
    assert isinstance(result["error"], str)
    assert akia not in result["error"], "AKIA key must be scrubbed from string content"
    assert "***REDACTED***" in result["error"]
    assert "invalid" in result["error"]  # surrounding prose preserved
    assert "access key" in result["error"]


def test_redact_contextual_blob_password_equals() -> None:
    """A long value immediately after 'password=' is scrubbed; marker is kept."""
    data = {"output": "password=SuperSecretPassword123456789"}
    result = redact(data)
    assert "SuperSecretPassword123456789" not in result["output"]
    assert "***REDACTED***" in result["output"]
    assert "password=" in result["output"]  # marker preserved


def test_redact_contextual_blob_token_colon() -> None:
    """A long value immediately after 'token: ' is scrubbed."""
    data = {"log": "token: abcdefghijklmnopqrstuvwxyz0123456789"}
    result = redact(data)
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in result["log"]
    assert "***REDACTED***" in result["log"]


def test_redact_no_false_positive_git_sha_not_near_marker() -> None:
    """A 40-char hex SHA NOT near a secret-marker keyword is NOT redacted."""
    sha = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"  # 40-char hex SHA
    prose = f"Commit {sha} is the latest on main."
    data = {"description": prose}
    result = redact(data)
    assert result["description"] == prose, (
        f"git SHA in ordinary prose must not be redacted; got: {result['description']!r}"
    )


def test_redact_no_false_positive_uuid_neutral_key() -> None:
    """A UUID stored under a neutral key name is NOT redacted."""
    uid = "550e8400-e29b-41d4-a716-446655440000"
    data = {"correlation_id": uid}
    result = redact(data)
    assert result["correlation_id"] == uid, "UUID must not be false-positive redacted"


def test_redact_no_false_positive_ordinary_prose() -> None:
    """Ordinary English sentences without secret markers or prefixes are unchanged."""
    prose = (
        "The quick brown fox jumps over the lazy dog. "
        "Today's build finished in 42 seconds with 0 failures."
    )
    assert redact(prose) == prose


def test_redact_sk_key_embedded_in_nested_structure() -> None:
    """sk- key inside a neutral-key string nested deep in a dict/list is scrubbed."""
    secret = "sk-ant-api03-ZzYyXxWwVvUuTtSsRrQqPpOo"
    data = {
        "events": [{"type": "message", "content": f"Response contained key {secret} in output"}]
    }
    result = redact(data)
    content = result["events"][0]["content"]
    assert secret not in content
    assert "***REDACTED***" in content


def test_redact_scalar_string_passthrough_unchanged() -> None:
    """Plain scalar strings with no secret content are returned as-is."""
    assert redact("hello world") == "hello world"
    assert redact("gpt-5") == "gpt-5"
    assert redact("openrouter") == "openrouter"
    assert redact("") == ""
