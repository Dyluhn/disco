from __future__ import annotations

from typing import Any

from disco.agent_server.verify.reliability import (
    ACTIONLESS_AUTO_RESUME_MARKER,
    BLOCKED_LANDING_META_KEY,
    EXPORT_GATE_TOKEN,
    EXPORT_RELEASE_DETAIL,
    HOST_VERIFY_REFUSAL_MARKER,
    PROBE_SPIN_DETAIL,
    STUCK_ESCAPE_DETAIL,
    SYNTHETIC_FINISH_ATTEMPT_DETAIL,
    aggregate_reliability_metrics,
    run_reliability_metrics,
)


def _status(status: str, detail: str | None = None) -> dict[str, Any]:
    return {
        "kind": "status",
        "source": "system",
        "status": status,
        "detail": detail,
    }


def _blocked_status(detail: str = "evt_question") -> dict[str, Any]:
    event = _status("AWAITING_USER_QUESTION", detail)
    event["meta"] = {BLOCKED_LANDING_META_KEY: True, "legacy_detail": "no_progress"}
    return event


def _env_message(content: str) -> dict[str, Any]:
    return {
        "kind": "message",
        "source": "environment",
        "message": {"role": "user", "content": content},
    }


def _user_message(content: str) -> dict[str, Any]:
    return {
        "kind": "message",
        "source": "user",
        "message": {"role": "user", "content": content},
    }


def test_run_reliability_metrics_counts_every_marker() -> None:
    events = [
        _status("PAUSED", "actionless"),
        _status("PAUSED", "actionless"),
        _env_message(f"<system-reminder>{ACTIONLESS_AUTO_RESUME_MARKER}</system-reminder>"),
        _status("RUNNING", SYNTHETIC_FINISH_ATTEMPT_DETAIL),
        _status("RUNNING", STUCK_ESCAPE_DETAIL),
        _status("STUCK", PROBE_SPIN_DETAIL),
        _env_message(f"<system-reminder>\n{EXPORT_GATE_TOKEN}: blank\n</system-reminder>"),
        _user_message(EXPORT_GATE_TOKEN),
        _status("RUNNING", EXPORT_RELEASE_DETAIL),
        _env_message(f"{HOST_VERIFY_REFUSAL_MARKER} for app artifact 'site'. Broken."),
        _user_message(HOST_VERIFY_REFUSAL_MARKER),
        _blocked_status(),
        _status("FINISHED", None),
    ]

    assert run_reliability_metrics(events) == {
        "actionless_pauses": 2,
        "auto_resumes": 1,
        "synthetic_finishes": 1,
        "stuck_escapes": 1,
        "probe_spin_trips": 1,
        "export_refusals": 1,
        "export_releases": 1,
        "host_verify_refusals": 1,
        "blocked_landings": 1,
        "terminal_status": "FINISHED",
        "stalled": False,
    }


def test_run_reliability_metrics_marks_stalled_terminal_statuses() -> None:
    assert run_reliability_metrics([])["stalled"] is True
    assert run_reliability_metrics([_status("STUCK", "repeated_action_error")]) == {
        "actionless_pauses": 0,
        "auto_resumes": 0,
        "synthetic_finishes": 0,
        "stuck_escapes": 0,
        "probe_spin_trips": 0,
        "export_refusals": 0,
            "export_releases": 0,
            "host_verify_refusals": 0,
            "blocked_landings": 0,
            "terminal_status": "STUCK",
            "stalled": True,
    }
    assert run_reliability_metrics([_status("ERROR", None)])["stalled"] is False


def test_aggregate_reliability_metrics_sums_totals_and_stall_rate() -> None:
    runs = [
        {
            "reliability_metrics": {
                "actionless_pauses": 2,
                "auto_resumes": 1,
                "synthetic_finishes": 0,
                "stuck_escapes": 1,
                "probe_spin_trips": 0,
                "export_refusals": 0,
                "export_releases": 0,
                "host_verify_refusals": 1,
                "blocked_landings": 2,
                "terminal_status": "STUCK",
                "stalled": True,
            }
        },
        {
            "result": {
                "reliability_metrics": {
                    "actionless_pauses": 0,
                    "auto_resumes": 0,
                    "synthetic_finishes": 1,
                    "stuck_escapes": 0,
                    "probe_spin_trips": 1,
                    "export_refusals": 2,
                    "export_releases": 1,
                    "host_verify_refusals": 0,
                    "blocked_landings": 1,
                    "terminal_status": "FINISHED",
                    "stalled": False,
                }
            }
        },
    ]

    assert aggregate_reliability_metrics(runs) == {
        "stall_rate": 0.5,
        "totals": {
            "actionless_pauses": 2,
            "auto_resumes": 1,
            "synthetic_finishes": 1,
            "stuck_escapes": 1,
            "probe_spin_trips": 1,
            "export_refusals": 2,
            "export_releases": 1,
            "host_verify_refusals": 1,
            "blocked_landings": 3,
            "stalled": 1,
            "runs": 2,
        },
    }
