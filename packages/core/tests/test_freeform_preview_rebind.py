"""A freeform handoff can follow its own restarted launch, never an arbitrary preview."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from disco.core import (
    ActionEvent,
    DeliverableEvent,
    EventSource,
    ObservationEvent,
    ToolCall,
    ToolResult,
)
from disco.core.events import WorkspaceMutationEvent
from disco.core.loop import HostVerificationDeliverable
from disco.core.loop.finish.verify_gate_parts import host_deliverable
from disco.core.loop.finish.verify_gate_parts.host_authority import bind_host_verification_authority
from event_fakes import with_seqs


def _preview(digit, *, framework=False, **overrides):
    intent = {
        "launch_kind": "framework" if framework else "custom",
        "command": "node server.js",
        "framework": "node" if framework else None,
        "cwd": None if framework else "/workspace",
        "serve_dir": None,
    }
    payload = {
        "name": "service-desk",
        "port": 8000,
        "command": "PORT=8000 node server.js",
        "exec_dir": "/workspace",
        "intent": intent,
        "launch_kind": intent["launch_kind"],
        "status": "running",
        "projection_id": "pv_" + digit * 32,
        "sandbox_instance_id": "owned-sandbox",
        "sandbox_generation": 1,
        "url": "http://127.0.0.1:38187",
        **overrides,
    }
    identity = {k: payload[k] for k in ("command", "exec_dir", "intent", "name", "port")}
    payload["intent_digest"] = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    action = ActionEvent(
        thought="preview", tool_call=ToolCall(tool_name="preview_start", arguments={})
    )
    return [
        action,
        ObservationEvent(
            action_id=action.id,
            tool_result=ToolResult(
                tool_name="preview_start",
                call_id=action.tool_call.call_id,
                success=True,
                content="running",
                structured=payload,
            ),
        ),
    ]


def _events(**overrides):
    return with_seqs(
        [
            *_preview("a", framework=True),
            DeliverableEvent(title="Service Desk", path="server.js", artifact_kind="app"),
            *_preview("b", **overrides),
        ]
    )


class _Loop:
    conversation_id = "conv-freeform"
    executor = SimpleNamespace(_browser_generation="a" * 32, _workspace_mutation_epoch=3)

    def __init__(self, events, *, rotate=False):
        self.events = list(events)
        self.rotate = rotate

    async def _emit(self, event):
        persisted = event.model_copy(update={"seq": len(self.events) + 1})
        self.events.append(persisted)
        if self.rotate:
            self.events = with_seqs([*self.events, *_preview("c")])
        return persisted

    async def _events(self):
        return list(self.events)


def _gate(events, *, rotate=False):
    return SimpleNamespace(
        _loop=_Loop(events, rotate=rotate), _verifier_contract_payload=lambda: {}
    )


def _bind(gate, events):
    return bind_host_verification_authority(
        gate,
        HostVerificationDeliverable(
            conversation_id="conv-freeform", artifact_path="server.js", artifact_kind="app"
        ),
        events,
    )


async def test_freeform_framework_to_custom_restart_gets_new_host_handoff_and_binding():
    events = _events()
    gate = _gate(events)
    assert not host_deliverable.governed_verification_required(events)
    assert _bind(gate, events).preview_selection is None
    rebound, refreshed = await host_deliverable._rebind_current_preview_handoff(
        gate, events, events[2]
    )
    assert rebound.source is EventSource.SYSTEM
    assert rebound.path == "server.js"
    assert rebound.meta == {"host_preview_rebind": True, "replaces_deliverable_id": events[2].id}
    bound = _bind(gate, refreshed)
    assert bound.preview_selection.projection_id == "pv_" + "b" * 32
    assert bound.preview_binding_required
    assert bound.deliverable_event_id == rebound.id != events[2].id
    assert bound.required_claims  # still needs actual host browser verification
    assert len(refreshed) == len(events) + 1  # no verdict/pass synthesized
    again, unchanged = await host_deliverable._rebind_current_preview_handoff(
        gate, refreshed, rebound
    )
    assert again is rebound and unchanged == refreshed


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "unrelated"},
        {"port": 8001},
        {"sandbox_instance_id": "foreign"},
        {"sandbox_generation": 2},
        {"command": "PORT=8000 node unrelated.js"},
        {"exec_dir": "/workspace/unrelated"},
        {"exec_dir": None},
        {"status": "stopped"},
        {"status": "unavailable"},
        {"projection_id": "malformed"},
    ],
)
async def test_freeform_does_not_adopt_unrelated_foreign_or_unproven_preview(overrides):
    events = _events(**overrides)
    gate = _gate(events)
    original = events[2]
    rebound, refreshed = await host_deliverable._rebind_current_preview_handoff(
        gate, events, original
    )
    assert rebound is original and refreshed == events
    assert gate._loop.events == events


async def test_freeform_does_not_rebind_a_stopped_current_preview():
    events = _events()
    stop = ActionEvent(
        thought="stop preview",
        tool_call=ToolCall(tool_name="preview_stop", arguments={"name": "service-desk"}),
    )
    events = with_seqs(
        [
            *events,
            stop,
            ObservationEvent(
                action_id=stop.id,
                tool_result=ToolResult(
                    tool_name="preview_stop",
                    call_id=stop.tool_call.call_id,
                    success=True,
                    content="stopped",
                    structured={"stopped": ["service-desk"]},
                ),
            ),
        ]
    )
    rebound, refreshed = await host_deliverable._rebind_current_preview_handoff(
        _gate(events), events, events[2]
    )
    assert rebound is events[2] and refreshed == events


async def test_freeform_does_not_carry_old_handoff_into_new_run():
    events = _events()
    events = with_seqs(
        [*events[:3], WorkspaceMutationEvent(operation="agent.run-intent.user-turn"), *events[3:]]
    )
    rebound, refreshed = await host_deliverable._rebind_current_preview_handoff(
        _gate(events), events, events[2]
    )
    assert rebound is events[2] and refreshed == events


async def test_rotation_while_host_handoff_is_emitted_invalidates_binding():
    events = _events()
    gate = _gate(events, rotate=True)
    rebound, refreshed = await host_deliverable._rebind_current_preview_handoff(
        gate, events, events[2]
    )
    bound = _bind(gate, refreshed)
    assert rebound is not events[2]
    assert bound.preview_selection is None and bound.preview_binding_required


async def test_governed_contract_mismatch_still_refuses_rebind(monkeypatch):
    events = _events()
    monkeypatch.setattr(host_deliverable, "governed_verification_required", lambda _: True)
    monkeypatch.setattr(host_deliverable, "governed_verification_contract", lambda _: object())
    monkeypatch.setattr(host_deliverable, "handoff_matches_verification_contract", lambda *_: False)
    rebound, refreshed = await host_deliverable._rebind_current_preview_handoff(
        _gate(events), events, events[2]
    )
    assert rebound is events[2] and refreshed == events


@pytest.mark.parametrize(
    "root,path,allowed", [("site", "site/index.html", True), ("site", "other/index.html", False)]
)
async def test_static_successor_keeps_exact_artifact_scope(root, path, allowed):
    launch = {"launch_kind": "static", "serve_dir": root}
    kwargs = {"launch_kind": "static", "intent": launch, "command": "python -m http.server 8000"}
    events = with_seqs(
        [
            *_preview("a", **kwargs),
            DeliverableEvent(title="Static app", path=path, artifact_kind="app"),
            *_preview("b", **kwargs),
        ]
    )
    rebound, refreshed = await host_deliverable._rebind_current_preview_handoff(
        _gate(events), events, events[2]
    )
    assert (rebound is not events[2]) is allowed
    assert len(refreshed) == len(events) + int(allowed)


async def test_freeform_non_app_handoff_stays_unchanged():
    events = _events()
    handoff = events[2].model_copy(update={"artifact_kind": "file"})
    events[2] = handoff
    rebound, refreshed = await host_deliverable._rebind_current_preview_handoff(
        _gate(events), events, handoff
    )
    assert rebound is handoff and refreshed == events
