"""Workflow model and scope-compiler tests."""

from __future__ import annotations

import re

import pytest
from disco.core.workflow import (
    McpMount,
    ScheduleSpec,
    WorkflowApproval,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowOutputContract,
    WorkflowPolicies,
    WorkflowVerify,
    compile_workflow_scope,
)
from pydantic import ValidationError


def _params_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["query"],
    }


def _defn(
    *,
    tools: tuple[str, ...] = ("file_read", "search"),
    mcp_mounts: tuple[McpMount, ...] = (),
    policies: WorkflowPolicies | None = None,
    schema: dict[str, object] | None = None,
) -> WorkflowDefinition:
    return WorkflowDefinition(
        name="research_summary",
        card="Summarize a bounded research query into a single workflow output file.",
        params_model_schema=schema or _params_schema(),
        tools=tools,
        mcp_mounts=mcp_mounts,
        skills=("research-notes",),
        policies=policies or WorkflowPolicies(),
        output_contract=WorkflowOutputContract(
            path_template="outputs/{query}.md",
            format="markdown",
        ),
        verify=WorkflowVerify(checks=("output_exists",), finalizer="ready_for_workflow_output"),
    )


def test_model_strictness_and_roundtrip() -> None:
    defn = _defn()
    assert WorkflowDefinition.model_validate(defn.model_dump(mode="json")) == defn

    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(
            {
                **defn.model_dump(mode="json"),
                "surprise": True,
            }
        )

    with pytest.raises(ValidationError):
        defn.name = "changed"


def test_instance_pins_embedded_definition_digest() -> None:
    defn = _defn()
    digest = defn.digest()
    instance = WorkflowInstance(
        definition_digest=digest,
        definition=defn,
        params={"query": "mcp safety", "limit": 3},
        connector_bindings={"github": "conn_123"},
        approval=WorkflowApproval(
            approved_at="2026-07-04T12:00:00Z",
            approved_by="user_1",
            surface_shown_digest=digest,
        ),
    )
    assert instance.enabled is False
    assert WorkflowInstance.model_validate(instance.model_dump(mode="json")) == instance

    with pytest.raises(ValidationError):
        WorkflowInstance(
            definition_digest="sha256:wrong",
            definition=defn,
            params={"query": "x"},
        )


def test_digest_stability_uses_canonical_json() -> None:
    first = _defn(schema=_params_schema())
    reordered_schema = {
        "required": ["query"],
        "properties": {
            "tags": {"items": {"type": "string"}, "type": "array"},
            "limit": {"type": "integer"},
            "query": {"type": "string"},
        },
        "additionalProperties": False,
        "type": "object",
    }
    second = _defn(schema=reordered_schema)

    assert first.digest() == second.digest()
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", first.digest())


def test_writable_mcp_mount_requires_write_policy() -> None:
    with pytest.raises(ValidationError, match="allows_writes=True"):
        _defn(
            mcp_mounts=(
                McpMount(server="github", tool_names=("create_issue",), read_only=False),
            )
        )

    defn = _defn(
        mcp_mounts=(McpMount(server="github", tool_names=("create_issue",), read_only=False),),
        policies=WorkflowPolicies(allows_writes=True),
    )
    assert defn.policies.allows_writes is True


def test_params_schema_rejects_bare_object_and_untyped_array_nodes() -> None:
    bare_object = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"payload": {"type": "object"}},
    }
    with pytest.raises(ValidationError, match="bare-object"):
        _defn(schema=bare_object)

    untyped_array = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"items": {"type": "array"}},
    }
    with pytest.raises(ValidationError, match="untyped-array"):
        _defn(schema=untyped_array)


def test_mcp_mount_uses_explicit_tool_names_only() -> None:
    mount = McpMount(
        server="github",
        tool_names=("mcp__github__search_issues", "read_issue"),
        read_only=True,
    )
    assert mount.tool_names == ("search_issues", "read_issue")

    with pytest.raises(ValidationError, match="explicit"):
        McpMount(server="github", tool_names=("*",), read_only=True)

    with pytest.raises(ValidationError, match="does not match"):
        McpMount(server="github", tool_names=("mcp__slack__search",), read_only=True)


def test_compile_workflow_scope_happy_path() -> None:
    defn = _defn(
        tools=("file_read", "search"),
        mcp_mounts=(McpMount(server="github", tool_names=("search_issues",), read_only=True),),
    )

    scope = compile_workflow_scope(defn, frozenset({"mcp__github__search_issues"}))

    expected = frozenset(
        {
            "file_read",
            "search",
            "mcp__github__search_issues",
            "finish",
            "skip",
            "needs_input",
        }
    )
    assert scope.allowed_tools == expected
    assert scope.advertised == expected


def test_compile_workflow_scope_rejects_unknown_builtin_tool() -> None:
    defn = _defn(tools=("file_read", "not_a_builtin"))

    with pytest.raises(ValueError, match="unknown workflow builtin"):
        compile_workflow_scope(defn, frozenset())


def test_compile_workflow_scope_rejects_missing_mounted_mcp_tool() -> None:
    defn = _defn(
        mcp_mounts=(McpMount(server="github", tool_names=("search_issues",), read_only=True),)
    )

    with pytest.raises(ValueError, match="missing mounted MCP tool"):
        compile_workflow_scope(defn, frozenset({"mcp__github__other_tool"}))


def test_schedule_spec_validates_cron() -> None:
    digest = "sha256:" + ("0" * 64)
    spec = ScheduleSpec(
        instance_id="wf_ok",
        instance_digest=digest,
        cron="*/5 * * * *",
    )

    assert spec.enabled is True

    with pytest.raises(ValidationError, match="invalid cron expression"):
        ScheduleSpec(
            instance_id="wf_ok",
            instance_digest=digest,
            cron="garbage",
        )
