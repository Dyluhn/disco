"""The single owner of tool-call identity ("did the model issue THIS call before?").

Two independent consumers need the same answer to that question and must never
answer it separately:

* the **loop**, deciding whether to surface the idempotent-observation memo
  before a repeat executes (`loop/dedup.py`, W-39);
* the **harness** `ThrashOracle`, deciding after the fact whether a run thrashed
  (`harness/build_soak/oracles/_thrash_checks.py`).

A6.3 §3.1 makes one owner a *binding* design constraint, and the reason is
specific: a memo that fires on a different equivalence class than the oracle
that grades the run is worse than no memo at all — the drift would be invisible
until a canary fired on a class the memo had never seen. So the normalization
lives here, in the lowest layer, and both sides import it. It is never copied.

The ordinary equivalence class is deliberately **exact**: tool name plus
canonical-JSON argument equality. Stateful browser actions add the semantic
browser state that existed before the action. Pressing ``P`` while a game is
playing and pressing ``P`` while it is paused are not the same question even
though the tool arguments are byte-identical. Observation-only browser probes
remain exact-argument calls, so repeated screenshots and console reads stay
bounded.

Why canonical JSON rather than Python `==` on the argument dicts: `==` treats
`{"n": 1}` and `{"n": 1.0}` as equal and cannot compare values that are not
directly comparable, while the oracle grades runs from *serialized* events where
that distinction survives. Keying both sides on the same rendering removes the
disagreement instead of documenting it.

Import-free by design (stdlib only) so the harness can depend on it without
pulling product machinery into evidence adjudication.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

# The placeholder for a call whose tool name is missing or empty. Kept as a
# module constant so both consumers render an unnamed tool identically rather
# than one emitting "" and the other "?" for the same event.
UNKNOWN_TOOL = "?"

BROWSER_STATEFUL_ACTIONS: frozenset[str] = frozenset(
    {"back", "click", "fill", "navigate", "press", "submit"}
)
_BROWSER_CAPTURE_ACTIONS: frozenset[str] = BROWSER_STATEFUL_ACTIONS | {"screenshot"}
_BROWSER_STATE_FIELDS: tuple[str, ...] = (
    "url",
    "title",
    "text",
    "visible_dom_text",
    "elements",
    "appkit_sections",
    "canvas_count",
    "document_content_type",
)
_UNKNOWN_BROWSER_STATE = "unknown"


def canonical_arguments(arguments: Mapping[str, Any] | None) -> str:
    """The byte-stable rendering of a tool call's arguments.

    ``sort_keys`` makes key order irrelevant, the tight separators keep the
    rendering free of incidental whitespace, and ``default=str`` guarantees the
    call never raises on a value JSON cannot encode — an identity function that
    throws would turn an unusual argument into a crash in the middle of grading
    a run.
    """
    return json.dumps(dict(arguments or {}), sort_keys=True, separators=(",", ":"), default=str)


def tool_call_fingerprint(tool_name: str | None, arguments: Mapping[str, Any] | None) -> str:
    """The identity of one tool call: ``"<tool_name>:<canonical arguments>"``.

    Equal fingerprints mean "the model issued this same call again". This is the
    ONLY definition of that in the codebase; see the module docstring.
    """
    return f"{tool_name or UNKNOWN_TOOL}:{canonical_arguments(arguments)}"


def _browser_state_fingerprint(structured: Mapping[str, Any]) -> str | None:
    """Stable semantic state from one successful structured browser capture.

    Screenshot paths, freshness nonces, and other per-call values are excluded;
    they would make an unchanged page look new on every probe.
    """

    state = {key: structured.get(key) for key in _BROWSER_STATE_FIELDS if key in structured}
    if not state:
        return None
    rendered = canonical_arguments(state)
    return hashlib.sha256(rendered.encode("utf-8", "surrogatepass")).hexdigest()


def _contextual_action(
    event: Mapping[str, Any], browser_state: str | None
) -> tuple[str, str, str | None] | None:
    """Parse one action into ``(id, fingerprint, browser_action)``."""

    if str(event.get("kind") or "") != "action":
        return None
    action_id = event.get("id")
    call = event.get("tool_call")
    if not isinstance(action_id, str) or not action_id or not isinstance(call, Mapping):
        return None
    tool_name = call.get("tool_name")
    arguments = call.get("arguments")
    args = arguments if isinstance(arguments, Mapping) else None
    fingerprint = tool_call_fingerprint(
        tool_name if isinstance(tool_name, str) else None,
        args,
    )
    action = args.get("action") if args is not None else None
    if tool_name != "browser" or not isinstance(action, str):
        return action_id, fingerprint, None
    if action in BROWSER_STATEFUL_ACTIONS:
        fingerprint = (
            f"{fingerprint}:browser-state={browser_state or _UNKNOWN_BROWSER_STATE}"
        )
    return action_id, fingerprint, action


def _browser_capture(
    event: Mapping[str, Any], browser_actions: Mapping[str, str]
) -> tuple[str, int] | None:
    """Return one successful common browser capture's state and sequence."""

    if str(event.get("kind") or "") != "observation":
        return None
    action_id = event.get("action_id")
    if not isinstance(action_id, str):
        return None
    if browser_actions.get(action_id) not in _BROWSER_CAPTURE_ACTIONS:
        return None
    result = event.get("tool_result")
    if not isinstance(result, Mapping):
        return None
    if result.get("success") is not True or result.get("tool_name") != "browser":
        return None
    structured = result.get("structured")
    if not isinstance(structured, Mapping):
        return None
    observed = _browser_state_fingerprint(structured)
    if observed is None:
        return None
    seq = event.get("seq")
    return observed, seq if isinstance(seq, int) and not isinstance(seq, bool) else 0


def contextual_tool_calls(
    events: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, str], int]:
    """Return action fingerprints and the latest browser-state transition.

    The stream shape is the durable serialized event contract. Only successful
    browser actions that return a common DOM capture advance browser state.
    ``console_view`` deliberately does not: it returns empty text/elements by
    contract and is an observation, not a state transition.

    Both outputs come from this single fold so the loop's browser-local currency
    boundary and the oracle's contextual identity cannot drift apart.
    """

    browser_state: str | None = None
    browser_actions: dict[str, str] = {}
    fingerprints: dict[str, str] = {}
    latest_state_change_seq = 0
    for event in events:
        action = _contextual_action(event, browser_state)
        if action is not None:
            action_id, fingerprint, browser_action = action
            fingerprints[action_id] = fingerprint
            if browser_action is not None:
                browser_actions[action_id] = browser_action
            continue
        capture = _browser_capture(event, browser_actions)
        if capture is None:
            continue
        observed, seq = capture
        if browser_state is not None and observed != browser_state:
            latest_state_change_seq = max(latest_state_change_seq, seq)
        browser_state = observed
    return fingerprints, latest_state_change_seq
