"""`reference_pack`: catalog with no args, the pack text by id, a helpful error for an
unknown id, and presence in the default registry and the free-form build scope."""

from __future__ import annotations

from disco.core.llm import ModelExecutionPolicy
from disco.core.reference_packs import ReferencePackRegistry
from disco.tools import DefaultToolExecutor, agent_scope, build_default_registry
from disco.tools.builtin.references import ReferencePackTool
from tool_fakes import FakeSandboxInstance, call


def _executor():
    return DefaultToolExecutor(
        build_default_registry(),
        agent_scope(model_policy=ModelExecutionPolicy.standard()),
        sandbox=FakeSandboxInstance(),
    )


async def test_no_arguments_returns_the_catalog():
    outcome = await _executor().execute(call("reference_pack"))
    assert outcome.success
    assert outcome.content == ReferencePackRegistry.default().index()


async def test_known_id_returns_the_pack_text():
    outcome = await _executor().execute(call("reference_pack", pack="payments-stripe"))
    assert outcome.success
    assert outcome.content.startswith("# Payments with Stripe")
    assert outcome.structured == {
        "pack": "payments-stripe",
        "title": "Payments with Stripe (Payment Element + webhooks)",
    }


async def test_unknown_id_fails_and_lists_the_ids():
    outcome = await _executor().execute(call("reference_pack", pack="stripe"))
    assert not outcome.success
    assert outcome.error is not None
    assert "unknown reference pack 'stripe'" in outcome.error
    assert "payments-stripe" in outcome.error


def test_registered_read_only_and_in_the_build_scope():
    scope = agent_scope(model_policy=ModelExecutionPolicy())
    tool = build_default_registry().get("reference_pack", scope=scope)
    assert tool is not None and isinstance(tool, ReferencePackTool)
    assert tool.definition.read_only and tool.definition.runs_in == "in_process"
    advertised = (
        scope.advertised_tools if scope.advertised_tools is not None else scope.allowed_tools
    )
    assert "reference_pack" in advertised


def test_description_carries_every_pack_id():
    description = ReferencePackTool.definition.description
    for pack_id in ReferencePackRegistry.default().ids():
        assert f"- {pack_id} — " in description
