"""`build_playbook`: catalog with no args, the playbook text by id, a helpful error for an
unknown id, and presence in the default registry and the free-form build scope."""

from __future__ import annotations

from disco.core.llm import ModelExecutionPolicy
from disco.core.playbooks import PlaybookRegistry
from disco.tools import DefaultToolExecutor, agent_scope, build_default_registry
from disco.tools.builtin.playbook import BuildPlaybookTool
from tool_fakes import FakeSandboxInstance, call


def _executor():
    return DefaultToolExecutor(
        build_default_registry(),
        agent_scope(model_policy=ModelExecutionPolicy.standard()),
        sandbox=FakeSandboxInstance(),
    )


async def test_no_arguments_returns_the_catalog():
    outcome = await _executor().execute(call("build_playbook"))
    assert outcome.success
    assert outcome.content == PlaybookRegistry.default().index()


async def test_known_id_returns_the_pack_text():
    outcome = await _executor().execute(call("build_playbook", playbook="payments-stripe"))
    assert outcome.success
    assert outcome.content.startswith("# Payments with Stripe")
    assert outcome.structured == {
        "playbook": "payments-stripe",
        "title": "Payments with Stripe (Payment Element + webhooks)",
    }


async def test_unknown_id_fails_and_lists_the_ids():
    outcome = await _executor().execute(call("build_playbook", playbook="stripe"))
    assert not outcome.success
    assert outcome.error is not None
    assert "unknown playbook 'stripe'" in outcome.error
    assert "payments-stripe" in outcome.error


def test_registered_read_only_and_in_the_build_scope():
    scope = agent_scope(model_policy=ModelExecutionPolicy())
    tool = build_default_registry().get("build_playbook", scope=scope)
    assert tool is not None and isinstance(tool, BuildPlaybookTool)
    assert tool.definition.read_only and tool.definition.runs_in == "in_process"
    advertised = (
        scope.advertised_tools if scope.advertised_tools is not None else scope.allowed_tools
    )
    assert "build_playbook" in advertised


def test_description_carries_every_pack_id():
    description = BuildPlaybookTool.definition.description
    for pack_id in PlaybookRegistry.default().ids():
        assert f"- {pack_id} — " in description
