#!/usr/bin/env python3
"""Reproduce the GLM actionless-stall mechanism.

This script intentionally contains no API key. For the live endpoint probe, set:

    GLM_REPRO_API_KEY=... python scripts/repro_glm_actionless.py

It always prints the evidence.db actionless arithmetic. With the key present, it
also shows a live GLM-5.2 response that has finish_reason=stop but no
adapter-visible content and no structured tool calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import httpx
from disco.core.events import (
    ActionEvent,
    AgentErrorEvent,
    EventSource,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
    event_from_json_dict,
)
from disco.core.loop import signals
from disco.core.migration import migrate_event

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "evidence.db"
CID = "conv_a7e7a24316064c8db8b821bc13bfa462"
BASE_URL = "https://opencode.ai/zen/go/v1"
MODEL = "glm-5.2"
ACTIONLESS_BREAK_CAP = 3


def _load_events() -> list[Any]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "select payload from events where conversation_id=? order by seq",
        (CID,),
    ).fetchall()
    return [event_from_json_dict(migrate_event(json.loads(row["payload"]))) for row in rows]


def _short(text: str, limit: int = 120) -> str:
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "..."


def _print_event_line(event: Any) -> None:
    """Print one trailing persisted event in the actionless-stall evidence."""
    if isinstance(event, MessageEvent):
        print(
            f"    seq={event.seq} message source={event.source.value} "
            f"content={_short(event.message.content)!r}"
        )
    elif isinstance(event, ActionEvent):
        tool = event.tool_call.tool_name if event.tool_call else None
        print(f"    seq={event.seq} action tool={tool}")
    elif isinstance(event, ObservationEvent):
        print(
            f"    seq={event.seq} observation "
            f"tool={event.tool_result.tool_name} "
            f"success={event.tool_result.success}"
        )
    elif isinstance(event, AgentErrorEvent):
        print(f"    seq={event.seq} agent_error")
    elif isinstance(event, StatusEvent):
        print(f"    seq={event.seq} status status={event.status.value} detail={event.detail}")
    else:
        print(f"    seq={getattr(event, 'seq', None)} {type(event).__name__}")


def _print_pause_evidence(events: list[Any], pause: StatusEvent) -> None:
    """Print the trailing persisted events behind one actionless PAUSED pause."""
    before = [event for event in events if (event.seq or 0) < (pause.seq or 0)]
    persisted = signals.consecutive_noops(before)
    inferred_invisible = ACTIONLESS_BREAK_CAP - persisted
    print(
        "pause_seq="
        f"{pause.seq} persisted_consecutive_noops={persisted} "
        f"inferred_invisible_steps_to_reach_3={inferred_invisible}"
    )
    print("  trailing_persisted_events:")
    for event in before[-5:]:
        _print_event_line(event)


def print_db_evidence() -> None:
    events = _load_events()
    pauses = [
        event
        for event in events
        if isinstance(event, StatusEvent)
        and event.status.value == "PAUSED"
        and event.detail == "actionless"
    ]
    print(f"conversation={CID}")
    print(f"persisted_PAUSED_actionless_count={len(pauses)}")
    print(f"persisted_PAUSED_actionless_seqs={[event.seq for event in pauses]}")

    for pause in pauses:
        _print_pause_evidence(events, pause)

    agent_messages = [
        event
        for event in events
        if isinstance(event, MessageEvent) and event.source == EventSource.AGENT
    ]
    print(f"persisted_agent_message_count={len(agent_messages)}")
    print(f"persisted_agent_message_seqs={[event.seq for event in agent_messages]}")


async def live_glm_empty_stop_probe() -> None:
    key = os.environ.get("GLM_REPRO_API_KEY")
    if not key:
        print("live_probe=skipped_missing_GLM_REPRO_API_KEY")
        return

    shell_tool = {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Run a shell command.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
    body = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Follow the user instruction exactly. Tools are available but optional."
                ),
            },
            {
                "role": "user",
                "content": ("Return exactly three spaces and nothing else. Do not call tools."),
            },
        ],
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
        "tools": [shell_tool],
    }

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_chunks: list[dict[str, Any]] = []
    finish_reasons: list[str] = []
    usage: dict[str, Any] | None = None

    async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
        async with client.stream(
            "POST",
            f"{BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "content-type": "application/json",
            },
            json=body,
        ) as resp:
            print(f"live_probe_http_status={resp.status_code}")
            if resp.status_code >= 400:
                body_text = (await resp.aread()).decode("utf-8", "replace")
                print(f"live_probe_error={body_text[:500]!r}")
                return
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                if delta.get("content") is not None:
                    content_parts.append(delta.get("content") or "")
                if delta.get("reasoning_content"):
                    reasoning_parts.append(delta["reasoning_content"])
                if delta.get("tool_calls"):
                    tool_chunks.extend(delta["tool_calls"])
                if choice.get("finish_reason"):
                    finish_reasons.append(choice["finish_reason"])

    content = "".join(content_parts)
    reasoning = "".join(reasoning_parts)
    print(f"live_probe_finish_reasons={finish_reasons}")
    print(f"live_probe_adapter_visible_content_len={len(content)}")
    print(f"live_probe_adapter_visible_content_repr={content!r}")
    print(f"live_probe_reasoning_content_len={len(reasoning)}")
    print(f"live_probe_reasoning_content_prefix={reasoning[:300]!r}")
    print(f"live_probe_structured_tool_chunk_count={len(tool_chunks)}")
    print(f"live_probe_usage={usage}")


def main() -> None:
    print_db_evidence()
    asyncio.run(live_glm_empty_stop_probe())


if __name__ == "__main__":
    main()
