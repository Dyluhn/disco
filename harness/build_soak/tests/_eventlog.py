"""Tiny builders for canonical full-event-dict fixtures (the persisted
`event.model_dump(mode="json")` shape). Used by the oracle/classifier unit tests so
each fixture event log is explicit and readable."""

from __future__ import annotations

import json
from typing import Any


def to_db_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render full-event dicts into the REAL SQLite row shape SqliteEventStore
    persists: top-level seq/kind/source/id columns + a JSON-STRING `payload`
    holding the whole event. Used to prove the normalizer/classifier behave
    identically on both shapes."""
    return [
        {
            "seq": e.get("seq"),
            "kind": e.get("kind"),
            "source": e.get("source"),
            "id": e.get("id"),
            "created_at": e.get("timestamp", ""),
            "payload": json.dumps(e),
        }
        for e in events
    ]


def msg(seq: int, source: str, content: str, *, role: str | None = None) -> dict[str, Any]:
    return {
        "id": f"evt_{seq}",
        "seq": seq,
        "kind": "message",
        "source": source,
        "message": {"role": role or source, "content": content},
    }


def status(seq: int, status_value: str, detail: str | None = None) -> dict[str, Any]:
    return {
        "id": f"evt_{seq}",
        "seq": seq,
        "kind": "status",
        "source": "system",
        "status": status_value,
        "detail": detail,
    }


def plan(seq: int, *, revision: int = 1, steps: int = 1) -> dict[str, Any]:
    return {
        "id": f"evt_{seq}",
        "seq": seq,
        "kind": "plan",
        "source": "agent",
        "summary": "a plan",
        "steps": [{"title": f"step {i}"} for i in range(steps)],
        "revision": revision,
    }


def action(
    seq: int, tool: str, *, args: dict[str, Any] | None = None, action_id: str | None = None
) -> dict[str, Any]:
    aid = action_id or f"evt_{seq}"
    return {
        "id": aid,
        "seq": seq,
        "kind": "action",
        "source": "agent",
        "thought": "do it",
        "tool_call": {"tool_name": tool, "arguments": args or {}, "call_id": f"call_{seq}"},
    }


def observation(
    seq: int, action_id: str, *, tool: str = "shell", success: bool = True
) -> dict[str, Any]:
    return {
        "id": f"evt_{seq}",
        "seq": seq,
        "kind": "observation",
        "source": "environment",
        "action_id": action_id,
        "tool_result": {
            "call_id": f"call_{seq}",
            "tool_name": tool,
            "success": success,
            "content": "ok" if success else "failed",
            "error": None if success else "boom",
        },
    }


def agent_error(seq: int, action_id: str, *, error: str = "rejected") -> dict[str, Any]:
    return {
        "id": f"evt_{seq}",
        "seq": seq,
        "kind": "agent_error",
        "source": "environment",
        "error": error,
        "action_id": action_id,
        "tool_call_id": f"call_{seq}",
    }


def clean_smoke_log() -> list[dict[str, Any]]:
    """A passing bare-Build event log: user -> plan -> approval -> action ->
    observation -> finished."""
    return [
        msg(1, "user", "build a page"),
        status(2, "RUNNING"),
        plan(3, revision=1),
        status(4, "AWAITING_PLAN_APPROVAL", "evt_3"),
        status(5, "RUNNING", "plan_approved"),
        status(6, "RUNNING"),
        action(7, "shell", args={"cmd": "ls"}, action_id="act7"),
        observation(8, "act7"),
        msg(9, "agent", "done", role="assistant"),
        status(10, "FINISHED"),
    ]
