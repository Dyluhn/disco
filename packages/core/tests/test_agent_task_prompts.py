"""Standalone Agent gets proportional task instructions without changing Build."""

from __future__ import annotations

from disco.core.llm import DriverPrompts, ModelRole, OperatingMode


def _prompt(mode: OperatingMode, *, assist: bool = False, autonomous: bool = False) -> str:
    return DriverPrompts(flavor="agent", autonomous=autonomous).system_prompt(
        model_family="deepseek",
        mode=mode,
        role=ModelRole.AGENT_DRIVER,
        assist=assist,
    )


def test_agent_planning_is_task_shaped_not_build_prompt_with_renamed_identity() -> None:
    prompt = _prompt(OperatingMode.PLANNING)

    assert "AGENT TASK SHAPE" in prompt
    assert "direct-answer task" in prompt
    assert "document, script, dataset, or archive is not a web app" in prompt
    assert "inflate a simple answer into build ceremony" in prompt
    assert "autonomous build agent" not in prompt
    assert "previous build" not in prompt


def test_agent_execution_keeps_artifact_and_preview_use_proportional() -> None:
    prompt = _prompt(OperatingMode.LONG_HORIZON)

    assert "For a no-artifact task, the summary is the user-facing result" in prompt
    assert "Skip it when the task has no artifact" in prompt
    assert "only for an actual runnable site or app" in prompt
    assert "Do not turn scripts, documents, data, or archives into web apps" in prompt
    assert "START FROM A STARTER, NOT A BLANK FILE" not in prompt
    assert "MAKE IT VISUAL" not in prompt
    assert "This is how you talk during a build" not in prompt


def test_agent_execution_preserves_critical_loop_and_file_truths() -> None:
    for assist in (False, True):
        prompt = _prompt(OperatingMode.LONG_HORIZON, assist=assist)
        for required in (
            "finish(summary)",
            "serve(title, path)",
            "ask_user",
            "notify_user",
            "file_read",
            "file_write",
            "file_replace_lines",
            "run_project_script",
            "preview_start",
            "slides_generate",
            "sheet_generate",
        ):
            assert required in prompt
        assert "never assume port 8000" in prompt
        assert "Do not call `submit_plan` during execution" in prompt


def test_agent_assist_variant_is_distinct_and_autonomous_contract_still_leads() -> None:
    capable = _prompt(OperatingMode.LONG_HORIZON)
    assisted = _prompt(OperatingMode.LONG_HORIZON, assist=True)
    autonomous = _prompt(OperatingMode.LONG_HORIZON, autonomous=True)

    assert capable != assisted
    assert "Call exactly one tool per turn" in assisted
    assert autonomous.startswith("AUTONOMOUS MODE")
    assert "AGENT TASK SHAPE" in autonomous
