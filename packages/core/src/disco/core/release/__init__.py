"""Release contract — the neutral, immutable, secret-free release spec (v1).

`disco.core.release` owns the KEYSTONE `ReleaseSpec` and its assessment / service
/ env / resource models: a single provider-neutral description of how a built app
becomes runnable, pinned to an exact committed workspace version. Track 1 emits
the LOCAL profile; the shapes are forward-compat so future cloud targets consume
them without a rework. See `spec` for the models and `spec_digest` /
`serialize_release_spec` / `load_release_spec` helpers.

Layering: this package imports ONLY pydantic + the stdlib (no `disco.tools.*` /
higher layers) — `disco.core` is the leaf.
"""

from __future__ import annotations

from .spec import (
    RELEASE_SPEC_SCHEMA_VERSION,
    CloudResourceProfile,
    DetectorProvenance,
    EnvScope,
    EnvVarDecl,
    GeneratedFileRef,
    LocalResourceProfile,
    ReleaseAssessment,
    ReleaseIntent,
    ReleaseService,
    ReleaseSpec,
    ResourceDecl,
    ResourceKind,
    ResourceProfiles,
    RuntimeStrategy,
    SecretClass,
    ServiceRole,
    load_release_spec,
    local_mount_target,
    serialize_release_spec,
    spec_digest,
)

__all__ = [
    "RELEASE_SPEC_SCHEMA_VERSION",
    "CloudResourceProfile",
    "DetectorProvenance",
    "EnvScope",
    "EnvVarDecl",
    "GeneratedFileRef",
    "LocalResourceProfile",
    "ReleaseAssessment",
    "ReleaseIntent",
    "ReleaseService",
    "ReleaseSpec",
    "ResourceDecl",
    "ResourceKind",
    "ResourceProfiles",
    "RuntimeStrategy",
    "SecretClass",
    "ServiceRole",
    "load_release_spec",
    "local_mount_target",
    "serialize_release_spec",
    "spec_digest",
]
