"""The single-source projection from event-log → cassette rows (D10).

The share bundle's `events` field is the event log (UI viewer). The harness
cassette is the service-call projection (replay). D10 unifies them: the share
bundle emits the cassette-format rows as a `cassette` field, derived from
the SAME scrubbed events the viewer renders. One function, one key function,
one row format — `Cassette.from_rows(bundle["cassette"])` round-trips.

A `cassette row` mirrors what the harness `Cassette` class records/loads:
    {"seam": "search"|"extract"|"llm.complete", "key": "<sha16>",
     "input": {...}, "output": <serialized>}

The mapping from event log → cassette row is deterministic: a search call is
the pair (ActionEvent tool_call == "ddgs.search" with `query`) →
(ObservationEvent action_id == action.id with `tool_result.content` =
`SearchHit[]`). An extract call is the same shape with `tool_name ==
"ddgs.extract"`. An LLM call surfaces as an ActionEvent whose
`thought`/context/llm_response_id carries the input payload (the routing
trace from the captured router), paired with the agent's subsequent message
emission carrying the output.

We intentionally project a CONSERVATIVE set of seams: only the calls whose
input/output can be reconstructed from the event log alone (search, extract,
llm.complete). The replay_runner honors the same seams, so the same
`Cassette` object serves both readers.

**Pure**: same scrubbed events → same cassette rows. **No clock, no random,
no side effects** — so the bundle is byte-deterministic (modulo the existing
`exported_at` timestamp), and the cassette's `key` matches what the live
recorder would have produced, meaning a real recording could be merged with
a bundle's projection without collision.
"""

from __future__ import annotations

from typing import Any

from .cassette import cassette_key

# Tool names that the harness `RecordingSearchProvider` / `RecordingExtractionProvider`
# recognize as the "search" and "extract" seams. Mapped 1:1 to cassette `seam`
# values; keep the constants synchronized with `harness/providers.py` so the
# replay side and the export side agree.
_SEARCH_TOOL_NAMES: frozenset[str] = frozenset({"ddgs.search", "search", "search_web"})
_EXTRACT_TOOL_NAMES: frozenset[str] = frozenset({"ddgs.extract", "extract", "extract_url"})
_LLM_TOOL_NAMES: frozenset[str] = frozenset({"llm.complete", "llm.call", "llm"})


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _search_input(arguments: dict[str, Any]) -> dict[str, Any]:
    """The semantic input to a search call — stable across record/replay
    (matches `harness.providers._search_payload`)."""
    return {
        "query": str(arguments.get("query", "")),
        "limit": int(arguments.get("limit", 10) or 10),
        "deny": sorted(arguments.get("deny") or []),
    }


def _extract_input(arguments: dict[str, Any]) -> dict[str, Any]:
    return {"url": str(arguments.get("url", ""))}


def _llm_input(arguments: dict[str, Any]) -> dict[str, Any]:
    """The semantic input to an LLM call — stable across record/replay
    (matches `harness.router._req_payload`)."""
    raw_msgs = arguments.get("messages") or []
    norm_msgs: list[dict[str, Any]] = []
    for m in raw_msgs:
        if isinstance(m, dict):
            norm_msgs.append(
                {
                    "role": str(m.get("role", "")),
                    "content": m.get("content", ""),
                    "tool_calls": m.get("tool_calls"),
                    "tool_call_id": m.get("tool_call_id"),
                    "images": m.get("images"),
                }
            )
    raw_profile = arguments.get("profile")
    if isinstance(raw_profile, dict):
        raw_role = str(raw_profile.get("role", ""))
        difficulty = str(raw_profile.get("difficulty", "routine"))
        requirements = sorted(_as_list(raw_profile.get("requirements")))
        mode = raw_profile.get("mode")
    else:
        raw_role = str(arguments.get("role", ""))
        difficulty = str(arguments.get("difficulty", "routine"))
        requirements = sorted(_as_list(arguments.get("requirements")))
        mode = arguments.get("mode")
    role = raw_role.rsplit(".", 1)[-1].lower()
    return {
        "profile": {
            "role": role,
            "difficulty": difficulty.rsplit(".", 1)[-1].lower(),
            "requirements": requirements,
            "mode": mode,
        },
        "messages": norm_msgs,
        "tools": _as_list(arguments.get("tools")),
        "assistant_prefill": arguments.get("assistant_prefill"),
        "temperature": arguments.get("temperature", 0.0),
        "max_tokens": arguments.get("max_tokens"),
        "response_format": arguments.get("response_format", "text"),
        "assist": bool(arguments.get("assist", False)),
        "enable_thinking": arguments.get("enable_thinking"),
        "attempt": int(arguments.get("attempt", 1) or 1),
        "provider_prefs": arguments.get("provider_prefs"),
        "stream": bool(arguments.get("stream", False)),
    }


def _classify_seam(
    tool_name: str, arguments: dict[str, Any]
) -> tuple[str | None, dict[str, Any] | None]:
    """Map a tool name to its cassette seam and input payload."""
    if tool_name in _SEARCH_TOOL_NAMES:
        return "search", _search_input(arguments)
    if tool_name in _EXTRACT_TOOL_NAMES:
        return "extract", _extract_input(arguments)
    if tool_name in _LLM_TOOL_NAMES:
        return "llm.complete", _llm_input(arguments)
    return None, None


def _paired_observation(
    action_id: Any,
    observations_by_action: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    """Return the first matching observation for an action id."""
    if not isinstance(action_id, str):
        return None
    obs_list = observations_by_action.get(action_id)
    if obs_list:
        return obs_list[0]
    return None


def _observation_output(paired_obs: dict[str, Any]) -> Any:
    """Extract the serialized output from a paired observation."""
    tool_result = paired_obs.get("tool_result") or {}
    if not isinstance(tool_result, dict):
        return None
    output = tool_result.get("structured")
    if output is None:
        output = tool_result.get("content")
    return output


def project_cassette_rows(scrubbed_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project a list of SCRUBBED event payloads (already through
    `redact_event_payload`) into the cassette row format.

    The projection is order-preserving and idempotent on duplicates: a single
    call pair (action → observation) yields exactly one row, keyed by
    `cassette_key(seam, input)` so the same logical call from the live
    recorder or a re-imported bundle hashes to the same key.

    Skips events that don't pair cleanly (orphan action / orphan observation /
    unknown tool name) — those are UI / plan / status events that don't
    represent a service call, and dropping them keeps the projection pure."""
    # Index observations by their action_id so we can pair action→observation
    # in a single pass.
    observations_by_action: dict[str, list[dict[str, Any]]] = {}
    for ev in scrubbed_events:
        if ev.get("kind") == "observation":
            action_id = ev.get("action_id")
            if isinstance(action_id, str):
                observations_by_action.setdefault(action_id, []).append(ev)

    rows: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    for ev in scrubbed_events:
        if ev.get("kind") != "action":
            continue
        tool_call = ev.get("tool_call") or {}
        if not isinstance(tool_call, dict):
            continue
        tool_name = str(tool_call.get("tool_name", ""))
        arguments = tool_call.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}

        seam, input_payload = _classify_seam(tool_name, arguments)
        if seam is None or input_payload is None:
            continue

        paired_obs = _paired_observation(ev.get("id"), observations_by_action)
        if paired_obs is None:
            # Orphan action — skip (the live recorder requires a paired
            # observation to capture the output; we mirror that contract).
            continue

        output = _observation_output(paired_obs)
        if output is None:
            continue

        key = cassette_key(seam, input_payload)
        if (seam, key) in seen_keys:
            continue
        seen_keys.add((seam, key))

        rows.append(
            {
                "seam": seam,
                "key": key,
                "input": input_payload,
                "output": output,
            }
        )

    return rows
