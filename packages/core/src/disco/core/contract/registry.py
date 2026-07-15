"""BuildContractRegistry (CONTRACT-2).

Declares the concrete BuildContract for each artifact kind — its required files,
starter kit, bootstrap/edit tool packs, verification finalizer, export pipeline,
prompt pack, and UI card — and looks one up by kind or from a BuildBrief. Build runs
resolve the same host-owned contract before execution. Pure data + lookup; no runtime
imports.
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

# CONTRACT-ACTIVATE harvest (2026-07-10, harness/build_soak/harvest_scopes.py over
# 35 live soak dossiers): the original 3-tool packs would have DENIED the bulk of
# real site-building work — shell 143×/30 runs, file_write 42×/18, browser 19×/12,
# file_insert_lines, file_append, shell_exec, code_exec, run_project_script,
# context_memory, delegate_explore. For raw-file kinds the file tools ARE the
# medium (there is no semantic-tool layer to protect, unlike AppKit), so the packs
# carry the full evidenced working set; the contract's enforcement value here is
# the read-only VERIFY phase + cross-kind hygiene (deck/doc/app_* tools stay out).
# codex follow-ups: file_edit in BOOTSTRAP (an artifact-mode "targeted change to
# an uploaded file" edits before any authoring — CUSTOM always allowed this);
# scaffold_starter in EDIT + REPAIR (any bootstrap-pack success — even a
# context_memory list — advances the phase, and the starter must not be
# stranded behind that flip; the tool is no-clobber, so late calls are safe).
_SITE_BOOTSTRAP_TOOLS = (
    "scaffold_starter",
    "file_write",
    "file_edit",
    "preview_start",
    "shell",
    "context_memory",
    "image_generate",
)
_SITE_EDIT_TOOLS = (
    "file_edit",
    "file_replace_lines",
    "file_insert_lines",
    "file_append",
    "file_write",
    "shell",
    "shell_exec",
    "shell_kill_process",
    "browser",
    "code_exec",
    "run_project_script",
    "context_memory",
    "image_generate",
    "delegate_explore",
    "scaffold_starter",
)
_SITE_REPAIR_TOOLS = (
    "file_write",
    "file_edit",
    "file_replace_lines",
    "file_insert_lines",
    "file_append",
    "shell",
    "shell_exec",
    "shell_kill_process",
    "browser",
    "context_memory",
    "scaffold_starter",
)


def _static_site() -> BuildContract:
    return BuildContract(
        kind=ContractKind.STATIC_SITE,
        artifact=ArtifactContract(
            kind=ContractKind.STATIC_SITE, required_files=("index.html",), starter_kit="app_shell"
        ),
        # P7: scaffold_starter materializes the app_shell starter; file_write builds it out.
        bootstrap=ToolPack(name="static.site.bootstrap", tools=_SITE_BOOTSTRAP_TOOLS),
        edit=EditContract(edit_tools=_SITE_EDIT_TOOLS, repair_tools=_SITE_REPAIR_TOOLS),
        verify=VerificationContract(finalizer="ready_for_static_site_verification"),
        export=ExportContract(
            name="static_standalone", pipeline=("preflight", "bundle", "validate", "deliver")
        ),
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
                # remove/reorder were legacy v1-spec extensions; the v2 engine
                # (hard-replace, fix-2) mutates structure via app_add_section +
                # app_update_content + regeneration — no remove/reorder surface.
                "app_set_design",
                "app_set_tweak",
                "app_snapshot_version",
            ),
            repair_tools=("file_write", "file_edit"),
        ),
        verify=VerificationContract(
            finalizer="ready_for_app_verification", level=VerificationLevel.STRICT
        ),
        export=ExportContract(
            name="cloudflare_project", pipeline=("preflight", "bundle", "validate", "deliver")
        ),
        prompt_pack="build_appkit_leadgen",
        ui_card="AppCard",
        skills=("appkit.leadgen", "cloudflare_export", "design_recipe"),
    )


def _deck() -> BuildContract:
    return BuildContract(
        kind=ContractKind.DECK,
        # P7: the deck's authored source is the AuthoredDeck sidecar `deck.authored.json`
        # (what slides_generate/deck_patch actually write/edit — NOT a hand-written
        # deck.json). slides_generate IS the deck materializer, so there is no file-map
        # starter_kit (it would be paper); the deck pack tells the model to use it.
        artifact=ArtifactContract(kind=ContractKind.DECK, required_files=("deck.authored.json",)),
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
        artifact=ArtifactContract(
            kind=ContractKind.DOCUMENT,
            required_files=("report.md", "report.pdf"),
        ),
        bootstrap=ToolPack(name="document.bootstrap", tools=("doc_set_section",)),
        edit=EditContract(
            edit_tools=("doc_set_section",),
            repair_tools=("doc_set_section", "doc_export"),
        ),
        verify=VerificationContract(finalizer="ready_for_document_verification"),
        export=ExportContract(
            name="document_pdf",
            pipeline=("preflight", "bundle", "validate", "deliver"),
            tools=("doc_export",),
        ),
        prompt_pack="build_document",
        ui_card="DocumentCard",
    )


def _interactive_prototype() -> BuildContract:
    return BuildContract(
        kind=ContractKind.INTERACTIVE_PROTOTYPE,
        artifact=ArtifactContract(
            kind=ContractKind.INTERACTIVE_PROTOTYPE,
            required_files=("index.html",),
            starter_kit="app_shell",
        ),
        # Raw-file kind: same evidenced working set as static.site (see the
        # harvest note above) — prototypes/games are shell/browser-heavy too.
        bootstrap=ToolPack(name="interactive.prototype.bootstrap", tools=_SITE_BOOTSTRAP_TOOLS),
        edit=EditContract(edit_tools=_SITE_EDIT_TOOLS, repair_tools=_SITE_REPAIR_TOOLS),
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
    # The escape hatch: broad tools + rewrite allowed; the generic artifact
    # finalizer. CUSTOM is what every UNMAPPED build runs through, so its packs
    # must be the BROADEST — the first live audited wave (2026-07-10) showed its
    # old 2-tool edit pack would-denying 24 shell calls in one ordinary run.
    return BuildContract(
        kind=ContractKind.CUSTOM,
        artifact=ArtifactContract(kind=ContractKind.CUSTOM),
        bootstrap=ToolPack(name="custom.bootstrap", tools=_SITE_BOOTSTRAP_TOOLS),
        edit=EditContract(
            edit_tools=_SITE_EDIT_TOOLS, repair_tools=_SITE_REPAIR_TOOLS, rewrite_allowed=True
        ),
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

# CONTRACT-ACTIVATE (2026-07-10): BuildBrief.app_kind → ContractKind. The brief
# classifier's vocabulary (core.appkit.build_brief._APP_KINDS — plain strings, no
# import needed here) maps CONSERVATIVELY: only kinds the browser-artifact
# contracts genuinely fit. api/cli/data_tool/mobile_app and "unknown" stay
# UNMAPPED → the CUSTOM escape hatch, exactly as before activation — a wrong
# contract (its required_files + finalizer) is worse than no contract.
# NARROWED further after codex review (2026-07-10): app_kind is MEDIUM-BLIND —
# "text-based terminal game in Python" classifies `game`, "PowerPoint deck about
# ecommerce trends" classifies `ecommerce` — and a wrongly-mapped browser
# contract's required_files can mis-gate finish (index.html becomes a
# dictated-content candidate). Only kinds whose CLASSIFIER KEYWORDS are
# intrinsically web-medium stay mapped: landing_page/blog ("landing page",
# "portfolio", "blog", "content site") and web_app ("web app", "website",
# "saas", "page"). game/dashboard/chat_app/ecommerce → CUSTOM: they lose the
# starter RECOMMENDATION but keep full catalog access via explicit `kind`.
APP_KIND_TO_CONTRACT: dict[str, ContractKind] = {
    "landing_page": ContractKind.STATIC_SITE,
    "blog": ContractKind.STATIC_SITE,
    "web_app": ContractKind.INTERACTIVE_PROTOTYPE,
}


def contract_kind_for_app_kind(app_kind: str | None) -> ContractKind | None:
    """The declared contract kind for a classified BuildBrief.app_kind, or None
    when the kind has no confident contract mapping (→ stay CUSTOM)."""
    if not app_kind:
        return None
    return APP_KIND_TO_CONTRACT.get(app_kind.strip().lower())


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
