"""Explicit Search defaults must not resurrect a different sticky model."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from disco.agent_server.routes.ws import _parse_research_request


@pytest.mark.parametrize(
    ("selection", "expected", "sticky_reads"),
    [({}, "previously-picked", 1), ({"model_override": None}, None, 0),
     ({"model_override": "configured-default"}, "configured-default", 0)],
)
def test_research_model_selection_distinguishes_omitted_from_explicit_default(
    selection, expected, sticky_reads
):
    sticky = Mock(return_value="previously-picked")
    runtime = SimpleNamespace(
        settings=SimpleNamespace(model_binding=SimpleNamespace(get_last_selected_model=sticky))
    )
    request = _parse_research_request({"query": "test", **selection}, runtime)
    assert request["model_override"] == expected
    assert sticky.call_count == sticky_reads


def test_research_default_without_runtime_does_not_need_sticky_storage():
    assert _parse_research_request({"model_override": None}, None)["model_override"] is None
