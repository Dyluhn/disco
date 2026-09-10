"""Collection primitive: the `collection_verify` hook and its check-cluster
helpers.

Extracted from ``primitive_verify`` to keep that module's facade under the
module logical-line budget and each check under its own complexity/length
cap. Pure ports: every check name and evidence string is byte-identical to
the original ``collection_verify``.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..generator import _ts
from ..primitive_verify import _component_path, _result
from ..primitives import PrimitiveVerifyResult, VerifyCheck
from ..spec import AppSpec, DesignSpec, Section


def _collection_sections(app: AppSpec) -> list[tuple[str, Section]]:
    """The AppSpec's folded collection-* list sections, pure port of the
    section-gathering comprehension in the original `collection_verify`."""
    return [
        (page.id, section)
        for page in app.pages
        for section in page.sections
        if section.kind == "list" and section.id.startswith("collection-")
    ]


def _collection_component_state(
    sections: list[tuple[str, Section]], app: AppSpec, tree: Mapping[str, str]
) -> tuple[list[str], list[str]]:
    """Missing/stale collection section components, pure port of the render
    loop in the original `collection_verify`."""
    missing_components: list[str] = []
    stale_components: list[str] = []
    for page_id, section in sections:
        path = _component_path(app, page_id, section.id)
        src = tree.get(path)
        if src is None:
            missing_components.append(path)
        elif f'data-appkit-section="{section.id}"' not in src or "c.items" not in src:
            stale_components.append(path)
    return missing_components, stale_components


def _collection_missing_items(
    sections: list[tuple[str, Section]], content_ts: str
) -> list[str]:
    """Folded collection item literals missing from generated content.ts, pure
    port of the item-check loop in the original `collection_verify`."""
    missing_items: list[str] = []
    for _page_id, section in sections:
        if section.content is None:
            continue
        for item in section.content.items:
            if _ts(item) not in content_ts and item not in content_ts:
                missing_items.append(f"{section.id}: {item}")
    return missing_items


def _collection_sections_render_check(
    missing_components: list[str], stale_components: list[str]
) -> VerifyCheck:
    """The collection_sections_render check, pure port of the block of the same
    name in the original `collection_verify`."""
    ok = not missing_components and not stale_components
    return VerifyCheck(
        "collection_sections_render",
        ok,
        "folded collection section component(s) render on their target pages."
        if ok
        else "; ".join(
            [
                *(
                    ["missing component(s): " + ", ".join(missing_components)]
                    if missing_components
                    else []
                ),
                *(
                    [
                        "component(s) missing collection render markers: "
                        + ", ".join(stale_components)
                    ]
                    if stale_components
                    else []
                ),
            ]
        ),
    )


def _collection_items_present_check(missing_items: list[str]) -> VerifyCheck:
    """The collection_items_present check, pure port of the block of the same
    name in the original `collection_verify`."""
    return VerifyCheck(
        "collection_items_present",
        not missing_items,
        "folded collection item literal(s) are present in generated content.ts."
        if not missing_items
        else "missing folded collection item literal(s): " + ", ".join(missing_items),
    )


def collection_verify(
    app: AppSpec | None, design: DesignSpec | None, tree: Mapping[str, str]
) -> PrimitiveVerifyResult:
    """Pure checks for folded collection sections.

    Reality note: lead_gen/directory/records components render collection items
    through generated `content.ts`, so item literals live there rather than inside
    the TSX component source.
    """
    del design
    if app is None:
        return _result(
            [
                VerifyCheck(
                    "collection_sections_render",
                    False,
                    "no .disco/appspec.json — cannot locate folded collection sections.",
                ),
                VerifyCheck(
                    "collection_items_present",
                    False,
                    "no .disco/appspec.json — cannot verify folded collection items.",
                ),
            ]
        )
    sections = _collection_sections(app)
    if not sections:
        return _result(
            [
                VerifyCheck(
                    "collection_sections_render",
                    False,
                    "the AppSpec has no folded collection-* list sections.",
                ),
                VerifyCheck(
                    "collection_items_present",
                    False,
                    "the AppSpec has no folded collection items to verify.",
                ),
            ]
        )

    missing_components, stale_components = _collection_component_state(sections, app, tree)
    content_ts = tree.get("src/generated/content.ts", "")
    missing_items = _collection_missing_items(sections, content_ts)

    return _result(
        [
            _collection_sections_render_check(missing_components, stale_components),
            _collection_items_present_check(missing_items),
        ]
    )
