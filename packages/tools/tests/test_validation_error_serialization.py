"""Recoverable argument errors never serialize validator exceptions or rejected values."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from disco.core import ObservationEvent, SqliteEventStore
from disco.tools import DefaultToolExecutor, ToolDef, ToolOutcome
from disco.tools.behavior import declares
from disco.tools.builtin import ReleaseDeclareTool
from disco.tools.registry import ToolRegistry, ToolScope
from pydantic import BaseModel, field_validator
from pydantic_core import PydanticCustomError
from tool_fakes import call

_SECRET = "PRIVATE_REJECTED_VALUE_8e7a"


def _executor(tool, **kwargs):
    registry = ToolRegistry()
    registry.register(tool)
    return DefaultToolExecutor(
        registry, ToolScope(allowed_tools=frozenset({tool.definition.name})), **kwargs
    )


@pytest.mark.parametrize("validator_error", ["value", "assertion", "custom", "spoofed_builtin"])
async def test_custom_validator_failures_are_redacted_serializable_and_never_executed(
    validator_error,
):
    class Arguments(BaseModel):
        token: str

        @field_validator("token")
        @classmethod
        def reject(cls, value):
            if validator_error == "value":
                raise ValueError(f"rejected token {value}")
            if validator_error == "assertion":
                raise AssertionError(f"rejected token {value}")
            kind = value if validator_error == "custom" else "literal_error"
            raise PydanticCustomError(
                kind, "rejected token {token}", {"token": value, "error": ValueError(value)}
            )

    class GuardedTool:
        definition = ToolDef(
            name="guarded",
            description="fixture",
            args_model=Arguments,
            runs_in="in_process",
            behavior=declares(planner_safe=False),
        )
        executions = 0

        async def run(self, args, ctx):
            self.executions += 1
            return ToolOutcome(success=True, content="unexpected")

    tool = GuardedTool()
    executor = _executor(tool)
    result = await executor.execute(call("guarded", token=_SECRET))
    assert not result.success
    assert result.structured["kind"] == "invalid_arguments"
    assert tool.executions == 0
    assert "token" in result.content and "Expected arguments" in result.content
    blob = result.model_dump_json()
    assert _SECRET not in blob
    errors = json.loads(blob)["structured"]["validation_errors"]
    assert len(errors) == 1 and errors[0]["loc"] == ["token"]
    assert set(errors[0]) == {"loc", "type", "msg"}
    assert (
        errors[0]["type"]
        == {
            "value": "value_error",
            "assertion": "assertion_error",
            "custom": "value_error",
            "spoofed_builtin": "literal_error",
        }[validator_error]
    )
    assert result.structured["expected_schema"] == Arguments.model_json_schema()
    store = SqliteEventStore(":memory:")
    try:
        await store.append(
            "validation", ObservationEvent(action_id="rejected-action", tool_result=result)
        )
        restored = (await store.get_events("validation"))[0].tool_result
        assert restored.model_dump(mode="json") == result.model_dump(mode="json")
    finally:
        store.close()


# Exact public release_declare payload shape from G41 action499, no fixture secrets.
_LIVE_DECLARATION = {
    "build_cmd": [],
    "env": [{"name": "PORT", "required": False, "scope": "runtime", "secret": "public"}],
    "health_path": "/__health/db",
    "install_cmd": [],
    "port_env": "PORT",
    "required_env": [],
    "resources": [
        {
            "consumers": ["api"],
            "id": "ticket-data",
            "kind": "sqlite",
            "persistent_path": "data/app.db",
            "profiles": {"local": {"url": "file:data/app.db", "volume": "ticket-data"}},
        }
    ],
    "runtime": "node",
    "start_cmd": ["node", "server.js"],
}


async def test_actual_release_declare_bad_nested_payload_is_recoverable_then_repair_persists_once():
    class CountingReleaseTool(ReleaseDeclareTool):
        executions = 0

        async def run(self, args, ctx):
            self.executions += 1
            return await super().run(args, ctx)

    written = []

    async def writer(conversation_id, owner_id, intent):
        written.append(intent)

    tool = CountingReleaseTool()
    executor = _executor(tool, release_intent_writer=writer)
    bad = await executor.execute(call("release_declare", **deepcopy(_LIVE_DECLARATION)))
    assert not bad.success and bad.structured["kind"] == "invalid_arguments"
    assert tool.executions == 0 and written == []
    assert "resources.0" in bad.content and "Expected arguments" in bad.content
    assert "ResourceDecl" in bad.structured["expected_schema"]["$defs"]
    assert json.loads(bad.model_dump_json())["error"] == bad.error
    again = await executor.execute(call("release_declare", **deepcopy(_LIVE_DECLARATION)))
    assert again.content == bad.content and again.structured == bad.structured
    assert tool.executions == 0 and written == []
    repaired = deepcopy(_LIVE_DECLARATION)
    resource = repaired["resources"][0]
    resource["persistent_path"] = "/data/app.db"
    resource["profiles"]["local"]["url"] = "file:/data/app.db"
    good = await executor.execute(call("release_declare", **repaired))
    assert good.success, good.error
    assert tool.executions == 1 and len(written) == 1
    assert written[0].resources[0].persistent_path == "/data/app.db"


async def test_nested_release_validator_rejection_does_not_echo_resource_value():
    tool = ReleaseDeclareTool()
    written = []

    async def writer(*args):
        written.append(args)

    executor = _executor(tool, release_intent_writer=writer)
    arguments = {
        "resources": [
            {
                "id": "db",
                "kind": "sqlite",
                "persistent_path": "/data/app.db",
                "profiles": {
                    "local": {"url": "file:/data/app.db", "volume": "bad value " + _SECRET}
                },
            }
        ]
    }
    result = await executor.execute(call("release_declare", **arguments))
    assert not result.success and result.structured["kind"] == "invalid_arguments"
    assert "resources.0.profiles.local.volume" in result.content
    assert _SECRET not in result.model_dump_json()
    assert written == []
