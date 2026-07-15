"""CONTRACT-1 tests: artifact-contract domain models — roundtrip, immutability,
validation, and the minimal() factory."""

from __future__ import annotations

import pytest
from disco.core.contract import (
    ArtifactContract,
    BuildContract,
    ContractKind,
    EditContract,
    ExportContract,
    ToolPack,
    VerificationContract,
    VerificationLevel,
)
from pydantic import BaseModel, ValidationError


def _roundtrip(m: BaseModel) -> None:
    assert type(m).model_validate(m.model_dump(mode="json")) == m


def test_all_models_roundtrip() -> None:
    bc = BuildContract(
        kind=ContractKind.APPKIT_LEADGEN,
        artifact=ArtifactContract(
            kind=ContractKind.APPKIT_LEADGEN,
            required_files=("index.html", ".disco/appspec.json"),
            starter_kit="lead_form",
        ),
        bootstrap=ToolPack(name="appkit.bootstrap", tools=("app_create",)),
        edit=EditContract(edit_tools=("app_update_content",), rewrite_allowed=False),
        verify=VerificationContract(
            finalizer="ready_for_app_verification", level=VerificationLevel.STRICT
        ),
        export=ExportContract(
            name="cloudflare", pipeline=("preflight", "bundle", "validate", "deliver")
        ),
        prompt_pack="build_appkit_leadgen",
        ui_card="AppCard",
    )
    for m in (bc, bc.artifact, bc.bootstrap, bc.edit, bc.verify, bc.export):
        _roundtrip(m)  # type: ignore[arg-type]


def test_contract_kind_wire_values() -> None:
    assert ContractKind.APPKIT_LEADGEN.value == "appkit.leadgen"
    assert ContractKind.STATIC_SITE.value == "static.site"
    assert {k.value for k in ContractKind} >= {
        "appkit.leadgen",
        "static.site",
        "interactive.prototype",
        "deck",
        "document",
        "workflow.output",
        "custom",
    }


def test_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        ToolPack.model_validate({"name": "x", "bogus": 1})


def test_bad_enum_rejected() -> None:
    with pytest.raises(ValidationError):
        VerificationContract.model_validate({"finalizer": "f", "level": "nonsense"})


def test_frozen_immutable() -> None:
    ac = ArtifactContract(kind=ContractKind.DECK)
    with pytest.raises(ValidationError):
        ac.kind = ContractKind.DOCUMENT  # type: ignore[misc]


def test_minimal_factory_is_well_formed() -> None:
    for kind in ContractKind:
        bc = BuildContract.minimal(kind)
        assert bc.kind is kind
        assert bc.artifact.kind is kind
        assert bc.bootstrap.name == f"{kind.value}.bootstrap"
        assert bc.verify.finalizer == "ready_for_artifact_verification"
        assert bc.verify.level is VerificationLevel.STANDARD
        assert bc.export is None
        _roundtrip(bc)


def test_defaults() -> None:
    bc = BuildContract.minimal(ContractKind.STATIC_SITE)
    assert bc.edit.rewrite_allowed is False
    assert bc.artifact.required_files == ()
    assert bc.artifact.starter_kit is None
    assert bc.prompt_pack is None and bc.ui_card is None


# --- semantic invariants (Codex round 1) --------------------------------------
def test_build_contract_kind_must_match_artifact_kind() -> None:
    with pytest.raises(ValidationError):
        BuildContract(
            kind=ContractKind.DECK,
            artifact=ArtifactContract(kind=ContractKind.DOCUMENT),  # mismatch
            bootstrap=ToolPack(name="b"),
            edit=EditContract(),
            verify=VerificationContract(finalizer="ready_for_deck_verification"),
        )


def test_finalizer_must_follow_convention() -> None:
    for bad in ("finish", "verify_now", "ready_for_app", "app_verification", ""):
        with pytest.raises(ValidationError):
            VerificationContract(finalizer=bad)
    # valid forms accepted
    for good in (
        "ready_for_app_verification",
        "ready_for_static_site_verification",
        "ready_for_artifact_verification",
        "ready_for_workflow_output_verification",
    ):
        assert VerificationContract(finalizer=good).finalizer == good
