"""Architecture budget check — the core scan + debt enforcement.

This module is the bounded helper that the stable wrapper
``scripts/check_arch_budget.py`` calls. It scans all tracked executable roots,
enforces all S2 Python/TypeScript logical budgets, McCabe, typed classifications,
collaborator and aggregate caps, and checks the shrink-only debt ledger.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from . import debt as debt_mod
from . import python_scan, source_governance, typescript_scan
from .policy import (
    REPO_ROOT,
    adjudicated_width_non_violations,
    is_adjudicated_width_non_violation,
    is_protocol_non_violation,
    load_ownership,
    load_policy,
)


def collect_current_violations(
    py_result: dict[str, Any],
    ts_result: dict[str, Any] | None,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """Collect current violations from Python and TypeScript scans.

    Filters out typed Protocol non-violations.
    """
    policy = load_policy(root)
    violations: list[dict[str, Any]] = []

    for module in py_result.get("modules", []):
        path = module["path"]
        for v in module.get("violations", []):
            symbol = v["symbol"]
            rule = v["rule"]
            if is_protocol_non_violation(policy, path, symbol, rule):
                continue
            if is_adjudicated_width_non_violation(policy, path, symbol, rule):
                continue
            violations.append(
                {
                    "path": path,
                    "symbol": symbol,
                    "qualified_symbol": v.get("qualified_symbol", symbol),
                    "rule": rule,
                    "value": v["value"],
                    "limit": v["limit"],
                }
            )

    if ts_result is not None:
        for module in ts_result.get("modules", []):
            path = module["path"]
            for v in module.get("violations", []):
                violations.append(
                    {
                        "path": path,
                        "symbol": v["symbol"],
                        "qualified_symbol": v.get("qualified_symbol", v["symbol"]),
                        "rule": v["rule"],
                        "value": v["value"],
                        "limit": v["limit"],
                    }
                )

    return violations


def check_parse_errors(
    py_result: dict[str, Any],
    ts_result: dict[str, Any] | None,
) -> list[str]:
    """Check for parse errors (fail-closed — no silent skipping)."""
    problems: list[str] = []
    for err in py_result.get("errors", []):
        problems.append(f"Python parse error in {err['path']}: {err['error']}")
    if ts_result is not None:
        for module in ts_result.get("modules", []):
            for diag in module.get("parse_diagnostics", []):
                problems.append(
                    f"TypeScript parse diagnostic in {module['path']}: {diag.get('message', '')}"
                )
        for problem in ts_result.get("frontend_context_policy_problems", []):
            problems.append(f"TypeScript context policy drift: {problem}")
    return problems


# ---------------------------------------------------------------------------
# Executable registry checks — AST-plus-registry enforcement
# ---------------------------------------------------------------------------


def _load_py_module(path: Path, root: Path) -> ast.Module:
    """Load and parse a Python file, raising on error."""
    text = path.read_text(encoding="utf-8")
    return ast.parse(text, filename=str(path))


def _find_class(tree: ast.Module, symbol: str) -> ast.ClassDef | None:
    """Find a top-level class by name in an AST."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and node.name == symbol:
            return node
    return None


def _find_function(
    tree: ast.Module, symbol: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Find a top-level function by name in an AST."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol:
            return node
    return None


def _resolve_path(path_str: str, root: Path) -> Path:
    """Resolve a relative path string against the repo root."""
    return root / path_str


def check_protocol_non_violations(root: Path) -> list[str]:
    """Check that all registered Protocol non-violations are AST-proven.

    A Protocol non-violation is only valid when AST proves:
      1. the class inherits from Protocol (or has @runtime_checkable), AND
      2. the class has zero mutable implementation state (no self.X = ...).

    The registry entry must match the AST exactly. A stale or wrong
    registry entry fails closed.
    """
    problems: list[str] = []
    policy = load_policy(root)

    # Check policy protocol_non_violations against AST
    for entry in policy["typed_classifications"]["protocol_non_violations"]:
        path = entry["path"]
        symbol = entry["symbol"]
        full = _resolve_path(path, root)
        if not full.is_file():
            problems.append(
                f"protocol non-violation {entry['disposition_id']}: "
                f"file absent: {path}"
            )
            continue
        try:
            tree = _load_py_module(full, root)
        except (SyntaxError, UnicodeDecodeError) as exc:
            problems.append(
                f"protocol non-violation {entry['disposition_id']}: "
                f"file unparseable: {exc!r}"
            )
            continue
        cls = _find_class(tree, symbol)
        if cls is None:
            problems.append(
                f"protocol non-violation {entry['disposition_id']}: "
                f"class {symbol} not found in {path}"
            )
            continue
        if not python_scan.is_protocol_class(cls):
            problems.append(
                f"protocol non-violation {entry['disposition_id']}: "
                f"class {symbol} is not a Protocol"
            )
            continue
        if python_scan.has_mutable_instance_state(cls):
            problems.append(
                f"protocol non-violation {entry['disposition_id']}: "
                f"class {symbol} has mutable implementation state"
            )
            continue

    return problems


_ADJUDICATED_FIELDS = frozenset(
    {
        "path",
        "symbol",
        "rule",
        "disposition_id",
        "width_kind",
        "observed_at_adjudication",
        "criterion",
        "decision_record",
        "accepting_receipt",
    }
)
_ADJUDICATED_WIDTH_KINDS = frozenset({"protocol", "intrinsic"})


def _adjudicated_shape_problem(entry: dict[str, Any], label: str) -> str | None:
    """Static shape validation for one registry entry: fields, emptiness, kind."""
    if set(entry) != _ADJUDICATED_FIELDS:
        return (
            f"adjudicated width non-violation {label}: field set must be "
            f"exactly {sorted(_ADJUDICATED_FIELDS)}"
        )
    if any(value == "" or value is None for value in entry.values()):
        return f"adjudicated width non-violation {label}: empty field"
    if entry["width_kind"] not in _ADJUDICATED_WIDTH_KINDS:
        return (
            f"adjudicated width non-violation {label}: width_kind "
            f"{entry['width_kind']!r} not in {sorted(_ADJUDICATED_WIDTH_KINDS)}"
        )
    return None


def _adjudicated_ast_problem(
    entry: dict[str, Any], label: str, root: Path
) -> str | None:
    """Prove the registered class still exists at the registered path."""
    full = _resolve_path(entry["path"], root)
    if not full.is_file():
        return f"adjudicated width non-violation {label}: file absent: {entry['path']}"
    try:
        tree = _load_py_module(full, root)
    except (SyntaxError, UnicodeDecodeError) as exc:
        return f"adjudicated width non-violation {label}: file unparseable: {exc!r}"
    if _find_class(tree, entry["symbol"]) is None:
        return (
            f"adjudicated width non-violation {label}: class "
            f"{entry['symbol']} not found in {entry['path']}"
        )
    return None


def _adjudicated_live_problems(
    entry: dict[str, Any],
    label: str,
    observed: dict[tuple[str, str, str], int],
    active_debt_ids: set[str],
    known_ids: set[str],
) -> list[str]:
    """Prove the entry still matches the live scan and has left the ledger."""
    key = (entry["path"], entry["symbol"], entry["rule"])
    if key not in observed:
        return [
            f"adjudicated width non-violation {label}: stale entry — the "
            f"scanner no longer reports {entry['rule']} for {entry['symbol']}; "
            "resolve the row in source instead of adjudicating it"
        ]
    problems: list[str] = []
    if observed[key] != entry["observed_at_adjudication"]:
        problems.append(
            f"adjudicated width non-violation {label}: value drift — "
            f"adjudicated at {entry['observed_at_adjudication']}, "
            f"currently {observed[key]}; re-adjudicate rather than absorb"
        )
    if entry["disposition_id"] not in known_ids:
        problems.append(
            f"adjudicated width non-violation {label}: unknown disposition id"
        )
    if entry["disposition_id"] in active_debt_ids:
        problems.append(
            f"adjudicated width non-violation {label}: still an ACTIVE debt "
            "row — register it as adjudicated before filtering it"
        )
    return problems


def check_adjudicated_width_non_violations(
    root: Path, py_result: dict[str, Any]
) -> list[str]:
    """Verify every adjudicated width row is still EXACTLY as adjudicated.

    An adjudicated row leaves the active ledger while its symbol keeps
    breaching the cap — that is the whole point of adjudicating rather than
    fixing it. ``collect_current_violations`` therefore filters these triples
    out, or ``check_shrink_only`` would report each as "new violation without
    debt row". Filtering is only safe if every entry is re-proven against the
    live scan on each run, so all of the following fail closed:

      1. the exact field set is present and no value is empty;
      2. ``width_kind`` is one of the two sanctioned evidence bars;
      3. the class still exists at the registered path;
      4. the scanner still reports the registered (path, symbol, rule) — a
         stale entry means the class was fixed and must be resolved in source,
         never carried as an adjudication;
      5. the live value equals ``observed_at_adjudication`` exactly. This is
         the ratchet: an adjudicated class may not grow, and a shrink must be
         re-adjudicated rather than silently absorbed;
      6. the disposition id is real and has genuinely left the active ledger.

    There are no wildcards and no blanket waivers: an entry authorizes exactly
    one triple at exactly one measured value.
    """
    problems: list[str] = []
    policy = load_policy(root)
    entries = adjudicated_width_non_violations(policy)
    if not entries:
        return problems

    observed: dict[tuple[str, str, str], int] = {}
    for module in py_result.get("modules", []):
        for violation in module.get("violations", []):
            key = (module["path"], violation["symbol"], violation["rule"])
            observed[key] = violation["value"]

    active_debt_ids = {row["id"] for row in debt_mod.load_debt(root)}
    known_ids = set(debt_mod.load_disposition_ids(root))

    for entry in entries:
        label = str(entry.get("disposition_id", "<no disposition_id>"))
        blocking = _adjudicated_shape_problem(entry, label) or _adjudicated_ast_problem(
            entry, label, root
        )
        if blocking is not None:
            problems.append(blocking)
            continue
        problems.extend(
            _adjudicated_live_problems(
                entry, label, observed, active_debt_ids, known_ids
            )
        )

    return problems


def check_constructor_collaborator_caps(root: Path) -> list[str]:
    """Delegate source-discovered collaborator enforcement."""
    return source_governance.check_constructor_collaborator_caps(root)


def check_dependency_aggregate_caps(root: Path) -> list[str]:
    """Delegate source-discovered dependency aggregate enforcement."""
    return source_governance.check_dependency_aggregate_caps(root)


def _check_composition_root_size(
    path: str, symbol: str, full: Path, root: Path, limit: int
) -> list[str]:
    """Check a single composition root's logical line count."""
    problems: list[str] = []
    try:
        result = python_scan.scan_file(full, root)
    except (SyntaxError, UnicodeDecodeError) as exc:
        problems.append(
            f"composition root {symbol}: file unparseable: {exc!r}"
        )
        return problems
    # Find the symbol in the scan results
    for sym_row in result.get("symbols", []):
        if sym_row["name"] == symbol:
            logical_size = sym_row["policy_logical_loc"]
            if logical_size > limit:
                problems.append(
                    f"composition root {symbol}: logical lines {logical_size} "
                    f"exceeds limit {limit}"
                )
            return problems
    # Symbol not found in scan results — try direct AST load
    try:
        tree = _load_py_module(full, root)
    except (SyntaxError, UnicodeDecodeError):
        return problems
    func = _find_function(tree, symbol)
    if func is None:
        problems.append(
            f"composition root {symbol}: function not found in {path}"
        )
        return problems
    # Compute logical lines for the function
    text = full.read_text(encoding="utf-8")
    logical = python_scan.logical_line_set(text)
    end = int(func.end_lineno or func.lineno)
    logical_size = sum(func.lineno <= number <= end for number in logical)
    if logical_size > limit:
        problems.append(
            f"composition root {symbol}: logical lines {logical_size} "
            f"exceeds limit {limit}"
        )
    return problems


def _check_composition_root_effects(
    symbol: str, full: Path, root: Path
) -> list[str]:
    """Check a single composition root for direct effect calls."""
    problems: list[str] = []
    try:
        tree = _load_py_module(full, root)
    except (SyntaxError, UnicodeDecodeError):
        return problems
    func = _find_function(tree, symbol)
    if func is None:
        return problems
    aliases = python_scan.effect_import_aliases(tree, func)
    if python_scan.has_direct_effect_calls(func, aliases):
        problems.append(
            f"composition root {symbol}: direct effect calls "
            f"(persistence/process/filesystem/network) — must be wiring-only"
        )
    return problems


def check_composition_roots(root: Path) -> list[str]:
    """Check composition root limits and wiring-only effects against the AST.

    Enforces:
      - composition root <=300 logical lines
      - wiring-only effects (no direct persistence/process/filesystem/network calls)

    The composition-root check is additive: it does NOT remove the existing
    ordinary callable debt (e.g. create_app's python_callable_logical_gt_100).
    The composition-root limit is a SEPARATE, additional check.
    """
    problems: list[str] = []
    policy = load_policy(root)
    ownership = load_ownership(root)
    limit = policy["logical_limits"]["python_composition_root"]

    for comp_root in ownership.get("composition_roots", []):
        path = comp_root["path"]
        symbol = comp_root["symbol"]
        full = _resolve_path(path, root)
        if not full.is_file():
            problems.append(
                f"composition root {symbol}: file absent: {path}"
            )
            continue
        problems.extend(_check_composition_root_size(path, symbol, full, root, limit))
        problems.extend(_check_composition_root_effects(symbol, full, root))

    return problems


_ROUTE_DECORATOR_METHODS = frozenset({
    "api_route", "delete", "get", "head", "options", "patch", "post", "put",
    "route", "trace", "websocket",
})


def _is_decorated_route_handler(sym_row: dict[str, Any]) -> bool:
    """Check whether a scanned symbol row is a decorated HTTP/WS route handler.

    A route handler is a function decorated with ``@router.<method>(...)`` (or
    ``@app.<method>(...)``). A registered effect-owner symbol that is a helper
    delegated to by the actual decorated handler is NOT itself a route handler
    and the <=80 route limit does not apply to it. The effect rule and source
    agreement still apply to all registered symbols.
    """
    for dec in sym_row.get("decorators", []):
        identity = dec.split("(", 1)[0].rsplit(".", 1)[-1]
        if identity in _ROUTE_DECORATOR_METHODS:
            return True
    return False


def _direct_effect_symbols(tree: ast.Module) -> set[str]:
    """Return top-level functions that directly own a prohibited effect."""
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and python_scan.has_direct_effect_calls(
            node, python_scan.effect_import_aliases(tree, node)
        )
    }


def _check_effect_symbol_set(
    path: str, symbols: list[str], tree: ast.Module
) -> list[str]:
    """Require exact agreement between registered and source effect owners."""
    problems: list[str] = []
    if len(symbols) != len(set(symbols)):
        problems.append(f"route effect owner: duplicate registered symbols in {path}")
    registered = set(symbols)
    actual = _direct_effect_symbols(tree)
    missing = sorted(registered - actual)
    unregistered = sorted(actual - registered)
    if missing:
        problems.append(
            f"route effect owner: registered symbols no longer own direct "
            f"effects in {path}: {missing}"
        )
    if unregistered:
        problems.append(
            f"route effect owner: unregistered direct effects in {path}: "
            f"{unregistered}"
        )
    return problems


def _check_registered_effect_symbols(
    path: str,
    symbols: list[str],
    tree: ast.Module,
    result: dict[str, Any],
) -> list[str]:
    """Require every registered effect symbol to exist and remain direct."""
    problems: list[str] = []
    scanned_names = {row["name"] for row in result.get("symbols", [])}
    for symbol in symbols:
        if symbol not in scanned_names:
            problems.append(
                f"route effect owner: stale symbol {symbol} not found in {path} "
                f"(source-observation shrink-only agreement)"
            )
            continue
        function = _find_function(tree, symbol)
        aliases = (
            python_scan.effect_import_aliases(tree, function)
            if function is not None
            else {}
        )
        if function is None or not python_scan.has_direct_effect_calls(
            function, aliases
        ):
            problems.append(
                f"route effect owner: {symbol} has no direct effect in {path}"
            )
    return problems


def _check_decorated_route_sizes(
    result: dict[str, Any], route_limit: int
) -> list[str]:
    """Apply the route cap only to decorated HTTP/WS handlers."""
    problems: list[str] = []
    for row in result.get("symbols", []):
        if not _is_decorated_route_handler(row):
            continue
        logical_size = row["policy_logical_loc"]
        if logical_size > route_limit:
            problems.append(
                f"route handler {row['name']}: logical lines {logical_size} "
                f"exceeds route limit {route_limit}"
            )
    return problems


def check_route_effect_owners(root: Path) -> list[str]:
    """Check route effect owners against the AST.

    Enforces:
      - decorated route handlers <=80 logical lines
      - route handlers must not own persistence/process/filesystem/network/lifecycle effects

    Corrects stale route symbols: the registered symbols must match the actual
    AST symbols exactly (source-observation shrink-only agreement).

    The <=80 route limit applies only to actual decorated HTTP/WS route
    handlers (functions with a ``@router.<method>(...)`` decorator). A
    registered effect-owner symbol that is a helper (no route decorator) is
    still checked for source agreement and effect ownership, but the route
    line limit does not apply to it — it is governed by the ordinary callable
    limit (<=100) and the effect rule, not the route-handler limit.
    """
    problems: list[str] = []
    policy = load_policy(root)
    ownership = load_ownership(root)
    route_limit = policy["logical_limits"]["python_route_handler"]
    registered: set[tuple[str, str]] = set()

    for eff in ownership.get("effect_owners", []):
        if eff["kind"] != "route_adapter_effect_violation":
            continue
        path = eff["path"]
        symbols = eff["symbols"]
        for symbol in symbols:
            key = (path, symbol)
            if key in registered:
                problems.append(
                    f"route effect owner: duplicate registered tuple {key}"
                )
            registered.add(key)
        full = _resolve_path(path, root)
        if not full.is_file():
            problems.append(
                f"route effect owner: file absent: {path}"
            )
            continue
        try:
            result = python_scan.scan_file(full, root)
            tree = _load_py_module(full, root)
        except (SyntaxError, UnicodeDecodeError) as exc:
            problems.append(
                f"route effect owner: file unparseable: {exc!r}"
            )
            continue
        problems.extend(_check_effect_symbol_set(path, symbols, tree))
        problems.extend(_check_registered_effect_symbols(path, symbols, tree, result))
        problems.extend(_check_decorated_route_sizes(result, route_limit))

    problems.extend(
        source_governance.check_unregistered_route_effects(root, registered)
    )

    return problems


def _check_symbol_exists_as_function(
    path: str, symbol: str, full: Path, root: Path, kind: str
) -> list[str]:
    """Check that a function symbol exists in a file."""
    problems: list[str] = []
    if not full.is_file():
        problems.append(f"{kind} {symbol}: file absent: {path}")
        return problems
    try:
        tree = _load_py_module(full, root)
    except (SyntaxError, UnicodeDecodeError):
        problems.append(f"{kind} {symbol}: file unparseable")
        return problems
    func = _find_function(tree, symbol)
    if func is None:
        problems.append(f"{kind} {symbol}: function not found in {path}")
    return problems


def _check_symbol_exists_as_class(
    path: str, symbol: str, full: Path, root: Path, kind: str
) -> list[str]:
    """Check that a class symbol exists in a file."""
    problems: list[str] = []
    if not full.is_file():
        problems.append(f"{kind} {symbol}: file absent: {path}")
        return problems
    try:
        tree = _load_py_module(full, root)
    except (SyntaxError, UnicodeDecodeError):
        problems.append(f"{kind} {symbol}: file unparseable")
        return problems
    cls = _find_class(tree, symbol)
    if cls is None:
        problems.append(f"{kind} {symbol}: class not found in {path}")
    return problems


def _check_symbol_exists_as_assignment(
    path: str, symbol: str, full: Path, root: Path, kind: str
) -> list[str]:
    """Check that a symbol exists as a module-level assignment in a file."""
    problems: list[str] = []
    if not full.is_file():
        problems.append(f"{kind}: file absent: {path}")
        return problems
    try:
        tree = _load_py_module(full, root)
    except (SyntaxError, UnicodeDecodeError):
        problems.append(f"{kind}: file unparseable")
        return problems
    found = False
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == symbol:
                    found = True
                    break
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == symbol:
                found = True
    if not found:
        problems.append(f"{kind}: symbol {symbol} not found in {path}")
    return problems


def check_ownership_agreement(root: Path) -> list[str]:
    """Check that the ownership registry agrees with the source observations.

    Enforces source-observation shrink-only agreement: the registered
    facade/root/aggregate/route symbols must match the actual AST symbols.
    """
    problems: list[str] = []
    ownership = load_ownership(root)

    # Check composition roots
    for comp_root in ownership.get("composition_roots", []):
        full = _resolve_path(comp_root["path"], root)
        problems.extend(
            _check_symbol_exists_as_function(
                comp_root["path"], comp_root["symbol"], full, root,
                "composition root"
            )
        )

    # Check composition facades
    for facade in ownership.get("composition_facades", []):
        full = _resolve_path(facade["path"], root)
        problems.extend(
            _check_symbol_exists_as_class(
                facade["path"], facade["symbol"], full, root,
                "composition facade"
            )
        )

    # Check declarative authorization positives
    for pos in ownership.get("declarative_authorization_positives", []):
        full = _resolve_path(pos["path"], root)
        for sym in pos["symbols"]:
            problems.extend(
                _check_symbol_exists_as_assignment(
                    pos["path"], sym, full, root,
                    "declarative authorization positive"
                )
            )

    return problems


def check_observations_agreement(
    root: Path,
    ts_result: dict[str, Any] | None = None,
) -> list[str]:
    """Delegate exact authority and source-observation agreement."""
    return source_governance.check_observations_agreement(root, ts_result)


def run_budget_check(
    scan_typescript: bool = True,
    root: Any = None,
) -> dict[str, Any]:
    """Run the full architecture budget check.

    Args:
        scan_typescript: whether to scan TypeScript (requires Node + pinned compiler)
        root: repository root path

    Returns:
        {"ok": bool, "problems": list[str], "stats": dict}
    """
    if root is None:
        root = REPO_ROOT
    root_path = Path(root) if not isinstance(root, Path) else root

    problems: list[str] = []

    # Scan Python
    py_result = python_scan.scan_all(root_path)
    problems.extend(check_parse_errors(py_result, None))

    # Scan TypeScript
    ts_result: dict[str, Any] | None = None
    if scan_typescript:
        try:
            ts_result = typescript_scan.scan_typescript(root_path)
            problems.extend(check_parse_errors(py_result, ts_result))
        except (FileNotFoundError, RuntimeError) as exc:
            problems.append(f"TypeScript scanner error: {exc}")

    # Collect current violations
    current_violations = collect_current_violations(py_result, ts_result, root_path)

    # Run debt check against current violations
    debt_result = debt_mod.run_debt_check(current_violations, root_path)
    problems.extend(debt_result["problems"])

    # Check generated/declarative proof
    from . import generated as generated_mod

    generated_result = generated_mod.check_generated(root_path)
    problems.extend(generated_result["problems"])

    # Executable registry checks (AST-plus-registry)
    problems.extend(check_protocol_non_violations(root_path))
    problems.extend(check_adjudicated_width_non_violations(root_path, py_result))
    problems.extend(check_constructor_collaborator_caps(root_path))
    problems.extend(check_dependency_aggregate_caps(root_path))
    problems.extend(check_composition_roots(root_path))
    problems.extend(check_route_effect_owners(root_path))
    problems.extend(check_ownership_agreement(root_path))
    problems.extend(check_observations_agreement(root_path, ts_result))

    return {
        "ok": len(problems) == 0,
        "problems": problems,
        "stats": {
            "python_modules": py_result.get("module_count", 0),
            "python_violations": py_result.get("violation_count", 0),
            "typescript_modules": ts_result.get("module_count", 0) if ts_result else 0,
            "typescript_violations": ts_result.get("violation_count", 0) if ts_result else 0,
            "current_violations": len(current_violations),
            "debt_count": debt_result["debt_count"],
            "generated_entries": generated_result["entry_count"],
        },
    }
