"""Workflow output-contract path templating."""

from __future__ import annotations


class _WorkflowPathParams(dict[str, object]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_workflow_output_path(template: str, params: dict[str, object]) -> str:
    values = _WorkflowPathParams(
        {key: "" if value is None else value for key, value in params.items()}
    )
    try:
        return template.format_map(values)
    except (IndexError, KeyError, ValueError):
        return template
