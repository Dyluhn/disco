"""Shared hermetic guards for the retrieval suite.

Below-the-model retries (`disco.retrieval._transport_retry`) back off with real
`asyncio.sleep`. Tests must never actually wait, and must never depend on the
jitter roll, so the two module-level seams are replaced for every test: sleeps
are recorded instead of slept, and the jitter roll is pinned to its midpoint so
each recorded delay is exactly the configured backoff.

A finished run also SAVES its evidence pool under the data directory
(`deep_research.pool`), so the data directory is redirected per test — without
it every engine test would write a pool into the developer's own
`~/.local/share/disco`.

The same module also holds two pieces of PROCESS-wide state — the outbound
search token bucket and the per-engine rate-limit cooldown registry. Left alone,
both would leak across tests and make a diagnostic depend on how many searches
earlier tests happened to issue. So every test starts from an empty registry and
a bucket that is explicitly disabled (`DISCO_SEARCH_RATE_PER_MIN=0`); the tests
that exist to prove pacing set their own rate and reset the bucket themselves.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from disco.retrieval import _transport_retry


@pytest.fixture(autouse=True)
def no_real_backoff(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every backoff delay instead of sleeping it. Request this fixture to
    assert that a retry actually paused (and for how long)."""
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(_transport_retry, "_sleep", fake_sleep)
    monkeypatch.setattr(_transport_retry, "_jitter", lambda: 0.5)  # midpoint → no jitter
    return slept


@pytest.fixture(autouse=True)
def no_shared_search_pacing(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give every test an empty cooldown registry and a disabled token bucket."""
    monkeypatch.setenv("DISCO_SEARCH_RATE_PER_MIN", "0")
    _transport_retry.reset_search_pacing()
    yield
    _transport_retry.reset_search_pacing()


@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Give every test its own data directory, so a run's saved evidence pool
    lands in the test's tmp_path instead of the developer's home."""
    monkeypatch.setenv("DISCO_DATA_DIR", str(tmp_path / "disco-data"))
