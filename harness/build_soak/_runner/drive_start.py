"""Scenario-start contract and audit state for the Build Soak driver."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..efficiency import LiveEfficiencyProgress
from ..ports import ProductClient
from .bindings import ScenarioBindings
from .common import _TRIGGER_AFTER_FIRST_FILE_WRITE


@dataclass
class DriveAudit:
    """The user-turn and decision facts accumulated by one scenario drive."""

    timeline: list[str] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    declared_followup_seqs: list[int] = field(default_factory=list)
    declared_followup_requires_revision: list[bool] = field(default_factory=list)
    injected_user_seqs: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class ScenarioStart:
    """Validated scenario controls anchored to the created conversation."""

    conversation_id: str
    state_initial: dict[str, Any]
    lifecycle: dict[str, Any]
    clarification_answer: str | None
    decision_answer: str | None
    mid_run: list[dict[str, Any]]
    after_terminal: list[dict[str, Any]]
    cancel_at: dict[str, Any] | None
    pause_at: dict[str, str] | None


def _optional_mapping(
    scenario: dict[str, Any],
    name: str,
) -> dict[str, Any] | None:
    value = scenario.get(name)
    if value is not None and not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _optional_answer(scenario: dict[str, Any], name: str) -> str | None:
    value = scenario.get(name)
    return str(value) if value is not None else None


def _scenario_followups(
    scenario: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    followups = scenario.get("followups") or []
    mid_run = [
        followup
        for followup in followups
        if followup.get("trigger") == _TRIGGER_AFTER_FIRST_FILE_WRITE
    ]
    after_terminal = [
        followup
        for followup in followups
        if followup.get("trigger") != _TRIGGER_AFTER_FIRST_FILE_WRITE
    ]
    return mid_run, after_terminal


async def start_scenario(
    client: ProductClient,
    scenario: dict[str, Any],
    runtime: ScenarioBindings,
    audit: DriveAudit,
    *,
    model: str | None,
    autonomous: bool,
    seed: int | None,
) -> ScenarioStart:
    """Validate controls, enable monitors, and create the single conversation."""
    client.enable_live_thrash_monitor(scenario)
    relay_log_path = runtime.relay_log_path()
    client.enable_efficiency_progress(
        LiveEfficiencyProgress(
            scenario_id=str(scenario.get("id") or "?"),
            seed=seed,
        ),
        ledger_path=str(relay_log_path) if relay_log_path is not None else None,
    )
    lifecycle = scenario.get("lifecycle") or {}
    if not isinstance(lifecycle, dict):
        raise ValueError("lifecycle must be an object")
    import_fixture = _optional_mapping(scenario, "import_fixture")
    verification = _optional_mapping(scenario, "verification_requirements")
    surface = str(scenario.get("surface") or "build")
    options: dict[str, Any] = {
        "model": model,
        "autonomous": autonomous,
        "appkit": bool(scenario.get("appkit")),
        "surface": surface,
        "import_fixture": import_fixture,
    }
    if verification is not None:
        options["verification_requirements"] = verification
    cid = await client.create_build_conversation(str(scenario["prompt"]), **options)
    audit.timeline.append(
        f"created {surface} conversation {cid} "
        f"(autonomous={autonomous}, appkit={options['appkit']})"
    )
    mid_run, after_terminal = _scenario_followups(scenario)
    pause_trigger = lifecycle.get("pause_resume_at")
    pause_at = (
        {"trigger": pause_trigger} if isinstance(pause_trigger, str) and pause_trigger else None
    )
    raw_cancel = scenario.get("cancel_at")
    return ScenarioStart(
        conversation_id=cid,
        state_initial=await client.get_state(cid),
        lifecycle=lifecycle,
        clarification_answer=_optional_answer(scenario, "clarification_answer"),
        decision_answer=_optional_answer(scenario, "decision_answer"),
        mid_run=mid_run,
        after_terminal=after_terminal,
        cancel_at=raw_cancel if isinstance(raw_cancel, dict) else None,
        pause_at=pause_at,
    )
