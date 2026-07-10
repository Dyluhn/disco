"""Trusted-components verification (spec §4).

TC1 ships only the canonical pin helper — the SINGLE hashing function shared by
the authoring script (scripts/component_pins.py) and the verify path, so the
two can never drift (spec §1.5). TC3 adds the three checks
(integrity / requires-graph / probe) on top.
"""

from __future__ import annotations

import hashlib


def pin(data: bytes) -> str:
    """The canonical content pin: byte-exact sha256, no normalization."""
    return "sha256:" + hashlib.sha256(data).hexdigest()
