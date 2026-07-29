"""Debt ratchet mutation tests for the architecture gate.

Tests every shrink-only failure mode: new violation, growth, stale, moved,
deleted, reused ID, resolution leak, ownership drift, source identity, and
limit failures. Uses the ``check_shrink_only`` and ``check_*`` boundaries
with synthetic debt rows and current violations.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import check_arch_debt  # noqa: E402

from architecture import debt  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]


def _debt_row(
    id: str = "PY-TEST",
    path: str = "packages/fake/test.py",
    symbol: str = "f",
    qname: str = "f",
    observed: int = 120,
    limit: int = 100,
    rule: str = "python_callable_logical_gt_100",
    owner: str = "PKG-TEST",
    delete_by: str = "PKG-TEST",
    source_identity: str = "test",
) -> dict:
    return {
        "id": id,
        "path": path,
        "symbol": symbol,
        "qualified_symbol": qname,
        "line_start": 1,
        "line_end": 10,
        "rule": rule,
        "observed": observed,
        "limit": limit,
        "owner_package": owner,
        "delete_by": delete_by,
        "source_identity": source_identity,
    }


def _violation(
    path: str = "packages/fake/test.py",
    qname: str = "f",
    rule: str = "python_callable_logical_gt_100",
    value: int = 120,
    limit: int = 100,
) -> dict:
    return {
        "path": path,
        "qualified_symbol": qname,
        "rule": rule,
        "value": value,
        "limit": limit,
    }


def _copy_debt_authority_tree(root: Path) -> None:
    arch = root / "architecture"
    arch.mkdir()
    for name in (
        "disposition-rows.json",
        "dispositions.json",
        "disposition-ids.txt",
        "debt.json",
        "observations.json",
    ):
        shutil.copy2(REPO_ROOT / "architecture" / name, arch / name)
    for name in ("frontend", "harness", "packages", "scripts"):
        (root / name).symlink_to(REPO_ROOT / name, target_is_directory=True)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestDebtSchema:
    def test_valid_debt_row_passes(self):
        row = _debt_row()
        assert debt.check_debt_schema([row]) == []

    def test_missing_field_fails(self):
        row = _debt_row()
        del row["owner_package"]
        problems = debt.check_debt_schema([row])
        assert len(problems) == 1
        assert "missing fields" in problems[0]

    def test_observed_below_limit_fails_stale(self):
        row = _debt_row(observed=50, limit=100)
        problems = debt.check_debt_schema([row])
        assert len(problems) == 1
        assert "stale" in problems[0].lower()

    def test_observed_equal_limit_fails_stale(self):
        row = _debt_row(observed=100, limit=100)
        problems = debt.check_debt_schema([row])
        assert len(problems) == 1
        assert "stale" in problems[0].lower()


# ---------------------------------------------------------------------------
# Identity uniqueness
# ---------------------------------------------------------------------------


class TestDebtIdentity:
    def test_duplicate_identity_fails(self):
        rows = [
            _debt_row(id="PY-001"),
            _debt_row(id="PY-002"),
        ]
        problems = debt.check_debt_identity_unique(rows)
        assert len(problems) == 1
        assert "duplicate" in problems[0].lower()

    def test_unique_identity_passes(self):
        rows = [
            _debt_row(id="PY-001", path="a.py", qname="f"),
            _debt_row(id="PY-002", path="b.py", qname="g"),
        ]
        assert debt.check_debt_identity_unique(rows) == []


# ---------------------------------------------------------------------------
# Shrink-only: new, growth, stale, moved/deleted
# ---------------------------------------------------------------------------


class TestShrinkOnly:
    def test_new_violation_without_debt_fails(self):
        problems = debt.check_shrink_only([], [_violation()])
        assert len(problems) == 1
        assert "new violation" in problems[0]

    def test_debt_growth_fails(self):
        row = _debt_row(observed=120)
        current = [_violation(value=130)]
        problems = debt.check_shrink_only([row], current)
        assert any("grew" in p for p in problems)

    def test_stale_debt_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        """Stale rows and stale generated authority bytes fail closed."""
        row = _debt_row()
        problems = debt.check_shrink_only([row], [])
        assert any(
            "stale" in problem.lower()
            or "moved" in problem.lower()
            or "deleted" in problem.lower()
            for problem in problems
        )
        _copy_debt_authority_tree(tmp_path)
        observations = tmp_path / "architecture" / "observations.json"
        observations.write_bytes(observations.read_bytes() + b"\n")
        assert debt.run_debt_check(root=tmp_path)["ok"] is True
        monkeypatch.setattr(
            sys,
            "argv",
            ["check_arch_debt.py", "--root", str(tmp_path), "--quiet"],
        )

        assert check_arch_debt.main() == 1
        output = capsys.readouterr().out
        assert "generated authority verification failed" in output
        assert "authority drift: architecture/observations.json" in output

    def test_moved_violation_fails(self):
        """A violation at a different path fails (old path stale, new path new)."""
        row = _debt_row(path="packages/old/test.py")
        current = [_violation(path="packages/new/test.py")]
        problems = debt.check_shrink_only([row], current)
        # Old path is stale/moved/deleted, new path is a new violation
        assert any(
            "stale" in problem.lower()
            or "moved" in problem.lower()
            or "deleted" in problem.lower()
            for problem in problems
        )
        assert any("new violation" in p for p in problems)

    def test_matching_violation_passes(self):
        row = _debt_row(observed=120)
        current = [_violation(value=120)]
        problems = debt.check_shrink_only([row], current)
        assert problems == []

    def test_shrinking_violation_passes(self):
        row = _debt_row(observed=120)
        current = [_violation(value=110)]
        problems = debt.check_shrink_only([row], current)
        assert problems == []

    def test_current_at_limit_while_active_fails_stale(self):
        """A current metric at the limit while the row is active fails as stale."""
        row = _debt_row(observed=120, limit=100)
        current = [_violation(value=100, limit=100)]
        problems = debt.check_shrink_only([row], current)
        assert any("stale" in p.lower() for p in problems)

    def test_current_below_limit_while_active_fails_stale(self):
        row = _debt_row(observed=120, limit=100)
        current = [_violation(value=50, limit=100)]
        problems = debt.check_shrink_only([row], current)
        assert any("stale" in p.lower() for p in problems)


# ---------------------------------------------------------------------------
# ID universe: new, deleted, reused, resolution leak
# ---------------------------------------------------------------------------


class TestIdUniverse:
    def test_unknown_debt_id_fails(self):
        debt_data = [_debt_row(id="UNKNOWN-001")]
        dispositions = [{"id": "PY-0001", "disposition": "VIOLATION", "state": "active"}]
        problems = debt.check_id_universe(debt_data, dispositions, ["PY-0001"])
        assert any("not in disposition universe" in p for p in problems)

    def test_resolved_id_in_active_debt_fails(self):
        debt_data = [_debt_row(id="PY-0890")]
        dispositions = [{"id": "PY-0890", "disposition": "VIOLATION", "state": "resolved"}]
        problems = debt.check_id_universe(debt_data, dispositions, ["PY-0890"])
        assert any("resolved" in p.lower() and "active debt" in p.lower() for p in problems)

    def test_duplicate_debt_ids_fails(self):
        debt_data = [_debt_row(id="PY-001"), _debt_row(id="PY-001", path="other.py")]
        dispositions = [{"id": "PY-001", "disposition": "VIOLATION", "state": "active"}]
        problems = debt.check_id_universe(debt_data, dispositions, ["PY-001"])
        assert any("duplicate" in p.lower() for p in problems)

    def test_disposition_universe_size_must_be_991(self):
        problems = debt.check_id_universe([], [], ["PY-001"] * 10)
        assert any("991" in p for p in problems)

    def test_active_violation_without_debt_row_fails(self):
        """An active VIOLATION disposition without a debt row fails."""
        debt_data = []
        dispositions = [{"id": "PY-0001", "disposition": "VIOLATION", "state": "active"}]
        problems = debt.check_id_universe(debt_data, dispositions, ["PY-0001"])
        assert any("without debt rows" in p for p in problems)


# ---------------------------------------------------------------------------
# Debt count shrinkability
# ---------------------------------------------------------------------------


class TestDebtCount:
    def test_debt_count_growth_fails(self):
        debt_data = [_debt_row(id=f"PY-{i:04d}") for i in range(966)]
        dispositions = [
            {"id": f"PY-{i:04d}", "disposition": "VIOLATION", "state": "active"}
            for i in range(966)
        ]
        problems = debt.check_debt_count_shrinkable(debt_data, dispositions)
        assert any("965" in p for p in problems)

    def test_debt_count_mismatch_fails(self):
        debt_data = [_debt_row(id="PY-0001")]
        dispositions = [
            {"id": "PY-0001", "disposition": "VIOLATION", "state": "active"},
            {"id": "PY-0002", "disposition": "VIOLATION", "state": "active"},
        ]
        problems = debt.check_debt_count_shrinkable(debt_data, dispositions)
        assert any("!=" in p for p in problems)


# ---------------------------------------------------------------------------
# Ownership stability
# ---------------------------------------------------------------------------


class TestOwnershipStability:
    def test_ownership_drift_fails(self):
        debt_data = [_debt_row(id="PY-0001", owner="WRONG-PKG", delete_by="WRONG-PKG")]
        dispositions = [{"id": "PY-0001", "owner_package": "PKG-CORRECT"}]
        problems = debt.check_ownership_stability(debt_data, dispositions)
        assert len(problems) == 2
        assert any("ownership drift" in p for p in problems)
        assert any("delete-by drift" in p for p in problems)

    def test_matching_ownership_passes(self):
        debt_data = [_debt_row(id="PY-0001", owner="PKG-CORRECT", delete_by="PKG-CORRECT")]
        dispositions = [{"id": "PY-0001", "owner_package": "PKG-CORRECT"}]
        assert debt.check_ownership_stability(debt_data, dispositions) == []

    def test_later_delete_by_package_fails(self):
        """A delete_by package different from the original owner fails."""
        debt_data = [_debt_row(id="PY-0001", owner="PKG-A", delete_by="PKG-B")]
        dispositions = [{"id": "PY-0001", "owner_package": "PKG-A"}]
        problems = debt.check_ownership_stability(debt_data, dispositions)
        assert any("delete-by drift" in p for p in problems)


# ---------------------------------------------------------------------------
# Resolved dispositions
# ---------------------------------------------------------------------------


class TestResolvedDispositions:
    def test_missing_resolved_id_fails(self):
        dispositions = [
            {"id": "PY-0890", "state": "active"},
            {"id": "PY-0891", "state": "resolved"},
            {"id": "DM-010", "state": "resolved"},
        ]
        problems = debt.check_resolved_dispositions(dispositions)
        assert any("PY-0890" in p for p in problems)

    def test_all_required_resolved_passes(self):
        dispositions = [
            {"id": "PY-0890", "state": "resolved"},
            {"id": "PY-0891", "state": "resolved"},
            {"id": "DM-010", "state": "resolved"},
        ]
        assert debt.check_resolved_dispositions(dispositions) == []

    def test_additional_resolved_passes(self):
        """The ratchet is permanently shrinkable — additional resolved IDs pass."""
        dispositions = [
            {"id": "PY-0890", "state": "resolved"},
            {"id": "PY-0891", "state": "resolved"},
            {"id": "DM-010", "state": "resolved"},
            {"id": "PY-0001", "state": "resolved"},
        ]
        assert debt.check_resolved_dispositions(dispositions) == []
