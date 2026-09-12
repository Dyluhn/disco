"""Which build is this? The one fact every bug report needs and nothing exposed.

The package versions are pinned contract values, so the useful identity is the image
tag and the commit the image was built from. Both arrive as build arguments baked
into the server image (`DISCO_BUILD_TAG`, `DISCO_BUILD_COMMIT`); a source checkout
falls back to `git rev-parse`. Unknown stays the literal word "unknown" — never a
guess.
"""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from .env import disco_env

UNKNOWN = "unknown"


@dataclass(frozen=True)
class BuildInfo:
    tag: str
    commit: str
    source: str  # "image" | "checkout" | "unknown"

    def as_dict(self) -> dict[str, str]:
        return asdict(self)

    def label(self) -> str:
        if self.tag != UNKNOWN and self.commit != UNKNOWN:
            return f"{self.tag} ({self.commit})"
        return self.commit if self.commit != UNKNOWN else self.tag


def _checkout_commit(start: Path) -> str | None:
    """Short commit of the checkout containing `start`, or None when it is not one."""
    for parent in (start, *start.parents):
        if (parent / ".git").exists():
            try:
                out = subprocess.run(
                    ["git", "-C", str(parent), "rev-parse", "--short", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            return out.stdout.strip() or None if out.returncode == 0 else None
    return None


def build_info() -> BuildInfo:
    tag = (disco_env("BUILD_TAG") or "").strip() or UNKNOWN
    commit = (disco_env("BUILD_COMMIT") or "").strip() or UNKNOWN
    if commit != UNKNOWN or tag != UNKNOWN:
        return BuildInfo(tag=tag, commit=commit, source="image")
    checkout = _checkout_commit(Path(__file__).resolve())
    if checkout:
        return BuildInfo(tag=UNKNOWN, commit=checkout, source="checkout")
    return BuildInfo(tag=UNKNOWN, commit=UNKNOWN, source="unknown")
