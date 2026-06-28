"""P5-DELIVERY tests: contract delivery shape (app|files) + the no-wrong-shape validator."""

from __future__ import annotations

from disco.core.contract import (
    BuildContractRegistry,
    ContractKind,
    delivery_mode_for_kind,
    deliverable_kind_matches_contract,
)

_APP_KINDS = {ContractKind.APPKIT_LEADGEN, ContractKind.STATIC_SITE, ContractKind.INTERACTIVE_PROTOTYPE}


def test_delivery_mode_for_every_kind() -> None:
    for kind in ContractKind:
        mode = delivery_mode_for_kind(kind)
        assert mode in ("app", "files")
        assert mode == ("app" if kind in _APP_KINDS else "files"), kind


def test_custom_defaults_to_files() -> None:
    assert delivery_mode_for_kind(ContractKind.CUSTOM) == "files"


def test_artifact_contract_property_matches_kind() -> None:
    reg = BuildContractRegistry.default()
    for kind in ContractKind:
        c = reg.get(kind)
        assert c is not None
        assert c.artifact.delivery_mode == delivery_mode_for_kind(kind)


def test_validator_accepts_matching_shape() -> None:
    reg = BuildContractRegistry.default()
    appkit = reg.get(ContractKind.APPKIT_LEADGEN)
    deck = reg.get(ContractKind.DECK)
    assert appkit is not None and deck is not None
    assert deliverable_kind_matches_contract(appkit, "app") is True
    assert deliverable_kind_matches_contract(deck, "files") is True


def test_validator_rejects_wrong_shape() -> None:
    reg = BuildContractRegistry.default()
    deck = reg.get(ContractKind.DECK)
    appkit = reg.get(ContractKind.APPKIT_LEADGEN)
    assert deck is not None and appkit is not None
    # a deck handed off as a runnable "app" is a wrong-shape handoff
    assert deliverable_kind_matches_contract(deck, "app") is False
    # an appkit app dumped as raw "files" is wrong-shape too
    assert deliverable_kind_matches_contract(appkit, "files") is False


def test_app_kinds_open_in_preview_files_kinds_download() -> None:
    reg = BuildContractRegistry.default()
    assert reg.get(ContractKind.STATIC_SITE).artifact.delivery_mode == "app"  # type: ignore[union-attr]
    assert reg.get(ContractKind.INTERACTIVE_PROTOTYPE).artifact.delivery_mode == "app"  # type: ignore[union-attr]
    assert reg.get(ContractKind.DOCUMENT).artifact.delivery_mode == "files"  # type: ignore[union-attr]
    assert reg.get(ContractKind.WORKFLOW_OUTPUT).artifact.delivery_mode == "files"  # type: ignore[union-attr]
