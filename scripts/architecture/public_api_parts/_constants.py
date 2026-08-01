"""Shared literal authority for the public-API gate.

These constants live below ``public_api`` so the interior parts can import
them without importing their parent.  The three values the adversarial suite
monkeypatches (``_ACCEPTED_AUTHORITY_COMMIT``, ``_ACCEPTED_AUTHORITY_SHA256``,
``_PKG02_BASE_COMMIT``) deliberately do **not** live here: they are read
through ``public_api``'s own globals, so they must be defined there.
"""

from __future__ import annotations

import re

SCHEMA = "disclaude-architecture-public-api-v1"
SHA256 = re.compile(r"[0-9a-f]{64}")
GIT_SHA = re.compile(r"[0-9a-f]{40}")
PACKAGE = re.compile(r"PKG-\d{2}-[A-Z0-9-]+")
SURFACES = {"frontend", "python"}
TRANSITION_FIELDS = {
    "surface", "path", "public_name", "target_sha256", "owner_package", "reason",
}
BRIDGE_FIELDS = {
    "path", "public_name", "old_origin", "new_origin", "owner_package",
    "removal_package", "reason",
}
MEMBER_FIELDS = {
    "surface",
    "path",
    "public_name",
    "origin",
    "removed_members",
    "added_members",
    "old_signature_sha256",
    "new_signature_sha256",
    "owner_package",
    "accepting_commit",
    "accepting_receipt",
}
ACCEPTED_DIAGRAM_SHA256 = (
    "759993f1a3104700efe8f48395923117546bd0fd1ca371681bc2a09fc95b9f26"
)
AUTHORITY_PATH = "architecture/public-api.json"
DERIVED_PATHS = {
    AUTHORITY_PATH,
    "architecture/test-inventory.json",
    "docs/governance/CAMPAIGN-STATUS.md",
    "docs/governance/PROTECTED.sha256",
}

TargetKey = tuple[str, str, str, str]
TargetIdentity = tuple[str, str, str]
