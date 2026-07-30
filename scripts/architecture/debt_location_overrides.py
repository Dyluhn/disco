"""Current AST anchors for active debt rows shifted by accepted extractions."""

CURRENT_LOCATION_OVERRIDES = {
    "PY-0187": (
        "packages/agent-server/src/disco/agent_server/build_contract_service.py:"
        "_fold_contract_from_history:221-315"
    ),
    "PY-0202": (
        "packages/agent-server/src/disco/agent_server/host_proxy.py:"
        "_rewrite_canonical_upstream_location:219-266"
    ),
    "PY-0203": (
        "packages/agent-server/src/disco/agent_server/host_proxy.py:"
        "HostPreviewProxyMiddleware:363-1081"
    ),
    "PY-0204": (
        "packages/agent-server/src/disco/agent_server/host_proxy.py:"
        "_forward_http_response:1220-1355"
    ),
    "PY-0205": (
        "packages/agent-server/src/disco/agent_server/host_proxy.py:"
        "_forward_http_response:1220-1355"
    ),
    "PY-0206": (
        "packages/agent-server/src/disco/agent_server/host_proxy.py:_proxy_http:1358-1483"
    ),
    "PY-0207": (
        "packages/agent-server/src/disco/agent_server/host_proxy.py:_proxy_http:1358-1483"
    ),
    "PY-0208": (
        "packages/agent-server/src/disco/agent_server/host_proxy.py:"
        "_proxy_websocket:1486-1569"
    ),
    "PY-0209": (
        "packages/agent-server/src/disco/agent_server/host_proxy.py:__call__:393-821"
    ),
    "PY-0210": (
        "packages/agent-server/src/disco/agent_server/host_proxy.py:__call__:393-821"
    ),
    "PY-0211": (
        "packages/agent-server/src/disco/agent_server/host_proxy.py:"
        "_handle_preview_bootstrap:823-1001"
    ),
    "PY-0212": (
        "packages/agent-server/src/disco/agent_server/host_proxy.py:"
        "_handle_preview_bootstrap:823-1001"
    ),
    "PY-0213": (
        "packages/agent-server/src/disco/agent_server/host_proxy.py:<module>:1-1592"
    ),
    "PY-0222": (
        "packages/agent-server/src/disco/agent_server/mcp_manager.py:McpManager:143-773"
    ),
    "PY-0223": (
        "packages/agent-server/src/disco/agent_server/mcp_manager.py:"
        "_start_mcp_pool:320-461"
    ),
    "PY-0224": (
        "packages/agent-server/src/disco/agent_server/mcp_manager.py:"
        "_start_mcp_pool:320-461"
    ),
    "PY-0233": (
        "packages/agent-server/src/disco/agent_server/preview_service.py:"
        "PreviewService:48-816"
    ),
    "PY-0234": (
        "packages/agent-server/src/disco/agent_server/preview_service.py:"
        "_prepare_sealed_node_dependencies:313-380"
    ),
    "PY-0235": (
        "packages/agent-server/src/disco/agent_server/preview_service.py:"
        "_replace_with_sealed_workspace:383-483"
    ),
    "PY-0236": (
        "packages/agent-server/src/disco/agent_server/preview_service.py:preview:556-661"
    ),
    "PY-0237": (
        "packages/agent-server/src/disco/agent_server/preview_service.py:"
        "ensure_preview:663-816"
    ),
    "PY-0238": (
        "packages/agent-server/src/disco/agent_server/preview_service.py:"
        "ensure_preview:663-816"
    ),
    "PY-0251": (
        "packages/agent-server/src/disco/agent_server/routes/deck_editor.py:"
        "_patch_deck_response:465-575"
    ),
    "PY-0252": (
        "packages/agent-server/src/disco/agent_server/routes/files.py:"
        "_register_upload_routes:89-220"
    ),
    "PY-0253": (
        "packages/agent-server/src/disco/agent_server/routes/files.py:upload_files:95-220"
    ),
    "PY-0254": (
        "packages/agent-server/src/disco/agent_server/routes/files.py:upload_files:95-220"
    ),
    "PY-0255": (
        "packages/agent-server/src/disco/agent_server/routes/files.py:upload_files:95-220"
    ),
    "PY-0256": (
        "packages/agent-server/src/disco/agent_server/routes/files.py:"
        "artifact_file:229-308"
    ),
    "PY-0262": (
        "packages/agent-server/src/disco/agent_server/routes/preview.py:"
        "_canonical_preview_authority:796-892"
    ),
    "PY-0263": (
        "packages/agent-server/src/disco/agent_server/routes/preview.py:"
        "_preview_capability_response:1080-1203"
    ),
    "PY-0264": (
        "packages/agent-server/src/disco/agent_server/routes/preview.py:"
        "_preview_capability_response:1080-1203"
    ),
    "PY-0265": (
        "packages/agent-server/src/disco/agent_server/routes/preview.py:"
        "_register_live_browser_start_route:1314-1432"
    ),
    "PY-0266": (
        "packages/agent-server/src/disco/agent_server/routes/preview.py:"
        "_register_preview_app_websocket_routes:1815-1945"
    ),
    "PY-0267": (
        "packages/agent-server/src/disco/agent_server/routes/preview.py:"
        "path_preview_bootstrap:1222-1311"
    ),
    "PY-0268": (
        "packages/agent-server/src/disco/agent_server/routes/preview.py:"
        "path_preview_bootstrap:1222-1311"
    ),
    "PY-0269": (
        "packages/agent-server/src/disco/agent_server/routes/preview.py:"
        "browser_live_url:1318-1432"
    ),
    "PY-0270": (
        "packages/agent-server/src/disco/agent_server/routes/preview.py:"
        "browser_live_url:1318-1432"
    ),
    "PY-0271": (
        "packages/agent-server/src/disco/agent_server/routes/preview.py:"
        "preview_app_websocket:1824-1945"
    ),
    "PY-0272": (
        "packages/agent-server/src/disco/agent_server/routes/preview.py:"
        "preview_app_websocket:1824-1945"
    ),
    "PY-0273": (
        "packages/agent-server/src/disco/agent_server/routes/preview.py:"
        "preview_app_websocket:1824-1945"
    ),
    "PY-0274": (
        "packages/agent-server/src/disco/agent_server/routes/preview.py:<module>:1-1957"
    ),
    "PY-0283": (
        "packages/agent-server/src/disco/agent_server/routes/projects.py:"
        "_handle_download_project:544-637"
    ),
    "PY-0284": (
        "packages/agent-server/src/disco/agent_server/routes/projects.py:"
        "_handle_project_manifest:640-726"
    ),
    "PY-0285": (
        "packages/agent-server/src/disco/agent_server/routes/projects.py:"
        "_zip_workspace_with_overlay:811-875"
    ),
    "PY-0291": (
        "packages/agent-server/src/disco/agent_server/routes/schedules.py:"
        "make_schedules_router:15-168"
    ),
    "PY-0292": (
        "packages/agent-server/src/disco/agent_server/routes/share.py:"
        "make_share_router:15-156"
    ),
    "PY-0293": (
        "packages/agent-server/src/disco/agent_server/routes/spaces.py:"
        "make_spaces_router:29-141"
    ),
    "PY-0325": (
        "packages/agent-server/src/disco/agent_server/runtime_model_probe.py:"
        "_do_live_model_probe:30-90"
    ),
    "PY-0326": (
        "packages/agent-server/src/disco/agent_server/runtime_model_probe.py:"
        "_models_context_length:93-151"
    ),
    "PY-0327": (
        "packages/agent-server/src/disco/agent_server/runtime_settings.py:"
        "RuntimeSettings:301-845"
    ),
    "PY-0328": (
        "packages/agent-server/src/disco/agent_server/runtime_settings.py:"
        "RuntimeSettings:301-845"
    ),
    "PY-0347": (
        "packages/agent-server/src/disco/agent_server/workspace_persistence.py:"
        "WorkspacePersistence:291-1256"
    ),
    "PY-0348": (
        "packages/agent-server/src/disco/agent_server/workspace_persistence.py:"
        "_commit_finished_workspace_locked:521-711"
    ),
    "PY-0349": (
        "packages/agent-server/src/disco/agent_server/workspace_persistence.py:"
        "_commit_finished_workspace_locked:521-711"
    ),
    "PY-0350": (
        "packages/agent-server/src/disco/agent_server/workspace_persistence.py:"
        "_do_recover_finalization_journals:724-867"
    ),
    "PY-0351": (
        "packages/agent-server/src/disco/agent_server/workspace_persistence.py:"
        "_do_recover_finalization_journals:724-867"
    ),
    "PY-0352": (
        "packages/agent-server/src/disco/agent_server/workspace_persistence.py:"
        "_do_capture_workspace:888-1045"
    ),
    "PY-0353": (
        "packages/agent-server/src/disco/agent_server/workspace_persistence.py:"
        "_do_capture_workspace:888-1045"
    ),
    "PY-0354": (
        "packages/agent-server/src/disco/agent_server/workspace_persistence.py:"
        "_do_maybe_synthesize_app_deliverable:1114-1179"
    ),
    "PY-0355": (
        "packages/agent-server/src/disco/agent_server/workspace_persistence.py:"
        "_trusted_verified_app_entry:1182-1237"
    ),
    "PY-0356": (
        "packages/agent-server/src/disco/agent_server/workspace_persistence.py:"
        "<module>:1-1256"
    ),
    "PY-0357": (
        "packages/agent-server/src/disco/agent_server/workspace_service.py:"
        "WorkspaceCoordinator:125-815"
    ),
    "PY-0358": (
        "packages/agent-server/src/disco/agent_server/workspace_service.py:"
        "WorkspaceCoordinator:125-815"
    ),
    "PY-0359": (
        "packages/agent-server/src/disco/agent_server/workspace_service.py:<module>:1-815"
    ),
}
