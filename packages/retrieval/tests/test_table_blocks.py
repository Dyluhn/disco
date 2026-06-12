from disco.retrieval.streaming import _to_blocks


def test_single_table():
    text = "| Header 1 | Header 2 |\n| --- | --- |\n| Row 1 Col 1 | Row 1 Col 2 |\n| Row 2 Col 1 | Row 2 Col 2 |"
    blocks = _to_blocks(text)
    assert len(blocks) == 1
    assert blocks[0]["kind"] == "table"
    assert blocks[0]["columns"] == ["Header 1", "Header 2"]
    assert blocks[0]["rows"] == [["Row 1 Col 1", "Row 1 Col 2"], ["Row 2 Col 1", "Row 2 Col 2"]]

def test_table_with_prose():
    text = "Some intro text.\n\n| H1 | H2 |\n|---|---|\n| R1 | R2 |\n\nSome outro text."
    blocks = _to_blocks(text)
    assert len(blocks) == 3
    assert blocks[0]["kind"] == "prose"
    assert blocks[0]["text"] == "Some intro text."
    assert blocks[1]["kind"] == "table"
    assert blocks[1]["columns"] == ["H1", "H2"]
    assert blocks[1]["rows"] == [["R1", "R2"]]
    assert blocks[2]["kind"] == "prose"
    assert blocks[2]["text"] == "Some outro text."

def test_malformed_table_no_divider():
    text = "| H1 | H2 |\n| R1 | R2 |"
    blocks = _to_blocks(text)
    assert len(blocks) == 1
    assert blocks[0]["kind"] == "prose"
    assert "| H1 | H2 |" in blocks[0]["text"]

def test_ragged_rows():
    text = "| H1 | H2 |\n|---|---|\n| R1 |\n| R1 | R2 | R3 |"
    blocks = _to_blocks(text)
    assert len(blocks) == 1
    assert blocks[0]["kind"] == "table"
    assert blocks[0]["columns"] == ["H1", "H2"]
    assert blocks[0]["rows"] == [["R1", ""], ["R1", "R2"]]

def test_adjacent_tables():
    text = "| T1 H1 |\n|---|\n| T1 R1 |\n\n| T2 H1 |\n|---|\n| T2 R1 |"
    blocks = _to_blocks(text)
    assert len(blocks) == 2
    assert blocks[0]["kind"] == "table"
    assert blocks[0]["columns"] == ["T1 H1"]
    assert blocks[1]["kind"] == "table"
    assert blocks[1]["columns"] == ["T2 H1"]

def test_no_data_rows():
    text = "| H1 | H2 |\n|---|---|"
    blocks = _to_blocks(text)
    # If no data rows, we don't treat it as a table in this implementation
    assert len(blocks) == 1
    assert blocks[0]["kind"] == "prose"

def test_table_with_citations():
    text = "| H1 [[s1]] | H2 |\n|---|---|\n| R1 [[s2]] | R2 [[s1]] |"
    blocks = _to_blocks(text)
    assert len(blocks) == 1
    assert blocks[0]["kind"] == "table"
    assert blocks[0]["cited_passage_ids"] == ["s1", "s2"]
