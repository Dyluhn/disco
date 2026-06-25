"""Build kernel seam (Disco Pi Build Kernel Campaign — PR A1/A2).

The `BuildKernel` Protocol + its two implementations (`DiscoKernel`, the current
loop; `PiKernel`, the experimental stub) + the selector that resolves the
configured kernel against the experimental gate.
"""

from __future__ import annotations

from typing import Any

from .base import (
    EXPERIMENTAL_ENV,
    BuildKernel,
    BuildKernelKind,
    BuildKernelPolicy,
    KernelEvent,
    experimental_kernels_enabled,
)
from .disco_kernel import DiscoKernel
from .pi_kernel import PiKernel

__all__ = [
    "EXPERIMENTAL_ENV",
    "BuildKernel",
    "BuildKernelKind",
    "BuildKernelPolicy",
    "DiscoKernel",
    "KernelEvent",
    "PiKernel",
    "experimental_kernels_enabled",
    "resolve_kernel_kind",
    "select_kernel",
]


def resolve_kernel_kind(selected: str | None) -> BuildKernelKind:
    """The kernel kind the runtime ACTUALLY routes to: the persisted setting,
    downgraded to `disco` unless `pi_experimental` is both selected AND unlocked
    by the experimental flag."""
    return BuildKernelPolicy.resolve(selected).effective_kind


def select_kernel(
    runtime: Any, *, disco: DiscoKernel, pi: PiKernel, selected: str | None
) -> BuildKernel:
    """Pick the active kernel instance from the (already-constructed) per-runtime
    kernels. `pi_experimental` resolves to the Pi stub only when the experimental
    flag is on; everything else (incl. an unknown/stale value) → the Disco kernel."""
    if resolve_kernel_kind(selected) == "pi_experimental":
        return pi
    return disco
