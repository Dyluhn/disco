"""Canonical <think>-span stripping for driver output.

Reasoning-class drivers (MiniMax-M3 et al.) can leak inline ``<think>…</think>``
spans into *content* channels. Reasoning is never part of a rendered document,
a parsed plan, a search query, or a report section — every consumer of raw
``resp.text`` must strip it before parsing. This module is the single home for
that rule; do not re-implement the regex at call sites.

Two shapes are handled:
- closed spans anywhere in the text (``<think>…</think>``), and
- an UNCLOSED trailing ``<think>`` (token budget ran out mid-thought), which
  drops everything to the end of the text.
"""

from __future__ import annotations

import re

_THINK_SPAN_RE = re.compile(r"<think\b[^>]*>.*?</think>\s*", re.IGNORECASE | re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think\b[^>]*>.*\Z", re.IGNORECASE | re.DOTALL)


def strip_think_spans(text: str, *, keep_edge_whitespace: bool = False) -> str:
    """Remove inline <think>…</think> reasoning a driver leaks into content.

    ``keep_edge_whitespace=True`` preserves leading/trailing whitespace of the
    remainder — for accumulation paths where a trailing cut boundary is
    load-bearing (e.g. synthesis truncation-continue).
    """
    text = _THINK_SPAN_RE.sub("", text)
    text = _THINK_OPEN_RE.sub("", text)
    return text if keep_edge_whitespace else text.strip()
