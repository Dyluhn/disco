"""Wave 2 workflow readiness, invocation value, and restart contracts."""

from __future__ import annotations

from disco.core.workflow import (
    McpMount,
    PinnedWorkflowInvocationState,
    WorkflowApproval,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowOutputContract,
    WorkflowPolicies,
    WorkflowValidationFinding,
    WorkflowVerify,
    evaluate_workflow_readiness,
    project_pinned_workflow_state,
    validate_workflow_params,
    workflow_run_brief,
    workflow_surface_digest,
    workflow_tool_descriptor,
)


def _definition(*, required: tuple[str, ...] = ("query",)) -> WorkflowDefinition:
    return WorkflowDefinition(
        name="Research Summary",
        card="Summarize the requested query into one bounded report.",
        params_model_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string"},
                "options": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"limit": {"type": "integer", "minimum": 1}},
                    "required": ["limit"],
                },
            },
            "required": list(required),
        },
        tools=("file_read",),
        policies=WorkflowPolicies(),
        output_contract=WorkflowOutputContract(
            path_template="reports/{query}.md", format="markdown"
        ),
        verify=WorkflowVerify(checks=("file_exists",)),
    )


def _instance(*, enabled: bool = True, approval_digest: str = "sha256:surface") -> WorkflowInstance:
    definition = _definition()
    return WorkflowInstance(
        owner_id="owner-a",
        definition_digest=definition.digest(),
        definition=definition,
        params={},
        enabled=enabled,
        approval=WorkflowApproval(
            approved_at="2026-08-25T00:00:00Z",
            approved_by="owner-a",
            surface_shown_digest=approval_digest,
        ),
    )


def test_readiness_is_one_fail_closed_predicate() -> None:
    instance = _instance()
    ready = evaluate_workflow_readiness(
        instance,
        owner_id="owner-a",
        current_surface_digest="sha256:surface",
        available_mcp_tool_names=frozenset(),
    )
    assert ready.ready is True
    assert ready.compiled_scope is not None

    for kwargs, reason in (
        ({"owner_id": "other"}, "owner_mismatch"),
        ({"current_surface_digest": "sha256:new"}, "surface_digest_stale"),
    ):
        values = {
            "owner_id": "owner-a",
            "current_surface_digest": "sha256:surface",
            "available_mcp_tool_names": frozenset(),
            **kwargs,
        }
        verdict = evaluate_workflow_readiness(instance, **values)
        assert verdict.ready is False
        assert reason in verdict.reasons

    disabled = _instance(enabled=False)
    assert (
        "disabled"
        in evaluate_workflow_readiness(
            disabled,
            owner_id="owner-a",
            current_surface_digest="sha256:surface",
            available_mcp_tool_names=frozenset(),
        ).reasons
    )


def test_browser_workflow_without_egress_is_not_ready() -> None:
    definition = _definition().model_copy(
        update={"tools": ("browser",), "policies": WorkflowPolicies()}
    )
    instance = WorkflowInstance(
        owner_id="owner-a",
        definition_digest=definition.digest(),
        definition=definition,
        params={},
        enabled=True,
        approval=WorkflowApproval(
            approved_at="2026-08-25T00:00:00Z",
            approved_by="owner-a",
            surface_shown_digest="sha256:surface",
        ),
    )
    readiness = evaluate_workflow_readiness(
        instance,
        owner_id="owner-a",
        current_surface_digest="sha256:surface",
        available_mcp_tool_names=frozenset(),
    )
    assert readiness.ready is False
    assert "browser_without_egress" in readiness.blocking_findings


def test_connector_bindings_are_one_readiness_gate() -> None:
    instance = _instance().model_copy(update={"connector_bindings": {"github": "conn-1"}})
    ready = evaluate_workflow_readiness(
        instance,
        owner_id="owner-a",
        current_surface_digest="sha256:surface",
        available_mcp_tool_names=frozenset(),
        available_connector_bindings=frozenset({"conn-1"}),
    )
    assert ready.ready is True

    blocked = evaluate_workflow_readiness(
        instance,
        owner_id="owner-a",
        current_surface_digest="sha256:surface",
        available_mcp_tool_names=frozenset(),
        available_connector_bindings=frozenset(),
    )
    assert blocked.ready is False
    assert blocked.status == "needs_setup"
    assert "connector_bindings_unavailable" in blocked.blocking_findings


def test_descriptor_name_and_schema_are_stable_and_exact() -> None:
    instance = _instance()
    first = workflow_tool_descriptor("wf_research_1234", instance)
    second = workflow_tool_descriptor("wf_research_1234", instance)
    assert first == second
    assert first.name.startswith("workflow__research-summary__")
    assert first.name.endswith("d40d356f7f191e704ddaab06")
    assert first.description == instance.definition.card
    assert first.parameters_schema is not instance.definition.params_model_schema
    assert first.parameters_schema == instance.definition.params_model_schema


def test_parameter_validation_is_exact_and_field_level() -> None:
    schema = _definition().params_model_schema
    invalid = validate_workflow_params(
        schema,
        {"query": 3, "options": {}, "extra": True},
    )
    assert invalid.valid is False
    assert {issue.field for issue in invalid.issues} >= {"query", "options.limit", "extra"}
    assert validate_workflow_params(schema, {"query": "weather", "options": {"limit": 2}}).valid


def test_brief_and_pinned_state_round_trip_needs_input_and_completion() -> None:
    instance = _instance()
    params = {"query": "weather", "options": {"limit": 2}}
    brief = workflow_run_brief(instance.definition, params)
    assert instance.definition.card in brief.render()
    assert '"query": "weather"' in brief.render()
    rendered = brief.render()
    assert "<workflow_card_instructions>" in rendered
    assert "<parameters_data_json>" in rendered
    assert "<workflow_card_data>" not in rendered
    state = PinnedWorkflowInvocationState.create(
        instance_id="wf_research",
        instance=instance,
        params=params,
        surface_digest="sha256:surface",
        run_id="run-1",
    ).model_copy(update={"status": "needs_input"})
    restored = PinnedWorkflowInvocationState.model_validate(state.model_dump(mode="json"))
    projection = project_pinned_workflow_state(restored, available_mcp_tool_names=frozenset())
    assert projection.sealed_active is True
    assert projection.general_capabilities_next_turn is False
    assert projection.workflow_run is not None
    drifted = project_pinned_workflow_state(
        restored,
        available_mcp_tool_names=frozenset(),
        current_surface_digest="sha256:new-surface",
    )
    assert drifted.fail_closed is True
    assert drifted.reason == "workflow_surface_digest_changed"
    completed = restored.model_copy(update={"status": "completed"})
    done = project_pinned_workflow_state(completed, available_mcp_tool_names=frozenset())
    assert done.general_capabilities_next_turn is True
    assert done.sealed_active is False


def test_restart_fails_closed_for_changed_definition_or_dependency() -> None:
    instance = _instance()
    state = PinnedWorkflowInvocationState.create(
        instance_id="wf_research",
        instance=instance,
        params={"query": "x", "options": {"limit": 1}},
        surface_digest="sha256:surface",
    )
    changed = instance.model_copy(update={"definition": _definition(required=())})
    projection = project_pinned_workflow_state(
        state,
        available_mcp_tool_names=frozenset(),
        current_instance=changed,
    )
    assert projection.fail_closed is True
    assert projection.reason == "workflow_definition_digest_changed"

    mcp_def = _definition()
    mcp_def = mcp_def.model_copy(
        update={"mcp_mounts": (McpMount(server="gmail", tool_names=("read",), read_only=True),)}
    )
    mcp_instance = WorkflowInstance(
        owner_id="owner-a",
        definition_digest=mcp_def.digest(),
        definition=mcp_def,
        params={},
        enabled=True,
        approval=WorkflowApproval(
            approved_at="2026-08-25T00:00:00Z",
            approved_by="owner-a",
            surface_shown_digest=workflow_surface_digest({"x": 1}),
        ),
    )
    mcp_state = PinnedWorkflowInvocationState.create(
        instance_id="wf_mcp", instance=mcp_instance, params={}, surface_digest="sha256:s"
    )
    blocked = project_pinned_workflow_state(mcp_state, available_mcp_tool_names=frozenset())
    assert blocked.fail_closed is True
    assert blocked.reason is not None and "dependencies_invalid" in blocked.reason


def test_restart_requires_live_owner_enabled_approval_and_instance() -> None:
    instance = _instance()
    state = PinnedWorkflowInvocationState.create(
        instance_id="wf_research",
        instance=instance,
        params={"query": "x"},
        surface_digest="sha256:surface",
    )
    assert project_pinned_workflow_state(
        state,
        available_mcp_tool_names=frozenset(),
        require_current_instance=True,
    ).reason == "workflow_instance_missing"
    for changed, reason in (
        (instance.model_copy(update={"owner_id": "other"}), "workflow_owner_changed"),
        (instance.model_copy(update={"enabled": False}), "workflow_disabled"),
        (instance.model_copy(update={"approval": None}), "workflow_not_approved"),
    ):
        verdict = project_pinned_workflow_state(
            state,
            available_mcp_tool_names=frozenset(),
            current_surface_digest="sha256:surface",
            current_instance=changed,
            require_current_instance=True,
        )
        assert verdict.fail_closed is True
        assert verdict.reason == reason


def test_restart_rechecks_connector_bindings() -> None:
    instance = _instance().model_copy(update={"connector_bindings": {"github": "conn-1"}})
    state = PinnedWorkflowInvocationState.create(
        instance_id="wf_research",
        instance=instance,
        params={"query": "x"},
        surface_digest="sha256:surface",
    )
    projection = project_pinned_workflow_state(
        state,
        available_mcp_tool_names=frozenset(),
        available_connector_bindings=frozenset(),
        current_surface_digest="sha256:surface",
        current_instance=instance,
        require_current_instance=True,
    )
    assert projection.fail_closed is True
    assert projection.reason == "connector_bindings_unavailable"


def test_restart_rechecks_current_approval_surface_and_blocking_findings() -> None:
    instance = _instance()
    state = PinnedWorkflowInvocationState.create(
        instance_id="wf_research",
        instance=instance,
        params={"query": "x"},
        surface_digest="sha256:surface",
    )
    stale_approval = instance.model_copy(
        update={
            "approval": instance.approval.model_copy(
                update={"surface_shown_digest": "sha256:other"}
            )
            if instance.approval is not None
            else None
        }
    )
    stale = project_pinned_workflow_state(
        state,
        available_mcp_tool_names=frozenset(),
        current_surface_digest="sha256:surface",
        current_instance=stale_approval,
        require_current_instance=True,
    )
    assert stale.fail_closed is True
    assert stale.reason == "workflow_approval_surface_stale"

    blocked_instance = instance.model_copy(
        update={
            "validation_findings": (
                WorkflowValidationFinding(
                    severity="error",
                    code="dependency_revoked",
                    path="tools",
                    message="required dependency is no longer available",
                ),
            )
        }
    )
    blocked = project_pinned_workflow_state(
        state,
        available_mcp_tool_names=frozenset(),
        current_surface_digest="sha256:surface",
        current_instance=blocked_instance,
        require_current_instance=True,
    )
    assert blocked.fail_closed is True
    assert blocked.reason == "workflow_validation_blocking"
