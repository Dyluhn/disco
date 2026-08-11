"""Resource admission for concurrently executed reliability suites."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

from harness.build_soak.resources import GIB, read_host_resources

__all__ = ["GIB", "WeightedSuitePool", "read_host_resources"]


class WeightedSuitePool:
    """Reserve variable per-suite memory while protecting desktop headroom."""

    def __init__(
        self,
        *,
        disk_path: Path,
        memory_reserve_bytes: int,
        disk_reserve_bytes: int,
        max_parallel: int,
        poll_s: float,
        wait_timeout_s: float,
    ) -> None:
        if min(memory_reserve_bytes, disk_reserve_bytes, max_parallel) <= 0:
            raise ValueError("resource reserves and parallelism must be positive")
        self.disk_path = disk_path
        self.memory_reserve_bytes = memory_reserve_bytes
        self.disk_reserve_bytes = disk_reserve_bytes
        self.max_parallel = max_parallel
        self.poll_s = poll_s
        self.wait_timeout_s = wait_timeout_s
        initial = read_host_resources(disk_path=disk_path)
        # Total memory defines whether a suite can ever fit. Available memory is
        # transient and is re-checked below so temporary desktop pressure waits
        # for the configured admission timeout instead of failing immediately.
        self.memory_budget = max(0, initial.memory_total_bytes - memory_reserve_bytes)
        self._active_memory = 0
        self._active_count = 0
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def slot(self, memory_bytes: int):
        if memory_bytes <= 0:
            raise ValueError("suite memory reservation must be positive")
        if memory_bytes > self.memory_budget:
            raise RuntimeError(
                "suite cannot fit without consuming the protected desktop memory reserve"
            )
        deadline = time.monotonic() + self.wait_timeout_s
        snapshot = None
        while snapshot is None:
            resources = read_host_resources(disk_path=self.disk_path)
            async with self._lock:
                memory_fits = self._active_memory + memory_bytes <= self.memory_budget
                live_memory_fits = (
                    resources.memory_available_bytes >= self.memory_reserve_bytes + memory_bytes
                )
                disk_fits = resources.disk_free_bytes >= self.disk_reserve_bytes
                count_fits = self._active_count < self.max_parallel
                if memory_fits and live_memory_fits and disk_fits and count_fits:
                    self._active_memory += memory_bytes
                    self._active_count += 1
                    snapshot = resources
            if snapshot is not None:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("suite resource admission timed out")
            await asyncio.sleep(self.poll_s)
        try:
            yield snapshot
        finally:
            async with self._lock:
                self._active_memory -= memory_bytes
                self._active_count -= 1
