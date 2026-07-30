"""Compiler/AST-derived public-surface extraction."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from typing import Any

from .typescript_scan import scan_typescript


def _tracked(root: Path) -> list[str]:
    raw = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"])
    return sorted(item.decode("utf-8") for item in raw.split(b"\0") if item)


def tracked_initializers(root: Path) -> list[str]:
    return [
        rel
        for rel in _tracked(root)
        if rel.startswith("packages/") and "/src/" in rel and rel.endswith("/__init__.py")
    ]


def _module_name(rel: str) -> str:
    module = rel.split("/src/", 1)[1][:-3].replace("/", ".")
    return module.removesuffix(".__init__")


def _module_index(root: Path) -> dict[str, Path]:
    return {
        _module_name(rel): root / rel
        for rel in _tracked(root)
        if rel.startswith("packages/") and "/src/" in rel and rel.endswith(".py")
    }


def _resolve_module(
    source_module: str,
    level: int,
    imported_module: str | None,
    *,
    source_is_package: bool,
) -> str:
    if level == 0:
        return imported_module or ""
    parts = source_module.split(".")
    effective_level = level - 1 if source_is_package else level
    base = parts[: len(parts) - effective_level]
    if imported_module:
        base.extend(imported_module.split("."))
    return ".".join(base)


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"{prefix} {node.name}({ast.unparse(node.args)}){returns}"


def _is_type_checking_guard(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Name)
        and node.id == "TYPE_CHECKING"
        or isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "typing"
        and node.attr == "TYPE_CHECKING"
    )


def _static_class_items(node: ast.ClassDef) -> list[ast.stmt]:
    """Return runtime declarations plus direct static compatibility declarations."""
    items: list[ast.stmt] = []
    for item in node.body:
        items.append(item)
        if isinstance(item, ast.If) and _is_type_checking_guard(item.test):
            items.extend(item.body)
    return items


def _class_signature(node: ast.ClassDef) -> dict[str, Any]:
    bases = [ast.unparse(base) for base in node.bases]
    members: list[str] = []
    fields: list[str] = []
    for item in _static_class_items(node):
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            not item.name.startswith("_") or item.name == "__init__"
        ):
            members.append(_function_signature(item))
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            if not item.target.id.startswith("_"):
                fields.append(f"{item.target.id}: {ast.unparse(item.annotation)}")
    return {
        "kind": "class",
        "signature": f"class {node.name}({', '.join(bases)})",
        "members": sorted(members),
        "fields": sorted(fields),
    }


def _definition_surface(node: ast.stmt) -> dict[str, Any] | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return {"kind": "function", "signature": _function_signature(node)}
    if isinstance(node, ast.ClassDef):
        return _class_signature(node)
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return {
            "kind": "annotated_value",
            "signature": f"{node.target.id}: {ast.unparse(node.annotation)}",
        }
    if isinstance(node, ast.Assign):
        names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        if names:
            return {
                "kind": "value",
                "signature": f"{','.join(names)}={ast.unparse(node.value)}",
            }
    return None


def _local_definition(tree: ast.Module, name: str) -> dict[str, Any] | None:
    for node in tree.body:
        node_name = getattr(node, "name", None)
        if node_name == name:
            return _definition_surface(node)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                return _definition_surface(node)
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return _definition_surface(node)
    return None


def _matching_import(
    tree: ast.Module, public_name: str
) -> tuple[ast.ImportFrom | ast.Import, ast.alias] | None:
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for alias in node.names:
            visible = alias.asname or alias.name.split(".")[0]
            if visible == public_name:
                return node, alias
    return None


def _resolve_symbol(
    module: str,
    name: str,
    index: dict[str, Path],
    visited: set[tuple[str, str]],
) -> dict[str, Any]:
    identity = (module, name)
    if identity in visited or module not in index:
        return {"kind": "unresolved", "signature": f"{module}:{name}"}
    visited.add(identity)
    tree = ast.parse(index[module].read_text(encoding="utf-8"), filename=module)
    local = _local_definition(tree, name)
    if local:
        return local
    imported = _matching_import(tree, name)
    if imported is None:
        return {"kind": "unresolved", "signature": f"{module}:{name}"}
    node, alias = imported
    if isinstance(node, ast.Import):
        return {"kind": "module", "signature": alias.name}
    target_module = _resolve_module(
        module,
        node.level,
        node.module,
        source_is_package=index[module].name == "__init__.py",
    )
    target_name = alias.name
    if target_name == "*":
        return {"kind": "wildcard", "signature": target_module}
    if target_module not in index and f"{target_module}.{target_name}" in index:
        return {"kind": "module", "signature": f"{target_module}.{target_name}"}
    return _resolve_symbol(target_module, target_name, index, visited)


def _literal_all(tree: ast.Module) -> tuple[bool, list[str] | None]:
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            continue
        if not isinstance(node.value, (ast.List, ast.Tuple)):
            return True, None
        values = [
            item.value
            for item in node.value.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        ]
        if len(values) != len(node.value.elts):
            return True, None
        return True, sorted(values)
    return False, None


def _visible_imports(tree: ast.Module) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        for alias in node.names:
            visible = alias.asname or alias.name.split(".")[0]
            if visible.startswith("_") or alias.name == "*":
                continue
            rows.append(
                {
                    "public_name": visible,
                    "name": alias.name,
                    "module": node.module if isinstance(node, ast.ImportFrom) else alias.name,
                    "level": node.level if isinstance(node, ast.ImportFrom) else 0,
                    "alias": alias.asname,
                    "kind": "from" if isinstance(node, ast.ImportFrom) else "import",
                }
            )
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))


def _public_signatures(
    tree: ast.Module,
    module: str,
    module_index: dict[str, Path],
    public_names: list[str],
    origins: list[dict[str, Any]],
) -> dict[str, Any]:
    signatures: dict[str, Any] = {}
    for name in public_names:
        local = _local_definition(tree, name)
        if local:
            signatures[name] = local
            continue
        imported = next((row for row in origins if row["public_name"] == name), None)
        if imported is None:
            signatures[name] = {"kind": "unresolved", "signature": name}
            continue
        if imported["kind"] == "import":
            signatures[name] = {
                "kind": "module",
                "signature": imported["module"],
            }
            continue
        target = _resolve_module(
            module,
            imported["level"],
            imported["module"],
            source_is_package=True,
        )
        signatures[name] = _resolve_symbol(target, imported["name"], module_index, set())
    return signatures


def extract_initializer(
    root: Path,
    rel: str,
    index: dict[str, Path] | None = None,
) -> dict[str, Any]:
    module_index = _module_index(root) if index is None else index
    path = root / rel
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    module = _module_name(rel)
    has_all, explicit_all = _literal_all(tree)
    imports = _visible_imports(tree)
    definitions = sorted(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not node.name.startswith("_")
    )
    default_names = sorted({row["public_name"] for row in imports} | set(definitions))
    public_names = explicit_all if has_all else default_names
    if public_names is None:
        public_names = []
    origins = [row for row in imports if row["public_name"] in set(public_names)]
    signatures = _public_signatures(tree, module, module_index, public_names, origins)
    return {
        "path": rel,
        "has_all": has_all,
        "explicit_all": explicit_all,
        "public_names": sorted(public_names),
        "import_origins": origins,
        "public_signatures": signatures,
    }


def scan_python_public_surface(root: Path) -> list[dict[str, Any]]:
    index = _module_index(root)
    return [extract_initializer(root, rel, index) for rel in tracked_initializers(root)]


def scan_frontend_public_surface(root: Path) -> list[dict[str, Any]]:
    result = scan_typescript(root)
    problems = result.get("frontend_context_policy_problems", [])
    diagnostics = [
        {"path": module["path"], "diagnostics": module["parse_diagnostics"]}
        for module in result["modules"]
        if module["parse_diagnostics"]
    ]
    if problems or diagnostics:
        raise RuntimeError(
            f"frontend public-surface scan failed: policy={problems}, diagnostics={diagnostics}"
        )
    return [
        {
            "path": module["path"],
            "public_declarations": module["public_declarations"],
        }
        for module in result["modules"]
        if not module["is_test"]
    ]
