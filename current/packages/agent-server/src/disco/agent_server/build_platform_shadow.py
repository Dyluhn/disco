"""Observe-only legacy/Platform-Core comparison for Build loop composition."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, Literal

from disco.core.build_platform import (
    BuildComposition,
    FrozenModel,
    ObserveOnlyShadowRecord,
    ShadowFieldComparison,
    ToolDescriptor,
    compare_observe_only,
    failed_observe_only,
)
from disco.core.build_platform.builtin_profiles import (
    resolve_builtin_composition_for_delivery,
)
from disco.core.build_platform.shadow import expected_legacy_snapshot_for_delivery
from disco.core.env import disco_env

logger = logging.getLogger(__name__)

_SHADOW_FLAG = "BUILD_PLATFORM_SHADOW"
_FREEFORM_ROUTE_FLAG = "FREEFORM_PLATFORM_ROUTE"
_APPKIT_ROUTE_FLAG = "APPKIT_PLATFORM_ROUTE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no", "off", "disabled"})


class BuildPlatformRouteError(RuntimeError):
    """The requested Platform route could not prove exact legacy parity."""


class BuildPlatformRouteRecord(FrozenModel):
    """Inspectable new-run decision; legacy host code remains the effect bridge."""

    source: Literal["freeform", "appkit"]
    active_route: Literal["platform"] = "platform"
    composition_authority: Literal["build_platform_core"] = "build_platform_core"
    execution_bridge: Literal["legacy_host"] = "legacy_host"
    composition_digest: str
    composition: BuildComposition
    comparisons: tuple[ShadowFieldComparison, ...]
    rollback_switch: Literal["DISCO_FREEFORM_PLATFORM_ROUTE", "DISCO_APPKIT_PLATFORM_ROUTE"]


def build_platform_shadow_enabled() -> bool:
    """Opt-in Phase-3 observer. Runtime authority remains legacy either way."""

    return str(disco_env(_SHADOW_FLAG) or "").strip().lower() in _TRUTHY


def freeform_platform_route_enabled() -> bool:
    """Immediate new-run cutover/rollback switch; default remains legacy."""

    return str(disco_env(_FREEFORM_ROUTE_FLAG) or "").strip().lower() in _TRUTHY


def appkit_platform_route_enabled() -> bool:
    """AppKit cutover defaults on; an explicit false value rolls new runs back."""

    value = disco_env(_APPKIT_ROUTE_FLAG)
    return value is None or str(value).strip().lower() not in _FALSEY


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
    delivery_kind: Literal["app", "files"],
) -> ObserveOnlyShadowRecord:
    """Resolve and compare without returning any value the loop can execute."""

    source = "appkit" if appkit_mode else "freeform"
    try:
        catalog = _tool_catalog(tool_specs)
        visible_tools = frozenset(tool.name for tool in catalog)
        composition = resolve_builtin_composition_for_delivery(
            appkit=appkit_mode,
            goal="observe existing Build composition",
            tool_catalog=catalog,
            visible_tools=visible_tools,
            delivery_kind=delivery_kind,
        )
        record = compare_observe_only(
            expected_legacy_snapshot_for_delivery(
                appkit=appkit_mode,
                visible_tools=visible_tools,
                delivery_kind=delivery_kind,
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


def select_freeform_platform_route(
    *,
    tool_specs: Iterable[Any],
    delivery_kind: Literal["app", "files"],
) -> BuildPlatformRouteRecord:
    """Resolve the exact Freeform composition or fail closed before execution."""

    catalog = _tool_catalog(tool_specs)
    visible_tools = frozenset(tool.name for tool in catalog)
    composition = resolve_builtin_composition_for_delivery(
        appkit=False,
        goal="admit existing Freeform Build execution",
        tool_catalog=catalog,
        visible_tools=visible_tools,
        delivery_kind=delivery_kind,
    )
    comparison = compare_observe_only(
        expected_legacy_snapshot_for_delivery(
            appkit=False,
            visible_tools=visible_tools,
            delivery_kind=delivery_kind,
        ),
        composition,
    )
    if not comparison.matches:
        mismatches = ", ".join(item.field for item in comparison.comparisons if not item.matches)
        detail = comparison.resolution_error or mismatches or "unknown mismatch"
        raise BuildPlatformRouteError(f"Freeform Platform parity failed: {detail}")
    if composition.blocked_operations:
        blocks = ", ".join(
            f"{block.operation}:{block.code}" for block in composition.blocked_operations
        )
        raise BuildPlatformRouteError(f"Freeform Platform composition is blocked: {blocks}")
    return BuildPlatformRouteRecord(
        source="freeform",
        composition_digest=composition.digest.digest,
        composition=composition,
        comparisons=comparison.comparisons,
        rollback_switch="DISCO_FREEFORM_PLATFORM_ROUTE",
    )


def select_appkit_platform_route(
    *,
    tool_specs: Iterable[Any],
) -> BuildPlatformRouteRecord:
    """Resolve the exact strict AppKit composition or fail before execution."""

    catalog = _tool_catalog(tool_specs)
    visible_tools = frozenset(tool.name for tool in catalog)
    composition = resolve_builtin_composition_for_delivery(
        appkit=True,
        goal="admit existing governed AppKit execution",
        tool_catalog=catalog,
        visible_tools=visible_tools,
        delivery_kind="app",
    )
    comparison = compare_observe_only(
        expected_legacy_snapshot_for_delivery(
            appkit=True,
            visible_tools=visible_tools,
            delivery_kind="app",
        ),
        composition,
    )
    if not comparison.matches:
        mismatches = ", ".join(item.field for item in comparison.comparisons if not item.matches)
        detail = comparison.resolution_error or mismatches or "unknown mismatch"
        raise BuildPlatformRouteError(f"AppKit Platform parity failed: {detail}")
    if composition.blocked_operations:
        blocks = ", ".join(
            f"{block.operation}:{block.code}" for block in composition.blocked_operations
        )
        raise BuildPlatformRouteError(f"AppKit Platform composition is blocked: {blocks}")
    return BuildPlatformRouteRecord(
        source="appkit",
        composition_digest=composition.digest.digest,
        composition=composition,
        comparisons=comparison.comparisons,
        rollback_switch="DISCO_APPKIT_PLATFORM_ROUTE",
    )
