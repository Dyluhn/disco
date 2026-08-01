"""Resolved-disposition ID sets for the Epic 10 sub-epics.

Epic 10 was split into sub-epics 10-A / 10-B / 10-C (owner directive,
2026-07-31: an epic over ~60 executable rows splits at package/subset
boundaries and each sub-epic seals independently). Partial-package sets
therefore carry the subset name rather than the package name.

These live outside ``generate_debt.py`` because that module sat at 690 logical
lines against a 700 cap before Epic 10-A, so inlining three sub-epics' worth of
ID sets would breach the size budget the generator itself enforces. This module
is listed in ``seal.py``'s ``PROTECTED`` tuple: it decides which rows leave the
active ledger, so it must be hash-gated exactly like the generator that
consumes it.

Every set is generated from ``architecture/debt.json``'s ``owner_package``
intersected with the live ``python_scan`` result at the accepted candidate, and
set-equality checked before being written here — never hand-transcribed.
"""

from __future__ import annotations

# --- Epic 10-A (sealed 2026-07-31) -----------------------------------------

PKG10_EXECUTOR_RESOLVED_IDS = frozenset(
    {*(f"PY-{number:04d}" for number in range(816, 824)), "PY-0892"}
)

# PKG-10-SANDBOX, agent-server host subset ONLY: host_proxy.py,
# host_service_bus.py, host_token_store.py. The other 48 SANDBOX rows belong to
# 10-B (tools/sandbox) and 10-C (browser cluster).
PKG10_SANDBOX_HOST_RESOLVED_IDS = frozenset(
    f"PY-{number:04d}" for number in range(202, 220)
)

PKG10_MEDIA_RESOLVED_IDS = frozenset(
    f"PY-{number:04d}"
    for number in (
        *range(240, 244),
        427,
        428,
        *range(735, 749),
        *range(758, 761),
        *range(789, 792),
        *range(797, 802),
    )
)

# 17 of 18. PY-0836 (`ProjectStore`, 20 public methods against a cap of 12)
# stays ACTIVE: the class implements no Protocol, so the typed-Protocol
# adjudication in PROTOCOL-WIDTH-DECISION-2026-07-31.md does not apply to it,
# and the candidate registry/version seam is blocked by byte-frozen closeout
# evidence. Escalated to the owner — see PY-0836-SEAM-EVALUATION-2026-07-31.md.
# Its shifted AST anchor is carried in ``debt_location_overrides.py``.
PKG10_PROJECTS_RESOLVED_IDS = frozenset(
    f"PY-{number:04d}" for number in (*range(827, 836), *range(837, 844), 888)
)

# Epic 10-A registered 75 rows: EXECUTOR 9 + SANDBOX-host 18 + MEDIA 31 +
# PROJECTS 17. EXPECTED_ACTIVE_DEBT_ROWS 383 -> 308.
EPIC10A_RESOLVED_IDS = (
    PKG10_EXECUTOR_RESOLVED_IDS
    | PKG10_SANDBOX_HOST_RESOLVED_IDS
    | PKG10_MEDIA_RESOLVED_IDS
    | PKG10_PROJECTS_RESOLVED_IDS
)

# --- Epic 10 aggregate -----------------------------------------------------
# `generate_debt.py` imports ONLY this name, so sealing 10-B and 10-C means
# adding their sets above and unioning them in here — the generator itself
# never has to change again for Epic 10, and stays inside its 700-line budget.
EPIC10_RESOLVED_IDS = EPIC10A_RESOLVED_IDS
