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

# 37 of 40 — the `tools/sandbox` subset of PKG-10-SANDBOX (the package's
# remaining 8 rows are the browser cluster, which is 10-C). Set equality was
# checked against `debt.json` owner_package intersected with the live tree
# delta, keyed on (path, qualified_symbol, rule); the 37 registered ids are
# exactly the 37 violations the scan shows cleared. Never transcribed.
#
# PY-0846 (`ContainerInstance`, 15), PY-0873 (`ProcessSandboxInstance`, 14) and
# PY-0877 (`SandboxSession`, 28) stay ACTIVE. All three DO faithfully implement
# the 11-member `SandboxInstance` Protocol, so the decision doc's premise holds
# for them (unlike PY-0836) — but the Protocol alone fills 11 of the cap's 12,
# so no seam can reach 12, and root's own consumer re-analysis found criterion 2
# failing: `host_service_relay_url` (both container classes) and
# `ensure_service` / `tracked_services` / `peek_recovered_memory_facts`
# (`SandboxSession`) have ZERO production consumers. Root did not self-authorize
# an adjudication the binding criteria reject. See
# PY-0846-0873-0877-SEAM-EVALUATION-2026-07-31.md.
PKG10_SANDBOX_TOOLS_RESOLVED_IDS = frozenset(
    f"PY-{number:04d}"
    for number in (
        *range(727, 729),
        *range(844, 846),
        *range(847, 873),
        *range(874, 877),
        *range(878, 882),
    )
)

# Epic 10-B registered 37 rows: the tools/sandbox subset of PKG-10-SANDBOX.
# EXPECTED_ACTIVE_DEBT_ROWS 308 -> 271.
EPIC10B_RESOLVED_IDS = PKG10_SANDBOX_TOOLS_RESOLVED_IDS

# --- Epic 10 aggregate -----------------------------------------------------
# `generate_debt.py` imports ONLY this name, so sealing 10-B and 10-C means
# adding their sets above and unioning them in here — the generator itself
# never has to change again for Epic 10, and stays inside its 700-line budget.
EPIC10_RESOLVED_IDS = EPIC10A_RESOLVED_IDS | EPIC10B_RESOLVED_IDS
