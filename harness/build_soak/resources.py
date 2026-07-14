"""Host resource admission for parallel live-soak workers.

The live harness creates real model requests, sandboxes, preview servers, and
occasionally browsers.  A plain ``asyncio.Semaphore(N)`` can therefore make a
fast soak less reliable than the product: it happily starts N workers even when
the desktop is already using most of the machine.

This module keeps the policy small and deterministic:

* ``MemAvailable`` (not merely ``MemFree``) is the memory truth source;
* a caller-selected desktop reserve is never allocated to new workers;
* CPU and free-disk slots cap the memory-derived worker count;
* a worker about to start re-checks the live host and waits, bounded, if an
  unrelated desktop workload consumed the remaining headroom.

It deliberately does not kill an in-flight build.  Disco's sandbox cgroup caps
bound those builds; admission controls only whether another one may start.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

GIB = 1024**3


@dataclass(frozen=True)
class HostResources:
    memory_total_bytes: int
    memory_available_bytes: int
    cpu_count: int
    disk_free_bytes: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class AdmissionPolicy:
    memory_reserve_bytes: int = 32 * GIB
    memory_per_worker_bytes: int = 3 * GIB
    disk_reserve_bytes: int = 10 * GIB
    disk_per_worker_bytes: int = 512 * 1024**2
    cpus_per_worker: int = 1

    def __post_init__(self) -> None:
        values = (
            self.memory_reserve_bytes,
            self.memory_per_worker_bytes,
            self.disk_reserve_bytes,
            self.disk_per_worker_bytes,
            self.cpus_per_worker,
        )
        if any(value <= 0 for value in values):
            raise ValueError("resource-admission values must all be positive")


def _meminfo(path: str | Path = "/proc/meminfo") -> tuple[int, int]:
    values: dict[str, int] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        key, _, tail = line.partition(":")
        if key not in {"MemTotal", "MemAvailable"}:
            continue
        amount = tail.strip().split()[0]
        values[key] = int(amount) * 1024  # Linux reports kB.
    if "MemTotal" not in values or "MemAvailable" not in values:
        raise RuntimeError("/proc/meminfo omitted MemTotal or MemAvailable")
    return values["MemTotal"], values["MemAvailable"]


def read_host_resources(
    *, disk_path: str | Path, meminfo_path: str | Path = "/proc/meminfo"
) -> HostResources:
    total, available = _meminfo(meminfo_path)
    disk = shutil.disk_usage(Path(disk_path))
    return HostResources(
        memory_total_bytes=total,
        memory_available_bytes=available,
        cpu_count=max(1, os.cpu_count() or 1),
        disk_free_bytes=disk.free,
    )


def safe_worker_count(resources: HostResources, policy: AdmissionPolicy) -> int:
    """Number of workers that fit *in addition to* the protected reserves.

    Zero is intentional: if the desktop already consumed the reserve, starting a
    build would violate the operator's stated host policy and the runner must
    wait/fail honestly rather than silently force one lane through.
    """
    memory_headroom = max(0, resources.memory_available_bytes - policy.memory_reserve_bytes)
    disk_headroom = max(0, resources.disk_free_bytes - policy.disk_reserve_bytes)
    memory_slots = memory_headroom // policy.memory_per_worker_bytes
    disk_slots = disk_headroom // policy.disk_per_worker_bytes
    cpu_slots = resources.cpu_count // policy.cpus_per_worker
    return max(0, min(memory_slots, disk_slots, cpu_slots))


def resolve_worker_count(
    requested: str | int,
    resources: HostResources,
    policy: AdmissionPolicy,
) -> int:
    safe = safe_worker_count(resources, policy)
    if isinstance(requested, str):
        value = requested.strip().lower()
        if value == "auto":
            return safe
        try:
            requested_n = int(value)
        except ValueError as exc:
            raise ValueError("parallel workers must be 'auto' or a positive integer") from exc
    else:
        requested_n = requested
    if requested_n <= 0:
        raise ValueError("parallel workers must be positive")
    return min(requested_n, safe)


class ResourceGate:
    """Bounded live re-check before a queued worker begins."""

    def __init__(
        self,
        policy: AdmissionPolicy,
        *,
        disk_path: str | Path,
        poll_s: float = 5.0,
        wait_timeout_s: float = 1800.0,
        probe: Callable[[], HostResources] | None = None,
    ) -> None:
        if poll_s <= 0 or wait_timeout_s <= 0:
            raise ValueError("resource poll and wait timeout must be positive")
        self._policy = policy
        self._disk_path = Path(disk_path)
        self._poll_s = poll_s
        self._wait_timeout_s = wait_timeout_s
        self._probe = probe

    def snapshot(self) -> HostResources:
        if self._probe is not None:
            return self._probe()
        return read_host_resources(disk_path=self._disk_path)

    @property
    def poll_s(self) -> float:
        return self._poll_s

    @property
    def wait_timeout_s(self) -> float:
        return self._wait_timeout_s

    async def wait_until_admitted(self, *, required_slots: int = 1) -> HostResources:
        if required_slots <= 0:
            raise ValueError("required_slots must be positive")
        deadline = time.monotonic() + self._wait_timeout_s
        while True:
            resources = self.snapshot()
            if safe_worker_count(resources, self._policy) >= required_slots:
                return resources
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "host resource reserve did not recover before the admission timeout"
                )
            await asyncio.sleep(self._poll_s)


class ResourcePool:
    """Concurrency-safe resource reservations for a batch.

    ``max_workers`` is the reservation made from one coherent host snapshot at
    batch start. The live check also prevents a new wave from starting when an
    unrelated desktop workload has consumed the remaining headroom.
    """

    def __init__(
        self,
        gate: ResourceGate,
        policy: AdmissionPolicy,
        *,
        max_workers: int | None = None,
    ) -> None:
        self._gate = gate
        self._policy = policy
        self._max_workers = max_workers or safe_worker_count(gate.snapshot(), policy)
        if self._max_workers <= 0:
            raise ValueError("resource pool requires capacity for at least one worker")
        self._active = 0
        self._lock = asyncio.Lock()

    @property
    def active(self) -> int:
        return self._active

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[HostResources]:
        deadline = time.monotonic() + self._gate.wait_timeout_s
        admitted: HostResources | None = None
        while admitted is None:
            resources = self._gate.snapshot()
            async with self._lock:
                if (
                    self._active < self._max_workers
                    and safe_worker_count(resources, self._policy) >= 1
                ):
                    self._active += 1
                    admitted = resources
            if admitted is not None:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "host resource reserve did not recover before the admission timeout"
                )
            await asyncio.sleep(self._gate.poll_s)

        try:
            yield admitted
        finally:
            async with self._lock:
                self._active -= 1
