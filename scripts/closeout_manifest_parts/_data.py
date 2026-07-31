"""Frozen data tables — the single source of truth re-imported by
:mod:`gen_closeout_acceptance_manifest` (and, transitively, by
``verify_export_track1_closeout.py`` via ``manifest_mod``).

Everything in this module is FROZEN ACCEPTANCE IDENTITY (plan §1.1 / §4.2): the
directory globs and single files that make up the frozen acceptance harness,
the lane/command inventory, the anti-bypass operational reading, the red-test
inventory, and the acceptance remediation ledgers (R0/v5/v6). The VALUES here
must stay byte-identical to what a fresh checkout of the pre-split
``gen_closeout_acceptance_manifest.py`` produced — same strings, same
ordering, same tuple/frozenset/dict types. Do not alter them; only genuine
follow-on acceptance authoring (a new remediation ledger, a new red test) may
extend this file, and any such change perturbs this script's own frozen file
hash (regenerate + re-review).

Nothing here is part of the public API: ``gen_closeout_acceptance_manifest.py``
remains the sole state-free public compatibility/export facade and re-imports
these names. External callers must never import from
``closeout_manifest_parts`` directly.
"""

from __future__ import annotations

# ``RED_TESTS`` and the R0/v5/v6 remediation ledgers live in sibling modules so
# that no single frozen-data module exceeds the 700-logical-line budget. They are
# re-exported here (explicit ``as`` form) so ``closeout_manifest_parts._data``
# remains the one import surface ``gen_closeout_acceptance_manifest`` reads.
from ._red_tests import RED_TESTS as RED_TESTS
from ._remediation import REMEDIATION_R0 as REMEDIATION_R0
from ._remediation import REMEDIATION_V5 as REMEDIATION_V5
from ._remediation import REMEDIATION_V6 as REMEDIATION_V6

# ---- frozen harness definition (plan §1.1) ------------------------------------

# Directory globs (recursively hashed) and single files that make up the frozen
# acceptance harness. The manifest itself is EXCLUDED (it cannot hash itself). This
# set matches REALITY on the acceptance branch: three closeout test dirs, the fixture
# README, the vitest dir, and the Playwright e2e DIR (three ``.spec.ts`` proofs).
FROZEN_DIR_GLOBS: tuple[str, ...] = (
    "packages/core/tests/export_track1_closeout",
    "packages/tools/tests/export_track1_closeout",
    "packages/agent-server/tests/export_track1_closeout",
    "packages/agent-server/tests/fixtures/export_track1_closeout",
    "frontend/src/test/export-track1-closeout",
    "frontend/e2e/export-track1-closeout",
)
FROZEN_FILES: tuple[str, ...] = (
    "scripts/verify_export_track1_closeout.py",
    "scripts/gen_closeout_acceptance_manifest.py",
    # R6b: the purpose-built pytest reporting plugin the verifier loads into every governed
    # pytest lane (``-p closeout_pytest_report``) to prove every governed-selected test truly
    # PASSED (JUnit collapses xfail->skipped, hides a non-strict xpass, and omits a vanished
    # test). Frozen so a lane's truth-source cannot be silently weakened.
    "scripts/closeout_pytest_report.py",
    "packages/agent-server/tests/integration/test_export_track1_closeout_live.py",
    "packages/agent-server/tests/integration/_closeout_live_support.py",
    # acceptance-v5: the capture/structural-rendering regression suite the 2026-07-17
    # owner rulings (A / E) require — proves the exec_env exemption is narrow, ordinary
    # recorded output is still swept, and the structural rendering excludes ONLY
    # externally supplied env (an in-yaml secret still fails). Frozen so the ruled
    # guarantees cannot be silently weakened.
    "packages/agent-server/tests/integration/test_closeout_live_capture_regression.py",
    ".github/workflows/export-track1-closeout.yml",
    "docs/export-track1-closeout-work-orders.md",
    # The dedicated tsconfig for the G11 nullable-binding compile lane (acceptance-v4).
    # The contract file it checks lives under the frozen frontend/src/test dir glob.
    "frontend/tsconfig.closeout-g11.json",
    # R6 (G18): the owner-approved suppression baseline the anti-bypass diff scanner
    # reads. Freezing it means adding an approved suppression requires a manifest
    # regeneration + re-review (tamper-evident), not a silent edit.
    "docs/export-track1-closeout-suppression-baseline.json",
)

# ---- lane definitions (single source of truth; the verifier imports these) ----

# The focused closeout pytest lane (plan §3.2, second command).
CLOSEOUT_TEST_DIRS: tuple[str, ...] = (
    "packages/core/tests/export_track1_closeout",
    "packages/tools/tests/export_track1_closeout",
    "packages/agent-server/tests/export_track1_closeout",
)

# The non-live pytest suite (plan §3.2, first command) — the FULL non-integration
# tree (the closeout reds run inside it too, so a green run proves nothing regressed).
NONLIVE_TEST_PATHS: tuple[str, ...] = (
    "packages/core/tests",
    "packages/tools/tests",
    "packages/agent-server/tests",
)

# ---- R6 additions (plan §9): the frozen required command inventory (G19 / criterion 3).
# CONSTANTS ONLY — they are NOT emitted into the manifest JSON, so adding them perturbs
# ONLY this script's own frozen file hash (regenerate + re-review), never the closeout
# test-dir hashes or the python/frontend inventories. The verifier imports them so the
# manifest stays the single source of truth for the lane definitions.

# The exact command each verifier lane runs, keyed by a stable ID. The verifier records
# the IDs it actually dispatched and gates on EXACT set-equality with
# REQUIRED_COMMAND_IDS: an omitted command OR a substitute/additional command is rejected
# (criterion 3). The browser and G11 descriptors name the e2e DIRECTORY and the dedicated
# tsconfig — never a nonexistent single ``e2e/export-track1-closeout.spec.ts`` file (G19).
# Descriptors use repo/frontend-relative paths for byte-stability across hosts.
COMMAND_INVENTORY: dict[str, str] = {
    "python_nonlive": (
        "python -m pytest packages/core/tests packages/tools/tests "
        "packages/agent-server/tests -o addopts= -m 'not integration' "
        "-p closeout_pytest_report --closeout-report-json <report> --junitxml"
    ),
    "python_closeout": (
        "python -m pytest packages/core/tests/export_track1_closeout "
        "packages/tools/tests/export_track1_closeout "
        "packages/agent-server/tests/export_track1_closeout -o addopts= "
        "-m 'export_track1_closeout and not integration' "
        "-p closeout_pytest_report --closeout-report-json <report> --junitxml"
    ),
    "python_closeout_collect": (
        "python -m pytest packages/core/tests/export_track1_closeout "
        "packages/tools/tests/export_track1_closeout "
        "packages/agent-server/tests/export_track1_closeout -o addopts= "
        "-m 'export_track1_closeout and not integration' --collect-only -q"
    ),
    "frontend_vitest": "npx vitest run src/test/export-track1-closeout --reporter=json",
    "frontend_typecheck": "npm run typecheck:build",
    "frontend_build": "npx vite build",
    "g11_typecheck": "npx tsc -p tsconfig.closeout-g11.json --noEmit",
    "browser_e2e": (
        "npx playwright test e2e/export-track1-closeout --reporter=json --project=firefox"
    ),
    "live_docker": (
        "python -m pytest "
        "packages/agent-server/tests/integration/test_export_track1_closeout_live.py "
        "-o addopts= -m 'export_track1_closeout and integration' -ra "
        "-p closeout_pytest_report --closeout-report-json <report> --junitxml"
    ),
    "live_capture": (
        "python -m pytest "
        "packages/agent-server/tests/integration/test_closeout_live_capture_regression.py "
        "-o addopts= -m 'export_track1_closeout and integration' -ra "
        "-p closeout_pytest_report --closeout-report-json <report> --junitxml"
    ),
}
REQUIRED_COMMAND_IDS: frozenset[str] = frozenset(COMMAND_INVENTORY)

# ---- the anti-bypass operational reading (§4.4), quoted verbatim by the scanner
# docstring, the manifest ``note``, and ``anti-bypass-scan.json`` -----------------

OPERATIONAL_READING = (
    "A closeout acceptance test may seam the system ONLY at (1) the config loader — "
    "monkeypatch.setattr(cfg_store, 'load', ...), the same injection the settings PUT "
    "performs — and (2) the environment — monkeypatch.setenv/delenv (e.g. "
    "DISCO_DATA_DIR). No other monkeypatch.setattr target is permitted; the code under "
    "test (detect / spec / emit / release-route / tool / store) is never patched. "
    "unittest.mock / MagicMock / Mock() / create_autospec / patch() are forbidden "
    "entirely. In the frontend, vi.fn() is allowed ONLY as a callback/prop value (e.g. "
    "onDownload={vi.fn()}) or a locally-declared const passed as one; "
    "vi.mock / vi.spyOn / vi.stubGlobal / vi.stubEnv / jest.mock (module replacement) "
    "are forbidden. Production source may not reference closeout fixture names, the C8 "
    "secret/build sentinels, CLOSEOUT_SEED, a force-verdict switch, or "
    "PYTEST_CURRENT_TEST outside the ratified pre-existing auth/workflow baseline. A "
    "lexical/AST scan cannot prove the absence of a semantically-equivalent indirect "
    "branch; the independent diff review (plan §4.9) is the required backstop."
)
