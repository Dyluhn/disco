"""Target-owned registration bundle for the Next.js static/server profiles.

This module is the single target-owned surface that carries Next.js/Vercel
identity and target-specific vocabulary for the built-in profiles.  It imports
the accepted static/server adapters and their profile factories and returns a
generic registration bundle that the target-neutral built-in catalog consumes.

The bundle deliberately names no target-neutral registration policy: it only
describes which components and profiles the trusted built-in catalog should
register.  `build_platform.builtin_profiles.build_builtin_registry` iterates the
bundle generically and never spells a Next.js/Vercel identity, command, output,
port, or provider vocabulary.
"""

from __future__ import annotations

from typing import Any

from ..build_platform.contracts import (
    BuildProfile,
    CapabilityLayer,
    ComponentId,
    ComponentKind,
    PolicyLayer,
)
from ..build_platform.registry import ComponentSpec
from .nextjs_server import (
    NEXTJS_SERVER_EXPORTER_ID,
    NEXTJS_SERVER_PREVIEW_ID,
    NEXTJS_SERVER_TARGET_ID,
    NEXTJS_SERVER_VERIFIER_ID,
    NextjsServerExporter,
    NextjsServerTarget,
    nextjs_server_profile,
)
from .nextjs_static import (
    NEXTJS_STATIC_EXPORTER_ID,
    NEXTJS_STATIC_PREVIEW_ID,
    NEXTJS_STATIC_TARGET_ID,
    NEXTJS_STATIC_VERIFIER_ID,
    NextjsStaticExporter,
    NextjsStaticTarget,
    nextjs_static_profile,
)

_TARGET_CAPABILITIES = frozenset({"workspace.read", "workspace.write", "process.execute"})


class RegisteredImplementation:
    """A live adapter/exporter implementation plus its typed spec and kind."""

    __slots__ = ("spec", "implementation", "kind")

    def __init__(
        self, spec: ComponentSpec, implementation: Any, kind: ComponentKind
    ) -> None:
        self.spec = spec
        self.implementation = implementation
        self.kind = kind


class BuiltinRegistrationBundle:
    """Generic registration material consumed by the trusted built-in catalog."""

    __slots__ = ("profiles", "implementations", "plain_specs")

    def __init__(
        self,
        *,
        profiles: tuple[BuildProfile, ...],
        implementations: tuple[RegisteredImplementation, ...],
        plain_specs: tuple[ComponentSpec, ...],
    ) -> None:
        self.profiles = profiles
        self.implementations = implementations
        self.plain_specs = plain_specs


def _capabilities(source: str) -> CapabilityLayer:
    return CapabilityLayer(source=source, allowed=_TARGET_CAPABILITIES)


def _spec(component_id: ComponentId, kind: ComponentKind) -> ComponentSpec:
    return ComponentSpec(
        id=component_id,
        kind=kind,
        capabilities=_capabilities(component_id.canonical),
        policy=PolicyLayer(source=component_id.canonical),
    )


def target_builtin_catalog() -> BuiltinRegistrationBundle:
    """Return the generic target registration bundle.

    This is the only target-owned surface the built-in catalog consumes.  It
    registers two distinct profiles and their distinct target/exporter
    implementations plus their verifier/preview specs.  It carries no
    connector and no digest/provider behavior in this slice.
    """
    return BuiltinRegistrationBundle(
        profiles=(
            nextjs_static_profile(),
            nextjs_server_profile(),
        ),
        implementations=(
            RegisteredImplementation(
                _spec(NEXTJS_STATIC_TARGET_ID, ComponentKind.TARGET),
                NextjsStaticTarget(),
                ComponentKind.TARGET,
            ),
            RegisteredImplementation(
                _spec(NEXTJS_STATIC_EXPORTER_ID, ComponentKind.EXPORTER),
                NextjsStaticExporter(),
                ComponentKind.EXPORTER,
            ),
            RegisteredImplementation(
                _spec(NEXTJS_SERVER_TARGET_ID, ComponentKind.TARGET),
                NextjsServerTarget(),
                ComponentKind.TARGET,
            ),
            RegisteredImplementation(
                _spec(NEXTJS_SERVER_EXPORTER_ID, ComponentKind.EXPORTER),
                NextjsServerExporter(),
                ComponentKind.EXPORTER,
            ),
        ),
        plain_specs=(
            _spec(NEXTJS_STATIC_VERIFIER_ID, ComponentKind.VERIFIER),
            _spec(NEXTJS_STATIC_PREVIEW_ID, ComponentKind.PREVIEW),
            _spec(NEXTJS_SERVER_VERIFIER_ID, ComponentKind.VERIFIER),
            _spec(NEXTJS_SERVER_PREVIEW_ID, ComponentKind.PREVIEW),
        ),
    )
