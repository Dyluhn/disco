"""Adversarial tests for the inventory execution-coverage gate.

PKG-19-CERT-STRUCTURAL findings F1/F2/F6 were one defect wearing three faces:
the repository had two test collectors and no comparator between them, so
``development/architecture/test-inventory.json`` certified 1213 ids no sanctioned command
could reach. ``architecture.inventory_execution`` is that comparator.

A gate is only worth its runtime if it can be shown to go red for the thing it
claims to catch, so every check here carries its mutation. The two vacuous-
control traps this campaign has hit before are both covered explicitly: a
comparison against an empty set (which always passes), and a declaration that
has silently drifted from the command it claims to quote.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "development" / "scripts"))
from architecture import inventory_execution  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]


def _inventory_with(root: Path, certified: dict[str, list[str]]) -> None:
    """Write a minimal inventory carrying exactly the given certified roots."""
    arch = root / "development" / "architecture"
    arch.mkdir(parents=True, exist_ok=True)
    (arch / "test-inventory.json").write_text(
        json.dumps(
            {
                "schema": "disclaude-architecture-test-inventory-v1",
                "collected": {
                    "total": sum(len(v) for v in certified.values()),
                    "counts": {k: len(v) for k, v in certified.items()},
                    "roots": certified,
                },
            }
        ),
        encoding="utf-8",
    )


class TestDeclarationDrift:
    """The gate must not become a third authority beside Makefile and CI."""

    def test_real_declarations_are_quoted_verbatim_in_their_sources(self):
        """Positive control: every declared command exists in its named source."""
        assert inventory_execution.declaration_drift(REPO_ROOT) == []

    def test_declaration_that_no_longer_matches_its_source_fails(self, tmp_path):
        """Mutation: a Makefile that no longer contains the quoted command."""
        (tmp_path / "Makefile").write_text("unit:\n\techo nothing\n", encoding="utf-8")
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
        (tmp_path / ".github" / "workflows" / "ci.yml").write_text("{}\n", encoding="utf-8")

        problems = inventory_execution.declaration_drift(tmp_path)

        assert problems, "declaration drift must be detected, not tolerated"
        assert any("drifted" in p for p in problems)

    def test_every_declared_command_names_a_source_file_the_gate_reads(self):
        """A declaration pointing at an unread file could never be checked."""
        for spec in inventory_execution.SANCTIONED_COMMANDS:
            source_file = spec["source"].split(":", 1)[0]
            assert source_file in inventory_execution._DECLARATION_SOURCES


class TestOrphanDetection:
    """The comparison itself: certified ids no sanctioned command reaches."""

    def test_orphaned_certified_id_is_reported_with_its_root(self):
        certified = {"packages": {"a.py::test_one", "a.py::test_two"}}
        executable = {"a.py::test_one"}

        problems = inventory_execution._orphan_problems(certified, executable)

        assert len(problems) == 1
        assert "1 of 2 certified ids" in problems[0]
        assert "a.py::test_two" in problems[0]

    def test_fully_covered_root_reports_nothing(self):
        certified = {"packages": {"a.py::test_one"}}
        executable = {"a.py::test_one", "a.py::test_extra"}

        assert inventory_execution._orphan_problems(certified, executable) == []

    def test_orphan_report_is_truncated_but_says_so(self):
        """A 1200-id report must stay readable without hiding the magnitude."""
        certified = {"harness": {f"h.py::test_{i}" for i in range(60)}}

        problems = inventory_execution._orphan_problems(certified, set())

        assert "60 of 60" in problems[0]
        assert f"+{60 - inventory_execution._MAX_REPORTED} more" in problems[0]


class TestVacuousControlGuards:
    """The traps this campaign has actually fallen into before."""

    def test_empty_executable_set_fails_rather_than_passing_vacuously(self, tmp_path):
        """A comparison against nothing always succeeds — so it must not run.

        The first run of this gate collected zero ids (a doubled ``-q`` switched
        pytest's ``--collect-only`` to a per-file summary). Without this guard it
        would have reported full coverage while proving nothing.
        """
        _inventory_with(tmp_path, {"packages": ["a.py::test_one"]})
        (tmp_path / "Makefile").write_text("", encoding="utf-8")
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
        (tmp_path / ".github" / "workflows" / "ci.yml").write_text("{}\n", encoding="utf-8")

        result = inventory_execution.check_inventory_execution(root=tmp_path)

        assert result["ok"] is False
        assert any("refusing to report coverage" in p for p in result["problems"])
        assert result["orphan_total"] == result["certified_total"]

    def test_collection_failure_is_an_error_not_an_empty_result(self, tmp_path, monkeypatch):
        """F2's symptom was a *failed collection*; it must never read as zero ids."""
        spec = {
            "label": "deliberately broken",
            "source": "Makefile:unit",
            "argv": ["-m", "pytest", "--collect-only", "no/such/path"],
            "needs_repo_root_on_path": False,
            "quote": "uv run pytest",
        }

        ids, error = inventory_execution.collect_for_command(tmp_path, spec)

        assert ids == set()
        assert error, "a failed collection must surface as an error, not silence"
        assert "deliberately broken" in error
        from types import SimpleNamespace

        monkeypatch.setattr(
            inventory_execution.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(
                returncode=2,
                stdout="packages/a.py::test_partial\n",
                stderr="one module failed import",
            ),
        )
        ids, error = inventory_execution.collect_for_command(tmp_path, spec)
        assert ids == set() and "exit 2" in error and "1 ids" in error


class TestCertifiedIdsSource:
    def test_certified_ids_are_read_from_the_sealed_inventory(self, tmp_path):
        _inventory_with(tmp_path, {"packages": ["a.py::test_one"], "harness": ["h.py::test_two"]})

        certified = inventory_execution.certified_ids(tmp_path)

        assert certified == {"packages": {"a.py::test_one"}, "harness": {"h.py::test_two"}}

    def test_real_inventory_exposes_every_python_root(self):
        """Positive control against the real tree: all four roots are certified."""
        certified = inventory_execution.certified_ids(REPO_ROOT)

        assert set(certified) == {"packages", "harness", "integrations", "tests"}
        assert all(ids for ids in certified.values())


def test_execution_node_ids_match_inventory_labels_without_changing_parameters(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    raw = (
        "packages/a.py::test_case[development/current/value]\n"
        "development/harness/b.py::test_case\n"
        "integrations/c.py::test_case\n"
        "development/tests/d.py::test_case\n"
    )
    monkeypatch.setattr(
        inventory_execution.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=raw, stderr=""),
    )
    ids, error = inventory_execution.collect_for_command(
        tmp_path, {"label": "test", "argv": [], "needs_repo_root_on_path": False}
    )
    assert error == ""
    assert ids == {
        "packages/a.py::test_case[development/current/value]",
        "harness/b.py::test_case",
        "integrations/c.py::test_case",
        "tests/d.py::test_case",
    }
