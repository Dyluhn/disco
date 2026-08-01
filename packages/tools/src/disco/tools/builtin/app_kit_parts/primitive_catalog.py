"""Primitive-catalog helpers shared by `app_create` and `app_add_primitive`."""

from __future__ import annotations

from disco.core.appkit import get_primitive, primitive_ids


def _unknown_primitive_msg(prim_id: str) -> str:
    known = ", ".join(sorted(primitive_ids()))
    return f"unknown primitive_id: {prim_id!r}. Known primitives: {known}."


def _addable_primitive_ids() -> list[str]:
    """Primitives with BOTH a spec_schema and an apply_spec — the ones
    app_add_primitive accepts. Base scaffolds (lead_gen, directory, …) are not."""
    addable: list[str] = []
    for pid in sorted(primitive_ids()):
        prim = get_primitive(pid)
        if prim is not None and prim.spec_schema is not None and prim.apply_spec is not None:
            addable.append(pid)
    return addable
