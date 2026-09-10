"""A hand-rolled, deterministic block-style YAML emitter for the compose document.

Extracted from ``local_compose.py`` to reduce module size; the public facade
re-imports ``_emit_yaml`` unchanged.

A minimal, DETERMINISTIC YAML writer over the closed value shape the compose
document uses: nested mappings, sequences of scalars, and string/int scalars.
Every string scalar is double-quoted (with `\\`/`"`/control-char escaping) so a
value like `"no"`, `"127.0.0.1:${HOST_PORT:-8080}:8080"`, or a `${VAR:?...}`
guard is never re-interpreted by a YAML parser as a bool/int/flow-collection.

This is a tier-1 (leaf) module within ``local_compose_parts``: it has no
dependency on any sibling part, the parent module, or the sibling ``spec``
module.
"""

from __future__ import annotations

type _Yaml = str | int | list[str] | dict[str, _Yaml]


def _scalar(value: str | int) -> str:
    if isinstance(value, bool):  # pragma: no cover - bool is not used, guard for safety
        raise TypeError("bool is not a supported YAML scalar here")
    if isinstance(value, int):
        return str(value)
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _emit_mapping(mapping: dict[str, _Yaml], indent: int) -> list[str]:
    pad = "  " * indent
    lines: list[str] = []
    for key, value in mapping.items():
        if isinstance(value, dict):
            if not value:
                lines.append(f"{pad}{key}: {{}}")
            else:
                lines.append(f"{pad}{key}:")
                lines.extend(_emit_mapping(value, indent + 1))
        elif isinstance(value, list):
            if not value:
                lines.append(f"{pad}{key}: []")
            else:
                lines.append(f"{pad}{key}:")
                for item in value:
                    lines.append(f"{pad}  - {_scalar(item)}")
        else:
            lines.append(f"{pad}{key}: {_scalar(value)}")
    return lines


def _emit_yaml(document: dict[str, _Yaml]) -> str:
    return "\n".join(_emit_mapping(document, 0)) + "\n"
