"""Explicit immutable bindings for old-path monkeypatch compatibility."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..adapters.disco_api import CollectedRun

DriveToTerminal = Callable[..., Awaitable[str]]
DriveScenario = Callable[..., Awaitable[CollectedRun]]
CleanupCollector = Callable[..., Awaitable[dict[str, Any]]]
ProviderLedger = Callable[[CollectedRun], list[dict[str, Any]] | None]
RelayLogPath = Callable[[], str | Path | None]
EvidenceAssembler = Callable[..., Path]


@dataclass(frozen=True)
class ScenarioBindings:
    """Scenario-driving seams resolved from the compatibility facade."""

    drive_to_terminal: DriveToTerminal
    relay_log_path: RelayLogPath


@dataclass(frozen=True)
class CoordinatorBindings:
    """One-run orchestration seams resolved from the compatibility facade."""

    drive_scenario: DriveScenario
    collect_terminal_cleanup_evidence: CleanupCollector
    provider_ledger_for_run: ProviderLedger
    relay_log_path: RelayLogPath
    assemble_dossier: EvidenceAssembler
    live_disco_container_count: Callable[[], int | None]
    dangling_volume_names: Callable[[], set[str] | None]


@dataclass(frozen=True)
class CleanupBindings:
    """Host resource probes used by the cleanup evidence owner."""

    live_disco_container_names: Callable[[], list[str] | None]
    disco_volume_names: Callable[[], list[str] | None]
    dangling_volume_names: Callable[[], set[str] | None]
