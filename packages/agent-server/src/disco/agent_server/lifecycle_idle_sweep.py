"""Background orchestration for lifecycle, stranded-run, and TTS idle sweeps."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math

from disco.core.env import disco_env

from .lifecycle import LifecycleManager
from .run_stranded_sweep import RunStrandedSweep

_LOG = logging.getLogger(__name__)
_DEFAULT_IDLE_SWEEP_INTERVAL_S = 60.0
_MIN_IDLE_SWEEP_INTERVAL_S = 5.0


class LifecycleIdleSweeper:
    """Run periodic cleanup through its two explicit application owners."""

    def __init__(
        self,
        lifecycle: LifecycleManager,
        stranded_runs: RunStrandedSweep,
    ) -> None:
        self._lifecycle = lifecycle
        self._stranded_runs = stranded_runs

    @staticmethod
    def _interval_s() -> float:
        raw = disco_env("IDLE_SWEEP_INTERVAL_S", str(_DEFAULT_IDLE_SWEEP_INTERVAL_S))
        assert raw is not None
        try:
            interval_s = float(raw)
        except ValueError:
            interval_s = 0.0
        if math.isfinite(interval_s) and interval_s >= _MIN_IDLE_SWEEP_INTERVAL_S:
            return interval_s
        _LOG.error(
            "DISCO_IDLE_SWEEP_INTERVAL_S must be finite and at least %.0fs; "
            "using bounded %.0fs default",
            _MIN_IDLE_SWEEP_INTERVAL_S,
            _DEFAULT_IDLE_SWEEP_INTERVAL_S,
        )
        return _DEFAULT_IDLE_SWEEP_INTERVAL_S

    async def _sweep_abandoned_gates_logged(self) -> None:
        try:
            await self._lifecycle.sweep_abandoned_gates_once()
        except Exception:
            _LOG.exception("abandoned gate sweep failed")

    @staticmethod
    async def _unload_idle_tts() -> None:
        with contextlib.suppress(Exception):
            raw_ttl = disco_env("TTS_IDLE_TTL_S", "1800")
            assert raw_ttl is not None
            from disco.agent_server import tts_local

            await tts_local.maybe_unload_if_idle(ttl_s=int(float(raw_ttl)))

    async def run(self) -> None:
        """Periodically execute the exact cleanup sequence until cancelled."""

        while True:
            try:
                await asyncio.sleep(self._interval_s())
            except asyncio.CancelledError:
                return
            with contextlib.suppress(Exception):
                await self._lifecycle.sweep_idle_once()
            with contextlib.suppress(Exception):
                await self._stranded_runs.sweep_once()
            await self._sweep_abandoned_gates_logged()
            await self._unload_idle_tts()
