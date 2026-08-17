#!/usr/bin/env python3
"""Compatibility entry point for the bounded gVisor stress owners."""

from __future__ import annotations

if __package__:
    from ._gvisor_stress import *  # noqa: F403 - frozen compatibility surface
    from ._gvisor_stress import main
else:
    from _gvisor_stress import *  # type: ignore[import-not-found]  # noqa: F403
    from _gvisor_stress import main  # type: ignore[import-not-found]


if __name__ == "__main__":
    raise SystemExit(main())
