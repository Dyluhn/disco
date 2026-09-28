"""Scope/evidence checks on disposable Git objects; no candidate code executes."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check_change_scope.py"
spec = importlib.util.spec_from_file_location("trusted_change_scope", SCRIPT)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
RECORD = "development/changes/example.json"


class ChangeScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="disco-scope-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Test Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.write("app.py", "original = True\n")
        self.write("tests/test_app.py", "def test_original(): pass\n")
        self.base = self.commit()
        self.write("app.py", "original = False\n")
        self.tested = self.commit()
        self.record = {
            "version": 1, "id": "scope-example", "issue": "owner-request:fixture",
            "purpose": "Demonstrate one change", "reproduction": "Scripted validator fixture",
            "base": self.base, "paths": ["app.py", RECORD],
            "risks": {k: "No product effect in synthetic fixture" for k in policy.RISK_FIELDS},
            "provider": {"applicable": False, "reason": "Synthetic non-LLM file", "variant_tests": []},
            "evidence": {"commit": self.tested, "tree": self.git("rev-parse", self.tested + "^{tree}"),
                         "checks": [{"command": "fixture-test", "exit": 0,
                                     "artifact_sha256": hashlib.sha256(b"fixture").hexdigest(), "tests": []}]},
            "ui": {"status": "not_applicable", "evidence": "No UI behavior"},
            "quality": {"status": "untested", "evidence": "No real output assessed"},
        }

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True,
                                       stderr=subprocess.DEVNULL).strip()

    def write(self, name, text):
        p = self.repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "Fixture")
        return self.git("rev-parse", "HEAD")

    def final(self):
        self.write(RECORD, json.dumps(self.record))
        return self.commit()

    def run_check(self, head):
        return policy.validate(self.repo, self.base, head)

    def advance_evidence(self):
        tested = self.commit()
        self.record["evidence"].update(commit=tested, tree=self.git("rev-parse", tested + "^{tree}"))

    def test_valid_scoped_change_and_metadata_only_finalization(self):
        result = self.run_check(self.final())
        self.assertEqual(result["status"], "scope_consistent")
        self.assertEqual(result["evidence_commit"], self.tested)
        self.assertEqual(result["requires_independent_review"], [])

    def test_missing_record_fails(self):
        with self.assertRaisesRegex(policy.Invalid, "Exactly one"):
            self.run_check(self.tested)

    def test_out_of_scope_change_is_rejected(self):
        self.write("other.py", "unexpected=True")
        self.advance_evidence()
        with self.assertRaisesRegex(policy.Invalid, "actual base/head diff"):
            self.run_check(self.final())

    def test_renamed_and_deleted_paths_cannot_hide_from_scope(self):
        self.git("mv", "app.py", "renamed.py")
        self.advance_evidence()
        with self.assertRaisesRegex(policy.Invalid, "actual base/head diff"):
            self.run_check(self.final())

    def test_declared_new_source_still_invalidates_old_evidence(self):
        self.write("later.py", "new=True")
        self.record["paths"].append("later.py")
        with self.assertRaisesRegex(policy.Invalid, "Stale evidence"):
            self.run_check(self.final())

    def test_wrong_evidence_tree_and_failed_check_are_rejected(self):
        for field, value, message in [("tree", "0" * 40, "Evidence tree"), ("exit", 1, "did not pass")]:
            with self.subTest(field=field):
                original = json.loads(json.dumps(self.record))
                target = self.record["evidence"] if field == "tree" else self.record["evidence"]["checks"][0]
                target[field] = value
                with self.assertRaisesRegex(policy.Invalid, message):
                    self.run_check(self.final())
                self.record = original

    def test_changed_checks_require_review_even_when_declared(self):
        self.write(".github/workflows/check.yml", "# Candidate cannot remove review")
        self.record["paths"].append(".github/workflows/check.yml")
        self.advance_evidence()
        result = self.run_check(self.final())
        self.assertEqual(result["status"], "requires_independent_review")
        self.assertEqual(result["requires_independent_review"], [".github/workflows/check.yml"])

    def test_test_removal_requires_separate_review(self):
        (self.repo / "tests/test_app.py").unlink()
        self.record["paths"].append("tests/test_app.py")
        self.advance_evidence()
        result = self.run_check(self.final())
        self.assertIn("tests/test_app.py", result["requires_independent_review"])

    def test_candidate_cannot_self_authorize_review(self):
        self.record["approved"] = True
        with self.assertRaisesRegex(policy.Invalid, "record fields"):
            self.run_check(self.final())

    def test_duplicate_keys_and_multiple_records_fail(self):
        self.write(RECORD, '{"version":1,"version":1}')
        with self.assertRaisesRegex(policy.Invalid, "Duplicate JSON key"):
            self.run_check(self.commit())
        self.write("development/changes/second.json", "{}")
        with self.assertRaisesRegex(policy.Invalid, "Exactly one"):
            self.run_check(self.commit())

    def test_llm_paths_cannot_opt_out_of_variant_evidence(self):
        name = "packages/core/src/disco/core/llm/example.py"
        self.write(name, "# fixture")
        self.record["paths"].append(name)
        self.advance_evidence()
        with self.assertRaisesRegex(policy.Invalid, "provider applicability"):
            self.run_check(self.final())
        self.record["provider"].update(applicable=True, variant_tests=["arbitrary-name-equivalence"])
        with self.assertRaisesRegex(policy.Invalid, "lack execution evidence"):
            self.run_check(self.final())
        self.record["evidence"]["checks"][0]["tests"] = ["arbitrary-name-equivalence"]
        self.assertEqual(self.run_check(self.final())["status"], "scope_consistent")

    def test_symlink_record_is_not_followed(self):
        self.write("outside.json", json.dumps(self.record))
        (self.repo / RECORD).parent.mkdir(parents=True, exist_ok=True)
        os.symlink("../../outside.json", self.repo / RECORD)
        with self.assertRaisesRegex(policy.Invalid, "regular Git file"):
            self.run_check(self.commit())

    def test_candidate_checker_is_data_never_executed(self):
        marker = self.repo / "executed"
        name = "development/scripts/check_change_scope.py"
        self.write(name, f"from pathlib import Path; Path({str(marker)!r}).touch()")
        self.record["paths"].append(name)
        self.advance_evidence()
        result = self.run_check(self.final())
        self.assertIn(name, result["requires_independent_review"])
        self.assertFalse(marker.exists())

    def test_changed_base_and_path_traversal_are_rejected(self):
        original = json.loads(json.dumps(self.record))
        self.record["base"] = self.tested
        with self.assertRaisesRegex(policy.Invalid, "current PR base"):
            self.run_check(self.final())
        self.record = original
        self.record["paths"].append("../escape.py")
        with self.assertRaisesRegex(policy.Invalid, "repository-relative"):
            self.run_check(self.final())

    def test_root_conftest_cannot_weaken_selection_without_review(self):
        self.write("conftest.py", "# candidate selection configuration")
        self.record["paths"].append("conftest.py")
        self.advance_evidence()
        self.assertIn("conftest.py", self.run_check(self.final())["requires_independent_review"])

    def test_cli_sensitive_delta_cannot_return_success(self):
        name = "development/harness/cassettes/record.jsonl"
        self.write(name, "{}")
        self.record["paths"].append(name)
        self.advance_evidence()
        head = self.final()
        result = subprocess.run(["python3", str(SCRIPT), "--repo", str(self.repo),
                                 "--base", self.base, "--head", head], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "requires_independent_review")


if __name__ == "__main__":
    unittest.main()
