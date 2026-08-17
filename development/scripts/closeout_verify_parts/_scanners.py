"""Anti-bypass static-scan primitives (AST/lexical detection rules) — extracted
from :mod:`verify_export_track1_closeout`.

Python is AST-parsed (never regex-only); the frontend + production scans are
lexical. These are the pure detection functions ``_lanes._run_scanner_lane``
composes; see that module for the top-level scan orchestration and the
reviewer-ratifiable operational reading.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
from pathlib import Path

import gen_closeout_acceptance_manifest as manifest_mod

# ---- anti-bypass scanner: production-source rules (plan §4.9) ------------------
# Closeout-specific tokens that must NEVER appear in production source (any hit is a
# violation): the seed env, the frozen fixture dir names, and the C8 secret/build
# sentinels (values mirrored from _closeout_live_support.py).
_CLOSEOUT_TOKENS: dict[str, str] = {
    "CLOSEOUT_SEED": "closeout_seed_reference",
    "export_track1_closeout": "closeout_fixture_reference",
    "export-track1-closeout": "closeout_fixture_reference",
    "DISCO-C8-RUNTIME-SECRET-9d1f7a3e": "closeout_secret_sentinel",
    "disco-c8-public-banner-6b2c9e": "closeout_build_sentinel",
    "DISCO-C8-BUILD-SECRET-4f8a1d2c": "closeout_build_sentinel",
}
# A test-only success switch that forces a release verdict (plan §4.9).
_FORCE_VERDICT_RE = re.compile(
    r"FORCE_VERIFIED|FORCE_SELF_HOST|FORCE_READY|DISCO_ACCEPTANCE"
    r"|ACCEPTANCE_SENTINEL|CLOSEOUT_FORCE"
)
# ``PYTEST_CURRENT_TEST`` is a general test-mode signal that PREDATES this campaign in
# the auth/workflow layer (NOT the release-verdict path). Those pre-existing sites are
# ratified here by (repo-relative file, stripped source line) so line-number drift as
# C1–C8 add code does not break them; ANY other production reference — especially in
# the release/export subsystem — is a violation. Keyed on text, not line number.
_PYTEST_CURRENT_TEST_BASELINE: frozenset[tuple[str, str]] = frozenset(
    {
        (
            "current/packages/agent-server/src/disco/agent_server/auth.py",
            'if not os.environ.get("PYTEST_CURRENT_TEST"):',
        ),
        (
            "current/packages/agent-server/src/disco/agent_server/auth.py",
            'os.environ.get("PYTEST_CURRENT_TEST") and server_host in {"testserver", "test", "t"}',
        ),
        (
            "current/packages/agent-server/src/disco/agent_server/routes/workflows.py",
            'if os.environ.get("PYTEST_CURRENT_TEST"):',
        ),
        (
            "current/packages/app-server/src/disco/app_server/auth.py",
            'if not os.environ.get("PYTEST_CURRENT_TEST"):',
        ),
    }
)

# ---- anti-bypass scanner: frozen-test rules (plan §4.4) -----------------------
# Python: mock imports are forbidden entirely; MagicMock/create_autospec are forbidden
# as ANY use; Mock(/patch( are forbidden as calls; mock.patch as an attribute; and
# monkeypatch.setattr is allowed ONLY against cfg_store.load (the config seam).
_PY_MOCK_CALL_NAMES = frozenset({"Mock", "patch"})
_PY_MOCK_ANY_USE_NAMES = frozenset({"MagicMock", "create_autospec"})
_MONKEYPATCH_FIXTURE_NAMES = frozenset({"monkeypatch", "mp"})
# Frontend: module-replacement helpers are forbidden outright. Detection is
# alias-aware (see `_vi_local_names`) and covers both attribute (`vi.mock(`) and
# subscript (`vi['mock']`) forms, so aliasing/bracketing `vi` cannot evade it.
_FORBIDDEN_VI_METHODS: tuple[tuple[str, str], ...] = (
    ("mock", "forbidden_vi_mock"),
    ("spyOn", "forbidden_vi_spyOn"),
    ("stubGlobal", "forbidden_vi_stubGlobal"),
    ("stubEnv", "forbidden_vi_stubEnv"),
)
# jest.* is never aliased in this codebase; flag its literal module-replacement forms.
_FRONTEND_FORBIDDEN = (
    ("jest.mock(", "forbidden_jest_mock"),
    ("jest.spyOn(", "forbidden_jest_spyOn"),
)
_VI_FN_ASSIGN_RE = re.compile(r"(?<![=!<>])=\s*vi\.fn\s*\(")
_DECL_TAIL_RE = re.compile(r"\b(?:const|let|var)\b[^=]*$")
_TS_IMPORT_RE = re.compile(r"""^\s*import\s+(?P<body>.+?)\s+from\s+["']""")
# Whole-source (DOTALL) form so multi-line `import { … } from "…"` blocks are parsed.
_TS_IMPORT_BLOCK_RE = re.compile(r"""import\s+(?P<body>.+?)\s+from\s+["']""", re.DOTALL)
# `const v = vi` / `let v = vi` / `v = vi` — a local aliased to the vi test runner.
_VI_ALIAS_RE = re.compile(r"\b(?:const|let|var)?\s*([A-Za-z_$][\w$]*)\s*=\s*vi\s*[;\n]")

# ---- G18 no-new-suppression: campaign-diff scanner rules (plan §9.4 / criterion 4) ----
# Each pattern matches the ACTUAL directive SYNTAX (a trailing comment directive, a
# decorator, a real call, or a YAML key), NOT a bare prose substring — so an explanatory
# comment ("no continue-on-error / skip path") or a docstring mentioning "xfail" is not
# flagged. ``@ts-expect-error`` is deliberately NOT matched: it is a negative type
# ASSERTION (tsc errors TS2578 if the expected error is absent), the inverse of a
# suppression, so it can never hide a real error — and the frozen g11 guard contract
# relies on that exclusion. ``@ts-nocheck`` IS matched: unlike ``@ts-expect-error`` it
# disables type-checking for the WHOLE file, so it can hide real errors on the
# typecheck:build + g11 tsc lanes. The module-level ``pytestmark`` global is matched only
# when it assigns a ``mark.skip``/``mark.skipif``/``mark.xfail`` (a whole-module skip the
# per-test decorator patterns miss); a legitimate ``pytestmark = pytest.mark.integration``
# / ``mark.export_track1_closeout`` marker is NOT a suppression and is not flagged.
# Out of scope by design: config-file ignore lists (e.g. ``[tool.ruff] ignore`` in
# pyproject.toml) — a distinct, human-reviewed class backstopped by the plan §4.4
# independent diff review, not this token scan.
_G18_SUPPRESSION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("noqa", re.compile(r"#\s*noqa")),
    ("type_ignore", re.compile(r"#\s*type:\s*ignore")),
    ("pyright_ignore", re.compile(r"#\s*pyright:\s*ignore")),
    ("ruff_noqa", re.compile(r"#\s*ruff:\s*noqa")),
    ("eslint_disable", re.compile(r"eslint-disable")),
    ("ts_ignore", re.compile(r"//\s*@ts-ignore")),
    ("ts_nocheck", re.compile(r"//\s*@ts-nocheck")),
    ("continue_on_error", re.compile(r"continue-on-error\s*:")),
    ("pytest_skip", re.compile(r"@pytest\.mark\.skip|@pytest\.mark\.skipif|pytest\.skip\s*\(")),
    ("pytest_xfail", re.compile(r"@pytest\.mark\.xfail|pytest\.xfail\s*\(")),
    (
        "pytestmark_skip",
        re.compile(r"\bpytestmark\b\s*=.*\bmark\.(?:skipif|skip|xfail)\b"),
    ),
    ("js_skip", re.compile(r"\b(?:describe|test|it)\s*\.\s*skip\s*\(")),
    ("js_only", re.compile(r"\b(?:describe|test|it)\s*\.\s*only\s*\(")),
)
# Authoritative source of a hunk's destination file: the ``diff --git a/… b/…`` header.
# Parsing this (not solely ``+++ b/``) keeps the scan anchored even when a runner's git is
# configured with ``diff.noprefix=true`` and emits ``+++ x`` (no ``b/`` prefix) — which
# would otherwise leave ``cur_file`` None and silently no-op the ENTIRE scan (fail-open).
# ``_lanes._run_scanner_lane`` additionally forces ``--src-prefix=a/ --dst-prefix=b/`` at
# the diff invocation so prefixes are always present regardless of the host's git config.
_DIFF_GIT_HEADER_RE: re.Pattern[str] = re.compile(r"^diff --git a/.+ b/(?P<dst>.+)$")
# The diff scan covers real code/config only.
_G18_DIFF_SCAN_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx", ".yml", ".yaml")
# Exempt the two gate-definition scripts (they DEFINE these patterns as detection rules)
# and the release_remediation meta-test dir (its mutation tests embed suppression tokens
# as fixtures). Both are byte-hashed by the manifest and reviewed; excluding them from the
# token scan is what stops the gate from flagging its own definition. Docs/JSON/manifest
# are excluded by suffix.
_G18_DIFF_EXEMPT_PREFIXES = (
    "development/scripts/verify_export_track1_closeout.py",
    "development/scripts/gen_closeout_acceptance_manifest.py",
    "current/packages/agent-server/tests/release_remediation/",
)


def _attr_root_name(node: ast.expr) -> str | None:
    """The leftmost identifier of an attribute chain (``a.b.c`` -> ``a``)."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _setattr_target_ok(call: ast.Call) -> tuple[bool, str]:
    """A ``monkeypatch.setattr`` is allowed ONLY as ``setattr(cfg_store, 'load', ...)``
    (the config seam). Anything else — a code-under-test module or a string target — is
    a violation. Returns (allowed, rendered-target)."""
    if len(call.args) >= 2:
        first, second = call.args[0], call.args[1]
        if (
            isinstance(first, ast.Name)
            and first.id == "cfg_store"
            and isinstance(second, ast.Constant)
            and second.value == "load"
        ):
            return True, "cfg_store, 'load'"
    try:
        rendered = ast.unparse(call.args[0]) if call.args else "<no-target>"
    except (ValueError, AttributeError):
        rendered = "<unrenderable-target>"
    return False, rendered


def _is_monkeypatch_source(call: ast.Call) -> bool:
    """True if a call yields a monkeypatch object: `MonkeyPatch(...)` or
    `<x>.getfixturevalue("monkeypatch")`."""
    func = call.func
    if isinstance(func, ast.Name) and func.id == "MonkeyPatch":
        return True
    if isinstance(func, ast.Attribute):
        if func.attr == "MonkeyPatch":
            return True
        if func.attr == "getfixturevalue" and call.args:
            arg0 = call.args[0]
            return isinstance(arg0, ast.Constant) and arg0.value == "monkeypatch"
    return False


def _monkeypatch_roots(tree: ast.Module) -> set[str]:
    """Names that refer to a monkeypatch fixture — the fixture params plus any local
    aliased to one (`m = monkeypatch`), a constructed `MonkeyPatch()`, or
    `request.getfixturevalue("monkeypatch")`, transitively — so aliasing cannot
    smuggle a `setattr` past the config-seam restriction."""
    roots = set(_MONKEYPATCH_FIXTURE_NAMES)
    changed = True
    while changed:  # fixpoint: catches chained aliases (m = monkeypatch; n = m)
        changed = False
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name) or target.id in roots:
                continue
            value = node.value
            aliased = (isinstance(value, ast.Name) and value.id in roots) or (
                isinstance(value, ast.Call) and _is_monkeypatch_source(value)
            )
            if aliased:
                roots.add(target.id)
                changed = True
    return roots


def _scan_import_node(node: ast.Import, rel: str) -> list[dict[str, object]]:
    """``import mock`` / ``import unittest.mock`` — forbidden entirely."""
    violations: list[dict[str, object]] = []
    for alias in node.names:
        if alias.name == "mock" or alias.name.startswith("unittest.mock"):
            violations.append(
                {
                    "file": rel,
                    "line": node.lineno,
                    "rule": "forbidden_mock_import",
                    "detail": f"import {alias.name}",
                }
            )
    return violations


def _scan_import_from_node(node: ast.ImportFrom, rel: str) -> list[dict[str, object]]:
    """``from unittest.mock import ...`` / ``from unittest import mock`` — forbidden."""
    module = node.module or ""
    if module == "unittest.mock" or module.startswith("unittest.mock."):
        return [
            {
                "file": rel,
                "line": node.lineno,
                "rule": "forbidden_mock_import",
                "detail": f"from {module} import ...",
            }
        ]
    if module == "unittest" and any(a.name == "mock" for a in node.names):
        return [
            {
                "file": rel,
                "line": node.lineno,
                "rule": "forbidden_mock_import",
                "detail": "from unittest import mock",
            }
        ]
    return []


def _scan_call_node(
    node: ast.Call, rel: str, monkeypatch_roots: set[str]
) -> list[dict[str, object]]:
    """``Mock(``/``patch(`` calls, ``mock.patch(...)``, and an out-of-seam
    ``monkeypatch.setattr(...)``."""
    violations: list[dict[str, object]] = []
    func = node.func
    if isinstance(func, ast.Name) and func.id in _PY_MOCK_CALL_NAMES:
        violations.append(
            {
                "file": rel,
                "line": node.lineno,
                "rule": "forbidden_mock_usage",
                "detail": f"{func.id}(...) call",
            }
        )
    elif isinstance(func, ast.Attribute):
        if func.attr == "patch" and _attr_root_name(func.value) == "mock":
            violations.append(
                {
                    "file": rel,
                    "line": node.lineno,
                    "rule": "forbidden_mock_usage",
                    "detail": "mock.patch(...)",
                }
            )
        if func.attr == "setattr" and _attr_root_name(func.value) in monkeypatch_roots:
            ok, target = _setattr_target_ok(node)
            if not ok:
                violations.append(
                    {
                        "file": rel,
                        "line": node.lineno,
                        "rule": "forbidden_monkeypatch_target",
                        "detail": f"monkeypatch.setattr({target}, ...) — only cfg_store.load "
                        "(the config seam) is allowed",
                    }
                )
    return violations


def _scan_python_test(source: str, rel: str) -> list[dict[str, object]]:
    violations: list[dict[str, object]] = []

    def add(line: int, rule: str, detail: str) -> None:
        violations.append({"file": rel, "line": line, "rule": rule, "detail": detail})

    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as exc:
        add(exc.lineno or 0, "python_parse_error", str(exc))
        return violations

    monkeypatch_roots = _monkeypatch_roots(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            violations.extend(_scan_import_node(node, rel))
        elif isinstance(node, ast.ImportFrom):
            violations.extend(_scan_import_from_node(node, rel))
        elif isinstance(node, ast.Name) and node.id in _PY_MOCK_ANY_USE_NAMES:
            add(node.lineno, "forbidden_mock_usage", f"use of {node.id}")
        elif isinstance(node, ast.Call):
            violations.extend(_scan_call_node(node, rel, monkeypatch_roots))
    return violations


def _ts_imported_names(source: str) -> set[str]:
    """Local names introduced by `import … from "…"`, including multi-line blocks and
    `x as y` aliases (the alias is the local binding)."""
    names: set[str] = set()
    for match in _TS_IMPORT_BLOCK_RE.finditer(source):
        body = match.group("body")
        for brace in re.findall(r"\{([^}]*)\}", body, re.DOTALL):
            for part in brace.split(","):
                token = part.strip().split(" as ")[-1].strip()
                if re.fullmatch(r"[A-Za-z_$][\w$]*", token):
                    names.add(token)
        outside = re.sub(r"\{[^}]*\}", "", body, flags=re.DOTALL)
        for part in outside.split(","):
            token = part.strip()
            star = re.match(r"\*\s+as\s+([A-Za-z_$][\w$]*)", token)
            if star:
                names.add(star.group(1))
            elif re.fullmatch(r"[A-Za-z_$][\w$]*", token):
                names.add(token)
    return names


def _vi_local_names(source: str) -> set[str]:
    """Every local name that refers to the `vi` test runner: `vi` itself, `import
    { vi as X }`, and `const X = vi`. Used to make the module-replacement scan
    alias-proof."""
    names: set[str] = {"vi"}
    for match in _TS_IMPORT_BLOCK_RE.finditer(source):
        for brace in re.findall(r"\{([^}]*)\}", match.group("body"), re.DOTALL):
            for part in brace.split(","):
                bits = [b.strip() for b in part.split(" as ")]
                if bits[0] == "vi" and len(bits) == 2 and bits[1]:
                    names.add(bits[1])
    for match in _VI_ALIAS_RE.finditer(source):
        names.add(match.group(1))
    return names


def _scan_frontend_test(source: str, rel: str) -> list[dict[str, object]]:
    violations: list[dict[str, object]] = []
    imported = _ts_imported_names(source)
    vi_names = _vi_local_names(source)
    # `<vi>.method(` and `<vi>['method'](` for every alias of vi and every forbidden
    # module-replacement method — attribute and subscript forms alike.
    vi_alt = "|".join(re.escape(n) for n in sorted(vi_names))
    forbidden_vi_res = [
        (
            re.compile(rf"(?<![\w$])(?:{vi_alt})\s*(?:\.\s*{m}\s*\(|\[\s*['\"]{m}['\"]\s*\])"),
            rule,
            m,
        )
        for m, rule in _FORBIDDEN_VI_METHODS
    ]
    for lineno, line in enumerate(source.splitlines(), start=1):
        for token, rule in _FRONTEND_FORBIDDEN:
            if token in line:
                violations.append({"file": rel, "line": lineno, "rule": rule, "detail": token})
        for pattern, rule, method in forbidden_vi_res:
            if pattern.search(line):
                detail = f"vi.{method} (attr/subscript, alias-aware)"
                violations.append({"file": rel, "line": lineno, "rule": rule, "detail": detail})
        match = _VI_FN_ASSIGN_RE.search(line)
        if match:
            lhs = line[: match.start()].rstrip()
            if not _DECL_TAIL_RE.search(lhs):
                target = lhs.split()[-1] if lhs.split() else lhs
                root_match = re.match(r"([A-Za-z_$][\w$]*)", target)
                root = root_match.group(1) if root_match else None
                if root and root in imported:
                    violations.append(
                        {
                            "file": rel,
                            "line": lineno,
                            "rule": "forbidden_vi_fn_module_override",
                            "detail": f"vi.fn() overwrites imported binding '{root}'",
                        }
                    )
    return violations


def _scan_production_line(rel: str, lineno: int, line: str) -> list[dict[str, object]]:
    hits: list[dict[str, object]] = []
    for token, rule in _CLOSEOUT_TOKENS.items():
        if token in line:
            hits.append({"file": rel, "line": lineno, "rule": rule, "detail": token})
    if "PYTEST_CURRENT_TEST" in line and (rel, line.strip()) not in _PYTEST_CURRENT_TEST_BASELINE:
        hits.append(
            {
                "file": rel,
                "line": lineno,
                "rule": "production_test_switch",
                "detail": "PYTEST_CURRENT_TEST outside the ratified auth/workflow baseline",
            }
        )
    if _FORCE_VERDICT_RE.search(line):
        hits.append(
            {
                "file": rel,
                "line": lineno,
                "rule": "production_force_verdict_switch",
                "detail": "test-only force-verdict env switch",
            }
        )
    return hits


def _is_test_path(parts: tuple[str, ...], name: str) -> bool:
    if any(seg in {"test", "tests", "__tests__", "e2e"} for seg in parts):
        return True
    return ".test." in name or ".spec." in name


def _frozen_python_test_files(repo: Path) -> list[Path]:
    files: list[Path] = []
    for rel in manifest_mod.CLOSEOUT_TEST_DIRS:
        base = repo / rel
        if base.exists():
            files.extend(p for p in sorted(base.rglob("*.py")) if "__pycache__" not in p.parts)
    for rel in (
        manifest_mod.LIVE_TEST_FILE,
        "current/packages/agent-server/tests/integration/_closeout_live_support.py",
    ):
        p = repo / rel
        if p.is_file():
            files.append(p)
    return files


def _frozen_frontend_test_files(repo: Path) -> list[Path]:
    files: list[Path] = []
    for rel in (manifest_mod.FRONTEND_VITEST_DIR, manifest_mod.FRONTEND_E2E_DIR):
        base = repo / rel
        if base.exists():
            files.extend(
                p for p in sorted(base.rglob("*")) if p.is_file() and p.suffix in {".ts", ".tsx"}
            )
    return files


def _production_src_files(repo: Path) -> list[Path]:
    files: list[Path] = []
    for src in sorted((repo / "packages").glob("*/src")):
        for p in sorted(src.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            rel_parts = p.relative_to(src).parts
            if _is_test_path(rel_parts, p.name):
                continue
            files.append(p)
    frontend_src = _frontend_root(repo) / "src"
    if frontend_src.exists():
        for p in sorted(frontend_src.rglob("*")):
            if not p.is_file() or p.suffix not in {".ts", ".tsx", ".js", ".jsx"}:
                continue
            rel_parts = p.relative_to(frontend_src).parts
            if _is_test_path(rel_parts, p.name):
                continue
            files.append(p)
    return files


def _g18_scanned_path(path: str) -> bool:
    """A campaign-diff path the G18 no-new-suppression scan inspects: real code/config,
    excluding the two gate-definition scripts and the release_remediation meta-test dir
    (which legitimately embed suppression tokens). Docs/JSON are excluded by suffix."""
    if not path.endswith(_G18_DIFF_SCAN_SUFFIXES):
        return False
    # The exemption list carries post-restructure paths, but this scans a real
    # git-history window that spans the move — compare on the logical path.
    def _logical(value: str) -> str:
        for bucket in ("current/", "development/"):
            if value.startswith(bucket):
                return value[len(bucket):]
        return value

    logical_path = _logical(path)
    return not any(
        logical_path.startswith(_logical(prefix)) for prefix in _G18_DIFF_EXEMPT_PREFIXES
    )


def _load_suppression_baseline(repo: Path) -> tuple[set[tuple[str, str]], str]:
    """Load the owner-approved suppression baseline (plan §9.4). Returns
    ``(entries, note)`` where entries is a set of ``(repo-relative-file, stripped-line)``
    pairs. An absent/unparseable baseline yields an EMPTY set (fail-closed: any new
    suppression then trips the scan) with an explanatory note."""
    path = repo / manifest_mod.SUPPRESSION_BASELINE_REL
    if not path.is_file():
        return set(), f"suppression baseline absent ({manifest_mod.SUPPRESSION_BASELINE_REL})"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return set(), f"suppression baseline is not valid JSON: {exc}"
    entries: set[tuple[str, str]] = set()
    approved = data.get("approved", []) if isinstance(data, dict) else []
    if isinstance(approved, list):
        for entry in approved:
            if not isinstance(entry, dict):
                continue
            file_val = entry.get("file")
            line_val = entry.get("line")
            if isinstance(file_val, str) and isinstance(line_val, str):
                entries.add((file_val, line_val.strip()))
    return entries, "loaded"


def _check_added_line_for_suppression(
    cur_file: str | None,
    added: str,
    new_lineno: int,
    baseline: set[tuple[str, str]],
) -> dict[str, object] | None:
    """The suppression check for ONE added ('+') diff line, or ``None`` if it is not a
    new, unbaselined suppression. Split out of ``_scan_campaign_diff_suppressions`` so
    that function's own branching stays a thin per-line dispatch."""
    if cur_file is None or not _g18_scanned_path(cur_file):
        return None
    stripped = added.strip()
    if (cur_file, stripped) in baseline:
        return None
    for kind, pattern in _G18_SUPPRESSION_PATTERNS:
        if pattern.search(added):
            return {
                "file": cur_file,
                "line": new_lineno,
                "rule": f"new_suppression_{kind}",
                "detail": stripped[:200],
            }
    return None


def _scan_campaign_diff_suppressions(
    diff_text: str, baseline: set[tuple[str, str]]
) -> list[dict[str, object]]:
    """Scan a unified ``git diff`` for NEWLY-ADDED suppression directives (plan §9.4 /
    criterion 4). For every ADDED line ('+') in a scanned file (see ``_g18_scanned_path``)
    whose text matches a suppression pattern, emit a violation UNLESS its
    ``(file, stripped-line)`` pair is in the owner-approved ``baseline``
    (``_check_added_line_for_suppression``). Pure/importable so the committed mutation
    tests drive it with synthetic hunks. New-file line numbers are tracked from the ``@@``
    hunk headers for locability; matching is by text, never by line number, so drift
    cannot break a baseline entry."""
    violations: list[dict[str, object]] = []
    cur_file: str | None = None
    new_lineno = 0
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            # AUTHORITATIVE per-file anchor: take the destination path from the
            # ``diff --git a/… b/…`` header. This is what makes the scan robust to a
            # no-prefix diff (``+++ x``) — cur_file is already set here, so a ``+++`` line
            # lacking a ``b/`` prefix cannot blank it and vacuously pass the whole gate.
            header = _DIFF_GIT_HEADER_RE.match(raw)
            cur_file = header.group("dst") if header is not None else None
            new_lineno = 0
            continue
        if raw.startswith("+++ b/"):
            cur_file = raw[6:]  # unambiguous refinement when a b/ prefix is present
            continue
        if raw.startswith("+++") or raw.startswith("---") or raw.startswith("diff "):
            continue
        if raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            new_lineno = int(match.group(1)) if match else 0
            continue
        if raw.startswith("+"):
            violation = _check_added_line_for_suppression(cur_file, raw[1:], new_lineno, baseline)
            if violation is not None:
                violations.append(violation)
            new_lineno += 1
        elif raw.startswith("-"):
            continue
        else:
            new_lineno += 1
    return violations


# ---- command inventory gate (G19 / plan §9 criterion 3) -----------------------


def _observed_command_ids(repo: Path) -> set[str]:
    """The set of command IDs this verifier actually dispatches on this host. The Python
    lanes always run; the current/frontend/browser/G11 commands run only when node/npm/npx are
    present. On a real acceptance host (all tools present) this equals
    ``manifest_mod.REQUIRED_COMMAND_IDS``; a host missing a toolchain OMITS commands and
    the inventory gate then fails (an acceptance run cannot omit a required command)."""
    ids = {
        "python_nonlive",
        "python_closeout",
        "python_closeout_collect",
        "live_docker",
        "live_capture",
    }
    if shutil.which("npx") is not None and shutil.which("npm") is not None:
        ids |= {
            "frontend_vitest",
            "frontend_typecheck",
            "frontend_build",
            "g11_typecheck",
            "browser_e2e",
        }
    return ids


def _check_command_inventory(
    observed_ids: set[str], required_ids: frozenset[str]
) -> tuple[bool, dict[str, object]]:
    """Gate the observed command inventory against the FROZEN required inventory: it must
    EXACTLY equal it — an omitted command OR an additional/substitute command is rejected
    (plan §9 criterion 3). Pure/importable so the mutation tests drive it directly."""
    missing = sorted(required_ids - observed_ids)
    extra = sorted(observed_ids - required_ids)
    ok = not missing and not extra
    return ok, {
        "ok": ok,
        "missing": missing,
        "extra": extra,
        "required_count": len(required_ids),
        "observed_count": len(observed_ids),
    }


def _frontend_root(repo):
    """The frontend tree, wherever the three-bucket layout puts it."""
    bucketed = repo / "current" / "frontend"
    return bucketed if bucketed.is_dir() else repo / "frontend"
