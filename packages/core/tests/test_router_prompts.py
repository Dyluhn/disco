"""Prompt-variant selection — llm-router-contract.md §10.4.

Correct family×mode prompt prepended when no system message is present; not
overridden when one is; family derivation maps each model to its tag.
"""

from __future__ import annotations

import pytest
from llm_fakes import FakeModelProvider, simple_config
from perpleximanus.core import LLMMessage
from perpleximanus.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    DefaultLLMRouter,
    ModelRole,
    OperatingMode,
    StaticPromptProvider,
    derive_family,
)


@pytest.mark.parametrize(
    "model_id,expected",
    [
        ("claude-opus-4", "anthropic"),
        ("anthropic/whatever", "anthropic"),
        ("gpt-5-mini", "gpt"),
        ("Qwen3-35B-A3B", "qwen"),
        ("llama-3.3-70b", "llama"),
        ("mistral-large", "mistral"),
        ("gemma-3-27b", "gemma"),
        ("bge-reranker", "unknown"),
    ],
)
def test_family_derivation(model_id, expected):
    assert derive_family(model_id) == expected


async def test_prompt_prepended_for_resolved_family_and_mode():
    # AGENT_DRIVER's default mode is LONG_HORIZON; local model family is "qwen".
    templates = {
        ("qwen", OperatingMode.LONG_HORIZON, ModelRole.AGENT_DRIVER): "QWEN-LONGHORIZON-DRIVER",
    }
    local = FakeModelProvider("ollama")
    providers = {"ollama": local, "openrouter": FakeModelProvider("openrouter")}
    router = DefaultLLMRouter(
        simple_config(), providers, prompt_provider=StaticPromptProvider(templates)
    )
    await router.complete(
        CompletionRequest(
            profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
            messages=[LLMMessage(role="user", content="hello")],
        )
    )
    seen = local.seen_requests[0]
    assert seen.messages[0].role == "system"
    assert seen.messages[0].content == "QWEN-LONGHORIZON-DRIVER"
    assert seen.messages[1].content == "hello"  # original user message preserved


async def test_existing_system_message_is_not_overridden():
    local = FakeModelProvider("ollama")
    providers = {"ollama": local, "openrouter": FakeModelProvider("openrouter")}
    router = DefaultLLMRouter(
        simple_config(),
        providers,
        prompt_provider=StaticPromptProvider({}, fallback="ROUTER-DEFAULT"),
    )
    await router.complete(
        CompletionRequest(
            profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
            messages=[
                LLMMessage(role="system", content="MY OWN SYSTEM PROMPT"),
                LLMMessage(role="user", content="hello"),
            ],
        )
    )
    seen = local.seen_requests[0]
    assert seen.messages[0].content == "MY OWN SYSTEM PROMPT"  # caller's, untouched
    assert len(seen.messages) == 2  # nothing prepended


def test_driver_prompts_inject_enabled_skills_block():
    """Enabled skills are prepended to BOTH driver prompts (planning + execution)
    so the agent follows the user's standing instructions; absent skills leave
    the prompts unchanged."""
    from perpleximanus.core import Skill, render_skills_for_prompt
    from perpleximanus.core.llm import DriverPrompts, ModelRole, OperatingMode

    skills = [
        Skill(id="yf", name="Yahoo Finance", description="stock data", body="Use the v8 endpoint."),
    ]
    block = render_skills_for_prompt(skills)
    dp = DriverPrompts(skills_block=block)

    planning = dp.system_prompt(
        model_family="qwen", mode=OperatingMode.PLANNING, role=ModelRole.AGENT_DRIVER
    )
    execution = dp.system_prompt(
        model_family="qwen", mode=OperatingMode.LONG_HORIZON, role=ModelRole.AGENT_DRIVER
    )
    assert "Yahoo Finance" in planning and "v8 endpoint" in planning
    assert "Yahoo Finance" in execution and "v8 endpoint" in execution
    # The original prompt content still follows.
    assert "PLANNING mode" in planning

    # No skills → prompts unchanged (no skills header).
    bare = DriverPrompts(skills_block="")
    bare_planning = bare.system_prompt(
        model_family="qwen", mode=OperatingMode.PLANNING, role=ModelRole.AGENT_DRIVER
    )
    assert "Skill:" not in bare_planning
