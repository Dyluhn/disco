"""Deterministic search-query compression — sub-question → engine-friendly query.

Deep Research plans sub-questions as full interrogative sentences ("Which
open-source LLM models (including base, instruct, ...) were released between
2026-08-12 and 2026-08-19, and what are their exact model names ...?").  Web
search engines — especially the keyless fallbacks (ddgs, the Brave HTML
scrape) — handle 40-word questions terribly and return SEO/leaderboard noise.

`compress_search_query` strips the interrogative scaffolding and keeps the
salient content terms, so the string that reaches a `SearchProvider` looks
like something a person would type into a search box.  It is pure string
processing — NO LLM call — so the same input always yields the same output
(table-driven tests in ``tests/test_query_compression.py``).

Scope: this shapes ONLY what discovery sees.  The uncompressed sub-question
stays on ``RetrievalRequest.query`` and is what reranking, corpus embedding,
gap reasoning, and synthesis consume (wired in ``engine.DefaultRetrievalEngine
._transform``, the existing Stage-1 query-transformation seam).
"""

from __future__ import annotations

import re

# Quoted phrases are the user's own exact-match hint — always kept verbatim
# (straight or curly quotes), each counting as ONE term against the cap.
_QUOTED = re.compile(r'"[^"]+"|“[^”]+”')

# Parentheticals are elaboration, not search terms ("(including base,
# instruct, and notable fine-tunes/merges)").  Non-nested — planner prose.
_PARENTHETICAL = re.compile(r"\([^()]*\)")

# ISO-ish dates and bare years, used for the range collapse below.
_DATE = r"\d{4}(?:-\d{2}(?:-\d{2})?)?"
# "between D1 and D2" / "from D1 to/until/through D2" → "D1..D2" — one range
# token search engines match, in place of five words of scaffolding.
_DATE_RANGE = re.compile(
    rf"\b(?:between|from)\s+({_DATE})\s+(?:and|to|until|through)\s+({_DATE})\b",
    re.IGNORECASE,
)

# Punctuation stripped from token EDGES only — interior hyphens, dots, and
# slashes survive ("open-source", "2026-08-12..2026-08-19", "fine-tunes").
_EDGE_PUNCT = re.compile(r"^[^\w\"“]+|[^\w\"”]+$")

# Interrogative/functional scaffolding plus planner filler.  Deliberately NOT
# a full NLP stopword list: only words that never help a web search.
_STOPWORDS = frozenset(
    """
    which what who whom whose when where why how
    is are was were be been being am do does did done doing
    have has had having will would shall should can could may might must
    a an the this that these those there here
    it its they them their theirs he she his her him we us our you your i my me
    of in on at by for to from with without into onto over under between among
    during within about as and or nor but if then than so because while
    against per via versus vs
    please exactly exact notable notably significant significantly
    specifically respectively various such etc also well really actually
    include includes included including
    list identify describe explain provide give tell name state
    """.split()
)

# Below this many surviving terms, compression has destroyed the query rather
# than sharpened it — fall back to the (whitespace-normalized) original.
_MIN_TERMS = 3

# Search engines reward focused queries; past roughly a dozen terms the tail
# only dilutes ranking.  Quoted phrases and date ranges count as one term.
_MAX_TERMS = 12

_PLACEHOLDER = "\x00{}\x00"
_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")


def compress_search_query(query: str) -> str:
    """Compress one planned sub-question into a keyword-style search query.

    Deterministic, order-preserving, and conservative: when fewer than
    ``_MIN_TERMS`` content terms survive (the query was already terse), the
    original query is returned unchanged apart from whitespace normalization.
    """
    original = " ".join(query.split())
    if not original:
        return original

    # 1. Protect quoted phrases — replaced by placeholders so the stopword
    #    pass cannot reach inside them, restored verbatim at the end.
    quoted: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        quoted.append(match.group(0))
        return f" {_PLACEHOLDER.format(len(quoted) - 1)} "

    text = _QUOTED.sub(_stash, original)

    # 2. Drop parentheticals; 3. collapse date ranges into one D1..D2 token.
    text = _PARENTHETICAL.sub(" ", text)
    text = _DATE_RANGE.sub(lambda m: f"{m.group(1)}..{m.group(2)}", text)

    # 4. Tokenize, strip edge punctuation, drop scaffolding, dedupe (first
    #    occurrence wins, case-insensitive), preserving original order.
    terms: list[str] = []
    seen: set[str] = set()
    for raw in text.split():
        if _PLACEHOLDER_RE.fullmatch(raw):
            terms.append(raw)  # quoted phrase — kept verbatim, one term
            if len(terms) >= _MAX_TERMS:
                break
            continue
        token = _EDGE_PUNCT.sub("", raw)
        if not token:
            continue
        lowered = token.lower()
        # All-caps tokens are acronyms ("US", "AI", "CPI"), not the function
        # words they collide with lowercase — never stopword-drop them.
        if lowered in _STOPWORDS and not (len(token) >= 2 and token.isupper()):
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        terms.append(token)
        if len(terms) >= _MAX_TERMS:
            break

    if len(terms) < _MIN_TERMS:
        return original

    # 5. Restore quoted phrases verbatim.
    return _PLACEHOLDER_RE.sub(lambda m: quoted[int(m.group(1))], " ".join(terms))
