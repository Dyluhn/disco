"""Frozen resolved-disposition ID sets for Epic 11's sub-epics.

Same contract as ``debt_resolved_epic10.py``: once a sub-epic is sealed its IDs
never change, and ``generate_debt_parts/resolved_ids.py`` imports ONLY the
``EPIC11_RESOLVED_IDS`` aggregate, so sealing 11-B/C/D means adding a set here
and unioning it in — no edit to the generator or its parts.

Set-equality against ``architecture/debt.json`` ``owner_package`` was checked
mechanically before this file was written (standing §7 step 1: never transcribe
a disposition set by hand).
"""

from __future__ import annotations

# --- 11-A: PKG-11-BUILD-SPECS ----------------------------------------------
# All 55 rows the ledger assigns `owner_package: PKG-11-BUILD-SPECS`.
# 48 of them sit under packages/core/src/disco/core/appkit/ and are governed by
# APPKIT-DONOTTOUCH-ADJUDICATION-2026-08-01.md: the Track-1 prohibition does not
# bind this campaign, but its engineering bar does — verbatim template
# partition, sha256 byte-identity on every emitted artifact, and
# `test_wo6_diff_does_not_touch_appkit_packages` green at seal.
#
# 4 of the 55 (PY-0409..0412, the stripe primitive/worker rows) were cleared by
# the `pkg08-corestripe` cherry-pick landed at `8242a597`, whose patch-id is
# identical to the owner-blessed `e9126843`.
PKG11_BUILD_SPECS_RESOLVED_IDS = frozenset(
    {
        *(f"PY-{number:04d}" for number in range(373, 421)),
        "PY-0439",
        "PY-0456",
        *(f"PY-{number:04d}" for number in range(672, 675)),
        *(f"PY-{number:04d}" for number in range(677, 679)),
    }
)

# --- 11-A rider: two PKG-11-AUTH-QUOTA rows -------------------------------
# These two belong to PKG-11-AUTH-QUOTA, which is otherwise Epic 11-C work, but
# the same `pkg08-corestripe` cherry-pick cleared them in source at `8242a597`
# (`stripe_host_service.py::_runtime_inputs` and `::_parse_stripe_checkout_url`).
# A row cleared in source and left active in the ledger makes `generate_debt`
# raise "no AST node matches the frozen baseline line anchor", so they must be
# registered at the seal that lands the source — this one.
#
# CONSEQUENCE FOR 11-C: PKG-11-AUTH-QUOTA has **10** rows remaining, not 12.
PKG11_AUTH_QUOTA_CORESTRIPE_RESOLVED_IDS = frozenset({"PY-0670", "PY-0671"})

# Epic 11-A registered 57: BUILD-SPECS 55 + the 2 AUTH-QUOTA corestripe rows.
# EXPECTED_ACTIVE_DEBT_ROWS 223 -> 166 (104 python + 62 typescript).
EPIC11A_RESOLVED_IDS = (
    PKG11_BUILD_SPECS_RESOLVED_IDS | PKG11_AUTH_QUOTA_CORESTRIPE_RESOLVED_IDS
)

# --- Epic 11 aggregate -----------------------------------------------------
EPIC11_RESOLVED_IDS = EPIC11A_RESOLVED_IDS
