"""THE ATTESTATION-BINDING INVARIANT — enforced (F58, 2026-08-07m; here 07r).

> Any test or instrument that attests a production string, count, or path must
> **derive it from the production symbol**, never restate it. A restatement is a
> copy, and a copy attests itself. Where a value genuinely cannot be imported,
> the attestation must carry a control that fails when the production value
> changes.

Designed at the pattern's fourth occurrence: F51 scored a surface by what was
syntactically *nearby*; F52 found instruments whose *reach* was never attested;
F54 gap 2 found awareness resting on a parameter no caller was checked to bind;
F58 is the same shape in a test — `test_f53_constraint4_confirmed_repeats` proved
F53's HALT path against a hand-copied duplicate of production prose, so a reword
of the product would have left it green while asserting on a string nothing
emits.

# The rule, and why it is keyed on DERIVABILITY rather than on length

The tempting rule — "no long production sentence may appear as a test literal" —
does not survive measurement. The constraint-4 family legitimately probes bodies
with short fragments (`"did not clear it"`, `"this same blocked context"`), and
the longest surviving fragment (44 chars) is longer than the shortest restatement
F58 named (36), so no length threshold separates the classes. Any cut would be
gerrymandered.

The invariant's own wording gives the honest rule instead: *derive it from the
production symbol*. So the question is not "is this text long?" but **"does
production have a NAME for this text?"** If it does, the test must use the name.
If it does not — a fragment of an inline expression, a call argument — then there
is no symbol to derive from, the text is a probe rather than a copy, and this
gate says nothing about it.

Concretely, a violation is: a non-docstring string literal in the constraint-4
test family whose value CONTAINS the whole value of a named production prose
symbol, where a named production prose symbol is a module-level `NAME = "..."`
or a bare `return "..."` in the loop package.

# What this gate deliberately does NOT cover

An assertion that a RETIRED string never comes back cannot be bound this way:
production deleted the string, so no live symbol owns it. That class is guarded
at its own site — `test_f51_constraint4_multifire._retired_delegated_fork_clause`
derives the retired clause from the docstring in which production still records
it, and raises if that record moves. This is the invariant's explicit escape
clause ("where a value genuinely cannot be imported…") and it is exercised there,
not here.

# Reach (F52) and population (2026-08-07o)

Both suites below print the roots they scanned and the size of every population,
and fail closed on an empty one: a zero found over a zero-sized population is not
evidence of absence, it is evidence the instrument did not reach.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

# The constraint-4 test family: the four files F58's scan covered, plus the
# route (B) driven-composition file added at 2026-08-07r. A new member of this
# family MUST be added here — the gate cannot police a file it does not know.
CONSTRAINT4_TEST_FAMILY = (
    "packages/core/tests/test_f51_constraint4_multifire.py",
    "packages/core/tests/test_f53_constraint4_confirmed_repeats.py",
    "packages/core/tests/test_f53_seo_primitive.py",
    "packages/core/tests/test_f55_f56_weakening_repeats_and_ordinals.py",
    "packages/core/tests/test_route_b_driven_composition.py",
)

PRODUCTION_ROOT = "packages/core/src/disco/core/loop"

# Below this, a shared string is a probe rather than an authored unit: no
# agent-facing sentence this campaign has repaired is shorter, and every
# fragment the family legitimately asserts on is. It is a floor on what counts
# as prose at all, NOT the discriminator — derivability is the discriminator.
_PROSE_MIN_CHARS = 24


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _is_prose(text: str) -> bool:
    return len(text) >= _PROSE_MIN_CHARS and " " in text


def _joined_literal(node: ast.AST) -> str | None:
    """Value of a literal string expression, including implicit concatenation.

    Returns None for anything with an interpolation or a non-literal part: a
    symbol whose value is only partly authored here is not a name the test could
    have used.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _joined_literal(node.left)
        right = _joined_literal(node.right)
        return None if left is None or right is None else left + right
    return None


def _docstring_ids(tree: ast.AST) -> set[int]:
    """id() of every docstring Constant, so narration can quote the defect.

    The same exemption `check-evidence-locality.sh` makes for comments, for the
    same reason: errata and design narration must be able to name the thing they
    are about. The F58 repair's own docstrings quote the retired restatements
    verbatim, and must not be flagged for doing so.
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            out.add(id(body[0].value))
    return out


def named_production_prose(root: Path) -> dict[str, str]:
    """Every NAMED production prose symbol under `root`, as `label -> value`.

    Named means addressable from a test: a module-level assignment, or a bare
    `return "..."` which the enclosing function names. Anything else — a call
    argument, a fragment of an f-string — has no name, so a test cannot derive
    it and this gate makes no claim about it.
    """
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        module = path.stem
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            value = _joined_literal(node.value)
            if value is None or not _is_prose(_normalise(value)):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[f"{module}.{target.id}"] = _normalise(value)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Return) or sub.value is None:
                    continue
                value = _joined_literal(sub.value)
                if value is None or not _is_prose(_normalise(value)):
                    continue
                found[f"{module}.{node.name}:{sub.lineno}"] = _normalise(value)
    return found


def find_restatements(paths: list[Path], symbols: dict[str, str]) -> tuple[int, list[str]]:
    """`(literals_scanned, violations)` for `paths` against `symbols`.

    THE ONE detection routine. The gate and its negative control both call this,
    deliberately: a control that re-implemented the check would attest a COPY of
    the logic rather than the logic, which is the F58 defect one level up.
    """
    scanned = 0
    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text())
        skip = _docstring_ids(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in skip:
                continue
            scanned += 1
            text = _normalise(node.value)
            if not _is_prose(text):
                continue
            # Report EVERY symbol the literal copies, not the first one found: a
            # hand-copied composition (F58's `_HALT_GUIDANCE` concatenated two
            # production symbols) needs all of its owners named to be repairable.
            matched = sorted(label for label, value in symbols.items() if value in text)
            if matched:
                violations.append(
                    f"{path.name}:{node.lineno} restates production symbol(s) "
                    f"{', '.join('`' + m + '`' for m in matched)}\n"
                    f"      test literal: {text[:90]!r}"
                )
    return scanned, violations


def test_family_files_all_exist():
    """Fail closed on a family member that has been renamed or deleted."""
    missing = [rel for rel in CONSTRAINT4_TEST_FAMILY if not (_REPO / rel).exists()]
    assert not missing, (
        f"the constraint-4 test family names files that do not exist: {missing}. "
        "A gate that silently skips its own population is the empty-class defect "
        "this campaign numbered three times (F52, and the 2026-08-07o rule)."
    )


def test_production_prose_corpus_is_non_empty():
    """NON-EMPTY POPULATION RULE — reach, printed and asserted."""
    root = _REPO / PRODUCTION_ROOT
    assert root.is_dir(), f"production root missing: {root}"
    files = list(root.rglob("*.py"))
    symbols = named_production_prose(root)
    print(f"reach: production root {PRODUCTION_ROOT}")
    print(f"population: production files = {len(files)}")
    print(f"population: named production prose symbols = {len(symbols)}")
    assert files, "no production files scanned — the gate did not reach"
    assert symbols, (
        "no named production prose symbols found. A zero found over a zero-sized "
        "population is not evidence of absence; it is evidence the instrument did "
        "not reach."
    )


def test_no_constraint4_test_restates_a_named_production_sentence():
    """THE GATE. A test may not carry a copy of a named production sentence.

    The failure this prevents, concretely: `_HALT_GUIDANCE` was a hand-copied
    duplicate of the guidance `governed_contract_refusal`'s HALT branch composes.
    Reword the product and that test keeps passing while asserting on a string
    the product no longer emits — it stops testing without ever going red.
    """
    symbols = named_production_prose(_REPO / PRODUCTION_ROOT)
    assert symbols, "empty production corpus — fail closed rather than pass vacuously"

    scanned, violations = find_restatements(
        [_REPO / rel for rel in CONSTRAINT4_TEST_FAMILY], symbols
    )

    print(f"population: constraint-4 test literals scanned = {scanned}")
    assert scanned, "no test literals scanned — the gate did not reach"

    assert not violations, (
        "ATTESTATION-BINDING INVARIANT violated — a test carries a COPY of a "
        "named production sentence instead of deriving it.\n\n"
        + "\n".join(violations)
        + "\n\nA restatement is a copy, and a copy attests itself: reword the "
        "product and this test keeps passing while asserting on a string nothing "
        "emits. Import the production symbol named above and use it. If the value "
        "genuinely cannot be imported, the attestation must instead carry a "
        "control that fails when the production value changes (see "
        "`test_f51_constraint4_multifire._retired_delegated_fork_clause`)."
    )


def test_the_gate_refuses_a_recreated_F58_shape(tmp_path: pytest.TempPathFactory):
    """NEGATIVE CONTROL — the gate must REFUSE the exact defect F58 recorded.

    A gate that has never been shown refusing anything is a claim nobody checked
    (F52). This recreates F58's shape — a module constant that hand-copies the
    guidance the HALT branch composes — and asserts the detection fires on it.
    """
    symbols = named_production_prose(_REPO / PRODUCTION_ROOT)
    halt_prefix = symbols.get("host_disposition._TARGET_REPEAT_HALT_PREFIX")
    assert halt_prefix, (
        "the HALT prefix symbol is gone; either it was renamed (update this "
        "control) or the F58 repair was reverted"
    )

    # The literal F58 found in the tree at 2026-08-07m, reconstructed from the
    # production symbols rather than retyped, so this control cannot itself rot.
    recreated = halt_prefix + "there is no handoff for the current target at all"
    offending = Path(tmp_path) / "test_recreated_f58_shape.py"  # type: ignore[arg-type]
    offending.write_text(
        '"""A docstring quoting the defect must NOT trip the gate."""\n'
        f"# {recreated}\n"
        f"_HALT_GUIDANCE = {recreated!r}\n"
    )

    scanned, violations = find_restatements([offending], symbols)

    assert scanned, "the control scanned nothing — it did not reach"
    assert violations, (
        "the gate did NOT refuse a recreated F58 shape — it would have let the "
        "original defect through, which makes it decoration rather than "
        "enforcement"
    )
    assert any(
        "host_disposition._TARGET_REPEAT_HALT_PREFIX" in v for v in violations
    ), f"refused for the wrong reason: {violations}"
    assert len(violations) == 1, (
        "the docstring and the comment must be EXEMPT — design narration has to "
        f"be able to quote the defect it is about; got {violations}"
    )


def test_the_gate_permits_a_fragment_probe():
    """The other half of the control: the gate must NOT flag a legitimate probe.

    A gate that refuses everything is as useless as one that refuses nothing —
    it would force the family to stop asserting on rendered bodies at all. A
    short fragment has no production symbol to derive from, so it is a probe.
    """
    symbols = named_production_prose(_REPO / PRODUCTION_ROOT)
    probe = _normalise("did not clear it")
    flagged = [label for label, value in symbols.items() if value in probe]
    assert not flagged, (
        f"a fragment probe was flagged against {flagged}; the gate would force "
        "the constraint-4 family to stop asserting on rendered bodies"
    )
