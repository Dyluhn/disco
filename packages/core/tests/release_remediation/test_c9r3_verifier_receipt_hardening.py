"""Focused regression attacks for the C9R3 verifier/receipt correction."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

manifest = importlib.import_module("gen_closeout_acceptance_manifest")
verify = importlib.import_module("verify_export_track1_closeout")


def _load_receipt() -> ModuleType:
    path = SCRIPTS / "export_track1_candidate_receipt.py"
    spec = importlib.util.spec_from_file_location("_c9r3_receipt", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


receipt: Any = _load_receipt()


def _load_live_support() -> ModuleType:
    path = ROOT / "packages/agent-server/tests/integration/_closeout_live_support.py"
    spec = importlib.util.spec_from_file_location("_c9r3_live_support", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


live_support = _load_live_support()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_certification_scripts_require_isolated_no_site_entrypoint(tmp_path: Path) -> None:
    ordinary = subprocess.run(
        [sys.executable, str(SCRIPTS / "export_track1_candidate_receipt.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ordinary.returncode == 2
    assert "pycache_prefix=/dev/null" in ordinary.stderr

    isolated_without_no_bytecode = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-P",
            str(SCRIPTS / "export_track1_candidate_receipt.py"),
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert isolated_without_no_bytecode.returncode == 2
    assert "-B" in isolated_without_no_bytecode.stderr

    isolated_without_cache_prefix = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-P",
            "-B",
            str(SCRIPTS / "export_track1_candidate_receipt.py"),
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert isolated_without_cache_prefix.returncode == 2
    assert "pycache_prefix=/dev/null" in isolated_without_cache_prefix.stderr

    isolated = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-P",
            "-B",
            "-X",
            "pycache_prefix=/dev/null",
            str(SCRIPTS / "export_track1_candidate_receipt.py"),
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert isolated.returncode == 0, isolated.stderr
    assert "--trusted-python-runtime-sha256" in isolated.stdout

    evidence = tmp_path / "ordinary-verifier"
    verifier = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "verify_export_track1_closeout.py"),
            "--all",
            "--repo-root",
            str(ROOT),
            "--evidence-dir",
            str(evidence),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verifier.returncode == 2
    assert "pycache_prefix=/dev/null" in verifier.stderr
    assert not evidence.exists()


def _repo(tmp_path: Path) -> tuple[Path, str, dict[str, object], dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "c9r3")
    _git(repo, "config", "user.email", "c9r3@example.invalid")
    governed = repo / "governed.txt"
    governed.write_text("fixed\n")
    manifest_path = repo / manifest.MANIFEST_REL
    manifest_path.parent.mkdir(parents=True)
    stored: dict[str, object] = {
        "files": {"governed.txt": hashlib.sha256(governed.read_bytes()).hexdigest()}
    }
    manifest_path.write_text(json.dumps(stored))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "candidate")
    head = _git(repo, "rev-parse", "HEAD")
    return repo, head, stored, verify._governed_byte_snapshot(repo, stored)


def test_post_run_integrity_accepts_unchanged_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, head, stored, snapshot = _repo(tmp_path)
    monkeypatch.setattr(verify, "_verify_frozen_manifest", lambda *args: (True, "match"))
    launch = {"runtime": "stable"}
    monkeypatch.setattr(manifest, "observed_launch_identity", lambda _repo: launch)
    ok, detail = verify._post_run_integrity(
        repo,
        candidate_sha=head,
        stored_manifest=stored,
        initial_snapshot=snapshot,
        initial_trusted_launch=launch,
    )
    assert ok is True
    assert detail["head_unchanged"] is True
    assert detail["final_clean_tree"] is True
    assert detail["governed_bytes_unchanged"] is True


def test_post_run_integrity_rejects_governed_mutation_and_untracked_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, head, stored, snapshot = _repo(tmp_path)
    monkeypatch.setattr(verify, "_verify_frozen_manifest", lambda *args: (True, "match"))
    launch = {"runtime": "stable"}
    monkeypatch.setattr(manifest, "observed_launch_identity", lambda _repo: launch)
    (repo / "governed.txt").write_text("mutated\n")
    nested = repo / "untracked" / "deep"
    nested.mkdir(parents=True)
    (nested / "artifact").write_text("x")
    ok, detail = verify._post_run_integrity(
        repo,
        candidate_sha=head,
        stored_manifest=stored,
        initial_snapshot=snapshot,
        initial_trusted_launch=launch,
    )
    assert ok is False
    assert detail["final_clean_tree"] is False
    assert detail["changed_governed_paths"] == ["governed.txt"]
    assert (
        verify._final_verdict(
            frozen_ok=True,
            all_lanes_green=True,
            clean=True,
            author=False,
            hygiene_ok=True,
            post_run_integrity_ok=ok,
        )
        is False
    )


def test_post_run_integrity_rejects_clean_head_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, head, stored, snapshot = _repo(tmp_path)
    monkeypatch.setattr(verify, "_verify_frozen_manifest", lambda *args: (True, "match"))
    launch = {"runtime": "stable"}
    monkeypatch.setattr(manifest, "observed_launch_identity", lambda _repo: launch)
    (repo / "unrelated.txt").write_text("new commit\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "move head during run")
    ok, detail = verify._post_run_integrity(
        repo,
        candidate_sha=head,
        stored_manifest=stored,
        initial_snapshot=snapshot,
        initial_trusted_launch=launch,
    )
    assert ok is False
    assert detail["final_clean_tree"] is True
    assert detail["head_unchanged"] is False


def test_git_nonzero_is_never_interpreted_as_empty_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, head, stored, snapshot = _repo(tmp_path)
    launch = {"runtime": "stable"}
    monkeypatch.setattr(manifest, "observed_launch_identity", lambda _repo: launch)
    real_run = verify._run

    def fail_git(cmd: list[str], *, cwd: Path, env: object = None) -> subprocess.CompletedProcess:
        if cmd and Path(cmd[0]).name == "git":
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: injected git failure")
        return real_run(cmd, cwd=cwd, env=env)

    monkeypatch.setattr(verify, "_run", fail_git)
    with pytest.raises(subprocess.CalledProcessError):
        verify._git(repo, "status", "--porcelain")
    ok, detail = verify._post_run_integrity(
        repo,
        candidate_sha=head,
        stored_manifest=stored,
        initial_snapshot=snapshot,
        initial_trusted_launch=launch,
    )
    assert ok is False
    assert detail["git_error"]
    assert detail["head_unchanged"] is False


def _vitest_payload(path: Path) -> dict[str, object]:
    return {
        "numTotalTests": 1,
        "numPassedTests": 1,
        "numFailedTests": 0,
        "numPendingTests": 0,
        "numTodoTests": 0,
        "testResults": [
            {
                "name": str(path),
                "assertionResults": [{"title": "works", "status": "passed"}],
            }
        ],
    }


def test_frontend_rejects_green_json_from_nonzero_vitest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    frontend = repo / "frontend"
    test_dir = frontend / manifest.FRONTEND_VITEST_DIR.split("/", 1)[1]
    e2e_dir = frontend / manifest.FRONTEND_E2E_DIR.split("/", 1)[1]
    test_dir.mkdir(parents=True)
    e2e_dir.mkdir(parents=True)
    test_file = test_dir / "one.test.tsx"
    test_file.write_text("it('works', () => {})\n")
    (e2e_dir / "one.spec.ts").write_text("test('works', () => {})\n")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "frontend-vitest.json").write_text("stale green")
    (evidence / "frontend-e2e.json").write_text(
        json.dumps(
            {
                "stats": {"expected": 1, "unexpected": 0, "flaky": 0, "skipped": 0},
                "suites": [{"file": "e2e/x/one.spec.ts"}],
            }
        )
    )

    def fake_run(cmd: list[str], *, cwd: Path, env: object = None) -> subprocess.CompletedProcess:
        if "vitest" in cmd:
            (evidence / "frontend-vitest.json").write_text(json.dumps(_vitest_payload(test_file)))
            return subprocess.CompletedProcess(cmd, 9, "", "crashed after writing")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(verify.shutil, "which", lambda _name: "/bin/true")
    monkeypatch.setattr(verify, "_run", fake_run)
    lane = verify._run_frontend_lane(repo, evidence, {"one.test.tsx": {"tests": ["works"]}})
    assert lane.detail["vitest"]["exit_code"] == 9
    assert lane.green is False


def test_frontend_removes_stale_vitest_json_before_nonwriting_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    frontend = repo / "frontend"
    (frontend / manifest.FRONTEND_VITEST_DIR.split("/", 1)[1]).mkdir(parents=True)
    (frontend / manifest.FRONTEND_E2E_DIR.split("/", 1)[1]).mkdir(parents=True)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    stale = evidence / "frontend-vitest.json"
    stale.write_text(json.dumps(_vitest_payload(Path("one.test.tsx"))))
    monkeypatch.setattr(verify.shutil, "which", lambda _name: "/bin/true")
    monkeypatch.setattr(
        verify,
        "_run",
        lambda cmd, *, cwd, env=None: subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    lane = verify._run_frontend_lane(repo, evidence, {})
    assert not stale.exists()
    assert lane.green is False
    assert lane.detail["vitest"]["parsed_ok"] is False


def test_browser_nonzero_with_parseable_green_json_is_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    (repo / "frontend").mkdir(parents=True)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    out = evidence / "frontend-e2e.json"
    out.write_text(json.dumps({"stats": {"expected": 99}}))
    green = json.dumps({"stats": {"expected": 1, "unexpected": 0, "flaky": 0, "skipped": 0}})
    monkeypatch.setattr(verify.shutil, "which", lambda _name: "/bin/true")
    monkeypatch.setattr(
        verify,
        "_run",
        lambda cmd, *, cwd, env=None: subprocess.CompletedProcess(cmd, 7, green, ""),
    )
    summary = verify._run_browser_e2e(repo, evidence)
    assert summary["exit_code"] == 7
    assert summary["status"] == "browser_run_failed"
    assert json.loads(out.read_text())["status"] == "browser_run_failed"


def test_collect_only_nonzero_and_zero_nodes_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        manifest.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 2, "fake::node\n", "boom"),
    )
    with pytest.raises(RuntimeError, match="exited 2"):
        manifest._collect_only_ids(tmp_path, ["pytest"], lane="attack")
    monkeypatch.setattr(
        manifest.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""),
    )
    with pytest.raises(RuntimeError, match="zero governed"):
        manifest._collect_only_ids(tmp_path, ["pytest"], lane="attack")
    monkeypatch.setattr(
        manifest.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "b.py::test_b\na.py::test_a\n", ""),
    )
    assert manifest._collect_only_ids(tmp_path, ["pytest"], lane="positive") == [
        "a.py::test_a",
        "b.py::test_b",
    ]


def test_supplemental_support_lane_is_exact_and_missing_node_is_red(tmp_path: Path) -> None:
    expected = manifest.collect_support_fail_closed_node_ids(ROOT)
    assert len(expected) == 36
    positive_dir = tmp_path / "positive"
    positive_dir.mkdir()
    positive = verify._run_support_fail_closed_lane(ROOT, positive_dir, expected)
    assert positive.green is True, positive.rejection_reasons
    assert positive.exit_code == 0
    assert positive.junit == {"tests": 36, "failures": 0, "errors": 0, "skipped": 0}
    assert positive.detail["selected"] == expected

    negative_dir = tmp_path / "negative"
    negative_dir.mkdir()
    negative = verify._run_support_fail_closed_lane(ROOT, negative_dir, expected[:-1])
    assert negative.green is False
    assert any("inventory mismatch" in reason for reason in negative.rejection_reasons)
    assert any("executed selection mismatch" in reason for reason in negative.rejection_reasons)
    assert any("JUnit count mismatch" in reason for reason in negative.rejection_reasons)


def test_supplemental_support_boundary_is_frozen_but_not_mislabelled_live() -> None:
    rel = manifest.SUPPORT_FAIL_CLOSED_TEST_FILE
    assert rel in manifest.FROZEN_FILES
    assert "hermetic_live_support_fail_closed" in manifest.REQUIRED_COMMAND_IDS
    live_scanned = {
        path.relative_to(ROOT).as_posix() for path in verify._frozen_python_test_files(ROOT)
    }
    assert rel not in live_scanned


def test_successful_command_inventory_uses_exit_codes_not_tool_presence() -> None:
    def lane(name: str, rc: int | None) -> Any:
        return verify.LaneResult(name=name, status="ran", exit_code=rc)

    frontend = lane("frontend", None)
    frontend.detail = {
        "vitest": {"exit_code": 0},
        "typecheck": {"exit_code": 0},
        "build": {"exit_code": 0},
    }
    ids = verify._successful_command_ids(
        nonlive=lane("python-nonlive", 0),
        closeout=lane("python-closeout", 0),
        closeout_collect_ok=True,
        browser_summary={"status": "executed", "exit_code": 1},
        frontend=frontend,
        g11=lane("g11", 0),
        live=lane("live", 0),
        capture=lane("capture", 0),
        support_fail_closed=lane("support", 0),
    )
    assert "browser_e2e" not in ids
    assert ids == set(manifest.REQUIRED_COMMAND_IDS) - {"browser_e2e"}
    all_ids = verify._successful_command_ids(
        nonlive=lane("python-nonlive", 0),
        closeout=lane("python-closeout", 0),
        closeout_collect_ok=True,
        browser_summary={"status": "executed", "exit_code": 0},
        frontend=frontend,
        g11=lane("g11", 0),
        live=lane("live", 0),
        capture=lane("capture", 0),
        support_fail_closed=lane("support", 0),
    )
    assert all_ids == set(manifest.REQUIRED_COMMAND_IDS)


def _binding_record() -> dict[str, object]:
    release_json: dict[str, object] = {
        "schema_version": 2,
        "kind": "node",
        "name": "demo",
        "version_seq": 4,
        "tree_digest": "a" * 64,
        "services": [],
        "env": [],
        "resources": [],
        "provenance": {"assessment": "candidate"},
        "targets": ["local_compose"],
        "generated_files": [],
    }
    canonical = json.dumps(
        release_json, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    digest = "sha256:" + hashlib.sha256(canonical).hexdigest()
    return {
        "release_json": release_json,
        "release_response": {
            "assessment": "candidate",
            "version_seq": 4,
            "tree_digest": "a" * 64,
            "spec_digest": digest,
        },
        "source_binding": {
            "bound_version_seq": 4,
            "bound_spec_digest": digest,
            "bundle_assessment": "candidate",
            "release_status": 200,
            "response_matches_supplied": True,
        },
        "binding_matches_response": True,
    }


def test_live_binding_is_recomputed_and_cross_compared() -> None:
    record = _binding_record()
    assert verify._release_binding_reasons("demo", record) == []
    record["binding_matches_response"] = True
    response = record["release_response"]
    assert isinstance(response, dict)
    response["tree_digest"] = "b" * 64
    response["spec_digest"] = "sha256:" + "0" * 64
    reasons = verify._release_binding_reasons("demo", record)
    assert any("tree_digest disagrees" in reason for reason in reasons)
    assert any("spec_digest does not match" in reason for reason in reasons)


def test_live_binding_rejects_source_assessment_disagreement() -> None:
    record = _binding_record()
    source = record["source_binding"]
    assert isinstance(source, dict)
    source["bundle_assessment"] = "needs_review"
    reasons = verify._release_binding_reasons("demo", record)
    assert reasons == [
        "live evidence: demo assessment disagrees across response/release.json/binding"
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [("release_status", 503), ("response_matches_supplied", False)],
)
def test_live_binding_rejects_unvalidated_second_release_response(
    field: str, value: object
) -> None:
    record = _binding_record()
    source = record["source_binding"]
    assert isinstance(source, dict)
    source[field] = value
    reasons = verify._release_binding_reasons("demo", record)
    assert reasons == [
        "live evidence: demo second release response was not a matching 200 response"
    ]


def _valid_disk_payload() -> Any:
    return {
        "rows": [
            {
                "Type": "Images",
                "TotalCount": "2",
                "Active": "1",
                "Size": "100B",
                "Reclaimable": "0B (0%)",
            },
            {
                "Type": "Containers",
                "TotalCount": "1",
                "Active": "1",
                "Size": "6.074MB",
                "Reclaimable": "0B",
            },
            {
                "Type": "Local Volumes",
                "TotalCount": "0",
                "Active": "0",
                "Size": "0B",
                "Reclaimable": "0B (0%)",
            },
            {
                "Type": "Build Cache",
                "TotalCount": "10",
                "Active": "0",
                "Size": "10.24GB",
                "Reclaimable": "10.24GB (100%)",
            },
        ]
    }


def test_cleanup_requires_real_empty_inventory_and_zero_errors() -> None:
    clean = {
        "zero_remaining": True,
        "remaining": {"containers": [], "networks": [], "volumes": [], "images": []},
        "errors": [],
        "disk_before": _valid_disk_payload(),
        "disk_after": _valid_disk_payload(),
    }
    assert verify._cleanup_evidence_reasons("demo", clean) == []
    forged = {**clean, "errors": ["docker ps failed"]}
    reasons = verify._cleanup_evidence_reasons("demo", forged)
    assert reasons == ["live evidence: demo cleanup recorded one or more errors"]
    missing_kind = {**clean, "remaining": {"containers": []}}
    assert any(
        "zero remaining" in reason
        for reason in verify._cleanup_evidence_reasons("demo", missing_kind)
    )
    garbage_disk = {**clean, "disk_before": {"available": 100}}
    assert any(
        "missing real disk" in reason
        for reason in verify._cleanup_evidence_reasons("demo", garbage_disk)
    )
    invalid_row = {
        **clean,
        "disk_before": {
            "rows": [
                {
                    "Type": "Images",
                    "TotalCount": True,
                    "Active": "0",
                    "Size": "1B",
                    "Reclaimable": "0B",
                },
                *_valid_disk_payload()["rows"][1:],
            ]
        },
    }
    assert any(
        "missing real disk" in reason
        for reason in verify._cleanup_evidence_reasons("demo", invalid_row)
    )


@pytest.mark.parametrize(
    ("mutator", "expected"),
    [
        (lambda value: value, True),
        (lambda value: {**value, "extra": []}, False),
        (lambda value: {"rows": value["rows"][:-1]}, False),
        (
            lambda value: {"rows": [{**value["rows"][0], "TotalCount": "02"}, *value["rows"][1:]]},
            False,
        ),
        (
            lambda value: {"rows": [{**value["rows"][0], "Active": "3"}, *value["rows"][1:]]},
            False,
        ),
        (
            lambda value: {"rows": [{**value["rows"][0], "Size": 100}, *value["rows"][1:]]},
            False,
        ),
        (
            lambda value: {
                "rows": [
                    {**value["rows"][0], "Reclaimable": "4MB (101%)"},
                    *value["rows"][1:],
                ]
            },
            False,
        ),
        (
            lambda value: {"rows": [value["rows"][0], value["rows"][0], *value["rows"][2:]]},
            False,
        ),
    ],
)
def test_live_capture_and_verifier_share_docker_df_semantics(mutator: Any, expected: bool) -> None:
    candidate = mutator(_valid_disk_payload())
    assert live_support.valid_docker_disk_usage(candidate) is expected
    assert verify._valid_docker_disk_usage(candidate) is expected


def test_python_injection_environment_is_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHONPATH", "/attacker")
    monkeypatch.setenv("PYTEST_PLUGINS", "attacker")
    monkeypatch.setenv("LD_PRELOAD", "/attacker.so")
    monkeypatch.setenv("LD_AUDIT", "/attacker-audit.so")
    monkeypatch.setenv("DYLD_PRINT_TO_FILE", "/tmp/attacker")
    monkeypatch.setenv("COVERAGE_FILE", "/tmp/attacker-coverage")
    env, removed = receipt.sanitized_python_env()
    injected = {
        "PYTHONPATH",
        "PYTEST_PLUGINS",
        "LD_PRELOAD",
        "LD_AUDIT",
        "DYLD_PRINT_TO_FILE",
        "COVERAGE_FILE",
    }
    assert injected <= set(removed)
    assert not (injected & set(env))
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"


def test_git_control_environment_and_fake_path_are_not_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    repo, _head, _stored, _snapshot = _repo(nested)
    rogue = tmp_path / "bin"
    rogue.mkdir()
    canary = tmp_path / "FAKE-GIT-RAN"
    fake_git = rogue / "git"
    fake_git.write_text(f"#!/bin/sh\ntouch {canary}\nexit 0\n")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", str(rogue))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "attacker.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "attacker-tree"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "attacker-config"))

    env, removed = receipt.sanitized_git_env()
    assert {"GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG_GLOBAL"} <= set(removed)
    assert not any(key.startswith("GIT_") for key in env)
    resolved = receipt.resolve_system_git()
    assert resolved in {Path("/usr/bin/git").resolve(), Path("/bin/git").resolve()}

    assert receipt.git(repo, "rev-parse", "--is-inside-work-tree") == "true"
    assert verify._git(repo, "rev-parse", "--is-inside-work-tree") == "true"
    assert not canary.exists()


def test_git_runtime_is_fixed_recorded_and_not_inventory_host_pinned() -> None:
    identity = receipt.git_runtime_identity()
    assert receipt.check_git_runtime_boundary(identity).ok is True
    assert identity["realpath"] in {
        str(Path("/usr/bin/git").resolve()),
        str(Path("/bin/git").resolve()),
    }
    policy = receipt.trusted_launch_policy()
    assert "trusted_git" not in policy
    assert str(identity["sha256"]) not in json.dumps(policy)
    assert "owner_uid" not in policy


def test_canonical_repository_identity_rejects_outer_repo_inheritance(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    _git(outer, "init", "-q")
    _git(outer, "config", "user.name", "c9r3")
    _git(outer, "config", "user.email", "c9r3@example.invalid")
    (outer / "tracked.txt").write_text("tracked\n")
    _git(outer, "add", "-A")
    _git(outer, "commit", "-q", "-m", "base")
    nested = outer / "nested"
    nested.mkdir()

    check = receipt.check_repository_identity(nested)
    assert check.ok is False
    assert "repository root mismatch" in check.detail


def test_canonical_repository_identity_supports_linked_worktree(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-q")
    _git(primary, "config", "user.name", "c9r3")
    _git(primary, "config", "user.email", "c9r3@example.invalid")
    (primary / "tracked.txt").write_text("tracked\n")
    _git(primary, "add", "-A")
    _git(primary, "commit", "-q", "-m", "base")
    linked = tmp_path / "linked"
    _git(primary, "worktree", "add", "-q", "-b", "c9r3-linked", str(linked))

    identity = receipt.git_repository_identity(linked)
    assert identity["canonical_toplevel"] == str(linked.resolve())
    assert Path(str(identity["canonical_git_dir"])).is_dir()
    assert receipt.check_repository_identity(linked, identity).ok is True


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_ambiguous_git_index_flags_are_rejected(tmp_path: Path, flag: str) -> None:
    repo, _head, _stored, _snapshot = _repo(tmp_path)
    _git(repo, "update-index", flag, "governed.txt")
    check = receipt.check_index_entry_flags(repo)
    assert check.ok is False
    assert check.data["ambiguous"]


def test_post_run_interpreter_identity_is_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    initial = receipt.interpreter_identity()
    assert receipt.check_interpreter_identity_unchanged(initial).ok is True
    monkeypatch.setattr(
        receipt,
        "interpreter_identity",
        lambda: {**initial, "sha256": "0" * 64},
    )
    changed = receipt.check_interpreter_identity_unchanged(initial)
    assert changed.ok is False
    assert changed.data["initial"] == initial
    assert changed.data["final"] != initial


def test_anti_bypass_git_failure_is_red_not_empty_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "_frozen_python_test_files", lambda _repo: [])
    monkeypatch.setattr(verify, "_frozen_frontend_test_files", lambda _repo: [])
    monkeypatch.setattr(verify, "_production_src_files", lambda _repo: [])
    monkeypatch.setattr(verify, "_load_suppression_baseline", lambda _repo: (set(), "test"))

    def fail_git(_repo: Path, *_args: str) -> str:
        raise subprocess.CalledProcessError(128, ["/usr/bin/git"], stderr="injected failure")

    monkeypatch.setattr(verify, "_git", fail_git)
    lane = verify._run_scanner_lane(ROOT, tmp_path)
    assert lane.green is False
    report = json.loads((tmp_path / "anti-bypass-scan.json").read_text())
    assert report["violations"][0]["rule"] == "campaign_diff_unavailable"


def test_verifier_and_collect_environments_are_closed_to_inherited_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "PYTHONPATH",
        "PYTEST_PLUGINS",
        "LD_AUDIT",
        "LD_PRELOAD",
        "DYLD_LIBRARY_PATH",
        "COVERAGE_PROCESS_START",
    ):
        monkeypatch.setenv(name, f"/attacker/{name}")
    verifier_env = verify._pytest_env()
    assert "PYTHONPATH" not in verifier_env
    assert verifier_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert not any(
        key in verifier_env
        for key in (
            "PYTEST_PLUGINS",
            "LD_AUDIT",
            "LD_PRELOAD",
            "DYLD_LIBRARY_PATH",
            "COVERAGE_PROCESS_START",
        )
    )
    collect_env = manifest._collect_env()
    assert "PYTHONPATH" not in collect_env
    assert collect_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    plugins = set(receipt._EXPLICIT_PYTEST_PLUGINS)
    assert plugins == {
        "pytest_asyncio.plugin",
        "anyio.pytest_plugin",
        "_hypothesis_pytestplugin",
    }


def test_inherited_fake_pytest_cannot_forge_execution_or_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rogue = tmp_path / "rogue"
    fake = rogue / "pytest"
    fake.mkdir(parents=True)
    canary = tmp_path / "FAKE-PYTEST-LOADED"
    (fake / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(canary)!r}).write_text('loaded')\n"
    )
    test_file = tmp_path / "test_real.py"
    test_file.write_text("def test_real():\n    assert True\n")
    monkeypatch.setenv("PYTHONPATH", str(rogue))
    evidence = tmp_path / "evidence"
    evidence.mkdir()

    lane = verify._run_pytest_lane(
        ROOT,
        evidence,
        name="isolation",
        paths=(str(test_file),),
        marker="not integration",
        junit_filename="isolated.xml",
        report_filename="isolated.json",
    )
    assert lane.exit_code == 0
    assert lane.junit == {"tests": 1, "failures": 0, "errors": 0, "skipped": 0}

    command = manifest.controlled_pytest_command(ROOT, [str(test_file), "--collect-only", "-q"])
    assert command[1:8] == [
        "-I",
        "-S",
        "-P",
        "-B",
        "-X",
        "pycache_prefix=/dev/null",
        "-c",
    ]
    observed = manifest._collect_only_ids(ROOT, command, lane="isolated")
    assert len(observed) == 1 and observed[0].endswith("test_real.py::test_real")
    assert not canary.exists()
    assert not (tmp_path / "__pycache__").exists()


def test_controlled_launch_ignores_timestamp_valid_ordinary_pycache(tmp_path: Path) -> None:
    module = tmp_path / "cached_target.py"
    module.write_text("VALUE = 'old'\n")
    seed = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {str(tmp_path)!r}); import cached_target",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={key: value for key, value in os.environ.items() if key != "PYTHONDONTWRITEBYTECODE"},
    )
    assert seed.returncode == 0, seed.stderr
    caches = list((tmp_path / "__pycache__").glob("cached_target*.pyc"))
    assert len(caches) == 1
    original_stat = module.stat()
    module.write_text("VALUE = 'new'\n")  # same byte length as the cached source
    os.utime(module, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    ordinary = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            f"import sys; sys.path.insert(0, {str(tmp_path)!r}); "
            "import cached_target; print(cached_target.VALUE)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ordinary.stdout.strip() == "old", "control cache was not timestamp-valid"

    controlled = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-P",
            "-B",
            "-X",
            "pycache_prefix=/dev/null",
            "-c",
            f"import os, sys; assert sys.pycache_prefix == os.devnull; "
            f"sys.path.insert(0, {str(tmp_path)!r}); import cached_target; "
            "print(cached_target.VALUE)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert controlled.returncode == 0, controlled.stderr
    assert controlled.stdout.strip() == "new"
    assert len(list((tmp_path / "__pycache__").glob("cached_target*.pyc"))) == 1


def test_complete_runtime_digest_binds_pytest_pth_pyc_native_and_symlink_metadata(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "site-packages"
    pytest_root = runtime / "_pytest"
    cache = pytest_root / "__pycache__"
    cache.mkdir(parents=True)
    (pytest_root / "__init__.py").write_bytes(b"pytest-source")
    pth = runtime / "editable.pth"
    pth.write_bytes(b"/trusted/source\n")
    pyc = cache / "module.cpython-313.pyc"
    pyc.write_bytes(b"compiled-bytecode")
    native = runtime / "native.so"
    native.write_bytes(b"native-module")
    link = runtime / "native-link.so"
    link.symlink_to("native.so")

    baseline = receipt.complete_tree_sha256(runtime)
    observed = {baseline}
    for path, data in (
        (pytest_root / "__init__.py", b"pytest-mutated"),
        (pth, b"import attacker\n"),
        (pyc, b"forged-bytecode"),
        (native, b"forged-native"),
    ):
        path.write_bytes(data)
        mutated = receipt.complete_tree_sha256(runtime)
        observed.add(mutated)
        assert (
            receipt.check_complete_runtime_trust(
                {},
                True,
                {"complete_runtime_sha256": mutated},
                baseline,
            ).ok
            is False
        )
    link.unlink()
    link.symlink_to("other.so")
    with pytest.raises(OSError, match="dangling, cyclic, or escapes"):
        receipt.complete_tree_sha256(runtime)
    assert len(observed) == 5


def test_runtime_tree_symlinks_are_strictly_confined(tmp_path: Path) -> None:
    runtime = tmp_path / "site-packages"
    runtime.mkdir()
    target = runtime / "inside.py"
    target.write_text("inside = True\n")
    link = runtime / "alias.py"
    link.symlink_to("inside.py")
    assert len(receipt.complete_tree_sha256(runtime)) == 64

    link.unlink()
    link.symlink_to(target.resolve())
    with pytest.raises(OSError, match="absolute symlink"):
        receipt.complete_tree_sha256(runtime)

    link.unlink()
    outside = tmp_path / "outside.py"
    outside.write_text("outside = True\n")
    link.symlink_to("../outside.py")
    with pytest.raises(OSError, match="escapes"):
        receipt.complete_tree_sha256(runtime)

    link.unlink()
    link.symlink_to("alias.py")
    with pytest.raises(OSError, match="cyclic"):
        receipt.complete_tree_sha256(runtime)


def test_complete_runtime_trust_root_rejects_wrong_environment(tmp_path: Path) -> None:
    runtime = tmp_path / "site-packages"
    runtime.mkdir()
    (runtime / "_pytest.py").write_text("runner = True\n")
    digest = receipt.complete_tree_sha256(runtime)
    identity = {"complete_runtime_sha256": digest}
    unpinned = receipt.check_complete_runtime_trust({}, True, identity, None)
    assert unpinned.ok is True
    check = receipt.check_complete_runtime_trust({}, True, identity, "0" * 64)
    assert check.ok is False
    good = receipt.check_complete_runtime_trust({}, True, identity, digest)
    assert good.ok is True


def test_frozen_launch_policy_is_location_independent_and_disables_bytecode() -> None:
    policy = receipt.trusted_launch_policy()
    rendered = json.dumps(policy, sort_keys=True)
    assert str(ROOT) not in rendered
    assert str(Path(sys.executable).resolve()) not in rendered
    assert policy["required_python_flags"] == {
        "isolated": True,
        "no_site": True,
        "safe_path": True,
        "dont_write_bytecode": True,
        "pycache_prefix_devnull": True,
    }
    assert policy["runtime_root"].startswith("venv/lib/python")
    assert "scripts/export_track1_candidate_receipt.py" in manifest.FROZEN_FILES
    assert "uv.lock" in manifest.FROZEN_FILES


def test_pinned_evidence_fd_cannot_be_retargeted_into_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    output = tmp_path / "evidence"
    check, fd = receipt.secure_evidence_directory(output, repo)
    assert check.ok and fd is not None
    pinned = tmp_path / "pinned-original"
    output.rename(pinned)
    output.symlink_to(repo, target_is_directory=True)
    try:
        receipt.write_evidence_at(fd, "candidate-receipt.json", {"green": True})
    finally:
        os.close(fd)
    assert json.loads((pinned / "candidate-receipt.json").read_text()) == {"green": True}
    assert not (repo / "candidate-receipt.json").exists()


def test_pinned_evidence_directory_renamed_into_repo_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    output = tmp_path / "evidence"
    check, fd = receipt.secure_evidence_directory(output, repo)
    assert check.ok and fd is not None
    moved = repo / "moved-evidence"
    output.rename(moved)
    try:
        assert receipt._evidence_fd_is_outside_repo(fd, repo) is False
        with pytest.raises(OSError, match="moved inside checkout"):
            receipt.write_evidence_at(fd, "candidate-receipt.json", {"green": True}, repo=repo)
    finally:
        os.close(fd)
    assert not (moved / "candidate-receipt.json").exists()


def test_evidence_moved_into_repo_during_publication_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    output = tmp_path / "evidence"
    check, fd = receipt.secure_evidence_directory(output, repo)
    assert check.ok and fd is not None
    moved = repo / "moved-during-write"
    real_check = receipt._evidence_fd_is_outside_repo
    calls = 0

    def move_after_publish(directory_fd: int, checked_repo: Path) -> bool:
        nonlocal calls
        calls += 1
        if calls == 3:
            output.rename(moved)
        return real_check(directory_fd, checked_repo)

    monkeypatch.setattr(receipt, "_evidence_fd_is_outside_repo", move_after_publish)
    try:
        with pytest.raises(OSError, match="during publication"):
            receipt.write_evidence_at(fd, "candidate-receipt.json", {"green": True}, repo=repo)
    finally:
        os.close(fd)
    assert calls == 3
    assert not (moved / "candidate-receipt.json").exists()
