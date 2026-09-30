"""Trace-only token-usage observability contract for ``record_model_io``.

Baseline (proven actual): ``usage={"input_tokens": 123, "output_tokens": 0,
"cached_tokens": 45}`` is stored as ``[REDACTED]`` per field. Telemetry
contract says numeric token counts are not secrets, so the three exact
``TokenUsage`` keys must preserve plain nonnegative ints (including true
zero) when they appear in the *direct* ``usage`` dictionary only.

Scope is intentionally narrow: no budgeting/model-request change, no latency
causal inference. All other redaction behaviour stays intact.
"""

from __future__ import annotations

import pytest
from disco.core import inspect as inspect_mod
from disco.core.inspect import InspectRegistry, record_model_io

CID = "test-inspect-token-usage-cid"


@pytest.fixture
def fresh_registry(monkeypatch):
    """Hermetic registry: fresh ring, no journal, no global leakage."""
    monkeypatch.setenv("DISCO_INSPECT", "1")
    reg = InspectRegistry()
    monkeypatch.setattr(inspect_mod, "_REGISTRY", reg)
    return reg


def _last_model_io(reg, cid=CID):
    snap = reg.snapshot(cid)
    assert snap is not None
    assert snap["model_io"], "expected one model_io event"
    return snap["model_io"][-1]


def _record(usage, **kwargs):
    kwargs.setdefault("model", "synthetic-model")
    kwargs.setdefault("role", "agent_driver")
    record_model_io(CID, usage=usage, **kwargs)


# ---- failing-positive: genuine usage counts must stay plain ints ----


def test_usage_counts_preserve_nonnegative_ints_including_true_zero(fresh_registry):
    _record({"input_tokens": 123, "output_tokens": 0, "cached_tokens": 45})
    stored = _last_model_io(fresh_registry)["usage"]
    assert stored["input_tokens"] == 123
    assert stored["output_tokens"] == 0
    assert stored["cached_tokens"] == 45
    assert type(stored["input_tokens"]) is int
    assert type(stored["output_tokens"]) is int
    assert type(stored["cached_tokens"]) is int


def test_usage_none_and_missing_fields_stay_distinct(fresh_registry):
    _record(None)
    assert _last_model_io(fresh_registry)["usage"] is None

    reg2_cid = CID + "-missing"
    record_model_io(reg2_cid, model="synthetic-model", usage={"input_tokens": 7})
    stored = fresh_registry.snapshot(reg2_cid)["model_io"][-1]["usage"]
    assert stored["input_tokens"] == 7
    assert "output_tokens" not in stored
    assert "cached_tokens" not in stored


def test_other_numeric_metadata_unchanged(fresh_registry):
    record_model_io(
        CID,
        model="synthetic-model",
        attempt=2,
        latency_ms=77,
        usage={"input_tokens": 1, "output_tokens": 2, "cached_tokens": 3, "cost_usd": 0.25},
    )
    stored = _last_model_io(fresh_registry)
    assert stored["attempt"] == 2
    assert stored["latency_ms"] == 77
    assert stored["usage"]["cost_usd"] == 0.25


# ---- type controls: only plain nonnegative ints pass under exact keys ----


@pytest.mark.parametrize("key", ["input_tokens", "output_tokens", "cached_tokens"])
@pytest.mark.parametrize("bad", [True, False, "123", 12.5, -1, -99, {"n": 1}, [1], None])
def test_non_genuine_int_usage_values_stay_redacted(fresh_registry, key, bad):
    _record({key: bad})
    stored = _last_model_io(fresh_registry)["usage"]
    assert stored[key] == "[REDACTED]"
    assert stored[key] != 0


def test_cached_none_not_coerced_to_zero(fresh_registry):
    # llm/types.py TokenUsage.cached_tokens is `int = 0` (not Optional), so
    # None is not a genuine count: it must not become 0.
    _record({"input_tokens": 1, "output_tokens": 2, "cached_tokens": None})
    stored = _last_model_io(fresh_registry)["usage"]
    assert stored["cached_tokens"] == "[REDACTED]"
    assert stored["cached_tokens"] != 0


# ---- scope controls: exception applies ONLY to direct usage dict ----


def test_usage_exception_does_not_leak_to_request_response(fresh_registry):
    record_model_io(
        CID,
        model="synthetic-model",
        request={"input_tokens": 123, "content": "hello world"},
        response={"output_tokens": 45, "content": "done"},
        usage={"input_tokens": 123, "output_tokens": 45, "cached_tokens": 0},
    )
    stored = _last_model_io(fresh_registry)
    assert stored["request"]["input_tokens"] == "[REDACTED]"
    assert stored["response"]["output_tokens"] == "[REDACTED]"
    assert stored["request"]["content"] == "hello world"
    assert stored["response"]["content"] == "done"


def test_unknown_token_keys_remain_redacted(fresh_registry):
    _record({"input_tokens": 1, "total_tokens": 10, "my_tokens": 5})
    stored = _last_model_io(fresh_registry)["usage"]
    assert stored["total_tokens"] == "[REDACTED]"
    assert stored["my_tokens"] == "[REDACTED]"


def test_nested_secret_container_under_usage_key_does_not_pass(fresh_registry):
    _record({"input_tokens": {"token": "synthetic-nested"}, "output_tokens": 1})
    stored = _last_model_io(fresh_registry)["usage"]
    assert stored["input_tokens"] == "[REDACTED]"


# ---- privacy controls: secrets stay redacted even when numeric ----


@pytest.mark.parametrize(
    "key", ["token", "access_token", "api_key", "Authorization", "cookie", "password"]
)
def test_secret_fields_remain_redacted_even_when_numeric(fresh_registry, key):
    record_model_io(
        CID,
        model="synthetic-model",
        request={key: 12345},
        usage={"input_tokens": 1, "output_tokens": 2, "cached_tokens": 0, key: 12345},
    )
    stored = _last_model_io(fresh_registry)
    assert stored["request"][key] == "[REDACTED]"
    assert stored["usage"][key] == "[REDACTED]"


def test_prompt_response_secret_scrubbing_and_ordinary_content(fresh_registry):
    secret = "bearer synthetic-test-value-abcdef1234567890"
    record_model_io(
        CID,
        model="synthetic-model",
        request={"content": "hello world planner"},
        response={"content": f"result {secret} end"},
        usage={"input_tokens": 3, "output_tokens": 4, "cached_tokens": 0},
    )
    stored = _last_model_io(fresh_registry)
    assert stored["request"]["content"] == "hello world planner"
    assert secret not in stored["response"]["content"]
    assert "[REDACTED]" in stored["response"]["content"]
    assert "result " in stored["response"]["content"]


def test_bounded_payload(fresh_registry):
    big = "A" * 20000
    record_model_io(
        CID,
        model="synthetic-model",
        request={"content": big},
        usage={"input_tokens": 1, "output_tokens": 1, "cached_tokens": 0},
    )
    stored = _last_model_io(fresh_registry)
    content = stored["request"]["content"]
    assert len(content) < len(big)
    assert "[TRUNCATED" in content


def test_capture_disabled_records_nothing(monkeypatch):
    monkeypatch.delenv("DISCO_INSPECT", raising=False)
    monkeypatch.setenv("DISCO_INSPECT", "0")
    reg = InspectRegistry()
    monkeypatch.setattr(inspect_mod, "_REGISTRY", reg)
    record_model_io(CID, model="synthetic-model", usage={"input_tokens": 1})
    assert reg.snapshot(CID) is None
