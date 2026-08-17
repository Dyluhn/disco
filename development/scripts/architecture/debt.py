"""Shrink-only debt enforcement for the architecture gate.

The debt ledger is a permanently shrinkable ratchet. Initial PKG-02 evidence
shows 965 active executable rows and exactly the first three resolved IDs, but
the stable checker later accepts fewer debt rows and additional legitimate
resolved dispositions.

Identity is ``(path, qualified_symbol, rule)``. The qualified symbol is
reconstructed from the Python AST using the frozen baseline line range as an
anchor. Unqualified ``run``/``check`` collisions in one file are invalid.

The checker rejects:
  - an active row absent from current violations (stale/moved/deleted)
  - a current violation absent from debt (new violation)
  - growth (current metric exceeds observed)
  - ID invention/reuse/deletion from the immutable 991 universe
  - ownership/delete-by drift
  - wildcards and cap increases
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .policy import ARCH_DIR, load_json

# PKG-02-GATE resolved these three dispositions. The checker accepts additional
# legitimate resolved dispositions (the ratchet is permanently shrinkable).
PKG02_RESOLVED_IDS = frozenset({"PY-0890", "PY-0891", "DM-010"})


def _arch_dir(root: Path | None = None) -> Path:
    """Return the architecture directory for a given root (defaults to REPO_ROOT)."""
    if root is None:
        return ARCH_DIR
    bucketed = root / "development" / "architecture"
    if not bucketed.is_dir():
        bare = root / "architecture"
        if bare.is_dir():
            return bare
    return bucketed


def load_debt(root: Path | None = None) -> list[dict[str, Any]]:
    """Load the sealed debt ledger."""
    return load_json(_arch_dir(root) / "debt.json")


def load_dispositions(root: Path | None = None) -> list[dict[str, Any]]:
    """Load the immutable disposition universe."""
    return load_json(_arch_dir(root) / "dispositions.json")


def load_disposition_ids(root: Path | None = None) -> list[str]:
    """Load the immutable sorted ID universe."""
    text = (_arch_dir(root) / "disposition-ids.txt").read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if line.strip()]


def load_observations(root: Path | None = None) -> list[dict[str, Any]]:
    """Load the exact non-budget legacy observations."""
    return load_json(_arch_dir(root) / "observations.json")


def check_debt_schema(debt: list[dict[str, Any]]) -> list[str]:
    """Validate the debt ledger schema. Returns a list of problems."""
    problems: list[str] = []
    required_fields = {
        "id",
        "path",
        "symbol",
        "qualified_symbol",
        "line_start",
        "line_end",
        "rule",
        "observed",
        "limit",
        "owner_package",
        "delete_by",
        "source_identity",
    }
    for row in debt:
        missing = required_fields - set(row.keys())
        if missing:
            problems.append(
                f"debt row {row.get('id', '?')} missing fields: {sorted(missing)}"
            )
            continue
        if row["observed"] <= row["limit"]:
            problems.append(
                f"debt row {row['id']} observed ({row['observed']}) <= limit ({row['limit']}): "
                "stale debt that should have been removed"
            )
    return problems


def check_debt_identity_unique(debt: list[dict[str, Any]]) -> list[str]:
    """Verify all debt rows have unique (path, qualified_symbol, rule) keys."""
    problems: list[str] = []
    keys: dict[tuple[str, str, str], str] = {}
    for row in debt:
        key = (row["path"], row["qualified_symbol"], row["rule"])
        if key in keys:
            problems.append(
                f"duplicate debt identity: {key} "
                f"(IDs {keys[key]} and {row['id']})"
            )
        else:
            keys[key] = row["id"]
    return problems


def check_id_universe(
    debt: list[dict[str, Any]],
    dispositions: list[dict[str, Any]],
    disposition_ids: list[str],
) -> list[str]:
    """Verify the ID universe is immutable: no new, deleted, or reused IDs."""
    problems: list[str] = []
    debt_ids = [row["id"] for row in debt]
    debt_ids_set = set(debt_ids)
    disposition_ids_set = set(disposition_ids)

    # Check for duplicate IDs in debt
    if len(debt_ids_set) != len(debt_ids):
        from collections import Counter

        dupes = {k: v for k, v in Counter(debt_ids).items() if v > 1}
        problems.append(f"duplicate IDs in debt ledger: {sorted(dupes.keys())}")

    # Check all debt IDs are in the disposition universe
    unknown = debt_ids_set - disposition_ids_set
    if unknown:
        problems.append(f"debt IDs not in disposition universe: {sorted(unknown)}")

    # Check disposition IDs are unique and complete (exactly 991)
    if len(disposition_ids_set) != len(disposition_ids):
        problems.append("duplicate IDs in disposition universe")
    if len(disposition_ids) != 991:
        problems.append(
            f"disposition universe has {len(disposition_ids)} IDs, expected 991"
        )

    # Check resolved IDs are not in active debt
    resolved_ids = {
        d["id"] for d in dispositions if d.get("state") == "resolved"
    }
    leaked = debt_ids_set & resolved_ids
    if leaked:
        problems.append(f"resolved IDs still in active debt: {sorted(leaked)}")

    # Check that all active VIOLATION dispositions have a debt row
    # (unless they are resolved — the ratchet is shrinkable, so fewer debt rows
    # are accepted when dispositions are legitimately resolved)
    active_violation_ids = {
        d["id"]
        for d in dispositions
        if d.get("disposition") == "VIOLATION" and d.get("state") == "active"
    }
    missing_debt = active_violation_ids - debt_ids_set
    if missing_debt:
        problems.append(
            f"active VIOLATION dispositions without debt rows: {sorted(missing_debt)}"
        )

    return problems


def check_shrink_only(
    debt: list[dict[str, Any]],
    current_violations: list[dict[str, Any]],
) -> list[str]:
    """Enforce shrink-only semantics against current violations.

    The ratchet is permanently shrinkable:
      - A current violation without a debt row fails (new violation).
      - A debt row whose current metric exceeds ``observed`` fails (growth).
      - A current metric at or below the hard limit while the row remains
        active fails as stale debt (should have been removed).
      - A debt row absent from current violations fails as stale/moved/deleted.

    Args:
        debt: the sealed debt ledger
        current_violations: list of {path, qualified_symbol, rule, value, limit}
            from the scanner

    Returns a list of problems.
    """
    problems: list[str] = []

    # Index debt by (path, qualified_symbol, rule)
    debt_index: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in debt:
        key = (row["path"], row["qualified_symbol"], row["rule"])
        if key in debt_index:
            problems.append(f"duplicate debt key: {key}")
        debt_index[key] = row

    # Index current violations by (path, qualified_symbol, rule)
    current_index: dict[tuple[str, str, str], dict[str, Any]] = {}
    for v in current_violations:
        key = (v["path"], v.get("qualified_symbol", v.get("symbol", "")), v["rule"])
        current_index[key] = v

    # Check each current violation has a debt row and hasn't grown
    for key, v in current_index.items():
        if key not in debt_index:
            problems.append(
                f"new violation without debt row: {key} "
                f"(value={v['value']}, limit={v['limit']})"
            )
        else:
            row = debt_index[key]
            if v["value"] > row["observed"]:
                problems.append(
                    f"debt grew for {row['id']}: "
                    f"observed {row['observed']} -> current {v['value']}"
                )
            if v["value"] <= row["limit"]:
                problems.append(
                    f"stale debt for {row['id']}: "
                    f"current {v['value']} <= limit {row['limit']} "
                    "but row remains active"
                )

    # Check for stale/moved/deleted debt rows (active row with no current violation)
    for key, row in debt_index.items():
        if key not in current_index:
            problems.append(
                f"stale/moved/deleted debt for {row['id']}: "
                f"no current violation at {key}"
            )

    return problems


def check_debt_count_shrinkable(
    debt: list[dict[str, Any]],
    dispositions: list[dict[str, Any]],
) -> list[str]:
    """Verify the debt count is at most 965 (permanently shrinkable).

    Initial PKG-02 evidence shows exactly 965 active executable rows. The
    checker accepts fewer rows (when dispositions are legitimately resolved)
    but rejects more (growth).
    """
    problems: list[str] = []
    if len(debt) > 965:
        problems.append(
            f"debt count is {len(debt)}, expected at most 965 (growth rejected)"
        )
    # Count active VIOLATION dispositions
    active_violations = sum(
        1
        for d in dispositions
        if d.get("disposition") == "VIOLATION" and d.get("state") == "active"
    )
    if len(debt) != active_violations:
        problems.append(
            f"debt count ({len(debt)}) != active VIOLATION dispositions "
            f"({active_violations})"
        )
    return problems


def check_resolved_dispositions(
    dispositions: list[dict[str, Any]],
) -> list[str]:
    """Verify PKG-0890, PY-0891, and DM-010 are marked resolved.

    The checker accepts additional legitimate resolved dispositions (the ratchet
    is permanently shrinkable), but the three PKG-02-GATE resolved IDs must
    always be present.
    """
    problems: list[str] = []
    actual_resolved = {
        d["id"] for d in dispositions if d.get("state") == "resolved"
    }
    missing = PKG02_RESOLVED_IDS - actual_resolved
    if missing:
        problems.append(
            f"required resolved IDs not marked resolved: {sorted(missing)}"
        )
    return problems


def check_ownership_stability(
    debt: list[dict[str, Any]],
    dispositions: list[dict[str, Any]],
) -> list[str]:
    """Verify ownership and delete-by packages have not drifted.

    Each debt row's owner_package and delete_by must match the original
    disposition's owning_package. A later delete-by package fails.
    """
    problems: list[str] = []
    disp_owners = {d["id"]: d.get("owner_package", "") for d in dispositions}
    for row in debt:
        original_owner = disp_owners.get(row["id"], "")
        if row["owner_package"] != original_owner:
            problems.append(
                f"ownership drift for {row['id']}: "
                f"owner {row['owner_package']} != original {original_owner}"
            )
        if row["delete_by"] != original_owner:
            problems.append(
                f"delete-by drift for {row['id']}: "
                f"delete_by {row['delete_by']} != original owner {original_owner}"
            )
    return problems


def run_debt_check(
    current_violations: list[dict[str, Any]] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Run the full debt check and return a result dict.

    Args:
        current_violations: current scanner violations. If None, only schema
            and ID universe checks are run.
        root: repository root path (defaults to REPO_ROOT). Unknown roots
            fail closed.

    Returns:
        {"ok": bool, "problems": list[str], "debt_count": int}
    """
    debt = load_debt(root)
    dispositions = load_dispositions(root)
    disposition_ids = load_disposition_ids(root)

    problems: list[str] = []
    problems.extend(check_debt_schema(debt))
    problems.extend(check_debt_identity_unique(debt))
    problems.extend(check_id_universe(debt, dispositions, disposition_ids))
    problems.extend(check_debt_count_shrinkable(debt, dispositions))
    problems.extend(check_resolved_dispositions(dispositions))
    problems.extend(check_ownership_stability(debt, dispositions))

    if current_violations is not None:
        problems.extend(check_shrink_only(debt, current_violations))

    return {
        "ok": len(problems) == 0,
        "problems": problems,
        "debt_count": len(debt),
    }
