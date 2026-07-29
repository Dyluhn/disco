"""Moved authority continuity collection implementations."""

from __future__ import annotations

from ._shared import GOVERNED_ADMISSION_BYPASSED
from .helpers_01 import (
    _continued_segment_reusing_prior_preview,
    _replace_receipt,
    _result,
    _segment,
)


def _impl_test_final_continue_segment_is_authoritative() -> None:
    events = [*_segment(base=0, passed=True), *_segment(base=20, passed=False)]
    result = _result(events)
    assert result.failed
    assert result.code == GOVERNED_ADMISSION_BYPASSED


def _impl_test_continue_segment_may_reuse_exact_still_active_prior_preview() -> None:
    assert _result(_continued_segment_reusing_prior_preview()).passed


def _impl_test_continue_segment_cannot_reuse_prior_preview_after_stop() -> None:
    assert _result(_continued_segment_reusing_prior_preview(stopped=True)).failed


def _impl_test_latest_same_segment_failure_overrides_earlier_pass() -> None:
    events = _segment()
    later = dict(events[-2])
    later["seq"] = 11
    later["id"] = "evt_later_fail"
    later["verified"] = False
    later["verdict"] = "fail"
    later["verification_result"] = None
    events[-1]["seq"] = 12
    events.insert(-1, later)
    assert _result(events).failed


def _impl_test_mutation_after_observed_authority_rejects_stale_receipt() -> None:
    events = _segment()
    events[-2]["seq"] = 11
    events[-1]["seq"] = 12
    events.insert(
        -2,
        {
            "kind": "action",
            "seq": 10,
            "id": "evt_mutation",
            "tool_call": {"tool_name": "file_write", "call_id": "mut", "arguments": {}},
        },
    )
    assert _result(events).failed


def _impl_test_receipt_after_mutation_capable_observation_uses_observation_authority() -> None:
    events = _segment()
    started = next(event for event in events if event.get("kind") == "verifier_started")
    verdict = next(event for event in events if event.get("kind") == "verifier_verdict")
    terminal = events[-1]
    started["seq"] = 11
    started["workspace_revision"] = 9
    started["observed_after_seq"] = 10
    verdict["seq"] = 12
    terminal["seq"] = 13
    events[events.index(started) : events.index(started)] = [
        {
            "kind": "action",
            "source": "agent",
            "seq": 9,
            "id": "evt_capability_shell",
            "tool_call": {
                "tool_name": "shell",
                "call_id": "capability-shell",
                "arguments": {"command": "inspect-artifact"},
            },
        },
        {
            "kind": "observation",
            "source": "environment",
            "seq": 10,
            "id": "evt_capability_shell_result",
            "action_id": "evt_capability_shell",
            "tool_result": {
                "tool_name": "shell",
                "call_id": "capability-shell",
                "success": True,
                "action_profile": {"capabilities": ["workspace.mutate"]},
                "effect_receipts": [],
            },
        },
    ]
    _replace_receipt(events, workspace_revision=9, observed_after_seq=10)

    assert _result(events).passed


def _impl_test_mutation_after_pass_rejects_stale_receipt() -> None:
    events = _segment()
    events[-1]["seq"] = 13
    events[-1:-1] = [
        {
            "kind": "action",
            "source": "agent",
            "seq": 11,
            "id": "evt_post_verdict_write",
            "tool_call": {
                "tool_name": "file_write",
                "call_id": "post-write",
                "arguments": {"path": "index.html"},
            },
        },
        {
            "kind": "observation",
            "source": "environment",
            "seq": 12,
            "id": "evt_post_verdict_write_result",
            "action_id": "evt_post_verdict_write",
            "tool_result": {
                "tool_name": "file_write",
                "call_id": "post-write",
                "success": True,
            },
        },
    ]
    assert _result(events).failed


def _impl_test_preview_stop_after_pass_rejects_stale_receipt() -> None:
    events = _segment()
    events[-1]["seq"] = 13
    events[-1:-1] = [
        {
            "kind": "action",
            "source": "agent",
            "seq": 11,
            "id": "evt_post_verdict_stop",
            "tool_call": {
                "tool_name": "preview_stop",
                "call_id": "post-stop",
                "arguments": {"name": "web"},
            },
        },
        {
            "kind": "observation",
            "source": "environment",
            "seq": 12,
            "id": "evt_post_verdict_stop_result",
            "action_id": "evt_post_verdict_stop",
            "tool_result": {
                "tool_name": "preview_stop",
                "call_id": "post-stop",
                "success": True,
                "structured": {"stopped": ["web"]},
            },
        },
    ]
    assert _result(events).failed


def _impl_test_future_target_mutation_capability_after_pass_rejects_stale_receipt() -> None:
    events = _segment(native=True)
    events[-1]["seq"] = 13
    events[-1:-1] = [
        {
            "kind": "action",
            "source": "agent",
            "seq": 11,
            "id": "evt_device_patch",
            "action_profile": {"capabilities": ["workspace.mutate"]},
            "tool_call": {
                "tool_name": "device_apply_patch",
                "call_id": "device-patch",
                "arguments": {"bundle_id": "dev.fixture"},
            },
        },
        {
            "kind": "observation",
            "source": "environment",
            "seq": 12,
            "id": "evt_device_patch_result",
            "action_id": "evt_device_patch",
            "tool_result": {
                "tool_name": "device_apply_patch",
                "call_id": "device-patch",
                "success": True,
                "action_profile": {"capabilities": ["workspace.mutate"]},
            },
        },
    ]
    assert _result(events, native=True).failed


def _impl_test_failed_partial_future_mutation_after_pass_rejects_stale_receipt() -> None:
    events = _segment(native=True)
    events[-1]["seq"] = 13
    events[-1:-1] = [
        {
            "kind": "action",
            "source": "agent",
            "seq": 11,
            "id": "evt_partial_device_patch",
            "action_profile": {"capabilities": ["workspace.mutate"]},
            "tool_call": {
                "tool_name": "device_apply_patch",
                "call_id": "partial-device-patch",
                "arguments": {"bundle_id": "dev.fixture"},
            },
        },
        {
            "kind": "agent_error",
            "source": "environment",
            "seq": 12,
            "id": "evt_partial_device_patch_result",
            "action_id": "evt_partial_device_patch",
            "error": "device update failed after applying part of the patch",
            "action_profile": {"capabilities": ["workspace.mutate"]},
            "effect_receipts": [
                {
                    "kind": "mutation",
                    "capability": "workspace.mutate",
                }
            ],
        },
    ]
    assert _result(events, native=True).failed


def _impl_test_foreign_preview_pair_rejects_receipt() -> None:
    events = _segment()
    preview_action = next(
        event
        for event in events
        if event.get("kind") == "action"
        and event.get("tool_call", {}).get("tool_name") == "preview_start"
    )
    preview_action["id"] = "evt_foreign_preview"
    assert _result(events).failed


def _impl_test_self_anchored_receipt_from_another_conversation_is_rejected() -> None:
    events = _segment()
    _replace_receipt(events, conversation_id="conv_foreign")
    assert _result(events).failed


def _impl_test_foreign_agent_view_is_rejected_even_when_receipt_is_self_anchored() -> None:
    events = _segment()
    deliverable = next(event for event in events if event.get("kind") == "deliverable")
    started = next(event for event in events if event.get("kind") == "verifier_started")
    deliverable["agent_view_id"] = "view_foreign"
    started["agent_view_id"] = "view_foreign"
    _replace_receipt(events, agent_view_id="view_foreign")
    assert _result(events).failed


def _impl_test_same_intent_handoff_survives_a_later_verifier_view() -> None:
    events = _segment()
    started = next(event for event in events if event.get("kind") == "verifier_started")
    verdict = next(event for event in events if event.get("kind") == "verifier_verdict")
    terminal = next(event for event in events if event.get("kind") == "status")
    started["seq"] = 10
    started["agent_view_id"] = "view-later"
    verdict["seq"] = 11
    terminal["seq"] = 12
    events.append(
        {
            "kind": "workspace_mutation",
            "source": "system",
            "seq": 9,
            "id": "evt_view_later",
            "operation": "agent.view-admitted",
            "run_intent_id": "evt_intent_0",
            "agent_view_id": "view-later",
        }
    )
    _replace_receipt(events, agent_view_id="view-later")

    assert _result(events).passed


def _impl_test_foreign_observed_url_is_rejected_even_when_receipt_is_self_anchored() -> None:
    events = _segment()
    _replace_receipt(events, observed_url="http://127.0.0.1:9999/")
    assert _result(events).failed


def _impl_test_zero_workspace_revision_is_rejected_against_durable_start_authority() -> None:
    events = _segment()
    started = next(event for event in events if event.get("kind") == "verifier_started")
    started["workspace_revision"] = 0
    _replace_receipt(events, workspace_revision=0)
    assert _result(events).failed


def _impl_test_future_observation_order_is_rejected_against_durable_start_authority() -> None:
    events = _segment()
    started = next(event for event in events if event.get("kind") == "verifier_started")
    started["observed_after_seq"] = 99
    _replace_receipt(events, observed_after_seq=99)
    assert _result(events).failed


def _impl_test_native_policy_may_omit_workspace_epoch_when_not_required() -> None:
    events = _segment(native=True)
    started = next(event for event in events if event.get("kind") == "verifier_started")
    started["workspace_epoch"] = None
    _replace_receipt(events, workspace_epoch=None)
    assert _result(events, native=True).passed


def _impl_test_recognized_pre_execution_rejection_does_not_stale_receipt() -> None:
    events = _segment()
    started = next(event for event in events if event.get("kind") == "verifier_started")
    verdict = next(event for event in events if event.get("kind") == "verifier_verdict")
    terminal = events[-1]
    started["seq"] = 11
    verdict["seq"] = 12
    terminal["seq"] = 13
    events[events.index(started) : events.index(started)] = [
        {
            "kind": "action",
            "source": "agent",
            "seq": 9,
            "id": "evt_gate_rejected_write",
            "tool_call": {
                "tool_name": "file_write",
                "call_id": "gate-write",
                "arguments": {"path": "index.html"},
            },
        },
        {
            "kind": "agent_error",
            "source": "environment",
            "seq": 10,
            "id": "evt_gate_rejected_write_result",
            "action_id": "evt_gate_rejected_write",
            "error": (
                "<system-reminder>\nREFUSED: `file_write` is not available in "
                "PLANNING mode. No workspace mutation or execution is allowed "
                "before plan approval. Call `submit_plan`.\n</system-reminder>"
            ),
        },
    ]
    assert _result(events).passed
