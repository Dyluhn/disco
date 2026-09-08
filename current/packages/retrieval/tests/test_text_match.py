"""Locating a phrase in text a machine extracted from a page.

Extraction damages text in ways a reader does not see: it puts spaces inside
words, leaves soft hyphens where a line broke, and markdown emphasis wraps a
unit in underscores. A person quoting the page quotes what the page says, so
the matcher has to ignore what the extraction added and nothing else. What it
must never ignore is a character that changes a number: "10-6 cm/s" is ten to
the minus six, and "3*4" is not "34".
"""

from __future__ import annotations

from disco.retrieval.text_match import locate, normalized_form, normalized_index


def find(needle: str, text: str):
    return locate(needle, normalized_index(text))


def test_a_unit_wrapped_in_markdown_emphasis_is_still_found() -> None:
    text = "| Electric Smooth Element | 207 _kWh/year_ |"

    span = find("207 kWh", text)

    assert span is not None
    assert text[span[0] : span[1]] == "207 _kWh"


def test_a_word_the_extraction_split_is_found_either_way_it_is_spelled() -> None:
    text = "Overall, the PPs reduced the total volume of stormwater ou tflow by 42%."

    assert find("ou tflow by 42", text) is not None
    assert find("outflow by 42", text) is not None
    assert find("outflow by 42", text) == find("ou tflow by 42", text)


def test_a_soft_hyphen_and_a_typographic_dash_do_not_hide_a_phrase() -> None:
    text = "Rec­ommendations for five–hour duration at 71 % efficiency."

    assert find("Recommendations", text) is not None
    assert find("five-hour duration", text) is not None
    assert find("71 % efficiency", text) is not None


def test_a_hyphen_is_never_ignored_because_it_changes_the_number() -> None:
    text = "Conductivity ranges between 10-6 and 10-4 cm/s."

    assert find("10-6", text) is not None
    assert find("106", text) is None


def test_an_operator_between_digits_is_not_emphasis() -> None:
    assert find("34", "The product 3*4 is twelve.") is None
    assert find("snake_case", "A snake_case identifier.") is not None


def test_a_phrase_the_text_does_not_contain_is_not_found() -> None:
    assert find("207 MWh", "| Electric Smooth Element | 207 _kWh/year_ |") is None


def test_the_normalised_form_is_what_a_not_found_report_can_quote() -> None:
    assert normalized_form("207 _kWh/year_") == "207kWh/year"
