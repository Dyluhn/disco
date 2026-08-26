"""Dynamic Agent tools for approved workflow instances.

The workflow definition remains the only schema authority.  This adapter wraps
that schema in a normal :class:`ToolDef` for the ordinary executor while
delegating all validation to ``core.workflow.validate_workflow_params``.  It
does not generate a second, lossy Pydantic field model.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, ClassVar

from disco.core import SecurityRisk
from disco.core.effects import EffectCapability
from disco.core.workflow import WorkflowToolDescriptor, validate_workflow_params
from pydantic import BaseModel, ConfigDict, model_validator
from pydantic.json_schema import DEFAULT_REF_TEMPLATE, GenerateJsonSchema, JsonSchemaMode

from ..anatomy import Tool, ToolContext, ToolDef, ToolOutcome
from ..behavior import declares


class _ExactWorkflowArgs(BaseModel):
    """Fieldless strict envelope whose before-validator owns the exact schema."""

    model_config = ConfigDict(extra="allow", strict=True)
    _workflow_schema: ClassVar[dict[str, Any]] = {}

    @model_validator(mode="before")
    @classmethod
    def _validate_exact_schema(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            raise ValueError("workflow parameters must be a JSON object")
        result = validate_workflow_params(cls._workflow_schema, value)
        if not result.valid:
            details = [issue.model_dump(mode="json") for issue in result.issues]
            raise ValueError(json.dumps(details, sort_keys=True, ensure_ascii=False))
        return dict(value)

    @classmethod
    def model_json_schema(
        cls,
        by_alias: bool = True,
        ref_template: str = DEFAULT_REF_TEMPLATE,
        schema_generator: type[GenerateJsonSchema] = GenerateJsonSchema,
        mode: JsonSchemaMode = "validation",
        **_: Any,
    ) -> dict[str, Any]:
        """Expose the original schema byte-for-byte (no generated shadow schema)."""
        del by_alias, ref_template, schema_generator, mode
        return copy.deepcopy(cls._workflow_schema)


def _args_model(schema: Mapping[str, Any], *, class_name: str) -> type[BaseModel]:
    model = type(
        class_name,
        (_ExactWorkflowArgs,),
        {"_workflow_schema": copy.deepcopy(dict(schema))},
    )
    return model


InvokeWorkflow = Callable[[dict[str, Any], ToolContext], Awaitable[ToolOutcome]]


class DynamicWorkflowTool:
    """A regular executor Tool backed by one approved workflow descriptor."""

    def __init__(self, descriptor: WorkflowToolDescriptor, invoke: InvokeWorkflow) -> None:
        schema_hash = descriptor.definition_digest.replace("sha256:", "")[:16]
        args_model = _args_model(
            descriptor.parameters_schema,
            class_name=f"WorkflowArgs_{schema_hash or 'dynamic'}",
        )
        self.definition = ToolDef(
            name=descriptor.name,
            description=descriptor.description,
            args_model=args_model,
            base_risk=SecurityRisk.LOW,
            runs_in="in_process",
            read_only=True,
            behavior=declares(EffectCapability.RUN_CONTROL, planner_safe=True),
        )
        self._invoke = invoke

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolOutcome:
        return await self._invoke(args.model_dump(mode="python"), ctx)


def dynamic_workflow_tool(
    descriptor: WorkflowToolDescriptor,
    invoke: InvokeWorkflow,
) -> Tool:
    """Build one ordinary Agent tool from a ready workflow descriptor."""

    return DynamicWorkflowTool(descriptor, invoke)


__all__ = ["DynamicWorkflowTool", "dynamic_workflow_tool"]
