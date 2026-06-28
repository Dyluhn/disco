"""BuildContractRegistry (CONTRACT-2).

Declares the concrete BuildContract for each artifact kind — its required files,
starter kit, bootstrap/edit tool packs, verification finalizer, export pipeline,
prompt pack, and UI card — and looks one up by kind or from a BuildBrief. Both kernels
(DiscoKernel/PiKernel) resolve the same contract here, so a Build run has a declared,
host-owned contract before execution. Pure data + lookup; no runtime imports.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import (
    ArtifactContract,
    BuildContract,
    ContractKind,
    EditContract,
    ExportContract,
    ToolPack,
    VerificationContract,
    VerificationLevel,
)


def _static_site() -> BuildContract:
    return BuildContract(
        kind=ContractKind.STATIC_SITE,
        artifact=ArtifactContract(
            kind=ContractKind.STATIC_SITE, required_files=("index.html",), starter_kit="app_shell"
        ),
        bootstrap=ToolPack(name="static.site.bootstrap", tools=("file_write", "preview_start")),
        edit=EditContract(edit_tools=("file_edit", "file_replace_lines"), repair_tools=("file_write",)),
        verify=VerificationContract(finalizer="ready_for_static_site_verification"),
        export=ExportContract(name="static_standalone", pipeline=("preflight", "bundle", "validate", "deliver")),
        prompt_pack="build_static_site",
        ui_card="SiteCard",
    )


def _appkit_leadgen() -> BuildContract:
    return BuildContract(
        kind=ContractKind.APPKIT_LEADGEN,
        artifact=ArtifactContract(
            kind=ContractKind.APPKIT_LEADGEN,
            required_files=(".disco/appspec.json", "index.html"),
            starter_kit="lead_form",
        ),
        # P4/TOOL-1: the AppKit semantic mutation tools are now registered, so the
        # contract scopes the SEMANTIC tools (edit the AppSpec, not raw HTML). Raw
        # file_write is the repair-only escape hatch.
        bootstrap=ToolPack(name="appkit.leadgen.bootstrap", tools=("app_create",)),
        edit=EditContract(
            edit_tools=(
                "app_update_content",
                "app_add_section",
                "app_remove_section",
                "app_reorder_section",
                "app_set_design",
                "app_set_tweak",
                "app_snapshot_version",
            ),
            repair_tools=("file_write", "file_edit"),
        ),
        verify=VerificationContract(finalizer="ready_for_app_verification", level=VerificationLevel.STRICT),
        export=ExportContract(name="cloudflare_project", pipeline=("preflight", "bundle", "validate", "deliver")),
        prompt_pack="build_appkit_leadgen",
        ui_card="AppCard",
        skills=("appkit.leadgen", "cloudflare_export", "design_recipe"),
    )


def _deck() -> BuildContract:
    return BuildContract(
        kind=ContractKind.DECK,
        artifact=ArtifactContract(kind=ContractKind.DECK, required_files=("deck.json",), starter_kit="deck_stage"),
        bootstrap=ToolPack(name="deck.bootstrap", tools=("slides_generate",)),
        edit=EditContract(edit_tools=("deck_patch",)),
        verify=VerificationContract(finalizer="ready_for_deck_verification"),
        export=ExportContract(name="deck_export", pipeline=("bundle", "validate", "deliver")),
        prompt_pack="build_deck",
        ui_card="DeckCard",
    )


def _document() -> BuildContract:
    return BuildContract(
        kind=ContractKind.DOCUMENT,
        artifact=ArtifactContract(kind=ContractKind.DOCUMENT, required_files=("report.md",)),
        bootstrap=ToolPack(name="document.bootstrap", tools=("file_write",)),
        edit=EditContract(edit_tools=("file_edit", "file_replace_lines")),
        verify=VerificationContract(finalizer="ready_for_document_verification"),
        export=ExportContract(name="document_pdf", pipeline=("preflight", "bundle", "validate", "deliver")),
        prompt_pack="build_document",
        ui_card="DocumentCard",
    )


def _interactive_prototype() -> BuildContract:
    return BuildContract(
        kind=ContractKind.INTERACTIVE_PROTOTYPE,
        artifact=ArtifactContract(
            kind=ContractKind.INTERACTIVE_PROTOTYPE, required_files=("index.html",), starter_kit="app_shell"
        ),
        bootstrap=ToolPack(name="interactive.prototype.bootstrap", tools=("file_write", "preview_start")),
        edit=EditContract(edit_tools=("file_edit", "file_replace_lines")),
        verify=VerificationContract(finalizer="ready_for_prototype_verification"),
        prompt_pack="build_interactive_prototype",
        ui_card="PrototypeCard",
    )


def _workflow_output() -> BuildContract:
    return BuildContract(
        kind=ContractKind.WORKFLOW_OUTPUT,
        artifact=ArtifactContract(kind=ContractKind.WORKFLOW_OUTPUT),
        bootstrap=ToolPack(name="workflow.output.bootstrap"),
        edit=EditContract(),
        verify=VerificationContract(finalizer="ready_for_workflow_output_verification"),
        ui_card="WorkflowCard",
    )


def _custom() -> BuildContract:
    # the escape hatch: broad tools + rewrite allowed; the generic artifact finalizer.
    return BuildContract(
        kind=ContractKind.CUSTOM,
        artifact=ArtifactContract(kind=ContractKind.CUSTOM),
        bootstrap=ToolPack(name="custom.bootstrap", tools=("file_write", "file_edit", "shell")),
        edit=EditContract(edit_tools=("file_edit", "file_replace_lines"), repair_tools=("file_write",), rewrite_allowed=True),
        verify=VerificationContract(finalizer="ready_for_artifact_verification"),
        ui_card="ArtifactCard",
    )


_BUILTINS = (
    _static_site,
    _appkit_leadgen,
    _deck,
    _document,
    _interactive_prototype,
    _workflow_output,
    _custom,
)


class BuildContractRegistry:
    """Maps a ContractKind → its BuildContract."""

    def __init__(self) -> None:
        self._by_kind: dict[ContractKind, BuildContract] = {}

    def register(self, contract: BuildContract) -> None:
        self._by_kind[contract.kind] = contract

    def get(self, kind: ContractKind) -> BuildContract | None:
        return self._by_kind.get(kind)

    def kinds(self) -> frozenset[ContractKind]:
        return frozenset(self._by_kind)

    def _custom(self) -> BuildContract:
        """The CUSTOM contract — registered one, else a deterministic minimal fallback
        (so a partial registry never KeyErrors). A run always gets a contract."""
        return self._by_kind.get(ContractKind.CUSTOM) or BuildContract.minimal(ContractKind.CUSTOM)

    def get_for_brief(
        self, brief: Mapping[str, Any] | None, *, strict_kind: bool = True
    ) -> BuildContract:
        """Resolve the contract for a BuildBrief from ``brief['kind']``.

        A MISSING kind (no 'kind' key) → the CUSTOM contract (the legitimate default: a
        brief that declares no kind is a custom build). A PRESENT-but-unknown/malformed
        kind (a typo, wrong type) is a bad input: with ``strict_kind=True`` (default) it
        raises ValueError rather than silently broadening to CUSTOM; ``strict_kind=False``
        opts into the compatibility fallback. Never returns None / KeyErrors on a partial
        registry."""
        raw = (brief or {}).get("kind")
        if raw is None:
            return self._custom()
        try:
            kind = ContractKind(raw)
        except (ValueError, TypeError) as exc:
            if strict_kind:
                raise ValueError(f"BuildBrief declares an unknown contract kind: {raw!r}") from exc
            return self._custom()
        return self._by_kind.get(kind) or self._custom()

    @classmethod
    def default(cls) -> BuildContractRegistry:
        """A registry with every built-in contract registered."""
        reg = cls()
        for factory in _BUILTINS:
            reg.register(factory())
        return reg
