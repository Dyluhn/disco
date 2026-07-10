"""Trusted Components — FAIL-CLOSED SEAM (v0.2, design-only).

This package intentionally contains ONLY the manifest contract from
docs/trusted-components-design.md (Dylan's verified vendored-component tier,
2026-07-10): immutable hash-pinned security cores (auth/RBAC/payments) + free
periphery; verification = integrity hash + requires-graph + per-component seam
probe; eject = honest relabel.

NOTHING here is wired: no tool is registered, no scope advertises it, no
runtime imports it. Same parking discipline as the f41-stripe / f33-webhook
seams — the contract is pinned and TESTED (test_trusted_components_seam.py)
so it cannot rot, and shipping anything on top of it is a deliberate v0.2
decision, never an accident. Do NOT register a tool against this module
without implementing §4's three verification checks first.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ComponentMounts(BaseModel):
    """The seam-ownership declaration (design doc §3): what the component
    itself mounts, so safe use never depends on the model remembering N sites."""

    routes_prefix: str | None = None
    middleware: str | None = None


class TrustedComponentManifest(BaseModel):
    """manifest.json contract (design doc §2). Frozen shape for the v0.2 pilot;
    version this model WITH the registry when the pilot lands."""

    name: str
    version: str
    kind: str = Field(pattern="^trusted_component$")
    summary: str
    files: dict[str, str] = Field(
        description="Immutable core files → sha256:… pins. The ONLY integrity-checked set."
    )
    config_surface: list[str] = Field(
        default_factory=list,
        description="Files the model MAY edit — the sanctioned customization channel.",
    )
    requires: list[str] = Field(
        default_factory=list,
        description="Dependency edges (e.g. 'database-kit>=1.0') — the RBAC⇒database rule.",
    )
    provides: list[str] = Field(default_factory=list)
    mounts: ComponentMounts = Field(default_factory=ComponentMounts)
    probe: str | None = Field(
        default=None, description="Per-component seam probe entrypoint (§4.3)."
    )
    guide: str = Field(description="GUIDE.md path — returned to the model at scaffold time.")
