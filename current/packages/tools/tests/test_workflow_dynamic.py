from __future__ import annotations

import pytest
from disco.core.workflow import WorkflowToolDescriptor
from disco.tools.anatomy import ToolContext, ToolOutcome
from disco.tools.builtin.workflow_dynamic import dynamic_workflow_tool


def _descriptor() -> WorkflowToolDescriptor:
    return WorkflowToolDescriptor(
        name="workflow__example__a" * 1,
        description="Run the example workflow.",
        parameters_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "minLength": 2},
                "limit": {"type": "integer", "minimum": 1},
            },
            "required": ["query"],
        },
        instance_id="example",
        definition_digest="sha256:example-definition",
    )


def test_dynamic_tool_uses_exact_original_schema() -> None:
    tool = dynamic_workflow_tool(_descriptor(), lambda _params, _ctx: _noop())
    assert tool.definition.to_spec().parameters_schema == _descriptor().parameters_schema
    assert tool.definition.args_model.model_json_schema() == _descriptor().parameters_schema


def test_dynamic_tool_rejects_unsupported_schema_keywords() -> None:
    descriptor = _descriptor().model_copy(
        update={
            "parameters_schema": {
                **_descriptor().parameters_schema,
                "patternProperties": {".*": {"type": "string"}},
            }
        }
    )
    tool = dynamic_workflow_tool(descriptor, lambda _params, _ctx: _noop())
    with pytest.raises(ValueError, match="unsupported_schema"):
        tool.definition.args_model.model_validate({"query": "ok"})


@pytest.mark.asyncio
async def test_dynamic_tool_passes_validated_values_to_host_callback() -> None:
    seen: list[dict] = []

    async def invoke(params, _ctx):
        seen.append(params)
        return ToolOutcome(success=True, content="queued")

    tool = dynamic_workflow_tool(_descriptor(), invoke)
    args = tool.definition.args_model.model_validate({"query": "ok", "limit": 2})
    ctx = ToolContext(
        sandbox=None,
        workspace_path="/workspace",
        timeout_s=10,
        capabilities=frozenset(),
        owner_id="owner",
        conversation_id="conversation",
    )
    outcome = await tool.run(args, ctx)
    assert outcome.success is True
    assert seen == [{"query": "ok", "limit": 2}]


async def _noop():
    return ToolOutcome(success=True, content="ok")
