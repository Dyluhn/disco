"""Exact source-evidence enforcement for architecture observations."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from . import debt, python_scan

_OBSERVATION_KIND_BY_DISPOSITION = {
    "DISTRIBUTED_CONCERN": "distributed_concern",
    "TYPED_NON_VIOLATION": "typed_non_violation",
}
_LINE_RANGES = re.compile(r"^\d+-\d+(?:,\d+-\d+)*$")
_SOURCE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_ALLOWED_DESCRIPTIVE_CLAUSES = {
    (
        "DM-022",
        "packages/tools/src/disco/tools/projects/store.py",
        "ProjectStore manifest/version layout",
    ),
}

SourceMatch = tuple[int, int, ast.AST]
TypeScriptMatch = tuple[int, int]
TypeScriptIndex = dict[str, dict[str, list[TypeScriptMatch]]]


def _logical(path: str) -> str:
    """Bucket-normalised path so a directory move is not a rename."""
    for bucket in ("current/", "development/"):
        if path.startswith(bucket):
            return path[len(bucket):]
    return path


def _parse_line_ranges(value: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for part in value.split(","):
        start_text, end_text = part.split("-", 1)
        ranges.append((int(start_text), int(end_text)))
    return ranges


def _observation_symbol_names(
    observation_id: str,
    path: str,
    clause: str,
) -> tuple[list[str], str | None]:
    if not clause:
        return [], None
    if " " in clause:
        if (observation_id, _logical(path), clause) in _ALLOWED_DESCRIPTIVE_CLAUSES:
            return [], None
        return [], (
            f"observation {observation_id}: unapproved descriptive observation "
            f"clause {clause!r} for {path}"
        )
    parts = clause.split("/")
    if not parts or any(not _SOURCE_IDENTIFIER.fullmatch(part) for part in parts):
        return [], (
            f"observation {observation_id}: invalid observation symbol clause "
            f"{clause!r} for {path}"
        )
    return parts, None


def _python_observation_symbols(
    tree: ast.Module,
) -> dict[str, list[SourceMatch]]:
    result: dict[str, list[SourceMatch]] = {}

    def add(name: str, node: ast.AST) -> None:
        start = int(getattr(node, "lineno", 1))
        end = int(getattr(node, "end_lineno", start) or start)
        result.setdefault(name, []).append((start, end, node))

    qnames = python_scan.build_qualified_name_map(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            end = int(node.end_lineno or node.lineno)
            qname = qnames.get((node.lineno, end, node.name), node.name)
            add(qname, node)
            add(node.name, node)
            if isinstance(node, ast.ClassDef):
                for child in ast.walk(node):
                    if isinstance(child, ast.Attribute):
                        add(f"{qname}.{child.attr}", child)
                        add(child.attr, child)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    add(target.id, node)
        elif isinstance(node, ast.Attribute):
            add(node.attr, node)
    return result


def _valid_source_path(path: str) -> bool:
    return bool(
        path
        and not Path(path).is_absolute()
        and ".." not in Path(path).parts
        and not any(char in path for char in "*?[]")
    )


def _range_problems(
    observation_id: str,
    path: str,
    ranges: list[tuple[int, int]],
    line_count: int,
) -> list[str]:
    return [
        f"observation {observation_id}: invalid/stale line range "
        f"{start}-{end} for {path} ({line_count} lines)"
        for start, end in ranges
        if start < 1 or end < start or end > line_count
    ]


def _overlaps(
    match: tuple[int, int] | SourceMatch,
    ranges: list[tuple[int, int]],
) -> bool:
    return not ranges or any(
        match[0] <= observed_end and match[1] >= observed_start
        for observed_start, observed_end in ranges
    )


def _check_python_symbols(
    observation_id: str,
    path: str,
    text: str,
    symbols: list[str],
    ranges: list[tuple[int, int]],
) -> tuple[list[str], list[ast.AST]]:
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, UnicodeDecodeError) as exc:
        return [
            f"observation {observation_id}: file unparseable: {path}: {exc!r}"
        ], []
    source_symbols = _python_observation_symbols(tree)
    problems: list[str] = []
    resolved: list[ast.AST] = []
    for symbol in symbols:
        matches = source_symbols.get(symbol, [])
        if not matches:
            problems.append(
                f"observation {observation_id}: symbol {symbol} not found in {path}"
            )
            continue
        in_range = [match for match in matches if _overlaps(match, ranges)]
        selected = (in_range or matches)[0]
        resolved.append(selected[2])
        if ranges and not in_range:
            problems.append(
                f"observation {observation_id}: symbol {symbol} at "
                f"{selected[0]}-{selected[1]} is outside registered ranges in {path}"
            )
    return problems, resolved


def _typescript_symbol_index(
    ts_result: dict[str, Any] | None,
) -> TypeScriptIndex:
    index: TypeScriptIndex = {}
    for module in ts_result.get("modules", []) if ts_result else []:
        path = module.get("path")
        if not isinstance(path, str):
            continue
        path_symbols: dict[str, list[TypeScriptMatch]] = {}
        for field in ("declaration_symbols", "symbols"):
            for row in module.get(field, []):
                if not isinstance(row, dict):
                    continue
                name = row.get("name")
                start = row.get("line_start")
                end = row.get("line_end")
                if (
                    isinstance(name, str)
                    and isinstance(start, int)
                    and isinstance(end, int)
                    and start >= 1
                    and end >= start
                ):
                    path_symbols.setdefault(name, []).append((start, end))
        index[path] = path_symbols
    return index


def _check_typescript_symbols(
    observation_id: str,
    path: str,
    symbols: list[str],
    ranges: list[tuple[int, int]],
    symbol_index: TypeScriptIndex,
) -> list[str]:
    # The scanner emits logical paths; observations record real ones.
    if _logical(path) in symbol_index:
        path = _logical(path)
    if path not in symbol_index:
        return [
            f"observation {observation_id}: TypeScript compiler evidence absent "
            f"for {path}"
        ]
    problems: list[str] = []
    for symbol in symbols:
        matches = symbol_index[path].get(symbol, [])
        if not matches:
            problems.append(
                f"observation {observation_id}: symbol {symbol} not found in {path}"
            )
        elif ranges and not any(_overlaps(match, ranges) for match in matches):
            start, end = matches[0]
            problems.append(
                f"observation {observation_id}: symbol {symbol} at {start}-{end} "
                f"is outside registered ranges in {path}"
            )
    return problems


def _split_observation_segment(
    segment: str,
) -> tuple[str, str, list[tuple[int, int]]]:
    parts = [part.strip() for part in segment.strip().split(":")]
    path = parts[0] if parts else ""
    ranges: list[tuple[int, int]] = []
    if len(parts) > 1 and _LINE_RANGES.fullmatch(parts[-1]):
        ranges = _parse_line_ranges(parts.pop())
    return path, ":".join(parts[1:]).strip(), ranges


def _check_observation_segment(
    observation_id: str,
    segment: str,
    root: Path,
    ts_symbols: TypeScriptIndex,
) -> tuple[list[str], list[ast.AST]]:
    path, clause, ranges = _split_observation_segment(segment)
    if not _valid_source_path(path):
        return [f"observation {observation_id}: invalid source path {path!r}"], []
    symbols, clause_problem = _observation_symbol_names(
        observation_id, path, clause
    )
    if clause_problem is not None:
        return [clause_problem], []
    full = root / path
    if not full.is_file():
        full = root / _logical(path)
    if not full.is_file():
        return [f"observation {observation_id}: file absent: {path}"], []
    text = full.read_text(encoding="utf-8")
    problems = _range_problems(
        observation_id, path, ranges, len(text.splitlines())
    )
    if not symbols:
        return problems, []
    if full.suffix == ".py":
        symbol_problems, nodes = _check_python_symbols(
            observation_id, path, text, symbols, ranges
        )
        return problems + symbol_problems, nodes
    if full.suffix in {".ts", ".tsx"}:
        problems.extend(
            _check_typescript_symbols(
                observation_id, path, symbols, ranges, ts_symbols
            )
        )
        return problems, []
    problems.append(
        f"observation {observation_id}: unsupported source type for {path}"
    )
    return problems, []


def _disposition_index(
    dispositions: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    result: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for row in dispositions:
        disposition_id = row.get("id")
        if not isinstance(disposition_id, str):
            continue
        if disposition_id in result:
            problems.append(
                f"observation authority: duplicate disposition {disposition_id}"
            )
        result[disposition_id] = row
    return result, problems


def _observation_authority_problems(
    observation_id: str,
    observation: dict[str, Any],
    disposition: dict[str, Any],
) -> list[str]:
    problems: list[str] = []
    if observation.get("source_disposition_id") != observation_id:
        problems.append(
            f"observation {observation_id}: source disposition id drift"
        )
    if disposition.get("state") != "active":
        problems.append(
            f"observation {observation_id}: source disposition is not active"
        )
    if observation.get("owner_package") != disposition.get("owner_package"):
        problems.append(
            f"observation {observation_id}: owner drift "
            f"{observation.get('owner_package')!r} != "
            f"{disposition.get('owner_package')!r}"
        )
    if observation.get("location") != disposition.get("location"):
        problems.append(
            f"observation {observation_id}: location drift from disposition"
        )
    disposition_kind = disposition.get("disposition")
    expected_kind = (
        _OBSERVATION_KIND_BY_DISPOSITION.get(disposition_kind)
        if isinstance(disposition_kind, str)
        else None
    )
    if expected_kind is None or observation.get("kind") != expected_kind:
        problems.append(
            f"observation {observation_id}: kind drift "
            f"{observation.get('kind')!r} != {expected_kind!r}"
        )
    field = (
        "classification"
        if observation.get("kind") == "typed_non_violation"
        else "concern"
    )
    if observation.get(field) != disposition.get("role_category"):
        problems.append(
            f"observation {observation_id}: {field} drift from disposition"
        )
    return problems


def _typed_observation_problem(
    observation_id: str,
    observation: dict[str, Any],
    nodes: list[ast.AST],
) -> str | None:
    if observation.get("kind") != "typed_non_violation":
        return None
    proven = bool(nodes) and all(
        isinstance(node, ast.ClassDef)
        and python_scan.is_protocol_with_zero_mutable_state(node)
        for node in nodes
    )
    if proven:
        return None
    return (
        f"observation {observation_id}: typed non-violation is not an "
        "AST-proven stateless Protocol"
    )


def check_observations_agreement(
    root: Path,
    ts_result: dict[str, Any] | None = None,
) -> list[str]:
    """Check authority facts plus all single- and multi-file source segments."""
    observations = debt.load_observations(root)
    ts_symbols = _typescript_symbol_index(ts_result)
    disposition_by_id, problems = _disposition_index(
        debt.load_dispositions(root)
    )
    seen: set[str] = set()
    for observation in observations:
        observation_id = observation.get("id")
        if not isinstance(observation_id, str) or not observation_id:
            problems.append("observation has invalid or missing id")
            continue
        if observation_id in seen:
            problems.append(f"duplicate observation id: {observation_id}")
        seen.add(observation_id)
        disposition = disposition_by_id.get(observation_id)
        if disposition is None:
            problems.append(
                f"observation {observation_id}: source disposition is absent"
            )
            continue
        problems.extend(
            _observation_authority_problems(
                observation_id, observation, disposition
            )
        )
        location = observation.get("location")
        if not isinstance(location, str) or not location.strip():
            problems.append(f"observation {observation_id}: invalid location")
            continue
        nodes: list[ast.AST] = []
        for segment in location.split(" + "):
            segment_problems, segment_nodes = _check_observation_segment(
                observation_id, segment, root, ts_symbols
            )
            problems.extend(segment_problems)
            nodes.extend(segment_nodes)
        typed_problem = _typed_observation_problem(
            observation_id, observation, nodes
        )
        if typed_problem is not None:
            problems.append(typed_problem)
    missing = sorted(
        disposition_id
        for disposition_id, row in disposition_by_id.items()
        if row.get("state") == "active"
        and row.get("disposition") in _OBSERVATION_KIND_BY_DISPOSITION
        and disposition_id not in seen
    )
    if missing:
        problems.append(
            f"observation authority: active dispositions missing observations: "
            f"{missing}"
        )
    return problems
