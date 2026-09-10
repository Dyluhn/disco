"""Build kernel seam.

The `BuildKernel` Protocol, the current `DiscoKernel` implementation, and the
selector that keeps legacy persisted choices resolving to Disco.
"""

from __future__ import annotations

from typing import Any

from .base import (
    BuildKernel,
    BuildKernelKind,
    KernelEvent,
)
from .disco_kernel import DiscoKernel

__all__ = [
    "BuildKernel",
    "BuildKernelKind",
    "DiscoKernel",
    "KernelEvent",
    "resolve_kernel_kind",
    "select_kernel",
]


def resolve_kernel_kind(selected: str | None) -> BuildKernelKind:
    """The kernel kind the runtime actually routes to.

    `selected` is accepted for compatibility with old persisted config values;
    every value resolves to Disco.
    """
    return "disco"


def select_kernel(runtime: Any, *, disco: DiscoKernel, selected: str | None) -> BuildKernel:
    """Pick the active kernel instance.

    Kept as the small selection seam so runtime pinning and tests keep exercising
    the `BuildKernel` protocol; legacy/unknown settings all resolve to Disco.
    """
    _ = runtime, selected
    return disco
