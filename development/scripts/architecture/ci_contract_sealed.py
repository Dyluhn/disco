"""Comparator between the sealed ordered gate list and the enforced one.

PKG-19-CERT-STRUCTURAL finding **F7**: mutation control M3 deleted the last entry
of ``ci.ordered_commands`` from the sealed ``development/governance/SEAL-INVOCATION.json``
and ``check_ci_contract.py`` stayed **green** — it carried its own private copy of
the order and never read the sealed file. Two authorities, no comparator; the
same disjoint-contract shape as F2. ``check_governance_seal.py`` pins the sealed
file's *bytes* (control M1), but nothing detected the two lists drifting apart
through an approved seal regeneration.

Kept in its own module rather than inside :mod:`architecture.ci_contract`
because adding it there pushed that module to 754 logical lines against its
700-line budget, and the campaign's rule is to decompose at a real seam rather
than register a debt row. The seam is real: this is a comparison *between*
authorities, where the rest of ``ci_contract`` checks one workflow's contents.

The constants arrive as parameters instead of by import, so that
``ci_contract`` can keep owning them without an import cycle.
"""

from __future__ import annotations

import json
from pathlib import Path

_NONWEB = "packages/core/tests/test_build_platform_nonweb_conformance.py"

# tool_schemas.py must run after check_ci_contract.py and before non-web
# conformance. It lives here with the ordered-list helpers rather than in
# ci_contract because it is a statement about POSITION IN THE ORDERED LIST,
# which is exactly this module's concern.
TOOL_SCHEMAS_SCRIPT = "development/scripts/check_tool_schemas.py"


def enforced_ordered_commands(
    ordered_gate_scripts: list[str],
    tool_schemas_script: str,
    gate_argv: dict[str, list[str]],
) -> list[str]:
    """The ordered gate sequence ``ci_contract`` actually enforces, as commands.

    ``ORDERED_GATE_SCRIPTS`` omits ``check_tool_schemas.py``, which
    ``_check_tool_schemas_position`` constrains separately to sit after
    ``check_ci_contract.py`` and before the persistent non-web conformance test.
    Splicing it back at that position reconstructs the full enforced order, so it
    can be compared against the sealed contract entry for entry.
    """
    keys = [gate for gate in ordered_gate_scripts if gate != _NONWEB]
    keys.append(tool_schemas_script)
    keys.append(_NONWEB)
    return [" ".join(gate_argv[key]) for key in keys]


def check_sealed_ordered_contract(root: Path, enforced: list[str]) -> list[str]:
    """Compare the enforced ordered gate list against the sealed one.

    Fails closed: an unreadable or structurally absent sealed list is a failure,
    never a silent pass, because a comparison against nothing always succeeds.
    This is what makes mutation control M3 load bearing.
    """
    seal_path = root / "development" / "governance" / "SEAL-INVOCATION.json"
    try:
        sealed_doc = json.loads(seal_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"sealed contract unreadable at {seal_path}: {exc}"]

    sealed = sealed_doc.get("ci", {}).get("ordered_commands")
    if not isinstance(sealed, list) or not sealed:
        return [
            "sealed contract has no non-empty ci.ordered_commands — refusing to "
            "report agreement against an absent list"
        ]

    if sealed == enforced:
        return []

    problems = [
        f"sealed/enforced ordered gate mismatch: sealed has {len(sealed)} entries, "
        f"this checker enforces {len(enforced)}"
    ]
    for index in range(max(len(sealed), len(enforced))):
        sealed_entry = sealed[index] if index < len(sealed) else "<absent>"
        enforced_entry = enforced[index] if index < len(enforced) else "<absent>"
        if sealed_entry != enforced_entry:
            problems.append(
                f"  position {index}: sealed {sealed_entry!r} != enforced {enforced_entry!r}"
            )
    return problems


def _find_gate_order(commands: list[str], gates: list[str]) -> dict[str, int]:
    """Find each exact gate argv position, or -1 when absent.

    ``GATE_ARGV`` and ``_tokenize`` stay owned by :mod:`architecture.ci_contract`
    — they describe how a workflow command is written, which is that module's
    concern, not this one's. The import is deferred to the call so the two
    modules can reference each other without a cycle at import time.
    """
    from .ci_contract import GATE_ARGV, _tokenize

    positions: dict[str, int] = {}
    for gate in gates:
        expected_argv = GATE_ARGV.get(gate)
        positions[gate] = -1
        if expected_argv is None:
            continue
        for i, cmd in enumerate(commands):
            actual_argv, err = _tokenize(cmd)
            if err:
                continue
            if actual_argv == expected_argv:
                positions[gate] = i
                break
    return positions


def _check_tool_schemas_position(
    commands: list[str], positions: dict[str, int], label: str
) -> list[str]:
    """Check that tool_schemas.py runs after CI contract and before non-web."""
    problems: list[str] = []
    ts_pos = _find_gate_order(commands, [TOOL_SCHEMAS_SCRIPT])[TOOL_SCHEMAS_SCRIPT]
    ci_contract_pos = positions.get("development/scripts/check_ci_contract.py", -1)
    nonweb_pos = positions.get(
        "packages/core/tests/test_build_platform_nonweb_conformance.py", -1
    )
    if ts_pos < 0:
        problems.append(f"{label}: missing gate: {TOOL_SCHEMAS_SCRIPT}")
        return problems
    if ts_pos >= 0 and ci_contract_pos >= 0 and ts_pos < ci_contract_pos:
        problems.append(f"{label}: tool_schemas.py must run after check_ci_contract.py")
    if ts_pos >= 0 and nonweb_pos >= 0 and ts_pos > nonweb_pos:
        problems.append(
            f"{label}: tool_schemas.py must run before persistent non-web conformance"
        )
    return problems
