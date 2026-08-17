"""Bounded structural YAML parser for the repository's workflow/pre-commit subset.

This parser handles only the limited YAML subset used in:
  - ``.github/workflows/ci.yml``
  - ``.github/workflows/release.yml``
  - ``.pre-commit-config.yaml``

It is NOT a general YAML parser. It handles:
  - nested mappings (``key: value``)
  - lists (``- item``)
  - string/bool/int/float scalar values
  - comments (``#`` at start of stripped line)
  - multi-line string values (``run: |`` blocks)
  - inline flow mappings (``{key: value}``)
  - the ``on:`` key in GitHub workflows (which YAML parses as boolean True)

It deliberately does NOT support:
  - anchors/aliases
  - tags
  - complex flow sequences
  - multi-document streams

Parse ambiguity fails closed: an unparseable structure raises ValueError.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _strip_comment(line: str) -> str:
    """Strip a trailing comment from a YAML line (not inside quotes)."""
    in_single = False
    in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:i].rstrip()
    return line


def _parse_scalar(value: str) -> Any:
    """Parse a YAML scalar value (string, bool, int, float, or null)."""
    stripped = value.strip()
    if not stripped:
        return None
    # Remove surrounding quotes
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
        return stripped[1:-1]
    # Booleans
    if stripped.lower() == "true":
        return True
    if stripped.lower() == "false":
        return False
    if stripped.lower() in ("null", "~"):
        return None
    # Integers
    try:
        return int(stripped)
    except ValueError:
        pass
    # Floats
    try:
        return float(stripped)
    except ValueError:
        pass
    # Plain string
    return stripped


def _parse_flow_mapping(value: str) -> dict[str, Any]:
    """Parse an inline flow mapping like ``{key: value, key2: value2}``."""
    inner = value.strip()
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1]
    result: dict[str, Any] = {}
    for part in _split_flow(inner):
        if ":" in part:
            key, val = part.split(":", 1)
            result[key.strip()] = _parse_scalar(val)
    return result


def _split_flow(s: str) -> list[str]:
    """Split a flow content string by commas, respecting braces and brackets."""
    parts: list[str] = []
    depth = 0
    current = ""
    for ch in s:
        if ch in "{[":
            depth += 1
            current += ch
        elif ch in "}]":
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current)
    return parts


def _indent(line: str) -> int:
    """Return the number of leading spaces in a line."""
    return len(line) - len(line.lstrip(" "))


def parse_yaml(text: str) -> Any:
    """Parse a bounded YAML subset into a Python data structure.

    Raises ValueError on parse ambiguity or unsupported constructs.
    """
    lines = text.splitlines()
    # Preprocess: strip comments and blank lines, but keep track of line numbers
    cleaned: list[tuple[int, int, str]] = []
    for lineno, raw in enumerate(lines, 1):
        line = _strip_comment(raw)
        if not line.strip():
            continue
        cleaned.append((lineno, _indent(line), line.rstrip()))

    if not cleaned:
        return None

    result, pos = _parse_block(cleaned, 0, 0)
    if pos != len(cleaned):
        raise ValueError(
            f"YAML parse error: trailing content at line {cleaned[pos][0]}"
        )
    return result


def _parse_block(
    lines: list[tuple[int, int, str]], start: int, min_indent: int
) -> tuple[Any, int]:
    """Parse a block of YAML lines starting at ``start`` with at least ``min_indent``.

    Returns (value, next_position).
    """
    if start >= len(lines):
        return None, start

    indent = lines[start][1]
    if indent < min_indent:
        return None, start

    # Check if this is a list block
    if lines[start][2].lstrip().startswith("- "):
        return _parse_list(lines, start, indent)
    # Otherwise it's a mapping
    return _parse_mapping(lines, start, indent)


def _parse_list(
    lines: list[tuple[int, int, str]], start: int, indent: int
) -> tuple[list[Any], int]:
    """Parse a YAML list block."""
    result: list[Any] = []
    pos = start
    while pos < len(lines):
        lineno, line_indent, line = lines[pos]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise ValueError(
                f"YAML parse error: unexpected indent at line {lineno}"
            )
        stripped = line.lstrip()
        if not stripped.startswith("- "):
            break
        # Item content after "- "
        item_content = stripped[2:]
        if not item_content:
            # Multi-line item: parse the nested block
            pos += 1
            if pos < len(lines) and lines[pos][1] > indent:
                value, pos = _parse_block(lines, pos, indent + 1)
            else:
                value = None
            result.append(value)
        else:
            # Check if it's "key: value" (a mapping item)
            if ":" in item_content and not item_content.startswith("{"):
                # This is a mapping item — parse as inline mapping start
                # Reconstruct the line as if it were at indent + 2
                fake_indent = indent + 2
                fake_line = " " * fake_indent + item_content
                # Replace current line and parse mapping
                new_lines = lines[:pos] + [(lineno, fake_indent, fake_line)] + lines[pos + 1:]
                value, new_pos = _parse_mapping(new_lines, pos, fake_indent)
                pos = new_pos
                result.append(value)
            else:
                # Scalar or flow item
                if item_content.startswith("{"):
                    result.append(_parse_flow_mapping(item_content))
                else:
                    result.append(_parse_scalar(item_content))
                pos += 1
    return result, pos


def _parse_mapping(
    lines: list[tuple[int, int, str]], start: int, indent: int
) -> tuple[dict[str, Any], int]:
    """Parse a YAML mapping block."""
    result: dict[str, Any] = {}
    pos = start
    while pos < len(lines):
        lineno, line_indent, line = lines[pos]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise ValueError(
                f"YAML parse error: unexpected indent at line {lineno}"
            )
        stripped = line.lstrip()
        if stripped.startswith("- "):
            break
        # Find the key: value separator
        # Handle the "on:" key specially (YAML parses "on" as boolean True)
        colon_idx = _find_colon(stripped)
        if colon_idx < 0:
            raise ValueError(
                f"YAML parse error: expected key: value at line {lineno}: {line}"
            )
        key_str = stripped[:colon_idx].strip()
        value_str = stripped[colon_idx + 1:].strip()
        # Handle the "on" key — YAML parses it as True
        if key_str == "on":
            key: Any = True
        elif key_str.startswith('"') and key_str.endswith('"'):
            key = key_str[1:-1]
        else:
            key = key_str

        if not value_str:
            # Multi-line value: parse the nested block
            pos += 1
            if pos < len(lines) and lines[pos][1] > indent:
                value, pos = _parse_block(lines, pos, indent + 1)
            else:
                value = None
        elif value_str == "|":
            # Multi-line literal string block
            pos += 1
            value, pos = _parse_literal_block(lines, pos, indent + 1)
        elif value_str.startswith("{"):
            value = _parse_flow_mapping(value_str)
            pos += 1
        elif value_str.startswith("["):
            value = _parse_flow_list(value_str)
            pos += 1
        else:
            value = _parse_scalar(value_str)
            pos += 1
        result[key] = value
    return result, pos


def _parse_literal_block(
    lines: list[tuple[int, int, str]], start: int, min_indent: int
) -> tuple[str, int]:
    """Parse a YAML literal block scalar (``|``)."""
    parts: list[str] = []
    pos = start
    block_indent = None
    while pos < len(lines):
        lineno, line_indent, line = lines[pos]
        if line_indent < min_indent:
            break
        if block_indent is None:
            block_indent = line_indent
        elif line_indent < block_indent:
            break
        # Preserve the content at the block indent
        content = line[block_indent:] if line_indent >= block_indent else ""
        parts.append(content)
        pos += 1
    return "\n".join(parts) + "\n", pos


def _parse_flow_list(value: str) -> list[Any]:
    """Parse an inline flow list like ``[a, b, c]``."""
    inner = value.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    result: list[Any] = []
    for part in _split_flow(inner):
        result.append(_parse_scalar(part))
    return result


def _find_colon(s: str) -> int:
    """Find the first colon that separates key from value, respecting quotes."""
    in_single = False
    in_double = False
    for i, ch in enumerate(s):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == ":" and not in_single and not in_double:
            # Must be followed by space or end of string
            if i + 1 >= len(s) or s[i + 1] in " \t":
                return i
    return -1


def load_yaml(path: Path) -> Any:
    """Load and parse a YAML file using the bounded parser."""
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    try:
        return parse_yaml(text)
    except ValueError as exc:
        raise ValueError(f"YAML parse error in {path}: {exc}") from exc
