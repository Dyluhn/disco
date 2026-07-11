"""WO-A4 core quota/accounting store acceptance and concurrency tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from disco.core.quota import (
    DEFAULT_RESERVATION_TTL_SECONDS,
    MAX_REQUEST_LIMIT,
    MAX_RESERVATION_TTL_SECONDS,
    MAX_TOKEN_LIMIT,
    MAX_WINDOW_SECONDS,
    QuotaConfig,
    QuotaConfigurationError,
    ReservationConflict,
    ReservationNotFound,
    ReservationStateError,
    SqliteQuotaStore,
)

NOW = datetime(2026, 7, 11, 12, 0, 30, tzinfo=UTC)


def _store(
    tmp_path: Path,
    limits: QuotaConfig | None = None,
    *,
    reservation_ttl_seconds: int = DEFAULT_RESERVATION_TTL_SECONDS,
) -> SqliteQuotaStore:
    default = limits or QuotaConfig(window_seconds=60, max_requests=1000)
    return SqliteQuotaStore(
        tmp_path / "quota.db",
        default_config=default,
        reservation_ttl_seconds=reservation_ttl_seconds,
    )


def _reserve(
    store: SqliteQuotaStore,
    reservation_id: str,
    *,
    owner: str = "owner-a",
    app: str = "app-a",
    service: str = "ai.chat",
    input_tokens: int = 0,
    output_tokens: int = 0,
    now: datetime = NOW,
):
    return store.reserve(
        owner_id=owner,
        audience=app,
        service=service,
        reservation_id=reservation_id,
        estimated_input_tokens=input_tokens,
        estimated_output_tokens=output_tokens,
        now=now,
    )


def test_safe_default_applies_when_app_has_no_stored_config(tmp_path: Path) -> None:
    store = _store(tmp_path, QuotaConfig(window_seconds=30, max_requests=1))
    assert store.get_config("owner-a", "app-a") is None
    assert _reserve(store, "first").allowed

    denied = _reserve(store, "second")
    assert not denied.allowed
    assert denied.reason == "requests"
    assert denied.limiting_service is None
    assert denied.retry_after_seconds == 30


def test_owner_and_audience_are_independent_quota_partitions(tmp_path: Path) -> None:
    store = _store(tmp_path, QuotaConfig(window_seconds=60, max_requests=1))
    assert _reserve(store, "one", owner="owner-a", app="app-a").allowed
    assert not _reserve(store, "two", owner="owner-a", app="app-a").allowed

    assert _reserve(store, "one", owner="owner-a", app="app-b").allowed
    assert _reserve(store, "one", owner="owner-b", app="app-a").allowed
    assert store.get_usage(owner_id="owner-a", audience="app-a", now=NOW).request_count == 1
    assert store.get_usage(owner_id="owner-a", audience="app-b", now=NOW).request_count == 1
    assert store.get_usage(owner_id="owner-b", audience="app-a", now=NOW).request_count == 1


def test_app_total_and_exact_service_limits_both_apply(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.configure(
        owner_id="owner-a",
        audience="app-a",
        limits=QuotaConfig(window_seconds=60, max_requests=3),
        now=NOW,
    )
    store.configure(
        owner_id="owner-a",
        audience="app-a",
        service="ai.chat",
        limits=QuotaConfig(window_seconds=60, max_requests=1),
        now=NOW,
    )

    assert _reserve(store, "ai-1").allowed
    service_denial = _reserve(store, "ai-2")
    assert not service_denial.allowed
    assert service_denial.limiting_service == "ai.chat"
    assert _reserve(store, "mail-1", service="email.send").allowed
    assert _reserve(store, "mail-2", service="email.send").allowed

    aggregate_denial = _reserve(store, "mail-3", service="email.send")
    assert not aggregate_denial.allowed
    assert aggregate_denial.limiting_service is None
    assert store.get_usage(
        owner_id="owner-a", audience="app-a", service="ai.chat", now=NOW
    ).request_count == 1
    assert store.get_usage(owner_id="owner-a", audience="app-a", now=NOW).request_count == 3


def test_exact_service_match_has_no_prefix_or_wildcard_behavior(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.configure(
        owner_id="owner-a",
        audience="app-a",
        service="ai.chat",
        limits=QuotaConfig(window_seconds=60, max_requests=1),
    )
    assert _reserve(store, "chat").allowed
    assert _reserve(store, "chat-long", service="ai.chat_long").allowed


@pytest.mark.parametrize(
    ("limits", "first", "second", "reason"),
    [
        (
            QuotaConfig(window_seconds=60, max_input_tokens=10),
            (6, 0),
            (5, 0),
            "input_tokens",
        ),
        (
            QuotaConfig(window_seconds=60, max_output_tokens=10),
            (0, 6),
            (0, 5),
            "output_tokens",
        ),
        (
            QuotaConfig(window_seconds=60, max_total_tokens=10),
            (3, 3),
            (2, 3),
            "total_tokens",
        ),
    ],
)
def test_input_output_and_total_token_limits(
    tmp_path: Path,
    limits: QuotaConfig,
    first: tuple[int, int],
    second: tuple[int, int],
    reason: str,
) -> None:
    store = _store(tmp_path, limits)
    assert _reserve(store, "one", input_tokens=first[0], output_tokens=first[1]).allowed
    denied = _reserve(store, "two", input_tokens=second[0], output_tokens=second[1])
    assert not denied.allowed
    assert denied.reason == reason


def test_completion_reconciles_estimates_down_to_actual_tokens(tmp_path: Path) -> None:
    store = _store(tmp_path, QuotaConfig(window_seconds=60, max_total_tokens=10))
    assert _reserve(store, "one", input_tokens=4, output_tokens=4).allowed
    assert not _reserve(store, "blocked", input_tokens=2, output_tokens=2).allowed

    completed = store.complete(
        owner_id="owner-a",
        audience="app-a",
        reservation_id="one",
        actual_input_tokens=2,
        actual_output_tokens=2,
        now=NOW + timedelta(seconds=1),
    )
    assert completed.state == "completed"
    assert completed.accounted_input_tokens == 2
    assert completed.accounted_output_tokens == 2
    assert _reserve(store, "now-fits", input_tokens=3, output_tokens=3).allowed

    usage = store.get_usage(owner_id="owner-a", audience="app-a", now=NOW)
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (5, 5, 10)


def test_actual_overage_is_recorded_and_blocks_future_admission(tmp_path: Path) -> None:
    store = _store(tmp_path, QuotaConfig(window_seconds=60, max_total_tokens=10))
    assert _reserve(store, "one", input_tokens=1, output_tokens=1).allowed
    store.complete(
        owner_id="owner-a",
        audience="app-a",
        reservation_id="one",
        actual_input_tokens=8,
        actual_output_tokens=8,
    )
    usage = store.get_usage(owner_id="owner-a", audience="app-a", now=NOW)
    assert usage.total_tokens == 16
    assert not _reserve(store, "two").allowed


def test_fixed_window_rollover_and_retry_after(tmp_path: Path) -> None:
    store = _store(tmp_path, QuotaConfig(window_seconds=60, max_requests=1))
    near_end = datetime(2026, 7, 11, 12, 0, 59, 100_000, tzinfo=UTC)
    assert _reserve(store, "old", now=near_end).allowed
    denied = _reserve(store, "denied", now=near_end + timedelta(milliseconds=100))
    assert not denied.allowed
    assert denied.retry_after_seconds == 1

    boundary = datetime(2026, 7, 11, 12, 1, 0, tzinfo=UTC)
    assert _reserve(store, "new", now=boundary).allowed
    assert store.get_usage(owner_id="owner-a", audience="app-a", now=boundary).request_count == 1


def test_stale_reservation_is_abandoned_during_usage_sweep(tmp_path: Path) -> None:
    store = _store(
        tmp_path,
        QuotaConfig(window_seconds=60, max_total_tokens=10),
        reservation_ttl_seconds=10,
    )
    assert _reserve(store, "crashed", input_tokens=6, output_tokens=4).allowed
    before = store.get_usage(
        owner_id="owner-a", audience="app-a", now=NOW + timedelta(seconds=9)
    )
    assert (before.request_count, before.total_tokens) == (1, 10)

    after = store.get_usage(
        owner_id="owner-a", audience="app-a", now=NOW + timedelta(seconds=10)
    )
    assert (after.request_count, after.total_tokens) == (1, 0)
    reservation = store.get_reservation("owner-a", "app-a", "crashed")
    assert reservation is not None
    assert reservation.state == "abandoned"
    assert reservation.accounted_input_tokens == 0
    assert reservation.accounted_output_tokens == 0
    assert reservation.actual_input_tokens == 0
    assert reservation.actual_output_tokens == 0


def test_expiry_preserves_request_limit_while_releasing_tokens(tmp_path: Path) -> None:
    store = _store(
        tmp_path,
        QuotaConfig(window_seconds=60, max_requests=1, max_total_tokens=10),
        reservation_ttl_seconds=5,
    )
    assert _reserve(store, "crashed", input_tokens=5, output_tokens=5).allowed

    denied = _reserve(store, "retry", now=NOW + timedelta(seconds=5))
    assert not denied.allowed
    assert denied.reason == "requests"
    assert denied.usage is not None
    assert (denied.usage.request_count, denied.usage.total_tokens) == (1, 0)


def test_expiry_during_admission_releases_tokens_for_new_request(tmp_path: Path) -> None:
    store = _store(
        tmp_path,
        QuotaConfig(window_seconds=60, max_total_tokens=10),
        reservation_ttl_seconds=5,
    )
    assert _reserve(store, "crashed", input_tokens=5, output_tokens=5).allowed
    admitted = _reserve(
        store,
        "replacement",
        input_tokens=6,
        output_tokens=4,
        now=NOW + timedelta(seconds=5),
    )
    assert admitted.allowed
    usage = store.get_usage(
        owner_id="owner-a", audience="app-a", now=NOW + timedelta(seconds=5)
    )
    assert (usage.request_count, usage.total_tokens) == (2, 10)


def test_expiry_sweep_is_scoped_to_one_owner_and_audience(tmp_path: Path) -> None:
    store = _store(tmp_path, reservation_ttl_seconds=5)
    assert _reserve(store, "a", owner="owner-a", app="app-a", input_tokens=3).allowed
    assert _reserve(store, "b", owner="owner-b", app="app-b", input_tokens=4).allowed

    usage_a = store.get_usage(
        owner_id="owner-a", audience="app-a", now=NOW + timedelta(seconds=5)
    )
    assert (usage_a.request_count, usage_a.total_tokens) == (1, 0)
    reservation_a = store.get_reservation("owner-a", "app-a", "a")
    reservation_b = store.get_reservation("owner-b", "app-b", "b")
    assert reservation_a is not None and reservation_a.state == "abandoned"
    assert reservation_b is not None and reservation_b.state == "reserved"


def test_abandoned_idempotency_key_cannot_redispatch_or_complete(tmp_path: Path) -> None:
    store = _store(tmp_path, reservation_ttl_seconds=5)
    assert _reserve(store, "crashed", input_tokens=2).allowed
    store.get_usage(owner_id="owner-a", audience="app-a", now=NOW + timedelta(seconds=5))

    repeated = _reserve(store, "crashed", input_tokens=2, now=NOW + timedelta(seconds=6))
    assert not repeated.allowed
    assert repeated.reason == "reservation_abandoned"
    with pytest.raises(ReservationStateError, match="abandoned"):
        store.complete(
            owner_id="owner-a",
            audience="app-a",
            reservation_id="crashed",
            actual_input_tokens=1,
            actual_output_tokens=0,
        )
    with pytest.raises(ReservationStateError, match="settled"):
        store.release(owner_id="owner-a", audience="app-a", reservation_id="crashed")


def test_reservation_and_completion_are_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path, QuotaConfig(window_seconds=60, max_requests=1))
    first = _reserve(store, "same", input_tokens=2, output_tokens=3)
    repeated = _reserve(store, "same", input_tokens=2, output_tokens=3)
    assert first.allowed and repeated.allowed
    assert repeated.reservation == first.reservation
    assert store.get_usage(owner_id="owner-a", audience="app-a", now=NOW).request_count == 1

    with pytest.raises(ReservationConflict):
        _reserve(store, "same", input_tokens=3, output_tokens=3)

    completed = store.complete(
        owner_id="owner-a",
        audience="app-a",
        reservation_id="same",
        actual_input_tokens=1,
        actual_output_tokens=4,
    )
    repeated_completion = store.complete(
        owner_id="owner-a",
        audience="app-a",
        reservation_id="same",
        actual_input_tokens=1,
        actual_output_tokens=4,
    )
    assert repeated_completion == completed
    with pytest.raises(ReservationConflict):
        store.complete(
            owner_id="owner-a",
            audience="app-a",
            reservation_id="same",
            actual_input_tokens=2,
            actual_output_tokens=4,
        )


def test_release_is_idempotent_and_returns_reserved_capacity(tmp_path: Path) -> None:
    store = _store(tmp_path, QuotaConfig(window_seconds=60, max_requests=1))
    assert _reserve(store, "released").allowed
    assert store.release(owner_id="owner-a", audience="app-a", reservation_id="released")
    assert not store.release(owner_id="owner-a", audience="app-a", reservation_id="released")
    assert store.get_usage(owner_id="owner-a", audience="app-a", now=NOW).request_count == 0
    assert _reserve(store, "replacement").allowed

    old = _reserve(store, "released")
    assert not old.allowed
    assert old.reason == "reservation_released"
    with pytest.raises(ReservationStateError):
        store.complete(
            owner_id="owner-a",
            audience="app-a",
            reservation_id="released",
            actual_input_tokens=0,
            actual_output_tokens=0,
        )


def test_completed_usage_cannot_be_released_and_missing_ids_are_typed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _reserve(store, "done")
    store.complete(
        owner_id="owner-a",
        audience="app-a",
        reservation_id="done",
        actual_input_tokens=0,
        actual_output_tokens=0,
    )
    with pytest.raises(ReservationStateError):
        store.release(owner_id="owner-a", audience="app-a", reservation_id="done")
    with pytest.raises(ReservationNotFound):
        store.release(owner_id="owner-a", audience="app-a", reservation_id="missing")


def test_atomic_admission_under_thread_concurrency(tmp_path: Path) -> None:
    store = _store(tmp_path, QuotaConfig(window_seconds=60, max_requests=7))

    def attempt(index: int) -> bool:
        return _reserve(store, f"request-{index}").allowed

    with ThreadPoolExecutor(max_workers=20) as pool:
        admitted = list(pool.map(attempt, range(40)))
    assert sum(admitted) == 7
    assert store.get_usage(owner_id="owner-a", audience="app-a", now=NOW).request_count == 7


def test_concurrent_token_reservations_cannot_oversubscribe(tmp_path: Path) -> None:
    store = _store(tmp_path, QuotaConfig(window_seconds=60, max_total_tokens=50))

    def attempt(index: int) -> bool:
        return _reserve(store, f"token-request-{index}", input_tokens=4, output_tokens=3).allowed

    with ThreadPoolExecutor(max_workers=20) as pool:
        admitted = list(pool.map(attempt, range(30)))
    assert sum(admitted) == 7
    usage = store.get_usage(owner_id="owner-a", audience="app-a", now=NOW)
    assert usage.total_tokens == 49


def test_atomic_admission_across_independent_sqlite_connections(tmp_path: Path) -> None:
    path = tmp_path / "shared.db"
    stores = [
        SqliteQuotaStore(path, default_config=QuotaConfig(window_seconds=60, max_requests=3))
        for _ in range(8)
    ]
    barrier = Barrier(len(stores))

    def attempt(index: int) -> bool:
        barrier.wait()
        return _reserve(stores[index], f"process-like-{index}").allowed

    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        admitted = list(pool.map(attempt, range(len(stores))))
    assert sum(admitted) == 3
    assert stores[0].get_usage(owner_id="owner-a", audience="app-a", now=NOW).request_count == 3
    for store in stores:
        store.close()


def test_expiry_and_admission_are_atomic_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "expiry-race.db"
    limits = QuotaConfig(window_seconds=60, max_total_tokens=10)
    stores = [
        SqliteQuotaStore(path, default_config=limits, reservation_ttl_seconds=5)
        for _ in range(10)
    ]
    assert _reserve(stores[0], "crashed", input_tokens=5, output_tokens=5).allowed
    barrier = Barrier(len(stores))

    def attempt(index: int) -> bool:
        barrier.wait()
        return _reserve(
            stores[index],
            f"replacement-{index}",
            input_tokens=1,
            output_tokens=1,
            now=NOW + timedelta(seconds=5),
        ).allowed

    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        admitted = list(pool.map(attempt, range(len(stores))))
    assert sum(admitted) == 5
    usage = stores[0].get_usage(
        owner_id="owner-a", audience="app-a", now=NOW + timedelta(seconds=5)
    )
    assert (usage.request_count, usage.total_tokens) == (6, 10)
    crashed = stores[0].get_reservation("owner-a", "app-a", "crashed")
    assert crashed is not None and crashed.state == "abandoned"
    for store in stores:
        store.close()


def test_config_reservations_and_reconciliation_persist_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "durable.db"
    store = SqliteQuotaStore(path)
    configured = store.configure(
        owner_id="owner-a",
        audience="app-a",
        service="ai.chat",
        limits=QuotaConfig(window_seconds=120, max_requests=2, max_total_tokens=50),
        now=NOW,
    )
    assert _reserve(store, "persisted", input_tokens=10, output_tokens=5).allowed
    store.complete(
        owner_id="owner-a",
        audience="app-a",
        reservation_id="persisted",
        actual_input_tokens=7,
        actual_output_tokens=4,
        now=NOW + timedelta(seconds=1),
    )
    store.close()

    reopened = SqliteQuotaStore(path)
    assert reopened.get_config("owner-a", "app-a", service="ai.chat") == configured
    reservation = reopened.get_reservation("owner-a", "app-a", "persisted")
    assert reservation is not None
    assert reservation.state == "completed"
    assert (reservation.actual_input_tokens, reservation.actual_output_tokens) == (7, 4)
    usage = reopened.get_usage(
        owner_id="owner-a", audience="app-a", service="ai.chat", now=NOW
    )
    assert (usage.request_count, usage.total_tokens) == (1, 11)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"window_seconds": 0, "max_requests": 1},
        {"window_seconds": MAX_WINDOW_SECONDS + 1, "max_requests": 1},
        {"max_requests": 0},
        {"max_requests": True},
        {"max_requests": MAX_REQUEST_LIMIT + 1},
        {"max_input_tokens": -1},
        {"max_total_tokens": MAX_TOKEN_LIMIT + 1},
    ],
)
def test_invalid_quota_configs_are_rejected(kwargs: dict[str, int]) -> None:
    with pytest.raises(QuotaConfigurationError):
        QuotaConfig(**kwargs)


@pytest.mark.parametrize("ttl", [0, -1, True, MAX_RESERVATION_TTL_SECONDS + 1])
def test_invalid_reservation_ttls_are_rejected(tmp_path: Path, ttl: int) -> None:
    with pytest.raises(QuotaConfigurationError, match="reservation_ttl_seconds"):
        _store(tmp_path, reservation_ttl_seconds=ttl)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner_id", ""),
        ("owner_id", "bad owner"),
        ("audience", "../escape"),
        ("service", "ai.*"),
        ("service", "AI.chat"),
        ("reservation_id", "bad/id"),
    ],
)
def test_invalid_identity_inputs_are_rejected(tmp_path: Path, field: str, value: str) -> None:
    store = _store(tmp_path)
    params = {
        "owner_id": "owner-a",
        "audience": "app-a",
        "service": "ai.chat",
        "reservation_id": "request-1",
    }
    params[field] = value
    with pytest.raises(QuotaConfigurationError):
        store.reserve(**params)


def test_negative_tokens_and_naive_clock_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(QuotaConfigurationError):
        _reserve(store, "negative", input_tokens=-1)
    with pytest.raises(QuotaConfigurationError):
        _reserve(store, "naive", now=datetime(2026, 7, 11, 12, 0, 0))


def test_delete_exact_config_does_not_delete_app_config(tmp_path: Path) -> None:
    store = _store(tmp_path)
    aggregate = QuotaConfig(window_seconds=60, max_requests=5)
    exact = QuotaConfig(window_seconds=30, max_requests=1)
    store.configure(owner_id="owner-a", audience="app-a", limits=aggregate)
    store.configure(owner_id="owner-a", audience="app-a", service="ai.chat", limits=exact)

    assert store.delete_config("owner-a", "app-a", service="ai.chat")
    assert store.get_config("owner-a", "app-a", service="ai.chat") is None
    stored_aggregate = store.get_config("owner-a", "app-a")
    assert stored_aggregate is not None and stored_aggregate.limits == aggregate
    assert not store.delete_config("owner-a", "app-a", service="ai.chat")
