"""Diagnostics regression: preserve transport cause vs HTTP errors on attempts.

Real Quick Research repeatedly raised ``LLMTransientError`` at several
latencies, but existing inspect records keep ``error_class`` only, so a
timeout/transport failure is indistinguishable from an HTTP error. This is a
diagnostics follow-up, not proof of a routing/timeout defect.

Bounded contract under test (content-free seam):

* ``end_attempt`` passes nullable ``http_status`` / ``error_cause_class``
  metadata into ``record_model_attempt``.
* ``http_status`` persists only for ``int`` 100..599 (``bool`` rejected);
  anything else persists as ``None``.
* ``error_cause_class`` persists only as a bounded safe identifier
  (ASCII class identifier, up to 128 chars); malformed input persists
  as ``None``.
* Existing ``error_class`` / ``latency`` / ``outcome`` / ``retry`` values
  stay identical. Raw messages, ``provider_detail``, headers, prompts,
  responses, URLs and credentials are never persisted.
* Unknown/absent attributes stay ``None``. Begin/success/cancel
  observation stays compatible. Observer failures never alter calls.

Provider neutrality (AGENTS.md): behavior must not depend on model,
provider, or endpoint names. Tests use arbitrary identifiers and include a
negative control proving a familiar label gains no implicit behavior.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from disco.core.events import LLMMessage
from disco.core.inspect import record_model_attempt, registry
from disco.core.llm._router_attempts import begin_attempt, end_attempt
from disco.core.llm.errors import LLMTransientError
from disco.core.llm.types import CapabilityProfile, CompletionRequest, ModelRole


def _req() -> CompletionRequest:
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        messages=[LLMMessage(role="user", content="probe")],
    )


def _ctx(cid: str) -> SimpleNamespace:
    return SimpleNamespace(conversation_id=cid)


def _rows(cid: str) -> list[dict]:
    snapshot = registry().snapshot(cid)
    assert snapshot is not None
    return snapshot["model_attempts"]


def _error_row(cid: str) -> dict:
    rows = _rows(cid)
    assert [r["outcome"] for r in rows] == ["started", "error"]
    return rows[1]


class _FakeReadTimeout(Exception):
    pass


class _FakeReadError(Exception):
    pass


def test_http_status_and_cause_survive_end_attempt_to_trace(monkeypatch):
    """Repro: an HTTP failure must keep both status and chained cause."""
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    cid = "diag-http-cause-01"
    attempt = begin_attempt(_req(), "probe-model-a1", "probe-provider-a1", 1, 1, _ctx(cid))

    err = LLMTransientError("http failure")
    err.http_status = 429
    err.__cause__ = _FakeReadError("boom")
    end_attempt(attempt, outcome="error", error=err, retry_scheduled=True)

    row = _error_row(cid)
    assert row["http_status"] == 429
    assert row["error_cause_class"] == "_FakeReadError"
    assert row["error_class"] == "LLMTransientError"
    assert row["retry_scheduled"] is True
    assert row["latency_ms"] is not None and row["latency_ms"] >= 0
    assert "http failure" not in str(row)
    assert "boom" not in str(row)


def test_transport_timeout_without_status_still_records_cause(monkeypatch):
    """Repro: a timeout with no HTTP response keeps status None, cause named."""
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    cid = "diag-transport-cause-01"
    attempt = begin_attempt(_req(), "probe-model-b2", "probe-provider-b2", 1, 1, _ctx(cid))

    err = LLMTransientError("timeout")
    err.http_status = None
    err.__cause__ = _FakeReadTimeout("timed out")
    end_attempt(attempt, outcome="error", error=err, retry_scheduled=True)

    row = _error_row(cid)
    assert row["http_status"] is None
    assert row["error_cause_class"] == "_FakeReadTimeout"
    assert row["error_class"] == "LLMTransientError"
    assert "timed out" not in str(row)


def test_absent_metadata_records_explicit_nones(monkeypatch):
    """Plain failures keep the new keys present but None; old fields unchanged."""
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    cid = "diag-absent-meta-01"
    attempt = begin_attempt(_req(), "probe-model-c3", "probe-provider-c3", 1, 1, _ctx(cid))

    end_attempt(
        attempt,
        outcome="error",
        error=LLMTransientError("plain"),
        retry_scheduled=False,
    )

    row = _error_row(cid)
    assert "http_status" in row and row["http_status"] is None
    assert "error_cause_class" in row and row["error_cause_class"] is None
    assert row["error_class"] == "LLMTransientError"
    assert row["retry_scheduled"] is False
    assert "plain" not in str(row)


@pytest.mark.parametrize(
    "bad_status",
    [True, False, "429", 429.0, None.__class__, 99, 0, 600, 9999, -1, object()],
)
def test_invalid_http_status_normalizes_to_none(monkeypatch, bad_status):
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    cid = f"diag-bad-status-{abs(hash(repr(bad_status))) % 10_000_000}"
    attempt = begin_attempt(_req(), "probe-model-d4", "probe-provider-d4", 1, 1, _ctx(cid))

    err = LLMTransientError("bad status shape")
    err.http_status = bad_status  # type: ignore[assignment]
    end_attempt(attempt, outcome="error", error=err, retry_scheduled=True)

    row = _error_row(cid)
    assert row["http_status"] is None
    assert row["error_class"] == "LLMTransientError"


@pytest.mark.parametrize("good_status", [100, 200, 429, 500, 599])
def test_http_status_bounds_persist(monkeypatch, good_status):
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    cid = f"diag-good-status-{good_status}"
    record_model_attempt(
        cid,
        stage="router",
        attempt=1,
        provider="probe-provider-e5",
        model="probe-model-e5",
        outcome="error",
        error_class="LLMTransientError",
        http_status=good_status,
        error_cause_class="_FakeReadError",
    )
    row = _rows(cid)[0]
    assert row["http_status"] == good_status
    assert row["error_cause_class"] == "_FakeReadError"


@pytest.mark.parametrize(
    "bad_status",
    [True, False, "500", 500.0, 99, 600, 0, -200, 10_000, None.__class__],
)
def test_seam_rejects_invalid_http_status_primitives(monkeypatch, bad_status):
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    cid = f"diag-seam-status-{abs(hash(repr(bad_status))) % 10_000_000}"
    record_model_attempt(
        cid,
        stage="router",
        attempt=1,
        provider="probe-provider-f6",
        model="probe-model-f6",
        outcome="error",
        error_class="LLMTransientError",
        http_status=bad_status,
        error_cause_class="_FakeReadError",
    )
    row = _rows(cid)[0]
    assert row["http_status"] is None
    # The valid sibling field is untouched by the invalid neighbor.
    assert row["error_cause_class"] == "_FakeReadError"


@pytest.mark.parametrize(
    "bad_cause",
    [
        "",
        "has space",
        "has-dash",
        "https://example.invalid/x",
        "sk-test-secret-value",
        "a" * 129,
        "Tïmeout",
        "123Start",
        "name;DROP",
        123,
        True,
    ],
)
def test_seam_rejects_malformed_cause_class(monkeypatch, bad_cause):
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    cid = f"diag-seam-cause-{abs(hash(repr(bad_cause))) % 10_000_000}"
    record_model_attempt(
        cid,
        stage="router",
        attempt=1,
        provider="probe-provider-g7",
        model="probe-model-g7",
        outcome="error",
        error_class="LLMTransientError",
        http_status=503,
        error_cause_class=bad_cause,  # type: ignore[arg-type]
    )
    row = _rows(cid)[0]
    assert row["error_cause_class"] is None
    assert row["http_status"] == 503


def test_malicious_cause_name_and_secrets_never_persisted(monkeypatch):
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    cid = "diag-secret-scrub-01"
    attempt = begin_attempt(_req(), "probe-model-h8", "probe-provider-h8", 1, 1, _ctx(cid))

    secret = "sk-test-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    url = "https://example.invalid/callback?token=abc123"
    err = LLMTransientError(f"detail {secret} {url}")
    err.http_status = 500
    err.provider_detail = f"credential {secret} {url}"  # type: ignore[attr-defined]
    EvilCause = type("evil payload https://example.invalid " + secret, (Exception,), {})
    err.__cause__ = EvilCause(f"raw {secret} {url}")
    end_attempt(attempt, outcome="error", error=err, retry_scheduled=True)

    row = _error_row(cid)
    assert row["error_cause_class"] is None
    assert row["http_status"] == 500
    blob = str(row)
    assert secret not in blob
    assert url not in blob
    assert "credential" not in blob
    assert "provider_detail" not in blob
    assert "prompt" not in row and "response" not in row and "headers" not in row


def test_success_and_cancel_keep_null_diagnostics(monkeypatch):
    import asyncio

    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()

    ok = begin_attempt(_req(), "probe-model-i9", "probe-provider-i9", 1, 1, _ctx("diag-success-01"))
    end_attempt(ok, outcome="success", error=None, retry_scheduled=False)
    success_rows = _rows("diag-success-01")
    assert [r["outcome"] for r in success_rows] == ["started", "success"]
    assert success_rows[1]["error_class"] is None
    assert success_rows[1]["http_status"] is None
    assert success_rows[1]["error_cause_class"] is None

    cancelled = begin_attempt(
        _req(), "probe-model-i9", "probe-provider-i9", 1, 1, _ctx("diag-cancel-01")
    )
    end_attempt(
        cancelled,
        outcome="cancelled",
        error=asyncio.CancelledError(),
        retry_scheduled=False,
    )
    cancel_rows = _rows("diag-cancel-01")
    assert [r["outcome"] for r in cancel_rows] == ["started", "cancelled"]
    assert cancel_rows[1]["error_class"] == "CancelledError"
    assert cancel_rows[1]["http_status"] is None
    assert cancel_rows[1]["error_cause_class"] is None


def test_observer_failure_never_alters_call(monkeypatch):
    """Inspect is passive: a registry failure must not raise or change outcome."""
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()

    def _boom(*args, **kwargs):
        raise RuntimeError("observer down")

    monkeypatch.setattr(registry(), "add", _boom)
    attempt = begin_attempt(
        _req(), "probe-model-j10", "probe-provider-j10", 1, 1, _ctx("diag-boom-01")
    )
    # Must not raise even though the "started" record failed inside begin_attempt.
    assert attempt.attempt == 1
    # Must not raise, and must not change retry classification handling here.
    end_attempt(attempt, outcome="error", error=LLMTransientError("x"), retry_scheduled=True)


def test_provider_labels_do_not_change_diagnostics(monkeypatch):
    """Negative control: a familiar label gains no implicit behavior."""
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()

    for cid, provider, model in [
        ("diag-neutral-familiar-01", "openai", "gpt-4o"),
        ("diag-neutral-arbitrary-01", "probe-provider-k11", "probe-model-k11"),
    ]:
        attempt = begin_attempt(_req(), model, provider, 1, 1, _ctx(cid))
        err = LLMTransientError("neutral")
        err.http_status = 503
        err.__cause__ = _FakeReadTimeout("t")
        end_attempt(attempt, outcome="error", error=err, retry_scheduled=True)

    familiar = _error_row("diag-neutral-familiar-01")
    arbitrary = _error_row("diag-neutral-arbitrary-01")
    assert familiar["http_status"] == arbitrary["http_status"] == 503
    assert familiar["error_cause_class"] == arbitrary["error_cause_class"] == "_FakeReadTimeout"
    assert familiar["error_class"] == arbitrary["error_class"] == "LLMTransientError"
