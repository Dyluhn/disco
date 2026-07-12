"""Tests for the release-shape detector (`disco.core.release.detect`).

Proves each WO-3 acceptance criterion:

1. table-driven: every fixture tree maps to the exact expected
   `(assessment, runtime strategy, ingress presence, resource kinds)` tuple, and
   a Dockerfile + a typed intent that disagree fail closed to `needs_review`
   citing BOTH pieces of evidence;
2. the unknown-stack fixture WITH a complete intent is a `candidate`; the SAME
   fixture WITHOUT intent is a `needs_review` naming every missing contract field;
3. the non-web fixture is `not_web` with a human reason; the native-sqlite fixture
   yields a sqlite resource bound to `file:/data/app.db`; a `mysql://` reference
   is `needs_review` with NO invented resource;
4. detect.py never references the requesting build lane / caller / free-text
   request (a source scan mirroring the acceptance grep);
5. detection is deterministic — the same file map twice returns EQUAL results.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from disco.core.release.detect import (
    DetectionResult,
    Provenance,
    detect_release,
)
from disco.core.release.spec import (
    ReleaseAssessment,
    ReleaseIntent,
    ResourceKind,
    RuntimeStrategy,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "release_detect"


def _load_fixture(name: str) -> dict[str, bytes]:
    """Walk a fixture tree into a `{posix-relpath: bytes}` map (os.walk so hidden
    files like `.disco/appspec.json` and `.env.example` are included)."""
    root = _FIXTURES / name
    files: dict[str, bytes] = {}
    for dirpath, _dirs, filenames in os.walk(root):
        for filename in filenames:
            full = Path(dirpath) / filename
            files[full.relative_to(root).as_posix()] = full.read_bytes()
    return files


def _complete_intent() -> ReleaseIntent:
    return ReleaseIntent(
        build_cmd=("npm", "run", "build"),
        start_cmd=("node", "dist/server.js"),
        port_env="PORT",
        health_path="/healthz",
        required_env=("SESSION_SECRET", "API_BASE_URL"),
    )


# ---- criterion 1: table-driven exact shape per fixture ------------------------

# name -> (assessment, ingress runtime | None, ingress present, resource kinds)
_EXPECTED: dict[
    str, tuple[ReleaseAssessment, RuntimeStrategy | None, bool, tuple[ResourceKind, ...]]
] = {
    "express-node": (ReleaseAssessment.candidate, RuntimeStrategy.node, True, ()),
    "fastapi": (ReleaseAssessment.candidate, RuntimeStrategy.python, True, ()),
    "static-site": (ReleaseAssessment.candidate, RuntimeStrategy.static, True, ()),
    "vite-spa": (ReleaseAssessment.candidate, RuntimeStrategy.static, True, ()),
    "appkit-shaped": (
        ReleaseAssessment.candidate,
        RuntimeStrategy.dev_server,
        True,
        (ResourceKind.sqlite,),
    ),
    "existing-dockerfile": (ReleaseAssessment.needs_review, None, False, ()),
    "unknown-stack": (ReleaseAssessment.needs_review, None, False, ()),
    "non-web-doc": (ReleaseAssessment.not_web, None, False, ()),
    "native-sqlite": (
        ReleaseAssessment.candidate,
        RuntimeStrategy.node,
        True,
        (ResourceKind.sqlite,),
    ),
}


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_fixture_maps_to_expected_release_shape(name: str) -> None:
    files = _load_fixture(name)
    assert files, f"fixture {name!r} loaded no files"
    # unknown-stack is an imported repo we cannot deterministically detect; that is
    # what routes it to needs_review (rung 4) when no intent is supplied.
    imported = name == "unknown-stack"
    result = detect_release(files, intent=None, provenance=Provenance(imported=imported))

    assessment, runtime, ingress_present, resource_kinds = _EXPECTED[name]
    assert result.assessment is assessment
    assert (result.ingress is not None) is ingress_present
    got_runtime = result.ingress.runtime if result.ingress is not None else None
    assert got_runtime is runtime
    assert tuple(resource.kind for resource in result.resources) == resource_kinds


def test_dockerfile_and_intent_conflict_is_needs_review_with_both_evidence() -> None:
    # A typed intent AND an owner-supplied Dockerfile are two competing release
    # declarations: fail closed to needs_review citing BOTH evidence strings.
    files = _load_fixture("existing-dockerfile")
    result = detect_release(files, intent=_complete_intent(), provenance=Provenance())

    assert result.assessment is ReleaseAssessment.needs_review
    assert any("Dockerfile" in signal for signal in result.evidence)
    assert any("release intent" in signal for signal in result.evidence)


# ---- criterion 2: intent rescues an undetectable stack; its absence is a diag --


def test_unknown_stack_with_complete_intent_is_candidate() -> None:
    files = _load_fixture("unknown-stack")
    result = detect_release(
        files, intent=_complete_intent(), provenance=Provenance(imported=True)
    )

    assert result.assessment is ReleaseAssessment.candidate
    assert result.ingress is not None
    assert result.ingress.runtime is RuntimeStrategy.node
    assert result.ingress.start_cmd == ("node", "dist/server.js")
    assert {var.name for var in result.env} == {"SESSION_SECRET", "API_BASE_URL"}


def test_unknown_stack_without_intent_names_every_missing_contract_field() -> None:
    files = _load_fixture("unknown-stack")
    result = detect_release(files, intent=None, provenance=Provenance(imported=True))

    assert result.assessment is ReleaseAssessment.needs_review
    named = {field.field for field in result.missing}
    assert {"build_cmd", "start_cmd", "port_env", "health_path", "required_env"} <= named
    # the diagnostic is repairable: each missing field carries a human detail.
    assert all(field.detail.strip() for field in result.missing)


# ---- criterion 3: not_web reason, sqlite profile, unknown-engine fail-closed ---


def test_non_web_doc_is_not_web_with_human_reason() -> None:
    files = _load_fixture("non-web-doc")
    result = detect_release(files, intent=None, provenance=Provenance())

    assert result.assessment is ReleaseAssessment.not_web
    assert result.ingress is None
    assert result.reasons and result.reasons[0].strip()


def test_native_sqlite_resource_uses_local_file_profile() -> None:
    files = _load_fixture("native-sqlite")
    result = detect_release(files, intent=None, provenance=Provenance())

    assert result.assessment is ReleaseAssessment.candidate
    assert len(result.resources) == 1
    resource = result.resources[0]
    assert resource.kind is ResourceKind.sqlite
    assert resource.profiles.local.url == "file:/data/app.db"
    assert resource.consumers == ("web",)


def test_unknown_database_engine_is_needs_review_and_invents_no_resource() -> None:
    files = dict(_load_fixture("express-node"))
    files["src/db.ts"] = b'export const url = "mysql://user:pass@db:3306/app";\n'
    result = detect_release(files, intent=None, provenance=Provenance())

    assert result.assessment is ReleaseAssessment.needs_review
    assert result.resources == ()
    assert any("mysql" in signal for signal in result.evidence)


# ---- criterion 4: detection cannot branch on mode (source scan) ---------------


def test_detect_module_does_not_branch_on_mode() -> None:
    import disco.core.release.detect as detect_module

    source = Path(detect_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("surface", "build_mode", "prompt"):
        assert forbidden not in source, f"detect.py must never reference {forbidden!r}"


# ---- criterion 5: determinism -------------------------------------------------


@pytest.mark.parametrize(
    "name", ["express-node", "native-sqlite", "appkit-shaped", "existing-dockerfile"]
)
def test_detection_is_deterministic(name: str) -> None:
    files = _load_fixture(name)
    imported = name == "unknown-stack"
    first = detect_release(files, intent=None, provenance=Provenance(imported=imported))
    second = detect_release(files, intent=None, provenance=Provenance(imported=imported))
    assert isinstance(first, DetectionResult)
    assert first == second


def test_detection_is_deterministic_on_intent_path() -> None:
    files = _load_fixture("unknown-stack")
    intent = _complete_intent()
    first = detect_release(files, intent=intent, provenance=Provenance(imported=True))
    second = detect_release(files, intent=intent, provenance=Provenance(imported=True))
    assert first == second
