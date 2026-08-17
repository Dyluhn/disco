"""disco.core.evidence — evidence/correlation schema (W4).

Public re-exports from the evidence package. Import from here, not from the
submodules, so internal layout can change without breaking callers.
"""

from __future__ import annotations

from .schema import (
    REDACTION_KEY_PATTERNS,
    SCHEMA_VERSION,
    ErrorTaxonomy,
    EvidenceRecord,
    make_traceparent,
    parse_traceparent,
    redact,
)

__all__ = [
    "REDACTION_KEY_PATTERNS",
    "SCHEMA_VERSION",
    "ErrorTaxonomy",
    "EvidenceRecord",
    "make_traceparent",
    "parse_traceparent",
    "redact",
]
