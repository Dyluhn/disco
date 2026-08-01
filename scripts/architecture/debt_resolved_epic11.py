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


# --- Epic 11-B: PKG-11-RETRIEVAL 29 + PKG-11-SETTINGS 22 = 51 --------------
#
# Cleared in source across three serialized waves (the wave plan is
# PY-0328-0365-0459-0473-SEAM-EVALUATION-2026-08-01.md):
#   wave 1  PKG-11-RETRIEVAL, 4 disjoint lanes                       29
#   wave 2  PKG-11-SETTINGS ordinary rows, 3 disjoint lanes          13
#   wave 3  PKG-11-SETTINGS width rows, sequential 3a -> 3b -> 3c     9
#
# The four width rows (PY-0328/0365/0459/0473) all had a GENUINE SEAM and
# NONE was adjudicated, so `architecture/policy.json` is untouched by 11-B and
# the tree-count identity still holds exactly: tree == active python rows.
# They could not be cleared by the `_parts/` extraction that clears the other
# 47, because `class_public_methods` counts non-underscore defs in the class
# body — a thin delegating method still counts and a mixin would be evasion.
# Each was cleared by genuinely moving authority off the class onto
# collaborators exposed as plain `__init__` attributes (a property would still
# count), with real consumer migration:
#   ConfigStore   23 -> 7   ConfigState 51 -> 1
#   SecretStore   13 -> 9   RuntimeSettings 14 -> 12
#
# EXPECTED_ACTIVE_DEBT_ROWS 166 -> 115 (53 python + 62 typescript).
PKG11_RETRIEVAL_RESOLVED_IDS = frozenset(
    {
        "PY-0192", "PY-0194", "PY-0195", "PY-0196", "PY-0197",
        "PY-0198", "PY-0199", "PY-0200", "PY-0704", "PY-0705",
        "PY-0706", "PY-0707", "PY-0708", "PY-0709", "PY-0710",
        "PY-0711", "PY-0712", "PY-0713", "PY-0714", "PY-0715",
        "PY-0716", "PY-0717", "PY-0718", "PY-0719", "PY-0720",
        "PY-0721", "PY-0722", "PY-0723", "PY-0826"
    }
)

PKG11_SETTINGS_RESOLVED_IDS = frozenset(
    {
        "PY-0275", "PY-0276", "PY-0277", "PY-0278", "PY-0279",
        "PY-0280", "PY-0325", "PY-0326", "PY-0327", "PY-0328",
        "PY-0364", "PY-0365", "PY-0366", "PY-0368", "PY-0369",
        "PY-0370", "PY-0371", "PY-0457", "PY-0458", "PY-0459",
        "PY-0460", "PY-0473"
    }
)

EPIC11B_RESOLVED_IDS = (
    PKG11_RETRIEVAL_RESOLVED_IDS | PKG11_SETTINGS_RESOLVED_IDS
)


# --- Epic 11 aggregate -----------------------------------------------------
EPIC11_RESOLVED_IDS = EPIC11A_RESOLVED_IDS | EPIC11B_RESOLVED_IDS
