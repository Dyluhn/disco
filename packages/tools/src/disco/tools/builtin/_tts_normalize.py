"""Text normalization for speech synthesis.

The normalizer is intentionally provider-agnostic: it turns markdown-ish text
into speakable prose before any local or remote TTS engine sees it.  It keeps
ordinary prose stable, preserves numeric/date/time digits, and focuses on
artifacts that TTS engines tend to read literally.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class UnitExpansion:
    token: str
    singular: str
    plural: str


UNIT_EXPANSIONS: tuple[UnitExpansion, ...] = (
    UnitExpansion("km/h", "kilometer per hour", "kilometers per hour"),
    UnitExpansion("mph", "mile per hour", "miles per hour"),
    UnitExpansion("lbs", "pound", "pounds"),
    UnitExpansion("lb", "pound", "pounds"),
    UnitExpansion("kg", "kilogram", "kilograms"),
    UnitExpansion("g", "gram", "grams"),
    UnitExpansion("mg", "milligram", "milligrams"),
    UnitExpansion("oz", "ounce", "ounces"),
    UnitExpansion("ft", "foot", "feet"),
    UnitExpansion("in", "inch", "inches"),
    UnitExpansion("km", "kilometer", "kilometers"),
    UnitExpansion("cm", "centimeter", "centimeters"),
    UnitExpansion("mm", "millimeter", "millimeters"),
    UnitExpansion("mi", "mile", "miles"),
    UnitExpansion("%", "percent", "percent"),
    UnitExpansion("°f", "degree Fahrenheit", "degrees Fahrenheit"),
    UnitExpansion("°c", "degree Celsius", "degrees Celsius"),
)

_UNIT_BY_TOKEN = {rule.token: rule for rule in UNIT_EXPANSIONS}
_UNIT_ALTERNATION = "|".join(
    re.escape(rule.token).replace("°", r"°\s*")
    for rule in sorted(UNIT_EXPANSIONS, key=lambda item: len(item.token), reverse=True)
)

_NUMBER_RE = r"\d+(?:,\d{3})*(?:\.\d+)?"
_MONEY_RE = re.compile(
    rf"\$(?P<number>{_NUMBER_RE})"
    r"(?:\s*(?P<magnitude>thousand|million|billion|trillion|[kKmMbBtT]))?\b"
)
_UNIT_RE = re.compile(
    rf"(?P<number>\b{_NUMBER_RE})\s*"
    rf"(?P<unit>{_UNIT_ALTERNATION})(?=\b|[^\w/]|$)",
    re.IGNORECASE,
)

_CODE_FENCE_RE = re.compile(r"(?ms)^[ \t]*(```|~~~)[^\n]*\n.*?^[ \t]*\1[ \t]*$")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_REFERENCE_LINK_RE = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
_CHIP_CITATION_RE = re.compile(r"\[\[[^\]]+\]\]")
_FOOTNOTE_DEFINITION_RE = re.compile(r"^[ \t]*\[\^?\d+\]:.*$", re.MULTILINE)
_FOOTNOTE_MARKER_RE = re.compile(r"\s*\[(?:\d{1,3})\]")
_URL_RE = re.compile(r"(?P<url>https?://[^\s<>()\[\]]+|www\.[^\s<>()\[\]]+)")
_HTML_AUTOLINK_RE = re.compile(r"<(https?://[^>]+)>")
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(?P<text>.*?)[ \t#]*$")
_BLOCKQUOTE_RE = re.compile(r"^[ \t]*>[ \t]?")
_HR_RE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$")
_LIST_RE = re.compile(r"^[ \t]*(?:[-+*•]|\d{1,3}[.)]|[A-Za-z][.)])[ \t]+(?P<item>.+)$")
_SENTENCE_END_RE = re.compile(r"""[.!?]["')\]]*$""")
_EMOJI_RE = re.compile(
    "["
    "\U0001f1e6-\U0001f1ff"
    "\U0001f300-\U0001f5ff"
    "\U0001f600-\U0001f64f"
    "\U0001f680-\U0001f6ff"
    "\U0001f700-\U0001f77f"
    "\U0001f780-\U0001f7ff"
    "\U0001f800-\U0001f8ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001faff"
    "\u2600-\u27bf"
    "\ufe0f"
    "]+"
)

_EMPHASIS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\*\*\*([^*\n]+?)\*\*\*"),
    re.compile(r"___([^_\n]+?)___"),
    re.compile(r"\*\*([^*\n]+?)\*\*"),
    re.compile(r"__([^_\n]+?)__"),
    re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)"),
    re.compile(r"(?<!\w)_([^_\n]+?)_(?!\w)"),
)

_ABBREVIATIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\be\.g\.", re.IGNORECASE), "for example"),
    (re.compile(r"\bi\.e\.", re.IGNORECASE), "that is"),
    (re.compile(r"\betc\.", re.IGNORECASE), "et cetera"),
    (re.compile(r"\bvs\.?(?=\s|$|[,:;!?])", re.IGNORECASE), "versus"),
)

_MAGNITUDES = {
    "k": "thousand",
    "m": "million",
    "b": "billion",
    "t": "trillion",
    "thousand": "thousand",
    "million": "million",
    "billion": "billion",
    "trillion": "trillion",
}


def normalize_tts_text(text: str) -> str:
    """Return speakable text for TTS while keeping clean prose stable.

    Markdown markers, citation brackets, bare URLs, emoji, repeated punctuation,
    common abbreviations, and unit symbols are normalized.  Numbers, dates, times,
    quotes, and apostrophes are preserved as plain text for the TTS engine.
    """
    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = _CODE_FENCE_RE.sub("", text)
    text = _FOOTNOTE_DEFINITION_RE.sub("", text)
    text = _IMAGE_RE.sub(lambda match: match.group(1).strip(), text)
    text = _LINK_RE.sub(lambda match: match.group(1), text)
    text = _REFERENCE_LINK_RE.sub(lambda match: match.group(1), text)
    text = _INLINE_CODE_RE.sub(_replace_inline_code, text)
    text = _CHIP_CITATION_RE.sub("", text)
    text = _FOOTNOTE_MARKER_RE.sub("", text)
    text = _HTML_AUTOLINK_RE.sub(lambda match: _domain_for_url(match.group(1)), text)
    text = _URL_RE.sub(_replace_url, text)
    text = _EMOJI_RE.sub("", text)
    text = text.replace("&", " and ").replace("~", " about ")
    text = text.replace("—", ", ").replace("–", ", ")
    text = text.replace("…", ".")
    text = _MONEY_RE.sub(_replace_money, text)
    text = _UNIT_RE.sub(_replace_unit, text)
    text = _expand_abbreviations(text)
    text = _strip_emphasis(text)
    text = _linearize_tables(text)
    text = _normalize_lines(text)
    text = _normalize_punctuation(text)
    text = _collapse_whitespace(text)
    return text.strip()


def _replace_inline_code(match: re.Match[str]) -> str:
    inner = match.group(1).strip()
    if len(inner) <= 40:
        return inner
    return "code example omitted"


def _strip_emphasis(text: str) -> str:
    previous = None
    while previous != text:
        previous = text
        for pattern in _EMPHASIS_PATTERNS:
            text = pattern.sub(r"\1", text)
    return text


def _replace_money(match: re.Match[str]) -> str:
    number = match.group("number")
    magnitude = match.group("magnitude")
    amount = number
    if magnitude:
        amount = f"{number} {_MAGNITUDES[magnitude.lower()]}"
    noun = "dollar" if _is_one(number) and not magnitude else "dollars"
    return f"{amount} {noun}"


def _replace_unit(match: re.Match[str]) -> str:
    number = match.group("number")
    unit = re.sub(r"\s+", "", match.group("unit").lower())
    rule = _UNIT_BY_TOKEN[unit]
    spoken = rule.singular if _is_one(number) else rule.plural
    return f"{number} {spoken}"


def _is_one(number: str) -> bool:
    try:
        return float(number.replace(",", "")) == 1.0
    except ValueError:
        return False


def _expand_abbreviations(text: str) -> str:
    for pattern, replacement in _ABBREVIATIONS:
        text = pattern.sub(replacement, text)
    return text


def _replace_url(match: re.Match[str]) -> str:
    raw = match.group("url")
    stripped = raw.rstrip(".,!?;:")
    trailing = raw[len(stripped) :]
    domain = _domain_for_url(stripped)
    return f"{domain}{trailing}" if domain else trailing


def _domain_for_url(raw: str) -> str:
    url = raw if re.match(r"https?://", raw, re.IGNORECASE) else f"https://{raw}"
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _linearize_tables(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if "|" not in line:
            out.append(line)
            i += 1
            continue

        block: list[str] = []
        while i < len(lines) and "|" in lines[i]:
            block.append(lines[i])
            i += 1

        linearized = _linearize_table_block(block)
        if linearized is None:
            out.extend(block)
        else:
            out.extend(linearized)
    return "\n".join(out)


def _parse_table_rows(block: list[str]) -> list[list[str]] | None:
    """Split each line into cells; bail (return None) if the block isn't a
    genuine table — too short, or any row has fewer than 2 cells."""
    if len(block) < 2:
        return None
    rows = [_split_table_row(line) for line in block]
    if any(len(row) < 2 for row in rows):
        return None
    return rows


def _table_headers_and_data_rows(rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """Detect an optional header row (row[1] is a ``---`` separator) and split
    `rows` into ``(headers, data_rows)``."""
    separator_index = 1 if _is_table_separator(rows[1]) else -1
    if separator_index == 1:
        headers = rows[0]
        data_rows = rows[2:]
    else:
        headers = []
        data_rows = rows
    return headers, data_rows


def _spoken_table_row(row: list[str], headers: list[str], column_count: int) -> str | None:
    """Render one data row as a spoken fragment, or None if it carries no
    speakable content.  Mirrors the original if/elif/elif precedence: once the
    headers case applies, the other two shapes are never considered — even if
    it yields no parts."""
    cells = row[:column_count]
    if headers and len(headers) == len(cells):
        parts = [
            f"{header}: {cell}"
            for header, cell in zip(headers, cells, strict=False)
            if header and cell
        ]
        if parts:
            return _ensure_sentence_end("; ".join(parts))
        return None
    if len(cells) == 2 and cells[0] and cells[1]:
        return _ensure_sentence_end(f"{cells[0]}: {cells[1]}")
    if cells:
        return _ensure_sentence_end("; ".join(cell for cell in cells if cell))
    return None


def _linearize_table_block(block: list[str]) -> list[str] | None:
    rows = _parse_table_rows(block)
    if rows is None:
        return None

    headers, data_rows = _table_headers_and_data_rows(rows)

    column_count = len(headers or data_rows[0])
    if column_count > 4:
        return []

    spoken_rows: list[str] = []
    for row in data_rows:
        spoken = _spoken_table_row(row, headers, column_count)
        if spoken is not None:
            spoken_rows.append(spoken)
    return spoken_rows


def _split_table_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [_strip_emphasis(cell.strip()) for cell in line.split("|")]


def _is_table_separator(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-{2,}:?", cell.strip()) for cell in cells if cell.strip())


def _normalize_lines(text: str) -> str:
    normalized: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            normalized.append("")
            continue
        if _HR_RE.fullmatch(line):
            normalized.append("")
            continue
        line = _BLOCKQUOTE_RE.sub("", line).strip()
        heading = _HEADING_RE.match(line)
        if heading:
            line = heading.group("text").strip()
        list_item = _LIST_RE.match(line)
        if list_item:
            line = _ensure_sentence_end(list_item.group("item").strip())
        normalized.append(line)
    return "\n".join(normalized)


def _normalize_punctuation(text: str) -> str:
    text = re.sub(r"\.{3,}", ".", text)
    text = re.sub(r"\s*\.\s*\.\s*\.", ".", text)
    text = re.sub(r"!{2,}", "!", text)
    text = re.sub(r"\?{2,}", "?", text)
    text = re.sub(r"([!?])(?:[!?])+", r"\1", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text


def _collapse_whitespace(text: str) -> str:
    paragraphs: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if not line:
            if current:
                paragraphs.append(_ensure_sentence_end(" ".join(current)))
                current = []
            continue
        current.append(line)
    if current:
        paragraphs.append(_ensure_sentence_end(" ".join(current)))
    return " ".join(paragraphs)


def _ensure_sentence_end(text: str) -> str:
    text = text.strip()
    if not text or _SENTENCE_END_RE.search(text):
        return text
    return f"{text}."


__all__ = ["UNIT_EXPANSIONS", "UnitExpansion", "normalize_tts_text"]
