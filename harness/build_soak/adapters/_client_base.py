"""Shared typed state and sibling-capability contract for ``DiscoApiClient``."""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from typing import Any

from ..efficiency import LiveEfficiencyProgress
from ._api_inspect import _InspectTraceAggregation
from ._api_types import (
    _ACTION_RESULT_PERSISTENCE_GRACE_S,
    _BROWSER_EVIDENCE_MAX_FILE_BYTES,
    _BROWSER_EVIDENCE_MAX_FILES,
    _BROWSER_EVIDENCE_MAX_TOTAL_BYTES,
    _DEFAULT_TOOL_TIMEOUT_S,
    _SNAPSHOT_UNPROVEN_STABLE_POLLS,
    _TOOL_TIMEOUT_OVERRIDES_S,
    Transport,
    _followup_pickup_timeout_s,
)
from ._api_workspace import _evaluate_snapshot_readiness


class _ClientBase:
    """Own client state while mixins supply independently bounded capabilities."""

    last_conversation_id: str | None
    scenario_evidence: dict[str, Any]

    def __init__(
        self,
        transport: Transport,
        *,
        db_path: str,
        poll_interval_s: float = 1.0,
        projects_root: str | None = None,
        snapshot_wait_s: float = 0.0,
        require_workspace_commit: bool = False,
    ) -> None:
        self._t = transport
        self._db_path = db_path
        self._poll = poll_interval_s
        self._projects_root = projects_root
        self._snapshot_wait_s = snapshot_wait_s
        self._require_workspace_commit = require_workspace_commit
        self._collected_workspace_dirs: dict[str, Path] = {}
        # A client that never opens an immutable version needs no scratch tree.
        # The live batch owner closes clients explicitly; lazy allocation also
        # keeps narrow adapter users from acquiring a resource they cannot use.
        self._verified_workspace_cache: tempfile.TemporaryDirectory[str] | None = None
        self._closed = False
        self.last_conversation_id = None
        self.scenario_evidence = {}
        self._live_thrash_scenario: dict[str, Any] | None = None
        self._live_thrash_samples = 0
        self._live_thrash_findings: list[dict[str, Any]] = []
        self._live_thrash_candidate = ""
        self._live_thrash_candidate_count = 0
        self._live_thrash_recorded: set[str] = set()
        self._live_thrash_last_sample = time.monotonic()
        self._inspect_aggregations: dict[str, _InspectTraceAggregation] = {}
        self._inspect_poll_tasks: dict[str, asyncio.Task[None]] = {}
        self._inspect_stop_events: dict[str, asyncio.Event] = {}
        self._inspect_sample_locks: dict[str, asyncio.Lock] = {}
        self._inspect_finish_tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._efficiency_progress: LiveEfficiencyProgress | None = None
        self._efficiency_sample_interval_s = 5.0
        self._efficiency_last_sample = 0.0
        self._efficiency_started_at = 0.0
        self._efficiency_ledger_path: str | None = None
        self._efficiency_ledger_offset = 0
        self._efficiency_ledger_calls = 0

    def _verified_workspace_cache_dir(self) -> Path:
        """Return this client's lazily-owned immutable-version scratch root."""

        if self._closed:
            raise RuntimeError("DiscoApiClient is closed")
        cache = self._verified_workspace_cache
        if cache is None:
            cache = tempfile.TemporaryDirectory(prefix="disco-soak-verified-")
            self._verified_workspace_cache = cache
        return Path(cache.name)

    async def aclose(self) -> None:
        """Release every task and filesystem resource owned by this client."""

        if self._closed:
            return
        self._closed = True

        for stop in self._inspect_stop_events.values():
            stop.set()
        tasks: set[asyncio.Task[Any]] = set(self._inspect_poll_tasks.values())
        tasks.update(self._inspect_finish_tasks.values())
        current = asyncio.current_task()
        owned_tasks = [task for task in tasks if task is not current]
        for task in owned_tasks:
            if not task.done():
                task.cancel()
        if owned_tasks:
            await asyncio.gather(*owned_tasks, return_exceptions=True)

        self._inspect_poll_tasks.clear()
        self._inspect_finish_tasks.clear()
        self._inspect_stop_events.clear()
        self._inspect_sample_locks.clear()
        self._inspect_aggregations.clear()
        self._collected_workspace_dirs.clear()

        cache = self._verified_workspace_cache
        self._verified_workspace_cache = None
        if cache is not None:
            cache.cleanup()

    @staticmethod
    def _default_tool_timeout_s() -> float:
        return _DEFAULT_TOOL_TIMEOUT_S

    @staticmethod
    def _tool_timeout_overrides_s() -> dict[str, float]:
        return _TOOL_TIMEOUT_OVERRIDES_S

    @staticmethod
    def _action_result_persistence_grace_s() -> float:
        return _ACTION_RESULT_PERSISTENCE_GRACE_S

    @staticmethod
    def _browser_evidence_max_files() -> int:
        return _BROWSER_EVIDENCE_MAX_FILES

    @staticmethod
    def _browser_evidence_max_file_bytes() -> int:
        return _BROWSER_EVIDENCE_MAX_FILE_BYTES

    @staticmethod
    def _browser_evidence_max_total_bytes() -> int:
        return _BROWSER_EVIDENCE_MAX_TOTAL_BYTES

    @staticmethod
    def _snapshot_unproven_stable_polls() -> int:
        return _SNAPSHOT_UNPROVEN_STABLE_POLLS

    @staticmethod
    def _followup_pickup_timeout_s() -> float:
        return _followup_pickup_timeout_s()

    @staticmethod
    def _evaluate_snapshot_readiness(
        declared: list[str],
        expected: dict[str, tuple[Any, ...]],
        manifest: dict[str, Any],
        stable_n: dict[str, int],
    ) -> tuple[bool, list[str], list[str]]:
        return _evaluate_snapshot_readiness(
            declared,
            expected,
            manifest,
            stable_n,
            stable_polls=_SNAPSHOT_UNPROVEN_STABLE_POLLS,
        )

    @staticmethod
    def _status_of(state: dict[str, Any]) -> str:
        raise NotImplementedError

    async def get_state(self, conversation_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def collect_events(self, conversation_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def collect_inspect_trace(
        self,
        conversation_id: str,
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    async def start_inspect_collection(self, conversation_id: str) -> None:
        raise NotImplementedError

    async def _sample_inspect_trace(self, conversation_id: str) -> None:
        raise NotImplementedError

    async def _fetch_inspect_snapshot(
        self,
        conversation_id: str,
    ) -> tuple[str, object | None]:
        raise NotImplementedError

    async def _emit_efficiency_progress(
        self,
        conversation_id: str,
        *,
        state: str,
    ) -> None:
        raise NotImplementedError

    async def pause(self, conversation_id: str) -> None:
        raise NotImplementedError

    def _read_events(self, conversation_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _progress_marker(self, conversation_id: str) -> tuple[int, int]:
        raise NotImplementedError

    def _latest_plan_revision(self, conversation_id: str) -> int:
        raise NotImplementedError

    def _planning_reentry_since(
        self,
        conversation_id: str,
        after_seq: int,
    ) -> bool:
        raise NotImplementedError

    def _progress_event_since(
        self,
        conversation_id: str,
        after_seq: int,
    ) -> bool:
        raise NotImplementedError

    async def _await_ready_snapshot(
        self,
        conversation_id: str,
        declared: list[str],
    ) -> tuple[dict[str, Any], Path | None]:
        raise NotImplementedError

    def _read_snapshot_manifest(
        self,
        conversation_id: str,
        declared: list[str],
        ws: Path | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _snapshot_workspace_dir(self, conversation_id: str) -> Path | None:
        raise NotImplementedError
