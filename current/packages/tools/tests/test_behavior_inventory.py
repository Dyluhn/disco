"""K3: production behavior inventory and mixed-call classifiers."""

from __future__ import annotations

import ast
from pathlib import Path

from disco.core.effects import ActionProfile, EffectCapability, ToolBehavior
from disco.tools.behavior import OPAQUE_MCP_BEHAVIOR
from disco.tools.builtin import build_default_registry
from disco.tools.builtin.app_kit import APPKIT_V2_TOOLS
from disco.tools.builtin.browser import BrowserArgs, BrowserTool
from disco.tools.builtin.context_memory import ContextMemoryArgs, ContextMemoryTool
from disco.tools.builtin.design_lint import DesignLintTool
from disco.tools.builtin.preview import PreviewStartArgs, PreviewStartTool
from disco.tools.builtin.request_custom_build import RequestCustomBuildTool
from disco.tools.builtin.run_script import RunProjectScriptTool, RunScriptArgs
from disco.tools.builtin.shell_sessions import ShellExecTool, ShellWriteTool
from disco.tools.builtin.system import CodeExecTool, ShellTool
from disco.tools.builtin.verify_appkit_app import VerifyAppKitAppTool
from disco.tools.builtin.workflow_tools import (
    DraftWorkflowTool,
    EnterWorkflowTool,
    ListWorkflowsTool,
    ReadWorkflowCardTool,
    WorkflowAbortTool,
)
from disco.tools.mcp.tool_search import meta_tool_search
from disco.tools.registry import ToolScope


def _profile(*capabilities: EffectCapability) -> ActionProfile:
    return ActionProfile(capabilities=frozenset(capabilities))


def test_default_registry_has_exhaustive_behavior_with_planner_parity() -> None:
    registry = build_default_registry()
    # Test the entire registry rather than one product surface.
    tools = registry.in_scope(ToolScope(allowed_tools=registry.names()))
    assert len(tools) == len(registry.names())
    assert all(tool.definition.behavior is not None for tool in tools)
    assert all(
        tool.definition.behavior is not None
        and tool.definition.behavior.planner_safe == tool.definition.read_only
        for tool in tools
    )


def test_strict_appkit_and_workflow_additions_are_classified() -> None:
    strict_classes = (
        *APPKIT_V2_TOOLS,
        DesignLintTool,
        RequestCustomBuildTool,
        VerifyAppKitAppTool,
    )
    definitions = [tool_class.definition for tool_class in strict_classes]
    definitions.extend(
        tool_class.definition
        for tool_class in (
            ListWorkflowsTool,
            ReadWorkflowCardTool,
            EnterWorkflowTool,
            WorkflowAbortTool,
            DraftWorkflowTool,
        )
    )
    assert all(definition.behavior is not None for definition in definitions)
    assert all(
        definition.behavior is not None and definition.behavior.planner_safe == definition.read_only
        for definition in definitions
    )


def test_mcp_defaults_opaque_while_tool_search_is_observation() -> None:
    assert OPAQUE_MCP_BEHAVIOR == ToolBehavior(
        planner_safe=False,
        possible_capabilities=frozenset({EffectCapability.OPAQUE_EXECUTE}),
    )
    assert meta_tool_search().behavior == ToolBehavior(
        planner_safe=True,
        possible_capabilities=frozenset({EffectCapability.EXTERNAL_OBSERVE}),
    )


def test_browser_classifier_separates_observation_from_interaction() -> None:
    for action in ("navigate", "screenshot", "back", "console_view"):
        args = BrowserArgs(action=action)
        assert BrowserTool().action_profile(args) == _profile(EffectCapability.WEB_OBSERVE)
    for action in ("click", "press", "fill", "submit"):
        args = BrowserArgs(action=action)
        assert BrowserTool().action_profile(args) == _profile(
            EffectCapability.WEB_OBSERVE,
            EffectCapability.OPAQUE_EXECUTE,
        )

    # A future action is broad by default; adding an observation-only action
    # requires an explicit owner-reviewed allowlist change.
    future_payload: dict[str, object] = {"action": "download"}
    future = BrowserArgs.model_construct(**future_payload)
    assert BrowserTool().action_profile(future) == _profile(
        EffectCapability.WEB_OBSERVE,
        EffectCapability.OPAQUE_EXECUTE,
    )


def test_context_memory_classifier_is_operation_aware() -> None:
    tool = ContextMemoryTool()
    assert tool.action_profile(ContextMemoryArgs(action="read")) == _profile(
        EffectCapability.WORKSPACE_CONTENT_READ
    )
    assert tool.action_profile(ContextMemoryArgs(action="list")) == _profile(
        EffectCapability.WORKSPACE_INVENTORY_READ
    )
    assert tool.action_profile(ContextMemoryArgs(action="write")) == _profile(
        EffectCapability.WORKSPACE_MUTATE
    )


def test_run_project_script_classifier_unions_supplied_operations() -> None:
    args = RunScriptArgs.model_validate(
        {
            "operations": [
                {"op": "read", "path": "a.txt"},
                {"op": "ls", "path": "."},
                {"op": "replace_text", "path": "a.txt", "old": "a", "new": "b"},
            ]
        }
    )
    assert RunProjectScriptTool().action_profile(args) == _profile(
        EffectCapability.WORKSPACE_CONTENT_READ,
        EffectCapability.WORKSPACE_INVENTORY_READ,
        EffectCapability.WORKSPACE_MUTATE,
    )


def test_preview_start_classifier_keeps_unknown_future_frameworks_flexible() -> None:
    tool = PreviewStartTool()
    expected_static = _profile(
        EffectCapability.PROCESS_CONTROL,
        EffectCapability.PROCESS_OUTPUT_READ,
    )
    assert tool.action_profile(PreviewStartArgs(serve_dir="dist")) == expected_static
    assert tool.action_profile(PreviewStartArgs(framework="static")) == expected_static
    assert tool.action_profile(PreviewStartArgs(framework="http")) == expected_static

    expected_opaque = _profile(
        EffectCapability.PROCESS_CONTROL,
        EffectCapability.PROCESS_OUTPUT_READ,
        EffectCapability.OPAQUE_EXECUTE,
        EffectCapability.WORKSPACE_MUTATE,
    )
    assert tool.action_profile(PreviewStartArgs(command="npm start")) == expected_opaque
    assert tool.action_profile(PreviewStartArgs(framework="future-kit")) == expected_opaque


def test_opaque_workspace_executors_can_carry_exact_host_mutation_receipts() -> None:
    for tool_class in (ShellTool, CodeExecTool, ShellExecTool, ShellWriteTool):
        behavior = tool_class.definition.behavior
        assert behavior is not None
        assert EffectCapability.OPAQUE_EXECUTE in behavior.possible_capabilities
        assert EffectCapability.WORKSPACE_MUTATE in behavior.possible_capabilities


def test_every_production_tool_constructor_declares_behavior() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    missing: list[str] = []
    for path in sorted(repo_root.glob("current/packages/*/src/**/*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else ""
            )
            if name not in {"ToolDef", "ToolSpec"}:
                continue
            if not any(keyword.arg == "behavior" for keyword in node.keywords):
                missing.append(f"{path.relative_to(repo_root)}:{node.lineno}:{name}")
    assert missing == []


def test_production_cannot_use_the_unclassified_test_escape() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    violations: list[str] = []
    for path in sorted(repo_root.glob("current/packages/*/src/**/*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "allow_unclassified_for_testing":
                    continue
                if not isinstance(keyword.value, ast.Constant) or keyword.value.value is not False:
                    violations.append(f"{path.relative_to(repo_root)}:{node.lineno}")
    assert violations == []
