"""BW-05 (single-bracket citation normalization) + BW-07 (table repair)."""

from disco.retrieval.deep_research.synthesis import (
    _normalize_citations,
    _repair_tables,
)

# ---- BW-05: bare [id] -> [[id]] ----------------------------------------


def test_normalize_promotes_known_single_bracket():
    md = "The market grew sharply [f1161a_p2] last year."
    out = _normalize_citations(md, {"f1161a_p2"})
    assert "[[f1161a_p2]]" in out
    assert "[f1161a_p2]" not in out.replace("[[f1161a_p2]]", "")


def test_normalize_leaves_already_doubled_alone():
    md = "Already cited [[abc_p1]] here."
    out = _normalize_citations(md, {"abc_p1"})
    assert out == md  # no triple-bracket mangling


def test_normalize_leaves_unknown_prose_brackets():
    md = "See footnote [1] and the link [text](http://x) and [note]."
    out = _normalize_citations(md, {"abc_p1"})
    assert out == md  # none of these ids are known passages


def test_normalize_mixed():
    md = "A [src1] and B [[src2]] and prose [aside]."
    out = _normalize_citations(md, {"src1", "src2"})
    assert "[[src1]]" in out
    assert "[[src2]]" in out
    assert "[aside]" in out  # unknown id untouched


def test_normalize_empty_ids_noop():
    md = "Nothing [x] to do."
    assert _normalize_citations(md, set()) == md


# ---- BW-07: malformed table repair -------------------------------------


def test_repair_injects_missing_delimiter_and_pipes():
    # No leading/trailing pipes, NO |---| delimiter row -> remark would fail.
    md = "Option | Pros | Cons\nA | fast | costly\nB | cheap | slow"
    out = _repair_tables(md)
    lines = [ln for ln in out.split("\n") if ln.strip()]
    assert lines[0] == "| Option | Pros | Cons |"
    # second line must now be a delimiter row
    assert set(lines[1].replace("|", "").replace(" ", "")) <= {"-"}
    assert "| A | fast | costly |" in out
    assert "| B | cheap | slow |" in out


def test_repair_preserves_valid_table():
    md = "| A | B |\n| --- | --- |\n| 1 | 2 |"
    out = _repair_tables(md)
    assert "| A | B |" in out
    assert "| 1 | 2 |" in out
    # exactly one delimiter row
    assert out.count("---") >= 1


def test_repair_leaves_prose_with_single_pipe():
    md = "Throughput is measured in tokens | second in this benchmark."
    out = _repair_tables(md)
    assert out == md  # lone pipe line is not a table


def test_repair_does_not_touch_code_fences():
    md = "```\na | b | c\n```\nafter"
    out = _repair_tables(md)
    assert out == md


def test_repair_pads_ragged_rows():
    md = "H1 | H2 | H3\nx | y"
    out = _repair_tables(md)
    assert "| x | y |  |" in out  # padded to 3 columns
