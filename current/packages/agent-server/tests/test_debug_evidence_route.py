"""Tests for GET /api/debug/evidence/{conversation_id} — evidence-harness-campaign.md W5.

Three assertions:

1. Route is inert (404) when ``DISCO_INSPECT`` is off — the endpoint must never
   expose conversation internals in a default deployment.
2. When ``DISCO_INSPECT=1``, the endpoint returns the expected four-key evidence
   bundle (state / events / inspect_trace / project_manifest).
3. A secret planted in an event's ``meta`` dict is replaced with
   ``'***REDACTED***'`` in the response — keys never leak through this surface.

All tests drive the real ASGI app via ``create_app`` + ``TestClient``, matching
the pattern established by ``test_health.py`` and ``test_activity.py``.
"""

from __future__ import annotations

import asyncio
import json

from disco.agent_server import create_app
from disco.core import EventSource, LLMMessage, MessageEvent, SqliteEventStore
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seeded_store(cid: str, *, meta: dict | None = None) -> SqliteEventStore:
    """Return an in-memory store pre-populated with *cid* and one user message.

    ``meta`` is forwarded to the event's ``meta`` dict so tests can plant
    secrets there.  The ``_write_lock`` is reset after the synchronous
    bootstrap so TestClient's internal event loop can acquire it cleanly."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(cid, owner_id="local")
    event = MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(role="user", content="hello"),
        meta=meta or {},
    )
    # store.append is async; asyncio.run bootstraps a temporary loop.
    asyncio.run(store.append(cid, event))
    # The write-lock was acquired by the temporary loop above.  Replace it so
    # TestClient's ASGI event loop can acquire it without a "wrong loop" error.
    store._write_lock = asyncio.Lock()
    return store


# ---------------------------------------------------------------------------
# 1. Flag off — route is inert
# ---------------------------------------------------------------------------


def test_evidence_inert_when_inspect_off(monkeypatch: object) -> None:
    """Without DISCO_INSPECT=1, the route returns 404 with a hint, never data."""
    # Strip both the new and legacy env vars so inspect_enabled() is False.
    monkeypatch.delenv("DISCO_INSPECT", raising=False)  # type: ignore[attr-defined]
    monkeypatch.delenv("PMX_INSPECT", raising=False)  # type: ignore[attr-defined]

    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store, runtime=None))
    resp = client.get("/api/debug/evidence/any-cid")

    assert resp.status_code == 404
    body = resp.json()
    assert body.get("error") == "inspect disabled"
    # The hint must name the flag so an operator knows exactly what to enable.
    assert "DISCO_INSPECT=1" in body.get("hint", "")


# ---------------------------------------------------------------------------
# 2. Flag on — returns the four-key bundle
# ---------------------------------------------------------------------------


def test_evidence_returns_bundle_when_inspect_on(monkeypatch: object) -> None:
    """With DISCO_INSPECT=1, the endpoint returns the full evidence bundle."""
    monkeypatch.setenv("DISCO_INSPECT", "1")  # type: ignore[attr-defined]

    cid = "conv_ev_bundle_01"
    store = _seeded_store(cid)
    client = TestClient(create_app(store, runtime=None))
    resp = client.get(f"/api/debug/evidence/{cid}")

    assert resp.status_code == 200
    body = resp.json()

    # Top-level keys the harness depends on.
    assert body["conversation_id"] == cid
    assert "state" in body, "bundle must include conversation state"
    assert "events" in body, "bundle must include the event log"
    assert "inspect_trace" in body, "bundle must include the inspect trace (may be null)"
    assert "project_manifest" in body, "bundle must include the project manifest (may be null)"
    assert "runtime" in body, "bundle must include runtime cleanup evidence (may be null)"
    assert body["runtime"] is None

    # The seeded user message must appear in the event list.
    assert isinstance(body["events"], list)
    assert len(body["events"]) >= 1, "at least the seeded event must be present"


# ---------------------------------------------------------------------------
# 3. Planted secret is REDACTED
# ---------------------------------------------------------------------------


def test_evidence_redacts_secret_in_event_meta(monkeypatch: object) -> None:
    """A secret key in an event's meta is replaced; the raw value must not leak."""
    monkeypatch.setenv("DISCO_INSPECT", "1")  # type: ignore[attr-defined]

    secret_value = "sk-super-secret-9999"
    cid = "conv_ev_secret_01"
    store = _seeded_store(cid, meta={"api_key": secret_value})
    client = TestClient(create_app(store, runtime=None))
    resp = client.get(f"/api/debug/evidence/{cid}")

    assert resp.status_code == 200
    body = resp.json()

    # The raw secret must not appear anywhere in the serialised response body.
    raw_response = json.dumps(body)
    assert secret_value not in raw_response, (
        f"secret '{secret_value}' must not appear in the response"
    )

    # The redaction marker must be present at the correct location.
    events = body["events"]
    assert len(events) >= 1
    assert events[0]["meta"]["api_key"] == "***REDACTED***", (
        "api_key in event meta must be replaced with ***REDACTED***"
    )


# ---------------------------------------------------------------------------
# 4. Trace route is also redacted
# ---------------------------------------------------------------------------


def test_trace_route_redacts_secret(monkeypatch: object) -> None:
    """A secret field planted in the inspect trace must be redacted by
    GET /api/debug/trace/{conversation_id} — the legacy route must apply
    the same redact() pass as the newer /api/debug/evidence endpoint."""
    monkeypatch.setenv("DISCO_INSPECT", "1")  # type: ignore[attr-defined]

    from disco.core.inspect import registry

    cid = "conv_trace_secret_01"
    secret_value = "sk-trace-secret-9999"

    # Plant a routing trace entry that carries a sensitive field.
    reg = registry()
    reg.clear()
    reg.add(cid, "routing", {"api_key": secret_value, "chosen_model": "test-model"})

    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store, runtime=None))

    resp = client.get(f"/api/debug/trace/{cid}")
    assert resp.status_code == 200

    raw_response = json.dumps(resp.json())
    assert secret_value not in raw_response, (
        f"secret '{secret_value}' must not appear in the raw trace response"
    )
    assert "***REDACTED***" in raw_response, (
        "redaction marker must be present in the trace response"
    )
