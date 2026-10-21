"""Compile plain search text into arXiv's fielded Boolean query language.

The API interprets whitespace-separated terms as OR, even after one ``all:``
field prefix. Preserve plain terms with explicit AND; quoted phrases remain one
term. This is syntax translation, not natural-language keyword extraction.
https://info.arxiv.org/help/api/user-manual.html#51-details-of-query-construction
"""

from __future__ import annotations

import re
import shlex

_FIELD_START = re.compile(
    r"^\s*\(*\s*(?:ti|au|abs|co|jr|cat|rn|id|all|submittedDate|lastUpdatedDate):"
)


def _balanced_expression(query: str) -> bool:
    quoted = False
    escaped = False
    depth = 0
    for char in query:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted and char == "(":
            depth += 1
        elif not quoted and char == ")":
            depth -= 1
            if depth < 0:
                return False
    return not quoted and not escaped and depth == 0


def _arxiv_query(query: str) -> str:
    """Keep explicit fielded syntax, otherwise conjunctively quote plain terms.

    Incomplete quotes/escapes/parentheses cannot become a broader search. Native
    fielded expressions keep their operators, parentheses and escaping verbatim.
    Plain-language instructions are not dropped: a verbose question may have no
    matching paper, rather than matching almost every paper through common words.
    """
    query = query.strip()
    if not query or not _balanced_expression(query):
        return ""
    if _FIELD_START.match(query):
        return query
    lexer = shlex.shlex(query, posix=True)
    lexer.quotes = '"'  # Apostrophes in names/words are literal, not query syntax.
    lexer.whitespace_split = True
    lexer.commenters = ""
    terms = [term.strip(".,?!;:()[]{}") for term in lexer]
    terms = [term for term in terms if any(char.isalnum() for char in term)]
    escaped = [term.replace("\\", "\\\\").replace('"', '\\"') for term in terms]
    return " AND ".join(f'all:"{term}"' for term in escaped)
