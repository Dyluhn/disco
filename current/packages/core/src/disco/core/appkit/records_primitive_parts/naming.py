"""Naming/ordering helpers shared by the records primitive's TS/SQL emitters.

Extracted verbatim from ``records_primitive.py`` to reduce module size; the
parent module re-imports every name here unchanged (see its module docstring).
"""

from __future__ import annotations

import re

from ..spec import Entity

_TS_RESERVED = frozenset(
    {
        "break",
        "case",
        "catch",
        "class",
        "const",
        "continue",
        "debugger",
        "default",
        "delete",
        "do",
        "else",
        "export",
        "extends",
        "finally",
        "for",
        "function",
        "if",
        "import",
        "in",
        "instanceof",
        "new",
        "return",
        "super",
        "switch",
        "this",
        "throw",
        "try",
        "typeof",
        "var",
        "void",
        "while",
        "with",
        "yield",
    }
)


def _records_table_name(entity: Entity) -> str:
    from ..generator import _slug

    return _slug(entity.id).replace("-", "_")


def _fk_order_entities(entities: tuple[Entity, ...]) -> tuple[Entity, ...]:
    by_id = {entity.id: entity for entity in entities}
    deps: dict[str, set[str]] = {}
    for entity in entities:
        refs = {field.references for field in entity.fields if field.references is not None}
        missing = sorted(ref for ref in refs if ref not in by_id)
        if missing:
            raise ValueError(f"entity {entity.id!r} references unknown entity {missing[0]!r}")
        deps[entity.id] = set(refs)

    ordered: list[Entity] = []
    emitted: set[str] = set()
    pending = dict(deps)
    while pending:
        ready = [
            entity.id
            for entity in entities
            if entity.id in pending and pending[entity.id] <= emitted
        ]
        if not ready:
            cycle = ", ".join(sorted(pending))
            raise ValueError(f"entity foreign key cycle detected among: {cycle}")
        for entity_id in ready:
            ordered.append(by_id[entity_id])
            emitted.add(entity_id)
            del pending[entity_id]
    return tuple(ordered)


def _ts_const_names(entities: tuple[Entity, ...]) -> dict[str, str]:
    names: dict[str, str] = {}
    used: set[str] = set()
    for entity in entities:
        base = re.sub(r"[^0-9A-Za-z_]", "_", _records_table_name(entity))
        if not re.match(r"^[A-Za-z_]", base):
            base = f"table_{base}"
        if base in _TS_RESERVED:
            base = f"{base}_table"
        name = base
        suffix = 2
        while name in used:
            name = f"{base}_{suffix}"
            suffix += 1
        used.add(name)
        names[entity.id] = name
    return names


def _function_names(entities: tuple[Entity, ...]) -> dict[str, str]:
    from ..generator import _pascal

    names: dict[str, str] = {}
    used: set[str] = set()
    for entity in entities:
        base = _pascal(entity.id)
        name = base
        suffix = 2
        while name in used:
            name = f"{base}{suffix}"
            suffix += 1
        used.add(name)
        names[entity.id] = name
    return names


def _field_meta(entity: Entity) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    from ..generator import _sql_type

    columns = [field.name for field in entity.fields]
    required = [field.name for field in entity.fields if field.required]
    integers: list[str] = []
    reals: list[str] = []
    emails: list[str] = []
    for field in entity.fields:
        sql_type = "INTEGER" if field.references is not None else _sql_type(field.type)
        if sql_type == "INTEGER":
            integers.append(field.name)
        elif sql_type == "REAL":
            reals.append(field.name)
        if field.name.lower() == "email" or field.type.strip().lower() == "email":
            emails.append(field.name)
    return columns, required, integers, reals, emails
