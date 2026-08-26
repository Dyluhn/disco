"""Citation numbering — the SAME ``[n]`` assignment the UI shows.

The frontend renders every citation as a numeral computed by
``citationNumbers`` in ``frontend/src/lib/sources.ts``: a passage's number is
the position of its WORK in first-seen ``source_url`` order over
``report.passages`` (one number per work; every passage from that work
shares it; a passage with no URL numbers alone, keyed by its id).  Exports
used to leak raw passage ids (``[[0b3199_p2]]``) and the PDF renumbered by
raw passage index — identifiers the user never saw.  This module is the
Python half of the py↔ts pair (the ``_report_normalize`` precedent): both
serializers present the numbering the user already knows from the UI.

``_source_url_key`` mirrors ``sourceUrlKey`` (lib/sources.ts) — the URL
identity that collapses tracking params, default ports and trailing slashes.
``_canonical_work_key`` adds only explicit DOI, arXiv, and PMID identities for
cross-host mirrors.  Known, accepted divergences (all degrade to the
pre-fix behaviour of numbering a source twice, never to a wrong merge):
Python does not IDNA-encode hosts, resolve ``/../`` dot segments, or
re-encode unusual path bytes the way WHATWG ``URL`` does.  Query pairs sort
by codepoint on both sides.

Private decomposition of report_export.py; report_export re-exports what its
serializers need, so the public seam stays ``disco.agent_server.report_export``.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

_TRACKING_QUERY_KEYS = frozenset(
    {"fbclid", "gclid", "mc_cid", "mc_eid", "msclkid", "msockid"}
)
_DOI_RE = re.compile(r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?(10\.\d{4,9}/[^\s<>\"']+)", re.I)
_ARXIV_RE = re.compile(
    r"(?:arxiv(?::|\.org/(?:abs/|pdf/|html/))|/(?:abs|pdf|html)/)([a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?(?:$|[/?#])",
    re.I,
)
_PMID_RE = re.compile(
    r"(?:pubmed\.ncbi\.nlm\.nih\.gov/|ncbi\.nlm\.nih\.gov/pubmed/|pmid[:/])([0-9]+)",
    re.I,
)

# WHATWG application/x-www-form-urlencoded safe set — what URLSearchParams
# .toString() leaves unescaped (alnum + "*-._", space → "+").
_FORM_SAFE = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789*-._"
)


def _form_encode(value: str) -> str:
    """Encode one query key/value like URLSearchParams.toString() does."""
    out: list[str] = []
    for byte in value.encode("utf-8"):
        ch = chr(byte)
        if ch in _FORM_SAFE:
            out.append(ch)
        elif ch == " ":
            out.append("+")
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


def _url_query_suffix(query: str) -> str:
    """Filtered, sorted, re-encoded ``?…`` suffix (empty when nothing survives)."""
    pairs = sorted(
        (k, v)
        for k, v in parse_qsl(query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_QUERY_KEYS
    )
    if not pairs:
        return ""
    return "?" + "&".join(f"{_form_encode(k)}={_form_encode(v)}" for k, v in pairs)


def _source_url_key(raw: str) -> str:
    """Conservative URL identity — mirrors ``sourceUrlKey`` in lib/sources.ts.

    Original URLs remain untouched for display; this key only groups passages
    that come from the same URL.  Unparseable / relative URLs fall back to
    the trimmed raw string, exactly like the TS ``catch``.
    """
    value = raw.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port  # property access can raise ValueError
    except ValueError:
        return value
    host = (parsed.hostname or "").lower().rstrip(".")
    if not parsed.scheme or not host:
        return value
    authority = f"{host}:{port}" if port and port not in (80, 443) else host
    path = parsed.path.rstrip("/") or "/"
    return f"{authority}{path}{_url_query_suffix(parsed.query)}"


def _canonical_work_key(raw: str) -> str:
    """Strong work identity, with conservative URL fallback.

    Passage URLs remain display/deep-link values.  DOI, arXiv and PMID resolver
    variants identify the same work; titles are intentionally never compared.
    """
    value = unquote(raw.strip())
    if not value:
        return "url:"
    match = _DOI_RE.search(value)
    if match:
        return f"doi:{match.group(1).split('?', 1)[0].split('#', 1)[0].rstrip('.,;:)]}').lower()}"
    match = _ARXIV_RE.search(value)
    if match:
        return f"arxiv:{match.group(1).lower()}"
    match = _PMID_RE.search(value)
    if match:
        return f"pmid:{match.group(1)}"
    return f"url:{_source_url_key(value)}"


def _passage_id(p: dict[str, Any]) -> str:
    """``String(p.id ?? "?")`` — the id string the footer always rendered."""
    pid = p.get("id")
    return "?" if pid is None else str(pid)


def _citation_numbers(passages: list[dict[str, Any]]) -> dict[str, int]:
    """passage-id → 1-based citation numeral — the UI's ``[n]`` assignment.

    A passage's number is the position of its work (``_canonical_work_key``) in
    first-seen order over ``passages``; passages from one source share the
    number.  A passage with no URL gets its own number, keyed by its id.
    Mirrors ``citationNumbers`` (lib/sources.ts) / ``reportCitationNumbers``
    (api/deepResearch.ts) — the parity tests pin all three to the same
    assignment.
    """
    by_source: dict[str, int] = {}
    numbers: dict[str, int] = {}
    for p in passages:
        pid = _passage_id(p)
        url = str(p.get("source_url") or "")
        key = _canonical_work_key(url) if url else f"#{pid}"
        n = by_source.get(key)
        if n is None:
            n = len(by_source) + 1
            by_source[key] = n
        numbers[pid] = n
    return numbers


def _cited_source_rows(
    passages: list[dict[str, Any]], numbers: dict[str, int]
) -> list[tuple[int, str, str]]:
    """One ``(n, title, url)`` row per citation number, in numeric order.

    The row carries the FIRST passage seen for that source — the same row the
    UI's Sources panel shows (deriveSourceTiers keeps the first passage per
    URL).  This is the footer/appendix body: ``[n] Title — URL``.
    """
    rows: dict[int, tuple[int, str, str]] = {}
    for p in passages:
        n = numbers.get(_passage_id(p))
        if n is None or n in rows:
            continue
        rows[n] = (n, str(p.get("source_title", "")), str(p.get("source_url", "")))
    return [rows[n] for n in sorted(rows)]
