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

Concretely, a violation is: a non-docstring string literal in ANY test file in
the tree whose value CONTAINS the whole value of a named production prose
symbol, where a named production prose symbol is a module-level `NAME = "..."`
or a bare `return "..."` in the loop package.

"ANY test file in the tree" is true as of 2026-08-07t and was NOT true before it.
Until then the population was a hand-maintained five-file tuple, and this
docstring said "the constraint-4 test family" while the invariant above says
"any test or instrument" — so "the invariant is enforced" and "the invariant is
enforced over five of 793 files" read identically. F62 measured the difference:
ten live instances of exactly this defect sat outside the five. They are bound,
and the population is now derived from the tree rather than typed out here.

A note on "named", because the word does two jobs above and they are not
interchangeable. A module-level `NAME = "..."` is importable: a test binds to it
with `from ... import NAME`. A bare `return "..."` is named only in the sense
that its enclosing function names it — `from ... import` cannot reach a return
statement, so a test can bind to it only by CALLING the function, which for some
assertions is circular and weakens them. Four such values were extracted to
module constants at 2026-08-07t so their tests could bind without weakening.

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
import re
from pathlib import Path

import pytest
from disco.core.loop.finish.verify_gate_parts.host_claims import handoff_refusal_detail

_REPO = Path(__file__).resolve().parents[2]

# The constraint-4 test family: the four files F58's scan covered, plus the
# route (B) driven-composition file added at 2026-08-07r.
#
# NO LONGER THE POPULATION (2026-08-07t). Until then this tuple WAS the gate's
# population, and the gate was real — 07s proved it refusing three of F58's four
# in-scope restatements against the tree's actual pre-repair bytes — but it policed
# five files of 793, and the same routine over the other 788 found TEN live
# instances of exactly the defect F58 named. That is F62. The ten are bound and the
# population is now DERIVED (below); this tuple is demoted to a HISTORICAL LOWER
# BOUND, asserted to remain a subset of the derived population so that a future
# edit to the derivation cannot silently narrow the gate back to nothing.
CONSTRAINT4_TEST_FAMILY = (
    "packages/core/tests/test_f51_constraint4_multifire.py",
    "packages/core/tests/test_f53_constraint4_confirmed_repeats.py",
    "packages/core/tests/test_f53_seo_primitive.py",
    "packages/core/tests/test_f55_f56_weakening_repeats_and_ordinals.py",
    "packages/core/tests/test_route_b_driven_composition.py",
)

PRODUCTION_ROOT = "packages/core/src/disco/core/loop"

# THE DERIVED-POPULATION RULE (2026-08-07s Amendment 4, standing; implemented here
# at 2026-08-07t). An enforcement's population must be DERIVED from artifacts — a
# glob, a manifest, a query — or the enforcement must print how wide that population
# actually is, so a reader can tell "no violations" from "no violations among the
# five I looked at". F52 was reach never attested; F60 a hand-maintained seed ledger
# that drifted; F62 this gate's own five-entry tuple. Third occurrence, general rule.
#
# The derivation: every `test_*.py` under the repository, minus vendored trees. It
# is deliberately a NAME glob rather than a read of `testpaths`, because
# `testpaths = ["packages", "tests/architecture", "tests/unbiased_gate"]` excludes
# `harness/`, and one of F62's ten lived in `harness/build_soak/tests/`. A
# population derived from what the battery RUNS would have missed it; a population
# derived from what EXISTS does not. There are no `*_test.py` files in this tree, so
# this glob is complete — `test_files_glob_is_complete` below re-measures that
# rather than trusting this sentence.
_VENDORED_DIR_NAMES = frozenset({".venv", "node_modules", ".git"})

# A HAND-ENUMERATED entry is permitted here ONLY if it is dated, names the symbol it
# restates, and states why binding is impossible at that site — per the same 07s
# rule. An undated, unexplained entry is the F58 defect wearing the gate's own
# clothes, and a reviewer will read it that way. IT IS EMPTY, and the ten sites F62
# found were repaired rather than excused: nine by importing the owning symbol and
# one — `harness/build_soak/tests/test_classifier.py` — by interpolating it.
# `allowlist_entries_are_dated_and_explained` below enforces the shape, so the rule
# is a check rather than a comment.
POPULATION_ALLOWLIST: tuple[tuple[str, str, str, str], ...] = ()


def derived_test_population(repo: Path) -> list[Path]:
    """Every test file in the tree, DERIVED — the gate's population.

    Sorted for determinism: the violation list this feeds is compared by a human
    across runs, and an arbitrary filesystem order would make two identical trees
    produce two different-looking reports.
    """
    allowed = {entry[0] for entry in POPULATION_ALLOWLIST}
    out: list[Path] = []
    for path in sorted(repo.rglob("test_*.py")):
        rel = path.relative_to(repo)
        if any(part in _VENDORED_DIR_NAMES for part in rel.parts):
            continue
        if rel.as_posix() in allowed:
            continue
        out.append(path)
    return out


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


def test_derived_population_covers_the_historical_family():
    """The widening may not silently NARROW (F62, 2026-08-07t).

    The five-file tuple stopped being the population here; it stays as a lower
    bound. If a future edit to `derived_test_population` drops the files the gate was
    originally built to police, that is a regression this catches — the failure
    mode a hand-population and a derived population share is going quietly empty.
    """
    derived = {p.relative_to(_REPO).as_posix() for p in derived_test_population(_REPO)}
    print(f"population: derived test files = {len(derived)}")
    print(f"population: historical constraint-4 family = {len(CONSTRAINT4_TEST_FAMILY)}")
    assert derived, "the derived population is EMPTY — fail closed, do not pass vacuously"
    dropped = [rel for rel in CONSTRAINT4_TEST_FAMILY if rel not in derived]
    assert not dropped, (
        f"the derived population no longer covers the original constraint-4 "
        f"family: {dropped}. The gate has been narrowed, not widened."
    )


def test_test_files_glob_is_complete():
    """`test_*.py` is the whole naming convention — measured, not assumed.

    The derivation above is a name glob. If this tree ever grows a `*_test.py`
    file, the glob stops being complete and the population silently loses it,
    which is F62 recurring in the repair for F62.
    """
    stragglers = [
        p.relative_to(_REPO).as_posix()
        for p in _REPO.rglob("*_test.py")
        if not any(part in _VENDORED_DIR_NAMES for part in p.relative_to(_REPO).parts)
    ]
    assert not stragglers, (
        f"test files exist that this gate's `test_*.py` glob does not reach: "
        f"{stragglers}. Widen the derivation — do not allowlist them."
    )


def test_allowlist_entries_are_dated_and_explained():
    """The 07s rule as a CHECK, not a comment.

    A hand-enumerated exclusion is permitted only where every entry is dated,
    names the symbol it restates, and says why binding is impossible there. The
    allowlist is empty today; this fails the moment an undated one is added.
    """
    print(f"population: allowlist entries = {len(POPULATION_ALLOWLIST)}")
    for entry in POPULATION_ALLOWLIST:
        assert len(entry) == 4, f"allowlist entry must be (path, date, symbol, why): {entry}"
        rel, date, symbol, why = entry
        assert (_REPO / rel).exists(), f"allowlist names a file that does not exist: {rel}"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}[a-z]?", date), (
            f"allowlist entry for {rel} is not dated: {date!r}"
        )
        assert symbol.strip(), f"allowlist entry for {rel} names no restated symbol"
        assert len(why.split()) >= 5, (
            f"allowlist entry for {rel} does not say why binding is impossible: {why!r}"
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

    THE NAME IS STALE AND IS KEPT ON PURPOSE. This gate no longer polices the
    constraint-4 family; it polices the whole test tree (see POPULATION below).
    Renaming it would DELETE a sealed test id from `architecture/test-inventory.json`,
    and the only mechanism that authorizes a deletion is `module_split_transitions`
    — a record for module splits. Spending it on a cosmetic rename would write a
    false justification into a sealed authority, which costs more than a stale
    identifier. Read the docstring, not the name.

    The failure this prevents, concretely: `_HALT_GUIDANCE` was a hand-copied
    duplicate of the guidance `governed_contract_refusal`'s HALT branch composes.
    Reword the product and that test keeps passing while asserting on a string
    the product no longer emits — it stops testing without ever going red.

    POPULATION: the WHOLE test tree since 2026-08-07t, derived rather than
    enumerated (F62). It was five files until then, and this docstring said
    "the constraint-4 test family" while the invariant it enforces says "any
    test or instrument". Those were indistinguishable in the record; they are
    now distinguished, and the sizes below are printed so a reader can tell
    "no violations" from "no violations among the five I looked at".
    """
    symbols = named_production_prose(_REPO / PRODUCTION_ROOT)
    assert symbols, "empty production corpus — fail closed rather than pass vacuously"

    population = derived_test_population(_REPO)
    print(f"population: test files policed = {len(population)}")
    print(f"population: named production prose symbols = {len(symbols)}")
    assert population, (
        "the derived test population is EMPTY — fail closed. A zero found over a "
        "zero-sized population is not evidence of absence; it is evidence the "
        "instrument did not reach."
    )

    scanned, violations = find_restatements(population, symbols)

    print(f"population: test literals scanned = {scanned}")
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
    #
    # 2026-08-07s Correction 1 found this claim OVERSTATED: only the prefix half was
    # derived and the second clause was retyped from `host_claims.handoff_refusal_detail`.
    # Repaired at 2026-08-07t by DERIVING that half too, rather than by softening the
    # sentence — `handoff_refusal_detail(None, None)` is production's own no-handoff
    # branch, so the claim above is now true of BOTH halves. The corpus cannot supply
    # it: that value is a bare `return` inside a three-return function, addressable in
    # `named_production_prose` only as `host_claims.handoff_refusal_detail:<lineno>`,
    # and a line number is exactly the kind of handle that moves under an unrelated
    # edit (this boundary moved it 324 -> 329 by adding a constant above it).
    recreated = halt_prefix + handoff_refusal_detail(None, None)
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
