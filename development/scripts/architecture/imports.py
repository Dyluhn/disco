"""Import/context graph enforcement for the architecture gate.

Enforces package/internal/frontend context edges, cycles, static and literal
dynamic imports, using one registry shared with the generated diagram.

The package layering, top siblings, and DM-005 exemptions are read from
``development/architecture/contexts.json`` — the single checked source. No duplicated
constants. Contexts are loaded **per requested root** so temp-tree tests use
the temp repo's own registry, not a module-import frozen copy.

Detects:
  - static imports (``import x`` and ``from x import y``)
  - relative imports (``from . import y``, ``from ..x import y``)
  - function-local imports (imports inside function bodies)
  - literal ``importlib.import_module("...")`` calls, including import aliases
  - literal ``from importlib import import_module`` aliases
  - literal ``__import__("...")`` calls
  - non-literal recognized dynamic imports (as fail-closed scan errors)
  - cycles and upward (layer-violating) edges
  - exact package/context policy from ``development/architecture/contexts.json``

A zero-edge accepted graph is invalid — the accepted tree has many package
and exact DM-005 edges.
"""

from __future__ import annotations

import ast
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

from .policy import REPO_ROOT, load_json


def _load_contexts_for_root(root: Path) -> dict[str, Any]:
    """Load only the requested root's registry; absence fails closed."""
    return load_json(root / "development" / "architecture" / "contexts.json")


def _build_layer_index(contexts: dict[str, Any]) -> dict[str, int]:
    """Build the layer index from the contexts registry."""
    layers = contexts["package_layering"]["layers"]
    index: dict[str, int] = {}
    for i, layer in enumerate(layers):
        for pkg in layer:
            index[pkg] = i
    return index


def _build_top_siblings(contexts: dict[str, Any]) -> set[str]:
    """Build the top siblings set from the contexts registry."""
    return set(contexts["package_layering"].get("top_siblings_independent", []))


def _build_dm005_exemptions(contexts: dict[str, Any]) -> frozenset[str]:
    """Build the DM-005 exemption set from the contexts registry."""
    exemptions: set[str] = set()
    for obs in contexts.get("dm005_upward_edge_observations", []):
        exemptions.add(obs["edge"])
    return frozenset(exemptions)


_SEALED_PACKAGES = {
    "disco.core",
    "disco.retrieval",
    "disco.tools",
    "disco.agent_server",
    "disco.app_server",
}


def _validate_layering(contexts: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    layering = contexts.get("package_layering")
    if not isinstance(layering, dict):
        return ["package_layering must be an object"]
    layers = layering.get("layers")
    if not isinstance(layers, list) or not all(
        isinstance(layer, list) and layer for layer in layers
    ):
        return ["package layers must be non-empty string lists"]
    packages = [
        package
        for layer in layers
        for package in layer
        if isinstance(package, str)
    ]
    if set(packages) != _SEALED_PACKAGES or len(packages) != len(_SEALED_PACKAGES):
        problems.append("package layers must contain each sealed package exactly once")
    siblings = layering.get("top_siblings_independent")
    if (
        not isinstance(siblings, list)
        or set(siblings) != {"disco.agent_server", "disco.app_server"}
    ):
        problems.append("top-sibling authority mismatch")
    return problems


def _validate_permitted_edge(
    row: Any,
    seen: set[tuple[str, str]],
    layer_index: dict[str, int],
) -> str | None:
    if not isinstance(row, dict):
        return "permitted edge must be an object"
    source, target = row.get("from"), row.get("to")
    if not isinstance(source, str) or not isinstance(target, str):
        return f"invalid permitted edge endpoints: {row}"
    pair = (source, target)
    invalid = (
        source not in _SEALED_PACKAGES
        or target not in _SEALED_PACKAGES
        or row.get("kind") != "import"
        or pair in seen
        or "*" in source
        or "*" in target
    )
    seen.add(pair)
    if invalid:
        return f"invalid or duplicate permitted edge: {row}"
    if layer_index[target] <= layer_index[source]:
        return f"permitted edge is not downward: {row}"
    return None


def _validate_permitted_edges(contexts: dict[str, Any]) -> list[str]:
    permitted = contexts.get("permitted_edges")
    if not isinstance(permitted, list) or not permitted:
        return ["permitted_edges must be a non-empty exact list"]
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    layer_index = _build_layer_index(contexts)
    for row in permitted:
        problem = _validate_permitted_edge(row, seen, layer_index)
        if problem:
            problems.append(problem)
    return problems


def _valid_dm005_row(row: Any) -> bool:
    return (
        isinstance(row, dict)
        and row.get("observation_id") == "DM-005"
        and row.get("owner_package") == "PKG-10-EXECUTOR"
        and row.get("tracked_in_importlinter") is True
        and isinstance(row.get("edge"), str)
        and "*" not in row["edge"]
    )


def _validate_dm005(contexts: dict[str, Any]) -> list[str]:
    observations = contexts.get("dm005_upward_edge_observations")
    if not isinstance(observations, list) or len(observations) != 3:
        return ["DM-005 authority must contain exactly three observations"]
    return [
        f"invalid DM-005 observation: {row}"
        for row in observations
        if not _valid_dm005_row(row)
    ]


def _validate_frontend_context(contexts: dict[str, Any]) -> list[str]:
    frontend = contexts.get("bounded_contexts", {}).get("frontend")
    if not isinstance(frontend, dict) or frontend.get("root") != "frontend/src":
        return ["frontend context root authority is missing"]
    return []


def _validate_contexts(contexts: dict[str, Any]) -> list[str]:
    """Validate the registry facts used by the Python and TypeScript gates."""
    problems: list[str] = []
    if contexts.get("schema") != "disclaude-architecture-contexts-v1":
        problems.append("contexts schema mismatch")
    problems.extend(_validate_layering(contexts))
    if problems:
        return problems
    problems.extend(_validate_permitted_edges(contexts))
    problems.extend(_validate_dm005(contexts))
    problems.extend(_validate_frontend_context(contexts))
    return problems


def _permitted_pairs(contexts: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (row["from"], row["to"])
        for row in contexts["permitted_edges"]
    }


def _dep_pkg(mod: str | None, layer_index: dict[str, int]) -> str | None:
    """Return the disco.<pkg> layer key a dotted module name belongs to.

    ``layer_index`` keys are full ``disco.<package>`` strings.
    """
    if not mod:
        return None
    parts = mod.split(".")
    if len(parts) >= 2 and parts[0] == "disco":
        candidate = parts[0] + "." + parts[1]
        if candidate in layer_index:
            return candidate
    return None


def _module_name(path: str) -> str:
    """Convert a .py file path to a dotted module name."""
    if "/src/" in path and path.endswith(".py"):
        rel = path.split("/src/", 1)[1][:-3].replace("/", ".")
        return rel[:-9] if rel.endswith(".__init__") else rel
    return path[:-3].replace("/", ".") if path.endswith(".py") else path


def _resolve_relative(
    level: int, module: str | None, source_module: str, is_init: bool = False
) -> str:
    """Resolve a relative import to an absolute dotted module name.

    ``level`` is the number of leading dots. ``module`` is the module part
    after the dots (may be None for ``from . import x``).
    ``source_module`` is the dotted name of the importing module.
    ``is_init`` indicates whether the source is an ``__init__.py`` file.

    For ``__init__.py`` files, the module name IS the package name, so
    ``from .`` (level=1) stays at the same package level. For regular modules,
    ``from .`` goes up one level to the containing package.
    """
    if level == 0:
        return module or ""
    # Find the package containing the source module
    parts = source_module.split(".")
    # For __init__.py, the source_module is already the package, so
    # level=1 means "this package" (don't go up).
    # For regular modules, level=1 means "the containing package" (go up 1).
    effective_level = level - 1 if is_init else level
    if effective_level > len(parts):
        return module or ""
    base_parts = parts[: len(parts) - effective_level]
    if module:
        base_parts.append(module)
    return ".".join(base_parts)


def _tracked_python(root: Path | None = None) -> list[Path]:
    """Return tracked .py files under current/packages/ that exist in the working tree."""
    if root is None:
        root = REPO_ROOT
    raw = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"])
    result: list[Path] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        rel = item.decode()
        if rel.endswith(".py") and rel.startswith("current/packages/"):
            path = root / rel
            if path.is_file():
                result.append(path)
    return result


def _import_row(
    module_path: str,
    target: str,
    kind: str,
    line: int,
    names: list[str] | None = None,
) -> dict[str, Any]:
    """Build one import edge row."""
    return {
        "source_module": module_path,
        "target_module": target,
        "kind": kind,
        "line": line,
        "imported_names": names or [],
    }


_ORDINARY = 0
_IMPORT_MODULE = 1
_IMPORT_FUNCTION = 2
_Bindings = dict[str, int]
_NodeList = list[ast.stmt] | list[ast.expr]
_ImportRows = list[dict[str, Any]]


class _LocalBindings(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()
        self.external: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        self.names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.names.update(
            alias.asname or alias.name for alias in node.names if alias.name != "*"
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.names.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_Global(self, node: ast.Global) -> None:
        self.external.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.external.update(node.names)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self.visit(node.iter)
        for condition in node.ifs:
            self.visit(condition)


def _local_names(arguments: ast.arguments, body: _NodeList) -> set[str]:
    collector = _LocalBindings()
    for node in body:
        collector.visit(node)
    positional = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
    optional = [
        argument for argument in (arguments.vararg, arguments.kwarg) if argument is not None
    ]
    parameters = {argument.arg for argument in [*positional, *optional]}
    return (parameters | collector.names) - collector.external


class _ImportExtractor(ast.NodeVisitor):
    def __init__(self, module_path: str, is_init: bool) -> None:
        self.module_path = module_path
        self.is_init = is_init
        self.imports: list[dict[str, Any]] = []
        initial = {"__import__": _IMPORT_FUNCTION}
        self.scopes: list[tuple[_Bindings, _Bindings, str]] = [
            (initial, initial, "module")
        ]
        self.function_depth = 0
        self.binding_only = False

    def _bindings(self) -> dict[str, int]:
        return self.scopes[-1][0]

    def _lexical_bindings(self) -> dict[str, int]:
        for _, final, kind in reversed(self.scopes):
            if kind != "class":
                return final
        raise RuntimeError("module scope is missing")

    def _final_bindings(self, nodes: _NodeList, initial: _Bindings) -> _Bindings:
        current = dict(initial)
        visitor = _ImportExtractor(self.module_path, self.is_init)
        visitor.scopes = [(current, current, "binding")]
        visitor.binding_only = True
        for node in nodes:
            visitor.visit(node)
        return current

    def _identity(self, value: ast.expr) -> int:
        if isinstance(value, ast.Name):
            return self._bindings().get(value.id, _ORDINARY)
        if isinstance(value, ast.NamedExpr):
            return self._identity(value.value)
        if (
            isinstance(value, ast.Attribute)
            and value.attr == "import_module"
            and self._identity(value.value) == _IMPORT_MODULE
        ):
            return _IMPORT_FUNCTION
        return _ORDINARY

    def _bind_target(self, target: ast.AST, identity: int) -> None:
        if isinstance(target, ast.Name):
            self._bindings()[target.id] = identity
            return
        for child in ast.walk(target):
            if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
                self._bindings()[child.id] = _ORDINARY

    def _add_static(self, target: str, node: ast.stmt, names: list[str] | None = None) -> None:
        if self.binding_only:
            return
        kind = "function_local" if self.function_depth else "static"
        self.imports.append(
            _import_row(self.module_path, target, kind, node.lineno, names)
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._add_static(alias.name, node)
            bound = alias.asname or alias.name.split(".")[0]
            identity = _IMPORT_MODULE if alias.name == "importlib" else _ORDINARY
            self._bindings()[bound] = identity

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = _resolve_relative(node.level, node.module, self.module_path, self.is_init)
        names = [alias.name for alias in node.names]
        self._add_static(base, node, names)
        for alias in node.names:
            if alias.name == "*":
                continue
            identity = _ORDINARY
            if node.level == 0 and (
                (node.module == "importlib" and alias.name == "import_module")
                or (node.module == "builtins" and alias.name == "__import__")
            ):
                identity = _IMPORT_FUNCTION
            self._bindings()[alias.asname or alias.name] = identity

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        identity = self._identity(node.value)
        for target in node.targets:
            self._bind_target(target, identity)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)
            self._bind_target(node.target, self._identity(node.value))

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.target)
        self.visit(node.value)
        self._bind_target(node.target, _ORDINARY)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._bind_target(node.target, self._identity(node.value))

    def visit_Call(self, node: ast.Call) -> None:
        if not self.binding_only and self._identity(node.func) == _IMPORT_FUNCTION:
            target = (
                node.args[0].value
                if node.args
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                else None
            )
            kind = "dynamic_literal" if target is not None else "dynamic_nonliteral"
            self.imports.append(
                _import_row(self.module_path, target or "", kind, node.lineno)
            )
        self.generic_visit(node)

    def _visit_deferred(self, arguments: ast.arguments, body: _NodeList) -> None:
        local = dict(self._lexical_bindings())
        for name in _local_names(arguments, body):
            local[name] = _ORDINARY
        final = self._final_bindings(body, local)
        self.scopes.append((local, final, "function"))
        self.function_depth += 1
        for child in body:
            self.visit(child)
        self.function_depth -= 1
        self.scopes.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.visit(node.args)
        if node.returns:
            self.visit(node.returns)
        self._bindings()[node.name] = _ORDINARY
        if self.binding_only:
            return
        self._visit_deferred(node.args, node.body)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.visit(node.args)
        if self.binding_only:
            return
        self._visit_deferred(node.args, [node.body])

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expression in [*node.decorator_list, *node.bases]:
            self.visit(expression)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self._bindings()[node.name] = _ORDINARY
        if self.binding_only:
            return
        local = dict(self._bindings())
        self.scopes.append((local, local, "class"))
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()


def _extract_imports(tree: ast.Module, module_path: str, is_init: bool = False) -> _ImportRows:
    extractor = _ImportExtractor(module_path, is_init)
    initial = dict(extractor._bindings())
    final = extractor._final_bindings(tree.body, initial)
    extractor.scopes[0] = (initial, final, "module")
    extractor.visit(tree)
    return extractor.imports


def build_import_graph(root: Path | None = None) -> dict[str, Any]:
    """Build the full import graph from tracked Python source.

    Returns a dict with:
      - edges: list of {from_pkg, to_pkg, source_module, target_module, kind, line}
      - package_edges: set of (from_pkg, to_pkg) tuples
      - cycles: list of cycles found
      - upward_edges: list of upward import edges

    Contexts (layer index, top siblings, DM-005 exemptions) are loaded from
    the requested root's ``development/architecture/contexts.json`` so temp-tree tests
    use their own registry, not a module-import frozen copy.
    """
    if root is None:
        root = REPO_ROOT
    contexts = _load_contexts_for_root(root)
    layer_index = _build_layer_index(contexts)
    dm005_exemptions = _build_dm005_exemptions(contexts)

    files = _tracked_python(root)
    edges: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    dynamic_import_errors: list[dict[str, Any]] = []

    for path in files:
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=rel)
        except (SyntaxError, UnicodeDecodeError) as exc:
            errors.append({"path": rel, "error": repr(exc)})
            continue
        module = _module_name(rel)
        source_pkg = _dep_pkg(module, layer_index)
        if source_pkg is None:
            continue
        is_init = rel.endswith("/__init__.py")
        for imp in _extract_imports(tree, module, is_init):
            if imp["kind"] == "dynamic_nonliteral":
                dynamic_import_errors.append(
                    {
                        "source_module": module,
                        "source_path": rel,
                        "line": imp["line"],
                    }
                )
                continue
            target_pkg = _dep_pkg(imp["target_module"], layer_index)
            if target_pkg is None or target_pkg == source_pkg:
                continue
            edge = {
                "from_pkg": source_pkg,
                "to_pkg": target_pkg,
                "source_module": imp["source_module"],
                "target_module": imp["target_module"],
                "kind": imp["kind"],
                "line": imp["line"],
                "source_path": rel,
                "imported_names": imp.get("imported_names", []),
            }
            edges.append(edge)

    # Package-level edges (excluding DM-005 exempted upward edges for cycle
    # detection — those are tracked observations, not real dependencies)
    package_edges: set[tuple[str, str]] = set()
    package_edges_for_cycles: set[tuple[str, str]] = set()
    for edge in edges:
        pkg_pair = (edge["from_pkg"], edge["to_pkg"])
        package_edges.add(pkg_pair)
        if not _edge_is_exempt(edge, dm005_exemptions):
            package_edges_for_cycles.add(pkg_pair)

    # Detect upward edges
    upward_edges: list[dict[str, Any]] = []
    for edge in edges:
        if layer_index.get(edge["to_pkg"], 0) < layer_index.get(edge["from_pkg"], 0):
            upward_edges.append(edge)

    # Detect cycles using DFS (excluding DM-005 exempted edges)
    cycles = _detect_cycles(package_edges_for_cycles)

    return {
        "edges": edges,
        "package_edges": sorted(package_edges),
        "enforced_package_edges": sorted(package_edges_for_cycles),
        "cycles": cycles,
        "upward_edges": upward_edges,
        "errors": errors,
        "dynamic_import_errors": dynamic_import_errors,
    }


def _detect_cycles(edges: set[tuple[str, str]]) -> list[list[str]]:
    """Detect cycles in the package dependency graph using DFS."""
    graph: dict[str, list[str]] = defaultdict(list)
    for a, b in edges:
        graph[a].append(b)

    cycles: list[list[str]] = []
    visited: set[str] = set()
    rec_stack: list[str] = []

    def dfs(node: str) -> None:
        visited.add(node)
        rec_stack.append(node)
        for neighbor in graph.get(node, []):
            if neighbor not in visited:
                dfs(neighbor)
            elif neighbor in rec_stack:
                idx = rec_stack.index(neighbor)
                cycles.append(rec_stack[idx:] + [neighbor])
        rec_stack.pop()

    for node in graph:
        if node not in visited:
            dfs(node)

    return cycles


def _edge_is_exempt(
    edge: dict[str, Any], dm005_exemptions: frozenset[str]
) -> bool:
    """Check whether an upward edge is an exact DM-005 observation."""
    exemptions = dm005_exemptions
    edge_str = f"{edge['source_module']} -> {edge['target_module']}"
    if edge_str in exemptions:
        return True
    for name in edge.get("imported_names", []):
        full = f"{edge['source_module']} -> {edge['target_module']}.{name}"
        if full in exemptions:
            return True
    return False


def _check_upward_edges(
    upward_edges: list[dict[str, Any]],
    dm005_exemptions: frozenset[str],
) -> list[str]:
    """Check for non-exempted upward import edges."""
    problems: list[str] = []
    for edge in upward_edges:
        if _edge_is_exempt(edge, dm005_exemptions):
            continue
        edge_str = f"{edge['source_module']} -> {edge['target_module']}"
        problems.append(
            f"upward import: {edge_str} "
            f"({edge['from_pkg']} -> {edge['to_pkg']}) "
            f"at {edge['source_path']}:{edge['line']}"
        )
    return problems


def _check_top_siblings(
    edges: list[dict[str, Any]], top_siblings: set[str]
) -> list[str]:
    """Check that top-sibling packages are independent."""
    siblings = top_siblings
    problems: list[str] = []
    for edge in edges:
        if (
            edge["from_pkg"] in siblings
            and edge["to_pkg"] in siblings
            and edge["from_pkg"] != edge["to_pkg"]
        ):
            problems.append(
                f"top siblings not independent: {edge['from_pkg']} -> "
                f"{edge['to_pkg']} at {edge['source_path']}:{edge['line']}"
            )
    return problems


def _collect_exemption_edges(
    upward_edges: list[dict[str, Any]],
    dm005_exemptions: frozenset[str],
) -> set[str]:
    """Collect the set of DM-005 exemption edges actually found."""
    exemptions = dm005_exemptions
    found: set[str] = set()
    for edge in upward_edges:
        edge_str = f"{edge['source_module']} -> {edge['target_module']}"
        if edge_str in exemptions:
            found.add(edge_str)
        for name in edge.get("imported_names", []):
            full = f"{edge['source_module']} -> {edge['target_module']}.{name}"
            if full in exemptions:
                found.add(full)
    return found


def check_imports(root: Path | None = None) -> dict[str, Any]:
    """Run the full import/context check and return a result dict.

    Contexts (layer index, top siblings, DM-005 exemptions) are loaded from
    the requested root's ``development/architecture/contexts.json``.
    """
    if root is None:
        root = REPO_ROOT
    contexts = _load_contexts_for_root(root)
    registry_problems = _validate_contexts(contexts)
    if registry_problems:
        return {
            "ok": False,
            "problems": registry_problems,
            "edge_count": 0,
            "upward_edge_count": 0,
            "cycle_count": 0,
            "exemption_edges_found": [],
        }
    top_siblings = _build_top_siblings(contexts)
    dm005_exemptions = _build_dm005_exemptions(contexts)

    graph = build_import_graph(root)
    problems: list[str] = []

    # A zero-edge accepted graph is invalid
    if not graph["edges"] and not graph["errors"]:
        problems.append(
            "import graph has zero edges — the accepted tree has many "
            "package and exact DM-005 edges; the scanner is broken"
        )

    # Check for parse errors (fail-closed)
    for err in graph["errors"]:
        problems.append(f"parse error in {err['path']}: {err['error']}")

    for err in graph["dynamic_import_errors"]:
        problems.append(
            "non-literal dynamic import must use an exact string target: "
            f"{err['source_module']} at {err['source_path']}:{err['line']}"
        )

    # Check for cycles
    for cycle in graph["cycles"]:
        problems.append(f"dependency cycle: {' -> '.join(cycle)}")

    # Check for non-exempted upward edges
    problems.extend(_check_upward_edges(graph["upward_edges"], dm005_exemptions))

    # Check top siblings are independent
    problems.extend(_check_top_siblings(graph["edges"], top_siblings))

    unexpected_pairs = (
        set(graph["enforced_package_edges"]) - _permitted_pairs(contexts)
    )
    if unexpected_pairs:
        problems.append(
            f"package imports absent from permitted_edges: {sorted(unexpected_pairs)}"
        )

    # Cross-check .importlinter against the three DM-005 observations
    problems.extend(_check_importlinter_exemptions(root, dm005_exemptions))

    # Collect DM-005 exemption edges found
    exemption_edges_found = _collect_exemption_edges(graph["upward_edges"], dm005_exemptions)
    if exemption_edges_found != set(dm005_exemptions):
        problems.append(
            "DM-005 source observations drift: "
            f"expected {sorted(dm005_exemptions)}, "
            f"found {sorted(exemption_edges_found)}"
        )

    return {
        "ok": len(problems) == 0,
        "problems": problems,
        "edge_count": len(graph["edges"]),
        "upward_edge_count": len(graph["upward_edges"]),
        "cycle_count": len(graph["cycles"]),
        "exemption_edges_found": sorted(exemption_edges_found),
    }


def _check_importlinter_exemptions(
    root: Path,
    dm005_exemptions: frozenset[str],
) -> list[str]:
    """Cross-check .importlinter against exactly the three DM-005 observations.

    The .importlinter ``ignore_imports`` must match exactly — no wildcard,
    no additional entries, no removal of an existing edge.
    """
    problems: list[str] = []
    exemptions = dm005_exemptions
    importlinter_path = root / ".importlinter"
    if not importlinter_path.is_file():
        problems.append(".importlinter is missing")
        return problems

    text = importlinter_path.read_text(encoding="utf-8")
    # Extract ignore_imports entries
    in_ignore = False
    found_exemptions: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("ignore_imports"):
            in_ignore = True
            continue
        if in_ignore:
            if stripped and not stripped.startswith("#"):
                if "->" in stripped:
                    found_exemptions.append(stripped)
            elif not stripped:
                in_ignore = False

    found_set = set(found_exemptions)
    expected_set = set(exemptions)

    # Check for wildcard exemptions
    for exp in found_exemptions:
        if "*" in exp:
            problems.append(f"wildcard exemption in .importlinter: {exp} — wildcards are forbidden")

    # Check for additional exemptions (broadening)
    extra = found_set - expected_set
    if extra:
        problems.append(f"additional exemptions in .importlinter (broadening): {sorted(extra)}")

    # Check for removed exemptions (must not shrink .importlinter without
    # resolving the disposition)
    # Note: the DM-005 exemptions may shrink when the owning package resolves
    # them, but the .importlinter must not remove them independently
    missing = expected_set - found_set
    if missing:
        problems.append(
            f"missing exemptions in .importlinter: {sorted(missing)} — "
            "exemptions may shrink only when the owning package resolves "
            "the disposition"
        )

    return problems


def load_contexts(root: Path | None = None) -> dict[str, Any]:
    """Load the contexts registry.

    Defaults to the stable repository, never to an imported live registry.
    """
    resolved_root = REPO_ROOT if root is None else root
    return _load_contexts_for_root(resolved_root)
