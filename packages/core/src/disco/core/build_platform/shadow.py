"""Observe-only comparison records; never a routing or execution authority."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .builtin_profiles import (
    APPKIT_ENGINE_ID,
    APPKIT_PROFILE_ID,
    APPKIT_PROMPT_ID,
    APPKIT_RULES,
    APPKIT_VERIFIER_ID,
    FREEFORM_ENGINE_ID,
    FREEFORM_PROFILE_ID,
    FREEFORM_PROMPT_ID,
    FREEFORM_RULES,
    HOST_PREVIEW_ID,
    HOST_VERIFIER_ID,
    LEGACY_EXPORTER_ID,
    WEB_TARGET_ID,
)
from .contracts import ComponentId, FrozenModel, PolicyRule
from .resolver import BuildComposition


class LegacyAuthoritySnapshot(FrozenModel):
    source: Literal["freeform", "appkit"]
    profile: ComponentId
    engine: ComponentId
    target: ComponentId
    policy: tuple[str, ...]
    prompt_modules: tuple[str, ...]
    visible_tools: tuple[str, ...]
    preview: str
    verifier: str
    exporter: str
    errors: tuple[str, ...] = ()


class ShadowFieldComparison(FrozenModel):
    field: str = Field(min_length=1, max_length=96)
    legacy: tuple[str, ...]
    platform: tuple[str, ...]
    matches: bool


class ObserveOnlyShadowRecord(FrozenModel):
    mode: Literal["observe_only"] = "observe_only"
    source: Literal["freeform", "appkit"]
    legacy_authoritative: Literal[True] = True
    active_route: Literal["legacy"] = "legacy"
    composition_digest: str | None = None
    comparisons: tuple[ShadowFieldComparison, ...] = ()
    platform_blocks: tuple[str, ...] = ()
    resolution_error: str | None = None

    @property
    def matches(self) -> bool:
        return self.resolution_error is None and all(
            comparison.matches for comparison in self.comparisons
        )


def _policy_values(rules: tuple[PolicyRule, ...]) -> tuple[str, ...]:
    return tuple(sorted(f"{rule.key}={rule.decision.value}" for rule in rules))


def expected_legacy_snapshot(
    *, appkit: bool, visible_tools: frozenset[str]
) -> LegacyAuthoritySnapshot:
    source: Literal["freeform", "appkit"] = "appkit" if appkit else "freeform"
    return LegacyAuthoritySnapshot(
        source=source,
        profile=APPKIT_PROFILE_ID if appkit else FREEFORM_PROFILE_ID,
        engine=APPKIT_ENGINE_ID if appkit else FREEFORM_ENGINE_ID,
        target=WEB_TARGET_ID,
        policy=_policy_values(APPKIT_RULES if appkit else FREEFORM_RULES),
        prompt_modules=((APPKIT_PROMPT_ID if appkit else FREEFORM_PROMPT_ID).canonical,),
        visible_tools=tuple(sorted(visible_tools)),
        preview=f"{HOST_PREVIEW_ID.canonical}|modality=legacy_host",
        verifier=(
            (
                f"{APPKIT_VERIFIER_ID.canonical}|check="
                "host.verify_appkit_strict,host.verify_deliverable"
            )
            if appkit
            else f"{HOST_VERIFIER_ID.canonical}|check=host.verify_deliverable"
        ),
        exporter=f"{LEGACY_EXPORTER_ID.canonical}|package=web.legacy_archive",
    )


def compare_observe_only(
    legacy: LegacyAuthoritySnapshot,
    composition: BuildComposition,
) -> ObserveOnlyShadowRecord:
    package_shape = (
        composition.target_plan.package.package_shape
        if composition.target_plan.package is not None
        else "none"
    )
    platform_values: dict[str, tuple[str, ...]] = {
        "profile": (composition.profile.id.canonical,),
        "engine": (composition.engine.canonical,),
        "target": (composition.target.canonical,),
        "policy": tuple(
            sorted(
                f"{rule.key}={rule.decision.value}" for rule in composition.effective_policy.rules
            )
        ),
        "prompt_modules": tuple(
            module.component.canonical for module in composition.prompt_context.modules
        ),
        "visible_tools": tuple(tool.name for tool in composition.prompt_context.visible_tools),
        "preview": (
            f"{composition.preview.canonical}|modality={composition.target_plan.preview.modality}",
        ),
        "verifier": (
            f"{composition.verifier.canonical}|check="
            + ",".join(check.intent.operation for check in composition.target_plan.verifier.checks),
        ),
        "exporter": (
            (f"{composition.exporter.canonical}|package={package_shape}",)
            if composition.exporter is not None
            else ()
        ),
        "errors": tuple(
            sorted(f"{block.operation}:{block.code}" for block in composition.blocked_operations)
        ),
    }
    legacy_values: dict[str, tuple[str, ...]] = {
        "profile": (legacy.profile.canonical,),
        "engine": (legacy.engine.canonical,),
        "target": (legacy.target.canonical,),
        "policy": legacy.policy,
        "prompt_modules": legacy.prompt_modules,
        "visible_tools": legacy.visible_tools,
        "preview": (legacy.preview,),
        "verifier": (legacy.verifier,),
        "exporter": (legacy.exporter,),
        "errors": legacy.errors,
    }
    comparisons = tuple(
        ShadowFieldComparison(
            field=field,
            legacy=legacy_values[field],
            platform=platform_values[field],
            matches=legacy_values[field] == platform_values[field],
        )
        for field in (
            "profile",
            "engine",
            "target",
            "policy",
            "prompt_modules",
            "visible_tools",
            "preview",
            "verifier",
            "exporter",
            "errors",
        )
    )
    return ObserveOnlyShadowRecord(
        source=legacy.source,
        composition_digest=composition.digest.digest,
        comparisons=comparisons,
        platform_blocks=platform_values["errors"],
    )


def failed_observe_only(
    *, source: Literal["freeform", "appkit"], error: Exception
) -> ObserveOnlyShadowRecord:
    return ObserveOnlyShadowRecord(
        source=source,
        resolution_error=f"{type(error).__name__}: {error}",
    )
