"""H282: strict AppKit prompts must agree with the enforced semantic scope."""

from __future__ import annotations

import pytest
from disco.core.llm import DriverPrompts, ModelRole, OperatingMode, Requirement

_FORBIDDEN = (
    "`file_write`",
    "`file_edit`",
    "`shell`",
    "`code_exec`",
    "`scaffold_starter`",
    "`update_plan_progress`",
    "`preview_start`",
    "`browser`",
)


@pytest.mark.parametrize("assist", [False, True])
@pytest.mark.parametrize("autonomous", [False, True])
def test_strict_appkit_prompt_never_promises_raw_tools(*, assist: bool, autonomous: bool) -> None:
    prompts = DriverPrompts(appkit_mode=True, autonomous=autonomous)
    for mode in (OperatingMode.PLANNING, OperatingMode.LONG_HORIZON):
        prompt = prompts.system_prompt(
            model_family="deepseek",
            mode=mode,
            role=ModelRole.AGENT_DRIVER,
            assist=assist,
        )
        for token in _FORBIDDEN:
            assert token not in prompt, (mode, token)
        assert "strict AppKit" in prompt or "STRICT APPKIT" in prompt
        assert "local_list" in prompt
        assert "app_create" in prompt
        assert "verify_appkit_app" in prompt


def test_strict_appkit_planning_omits_generic_capability_suffixes() -> None:
    prompt = DriverPrompts(appkit_mode=True).system_prompt(
        model_family="deepseek",
        mode=OperatingMode.PLANNING,
        role=ModelRole.AGENT_DRIVER,
    )
    assert "EXECUTION TOOLS — available AFTER plan approval" not in prompt
    assert "HARDWARE IDENTITY EVIDENCE" not in prompt
    assert "submit_plan" in prompt


def test_strict_appkit_planning_never_routes_local_list_to_add_on_primitive() -> None:
    prompt = DriverPrompts(appkit_mode=True).system_prompt(
        model_family="deepseek",
        mode=OperatingMode.PLANNING,
        role=ModelRole.AGENT_DRIVER,
    )
    assert "`local_list` is a complete base with no compatible add-ons" in prompt
    assert "never plan `app_add_primitive` for it" in prompt
    assert "For supported base primitives" in prompt
    assert "only when a compatible add-on is required" in prompt


@pytest.mark.parametrize("assist", [False, True])
def test_strict_appkit_execution_never_routes_local_list_to_add_on_primitive(
    *, assist: bool
) -> None:
    prompt = DriverPrompts(appkit_mode=True).system_prompt(
        model_family="deepseek",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
        assist=assist,
    )
    assert "`local_list` supports no add-on primitives" in prompt
    assert "never call `app_add_primitive` for it" in prompt
    assert "For another supported base" in prompt
    assert "remains available only for a compatible add-on" in prompt


@pytest.mark.parametrize("mode", [OperatingMode.PLANNING, OperatingMode.LONG_HORIZON])
def test_strict_appkit_suppresses_incompatible_model_capability_bullets(
    mode: OperatingMode,
) -> None:
    prompt = DriverPrompts(appkit_mode=True).system_prompt(
        model_family="deepseek",
        mode=mode,
        role=ModelRole.AGENT_DRIVER,
        capabilities=frozenset({Requirement.VISION, Requirement.ANCHORED_EDIT}),
    )
    assert "latest browser screenshot" not in prompt
    assert "After navigating" not in prompt
    assert "`exact_replace`" not in prompt


def test_non_autonomous_strict_appkit_planning_does_not_advertise_withheld_escape() -> None:
    prompt = DriverPrompts(appkit_mode=True, autonomous=False).system_prompt(
        model_family="deepseek",
        mode=OperatingMode.PLANNING,
        role=ModelRole.AGENT_DRIVER,
    )
    assert "request_custom_build" not in prompt


def test_non_autonomous_strict_appkit_execution_describes_confirmed_escape() -> None:
    prompt = DriverPrompts(appkit_mode=True, autonomous=False).system_prompt(
        model_family="deepseek",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
    )
    assert "`request_custom_build` is the only escape hatch" in prompt
    assert "requires explicit human confirmation" in prompt
    assert "demonstrated capability gap" in prompt
    assert "unavailable for the entire strict run" not in prompt


def test_confirmed_widening_switches_dynamic_prompt_to_ordinary_build_profile() -> None:
    strict = True
    prompts = DriverPrompts(appkit_mode=True, appkit_mode_active=lambda: strict)
    strict_prompt = prompts.system_prompt(
        model_family="deepseek",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
    )
    assert "STRICT APPKIT" in strict_prompt
    assert "`file_write`" not in strict_prompt

    strict = False
    widened_prompt = prompts.system_prompt(
        model_family="deepseek",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
    )
    ordinary_prompt = DriverPrompts().system_prompt(
        model_family="deepseek",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
    )
    assert widened_prompt == ordinary_prompt
    assert "STRICT APPKIT" not in widened_prompt
    assert "file_write" in widened_prompt


@pytest.mark.parametrize("mode", [OperatingMode.PLANNING, OperatingMode.LONG_HORIZON])
def test_autonomous_strict_appkit_never_advertises_unavailable_escape(
    mode: OperatingMode,
) -> None:
    prompt = DriverPrompts(appkit_mode=True, autonomous=True).system_prompt(
        model_family="deepseek",
        mode=mode,
        role=ModelRole.AGENT_DRIVER,
    )
    assert "request_custom_build" not in prompt
    assert "no human-confirmed scope-widening path" in prompt
    assert "disabled for the entire run" in prompt


def test_ordinary_build_prompt_is_unchanged_by_appkit_profile() -> None:
    before = DriverPrompts()
    DriverPrompts(appkit_mode=True)
    after = DriverPrompts()
    for mode in (OperatingMode.PLANNING, OperatingMode.LONG_HORIZON):
        assert before.system_prompt(
            model_family="qwen", mode=mode, role=ModelRole.AGENT_DRIVER, assist=False
        ) == after.system_prompt(
            model_family="qwen", mode=mode, role=ModelRole.AGENT_DRIVER, assist=False
        )


def test_ordinary_build_keeps_compatible_model_capability_bullets() -> None:
    prompt = DriverPrompts().system_prompt(
        model_family="deepseek",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
        capabilities=frozenset({Requirement.VISION, Requirement.ANCHORED_EDIT}),
    )
    assert "latest browser screenshot" in prompt
    assert "After navigating" in prompt
    assert "`exact_replace`" in prompt
