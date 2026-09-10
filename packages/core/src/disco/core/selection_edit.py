"""Selection→scoped-edit directive (P8 — semantic direct manipulation, the wire).

The essential consumer that was missing: it turns a *clicked preview element* +
a user *edit instruction* into a host-owned, precisely-anchored directive the build
loop applies as a TARGETED edit to exactly that element — never a rewrite.

Flow: the in-frame selection agent (``selection_agent.js``) resolves a clicked DOM
element to a typed ref (a ``data-oid`` SourceRef, a deck DeckRef, or — for AppKit
output stamped with ``data-disco-*`` — a SemanticRef). The frontend submits
``{selection_ref, edit_instruction}`` over the ``selection_edit`` WS frame
(``wire.py``); the WS handler (``routes/ws.py``) parses the ref HERE and builds the
directive HERE, then feeds it to the loop as a steer user-turn. Because the directive
names the exact anchor and mandates a targeted-edit tool, the existing Targeted-edit
law + the tool-layer no-rewrite guards (F1 / REL-RC-M / CD-TOOLS-1) do the enforcing —
this module only has to produce truthful, unambiguous instructions.

Pure: mirrors ``selectionBridge.ts``'s ref shapes (the cross-language contract) with
no runtime/tool imports, exactly like ``semantic_refs.py``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------- typed refs
# These mirror the TS `SelectionRef` union in frontend/src/lib/selectionBridge.ts
# (the wire contract the browser produces). Frozen + extra-forbid so a malformed
# frame is rejected, not silently coerced.


class SourceSelectionRef(BaseModel):
    """A source-file anchor from a serve-time ``data-oid="{relpath}:{line}"`` stamp
    (preview_edit.py stamps every element). Precise for any static-site build."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["source"] = "source"
    oid: str
    file: str
    line: int = Field(ge=0)


class DeckSelectionRef(BaseModel):
    """A deck element anchor (data-slide-id + data-element-id) → deck_patch."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["deck"] = "deck"
    slide_id: str
    element_id: str


class SemanticSelectionRef(BaseModel):
    """An AppKit ``data-disco-*`` semantic anchor → the matching app_* targeted tool.

    ``field_id`` present ⇒ a single field of a section (→ app_update_content).
    ``collection_id``+``index`` present ⇒ the Nth item of a collection.
    Only ``section_id`` ⇒ the whole section.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["semantic"] = "semantic"
    section_id: str
    field_id: str | None = None
    collection_id: str | None = None
    index: int | None = Field(default=None, ge=0)
    screen_label: str | None = None


SelectionRef = SourceSelectionRef | DeckSelectionRef | SemanticSelectionRef

_REF_MODELS: tuple[type[BaseModel], ...] = (
    SourceSelectionRef,
    DeckSelectionRef,
    SemanticSelectionRef,
)


def parse_selection_ref(raw: object) -> SelectionRef | None:
    """Validate an untrusted ref dict (from a WS frame) into a typed ref, or None.

    Dispatches on ``kind`` so a wrong-shape ref (e.g. a ``source`` missing ``line``)
    is rejected rather than mis-parsed as another kind. Never raises."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    model = {
        "source": SourceSelectionRef,
        "deck": DeckSelectionRef,
        "semantic": SemanticSelectionRef,
    }.get(kind if isinstance(kind, str) else "")
    if model is None:
        return None
    try:
        parsed = model.model_validate(raw)
    except ValueError:
        return None
    # mypy/basedpyright: model_validate returns BaseModel; the map guarantees a member.
    return parsed  # type: ignore[return-value]


# ---------------------------------------------------------------- directive


# A source anchor is only trustworthy when it names a real file+line. An empty
# file (resolveRef's no-anchor fallback) must NOT masquerade as a precise target —
# the directive then falls back to the human label + "find it yourself" framing.
def _source_is_anchored(ref: SourceSelectionRef) -> bool:
    return bool(ref.file) and ref.line > 0


_PREFIX = "[Scoped edit request — the user selected one element in the live preview]"

_TARGETED_LAW = (
    "Apply EXACTLY this change to THAT element and nothing else. Make a TARGETED edit "
    "in place — do NOT rewrite the file, and do NOT touch any other element."
)


def is_scoped_edit_directive(text: str) -> bool:
    """True when ``text`` is a host-owned scoped edit directive."""
    return text.startswith(_PREFIX)


def build_scoped_edit_directive(
    ref: SelectionRef,
    instruction: str,
    *,
    human_label: str | None = None,
) -> str:
    """Assemble the host-owned directive for a selection edit. Pure.

    Names the exact anchor per ref kind and mandates the matching targeted-edit tool,
    so the loop mutates only the selected element. ``instruction`` is the user's
    verbatim change; ``human_label`` is the selection-agent's readable description
    (e.g. ``h1 — "Nightshift Coffee"``), used to disambiguate for the model.

    TRUST NOTE (documented limitation, Codex P8-1): a source ref's ``file``/``line``
    come from a ``data-oid`` in the served DOM. The host stamps those serve-time, but
    a page whose CONTENT embeds an authored ``data-oid`` could steer the edit at a
    different workspace file. This is bounded — the edit is a targeted change inside
    the run's own sandboxed workspace (no path escape, no privilege gain) — but a
    fully robust fix would re-verify the ref against the host's own stamp registry.
    """
    instruction = instruction.strip()
    label = f" ({human_label.strip()})" if human_label and human_label.strip() else ""

    if isinstance(ref, DeckSelectionRef):
        where = (
            f"The element is on slide `{ref.slide_id}`, element `{ref.element_id}`{label}. "
            "Use the `deck_patch` tool to change only that slide element."
        )
    elif isinstance(ref, SemanticSelectionRef):
        where = _semantic_where(ref, label)
    elif _source_is_anchored(ref):
        where = (
            f"The element is anchored at `{ref.file}` line {ref.line}{label}. "
            f"Read `{ref.file}` first, then edit that exact region with a targeted "
            "edit tool (`file_edit` / `file_replace_lines` / `file_str_replace`)."
        )
    else:
        # No usable anchor — fall back to the human label so the model can locate it.
        desc = (
            human_label.strip() if human_label and human_label.strip() else "the selected element"
        )
        where = (
            f"The user selected {desc} in the preview (no precise source anchor was "
            "captured). Read the relevant file, locate that element, and edit only it "
            "with a targeted edit tool — do not rewrite the file."
        )

    body = instruction or "(no change described — ask the user what to change)"
    return f"{_PREFIX} {where}\n\n{_TARGETED_LAW}\n\nThe requested change:\n{body}"


def _semantic_where(ref: SemanticSelectionRef, label: str) -> str:
    scope = ref.screen_label or ref.section_id
    if ref.field_id is not None:
        return (
            f"The element is the `{ref.field_id}` field of the `{scope}` section{label}. "
            "Use `app_update_content` to change only that field."
        )
    if ref.collection_id is not None and ref.index is not None:
        return (
            f"The element is item #{ref.index} of the `{ref.collection_id}` collection "
            f"in the `{scope}` section{label}. Use the matching app_* tool to change only "
            "that item."
        )
    return (
        f"The element is the `{scope}` section{label}. Use the matching app_* tool "
        "(`app_update_content` / `app_set_design`) to change only that section."
    )


def selection_edit_frame_valid(selection_ref: object, edit_instruction: object) -> bool:
    """Cheap wire-level precondition for a `selection_edit` frame: a parseable ref
    AND a non-empty instruction. The WS handler uses this to refuse a malformed frame
    without kicking the loop."""
    if not isinstance(edit_instruction, str) or not edit_instruction.strip():
        return False
    return parse_selection_ref(selection_ref) is not None


__all__ = [
    "SourceSelectionRef",
    "DeckSelectionRef",
    "SemanticSelectionRef",
    "SelectionRef",
    "parse_selection_ref",
    "build_scoped_edit_directive",
    "is_scoped_edit_directive",
    "selection_edit_frame_valid",
]
