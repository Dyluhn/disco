from __future__ import annotations

from pathlib import Path

import pytest

from harness.reliability.matrix import load_matrix

MATRIX = Path(__file__).parents[1] / "matrix.yaml"


def test_repository_matrix_is_complete_and_selectable() -> None:
    matrix = load_matrix(MATRIX)
    assert matrix.claims
    assert matrix.suites
    assert matrix.select_suites(proofs={"live"}, surfaces={"build"})
    assert all(
        suite.proof == "live"
        for suite in matrix.select_suites(proofs={"live"}, surfaces={"build"})
    )


def test_every_live_suite_owns_an_isolated_stack_and_unique_ports() -> None:
    """H-007: promotion must never touch the operator's default localhost stack."""

    matrix = load_matrix(MATRIX)
    ports: set[int] = set()
    seed_env = {
        "DISCO_RELIABILITY_SEED_CONFIG",
        "DISCO_RELIABILITY_SEED_SECRETS",
        "DISCO_RELIABILITY_SEED_SECRET_KEY",
        "DISCO_RELIABILITY_SEED_APPROVALS",
    }
    for suite in matrix.select_suites(proofs={"live"}):
        command = list(suite.command)
        assert "harness.reliability.isolated_stack" in command, suite.id
        assert "{suite_out}/stack" in command, suite.id
        for flag in ("--agent-port", "--app-port", "--ui-port"):
            assert flag in command, (suite.id, flag)
            port = int(command[command.index(flag) + 1])
            assert port not in ports, (suite.id, flag, port)
            ports.add(port)
        if suite.provider_evidence:
            assert seed_env.issubset(suite.requires_env), suite.id
        if suite.kind == "build_soak":
            base = command[command.index("--base-url") + 1]
            assert base not in {
                "http://127.0.0.1:8000",
                "http://localhost:8000",
            }, suite.id


def test_all_model_driven_live_suites_require_provider_evidence() -> None:
    matrix = load_matrix(MATRIX)
    live = matrix.select_suites(proofs={"live"})
    assert {suite.id for suite in live if not suite.provider_evidence} == {
        "live-settings-roundtrip"
    }


def test_matrix_rejects_uncovered_claim(tmp_path: Path) -> None:
    path = tmp_path / "matrix.yaml"
    path.write_text(
        """
schema_version: 1
claims:
  - id: x
    surface: build
    proof: live
    target: 1
    description: uncovered
suites:
  - id: y
    proof: hermetic
    kind: generic
    cwd: .
    timeout_s: 1
    memory_gib: 1
    units: 1
    command: ["true"]
    claims: [missing]
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown claim"):
        load_matrix(path)


def test_matrix_rejects_duplicate_yaml_keys(tmp_path: Path) -> None:
    path = tmp_path / "matrix.yaml"
    path.write_text("schema_version: 1\nschema_version: 2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate YAML mapping key 'schema_version'"):
        load_matrix(path)
