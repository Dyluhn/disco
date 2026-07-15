"""Trusted Components — the verified vendored-component tier (v0.2).

Implements docs/trusted-components-spec.md (mechanics) on top of
docs/trusted-components-design.md (intent): immutable hash-pinned security
cores (auth/RBAC/payments) + free periphery; verification = integrity hash +
requires-graph + per-component seam probe; eject = honest relabel.

Build order (the spec's WO-TC ladder): TC1 registry (this package's
`registry`), TC2 tools, TC3 verify, TC4/5 pilot components. Until TC2 lands,
NOTHING advertises the tier — `test_trusted_components_seam.py` enforces that
fail-closed state; it inverts into a scope-placement tripwire when the tools
register.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

# D10 (spec §0): exactly two operators, ONE grammar — this single pattern is the
# source of truth for both the manifest validator and parse_requirement(), so
# the two can never drift (adversarial-review finding: a drifted parse regex
# accepted `>=X.Y.Z` and silently discarded the patch digit).
REQUIREMENT_RE = re.compile(r"^(?P<name>[a-z0-9-]+)(?:>=(?P<ge>\d+\.\d+)|==(?P<eq>\d+\.\d+\.\d+))$")
_PIN_SHAPE = re.compile(r"^sha256:[0-9a-f]{64}$")
_NAME_SHAPE = re.compile(r"^[a-z0-9-]+$")
# No leading zeros: "01.2.3" and "1.2.3" would otherwise be two distinct,
# semantically identical registry entries.
_VERSION_SHAPE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SEGMENT_SHAPE = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_relpath(path: str) -> str:
    """Reject absolute paths (POSIX and Windows drive-letter forms), traversal,
    and exotic characters — manifest paths are workspace-relative under the
    component's install dir, plain ASCII-safe segments only (control or
    direction-override characters could spoof filenames in logs/UI)."""
    if path.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", path):
        raise ValueError(f"manifest path must be relative: {path!r}")
    parts = path.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"manifest path must be normalized and traversal-free: {path!r}")
    for p in parts:
        if not _SEGMENT_SHAPE.match(p):
            raise ValueError(f"manifest path segment has unsafe characters: {p!r}")
    return path


class ComponentMounts(BaseModel):
    """The seam-ownership declaration (design doc §3): what the component
    itself mounts, so safe use never depends on the model remembering N sites."""

    model_config = ConfigDict(frozen=True)

    routes_prefix: str | None = None
    middleware: str | None = None


class TrustedComponentManifest(BaseModel):
    """manifest.json contract (spec §1.2). Loaded ONLY from the host-side
    registry (D1) — workspace copies of this file are never trusted."""

    model_config = ConfigDict(frozen=True)

    name: str
    version: str
    kind: str = Field(pattern="^trusted_component$")
    summary: str
    when_to_use: str = Field(
        description="One-line catalog entry rendered into the tool schema "
        "(catalog-in-schema doctrine)."
    )
    files: dict[str, str] = Field(
        description="Immutable core files → sha256:… pins. The ONLY integrity-checked set."
    )
    config_surface: list[str] = Field(
        default_factory=list,
        description="Files the model MAY edit — the sanctioned customization channel.",
    )
    config_defaults_hashed: bool = Field(
        default=False,
        description="Reserved (spec §1.2): always False in the pilot.",
    )
    requires: list[str] = Field(
        default_factory=list,
        description="Dependency edges (e.g. 'database-kit>=1.0') — the RBAC⇒database rule.",
    )
    provides: list[str] = Field(default_factory=list)
    mounts: ComponentMounts = Field(default_factory=ComponentMounts)
    probe: str | None = Field(
        default=None,
        description="Per-component seam probe entrypoint (§4.3) — host-run, never installed.",
    )
    guide: str = Field(description="GUIDE.md path — returned to the model at scaffold time.")

    @field_validator("name")
    @classmethod
    def _name_shape(cls, v: str) -> str:
        if not _NAME_SHAPE.match(v):
            raise ValueError(f"component name must be kebab-case [a-z0-9-]: {v!r}")
        return v

    @field_validator("version")
    @classmethod
    def _version_shape(cls, v: str) -> str:
        if not _VERSION_SHAPE.match(v):
            raise ValueError(f"component version must be X.Y.Z: {v!r}")
        return v

    @field_validator("files")
    @classmethod
    def _files_shape(cls, v: dict[str, str]) -> dict[str, str]:
        if not v:
            raise ValueError("a trusted component must pin at least one core file")
        for path, pin in v.items():
            _safe_relpath(path)
            if not _PIN_SHAPE.match(pin):
                raise ValueError(f"pin for {path!r} must be sha256:<64 hex>: {pin!r}")
        return v

    @field_validator("config_surface")
    @classmethod
    def _config_paths(cls, v: list[str]) -> list[str]:
        for path in v:
            _safe_relpath(path)
        return v

    @field_validator("probe", "guide")
    @classmethod
    def _aux_paths(cls, v: str | None) -> str | None:
        if v is not None:
            _safe_relpath(v)
        return v

    @field_validator("requires")
    @classmethod
    def _requires_grammar(cls, v: list[str]) -> list[str]:
        for entry in v:
            if not REQUIREMENT_RE.match(entry):
                raise ValueError(
                    f"requires entry {entry!r} must match <name>>=X.Y or <name>==X.Y.Z (D10)"
                )
        return v


__all__ = [
    "REQUIREMENT_RE",
    "ComponentMounts",
    "TrustedComponentManifest",
]
