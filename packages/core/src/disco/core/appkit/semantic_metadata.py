"""data-disco-* semantic metadata conventions (P8B).

ONE canonical vocabulary of the ``data-disco-*`` HTML attributes that make AppKit output
addressable by semantic reference (P8A) — for the frontend resolver (P8C) and the product
oracles (P8D). The renderer emits THROUGH this module, so the attribute names live in
exactly one place: no hardcoded strings, no parallel constants. The attribute string
VALUES are the cross-language contract the TS frontend (which can't import Python core)
consumes from the DOM. Pure; reuses P8A ``normalize_screen_label`` for stable labels.
"""

from __future__ import annotations

import html
import re
from enum import Enum
from typing import TYPE_CHECKING

from disco.core.semantic_refs import normalize_screen_label

if TYPE_CHECKING:  # avoid a runtime import cycle (models.py imports this module)
    from disco.core.appkit.models import AppSpec

METADATA_VERSION = "1"  # bump when the convention changes (emitted as data-disco-version)


class DataDiscoAttr(str, Enum):
    FIELD = "data-disco-field"
    SECTION = "data-disco-section"
    FILE = "data-disco-file"
    FLOW = "data-disco-flow"
    SCREEN_LABEL = "data-disco-screen-label"
    COMMENT_ANCHOR = "data-disco-comment-anchor"
    METRIC_ID = "data-disco-metric-id"
    VERSION = "data-disco-version"
    # P8B rev: indexed/repeated-item resolution (mirrors P8A IndexedLocator).
    COLLECTION = "data-disco-collection"
    INDEX = "data-disco-index"
    ITEM_KIND = "data-disco-item-kind"


class SemanticMetadataError(ValueError):
    """An AppKit output violated a metadata invariant (duplicate/invented anchor, …)."""


def attr(a: DataDiscoAttr, value: object) -> str:
    """A ready-to-inject, html-escaped attribute fragment with a LEADING space:
    ``' data-disco-x="value"'``. Stable: callers concatenate in a fixed order."""
    return f' {a.value}="{html.escape(str(value), quote=True)}"'


def screen_label_value(label: str) -> str:
    """The stable, deterministic screen-label value for a section (P8A normalize)."""
    return normalize_screen_label(label)


def item_attrs(collection_id: str, index: int, item_kind: str) -> str:
    """The metadata for one item of a collection, in stable attr order — so a card/column
    is resolvable as a P8A IndexedLocator(collection_id, index). (The renderer wires this
    when AppKit gains a collection/list section kind; emit + extract exist + tested now.)"""
    if index < 0:
        raise SemanticMetadataError(f"item index must be >= 0, got {index}")
    return (
        attr(DataDiscoAttr.COLLECTION, collection_id)
        + attr(DataDiscoAttr.INDEX, index)
        + attr(DataDiscoAttr.ITEM_KIND, item_kind)
    )


# --- extraction + validation over rendered HTML -------------------------------
_ATTR_RE: dict[DataDiscoAttr, re.Pattern[str]] = {
    a: re.compile(rf'{re.escape(a.value)}="([^"]*)"') for a in DataDiscoAttr
}


def extract_metadata(html_text: str) -> dict[DataDiscoAttr, list[str]]:
    """Every data-disco-* value present in HTML, html-UNescaped so an encoded value and its
    decoded form compare equal (e.g. ``a&amp;b`` and ``a&b`` are the same anchor)."""
    out: dict[DataDiscoAttr, list[str]] = {}
    for a, rx in _ATTR_RE.items():
        vals = [html.unescape(m.group(1)) for m in rx.finditer(html_text)]
        if vals:
            out[a] = vals
    return out


def validate_no_duplicate_anchors(html_text: str) -> None:
    """A data-disco-comment-anchor value must be unique within a document (decoded)."""
    anchors = extract_metadata(html_text).get(DataDiscoAttr.COMMENT_ANCHOR, [])
    seen: set[str] = set()
    dups: set[str] = set()
    for a in anchors:
        (dups if a in seen else seen).add(a)
    if dups:
        raise SemanticMetadataError(f"duplicate comment anchors: {sorted(dups)}")


def declared_anchors_from_spec(spec: AppSpec) -> frozenset[str]:
    """The legitimate comment-anchor namespace for a spec — derived DETERMINISTICALLY from
    the source of truth (its section ids). The single authority for "not invented"."""
    return frozenset(s.id for s in spec.sections)


def validate_anchors_not_invented(html_text: str, declared: frozenset[str]) -> None:
    """Every emitted data-disco-comment-anchor must be in the declared namespace — the model
    cannot invent an anchor that doesn't trace back to the spec."""
    anchors = set(extract_metadata(html_text).get(DataDiscoAttr.COMMENT_ANCHOR, []))
    invented = anchors - declared
    if invented:
        raise SemanticMetadataError(f"invented comment anchors not in the spec: {sorted(invented)}")
