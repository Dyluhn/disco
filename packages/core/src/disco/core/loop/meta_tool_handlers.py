"""Composition boundary for the extracted virtual meta-tool handlers."""

from __future__ import annotations

from .meta_tool_common import MetaToolCommonMixin
from .meta_tool_planning import MetaToolPlanningMixin
from .meta_tool_questions import MetaToolQuestionMixin


class MetaToolHandlerMixin(
    MetaToolCommonMixin,
    MetaToolPlanningMixin,
    MetaToolQuestionMixin,
):
    """Ordered handler composition consumed by the compatibility facade."""
