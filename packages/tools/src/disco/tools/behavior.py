"""Small constructors for explicit reliability behavior declarations.

The call sites remain the source of truth: each tool names every capability it
may exercise beside its :class:`~disco.tools.anatomy.ToolDef`.  These helpers
only remove repetitive Pydantic boilerplate; they never infer behavior from a
tool name, risk tier, execution scope, or legacy ``read_only`` flag.
"""

from __future__ import annotations

from disco.core.effects import ActionProfile, EffectCapability, ToolBehavior


def declares(
    *capabilities: EffectCapability,
    planner_safe: bool,
) -> ToolBehavior:
    """Build one explicit, immutable static behavior declaration."""

    return ToolBehavior(
        planner_safe=planner_safe,
        possible_capabilities=frozenset(capabilities),
    )


def narrows(*capabilities: EffectCapability) -> ActionProfile:
    """Build an invocation profile for an argument-aware mixed tool."""

    return ActionProfile(capabilities=frozenset(capabilities))


OPAQUE_MCP_BEHAVIOR = declares(EffectCapability.OPAQUE_EXECUTE, planner_safe=False)


__all__ = ["OPAQUE_MCP_BEHAVIOR", "declares", "narrows"]
