"""Observe-only legacy/Platform-Core comparison for Build loop composition."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from disco.core.build_platform import (
    ObserveOnlyShadowRecord,
    ToolDescriptor,
    compare_observe_only,
    expected_legacy_snapshot,
    failed_observe_only,
    resolve_builtin_composition,
)
from disco.core.env import disco_env

logger = logging.getLogger(__name__)

_SHADOW_FLAG = "BUILD_PLATFORM_SHADOW"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def build_platform_shadow_enabled() -> bool:
    """Opt-in Phase-3 observer. Runtime authority remains legacy either way."""

    return str(disco_env(_SHADOW_FLAG) or "").strip().lower() in _TRUTHY


def _tool_catalog(tool_specs: Iterable[Any]) -> tuple[ToolDescriptor, ...]:
    descriptors: list[ToolDescriptor] = []
    for spec in tool_specs:
        name = getattr(spec, "name", None)
        description = getattr(spec, "description", "")
        if not isinstance(name, str):
            raise ValueError("legacy tool spec has no string name")
        descriptors.append(
            ToolDescriptor(
                name=name,
                description=description if isinstance(description, str) else "",
            )
        )
    return tuple(sorted(descriptors, key=lambda tool: tool.name))


def observe_legacy_build(
    *,
    appkit_mode: bool,
    tool_specs: Iterable[Any],
) -> ObserveOnlyShadowRecord:
    """Resolve and compare without returning any value the loop can execute."""

    source = "appkit" if appkit_mode else "freeform"
    try:
        catalog = _tool_catalog(tool_specs)
        visible_tools = frozenset(tool.name for tool in catalog)
        composition = resolve_builtin_composition(
            appkit=appkit_mode,
            goal="observe existing Build composition",
            tool_catalog=catalog,
            visible_tools=visible_tools,
        )
        record = compare_observe_only(
            expected_legacy_snapshot(
                appkit=appkit_mode,
                visible_tools=visible_tools,
            ),
            composition,
        )
    except Exception as exc:
        record = failed_observe_only(source=source, error=exc)  # type: ignore[arg-type]
    logger.info(
        "build-platform shadow source=%s match=%s digest=%s blocks=%s error=%s",
        record.source,
        record.matches,
        record.composition_digest,
        record.platform_blocks,
        record.resolution_error,
        extra={
            "event": "build_platform_shadow",
            "source": record.source,
            "matches": record.matches,
            "composition_digest": record.composition_digest,
            "blocks": record.platform_blocks,
            "error": record.resolution_error,
            "legacy_authoritative": True,
            "active_route": "legacy",
        },
    )
    return record
