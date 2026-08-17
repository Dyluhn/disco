"""Frozen resolved-disposition ID sets for Epic 11's sub-epics.

Same contract as ``debt_resolved_epic10.py``: once a sub-epic is sealed its IDs
never change, and ``generate_debt_parts/resolved_ids.py`` imports ONLY the
``EPIC11_RESOLVED_IDS`` aggregate, so sealing 11-B/C/D means adding a set here
and unioning it in — no edit to the generator or its parts.

Set-equality against ``development/architecture/debt.json`` ``owner_package`` was checked
mechanically before this file was written (standing §7 step 1: never transcribe
a disposition set by hand).
"""

from __future__ import annotations

# --- 11-A: PKG-11-BUILD-SPECS ----------------------------------------------
# All 55 rows the ledger assigns `owner_package: PKG-11-BUILD-SPECS`.
# 48 of them sit under current/packages/core/src/disco/core/appkit/ and are governed by
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
# NONE was adjudicated, so `development/architecture/policy.json` is untouched by 11-B and
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


# --- Epic 11-C -------------------------------------------------------------
# BUILD-ADAPTERS 19 + AUTH-QUOTA 10 + WORKFLOWS 10 = 39 rows, in seven waves:
#   wave 1  app_kit.py -> app_kit_parts/ (+ trusted_components)       11
#   wave 2  verify_appkit_app.py -> verify_appkit_parts/               7
#   wave 3  five WORKFLOWS route factories reduced in place            6
#   wave 4  core/auth.py -> auth_parts/                                6
#   wave 5  AgentAuthMiddleware.dispatch decomposed in place           1
#   wave 6  routes/workflows.py + core/workflow/models.py              4
#   wave 7  core/quota.py -> quota_parts/ (+ app-server quota router)  3
#   root    PY-0889, the appkit-verify test doubles                    1
#
# AUTH-QUOTA is 10 here, not 12: its two rider rows (PY-0670/PY-0671) sealed
# with 11-A.
#
# Thirty-four of the 39 are width rows cleared by moving authority into
# `*_parts/` collaborators; five are mccabe rows, and those cannot be cleared
# that way — relocating a function carries its branch graph with it. An early
# attempt on core/auth.py was rejected for exactly that: it moved
# validated_canonical_preview_url (28) and _is_generated_preview_label (18)
# verbatim into a parts module, where the scanner reported them unchanged.
# Each mccabe row was cleared by genuinely decomposing the decision structure
# first (AgentAuthMiddleware.dispatch 34 -> eight named guards; _walk_schema
# 17 -> a walker at 1 that only recurses and delegates).
#
# NO adjudications — every row had a genuine seam — so `development/architecture/policy.json`
# is untouched and the tree-count identity still holds exactly: tree == active
# python rows.
#
# EXPECTED_ACTIVE_DEBT_ROWS 115 -> 76 (14 python + 62 typescript).
PKG11_BUILD_ADAPTERS_RESOLVED_IDS = frozenset(
    {
        "PY-0749", "PY-0750", "PY-0751", "PY-0752", "PY-0753",
        "PY-0754", "PY-0755", "PY-0756", "PY-0757", "PY-0803",
        "PY-0804", "PY-0809", "PY-0810", "PY-0811", "PY-0812",
        "PY-0813", "PY-0814", "PY-0815", "PY-0889"
    }
)

PKG11_AUTH_QUOTA_RESOLVED_IDS = frozenset(
    {
        "PY-0186", "PY-0372", "PY-0421", "PY-0422", "PY-0423",
        "PY-0424", "PY-0425", "PY-0426", "PY-0632", "PY-0633"
    }
)

PKG11_WORKFLOWS_RESOLVED_IDS = frozenset(
    {
        "PY-0251", "PY-0289", "PY-0290", "PY-0291", "PY-0292",
        "PY-0293", "PY-0298", "PY-0299", "PY-0699", "PY-0700"
    }
)

EPIC11C_RESOLVED_IDS = (
    PKG11_BUILD_ADAPTERS_RESOLVED_IDS
    | PKG11_AUTH_QUOTA_RESOLVED_IDS
    | PKG11_WORKFLOWS_RESOLVED_IDS
)


# --- Epic 11-D (PLATFORM-STRUCTURE 7 + MCP 7) ------------------------------
# The sub-epic that CLOSES Epic 11 and drives active PYTHON debt to ZERO.
#
# Nine of these fourteen were mccabe rows, so each was cleared by decomposing
# the decision structure, never by relocating a callable: `compile_prompt_context`
# 23 -> two phase helpers, `resolve_build_composition` 28 -> four, and
# `_validate_transport_target` 19 -> a stdio/http validator pair (whose http half
# still measured 16 until its 10-operand `or` chain moved behind a predicate).
#
# PY-0431 (`BuildPlatformRegistry` 14/12) could not be cleared by extraction at
# all — `service_public_methods` counts non-underscore defs in the class body, so
# a thin delegator still counts and a mixin is evasion. Authority was moved onto
# two collaborators exposed as plain `__init__` attributes (`ProfileCatalog`,
# `ComponentCatalog`) with a shared `_ComponentKeyspace` retaining the one
# id-uniqueness invariant that spanned both, following the 11-B precedent.
# Catalog mutation stayed underscore-private so namespace protection remains
# enforceable only through the registry's public `register_*` methods.
#
# NO adjudications — every row had a genuine seam — so `development/architecture/policy.json`
# is untouched and the tree-count identity holds exactly: tree == active python
# rows, now 0 == 0.
#
# EXPECTED_ACTIVE_DEBT_ROWS 76 -> 62 (0 python + 62 typescript).
PKG11_PLATFORM_STRUCTURE_RESOLVED_IDS = frozenset(
    {
        "PY-0187", "PY-0429", "PY-0430", "PY-0431", "PY-0432",
        "PY-0433", "PY-0434"
    }
)

PKG11_MCP_RESOLVED_IDS = frozenset(
    {
        "PY-0222", "PY-0223", "PY-0224", "PY-0257", "PY-0367",
        "PY-0824", "PY-0825"
    }
)

EPIC11D_RESOLVED_IDS = (
    PKG11_PLATFORM_STRUCTURE_RESOLVED_IDS | PKG11_MCP_RESOLVED_IDS
)


# --- Epic 11 aggregate -----------------------------------------------------
EPIC11_RESOLVED_IDS = (
    EPIC11A_RESOLVED_IDS
    | EPIC11B_RESOLVED_IDS
    | EPIC11C_RESOLVED_IDS
    | EPIC11D_RESOLVED_IDS
)
