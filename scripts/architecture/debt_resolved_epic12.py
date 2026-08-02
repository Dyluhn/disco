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

EPIC12_RESOLVED_IDS = PKG12_FE_BUILD_RESOLVED_IDS
