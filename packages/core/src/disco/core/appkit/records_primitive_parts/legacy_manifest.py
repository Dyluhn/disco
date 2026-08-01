"""The legacy (non-auth) records build manifest emitter.

Extracted verbatim from ``records_primitive.py`` to reduce module size; the
parent module re-imports every name here unchanged (see its module docstring).
"""

from __future__ import annotations

import hashlib
import json

from ..spec import AppSpec, DesignSpec


def _legacy_records_app_dump(app: AppSpec) -> dict[str, object]:
    data = app.model_dump(mode="json")
    data.pop("roles", None)
    entities = data.get("entities")
    if isinstance(entities, list):
        for entity in entities:
            if isinstance(entity, dict):
                entity.pop("write_roles", None)
                entity.pop("read_roles", None)
    return data


def _emit_records_legacy_manifest_ts(
    app: AppSpec, design: DesignSpec, names: dict[tuple[str, str], str]
) -> str:
    from ..generator import _comp_name, _iter_sections, _ts

    payload = json.dumps(
        {
            "app": _legacy_records_app_dump(app),
            "design": design.model_dump(mode="json"),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    sections = [
        {"page": page.id, "section": section.id, "component": _comp_name(names, page, section)}
        for page, section in _iter_sections(app)
    ]
    return (
        "/* Auto-generated build manifest — a digest of the App + Design specs. */\n"
        f"export const SPEC_DIGEST = {_ts(digest)};\n"
        f"export const SECTIONS = {_ts(sections)} as const;\n"
    )
