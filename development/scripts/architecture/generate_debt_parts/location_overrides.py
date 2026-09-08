"""AST location overrides for debt rows whose anchors moved after the freeze.

Lifted verbatim out of ``generate_debt.py`` (lines 254-369 at ``8242a597``).
``CURRENT_LOCATION_OVERRIDES`` continues to supply the actively-maintained
anchors; the literals below are the historical distributed/aggregate entries.
"""

from __future__ import annotations

try:
    from ..debt_location_overrides import CURRENT_LOCATION_OVERRIDES
except ImportError:
    from debt_location_overrides import CURRENT_LOCATION_OVERRIDES

LOCATION_OVERRIDES = {
    **CURRENT_LOCATION_OVERRIDES,
    "DM-001": (
        "current/packages/agent-server/src/disco/agent_server/runtime.py:ConversationRuntime:810-5369"
    ),
    # The citation helpers were consolidated into the writer before this
    # campaign; keep the active observation anchored to the files that now own
    # the duplicated grammar/coupling concern.
    "DM-008": (
        "current/packages/retrieval/src/disco/retrieval/streaming.py + "
        "current/packages/retrieval/src/disco/retrieval/deep_research/writer.py"
    ),
    "DM-012": (
        "current/packages/agent-server/src/disco/agent_server/preview_service.py:"
        "_sealed_runtime_contract:194-213 + "
        "development/harness/build_soak/adapters/_client_collection.py:"
        "_CollectionMixin.collect_browser_evidence:409-491"
    ),
    # Re-anchored 2026-08-02 (Epic 12-A). The TS anchors moved when Amendment
    # A3's contract-parity gate landed: three event interfaces were added ahead
    # of the AgentEvent union, and WSServerFrame became an alias over
    # WSWireServerFrame | WSClientSynthesizedFrame. Location only — DM-017 is
    # resolved by PKG-12-FE-SHELL. Its concern ("three event kinds and token
    # frame absent from TS; connection is UI-local") is discharged in substance
    # and enforced fail-closed by
    # development/tests/architecture/test_frontend_contract_parity.py, but resolving the
    # observation belongs to its named owner, not to this gate boundary.
    "DM-017": (
        "current/packages/core/src/disco/core/events.py:Event:187-217 + "
        "current/packages/core/src/disco/core/wire.py:WSServerFrame:39-65 + "
        "current/frontend/src/types/agent.ts:AgentEvent/WSServerFrame:598-700"
    ),
    "PY-0189": (
        "current/packages/agent-server/src/disco/agent_server/build_kernel/disco_kernel.py:"
        "DiscoKernel:46-190"
    ),
    "PY-0192": (
        "current/packages/agent-server/src/disco/agent_server/deep_research_service.py:"
        "DeepResearchService:66-1083"
    ),
    "PY-0194": (
        "current/packages/agent-server/src/disco/agent_server/deep_research_service.py:"
        "_maybe_run_deep_research:350-435"
    ),
    "PY-0195": (
        "current/packages/agent-server/src/disco/agent_server/deep_research_service.py:"
        "_propose_deep_research_plan:437-590"
    ),
    "PY-0196": (
        "current/packages/agent-server/src/disco/agent_server/deep_research_service.py:"
        "_propose_deep_research_plan:437-590"
    ),
    "PY-0197": (
        "current/packages/agent-server/src/disco/agent_server/deep_research_service.py:"
        "_execute_deep_research:592-860"
    ),
    "PY-0198": (
        "current/packages/agent-server/src/disco/agent_server/deep_research_service.py:"
        "_execute_deep_research:592-860"
    ),
    "PY-0199": (
        "current/packages/agent-server/src/disco/agent_server/deep_research_service.py:"
        "_follow_up_deep_research:878-1055"
    ),
    "PY-0200": (
        "current/packages/agent-server/src/disco/agent_server/deep_research_service.py:<module>:1-1083"
    ),
    "PY-0305": (
        "current/packages/agent-server/src/disco/agent_server/runtime.py:_sync_appkit_live_preview:512-595"
    ),
    "PY-0306": (
        "current/packages/agent-server/src/disco/agent_server/runtime.py:_probe_live_model:651-747"
    ),
    "PY-0307": (
        "current/packages/agent-server/src/disco/agent_server/runtime.py:ConversationRuntime:810-5369"
    ),
    "PY-0308": (
        "current/packages/agent-server/src/disco/agent_server/runtime.py:ConversationRuntime:810-5369"
    ),
    "PY-0309": ("current/packages/agent-server/src/disco/agent_server/runtime.py:__init__:832-1166"),
    "PY-0310": ("current/packages/agent-server/src/disco/agent_server/runtime.py:__init__:832-1166"),
    "PY-0311": ("current/packages/agent-server/src/disco/agent_server/runtime.py:_surface_of:1318-1389"),
    "PY-0312": (
        "current/packages/agent-server/src/disco/agent_server/runtime.py:_resolve_driver_context:1433-1505"
    ),
    "PY-0313": (
        "current/packages/agent-server/src/disco/agent_server/runtime.py:_compose_build_loop:2231-2676"
    ),
    "PY-0314": (
        "current/packages/agent-server/src/disco/agent_server/runtime.py:_compose_build_loop:2231-2676"
    ),
    "PY-0315": (
        "current/packages/agent-server/src/disco/agent_server/runtime.py:"
        "_maybe_auto_resume_actionless_pause:3047-3113"
    ),
    "PY-0316": (
        "current/packages/agent-server/src/disco/agent_server/runtime.py:_finalize_clean_return:3143-3281"
    ),
    "PY-0317": (
        "current/packages/agent-server/src/disco/agent_server/runtime.py:_finalize_clean_return:3143-3281"
    ),
    "PY-0318": (
        "current/packages/agent-server/src/disco/agent_server/runtime.py:_preflight_driver:3448-3570"
    ),
    "PY-0319": (
        "current/packages/agent-server/src/disco/agent_server/runtime.py:"
        "reconcile_sandbox_backend:3890-3932"
    ),
    "PY-0320": (
        "current/packages/agent-server/src/disco/agent_server/runtime.py:_run_with_persistence:3934-4066"
    ),
    "PY-0321": (
        "current/packages/agent-server/src/disco/agent_server/runtime.py:"
        "_sealed_run_conversation_setup:4771-4958"
    ),
    "PY-0322": (
        "current/packages/agent-server/src/disco/agent_server/runtime.py:_sealed_run_execute:4960-5182"
    ),
    "PY-0323": (
        "current/packages/agent-server/src/disco/agent_server/runtime.py:_sealed_run_execute:4960-5182"
    ),
    "PY-0324": ("current/packages/agent-server/src/disco/agent_server/runtime.py:<module>:1-5369"),
    "PY-0357": (
        "current/packages/agent-server/src/disco/agent_server/workspace_service.py:"
        "WorkspaceCoordinator:125-815"
    ),
    "PY-0358": (
        "current/packages/agent-server/src/disco/agent_server/workspace_service.py:"
        "WorkspaceCoordinator:125-815"
    ),
    "PY-0359": ("current/packages/agent-server/src/disco/agent_server/workspace_service.py:<module>:1-815"),
}
