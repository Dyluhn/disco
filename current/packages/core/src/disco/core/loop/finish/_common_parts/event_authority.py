"""Decomposition helpers for the four finish-path callables that exceeded the
McCabe budget in `finish/common.py`: `_last_verification_authority_seq`,
`_is_web_deliverable`, `_real_web_failure_evidence`, and `_browser_verified`.

Each public function stays DEFINED in `common.py` (callers — `verify_gates.py`,
`content_gates.py`, `finish/__init__.py` — import it from there); this module
holds the inner branching as pure, module-level helpers taking explicit
arguments, so the public function bodies are thin dispatchers. Behavior,
scan direction, and short-circuit order are unchanged from the pre-split
bodies — only the branches' physical location moved.
"""

from __future__ import annotations

from typing import Any

from ....effects import EffectCapability
from ....events import ActionEvent, AgentErrorEvent, Event, ObservationEvent, ToolResult
from .browser_signals import _browser_content_meaningful
from .preview_urls import _safe_deliverable_file_path, _url_targets_preview

# ---------------------------------------------------------------------------
# _last_verification_authority_seq decomposition
# ---------------------------------------------------------------------------


def _mutation_capability(event: ObservationEvent | AgentErrorEvent) -> bool:
    """Was `event` capable of workspace-mutating side effects?

    Extracted verbatim from the `mutation_capability` closure formerly nested
    inside `_last_verification_authority_seq`."""
    if isinstance(event, ObservationEvent):
        profile = event.tool_result.action_profile
        receipts = event.tool_result.effect_receipts
    else:
        profile = event.action_profile
        receipts = event.effect_receipts
    return bool(
        (profile is not None and EffectCapability.WORKSPACE_MUTATE in profile.capabilities)
        or any(
            getattr(receipt, "capability", None) is EffectCapability.WORKSPACE_MUTATE
            for receipt in receipts
        )
    )


def _mutation_authority_update(
    event: ObservationEvent | AgentErrorEvent, authority_seq: int
) -> int:
    """Workspace-mutation authority bump shared by the ObservationEvent and
    AgentErrorEvent branches of `_last_verification_authority_seq`'s per-event
    loop — factored out to keep that loop within the McCabe budget."""
    if _mutation_capability(event) and type(event.seq) is int:
        return max(authority_seq, event.seq)
    return authority_seq


def _preview_action_paired(
    action: ActionEvent | None, result: ToolResult, event_seq: int
) -> bool:
    """Is `result` (a preview_start/preview_stop observation) bound to a
    matching, time-ordered, successful action? Shared pairing-validity half of
    `_preview_authority_update`'s combined check, factored out to keep that
    function within the McCabe budget. Condition order is preserved verbatim
    (``action is None`` first) so the short-circuit never dereferences a None
    action."""
    return not (
        action is None
        or action.tool_call.tool_name != result.tool_name
        or action.tool_call.call_id != result.call_id
        or type(action.seq) is not int
        or action.seq >= event_seq
        or result.success is not True
    )


def _preview_start_authority(
    structured: dict[str, Any],
    active_preview_sessions: set[str],
    authority_seq: int,
    event_seq: int,
) -> int:
    """`preview_start` half of `_preview_authority_update`."""
    name = structured.get("name")
    if (
        structured.get("status") not in {"running", "unavailable"}
        or not isinstance(name, str)
        or not name
    ):
        return authority_seq
    active_preview_sessions.add(name)
    return max(authority_seq, event_seq)


def _preview_stop_authority(
    structured: dict[str, Any],
    active_preview_sessions: set[str],
    authority_seq: int,
    event_seq: int,
) -> int:
    """`preview_stop` half of `_preview_authority_update`."""
    stopped = structured.get("stopped")
    if (
        not isinstance(stopped, list)
        or not all(isinstance(name, str) and name for name in stopped)
        or not stopped
    ):
        return authority_seq
    active_preview_sessions.difference_update(stopped)
    return max(authority_seq, event_seq)


def _preview_authority_update(
    event: ObservationEvent,
    actions: dict[str, ActionEvent],
    active_preview_sessions: set[str],
    authority_seq: int,
) -> int:
    """`preview_start` / `preview_stop` authority handling, extracted from
    `_last_verification_authority_seq`'s per-event loop. Mutates
    `active_preview_sessions` in place and returns the (possibly advanced)
    authority_seq — a causally paired successful start/stop changes the
    browser authority even though preview controls are nonproductive for
    execution accounting."""
    result = event.tool_result
    if result.tool_name not in {"preview_start", "preview_stop"}:
        return authority_seq
    event_seq = event.seq
    if type(event_seq) is not int:
        return authority_seq
    action = actions.get(event.action_id)
    structured = result.structured
    if not _preview_action_paired(action, result, event_seq) or not isinstance(structured, dict):
        return authority_seq
    if result.tool_name == "preview_start":
        return _preview_start_authority(
            structured, active_preview_sessions, authority_seq, event_seq
        )
    return _preview_stop_authority(structured, active_preview_sessions, authority_seq, event_seq)


# ---------------------------------------------------------------------------
# _is_web_deliverable decomposition
# ---------------------------------------------------------------------------


def _preview_start_pair_observed(events: list[Event]) -> bool:
    """H357: detect a successful preview_start action/observation pair.  The
    observation must be bound to the matching action id and the tool name
    must be "preview_start".  A failed, malformed, unpaired/orphaned,
    wrong-tool, or action-only preview record must not create this signal.
    Uses a SINGLE forward scan so that an observation before its causal
    action (forged/reordered) never qualifies — only an action-then-
    observation pair counts."""
    preview_action_ids: set[str] = set()
    for ev in events:
        if isinstance(ev, ActionEvent) and ev.tool_call.tool_name == "preview_start":
            preview_action_ids.add(ev.id)
        elif (
            isinstance(ev, ObservationEvent)
            and ev.tool_result.tool_name == "preview_start"
            and ev.tool_result.success
            and isinstance(ev.action_id, str)
            and ev.action_id in preview_action_ids
        ):
            return True
    return False


def _index_html_or_port_owned(events: list[Event]) -> bool:
    """Existing: index.html was written/edited OR port 8000 owned by non-preview."""
    for ev in reversed(events):
        if isinstance(ev, ActionEvent):
            if ev.tool_call.tool_name in (
                "file_write",
                "file_edit",
                "file_append",
                "file_replace_lines",
                "file_insert_lines",
            ):
                path = ev.tool_call.arguments.get("path")
                # Canonical workspace-root paths
                if isinstance(path, str) and _safe_deliverable_file_path(path) == "index.html":
                    return True
        elif isinstance(ev, ObservationEvent) and ev.tool_result.tool_name == "server_status":
            content = ev.tool_result.content
            # "  - 8000: OWNED by pid 123 (python) [session: dev]"
            for line in content.splitlines():
                if "8000: OWNED" in line and "[session: " in line:
                    session = line.split("[session: ")[1].split("]")[0]
                    if session != "preview":
                        return True
    return False


# ---------------------------------------------------------------------------
# _real_web_failure_evidence decomposition
# ---------------------------------------------------------------------------


def _verify_web_app_verdict_failed(st: dict) -> bool:
    """`verify_web_app` verdict evaluation half of `_real_web_failure_evidence`:
    console errors, network failures, or an HTTP-200-but-blank render."""
    if st.get("console_errors") or st.get("network_failures"):
        return True
    http_ok = 200 <= int(st.get("http_status") or 0) < 400
    return bool(http_ok and st.get("meaningful_content") is False)


def _browser_observation_failed(success: bool, st: dict) -> bool:
    """`browser` observation evaluation half of `_real_web_failure_evidence`. A
    failed/unavailable browser call is infra, not app-broken, and yields False."""
    if not success:
        return False  # a failed/unavailable browser call is infra, not app-broken
    # console errors — the app threw at runtime.
    if any(c.get("level") == "error" for c in (st.get("console") or [])):
        return True
    # NETWORK failures — the daemon's `network` ring holds failed/4xx-5xx
    # requests (B7). A page that loaded with broken requests is NOT a clean
    # unverifiable delivery.
    if st.get("network") or st.get("network_failures"):
        return True
    # BLANK render — the page served but nothing a user would see mounted
    # (empty/trivial DOM: no meaningful text, no elements/links/forms).
    return not _browser_content_meaningful(st)


# ---------------------------------------------------------------------------
# _browser_verified decomposition
# ---------------------------------------------------------------------------


def _qualifying_browser_observations(
    events: list[Event], since_seq: int, target_key: tuple[str, int] | None
) -> list[dict]:
    """Selection half of `_browser_verified`: the AGENT's browser observations
    since since_seq. An observation is valid if it's from the browser tool,
    against the resolved preview target (`target_key`; :8000 when undetectable —
    see `_url_targets_preview`), and is not the finish gate's OWN driven probe
    (ActionEvent tagged `verify_probe`) — this helper answers "did the AGENT
    verify cleanly", and the gate judges its own probe separately."""
    probe_action_ids = {
        ev.id for ev in events if isinstance(ev, ActionEvent) and ev.meta.get("verify_probe")
    }
    valid_obs = []
    for ev in events:
        if ev.seq is None or ev.seq <= since_seq:
            continue
        if isinstance(ev, ObservationEvent) and ev.tool_result.tool_name == "browser":
            if ev.action_id in probe_action_ids:
                continue  # the gate's own probe — judged actively, not as agent work
            res = ev.tool_result
            if res.success and res.structured:
                url = str(res.structured.get("url", ""))
                if _url_targets_preview(url, target_key):
                    valid_obs.append(res.structured)
    return valid_obs


def _browser_verified_verdict(valid_obs: list[dict]) -> tuple[bool, str | None]:
    """ok / first-error-line derivation half of `_browser_verified`. ok = at
    least one observation with zero console errors; first_error_line = from
    the LATEST qualifying observation that has error-level entries."""
    if not valid_obs:
        return False, None

    # ok = at least one observation with zero console errors
    ok = any(
        not any(c.get("level") == "error" for c in obs.get("console", [])) for obs in valid_obs
    )

    # first_error_line = from the LATEST qualifying-URL observation that has error-level entries
    first_error_line = None
    for obs in reversed(valid_obs):
        errors = [
            str(c.get("text", "")) for c in obs.get("console", []) if c.get("level") == "error"
        ]
        if errors:
            first_error_line = errors[0]
            break

    return ok, first_error_line
