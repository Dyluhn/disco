"""Leaked <think> reasoning must never reach the grounded answer (blocks, claims,
or the token stream)."""

from disco.retrieval.streaming import _strip_think_spans


def test_strip_closed_span_prefix():
    assert _strip_think_spans("<think>plan</think>Real answer [[a1]].") == "Real answer [[a1]]."


def test_strip_mid_text_span():
    assert _strip_think_spans("Intro. <think>mid</think> More.") == "Intro. More."


def test_unclosed_trailing_think_dropped():
    assert _strip_think_spans("Answer done. <think>ran out of budg") == "Answer done."


def test_clean_text_unchanged():
    assert _strip_think_spans("No think here.") == "No think here."
