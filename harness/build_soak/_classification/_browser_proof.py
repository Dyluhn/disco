"""Strict browser-verification path extraction from durable events.

A tool-level success is insufficient: ``verify_web_app`` also uses successful
tool transport for an ``unverifiable`` verdict.  Requiring ``structured.passed``
to be exactly true keeps that branch from satisfying a browser-verification
contract. A direct ``browser`` observation is admissible only when it carries
the same strict facts the product finish gate consumes.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

_BROWSER_VERIFICATION_TOOLS = frozenset({"verify_web_app", "verify_appkit_app"})
_DELIVERABLE_MUTATION_TOOLS = frozenset(
    {
        "file_write",
        "file_edit",
        "file_append",
        "file_replace_lines",
        "file_insert_lines",
        "file_str_replace",
        "exact_replace",
        "safe_write_file",
        "write_file",
        "app_create",
        "app_update_content",
        "app_add_section",
        "app_remove_section",
        "app_reorder_section",
        "app_set_design",
        "app_set_tweak",
        "app_add_primitive",
    }
)


def _admissible_browser_screenshot_path(path: str) -> bool:
    """Mirror H190's exact product-owned screenshot namespace."""
    parts = path.split("/")
    return (
        bool(path)
        and not path.startswith("/")
        and "\\" not in path
        and "\x00" not in path
        and len(parts) == 3
        and parts[:2] == [".pmx", "screenshots"]
        and not any(part in {"", ".", ".."} for part in parts)
        and parts[2].endswith(".png")
    )


def _local_url_port(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        explicit_port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or explicit_port is None
    ):
        return None
    return explicit_port


def _paired_action(
    event: dict[str, Any],
    result: dict[str, Any],
    actions_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    action_id = event.get("action_id")
    if not isinstance(action_id, str):
        return None
    candidate = actions_by_id.get(action_id)
    if candidate is None:
        return None
    tool_call = candidate.get("tool_call")
    if not isinstance(tool_call, dict):
        return None
    call_id = tool_call.get("call_id")
    if not isinstance(call_id, str) or result.get("call_id") != call_id:
        return None
    if result.get("tool_name") != tool_call.get("tool_name"):
        return None
    return candidate


def _last_mutation_observation_seq(
    events: list[dict[str, Any]],
) -> int:
    """Seq of the latest successful deliverable-mutation observation."""
    mutation_actions: dict[str, dict[str, Any]] = {}
    last_seq = 0
    for event in events:
        if event.get("kind") == "action":
            event_id = event.get("id")
            if isinstance(event_id, str):
                mutation_actions[event_id] = event
            continue
        if event.get("kind") != "observation":
            continue
        result = event.get("tool_result")
        action_id = event.get("action_id")
        seq = event.get("seq")
        if (
            not isinstance(result, dict)
            or result.get("success") is not True
            or not isinstance(action_id, str)
            or not isinstance(seq, int)
        ):
            continue
        action_event = mutation_actions.get(action_id)
        tool_call = action_event.get("tool_call") if isinstance(action_event, dict) else None
        if not isinstance(tool_call, dict):
            continue
        tool_name = tool_call.get("tool_name")
        if (
            tool_name in _DELIVERABLE_MUTATION_TOOLS
            and result.get("tool_name") == tool_name
            and result.get("call_id") == tool_call.get("call_id")
        ):
            last_seq = max(last_seq, seq)
    return last_seq


def _validate_browser_console(structured: dict[str, Any]) -> bool:
    """Validate console has no errors and network is clean."""
    console = structured.get("console")
    network = structured.get("network")
    if not isinstance(console, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("level"), str)
        and isinstance(item.get("text"), str)
        for item in console
    ):
        return False
    if any(item["level"].lower() == "error" for item in console):
        return False
    return isinstance(network, list) and not network


def _validate_browser_content(structured: dict[str, Any]) -> bool:
    """Validate title, text, semantic elements, and element list."""
    title = structured.get("title")
    text = structured.get("text")
    if not isinstance(title, str) or not isinstance(text, str):
        return False
    semantic_count = structured.get("visible_semantic_elements")
    if not isinstance(semantic_count, int) or isinstance(semantic_count, bool):
        return False
    semantic = semantic_count > 0
    elements = structured.get("elements")
    if not isinstance(elements, list) or not all(
        isinstance(item, str) and bool(item.strip()) for item in elements
    ):
        return False
    meaningful = len((title + " " + text).strip()) >= 20 or semantic or bool(elements)
    path = structured.get("screenshot_path")
    return meaningful and isinstance(path, str) and bool(path)


def _validate_screenshot_freshness(structured: dict[str, Any]) -> bool:
    """Validate the browser daemon's workspace synchronization receipt."""
    freshness = structured.get("freshness")
    if not isinstance(freshness, dict):
        return False
    requested_epoch = freshness.get("requested_epoch")
    synchronized_epoch = freshness.get("synchronized_epoch")
    return (
        isinstance(requested_epoch, int)
        and not isinstance(requested_epoch, bool)
        and isinstance(synchronized_epoch, int)
        and not isinstance(synchronized_epoch, bool)
        and requested_epoch == synchronized_epoch
        and isinstance(freshness.get("sync_performed"), bool)
        and freshness.get("page_kind") == "local_preview"
    )


def _validate_browser_action_port(
    arguments: dict[str, Any],
    structured: dict[str, Any],
    selected_port: int | None,
) -> bool:
    """Validate the port binding for navigate/screenshot actions."""
    action = arguments.get("action")
    action_url = arguments.get("url")
    action_port = _local_url_port(action_url)
    observed_port = _local_url_port(structured.get("url"))
    if selected_port is None or observed_port != selected_port:
        return False
    if action == "navigate" or (isinstance(action_url, str) and action_url):
        if action_port != selected_port:
            return False
    if action == "screenshot":
        return _validate_screenshot_freshness(structured)
    return True


def _strict_direct_browser(
    structured: dict[str, Any],
    action_event: dict[str, Any],
    selected_port: int | None,
) -> bool:
    """Whether a direct ``browser`` observation carries strict finish-gate facts."""
    if structured.get("ok") is not True:
        return False
    tool_call = action_event.get("tool_call")
    if not isinstance(tool_call, dict) or tool_call.get("tool_name") != "browser":
        return False
    arguments = tool_call.get("arguments")
    if not isinstance(arguments, dict):
        return False
    action = arguments.get("action")
    if action not in {"navigate", "screenshot"}:
        return False
    if not _validate_browser_action_port(arguments, structured, selected_port):
        return False
    if not _validate_browser_console(structured):
        return False
    return _validate_browser_content(structured)


def _collect_screenshot_paths(value: Any, paths: set[str]) -> None:
    """Recursively collect screenshot_path keys from a nested structure."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "screenshot_path" or key.endswith("_screenshot_path"):
                if isinstance(child, str) and child:
                    paths.add(child)
            else:
                _collect_screenshot_paths(child, paths)
    elif isinstance(value, list):
        for child in value:
            _collect_screenshot_paths(child, paths)


def _filter_post_restore(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop events at or before the latest workspace restore fence."""
    restore_fence = max(
        (
            event["seq"]
            for event in events
            if (
                (
                    event.get("kind") == "workspace_mutation"
                    and event.get("operation") == "version.restore"
                )
                or event.get("kind") == "workspace_restored"
            )
            and type(event.get("seq")) is int
        ),
        default=0,
    )
    if restore_fence:
        events = [
            event
            for event in events
            if type(event.get("seq")) is int and event["seq"] > restore_fence
        ]
    return events


def _process_preview_start(
    structured: dict[str, Any], event: dict[str, Any], active_previews: dict[str, tuple[int, int]]
) -> None:
    """Track a running preview from a successful preview_start observation."""
    name = structured.get("name")
    port = structured.get("port")
    seq = event.get("seq")
    if (
        isinstance(name, str)
        and bool(name)
        and isinstance(port, int)
        and not isinstance(port, bool)
        and 1 <= port <= 65535
        and isinstance(seq, int)
        and structured.get("status") == "running"
    ):
        active_previews[name] = (seq, port)


def _process_preview_stop(
    structured: dict[str, Any], active_previews: dict[str, tuple[int, int]]
) -> None:
    """Remove stopped previews, or clear all on a malformed stop result."""
    stopped = structured.get("stopped")
    if isinstance(stopped, list) and all(isinstance(name, str) and bool(name) for name in stopped):
        for name in stopped:
            active_previews.pop(name, None)
    else:
        active_previews.clear()


def _process_browser_observation(
    structured: dict[str, Any],
    action_event: dict[str, Any] | None,
    proof_is_fresh: bool,
    active_previews: dict[str, tuple[int, int]],
    paths: set[str],
) -> None:
    """Collect screenshot paths from a strict direct browser observation."""
    if action_event is None or not proof_is_fresh:
        return
    selected_port = (
        max(active_previews.values(), key=lambda preview: preview[0])[1]
        if active_previews
        else None
    )
    if _strict_direct_browser(structured, action_event, selected_port):
        _collect_screenshot_paths(structured, paths)


def _process_verification_observation(
    structured: dict[str, Any],
    tool_name: str,
    proof_is_fresh: bool,
    paths: set[str],
) -> None:
    """Collect screenshot paths from a passing verify_web_app/verify_appkit_app."""
    if (
        not proof_is_fresh
        or tool_name not in _BROWSER_VERIFICATION_TOOLS
        or structured.get("passed") is not True
    ):
        return
    _collect_screenshot_paths(structured, paths)


def _process_verifier_verdict(
    event: dict[str, Any],
    last_mutation_seq: int,
    paths: set[str],
) -> None:
    """Collect an admissible screenshot path from a passing verifier_verdict."""
    path = event.get("screenshot_path")
    if (
        isinstance(event.get("seq"), int)
        and event["seq"] > last_mutation_seq
        and event.get("verified") is True
        and event.get("verdict") == "pass"
        and isinstance(path, str)
        and _admissible_browser_screenshot_path(path)
    ):
        paths.add(path)


def _process_observation_event(
    event: dict[str, Any],
    last_mutation_seq: int,
    actions_by_id: dict[str, dict[str, Any]],
    active_previews: dict[str, tuple[int, int]],
    paths: set[str],
) -> None:
    """Process one observation event for browser/preview/verification evidence."""
    event_seq = event.get("seq")
    proof_is_fresh = isinstance(event_seq, int) and event_seq > last_mutation_seq
    result = event.get("tool_result")
    if not isinstance(result, dict) or result.get("success") is not True:
        return
    structured = result.get("structured")
    if not isinstance(structured, dict):
        return
    tool_name = result.get("tool_name")
    action_event = _paired_action(event, result, actions_by_id)
    if action_event is None:
        return
    if tool_name == "preview_start":
        _process_preview_start(structured, event, active_previews)
    elif tool_name == "preview_stop":
        _process_preview_stop(structured, active_previews)
    elif tool_name == "browser":
        _process_browser_observation(
            structured, action_event, proof_is_fresh, active_previews, paths
        )
    elif isinstance(tool_name, str):
        _process_verification_observation(structured, tool_name, proof_is_fresh, paths)


def successful_browser_verification_paths(events: list[dict[str, Any]]) -> set[str]:
    """Return screenshot paths claimed by strict passing browser evidence.

    A tool-level success is insufficient: ``verify_web_app`` also uses successful
    tool transport for an ``unverifiable`` verdict.  Requiring ``structured.passed``
    to be exactly true keeps that branch from satisfying a browser-verification
    contract. A direct ``browser`` observation is admissible only when it carries
    the same strict facts the product finish gate consumes: successful render on an
    explicit loopback preview port, clean console/network, meaningful content, and
    a screenshot path. Screenshot keys may be nested (notably for AppKit
    interactions), so collect the same explicit path-key family retained by the
    H190 capture. Host ``verifier_verdict`` events are accepted only with exact
    verified/pass fields and one admissible top-level screenshot path.
    """
    events = _filter_post_restore(events)

    paths: set[str] = set()
    actions_by_id: dict[str, dict[str, Any]] = {}
    active_previews: dict[str, tuple[int, int]] = {}
    last_mutation_seq = _last_mutation_observation_seq(events)

    for event in events:
        if event.get("kind") == "action":
            event_id = event.get("id")
            if isinstance(event_id, str):
                actions_by_id[event_id] = event
            continue
        if event.get("kind") == "verifier_verdict":
            _process_verifier_verdict(event, last_mutation_seq, paths)
            continue
        if event.get("kind") != "observation":
            continue
        _process_observation_event(event, last_mutation_seq, actions_by_id, active_previews, paths)
    return paths
