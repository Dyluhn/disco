"""Recovery guidance for a managed Preview generation rotation."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from disco.core import (
    ActionEvent,
    DeliverableEvent,
    EventSource,
    ObservationEvent,
    ToolCall,
    ToolResult,
)
from disco.core.loop.boundaries import HostVerificationDeliverable
from disco.core.loop.finish.verify_gate_parts import host_deliverable, host_disposition
from disco.core.loop.finish.verify_gate_parts.host_disposition import (
    _governed_non_pass_guidance,
)
from event_fakes import with_seqs


def _guidance(*, rehandoff: bool, summary: str = "managed-preview authority is unavailable") -> str:
    return _governed_non_pass_guidance(
        typed_result=None,
        host_label="unavailable",
        next_action="",
        summary=summary,
        failed_claim=None,
        unavailable_claim=None,
        preview_rehandoff_required=rehandoff,
    )


def test_generation_rotation_assigns_rebind_to_host_not_model() -> None:
    guidance = _guidance(rehandoff=True)

    assert "call `finish` once more" in guidance
    assert "host will bind and verify that generation itself" in guidance
    assert "No preview or handoff change is needed" in guidance
    assert "`serve`" not in guidance
    assert "Start a managed preview" not in guidance


def test_missing_preview_keeps_existing_start_guidance() -> None:
    guidance = _guidance(rehandoff=False)

    assert "Start a managed preview" in guidance
    assert "`serve`" not in guidance


def test_rotation_reported_during_verification_also_requires_rehandoff() -> None:
    guidance = _guidance(
        rehandoff=False,
        summary="selected preview generation is absent, changed, or foreign",
    )

    assert "host will bind and verify that generation itself" in guidance
    assert "call `serve` again" not in guidance
    assert "Start a managed preview" not in guidance


def test_rehandoff_predicate_requires_two_different_real_preview_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff = DeliverableEvent(title="App", path="index.html", artifact_kind="app").model_copy(
        update={"seq": 3}
    )
    deliverable = HostVerificationDeliverable(
        conversation_id="conv-preview",
        artifact_path="index.html",
        artifact_kind="app",
    )
    monkeypatch.setattr(host_disposition, "_latest_app_deliverable_event", lambda _events: handoff)
    selections = iter(
        [
            cast(Any, SimpleNamespace(operational_identity=("preview-old",))),
            cast(Any, SimpleNamespace(operational_identity=("preview-current",))),
        ]
    )
    monkeypatch.setattr(
        host_disposition,
        "preview_selection_at",
        lambda *_args, **_kwargs: next(selections),
    )

    assert host_disposition._preview_rehandoff_required([handoff], deliverable)


def _preview_pair(generation: int, projection_digit: str):
    intent = {"launch_kind": "static", "serve_dir": "."}
    identity = {
        "command": "python -m http.server 8000",
        "exec_dir": ".",
        "intent": intent,
        "name": "web",
        "port": 8000,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    action = ActionEvent(
        thought="start preview",
        tool_call=ToolCall(tool_name="preview_start", arguments={}),
    )
    observation = ObservationEvent(
        action_id=action.id,
        tool_result=ToolResult(
            call_id=action.tool_call.call_id,
            tool_name="preview_start",
            success=True,
            content="running",
            structured={
                **identity,
                "status": "running",
                "url": "http://127.0.0.1:8000/",
                "projection_id": "pv_" + projection_digit * 32,
                "sandbox_instance_id": "sandbox-test",
                "sandbox_generation": generation,
                "launch_kind": "static",
                "intent_digest": digest,
            },
        ),
    )
    return action, observation


@pytest.mark.asyncio
async def test_host_rebinds_rotated_preview_without_model_serve(monkeypatch) -> None:
    first_action, first_observation = _preview_pair(1, "a")
    handoff = DeliverableEvent(title="App", path="index.html", artifact_kind="app")
    second_action, second_observation = _preview_pair(2, "b")
    events = with_seqs(
        [first_action, first_observation, handoff, second_action, second_observation]
    )
    persisted_handoff = cast(DeliverableEvent, events[2])

    class _Loop:
        def __init__(self) -> None:
            self.events = list(events)

        async def _emit(self, event):
            persisted = event.model_copy(update={"seq": len(self.events) + 1})
            self.events.append(persisted)
            return persisted

        async def _events(self):
            return list(self.events)

    loop = _Loop()
    gate = SimpleNamespace(_loop=loop)
    monkeypatch.setattr(host_deliverable, "governed_verification_required", lambda _events: True)
    monkeypatch.setattr(host_deliverable, "governed_verification_contract", lambda _events: None)

    rebound, refreshed = await host_deliverable._rebind_current_preview_handoff(
        gate, events, persisted_handoff
    )

    assert rebound is not None and rebound is not persisted_handoff
    assert rebound.source is EventSource.SYSTEM
    assert rebound.meta == {
        "host_preview_rebind": True,
        "replaces_deliverable_id": persisted_handoff.id,
    }
    assert rebound.path == persisted_handoff.path
    assert not any(
        isinstance(event, ActionEvent) and event.tool_call.tool_name == "serve"
        for event in refreshed
    )
    current = host_deliverable.preview_selection_at(
        refreshed, through_seq=rebound.seq or 0
    )
    assert current is not None and current.sandbox_generation == 2


def test_host_does_not_rebind_without_a_new_active_preview() -> None:
    action, observation = _preview_pair(1, "a")
    events = with_seqs(
        [
            action,
            observation,
            DeliverableEvent(title="App", path="index.html", artifact_kind="app"),
        ]
    )
    assert not host_deliverable._preview_generation_changed(
        events, cast(DeliverableEvent, events[-1])
    )
