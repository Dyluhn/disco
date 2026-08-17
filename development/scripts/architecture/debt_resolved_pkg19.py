"""Resolved-disposition IDs proven by final stable certification work.

The earlier package/epic sets are immutable accepted history.  PKG-19 closes
only the distributed concerns whose fixed release policies are implemented and
exercised on the final source identity.
"""

from __future__ import annotations

# PKG-19-CERT-SIGNOFF implements the previously absent SecretStore migration:
# versioned key IDs, one active writer, a bounded old-key read set, atomic and
# resumable re-encryption, a verified rollback window, and loud unknown-key
# failure.  The focused rotation corpus proves interruption, resume, rollback,
# locked deletion/re-entry and zero plaintext leakage.
PKG19_CERT_SIGNOFF_RESOLVED_IDS = frozenset({"DM-019"})
