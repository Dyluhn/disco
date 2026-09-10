"""Chart rendering safety: the streaming block parser (`_to_blocks`) and the
report writer's chart validation (`_writer_parts.validate_charts`), which
degrades a broken ```chart block to a table rather than shipping broken JSON
to the UI."""

import json

from disco.retrieval.deep_research._writer_parts import validate_charts
from disco.retrieval.streaming import _to_blocks


def test_bar_chart_block():
    payload = {
        "chart_type": "bar",
        "title": "Test Chart",
        "data": [{"label": "A", "value": 10}, {"label": "B", "value": 20}],
    }
    text = f"```chart\n{json.dumps(payload)}\n```"
    blocks = _to_blocks(text)
    assert len(blocks) == 1
    assert blocks[0]["kind"] == "chart"
    assert blocks[0]["chart_type"] == "bar"
    assert blocks[0]["data"] == payload["data"]
    assert blocks[0]["title"] == "Test Chart"


def test_chart_with_citations_inside():
    # Citations inside the JSON string (though unusual, _to_blocks should find them)
    text = '```chart\n{"chart_type": "line", "data": [{"label": "A [[src1]]", "value": 10}]}\n```'
    blocks = _to_blocks(text)
    assert len(blocks) == 1
    assert blocks[0]["kind"] == "chart"
    assert "src1" in blocks[0]["cited_passage_ids"]


def test_invalid_chart_json_degrade_to_code():
    text = "```chart\n{invalid json}\n```"
    blocks = _to_blocks(text)
    assert len(blocks) == 1
    assert blocks[0]["kind"] == "code"
    assert blocks[0]["language"] == "chart"


def test_validate_charts_valid():
    payload = {"chart_type": "bar", "data": [{"label": "A", "value": 10}]}
    md = f"Intro\n\n```chart\n{json.dumps(payload)}\n```\nOutro"
    valid_md = validate_charts(md)
    assert "```chart" in valid_md
    assert "Intro" in valid_md
    assert "Outro" in valid_md


def test_validate_charts_invalid_degrade_to_table():
    # 'wrong' instead of 'value'
    payload = {"chart_type": "bar", "data": [{"label": "A", "wrong": 10}]}
    md = f"```chart\n{json.dumps(payload)}\n```"
    valid_md = validate_charts(md)
    assert "```chart" not in valid_md
    assert "|" in valid_md
    assert "Label" in valid_md
    assert "Value" in valid_md


def test_scatter_chart_roundtrip():
    data = [{"x": 1, "y": 2, "group": "A"}, {"x": 2, "y": 3, "group": "B"}]
    payload = {"chart_type": "scatter", "data": data}
    text = f"```chart\n{json.dumps(payload)}\n```"
    blocks = _to_blocks(text)
    assert blocks[0]["kind"] == "chart"
    assert blocks[0]["chart_type"] == "scatter"
    assert blocks[0]["data"] == data


def test_validate_charts_malformed_json_drop():
    md = "```chart\n{bad}\n```"
    valid_md = validate_charts(md)
    assert "```chart" not in valid_md
    assert valid_md.strip() == ""
