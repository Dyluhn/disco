"""Frozen resolved-disposition ID sets for Epic 12's sub-epics.

Same contract as ``debt_resolved_epic10.py`` and ``debt_resolved_epic11.py``:
once a sub-epic is sealed its IDs never change, and
``generate_debt_parts/resolved_ids.py`` imports ONLY the ``EPIC12_RESOLVED_IDS``
aggregate, so sealing 12-C/D means adding a set here and unioning it in — no
edit to the generator or its parts.

Set-equality against ``architecture/debt.json`` ``owner_package`` was checked
mechanically before this file was written (standing §7 step 1: never transcribe
a disposition set by hand).
"""

from __future__ import annotations

# --- 12-B: PKG-12-FE-BUILD --------------------------------------------------
# All 25 rows the ledger assigns `owner_package: PKG-12-FE-BUILD`, and the first
# TypeScript DECOMPOSITION rows the campaign has cleared: 15
# `typescript_callable_ast_mccabe_gt_15`, 6 `typescript_module_logical_gt_500`,
# 3 `react_component_logical_gt_250`, 1 `react_hook_logical_gt_200`.
#
# Relocating a callable does not reduce its cyclomatic complexity, so every
# mccabe row was cleared by genuine branch reduction — per-action handler maps
# (`reducerInner` 72 -> 3), per-case dispatch over a discriminant
# (`deriveActivity` 79 -> 13), and extraction of self-contained conditional
# regions into child components that carry their own branches
# (`BuildSurface` 88 -> 9, `WorkflowAuthorForm` 49 -> 3).
#
# Proved as a SET IDENTITY on `(path, qualified_symbol, rule)` via
# `tools/ts-tree-scan.py`, not a count match: 25 removed, 0 added, while the
# scanned TypeScript module denominator rose 413 -> 478. The qualified key
# matters — `AgentCanvas.tsx` carries `BrowserPane>probe` as a nested callable
# with its own row, which a bare-`symbol` key would have merged (the Epic 10-A
# defect).
#
# EXPECTED_ACTIVE_DEBT_ROWS 62 -> 37 (0 python + 37 typescript).
PKG12_FE_BUILD_RESOLVED_IDS = frozenset(
    {
        "TS-0002", "TS-0004", "TS-0005", "TS-0006", "TS-0008",
        "TS-0009", "TS-0010", "TS-0011", "TS-0012", "TS-0013",
        "TS-0014", "TS-0015", "TS-0037", "TS-0038", "TS-0039",
        "TS-0040", "TS-0041", "TS-0042", "TS-0045", "TS-0046",
        "TS-0047", "TS-0048", "TS-0049", "TS-0050", "TS-0062"
    }
)

# --- 12-C: PKG-12-FE-RESEARCH + PKG-12-FE-PREVIEW ---------------------------
# All 13 rows the ledger assigns `owner_package: PKG-12-FE-RESEARCH` and all 3
# it assigns `PKG-12-FE-PREVIEW`, across 6 files: 10
# `typescript_callable_ast_mccabe_gt_15`, 2 `typescript_module_logical_gt_500`,
# 3 `react_component_logical_gt_250`, 1 `react_hook_logical_gt_200`.
#
# Relocating a callable does not reduce its cyclomatic complexity, so every
# mccabe row was cleared by genuine branch distribution: `deriveStats` 37 -> a
# per-tool-name handler set over one accumulator; `PreviewPane` 76 -> 9 and
# `DeepResearchSurface` 48 -> 8 by moving self-contained JSX conditional regions
# into child components that carry their own branches; `ExportModal` 22 -> under
# 15 by collapsing near-duplicate `handleMd`/`handleFmt` into one `runExport`.
#
# TS-0024 is the campaign's second NESTED-callable row: the ledger records it as
# the bare `viaStream`, but its real scanner key is `AudioSection>viaStream`. It
# was cleared by moving the SSE read loop OUT of the component tree entirely and
# into `api/deepResearch.ts` — which Amendment A3 required anyway, since raw
# transport is banned under `src/components/` and `src/hooks/`.
#
# Proved as a SET IDENTITY on `(path, qualified_symbol, rule)` via
# `tools/ts-tree-scan.py`, not a count match: 16 removed, 0 added, the removed
# set proved set-equal to these 16 ids, while the scanned TypeScript module
# denominator rose 477 -> 516.
#
# EXPECTED_ACTIVE_DEBT_ROWS 37 -> 21 (0 python + 21 typescript).
PKG12_FE_RESEARCH_RESOLVED_IDS = frozenset(
    {
        "TS-0007", "TS-0019", "TS-0020", "TS-0021", "TS-0022",
        "TS-0023", "TS-0024", "TS-0025", "TS-0043", "TS-0044",
        "TS-0051", "TS-0052", "TS-0053"
    }
)

PKG12_FE_PREVIEW_RESOLVED_IDS = frozenset(
    {
        "TS-0016", "TS-0017", "TS-0018"
    }
)

# --- 12-D: PKG-12-FE-SETTINGS + PKG-12-FE-SHELL -----------------------------
# All 12 rows the ledger assigns `owner_package: PKG-12-FE-SETTINGS` and all 9
# it assigns `PKG-12-FE-SHELL`, across 17 files: 15
# `typescript_callable_ast_mccabe_gt_15`, 3 `typescript_module_logical_gt_500`,
# 3 `react_component_logical_gt_250`.
#
# **This is the boundary that takes the active ledger to ZERO** — the first
# time in the campaign. EXPECTED_ACTIVE_DEBT_ROWS 21 -> 0.
#
# Relocating a callable does not reduce its cyclomatic complexity, so every
# mccabe row was cleared by genuine branch reduction, including the campaign's
# worst single row: `ImageGenSection` 59 -> 7, by lifting an inline
# workflow-JSON validator and a nested `fallbackWarning` ternary chain into
# pure helpers and splitting the render into child components that carry their
# own branches. `validateCronField` 23 -> 6 went by dispatch-per-syntactic-form
# and `E2EBridgeMounter` 22 -> 1 by a route lookup table.
#
# TS-0055 is the campaign's THIRD nested-callable row: the ledger records it as
# the bare `visit`, but its real scanner key is
# `redactFailureStringsInPlace>visit`. It was cleared by extracting the Error
# branch into a sibling `redactErrorInPlace`, which required proving that
# `immutable` is per-`visit`-invocation state whose only consumer is the very
# next branch — so passing it by value preserves behaviour exactly.
#
# `views/SpacesView.tsx` is the two-`submit` file the qualified key was built
# for. Both `CreateSpaceDialog>submit` and `RenameSpaceDialog>submit` relocated
# into `views/spacesViewParts/spaceDialogs.tsx` and were measured at mccabe 2
# apiece afterwards, so neither crossed the threshold and neither could merge
# with the other in the delta.
#
# Proved as a SET IDENTITY on `(path, qualified_symbol, rule)` via
# `tools/ts-tree-scan.py`, not a count match: 21 removed, 0 added, the removed
# set proved set-equal to these 21 ids, while the scanned TypeScript module
# denominator rose 516 -> 578 and `violation_count` reached 0.
PKG12_FE_SETTINGS_RESOLVED_IDS = frozenset(
    {
        "TS-0003", "TS-0026", "TS-0027", "TS-0028", "TS-0029",
        "TS-0030", "TS-0031", "TS-0032", "TS-0033", "TS-0034",
        "TS-0035", "TS-0036"
    }
)

PKG12_FE_SHELL_RESOLVED_IDS = frozenset(
    {
        "TS-0001", "TS-0054", "TS-0055", "TS-0056", "TS-0057",
        "TS-0058", "TS-0059", "TS-0060", "TS-0061"
    }
)

EPIC12_RESOLVED_IDS = (
    PKG12_FE_BUILD_RESOLVED_IDS
    | PKG12_FE_RESEARCH_RESOLVED_IDS
    | PKG12_FE_PREVIEW_RESOLVED_IDS
    | PKG12_FE_SETTINGS_RESOLVED_IDS
    | PKG12_FE_SHELL_RESOLVED_IDS
)
