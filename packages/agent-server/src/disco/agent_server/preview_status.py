"""Passive preview lifecycle status projection."""

from __future__ import annotations

from typing import Any


def empty_preview_metadata(*, port: int | None = None) -> dict[str, Any]:
    return {
        "port": port,
        "status": None,
        "generation": None,
        "launch_kind": None,
        "reload_strategy": None,
        "update_error": None,
    }


def managed_preview_metadata(manager: Any) -> tuple[dict[str, Any], str]:
    """Read the canonical lifecycle snapshot without probing or launching."""

    if manager is None:
        return empty_preview_metadata(), ""
    try:
        lifecycle = manager.canonical_lifecycle_session()
        data = lifecycle.to_dict() if lifecycle is not None else None
    except Exception:  # noqa: BLE001 — malformed registry fails closed
        data = None
    if not isinstance(data, dict):
        return empty_preview_metadata(), ""
    port = data.get("port")
    if not isinstance(port, int) or isinstance(port, bool):
        port = None
    status = data.get("status")
    generation = data.get("generation")
    launch_kind = data.get("launch_kind")
    reload_strategy = data.get("reload_strategy")
    metadata = {
        "port": port,
        "status": status if isinstance(status, str) else None,
        "generation": generation if isinstance(generation, str) else None,
        "launch_kind": launch_kind if isinstance(launch_kind, str) else None,
        "reload_strategy": reload_strategy if reload_strategy in {"hmr", "reload"} else None,
        "update_error": (
            data.get("update_error") if isinstance(data.get("update_error"), str) else None
        ),
    }
    detail = data.get("detail")
    return metadata, detail if isinstance(detail, str) else ""


def managed_unavailable_reason(status: str | None, detail: str) -> str | None:
    if status == "starting":
        return "Preparing preview: the managed runtime is starting."
    if status == "restarting":
        return "Preparing preview: the managed runtime is restarting."
    if status == "crashed":
        suffix = f" {detail}" if detail else ""
        return f"The managed preview runtime crashed.{suffix}"
    if status == "stopped":
        return "The managed preview runtime is stopped."
    if status == "unavailable":
        return detail or "The managed preview is healthy but cannot be exposed here."
    return None
