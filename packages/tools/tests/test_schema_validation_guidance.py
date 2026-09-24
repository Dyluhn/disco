"""Rejected calls receive relevant authored constraints, never validator prose or input."""

from __future__ import annotations

from copy import deepcopy

import pytest
from disco.core.release.spec import ResourceDecl
from disco.tools import ToolDef, ToolOutcome
from disco.tools.behavior import declares
from disco.tools.builtin import ReleaseDeclareTool
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from test_validation_error_serialization import _LIVE_DECLARATION, _SECRET, _executor
from tool_fakes import call


@pytest.mark.parametrize("url", ["file:/app/data/app.db", f"file:/app/{_SECRET}.db"])
async def test_actual_resource_mismatch_explains_matching_absolute_url_in_model_visible_error(url):
    class CountingReleaseTool(ReleaseDeclareTool):
        executions = 0

        async def run(self, args, ctx):
            self.executions += 1
            return await super().run(args, ctx)

    recorded = []

    async def writer(*args):
        recorded.append(args)

    tool = CountingReleaseTool()
    executor = _executor(tool, release_intent_writer=writer)
    arguments = deepcopy(_LIVE_DECLARATION)
    resource = arguments["resources"][0]
    resource["profiles"]["local"]["url"] = url  # G41 action527/534 plus a rejected secret value.
    failed = await executor.execute(call("release_declare", **arguments))
    assert not failed.success and failed.structured["kind"] == "invalid_arguments"
    assert recorded == [] and tool.executions == 0
    assert failed.content == failed.error
    assert "resources[*].persistent_path" in failed.content
    assert "resources[*].profiles" in failed.content
    assert "absolute POSIX path" in failed.content
    assert "after trimming slashes" in failed.content
    assert "persistent_path such as /app/data/app.db" in failed.content
    assert "Expected arguments" in failed.content
    assert _SECRET not in failed.model_dump_json()
    resource["persistent_path"] = "/app/data/app.db"
    resource["profiles"]["local"]["url"] = "file:/app/data/app.db"
    repaired = await executor.execute(call("release_declare", **arguments))
    assert repaired.success, repaired.error
    assert len(recorded) == 1 and tool.executions == 1


def test_resource_guidance_does_not_strengthen_existing_path_consistency_rules():
    base = deepcopy(_LIVE_DECLARATION["resources"][0])
    base["profiles"]["local"]["url"] = "file:/data/app.db"
    for path in ("data/app.db", "/data/app.db", "/data", "/data/app.db/child"):
        assert ResourceDecl.model_validate({**base, "persistent_path": path})
    with pytest.raises(ValidationError):
        ResourceDecl.model_validate({**base, "persistent_path": "/other/app.db"})
    schema = ResourceDecl.model_json_schema()
    assert schema["required"] == ["id", "kind", "persistent_path", "profiles"]
    assert "Matching pair" in schema["properties"]["persistent_path"]["description"]


class _ConstraintItem(BaseModel):
    """The opening number must be smaller than the closing number."""

    opening: int = Field(description="Opening bound: choose a number smaller than closing.")
    closing: int = Field(description="Closing bound: choose a number greater than opening.")

    @model_validator(mode="after")
    def validate_interval(self):
        if self.opening >= self.closing:
            raise ValueError(f"private rejected data {_SECRET}: {self.opening}")
        return self


class _ConstraintArgs(BaseModel):
    windows: list[_ConstraintItem] | None = None
    unrelated: str = Field(default="", description="UNRELATED_DESCRIPTION_MUST_NOT_APPEAR")


async def test_generic_nested_nullable_model_guidance_is_relevant_deduplicated_and_redacted():
    class ConstraintTool:
        definition = ToolDef(
            name="bounded_window",
            description="fixture",
            args_model=_ConstraintArgs,
            runs_in="in_process",
            behavior=declares(planner_safe=False),
        )
        executions = 0

        async def run(self, args, ctx):
            self.executions += 1
            return ToolOutcome(success=True, content="accepted")

    tool = ConstraintTool()
    executor = _executor(tool)
    failed = await executor.execute(
        call("bounded_window", windows=[{"opening": 9, "closing": 1}] * 12)
    )
    assert not failed.success and tool.executions == 0
    assert "The opening number must be smaller" in failed.content
    assert failed.content.count("Opening bound: choose") == 1
    assert failed.content.count("Closing bound: choose") == 1
    assert "UNRELATED_DESCRIPTION_MUST_NOT_APPEAR" not in failed.content
    assert _SECRET not in failed.model_dump_json()
    repaired = await executor.execute(
        call("bounded_window", windows=[{"opening": 1, "closing": 9}])
    )
    assert repaired.success and tool.executions == 1


class _LongDescriptionArgs(BaseModel):
    code: str = Field(description="AUTHOR_GUIDANCE " * 1000)
    unrelated: str = Field(default="", description="UNRELATED_DESCRIPTION")

    @field_validator("code")
    @classmethod
    def invalid_code(cls, value):
        raise ValueError(f"Never disclose {value}")


async def test_field_guidance_is_bounded_and_never_uses_rejected_value():
    class ConstraintTool:
        definition = ToolDef(
            name="constraint_check",
            description="fixture",
            args_model=_LongDescriptionArgs,
            runs_in="in_process",
            behavior=declares(planner_safe=False),
        )

        async def run(self, args, ctx):
            raise AssertionError("invalid arguments must not execute")

    result = await _executor(ConstraintTool()).execute(call("constraint_check", code=_SECRET))
    assert not result.success
    hint = result.content.split(" Schema guidance: ", 1)[1].split(" You provided:", 1)[0]
    assert len(hint) <= 510
    assert "AUTHOR_GUIDANCE" in hint and "…" in hint
    assert "UNRELATED_DESCRIPTION" not in result.content
    assert _SECRET not in result.model_dump_json()
