"""Single bounded owner for Build Soak scenario driving."""

from __future__ import annotations

from functools import partial
from typing import Any

from ..adapters.disco_api import CollectedRun
from ..ports import ProductClient
from .bindings import ScenarioBindings
from .common import _DEFAULT_HARD_CAP_S, _DEFAULT_INACTIVITY_S
from .drive_capture import collect_scenario_run
from .drive_phases import run_drive_phases
from .drive_start import DriveAudit, start_scenario
from .drive_timeout import freeze_progress_timeout
from .ledger import _relay_log_path
from .triggers import _drive_to_terminal


async def drive_scenario(
    client: ProductClient,
    scenario: dict[str, Any],
    *,
    model: str | None,
    autonomous: bool,
    timeout_s: float = _DEFAULT_INACTIVITY_S,
    hard_cap_s: float = _DEFAULT_HARD_CAP_S,
    seed: int | None = None,
    bindings: ScenarioBindings | None = None,
) -> CollectedRun:
    """Create, drive, and capture one scenario through explicit lifecycle owners."""
    runtime = bindings or ScenarioBindings(_drive_to_terminal, _relay_log_path)
    audit = DriveAudit()
    start = await start_scenario(
        client,
        scenario,
        runtime,
        audit,
        model=model,
        autonomous=autonomous,
        seed=seed,
    )
    freeze_timeout = partial(
        freeze_progress_timeout,
        client,
        start.conversation_id,
        scenario,
        start.state_initial,
        audit,
    )
    phase = await run_drive_phases(
        client,
        start,
        scenario,
        audit,
        runtime,
        autonomous=autonomous,
        timeout_s=timeout_s,
        hard_cap_s=hard_cap_s,
        freeze_timeout=freeze_timeout,
    )
    return await collect_scenario_run(client, scenario, start, phase, audit)
