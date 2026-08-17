"""Canonical resource naming for the container sandbox backends (disco-rename).

The WRITE side emits the current `disco-*` / `disco.*` names. The READ side
(teardown, list-live, destroy-by-conversation) must accept BOTH the current names
AND the legacy `pmx-*` / `pmx.*` ones, so renaming never orphans a container,
network, or volume that a prior build started under the old scheme. This is the
"dual-read" contract: write new, sweep both.

Centralized here (rather than string literals per backend) so the gVisor, local,
and Podman backends cannot drift — a teardown that reads only one prefix is the
exact bug that leaks containers across a rename.
"""

from __future__ import annotations

from collections.abc import Mapping

# --- WRITE side: the names a freshly created resource gets -------------------
SBX_NAME_PREFIX = "disco-sbx-"  # sandbox container name prefix
EGR_NET_PREFIX = "disco-egr-"  # filtered-egress internal network name prefix
LABEL_CONV = "disco.conversation_id"  # per-container conversation label key

# --- legacy (read-only): swept during teardown so a rename never orphans -----
_LEGACY_SBX_NAME_PREFIX = "pmx-sbx-"
_LEGACY_EGR_NET_PREFIX = "pmx-egr-"
_LEGACY_LABEL_CONV = "pmx.conversation_id"

# Tuples to iterate when READING (current first, legacy second).
SBX_NAME_PREFIXES = (SBX_NAME_PREFIX, _LEGACY_SBX_NAME_PREFIX)
EGR_NET_PREFIXES = (EGR_NET_PREFIX, _LEGACY_EGR_NET_PREFIX)
LABEL_CONV_KEYS = (LABEL_CONV, _LEGACY_LABEL_CONV)


def conv_id_from_labels(labels: Mapping[str, str] | None) -> str | None:
    """The conversation id from a container's labels, reading the current key
    first then the legacy one. None if neither is present (an unlabeled
    pre-scheme container)."""
    if not labels:
        return None
    for key in LABEL_CONV_KEYS:
        v = labels.get(key)
        if v:
            return v
    return None


def is_sandbox_name(name: str | None) -> bool:
    """True if `name` is a sandbox container under the current OR legacy prefix."""
    if not name:
        return False
    name = name.lstrip("/")  # docker-py prefixes a leading slash on container names
    return name.startswith(SBX_NAME_PREFIXES)
