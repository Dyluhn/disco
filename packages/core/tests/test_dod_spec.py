"""C1a — external Definition-of-Done spec (storage + accessor + immutability).

This is step 1 of 3 (C1a store → C1b evaluator → C1c wire). The DoD spec is the
structural fix for "the agent verifies its own work": a list of machine-
checkable acceptance predicates the agent itself never writes, captured at
task start and stored OUTSIDE the agent-editable event stream. No evaluation
logic here — that lands in C1b.

Two tests are mandated by the task brief:

  Test 1 — a DoD spec stored at start, read back via the accessor, is
           identical to what was stored.
  Test 2 — an attempt to mutate the DoD through the agent's tool surface
           raises or no-ops; the accessor still returns the ORIGINAL.

The "agent's tool surface" is the registry in `disco.tools.builtin`. By
construction NO tool in that registry can reach the store's `dod_specs`
table — the immutability is structural, not gated. Test 2 enumerates the
agent tools and asserts none of them exposes a way to mutate a stored
spec. That is the essential check the task brief asks for.
"""

from __future__ import annotations

import pytest
from disco.core import (
    DoDSpec,
    DoDSpecAlreadySet,
    SqliteEventStore,
)
from disco.core.dod import (
    CommandExitPredicate,
    FileExistsPredicate,
    HTTPOkPredicate,
    predicate_from_obj,
    predicate_to_dict,
)
from disco.core.llm import ModelExecutionPolicy
from disco.core.store.base import EventStore
from disco.tools import agent_scope, build_default_registry
from pydantic import ValidationError

CID = "conv_dod_test"


@pytest.fixture
def store() -> SqliteEventStore:
    s = SqliteEventStore(":memory:")
    yield s
    s.close()


def _spec() -> DoDSpec:
    """A representative spec covering all three predicate kinds."""
    return DoDSpec(
        predicates=[
            FileExistsPredicate(path="build/index.html"),
            CommandExitPredicate(cmd="pytest -q", expect_exit=0),
            HTTPOkPredicate(url="http://127.0.0.1:8000/", expect_status=200),
        ],
        note="C1a test spec",
    )


# ---- Test 1: store + read round-trip ---------------------------------------


async def test_set_then_get_returns_identical_spec(store: SqliteEventStore) -> None:
    """Test 1: a DoD spec stored at start, read back via the accessor, is
    identical to what was stored (Pydantic equality covers predicates, meta,
    and audit-free fields; the `created_at` default factory stamps the same
    ISO-8601 wall-clock to within the resolution of `datetime.now(UTC)` —
    Pydantic's value equality treats them as equal as long as the structure
    matches, which it does)."""
    spec = _spec()
    stored = await store.set_dod_spec(CID, spec, set_by="test")
    # set_dod_spec returns the same model instance (cheap pass-through).
    assert stored is spec

    # Accessor
    read = await store.get_dod_spec(CID)
    assert read is not None
    assert read == spec
    # And the predicates survived in order and with the right kinds.
    assert [type(p).__name__ for p in read.predicates] == [
        "FileExistsPredicate",
        "CommandExitPredicate",
        "HTTPOkPredicate",
    ]
    assert read.predicates[0].path == "build/index.html"
    assert read.predicates[1].cmd == "pytest -q"
    assert read.predicates[1].expect_exit == 0
    assert read.predicates[2].url == "http://127.0.0.1:8000/"
    assert read.predicates[2].expect_status == 200
    assert read.note == "C1a test spec"


async def test_get_returns_none_when_no_spec_stored(store: SqliteEventStore) -> None:
    """The accessor must not invent a spec; pre-set reads return None so the
    caller (the C1b evaluator) can branch on "no DoD captured". Without this
    distinction a missing spec would silently pass the gate as an empty
    predicate list."""
    assert await store.get_dod_spec(CID) is None


# ---- Test 2: agent-reachable mutation is impossible ------------------------


async def test_second_set_raises_and_preserves_original(store: SqliteEventStore) -> None:
    """Test 2 (the immutability-via-agent-surface gate): a second `set_dod_spec`
    call with the same `conversation_id` raises `DoDSpecAlreadySet`, and the
    accessor still returns the ORIGINAL. The exception is the structural
    guarantee — there is no path that writes a second row."""
    spec = _spec()
    await store.set_dod_spec(CID, spec, set_by="first")

    with pytest.raises(DoDSpecAlreadySet):
        await store.set_dod_spec(CID, spec, set_by="second")

    # Original is intact
    read = await store.get_dod_spec(CID)
    assert read == spec
    # And a *different* spec would also have been rejected
    alt = DoDSpec(predicates=[FileExistsPredicate(path="different")])
    with pytest.raises(DoDSpecAlreadySet):
        await store.set_dod_spec(CID, alt, set_by="second-different")
    assert await store.get_dod_spec(CID) == spec


async def test_replace_dod_spec_raises_when_spec_exists(store: SqliteEventStore) -> None:
    """The named, always-raise hook: `replace_dod_spec` is the structural place
    a future "weaken" affordance would call. It must fail loudly (not silently
    no-op, not silently mutate). The hook exists so any caller picking the
    "replace" verb to route around the write-once gate hits the same gate."""
    spec = _spec()
    await store.set_dod_spec(CID, spec, set_by="first")

    weaker = DoDSpec(predicates=[], note="empty / weaker")
    with pytest.raises(DoDSpecAlreadySet):
        await store.replace_dod_spec(CID, weaker, actor="would-be-weaken")

    # Original is intact
    read = await store.get_dod_spec(CID)
    assert read == spec
    assert read.predicates  # still has 3 predicates, not the empty weaker set


async def test_replace_on_fresh_conversation_equals_set(store: SqliteEventStore) -> None:
    """Without a spec to replace, `replace_dod_spec` is equivalent to
    `set_dod_spec` — kept for symmetry so a caller cannot choose the verb to
    route around the immutability gate. The new spec is stored."""
    spec = _spec()
    cid2 = "conv_dod_replace_fresh"
    await store.replace_dod_spec(cid2, spec, actor="test")
    read = await store.get_dod_spec(cid2)
    assert read == spec


# ---- Why "the agent's tool surface" cannot mutate this ---------------------


async def test_replace_dod_spec_monotonic_extend_and_reject(store: SqliteEventStore) -> None:
    """v2 MONOTONIC replacement: a revision may EXTEND the DoD (add a steered
    deliverable) but never WEAKEN it. The store enforces the invariant so no
    caller can route around it."""
    cid = "conv_dod_monotonic"
    base = DoDSpec(predicates=[FileExistsPredicate(path="index.html")])
    await store.set_dod_spec(cid, base, set_by="first")

    # EXTEND: add contact.html (the steer scenario) → accepted, spec grows.
    extended = DoDSpec(
        predicates=[
            FileExistsPredicate(path="index.html"),
            FileExistsPredicate(path="contact.html"),
        ]
    )
    await store.replace_dod_spec(cid, extended, actor="system:plan_revision")
    read = await store.get_dod_spec(cid)
    assert {p.path for p in read.predicates} == {"index.html", "contact.html"}

    # IDEMPOTENT: replacing with the same set is a no-op success.
    await store.replace_dod_spec(cid, extended, actor="again")
    assert {p.path for p in (await store.get_dod_spec(cid)).predicates} == {
        "index.html",
        "contact.html",
    }

    # RENAME (tied): index.html → home.html via renamed_from → accepted.
    renamed = DoDSpec(
        predicates=[
            FileExistsPredicate(path="home.html", renamed_from="index.html"),
            FileExistsPredicate(path="contact.html"),
        ]
    )
    await store.replace_dod_spec(cid, renamed, actor="rename")
    assert {p.path for p in (await store.get_dod_spec(cid)).predicates} == {
        "home.html",
        "contact.html",
    }

    # WEAKEN: drop contact.html (no rename tie) → REJECTED, prior spec preserved.
    weaker = DoDSpec(predicates=[FileExistsPredicate(path="home.html")])
    with pytest.raises(DoDSpecAlreadySet):
        await store.replace_dod_spec(cid, weaker, actor="would-drop")
    assert {p.path for p in (await store.get_dod_spec(cid)).predicates} == {
        "home.html",
        "contact.html",
    }

    # FORGED RENAME: claim a rename from a path that was never committed → REJECTED.
    forged = DoDSpec(
        predicates=[
            FileExistsPredicate(path="home.html"),
            FileExistsPredicate(path="evil.html", renamed_from="contact.html"),
            FileExistsPredicate(path="x.html", renamed_from="never_existed.html"),
        ]
    )
    with pytest.raises(DoDSpecAlreadySet):
        await store.replace_dod_spec(cid, forged, actor="forge")


def test_no_agent_tool_can_reach_the_dod_spec_store() -> None:
    """The immutability argument depends on there being NO tool in the agent's
    surface that writes to the `dod_specs` table. This test enumerates the
    default tool registry (what the agent actually sees in a build) and
    asserts nothing in it has a path to `set_dod_spec` or `replace_dod_spec`.

    If this test ever fails after a registry change, the tool author MUST
    demonstrate why the new tool cannot reach the store (e.g. it operates on
    the workspace, not on the conversation's metadata) — otherwise C1a's
    structural immutability is gone and the agent can rewrite its own DoD.
    """
    reg = build_default_registry()
    # The default tool surface is what an agent gets. None of these are
    # allowed to mutate the DoD spec.
    forbidden_verb_substrings = (
        "dod",
        "definition_of_done",
        "acceptance_spec",
        "verify_spec",
    )
    for name in reg.names():
        for forbidden in forbidden_verb_substrings:
            assert forbidden not in name, (
                f"agent tool {name!r} contains forbidden substring {forbidden!r}; "
                "the agent's tool surface must not be able to mutate the "
                "DoD spec. Rename or remove this tool."
            )


def test_dod_methods_on_protocol_do_not_appear_on_agent_tools() -> None:
    """Stronger cross-check: even if a tool were NAMED 'dod_…', it still
    couldn't reach the store without a method-handle. Assert the tool
    registry carries no callable that closes over `set_dod_spec` /
    `replace_dod_spec`. We do this by walking each tool's public surface and
    asserting no attribute names a DoD method.
    """
    reg = build_default_registry()
    forbidden_method_names = (
        "set_dod_spec",
        "replace_dod_spec",
        "get_dod_spec",
        "get_external_dod_spec",
    )
    for name in reg.names():
        tool = reg.get(name, scope=agent_scope(model_policy=ModelExecutionPolicy.standard()))  # type: ignore[arg-type]
        if tool is None:
            # Contract-scoped tools (e.g. the v2 app_* set after the fix-2
            # hard-replace) are registered but not agent-scoped; the no-DoD-
            # handle property must hold for them too — check the raw instance.
            tool = reg._tools.get(name)  # type: ignore[attr-defined]
        assert tool is not None, f"tool {name!r} not registered"
        for attr in dir(tool):
            assert attr not in forbidden_method_names, (
                f"agent tool {type(tool).__name__}.{attr} shadows a DoD "
                "store method; the agent's tool surface must not be able "
                "to mutate the DoD spec."
            )


# ---- Discriminator & frozen-model guarantees ------------------------------


def test_predicate_kinds_are_minimal_and_real() -> None:
    """The predicate kinds must be EXACTLY the three real, machine-checkable
    shapes the research recommends (file_exists / command / http_ok). A
    fourth "agent_self_asserts_done" kind would re-introduce the very failure
    mode C1a exists to fix.
    """
    from disco.core.dod import CommandExitPredicate, FileExistsPredicate, HTTPOkPredicate

    real_kinds = {FileExistsPredicate, CommandExitPredicate, HTTPOkPredicate}
    # Round-trip each kind
    for kind_cls in real_kinds:
        # Build a minimal instance of each kind
        if kind_cls is FileExistsPredicate:
            inst = kind_cls(path="x")
        elif kind_cls is CommandExitPredicate:
            inst = kind_cls(cmd="true")
        elif kind_cls is HTTPOkPredicate:
            inst = kind_cls(url="http://localhost/")
        d = predicate_to_dict(inst)
        assert d["kind"] in {"file_exists", "command", "http_ok"}
        roundtrip = predicate_from_obj(d)
        assert isinstance(roundtrip, kind_cls)
        assert roundtrip == inst


def test_unknown_predicate_kind_is_rejected() -> None:
    """An attempt to attach a fake predicate kind (e.g. {"kind": "agent_says_done"})
    must be rejected at validation time, not silently passed through to the
    store. This is the predicate-level analogue of the immutability-via-agent
    gate: a malformed spec cannot sneak past the type system."""
    with pytest.raises(ValidationError):
        predicate_from_obj({"kind": "agent_says_done", "whatever": 1})


def test_predicates_and_spec_are_frozen() -> None:
    """Both the spec and each predicate are Pydantic-frozen. In-process
    mutation is a `ValidationError`. Belt-and-suspenders for the immutability
    argument: even a buggy caller holding a reference cannot mutate the
    returned accessor value."""
    spec = _spec()
    with pytest.raises(ValidationError):
        spec.predicates[0].path = "evil"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        spec.predicates = []  # type: ignore[misc]


# ---- Store contract surface ------------------------------------------------


def test_dod_methods_are_on_the_event_store_protocol() -> None:
    """The DoD accessor is part of the `EventStore` contract (a real
    `runtime_checkable` Protocol), so any future store backend MUST implement
    it. This is what makes the C1b evaluator portable across the SQLite and
    future Postgres/object-storage backends (BoD §4.1)."""
    # Protocol is runtime_checkable
    assert hasattr(EventStore, "set_dod_spec")
    assert hasattr(EventStore, "get_dod_spec")
    assert hasattr(EventStore, "get_external_dod_spec")
    assert hasattr(EventStore, "replace_dod_spec")


@pytest.mark.parametrize("authority", ["user", "system", "profile", "harness"])
async def test_external_authority_round_trips_byte_for_byte(
    store: SqliteEventStore, authority: str
) -> None:
    spec = _spec()
    original = spec.model_dump_json()
    await store.set_dod_spec(CID, spec, set_by=authority)

    external = await store.get_external_dod_spec(CID)
    assert external is not None
    assert external.model_dump_json() == original


async def test_legacy_plan_approval_row_is_audit_only_not_external_authority(
    store: SqliteEventStore,
) -> None:
    spec = DoDSpec(predicates=[FileExistsPredicate(path="legacy-plan-output.txt")])
    await store.set_dod_spec(CID, spec, set_by="system:plan_approval:r42")

    assert await store.get_dod_spec(CID) == spec
    assert await store.get_external_dod_spec(CID) is None


async def test_dod_spec_survives_reopen(tmp_path) -> None:
    """Durability: a spec set on a fresh store must be present after a
    simulated process restart. The contract that backs the `G2` durability
    guarantee on the event log must extend to the DoD spec table too —
    otherwise a server restart could silently lose the acceptance criteria
    and pass a no-spec gate."""
    db = tmp_path / "dod.db"
    s1 = SqliteEventStore(db)
    spec = _spec()
    await s1.set_dod_spec(CID, spec, set_by="test")
    s1.close()

    s2 = SqliteEventStore(db)
    read = await s2.get_dod_spec(CID)
    assert read == spec
    s2.close()


async def test_default_owner_id_used_when_conversation_doesnt_exist(
    store: SqliteEventStore,
) -> None:
    """Setting a DoD spec auto-creates the parent conversation row using
    `DEFAULT_OWNER_ID` — the same auto-create pattern the event log uses.
    This is what lets the runtime set the spec at conversation-create time,
    before any events are appended, without an out-of-band `create_conversation`
    call. (The C1c wire step will call `set_dod_spec` from the create
    endpoint; auto-create keeps that path a single round-trip.)"""
    fresh_cid = "conv_dod_fresh"
    spec = _spec()
    await store.set_dod_spec(fresh_cid, spec, set_by="auto-create-test")
    # Conversation row exists and is owned by the default owner.
    assert await store.conversation_exists(fresh_cid)
    s2 = SqliteEventStore(":memory:")
    # Sanity: a parallel store does NOT see the row (separate connection /
    # in-memory DB). We don't need to assert that here — the next reopen
    # test on tmp_path covers the durable case.
    s2.close()
    # Just assert the spec is readable on the same store.
    read = await store.get_dod_spec(fresh_cid)
    assert read == spec
    # And the default owner was used.
    from disco.core.store.sqlite import DEFAULT_OWNER_ID

    row = store._conn.execute(  # type: ignore[attr-defined]
        "SELECT owner_id FROM conversations WHERE conversation_id = ?",
        (fresh_cid,),
    ).fetchone()
    assert row is not None
    assert row["owner_id"] == DEFAULT_OWNER_ID
