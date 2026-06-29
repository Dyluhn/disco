"""Semantic reference model (P8A) — deterministic human→target resolution.

A user says "the hero headline", "the second service card", "slide 5", "section 3". This
resolves such a phrase to a TYPED target against a structured screen inventory
(SemanticReferenceContext) — deterministically, 1-index-safe, and REJECTING ambiguity
rather than guessing. The frontend (P8C) and the edit tools (P8B/P4) consume the typed
targets; this module is pure (no runtime/tool imports).

Invariants: a resolution is either RESOLVED (exactly one typed target) or it carries a
reason (AMBIGUOUS with candidates / NO_MATCH / INVALID_ORDINAL / OUT_OF_RANGE) and resolves
to nothing. It never invents a target it isn't sure about.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------- kinds + reasons


class SemanticTargetKind(str, Enum):
    FIELD = "field"  # a field of a section (hero.headline)
    SECTION = "section"  # a whole section
    INDEXED = "indexed"  # the Nth item of a collection (services.cards[1])
    SLIDE = "slide"  # the Nth slide of a deck
    COMMENT_ANCHOR = "comment_anchor"  # a stable comment anchor


class ResolutionReason(str, Enum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"  # >1 candidate — surfaced, never guessed
    NO_MATCH = "no_match"
    INVALID_ORDINAL = "invalid_ordinal"  # "0th" / negative / non-ordinal
    OUT_OF_RANGE = "out_of_range"  # ordinal beyond the collection/deck length


# ---------------------------------------------------------------- typed locators
# Each locator is frozen and carries a Literal kind, so SemanticTarget is a
# discriminated union — an invalid shape (e.g. a SLIDE with a field) is impossible.


class FieldLocator(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal[SemanticTargetKind.FIELD] = SemanticTargetKind.FIELD
    section_id: str
    field_id: str


class SectionLocator(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal[SemanticTargetKind.SECTION] = SemanticTargetKind.SECTION
    section_id: str


class IndexedLocator(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal[SemanticTargetKind.INDEXED] = SemanticTargetKind.INDEXED
    collection_id: str
    index: int = Field(ge=0)  # 0-based array index; never negative


class SlideLocator(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal[SemanticTargetKind.SLIDE] = SemanticTargetKind.SLIDE
    slide_id: str


class CommentAnchorLocator(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal[SemanticTargetKind.COMMENT_ANCHOR] = SemanticTargetKind.COMMENT_ANCHOR
    anchor_id: str


SemanticTarget = Union[
    FieldLocator, SectionLocator, IndexedLocator, SlideLocator, CommentAnchorLocator
]


def target_id(t: SemanticTarget) -> str:
    """A stable id string for a target (bare — within one kind)."""
    if isinstance(t, FieldLocator):
        return f"{t.section_id}.{t.field_id}"
    if isinstance(t, SectionLocator):
        return t.section_id
    if isinstance(t, IndexedLocator):
        return f"{t.collection_id}[{t.index}]"
    if isinstance(t, SlideLocator):
        return t.slide_id
    return t.anchor_id


def qualified_target_id(t: SemanticTarget) -> str:
    """A GLOBALLY unique id: kind-qualified, so a section and a comment anchor that share an
    id string stay distinct (e.g. 'section:notes' vs 'comment_anchor:notes'). Used as the
    identity key for dedup AND for ambiguity candidate lists."""
    return f"{t.kind.value}:{target_id(t)}"


# ---------------------------------------------------------------- context inventory


class SectionEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    label: str
    aliases: tuple[str, ...] = ()
    ordinal: int  # 1-based position on the screen


class FieldEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    section_id: str
    field_id: str
    labels: tuple[str, ...] = ()  # human names for the field ("headline", "cta")


class CollectionEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str  # e.g. "services.cards"
    item_kind: str  # e.g. "card", "column"
    length: int
    labels: tuple[str, ...] = ()  # human names ("service card", "services")


class SlideEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    title: str
    ordinal: int  # 1-based


class AnchorEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    label: str


class SemanticReferenceContext(BaseModel):
    """The typed inventory a phrase resolves AGAINST."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    sections: tuple[SectionEntry, ...] = ()
    fields: tuple[FieldEntry, ...] = ()
    collections: tuple[CollectionEntry, ...] = ()
    slides: tuple[SlideEntry, ...] = ()
    comment_anchors: tuple[AnchorEntry, ...] = ()


# ---------------------------------------------------------------- resolution result


class ReferenceResolution(BaseModel):
    model_config = ConfigDict(frozen=True)
    resolved: SemanticTarget | None = None
    ambiguous: bool = False
    reason: ResolutionReason = ResolutionReason.NO_MATCH
    candidates: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _invariants(self) -> ReferenceResolution:
        # Full biconditional, one branch per reason — no slack states.
        if self.reason is ResolutionReason.RESOLVED:
            if self.resolved is None or self.ambiguous or self.candidates:
                raise ValueError("RESOLVED: exactly one target, not ambiguous, no candidates")
        elif self.reason is ResolutionReason.AMBIGUOUS:
            if self.resolved is not None or not self.ambiguous or not self.candidates:
                raise ValueError("AMBIGUOUS: no target, ambiguous=True, non-empty candidates")
        else:  # NO_MATCH / INVALID_ORDINAL / OUT_OF_RANGE
            if self.resolved is not None or self.ambiguous or self.candidates:
                raise ValueError(f"{self.reason.value}: no target, not ambiguous, no candidates")
        return self


def _resolved(t: SemanticTarget) -> ReferenceResolution:
    return ReferenceResolution(resolved=t, reason=ResolutionReason.RESOLVED)


def _ambiguous(candidates: list[str]) -> ReferenceResolution:
    return ReferenceResolution(
        ambiguous=True, reason=ResolutionReason.AMBIGUOUS, candidates=tuple(sorted(set(candidates)))
    )


def _reject(reason: ResolutionReason) -> ReferenceResolution:
    return ReferenceResolution(reason=reason)


# ---------------------------------------------------------------- label normalization

_PUNCT_RUN = re.compile(r"[^a-z0-9]+")


def normalize_screen_label(label: str) -> str:
    """Deterministic, idempotent slug: casefold, collapse any run of non-alphanumerics
    to a single '-', trim. ``normalize(normalize(x)) == normalize(x)``."""
    return _PUNCT_RUN.sub("-", label.casefold()).strip("-")


# ---------------------------------------------------------------- ordinal parsing

_ORDINAL_WORDS: dict[str, int] = {
    w: i + 1
    for i, w in enumerate(
        ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"]
    )
}
_DIGIT_ORDINAL = re.compile(r"\b(\d+)(?:st|nd|rd|th)?\b")
# a zero ordinal on the normalized string ("0", "0th"); a NEGATIVE on the RAW string (a
# minus at start/after-space before a digit — NOT a normalization hyphen-separator).
_ZERO_ORDINAL = re.compile(r"\b0+(?:st|nd|rd|th)?\b")
# a NEGATIVE = a minus directly before a digit whose left neighbour is NOT alphanumeric —
# i.e. start, whitespace, or punctuation (catches "-1", " -1", "slide:-1"). A minus between
# two word chars ("slide-5", a normalization separator) is NOT a negative.
_NEG_ORDINAL = re.compile(r"(?:^|[^a-z0-9])-\s*\d", re.IGNORECASE)


class _Ordinal(BaseModel):
    """The outcome of looking for a human ordinal in a phrase."""

    model_config = ConfigDict(frozen=True)
    value: int | None = None  # 1-based ordinal found, or None if none present
    invalid: bool = False  # an explicit but invalid ordinal (0th / negative)


def parse_human_ordinal(phrase: str) -> _Ordinal:
    """Find a 1-based human ordinal in ``phrase``. Ordinal WORDS (first..tenth) and DIGITS
    (5 / 5th) both count. An explicit 0 / 0th / zeroth / negative is INVALID (not absent)."""
    if _NEG_ORDINAL.search(phrase):
        return _Ordinal(invalid=True)
    norm = normalize_screen_label(phrase)
    if _ZERO_ORDINAL.search(norm) or "zeroth" in norm:
        return _Ordinal(invalid=True)
    for word, n in _ORDINAL_WORDS.items():
        if re.search(rf"\b{word}\b", norm):
            return _Ordinal(value=n)
    m = _DIGIT_ORDINAL.search(norm)
    if m:
        n = int(m.group(1))
        return _Ordinal(value=n) if n >= 1 else _Ordinal(invalid=True)
    return _Ordinal()


def human_ordinal_to_index(ordinal: int) -> int:
    """1-based human ordinal → 0-based array index. Rejects < 1."""
    if ordinal < 1:
        raise ValueError(f"ordinal must be >= 1, got {ordinal}")
    return ordinal - 1


# ---------------------------------------------------------------- the resolver


def _tokens(norm: str) -> set[str]:
    return set(norm.split("-")) - {""}


def _labels_match(phrase_norm: str, labels: tuple[str, ...]) -> bool:
    """True iff ANY of the entry's labels appears as a contiguous token-run in the phrase."""
    pt = _tokens(phrase_norm)
    for lab in labels:
        lt = _tokens(normalize_screen_label(lab))
        if lt and lt <= pt:
            return True
    return False


def _section_of(target: SemanticTarget) -> str | None:
    """The owning section id of a target, for subsumption (a field/item refines its section)."""
    if isinstance(target, FieldLocator):
        return target.section_id
    if isinstance(target, IndexedLocator):
        return target.collection_id.split(".", 1)[0]  # "services.cards" -> "services"
    return None


def resolve_human_reference(phrase: str, *, context: SemanticReferenceContext) -> ReferenceResolution:
    """Resolve a human reference phrase to a typed target, or reject (ambiguous / no_match /
    invalid_ordinal / out_of_range). Deterministic and GLOBALLY ambiguity-rejecting: it gathers
    every matching target across kinds and only resolves when exactly one survives subsumption.

    Slides/sections are resolved by EXPLICIT ordinal equality (not tuple position), so sparse,
    out-of-order, or duplicate ordinals behave correctly — a duplicate ordinal is ambiguous.
    """
    norm = normalize_screen_label(phrase)
    if not norm:
        return _reject(ResolutionReason.NO_MATCH)
    ordinal = parse_human_ordinal(phrase)
    if ordinal.invalid:
        return _reject(ResolutionReason.INVALID_ORDINAL)
    n = ordinal.value
    toks = _tokens(norm)

    candidates: list[SemanticTarget] = []
    out_of_range = False  # a label matched but the ordinal exceeded its range — only wins if nothing else does

    # SLIDE — "slide N" / "Nth slide": resolve by explicit ordinal equality.
    if "slide" in toks and n is not None:
        hits = [s for s in context.slides if s.ordinal == n]
        if hits:
            candidates += [SlideLocator(slide_id=s.id) for s in hits]
        else:
            out_of_range = True

    # SECTION BY ORDINAL — "section N": explicit ordinal equality.
    if "section" in toks and n is not None:
        hits = [s for s in context.sections if s.ordinal == n]
        if hits:
            candidates += [SectionLocator(section_id=s.id) for s in hits]
        else:
            out_of_range = True

    # FIELD — section label + field label both present.
    for f in context.fields:
        sec = next((s for s in context.sections if s.id == f.section_id), None)
        sec_labels = (sec.label, *sec.aliases) if sec is not None else ()
        if _labels_match(norm, sec_labels) and _labels_match(norm, f.labels or (f.field_id,)):
            candidates.append(FieldLocator(section_id=f.section_id, field_id=f.field_id))

    # COLLECTION ITEM — ordinal + collection label; index is positional (0-based). A matched
    # collection (in- OR out-of-range) "claims" its owning section, so an indexed intent like
    # "fifth pricing column" does NOT silently degrade into the whole "pricing" section.
    claimed_sections: set[str] = set()
    if n is not None:
        for col in context.collections:
            if _labels_match(norm, (*col.labels, col.item_kind)):
                claimed_sections.add(col.id.split(".", 1)[0])
                idx = human_ordinal_to_index(n)
                if idx < col.length:
                    candidates.append(IndexedLocator(collection_id=col.id, index=idx))
                else:
                    out_of_range = True

    # BARE SECTION label + COMMENT ANCHOR.
    for s in context.sections:
        if _labels_match(norm, (s.label, *s.aliases)):
            candidates.append(SectionLocator(section_id=s.id))
    for a in context.comment_anchors:
        if _labels_match(norm, (a.label, a.id)):
            candidates.append(CommentAnchorLocator(anchor_id=a.id))

    # SUBSUMPTION: a bare SectionLocator(S) is subsumed by any field/item that refines S, so
    # "hero headline" resolves to the field, not field-vs-section ambiguity. Genuinely unrelated
    # cross-kind matches (e.g. a section AND a comment anchor sharing a label) survive → ambiguous.
    refined = {sid for t in candidates if (sid := _section_of(t)) is not None} | claimed_sections
    survivors = [
        t for t in candidates
        if not (isinstance(t, SectionLocator) and t.section_id in refined)
    ]
    uniq = _dedupe(survivors)
    if len(uniq) == 1:
        return _resolved(uniq[0])
    if len(uniq) > 1:
        return _ambiguous([qualified_target_id(t) for t in uniq])
    if out_of_range:
        return _reject(ResolutionReason.OUT_OF_RANGE)
    return _reject(ResolutionReason.NO_MATCH)


def _dedupe(targets: list[SemanticTarget]) -> list[SemanticTarget]:
    seen: set[str] = set()
    out: list[SemanticTarget] = []
    for t in targets:
        key = qualified_target_id(t)  # kind-qualified — distinct targets never collapse
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out
