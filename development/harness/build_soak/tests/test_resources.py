from __future__ import annotations

import asyncio

import pytest
from harness.build_soak.resources import (
    GIB,
    AdmissionPolicy,
    HostResources,
    ResourceGate,
    ResourcePool,
    resolve_worker_count,
    safe_worker_count,
)


def _host(*, available: int = 80, cpus: int = 12, disk: int = 100) -> HostResources:
    return HostResources(
        memory_total_bytes=128 * GIB,
        memory_available_bytes=available * GIB,
        cpu_count=cpus,
        disk_free_bytes=disk * GIB,
    )


def test_auto_parallelism_preserves_32_gib_and_honors_cpu() -> None:
    policy = AdmissionPolicy(memory_reserve_bytes=32 * GIB, memory_per_worker_bytes=4 * GIB)
    # Memory permits 12 workers, CPU permits 6 at two logical CPUs each.
    cpu_policy = AdmissionPolicy(
        memory_reserve_bytes=policy.memory_reserve_bytes,
        memory_per_worker_bytes=policy.memory_per_worker_bytes,
        cpus_per_worker=2,
    )
    assert safe_worker_count(_host(), cpu_policy) == 6
    assert resolve_worker_count("auto", _host(), cpu_policy) == 6


def test_explicit_parallelism_is_safely_clamped() -> None:
    policy = AdmissionPolicy(memory_reserve_bytes=32 * GIB, memory_per_worker_bytes=8 * GIB)
    assert safe_worker_count(_host(available=56), policy) == 3
    assert resolve_worker_count(20, _host(available=56), policy) == 3


def test_no_worker_is_admitted_inside_desktop_reserve() -> None:
    policy = AdmissionPolicy(memory_reserve_bytes=32 * GIB, memory_per_worker_bytes=3 * GIB)
    assert safe_worker_count(_host(available=34), policy) == 0
    assert resolve_worker_count("auto", _host(available=34), policy) == 0


@pytest.mark.asyncio
async def test_gate_rechecks_and_waits_for_recovered_capacity(tmp_path) -> None:
    samples = iter([_host(available=34), _host(available=40)])
    gate = ResourceGate(
        AdmissionPolicy(memory_reserve_bytes=32 * GIB, memory_per_worker_bytes=3 * GIB),
        disk_path=tmp_path,
        poll_s=0.001,
        wait_timeout_s=1,
        probe=lambda: next(samples),
    )
    admitted = await gate.wait_until_admitted()
    assert admitted.memory_available_bytes == 40 * GIB


@pytest.mark.asyncio
async def test_pool_counts_active_workers_against_live_capacity(tmp_path) -> None:
    # Exactly one worker fits. A second waiter must remain queued until the first
    # releases its reservation, even though both see the same host snapshot.
    host = _host(available=36)
    policy = AdmissionPolicy(memory_reserve_bytes=32 * GIB, memory_per_worker_bytes=4 * GIB)
    gate = ResourceGate(
        policy,
        disk_path=tmp_path,
        poll_s=0.001,
        wait_timeout_s=1,
        probe=lambda: host,
    )
    pool = ResourcePool(gate, policy)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def first() -> None:
        async with pool.slot():
            order.append("first")
            first_entered.set()
            await release_first.wait()

    async def second() -> None:
        await first_entered.wait()
        async with pool.slot():
            order.append("second")

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_entered.wait()
    await asyncio.sleep(0.01)
    assert order == ["first"]
    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert order == ["first", "second"]
