"""State-free selection and publication mechanics for workspace deliverables."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from disco.core import (
    ActionEvent,
    BuildPlatformAdmissionEvent,
    DeliverableEvent,
    EventSource,
    ObservationEvent,
    WorkspaceMutationEvent,
    agent_view_consistent_events,
    current_appkit_ejection,
    current_workspace_agent_view_id,
    event_matches_current_workspace_view,
)
from disco.tools import APPKIT_MUTATORS

if TYPE_CHECKING:
    from disco.core.store.sqlite import SqliteEventStore

_LOG = logging.getLogger(__name__)


def _paired_verifier_payload(
    event: Any,
    actions: dict[str, ActionEvent],
    last_mutation_seq: int,
) -> dict[str, Any] | None:
    if not isinstance(event, ObservationEvent):
        return None
    result = event.tool_result
    action = actions.get(event.action_id or "")
    if action is None:
        return None
    if result.tool_name != "verify_appkit_app":
        return None
    if action.tool_call.tool_name != result.tool_name:
        return None
    if result.call_id != action.tool_call.call_id:
        return None
    if result.success is not True:
        return None
    if (event.seq or -1) <= last_mutation_seq:
        return None
    structured = result.structured
    if not isinstance(structured, dict):
        return None
    if structured.get("passed") is not True:
        return None
    return structured


def _safe_snapshot_entry(structured: dict[str, Any], snapshot_dir: Path) -> str | None:
    entry = structured.get("canonical_entry_path")
    if not isinstance(entry, str):
        return None
    if not entry:
        return None
    if "\\" in entry:
        return None
    if "\x00" in entry:
        return None
    relative = Path(entry)
    if relative.is_absolute():
        return None
    if any(part in {"", ".", ".."} for part in relative.parts):
        return None
    target = snapshot_dir.joinpath(*relative.parts)
    if target.is_symlink():
        return None
    if not target.is_file():
        return None
    return relative.as_posix()


def _verified_entry_candidate(
    event: Any,
    actions: dict[str, ActionEvent],
    last_mutation_seq: int,
    snapshot_dir: Path,
) -> str | None:
    structured = _paired_verifier_payload(event, actions, last_mutation_seq)
    return _safe_snapshot_entry(structured, snapshot_dir) if structured is not None else None


def _relevant_mutation_sequence(
    event: Any,
    actions: dict[str, ActionEvent],
) -> int | None:
    if isinstance(event, WorkspaceMutationEvent):
        if event.operation == "agent.artifact-manifest-fold":
            return None
        return event.seq or -1
    if not isinstance(event, ObservationEvent):
        return None
    if event.tool_result.success is not True:
        return None
    action = actions.get(event.action_id or "")
    if action is None:
        return None
    if action.tool_call.tool_name not in APPKIT_MUTATORS:
        return None
    return event.seq or -1


def trusted_verified_app_entry(events: list[Any], snapshot_dir: Path) -> str | None:
    """Return the latest paired host verifier's exact built entry."""

    projected = agent_view_consistent_events(events)
    if current_appkit_ejection(projected) is not None:
        return None
    actions = {event.id: event for event in projected if isinstance(event, ActionEvent)}
    mutation_sequences = (
        sequence
        for event in projected
        if (sequence := _relevant_mutation_sequence(event, actions)) is not None
    )
    last_mutation_seq = max(
        mutation_sequences,
        default=-1,
    )
    candidates: list[tuple[int, str]] = []
    for event in projected:
        entry = _verified_entry_candidate(
            event,
            actions,
            last_mutation_seq,
            snapshot_dir,
        )
        if entry is not None:
            candidates.append((event.seq or -1, entry))
    return max(candidates)[1] if candidates else None


def find_snapshot_index(snapshot_dir: Path) -> Path | None:
    """Find the preferred runnable index without traversing internal trees."""

    root = snapshot_dir / "index.html"
    if root.is_file():
        return root
    candidates = [
        path
        for path in snapshot_dir.rglob("index.html")
        if path.is_file()
        and ".pmx" not in path.parts
        and ".disco" not in path.parts
        and "node_modules" not in path.parts
    ]
    return min(candidates, key=lambda path: len(path.parts)) if candidates else None


def _governed_contract(events: list[Any]) -> Any | None:
    latest_admission = next(
        (event for event in reversed(events) if isinstance(event, BuildPlatformAdmissionEvent)),
        None,
    )
    return latest_admission.verification_contract if latest_admission is not None else None


def _matches_current_app_contract(
    event: Any,
    events: list[Any],
    governed_contract: Any | None,
) -> bool:
    if not isinstance(event, DeliverableEvent):
        return False
    if event.artifact_kind != "app":
        return False
    if not event_matches_current_workspace_view(events, event):
        return False
    if governed_contract is None:
        return True
    return (
        event.target_id == governed_contract.target_id
        and event.delivery_contract == governed_contract.delivery
        and event.verification_contract_digest == governed_contract.digest
    )


def _fallback_app_path(events: list[Any], snapshot_dir: Path) -> str | None:
    if any(isinstance(event, DeliverableEvent) for event in events):
        return None
    index = find_snapshot_index(snapshot_dir)
    if index is None:
        return None
    relative_dir = index.parent.relative_to(snapshot_dir).as_posix()
    return relative_dir if relative_dir and relative_dir != "." else "."


async def maybe_synthesize_app_deliverable(
    event_store: SqliteEventStore,
    conversation_id: str,
    snapshot_dir: Path,
) -> None:
    """Append the historical synthetic app handoff when exact evidence allows it."""

    try:
        existing = await event_store.get_events(conversation_id)
        governed_contract = _governed_contract(existing)
        if any(
            _matches_current_app_contract(event, existing, governed_contract) for event in existing
        ):
            return
        if governed_contract is not None:
            return
        path = trusted_verified_app_entry(existing, snapshot_dir)
        if path is None:
            path = _fallback_app_path(existing, snapshot_dir)
            if path is None:
                return
        await event_store.append(
            conversation_id,
            DeliverableEvent(
                source=EventSource.AGENT,
                agent_view_id=current_workspace_agent_view_id(existing),
                title="Web app",
                path=path,
                artifact_kind="app",
                deployment_url="",
            ),
        )
    except Exception:  # noqa: BLE001 - synthetic publication remains best-effort
        _LOG.debug(
            "synthetic app-deliverable skipped for %s",
            conversation_id,
            exc_info=True,
        )
