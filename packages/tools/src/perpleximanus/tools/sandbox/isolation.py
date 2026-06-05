"""Isolation profiles — the sandbox picker's risk legibility, and the
isolation→confirmation coupling (the cost-legible picker, applied to isolation).

Each backend position is honest about HOW strong its isolation is and whether it is
appropriate for adversarial/untrusted code. The local container tier is explicitly
the WEAKEST (shared host kernel); this module makes that legible at the point of
choice AND couples it to a TIGHTER confirmation default — so a weaker sandbox is
never silently as permissive as the strong tier (BoD §17 security/sandbox coupling).

The coupling is concrete, not advisory: `recommended_confirmation()` returns an
actual `ConfirmRisky` policy whose threshold is LOWER for weaker tiers. The Build/
agent surface consumes this as its default-when-unset, so selecting the weak backend
demonstrably gates more actions than selecting the strong one.
"""

from __future__ import annotations

from perpleximanus.core import SecurityRisk
from perpleximanus.core.loop.policies import ConfirmRisky
from pydantic import BaseModel, ConfigDict


class IsolationProfile(BaseModel):
    """[security legibility] One backend position's isolation story + its coupled
    confirmation default. Frozen; built once per backend in the registry below."""

    model_config = ConfigDict(frozen=True)

    backend: str
    tier: str  # short id for the picker UI ("gvisor" | "container-remote" | "container")
    label: str  # one-line, shown at the point of choice — honest about strength
    adversarial_safe: bool  # safe for untrusted/adversarial code, eyes-open?

    # isolation→confirmation coupling: a WEAKER tier carries a LOWER threshold (more
    # actions gated) and gates UNKNOWN risk. Strong isolation earns a more permissive
    # default; weak isolation does not get it silently.
    confirm_threshold: SecurityRisk
    confirm_unknown: bool

    def recommended_confirmation(self) -> ConfirmRisky:
        """The confirmation policy this isolation tier recommends as its default.
        Wiring the coupling for real: a weaker sandbox returns a STRICTLY tighter
        policy than a stronger one (lower threshold / gates UNKNOWN)."""
        return ConfirmRisky(threshold=self.confirm_threshold, confirm_unknown=self.confirm_unknown)


# The three built backends + a fail-safe fallback. Ordered strong → weak.
_PROFILES: dict[str, IsolationProfile] = {
    "gvisor": IsolationProfile(
        backend="gvisor",
        tier="gvisor",
        label="gVisor user-space kernel — strong isolation; safe for untrusted/adversarial code.",
        adversarial_safe=True,
        confirm_threshold=SecurityRisk.HIGH,  # strong sandbox → only gate HIGH risk…
        confirm_unknown=False,  # …and trust the agent on UNKNOWN.
    ),
    "podman": IsolationProfile(
        backend="podman",
        tier="container-remote",
        label=(
            "Rootless Podman/crun on a remote host — container isolation (shared kernel) "
            "behind a host boundary; not for adversarial workloads."
        ),
        adversarial_safe=False,
        confirm_threshold=SecurityRisk.HIGH,
        confirm_unknown=True,  # container-grade → gate UNKNOWN.
    ),
    "local": IsolationProfile(
        backend="local",
        tier="container",
        label=(
            "Local container (runc) — container-grade isolation (SHARED host kernel), weaker "
            "than gVisor. Appropriate for trusted local use, NOT adversarial workloads."
        ),
        adversarial_safe=False,
        confirm_threshold=SecurityRisk.MEDIUM,  # weakest tier → gate at MEDIUM, strictly
        confirm_unknown=True,  # tighter than the gVisor default by construction.
    ),
}

# Fail-safe: an unrecognized backend is treated as the WEAKEST isolation (never assume
# strength we can't name) — gate at LOW and on UNKNOWN.
_FALLBACK = IsolationProfile(
    backend="unknown",
    tier="unknown",
    label="Unknown backend — treated as weakest isolation (no assumptions).",
    adversarial_safe=False,
    confirm_threshold=SecurityRisk.LOW,
    confirm_unknown=True,
)


def isolation_for(backend: str) -> IsolationProfile:
    """The isolation profile for a backend position. Unknown backends fall back to the
    WEAKEST profile — fail-safe, never assume isolation we can't account for."""
    profile = _PROFILES.get(backend)
    return profile if profile is not None else _FALLBACK.model_copy(update={"backend": backend})
