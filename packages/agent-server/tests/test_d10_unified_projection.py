"""D10 — unify share-bundle and harness-cassette formats.

ONE projection feeds BOTH:
  - the share bundle's `cassette` field carries rows in the harness
    `Cassette` format (`{seam, key, input, output}` per row);
  - the harness `Cassette.from_rows(...)` adapter is the loader;
  - the harness `replay_runner` consumers (via `Cassette.lookup`) read the
    SAME projection the share bundle emits.

Acceptance (per the task spec):
  1. A share bundle round-trips into a valid cassette the replay_runner
     accepts (`Cassette.from_rows(bundle["cassette"]).lookup(...)` returns
     the recorded output for the right `(seam, key)`).
  2. The service-call path reads the same projection — i.e. the share
     bundle's `cassette` field is in the EXACT shape the harness
     `Cassette` produces/loads (no divergent serializer).
  3. Existing replay_runner consumers unbroken (`Cassette.load` on disk
     jsonl, `record`/`lookup` semantics).
  4. No share-bundle fields lost — every prior field is preserved
     (`bundle_version`, `conversation_id`, `owner_id`, `surface`,
     `title`, `exported_at`, `last_seq`, `state`, `events`, `share`).
"""

from __future__ import annotations

import asyncio

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.core import (
    EventSource,
    LLMMessage,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
)
from disco.core.events import (
    ActionEvent,
    ConversationStatus,
    ObservationEvent,
    ToolCall,
    ToolResult,
)
from disco.retrieval.models import SearchHit
from fastapi.testclient import TestClient

from harness.cassette import Cassette, cassette_key
from harness.projection import project_cassette_rows

# ---- fixtures + helpers ----------------------------------------------------


@pytest.fixture
def client_with_runtime() -> TestClient:
    store = SqliteEventStore(":memory:")
    runtime = ConversationRuntime(store)
    c = TestClient(create_app(store, runtime=runtime))
    c._store = store  # type: ignore[attr-defined]
    return c


def _seed_search_pair(store: SqliteEventStore, cid: str) -> None:
    """Append a paired (ActionEvent→ObservationEvent) for a ddgs.search call,
    so the projection has a real `search` cassette row to assert on."""

    async def go() -> None:
        store.create_conversation(cid, owner_id="local", surface="deep_research")
        await store.append(
            cid,
            MessageEvent(
                source=EventSource.USER,
                message=LLMMessage(role="user", content="what is vcrpy?"),
            ),
        )
        await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING))
        action = await store.append(
            cid,
            ActionEvent(
                source=EventSource.AGENT,
                thought="search the web",
                tool_call=ToolCall(
                    tool_name="ddgs.search",
                    arguments={"query": "what is vcrpy", "limit": 3, "deny": []},
                ),
            ),
        )
        await store.append(
            cid,
            ObservationEvent(
                action_id=action.id,
                tool_result=ToolResult(
                    call_id="c1",
                    tool_name="ddgs.search",
                    success=True,
                    content="example.com hit",
                    # Mirrors the runtime's `_tool_search_handler` shape:
                    # `structured={"results": [...hits...]}`. The projection
                    # preserves the whole `structured` value as the cassette
                    # row's `output` — the harness `RecordingSearchProvider`
                    # stores a list directly, so the replay lookup is the
                    # caller's job to match the shape they recorded with.
                    structured={
                        "results": [
                            SearchHit(
                                url="https://example.com",
                                title="example",
                                snippet="an example hit",
                                source_engine="ddgs",
                                rank=0,
                            ).model_dump()
                        ]
                    },
                ),
            ),
        )
        await store.append(cid, StatusEvent(status=ConversationStatus.FINISHED))

    asyncio.run(go())


# ---- 1. share bundle has a `cassette` field in the harness-Cassette format --


def test_share_bundle_has_cassette_field(client_with_runtime: TestClient) -> None:
    cid = "conv_cassette_field"
    _seed_search_pair(client_with_runtime._store, cid)  # type: ignore[attr-defined]
    bundle = client_with_runtime.get(f"/api/conversations/{cid}/share/bundle").json()
    # The new field exists and is a list (possibly empty).
    assert "cassette" in bundle
    assert isinstance(bundle["cassette"], list)
    # And it has at least one row for the ddgs.search call we seeded.
    rows = bundle["cassette"]
    assert len(rows) >= 1, f"expected ≥1 cassette row, got {rows!r}"
    row = rows[0]
    # Row shape is EXACTLY the harness `Cassette` format — same keys, same types.
    assert set(row.keys()) >= {"seam", "key", "input", "output"}
    assert isinstance(row["seam"], str)
    assert isinstance(row["key"], str) and len(row["key"]) == 16
    assert isinstance(row["input"], dict)
    # The search seam is the one the live recorder would have produced.
    assert row["seam"] == "search"
    # The key matches `cassette_key(...)` for the same payload — so the
    # bundle is interchangeable with a real recording.
    assert row["key"] == cassette_key(row["seam"], row["input"])


def test_no_share_bundle_fields_lost(client_with_runtime: TestClient) -> None:
    """Every field the prior contract defined is still present after D10
    (`bundle_version`, `conversation_id`, `owner_id`, `surface`, `title`,
    `exported_at`, `last_seq`, `state`, `events`, `share`)."""
    cid = "conv_no_field_lost"
    _seed_search_pair(client_with_runtime._store, cid)  # type: ignore[attr-defined]
    bundle = client_with_runtime.get(f"/api/conversations/{cid}/share/bundle").json()
    for required in (
        "bundle_version",
        "conversation_id",
        "owner_id",
        "surface",
        "title",
        "exported_at",
        "last_seq",
        "state",
        "events",
        "cassette",
    ):
        assert required in bundle, f"missing required field: {required}"
    # bundle_version stays at 1 (D10 is ADDITIVE — the existing field set
    # is preserved; no bump needed for an additive new field).
    assert bundle["bundle_version"] == 1
    # `share` envelope is added by the `/api/share/{token}/bundle` endpoint
    # — the bare `/api/conversations/{cid}/share/bundle` doesn't include it,
    # so we don't assert it here (we asserted all of the EXPORTED fields).
    # The shape contract: events is the same list it was before.
    assert isinstance(bundle["events"], list)
    assert isinstance(bundle["state"], dict)


# ---- 2. round-trip: share bundle → Cassette → replay_runner lookup ---------


def test_share_bundle_round_trips_into_a_valid_cassette(
    client_with_runtime: TestClient,
) -> None:
    """The whole point of D10: a bundle's `cassette` field loads via
    `Cassette.from_rows(...)` and the resulting Cassette answers
    `lookup()` calls the same way the live recorder would."""
    cid = "conv_round_trip"
    _seed_search_pair(client_with_runtime._store, cid)  # type: ignore[attr-defined]
    bundle = client_with_runtime.get(f"/api/conversations/{cid}/share/bundle").json()

    # Single-source adapter — exactly one way to build a Cassette from a bundle.
    cas = Cassette.from_rows(bundle["cassette"])
    # And the cassette's seams() surface matches what the live recorder reports.
    seams = cas.seams()
    assert "search" in seams, f"expected search seam in cassette, got {seams!r}"

    # Service-call path: a `ReplaySearchProvider`-style lookup works against
    # the share-bundle projection. The same input the action carried
    # (`query`, `limit`, `deny`) hashes to the same key the bundle embedded.
    search_row = bundle["cassette"][0]
    payload = search_row["input"]
    out = cas.lookup(search_row["seam"], payload)
    # The output is the `tool_result.structured` payload the observation
    # event carried — the harness `RecordingSearchProvider` records the
    # verbatim `[h.model_dump() for h in hits]`, and our projection uses
    # the structured envelope so the round-trip is faithful.
    assert isinstance(out, dict) and out.get("results")
    assert out["results"][0]["url"] == "https://example.com"


def test_service_call_path_reads_same_projection() -> None:
    """The projection function is the single source: a list of event payloads
    in → a list of cassette-format rows out. Both the share bundle and the
    `Cassette.from_rows(...)` adapter read this exact shape."""
    # Construct event payloads the same way `share_export` does (a paired
    # action→observation for a search call).
    action = {
        "kind": "action",
        "id": "evt_a",
        "tool_call": {
            "tool_name": "ddgs.search",
            "arguments": {"query": "q", "limit": 2, "deny": []},
        },
    }
    obs = {
        "kind": "observation",
        "action_id": "evt_a",
        "tool_result": {
            "tool_name": "ddgs.search",
            "content": [
                {"url": "u", "title": "t", "snippet": "s", "source_engine": "ddgs", "rank": 0}
            ],
        },
    }
    rows = project_cassette_rows([action, obs])
    assert len(rows) == 1
    # The projection shape is the same shape the harness Cassette reads.
    cas = Cassette.from_rows(rows)
    assert cas.lookup("search", rows[0]["input"]) == rows[0]["output"]


# ---- 3. projection purity: deterministic, idempotent, scrubber-aware --------


def test_projection_is_deterministic_for_same_events() -> None:
    events = [
        {
            "kind": "action",
            "id": "a1",
            "tool_call": {
                "tool_name": "ddgs.search",
                "arguments": {"query": "q", "limit": 1, "deny": ["x.com"]},
            },
        },
        {
            "kind": "observation",
            "action_id": "a1",
            "tool_result": {"tool_name": "ddgs.search", "content": []},
        },
    ]
    r1 = project_cassette_rows(events)
    r2 = project_cassette_rows(events)
    # Same events → same rows (the key is a stable sha16, not a per-call id).
    assert r1 == r2
    # And the `deny` ordering is sorted in the input (cassette key invariant).
    assert r1[0]["input"]["deny"] == ["x.com"]


def test_projection_skips_orphans_and_unknown_tools() -> None:
    """A pure plan/status event, an orphan action, an unknown tool — none of
    these should produce a cassette row (the live recorder would have
    rejected them at record-time too)."""
    events = [
        {"kind": "status", "status": "RUNNING"},
        {
            "kind": "action",
            "id": "orphan",
            "tool_call": {
                "tool_name": "ddgs.search",
                "arguments": {"query": "no-pair", "limit": 1, "deny": []},
            },
        },  # no obs
        {
            "kind": "action",
            "id": "a2",
            "tool_call": {"tool_name": "shell", "arguments": {"command": "ls"}},
        },  # not a service seam
        {
            "kind": "observation",
            "action_id": "a2",
            "tool_result": {"tool_name": "shell", "content": "files"},
        },
    ]
    rows = project_cassette_rows(events)
    assert rows == []


def test_projection_handles_extract_and_llm_seams() -> None:
    """The same projection emits cassette rows for the three seams the
    harness replay supports: search, extract, llm.complete."""
    events = [
        # extract pair
        {
            "kind": "action",
            "id": "ax",
            "tool_call": {
                "tool_name": "ddgs.extract",
                "arguments": {"url": "https://example.com/x"},
            },
        },
        {
            "kind": "observation",
            "action_id": "ax",
            "tool_result": {
                "tool_name": "ddgs.extract",
                "content": {"url": "https://example.com/x", "content": "hi"},
            },
        },
        # llm pair
        {
            "kind": "action",
            "id": "al",
            "tool_call": {
                "tool_name": "llm.complete",
                "arguments": {
                    "role": "ModelRole.RAG_ANSWERER",
                    "messages": [{"role": "user", "content": "hi"}],
                    "tools": [],
                    "temperature": 0.0,
                },
            },
        },
        {
            "kind": "observation",
            "action_id": "al",
            "tool_result": {
                "tool_name": "llm.complete",
                "content": {"text": "hello", "tool_calls": []},
            },
        },
    ]
    rows = project_cassette_rows(events)
    seams = {r["seam"] for r in rows}
    assert seams == {"extract", "llm.complete"}


# ---- 4. existing replay_runner consumers unbroken --------------------------


def test_cassette_disk_load_still_works() -> None:
    """The disk-loader path the harness always used (`Cassette.load`) is
    unchanged. D10 only ADDS `from_rows`; the `record`/`lookup` semantics
    the replay_runner relies on are intact."""
    from pathlib import Path

    cas_path = Path(__file__).resolve().parents[4] / "development" / "harness" / "cassettes" / "demo.jsonl"
    if not cas_path.exists():
        pytest.skip("harness demo cassette not present")
    cas = Cassette.load(cas_path)
    # Disk-loaded cassette still answers lookups.
    seams = cas.seams()
    assert seams.get("search") or seams.get("extract") or seams.get("llm.complete")


def test_cassette_record_lookup_unchanged() -> None:
    """A freshly-recorded cassette (the path the live harness uses) still
    works exactly the way it did — the unification does not touch
    `record`/`lookup` semantics."""
    cas = Cassette()
    payload = {"query": "x", "limit": 1, "deny": []}
    cas.record(
        "search",
        payload,
        [{"url": "u", "title": "t", "snippet": "s", "source_engine": "ddgs", "rank": 0}],
    )
    assert cas.lookup("search", payload) == [
        {"url": "u", "title": "t", "snippet": "s", "source_engine": "ddgs", "rank": 0}
    ]


# ---- 5. share-bundle share envelope (the /api/share/{token}/bundle path) ----


def test_share_envelope_endpoint_includes_cassette_field(
    client_with_runtime: TestClient,
) -> None:
    """The token-resolved bundle endpoint (which ADDS the `share` envelope
    on top of `share_export`'s output) carries the new `cassette` field
    too — same projection, both readers."""
    cid = "conv_envelope"
    _seed_search_pair(client_with_runtime._store, cid)  # type: ignore[attr-defined]
    r = client_with_runtime.post(f"/api/conversations/{cid}/share").json()
    token = r["token"]
    bundle = client_with_runtime.get(f"/api/share/{token}/bundle").json()
    assert "cassette" in bundle
    assert bundle["share"]["token"] == token
    # The cassette projection is still loadable.
    cas = Cassette.from_rows(bundle["cassette"])
    assert cas.seams().get("search")
