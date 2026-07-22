from __future__ import annotations

from disco.core.build_platform import (
    APPKIT_ENGINE_ID,
    APPKIT_LIFECYCLE_OPERATIONS,
    APPKIT_PROFILE_ID,
    FREEFORM_ENGINE_ID,
    ConstructionEngine,
    PolicyDecision,
    ToolDescriptor,
    build_builtin_registry,
    resolve_builtin_composition,
)


def test_appkit_has_a_distinct_strict_engine_and_governed_lifecycle() -> None:
    tools = frozenset(
        {
            "file_read",
            "submit_plan",
            "app_create",
            "app_update_content",
            "verify_appkit_app",
        }
    )
    registry = build_builtin_registry(appkit_tools=tools)
    appkit_engine = registry.engine(APPKIT_ENGINE_ID)
    freeform_engine = registry.engine(FREEFORM_ENGINE_ID)
    assert isinstance(appkit_engine, ConstructionEngine)
    assert isinstance(freeform_engine, ConstructionEngine)
    assert type(appkit_engine) is not type(freeform_engine)

    composition = resolve_builtin_composition(
        appkit=True,
        goal="create a governed application",
        tool_catalog=tuple(
            ToolDescriptor(name=name, description=f"strict {name}") for name in sorted(tools)
        ),
        visible_tools=tools,
    )

    assert composition.profile.id == APPKIT_PROFILE_ID
    assert composition.blocked_operations == ()
    assert tuple(intent.operation for intent in composition.construction.intents) == (
        APPKIT_LIFECYCLE_OPERATIONS
    )
    assert composition.construction.requested_tools == tools
    assert frozenset(tool.name for tool in composition.prompt_context.visible_tools) == tools

    rules = {rule.key: rule.decision for rule in composition.effective_policy.rules}
    assert rules["mutation.raw_files"] is PolicyDecision.DENY
    assert rules["mutation.semantic"] is PolicyDecision.ALLOW


def test_appkit_engine_describes_governance_without_effect_authority() -> None:
    composition = resolve_builtin_composition(
        appkit=True,
        goal="preserve auth, roles, persistence, and mediated delivery",
        tool_catalog=(ToolDescriptor(name="file_read", description="read"),),
        visible_tools=frozenset({"file_read"}),
    )
    operations = tuple(intent.operation for intent in composition.construction.intents)
    assert "appkit.govern_session_auth" in operations
    assert "appkit.govern_rbac" in operations
    assert "appkit.govern_persistence" in operations
    assert "host.deploy_mediated" in operations
    assert "host.verify_appkit_strict" in operations

    serialized = composition.model_dump_json().casefold()
    for forbidden_authority in (
        "sandbox",
        "event_store",
        "secret_value",
        "subprocess",
        "publish_success",
        "verifier_verdict",
    ):
        assert forbidden_authority not in serialized
