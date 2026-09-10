"""AST-based Python structure scanner for the architecture budget gate.

Replicates the exact logical-LOC and McCabe rules from the mapping scanner so
the gate and the mapping inventory agree on every metric.
"""

from __future__ import annotations

import ast
import subprocess
import tokenize
from collections import Counter
from collections.abc import Iterable
from io import StringIO
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]


def tracked_python_files(root: Path | None = None) -> list[Path]:
    """Return tracked .py files under the scan root prefixes.

    Only files present in the working tree are returned: the budget scanner
    measures current source, not git history. A tracked file deleted from the
    working tree is not current source and is skipped.
    """
    if root is None:
        root = REPO_ROOT
    raw = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"])
    prefixes = ("packages/", "development/harness/", "development/scripts/", "development/tests/")
    result: list[Path] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        rel = item.decode()
        if rel.endswith(".py") and rel.startswith(prefixes):
            path = root / rel
            if path.is_file():
                result.append(path)
    return result


def string_lines(text: str) -> set[int]:
    """Return line numbers covered by string tokens (multiline strings count)."""
    result: set[int] = set()
    try:
        for token in tokenize.generate_tokens(StringIO(text).readline):
            if token.type == tokenize.STRING:
                result.update(range(token.start[0], token.end[0] + 1))
    except (tokenize.TokenError, IndentationError):
        pass
    return result


def logical_line_set(text: str) -> set[int]:
    """Return the set of logical (nonblank, non-comment-only) line numbers.

    Multiline-string content counts as logical source.
    """
    strings = string_lines(text)
    result: set[int] = set()
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") and number not in strings:
            continue
        result.add(number)
    return result


class Complexity(ast.NodeVisitor):
    """AST-McCabe: 1 + branch/loop/handler/comprehension + boolean extra paths.

    A callable's McCabe score does not absorb branches declared inside a nested
    callable or class. Those declarations receive their own rows.
    """

    def __init__(self) -> None:
        self.value = 1

    def visit_If(self, node: ast.If) -> None:
        self.value += 1
        self.generic_visit(node)

    visit_IfExp = visit_If

    def visit_For(self, node: ast.For) -> None:
        self.value += 1
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_While(self, node: ast.While) -> None:
        self.value += 1
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        self.value += len(node.handlers)
        self.generic_visit(node)

    visit_TryStar = visit_Try

    def visit_With(self, node: ast.With) -> None:
        self.generic_visit(node)

    visit_AsyncWith = visit_With

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self.value += max(0, len(node.values) - 1)
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self.value += 1 + len(node.ifs)
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:
        self.value += max(0, len(node.cases) - 1)
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self.value += 1
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return


def complexity(node: ast.AST) -> int:
    """Compute AST-McCabe for a node (not descending into nested callables)."""
    visitor = Complexity()
    for child in ast.iter_child_nodes(node):
        visitor.visit(child)
    return visitor.value


def annotation_name(node: ast.expr | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def import_names(tree: ast.Module) -> list[str]:
    """Extract import module names from an AST (static imports only)."""
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level + (node.module or "")
            result.add(prefix)
    return sorted(result)


def is_test_path(rel: str) -> bool:
    """Check whether a path is a test module."""
    return (
        "/tests/" in f"/{rel}"
        or Path(rel).name.startswith("test_")
        or rel.startswith("development/tests/")
    )


def root_name(rel: str) -> str:
    """Return the root name for a relative path."""
    if rel.startswith("packages/"):
        return "/".join(rel.split("/")[:2])
    return rel.split("/", 1)[0]


def class_public_methods(node: ast.ClassDef) -> list[str]:
    """Return public method names (non-underscore) defined directly in a class."""
    return [
        child.name
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not child.name.startswith("_")
    ]


def init_parameters(node: ast.ClassDef) -> list[dict[str, str]]:
    """Return __init__ parameters (excluding self/cls) with their annotations."""
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == "__init__":
            args = [*child.args.posonlyargs, *child.args.args, *child.args.kwonlyargs]
            return [
                {"name": arg.arg, "annotation": annotation_name(arg.annotation)}
                for arg in args
                if arg.arg not in {"self", "cls"}
            ]
    return []


# ---------------------------------------------------------------------------
# Collaborator classification — scalar/config vs injected service collaborators
# ---------------------------------------------------------------------------

# Built-in scalar/config type names that are NOT collaborators.
_SCALAR_CONFIG_TYPES = frozenset(
    "Callable Config Enum Literal None NoneType OperatingMode Optional Path PathLike "
    "RouterConfig SandboxSpec Union bool bytes date datetime float int str time timedelta".split()
)
_GENERIC_CONTAINER_TYPES = frozenset(
    "Any Dict Iterable Iterator Mapping Sequence dict frozenset list set tuple".split()
)
_SCALAR_NAME_TOKENS = frozenset(
    (
        "assist autonomous backoff config delay depth enable grace host interval limit "
        "max min mode name path port providers quiet retry spec surface timeout "
        "title ttl url version"
    ).split()
)
_COLLABORATOR_NAME_TOKENS = frozenset(
    "client clients port ports repositories repository router runtime service services "
    "store stores transport transports verifier verifiers".split()
)
_COLLABORATOR_TYPE_SUFFIXES = tuple(
    "Client Port Repository Router Runtime Service Store Transport Verifier".split()
)

# Named value/callback seams are configuration inputs, not long-lived injected
# service collaborators.  These aliases are intentionally exact: broad suffix
# matching (for example every ``*Factory``) would let a service factory evade
# the cap.
_VALUE_INPUT_TYPES = frozenset(
    "ControlFenceFactory SealabilityProbe StuckThresholds TerminalCommitHook WorkflowRun".split()
)

# Disguise patterns — parameter annotations that hide a collaborator aggregate
# behind a generic container. These are rejected: a parameter typed as one of
# these is NOT a valid scalar/config exclusion and is flagged as a disguised
# collaborator aggregate.
#
# The disguise check is name+annotation based: a parameter is disguised when
# its annotation base is a generic container (dict, Any) AND its name matches
# a collaborator-aggregate disguise pattern (Context, Services, Runtime, Deps).
# A ``dict[str, Any]`` parameter named ``research_providers`` is a config input
# (name-based scalar/config pattern), NOT a disguised collaborator.
_DISGUISE_ANNOTATION_PATTERNS = _GENERIC_CONTAINER_TYPES
_DISGUISE_NAME_PATTERNS = frozenset(
    "Context Services Runtime Deps context ctx services runtime deps".split()
)
# Exact disguise type names (used when the annotation IS the pattern, e.g.
# ``def __init__(self, ctx: Context)`` or ``def __init__(self, deps: Deps)``).
_DISGUISE_EXACT_TYPES = frozenset({"Context", "Services", "Runtime", "Deps"})


def _parameter_name_tokens(name: str) -> set[str]:
    tokens = (token.rstrip("0123456789") for token in name.lower().split("_"))
    return {token for token in tokens if token}


def _annotation_base(annotation: str) -> str:
    """Return the base type name from an annotation string.

    Strips subscription (``dict[str, Any]`` → ``dict``), Optional
    (``Optional[Foo]`` → ``Foo``), and union (``Foo | None`` → ``Foo``)
    to identify the primary type.
    """
    if not annotation:
        return ""
    # Strip whitespace
    ann = annotation.strip().removeprefix("typing.").removeprefix("collections.abc.")
    # Handle ``X | None`` union — take the first non-None arm
    if "|" in ann:
        parts = [p.strip() for p in ann.split("|")]
        non_none = [p for p in parts if p and p != "None"]
        if non_none:
            ann = non_none[0]
    # Handle ``Optional[X]`` — unwrap
    if ann.startswith("Optional[") and ann.endswith("]"):
        ann = ann[len("Optional["):-1]
    if ann.startswith("Union[") and ann.endswith("]"):
        ann = ann[len("Union["):-1].split(",", 1)[0].strip()
    # Handle subscription ``X[...]`` — take the base
    if "[" in ann:
        ann = ann.split("[", 1)[0]
    return ann.strip()


def is_scalar_config_param(name: str, annotation: str) -> bool:
    """Check whether a constructor parameter is a scalar/config input.

    Scalar/config inputs are typed separately from injected service
    collaborators. A parameter is scalar/config when:
      - its annotation base is a built-in scalar/config type, OR
      - its name matches a known scalar/config pattern (config, mode, enable_*,
        *_spec, *_providers, timeout, etc.)

    Parameters typed as disguise patterns (dict, Any, Context, Services, Runtime,
    Deps) are NOT scalar/config — they are disguised collaborator aggregates.
    A ``dict[str, Any]`` parameter named ``research_providers`` is a config
    input (name-based scalar/config pattern), NOT a disguised collaborator.
    """
    base = _annotation_base(annotation)
    if base in _DISGUISE_EXACT_TYPES:
        return False
    if base in _VALUE_INPUT_TYPES or base in _SCALAR_CONFIG_TYPES:
        return True
    if base.endswith(_COLLABORATOR_TYPE_SUFFIXES):
        return False
    name_lower = name.lower()
    tokens = _parameter_name_tokens(name)
    if name_lower in {"appkit_mode", "artifact_mode", "model_policy"} or (
        tokens.isdisjoint(_COLLABORATOR_NAME_TOKENS)
        and not tokens.isdisjoint(_SCALAR_NAME_TOKENS)
    ):
        return True
    return False


def is_disguised_collaborator(name: str, annotation: str) -> bool:
    """Check whether a parameter is a disguised collaborator aggregate.

    A parameter is disguised when:
      - its annotation base is an exact disguise type (Context, Services,
        Runtime, Deps), OR
      - its annotation base is a generic container (dict, Any) AND its name
        matches a collaborator-aggregate disguise pattern (context, services,
        runtime, deps).

    A ``dict[str, Any]`` parameter named ``research_providers`` is NOT
    disguised — it is a config input (name-based scalar/config pattern).
    """
    base = _annotation_base(annotation)
    # Exact disguise type names
    if base in _DISGUISE_EXACT_TYPES:
        return True
    # Generic container + disguise name pattern
    if base in _DISGUISE_ANNOTATION_PATTERNS:
        name_lower = name.lower()
        if (
            name in _DISGUISE_NAME_PATTERNS
            or name_lower in _DISGUISE_NAME_PATTERNS
            or not _parameter_name_tokens(name).isdisjoint(
                _COLLABORATOR_NAME_TOKENS
            )
        ):
            return True
    return False


def classify_init_parameters(
    node: ast.ClassDef,
) -> dict[str, list[dict[str, str]]]:
    """Classify __init__ parameters into collaborators and scalar/config.

    Returns a dict with ``collaborators`` and ``scalar_config`` lists, each
    containing ``{"name": ..., "annotation": ...}`` dicts. Disguised parameters
    (dict, Any, Context, Services, Runtime, Deps) are listed in a separate
    ``disguised`` list.
    """
    params = init_parameters(node)
    collaborators: list[dict[str, str]] = []
    scalar_config: list[dict[str, str]] = []
    disguised: list[dict[str, str]] = []
    for param in params:
        if is_disguised_collaborator(param["name"], param["annotation"]):
            disguised.append(param)
        elif is_scalar_config_param(param["name"], param["annotation"]):
            scalar_config.append(param)
        else:
            collaborators.append(param)
    return {
        "collaborators": collaborators,
        "scalar_config": scalar_config,
        "disguised": disguised,
    }


def constructor_collaborator_count(node: ast.ClassDef) -> int:
    """Return the number of constructor collaborators (excluding scalar/config).

    Disguised parameters (dict, Any, Context, Services, Runtime, Deps) are
    counted as collaborators — they hide an aggregate behind a generic type.
    """
    classified = classify_init_parameters(node)
    return len(classified["collaborators"]) + len(classified["disguised"])


# ---------------------------------------------------------------------------
# Protocol detection — AST-proven Protocol inheritance + zero mutable state
# ---------------------------------------------------------------------------


def is_protocol_class(node: ast.ClassDef) -> bool:
    """Check whether a class has an exact typing.Protocol base."""
    for base in node.bases:
        base_name = annotation_name(base)
        if base_name == "Protocol" or base_name == "typing.Protocol":
            return True
    return False


def has_mutable_instance_state(node: ast.ClassDef) -> bool:
    """Check whether a class has mutable instance state (self.X = ... assignments).

    A Protocol with zero mutable implementation state is a typed non-violation.
    This walks the class body looking for ``self.X = ...`` assignments in
    methods (excluding ``@staticmethod`` / ``@classmethod`` methods).
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Assign):
            for target in child.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    return True
        if isinstance(child, ast.AnnAssign):
            target = child.target
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                return True
    return False


def is_protocol_with_zero_mutable_state(node: ast.ClassDef) -> bool:
    """Check whether a class is a Protocol with zero mutable implementation state."""
    return is_protocol_class(node) and not has_mutable_instance_state(node)


# ---------------------------------------------------------------------------
# Dependency aggregate member detection
# ---------------------------------------------------------------------------


def is_dependency_aggregate_assignment(node: ast.AST) -> bool:
    """Check whether an assignment is a dependency aggregate.

    A dependency aggregate is a module-level or class-level assignment of a
    collection of service/port/repository/client/runtime names. This detects
    ``frozenset({...})``, ``set({...})``, ``[...]``, ``({...})`` patterns.
    """
    if isinstance(node, ast.Assign):
        return _is_aggregate_value(node.value)
    if isinstance(node, ast.AnnAssign):
        return node.value is not None and _is_aggregate_value(node.value)
    return False


def _is_aggregate_value(value: ast.AST) -> bool:
    """Check whether a value expression is an aggregate of service-like names."""
    if isinstance(value, ast.Call):
        # frozenset({...}), set({...}), dict({...})
        func_name = annotation_name(value.func)
        if func_name in ("frozenset", "set", "frozenset"):
            for arg in value.args:
                if _is_aggregate_value(arg):
                    return True
        return False
    if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
        return len(value.elts) > 0
    return False


def aggregate_member_count(node: ast.AST) -> int:
    """Return the number of members in a dependency aggregate assignment."""
    if isinstance(node, ast.Assign):
        return _aggregate_member_count_value(node.value)
    if isinstance(node, ast.AnnAssign):
        return _aggregate_member_count_value(node.value) if node.value else 0
    return 0


def _aggregate_member_count_value(value: ast.AST) -> int:
    """Return the number of members in an aggregate value expression."""
    if isinstance(value, ast.Call):
        func_name = annotation_name(value.func)
        if func_name in ("frozenset", "set"):
            for arg in value.args:
                count = _aggregate_member_count_value(arg)
                if count > 0:
                    return count
        return 0
    if isinstance(value, ast.Set):
        return len(value.elts)
    if isinstance(value, ast.List):
        return len(value.elts)
    if isinstance(value, ast.Tuple):
        return len(value.elts)
    return 0


# ---------------------------------------------------------------------------
# Composition root detection — wiring-only effects
# ---------------------------------------------------------------------------

# Effect-owning call patterns that a composition root must NOT own directly.
# These are the same effect categories the route adapter check uses.
_EFFECT_CALL_EXACT = frozenset({
    # persistence
    "sqlite3.connect", "open", "json.dump", "json.load", "shelve.open",
    # process
    "create_subprocess", "os.system",
    # filesystem
    "os.mkdir", "os.makedirs", "Path.mkdir", "pathlib.Path.mkdir", "shutil.rmtree",
    "shutil.copy", "shutil.move", "os.remove", "os.unlink",
    # network
    "socket.socket",
    # temporary filesystem ownership and path-instance writes
    "tempfile.TemporaryDirectory", "tempfile.NamedTemporaryFile",
})
_EFFECT_CALL_PREFIXES = ("subprocess.", "asyncio.create_subprocess", "os.exec")
_EFFECT_CALL_EXACT_NETWORK = frozenset({
    "httpx.get", "httpx.post", "httpx.put", "httpx.patch",
    "httpx.delete", "httpx.request", "requests.get", "requests.post",
    "requests.put", "requests.patch", "requests.delete", "requests.request",
    "urllib.request.urlopen", "urllib.request.urlretrieve", "aiohttp.request",
    "websockets.connect", "websockets.serve",
})
_EFFECT_CALL_SUFFIXES = (".mkdir", ".write_bytes", ".write_text", ".unlink", ".rmdir")


def effect_import_aliases(
    module: ast.Module,
    scope: ast.AST | None = None,
) -> dict[str, str]:
    """Map module and callable-local aliases to canonical effect API names."""
    aliases: dict[str, str] = {}
    nodes: list[ast.AST] = [
        child for statement in module.body
        if not isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        for child in ast.walk(statement)
    ]
    if scope is not None:
        nodes.extend(ast.walk(scope))
    for child in nodes:
        if not isinstance(child, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(child, ast.ImportFrom):
            if not child.module:
                continue
            prefix = f"{child.module}."
        else:
            prefix = ""
        for item in child.names:
            if item.name == "*":
                continue
            local = item.asname or item.name.split(".", 1)[0]
            aliases[local] = (
                f"{prefix}{item.name}" if prefix or item.asname else local
            )
    return aliases


def resolve_import_alias(name: str, aliases: dict[str, str] | None) -> str:
    """Resolve the leading identifier of a dotted name through exact imports."""
    head, separator, tail = name.partition(".")
    if not aliases or head not in aliases:
        return name
    return f"{aliases[head]}.{tail}" if separator else aliases[head]


def is_direct_effect_call(
    node: ast.Call,
    aliases: dict[str, str] | None = None,
) -> bool:
    """Classify a call by alias-resolved exact API identity/prefix/suffix."""
    name = resolve_import_alias(annotation_name(node.func), aliases)
    return (
        name in _EFFECT_CALL_EXACT
        or name in _EFFECT_CALL_EXACT_NETWORK
        or name.startswith(_EFFECT_CALL_PREFIXES)
        or name.endswith(_EFFECT_CALL_SUFFIXES)
    )


def has_direct_effect_calls(
    node: ast.AST,
    aliases: dict[str, str] | None = None,
) -> bool:
    """Check whether a function body directly calls effect-owning operations.

    A composition root is wiring-only: it assembles collaborators and wires
    routers/middleware. It must NOT directly own persistence, process,
    filesystem, or network effects. Effect calls that are delegated to
    injected collaborators (``self.store.append()``, ``runtime.kick()``) are
    NOT direct effects — they are delegation.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and is_direct_effect_call(child, aliases):
            return True
    return False


def decorators(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> list[str]:
    """Return decorator source strings for a node."""
    values = []
    for dec in node.decorator_list:
        try:
            values.append(ast.unparse(dec))
        except Exception:
            values.append(type(dec).__name__)
    return values


_ROUTE_DECORATORS = frozenset({
    "api_route", "delete", "get", "head", "options", "patch", "post", "put", "route",
    "trace", "websocket",
})


def is_route(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    rel: str,
    aliases: dict[str, str] | None = None,
) -> bool:
    """Check whether a function is an HTTP/WS route handler."""
    if "/routes/" not in f"/{rel}" and not rel.endswith("/routes.py"):
        return False
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        identity = resolve_import_alias(annotation_name(decorator.func), aliases)
        if identity.rsplit(".", 1)[-1] in _ROUTE_DECORATORS:
            return True
    return False


def iter_symbols(tree: ast.Module) -> Iterable[ast.AST]:
    """Yield all class and function definitions in an AST."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def build_qualified_name_map(tree: ast.Module) -> dict[tuple[int, int, str], str]:
    """Build a map from (lineno, end_lineno, name) to qualified symbol name.

    Recursively walks the AST to produce qualified names like
    ``ClassName.method_name`` for nested symbols. This is used to match
    debt rows by ``(path, qualified_symbol, rule)`` identity.
    """
    result: dict[tuple[int, int, str], str] = {}

    def walk(node: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                qname = f"{prefix}.{child.name}" if prefix else child.name
                end = int(child.end_lineno or child.lineno)
                result[(child.lineno, end, child.name)] = qname
                walk(child, qname)
            else:
                walk(child, prefix)

    walk(tree)
    return result


def _violation(
    rule: str, symbol: str, qname: str, node: ast.AST, value: int, limit: int
) -> dict[str, Any]:
    """Build a violation dict."""
    return {
        "rule": rule,
        "symbol": symbol,
        "qualified_symbol": qname,
        "line_start": node.lineno,
        "line_end": int(node.end_lineno or node.lineno),
        "value": value,
        "limit": limit,
    }


def _class_violations(
    node: ast.ClassDef, qname: str, logical_size: int, test: bool
) -> list[dict[str, Any]]:
    """Collect violations for a class symbol."""
    result: list[dict[str, Any]] = []
    if test:
        return result
    if logical_size > 350:
        result.append(
            _violation("python_class_logical_gt_350", node.name, qname, node, logical_size, 350)
        )
    public_methods = class_public_methods(node)
    if len(public_methods) > 12:
        result.append(
            _violation(
                "python_service_public_methods_gt_12_candidate",
                node.name,
                qname,
                node,
                len(public_methods),
                12,
            )
        )
    return result


def _callable_violations(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    qname: str,
    logical_size: int,
    mccabe: int,
    test: bool,
    rel: str,
    aliases: dict[str, str],
) -> list[dict[str, Any]]:
    """Collect violations for a callable symbol."""
    result: list[dict[str, Any]] = []
    if test:
        return result
    if logical_size > 100:
        result.append(
            _violation("python_callable_logical_gt_100", node.name, qname, node, logical_size, 100)
        )
    if mccabe > 15:
        result.append(
            _violation("python_callable_ast_mccabe_gt_15", node.name, qname, node, mccabe, 15)
        )
    if is_route(node, rel, aliases) and logical_size > 80:
        result.append(
            _violation(
                "http_ws_route_handler_logical_gt_80",
                node.name,
                qname,
                node,
                logical_size,
                80,
            )
        )
    return result


def _module_violations(
    rel: str, logical_count: int, line_count: int, test: bool
) -> list[dict[str, Any]]:
    """Collect module-level violations."""
    result: list[dict[str, Any]] = []
    module_limit = 1200 if test else 700
    if not test and rel.startswith("development/harness/"):
        module_limit = 700
    if logical_count > module_limit:
        rule = "test_module_logical_gt_1200" if test else "python_or_harness_module_logical_gt_700"
        result.append({
            "rule": rule,
            "symbol": "<module>",
            "qualified_symbol": "<module>",
            "line_start": 1,
            "line_end": line_count,
            "value": logical_count,
            "limit": module_limit,
        })
    if not test and "/oracles/" in f"/{rel}" and logical_count > 400:
        result.append({
            "rule": "harness_oracle_module_logical_gt_400",
            "symbol": "<module>",
            "qualified_symbol": "<module>",
            "line_start": 1,
            "line_end": line_count,
            "value": logical_count,
            "limit": 400,
        })
    return result


def _symbol_row(
    node: ast.AST, qname: str, logical_size: int, physical: int
) -> dict[str, Any]:
    """Build a symbol row dict."""
    kind = "class" if isinstance(node, ast.ClassDef) else "function"
    row: dict[str, Any] = {
        "kind": kind,
        "name": node.name,
        "qualified_symbol": qname,
        "line_start": node.lineno,
        "line_end": int(node.end_lineno or node.lineno),
        "physical_span": physical,
        "policy_logical_loc": logical_size,
        "decorators": decorators(node),
    }
    if isinstance(node, ast.ClassDef):
        public_methods = class_public_methods(node)
        row["public_methods"] = public_methods
        row["public_method_count"] = len(public_methods)
        row["constructor_parameters"] = init_parameters(node)
    else:
        row["ast_mccabe"] = complexity(node)
    return row


def scan_file(path: Path, root: Path | None = None) -> dict[str, Any]:
    """Scan a single Python file and return its structure and violations.

    Raises SyntaxError/UnicodeDecodeError on parse failure (fail-closed).
    """
    if root is None:
        root = REPO_ROOT
    rel = path.relative_to(root).as_posix()
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    logical = logical_line_set(text)
    tree = ast.parse(text, filename=rel)
    test = is_test_path(rel)
    qname_map = build_qualified_name_map(tree)
    aliases = effect_import_aliases(tree)
    symbols: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for node in iter_symbols(tree):
        end = int(node.end_lineno or node.lineno)
        physical = end - node.lineno + 1
        logical_size = sum(node.lineno <= number <= end for number in logical)
        qname = qname_map.get((node.lineno, end, node.name), node.name)
        row = _symbol_row(node, qname, logical_size, physical)
        if isinstance(node, ast.ClassDef):
            violations.extend(_class_violations(node, qname, logical_size, test))
        else:
            violations.extend(
                _callable_violations(
                    node, qname, logical_size, row["ast_mccabe"], test, rel,
                    aliases,
                )
            )
        symbols.append(row)
    violations.extend(_module_violations(rel, len(logical), len(lines), test))
    return {
        "path": rel,
        "root": root_name(rel),
        "is_test": test,
        "physical_loc": len(lines),
        "policy_logical_loc": len(logical),
        "imports": import_names(tree),
        "symbols": symbols,
        "violations": violations,
    }


def scan_all(root: Path | None = None) -> dict[str, Any]:
    """Scan all tracked Python files and return the full inventory.

    Fails closed on any parse error (does NOT silently skip).
    """
    if root is None:
        root = REPO_ROOT
    files = tracked_python_files(root)
    modules: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for path in files:
        try:
            modules.append(scan_file(path, root))
        except (SyntaxError, UnicodeDecodeError) as exc:
            errors.append(
                {"path": path.relative_to(root).as_posix(), "error": repr(exc)}
            )
    return {
        "module_count": len(modules),
        "root_counts": dict(Counter(m["root"] for m in modules)),
        "violation_count": sum(len(m["violations"]) for m in modules),
        "errors": errors,
        "modules": modules,
    }
