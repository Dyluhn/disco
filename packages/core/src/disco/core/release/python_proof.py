"""Single authoritative proof helpers for Python release candidates.

Dependency requirements are validated by exactly one bounded grammar: the reference
PEP 508/440 implementation in :mod:`packaging` decides names, extras, specifiers, and
URLs; a small satisfiability guard rejects contradictory constraints; and an
invariance-aware marker evaluator refuses to certify a required dependency whose marker
references any environment variable that is not fixed for the emitted ``python:3.13-slim``
image.  ``requires-python`` is decided by the same specifier engine against the image's
invariant version band.  The Python target proof establishes *reachability* of one exact
``module:attribute`` (or ``--factory`` callable) from the immutable source: a known
ASGI/WSGI framework construction or an exact protocol callable, with every module-scope
and target-scope import backed by the install plan or a proven workspace-local module.
When the target name is itself bound by a workspace-local re-export
(``from <module> import <name>``), the same strict proof recurses into the referenced
module's immutable source and proves the re-exported name there — bounded to
``_MAX_REEXPORT_DEPTH`` hops, guarded by a visited-set against cycles, and restricted to
modules whose source is supplied and that exist in the workspace graph, so a missing
module, an unresolvable relative level (e.g. a top-level relative import), a ``*`` import,
a cycle, or a depth overflow all fail closed.  Nothing here executes user text, imports the
target, or guesses through aliases.

Name-collision ceiling (fail closed).  A workspace root that is *also* an installed
distribution is a sys.path-order shadow collision: the server (uvicorn/gunicorn) puts the
application directory on ``sys.path[0]``, so a bare ``import <name>`` binds the workspace
file and shadows the distribution of the same name for the whole process.  Such a name is
never certifiably two things at once, and the shadow path is typically broken (a workspace
``fastapi.py`` doing ``from fastapi import FastAPI`` is a self-import circular crash), so a
colliding import or re-export cannot be soundly certified and fails closed wherever it is
relied upon (:func:`_root_is_shadow_collision`).  This conservatively rejects even the rare
benign shadow (e.g. a colliding module that defines a self-contained raw-ASGI app with no
imports) reached via a re-export or a colliding module-scope import — a workspace module that
shadows an installed distribution is a genuine footgun.  Non-colliding workspace names
(``real``, ``pkg.real``, ``app``, ``server`` — names that are not installed distributions)
are unaffected and resolve exactly as before.

File/package identity ceiling (fail closed).  A dotted name produced by two distinct workspace
files — most importantly ``<name>.py`` and ``<name>/__init__.py``, which CPython resolves to the
*package* — is not statically decidable to one source, so any module-scope import or re-export
hop that resolves through such an ambiguous identity fails closed rather than certifying the
file's app when the package (an unrunnable ``app = None``) is what actually imports.

All rejection text is fixed and value-free: requirement strings, source text, attribute
names, and package names can be attacker-controlled or secret-bearing.
"""

from __future__ import annotations

import ast
import builtins
import keyword
import operator
import re
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

_REQUIREMENT_ERROR = "requirement is outside the bounded PEP 508/440 proof grammar"
_SOURCE_ERROR = "python source is not statically parseable"
_FACTORY_ERROR = "python --factory target is not a proven zero-argument application factory"
_ATTRIBUTE_ERROR = "python target attribute is not a simple identifier"
_BINDING_ERROR = "python target is missing or ambiguously bound at module scope"
_SHAPE_ERROR = "python target has no closed server-compatible application shape"
_DEPENDENCY_ERROR = "python target uses an unproven import or global dependency"

# The emitted image is python:3.13-slim: the minor is fixed but the patch is not, so a
# requires-python band is only *compatible* when EVERY plausible final 3.13 patch satisfies
# it. A dense sample (not just the endpoints) rejects a mid-range hole such as ``!=3.13.5``.
_IMAGE_PYTHON_BAND: tuple[str, ...] = tuple(f"3.13.{patch}" for patch in range(0, 64))

_MAX_REQUIREMENT_LENGTH = 2048
_MAX_MARKER_TOKENS = 192
_MAX_MARKER_DEPTH = 16
_MAX_MARKER_COMPARISONS = 64
_MAX_SOURCE_LENGTH = 1_000_000

_DISTRIBUTION_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?\Z")
_NUMERIC_RELEASE_RE = re.compile(r"[0-9]+(?:\.[0-9]+){0,7}\Z")
_MARKER_STRING_RE = re.compile(r"[A-Za-z0-9._ -]{0,128}\Z")

_INVARIANT_MARKER_ENV: dict[str, str] = {
    "implementation_name": "cpython",
    "os_name": "posix",
    "platform_python_implementation": "CPython",
    "platform_system": "Linux",
    "python_version": "3.13",
    "sys_platform": "linux",
}
_MOVING_OR_PLATFORM_MARKERS = frozenset(
    {
        "implementation_version",
        "platform_machine",
        "platform_release",
        "platform_version",
        "python_full_version",
    }
)


def _reject_requirement() -> ValueError:
    return ValueError(_REQUIREMENT_ERROR)


def _normalize_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _strip_requirement_comment(spec: str) -> str:
    """Strip only a whitespace-introduced requirements-file comment.

    Quotes are tracked solely so ``#`` cannot truncate a marker string.  Escapes,
    control characters, multiline continuations, and unterminated quotes are outside
    this proof language and fail closed.
    """

    quote: str | None = None
    for index, char in enumerate(spec):
        if ord(char) < 32 or ord(char) == 127 or char == "\\":
            raise _reject_requirement()
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "#":
            if index == 0 or not spec[index - 1].isspace():
                raise _reject_requirement()
            return spec[:index].rstrip()
    if quote is not None:
        raise _reject_requirement()
    return spec.strip()


class _MarkerToken(NamedTuple):
    kind: str
    value: str


def _lex_marker(marker: str) -> tuple[_MarkerToken, ...]:
    tokens: list[_MarkerToken] = []
    index = 0
    while index < len(marker):
        char = marker[index]
        if char == " ":
            index += 1
            continue
        if char == "(":
            tokens.append(_MarkerToken("lparen", char))
            index += 1
            continue
        if char == ")":
            tokens.append(_MarkerToken("rparen", char))
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            end = index + 1
            while end < len(marker) and marker[end] != quote:
                if marker[end] == "\\" or ord(marker[end]) < 32:
                    raise _reject_requirement()
                end += 1
            if end >= len(marker):
                raise _reject_requirement()
            value = marker[index + 1 : end]
            if _MARKER_STRING_RE.fullmatch(value) is None:
                raise _reject_requirement()
            tokens.append(_MarkerToken("string", value))
            index = end + 1
            continue
        if char.isascii() and (char.isalpha() or char == "_"):
            end = index + 1
            while (
                end < len(marker)
                and marker[end].isascii()
                and (marker[end].isalnum() or marker[end] == "_")
            ):
                end += 1
            word = marker[index:end]
            kind = word if word in {"and", "or", "not", "in"} else "name"
            tokens.append(_MarkerToken(kind, word))
            index = end
            continue
        operator = next(
            (
                candidate
                for candidate in ("===", "==", "!=", "<=", ">=", "~=", "<", ">")
                if marker.startswith(candidate, index)
            ),
            None,
        )
        if operator is None:
            raise _reject_requirement()
        tokens.append(_MarkerToken("operator", operator))
        index += len(operator)
        if len(tokens) > _MAX_MARKER_TOKENS:
            raise _reject_requirement()
    if not tokens or len(tokens) > _MAX_MARKER_TOKENS:
        raise _reject_requirement()
    return (*tokens, _MarkerToken("eof", ""))


def _numeric_release(value: str) -> tuple[int, ...]:
    if _NUMERIC_RELEASE_RE.fullmatch(value) is None:
        raise _reject_requirement()
    parts = value.split(".")
    if any(len(part) > 9 for part in parts):
        raise _reject_requirement()
    release = tuple(int(part) for part in parts)
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]
    return release


def _ordered_releases(left: str, right: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    lhs = _numeric_release(left)
    rhs = _numeric_release(right)
    width = max(len(lhs), len(rhs))
    return lhs + (0,) * (width - len(lhs)), rhs + (0,) * (width - len(rhs))


def _compare_marker(variable: str, literal: str, operator: str, *, variable_left: bool) -> bool:
    if variable in _MOVING_OR_PLATFORM_MARKERS or variable not in _INVARIANT_MARKER_ENV:
        raise _reject_requirement()
    known = _INVARIANT_MARKER_ENV[variable]
    if variable == "python_version":
        if operator in {"in", "not in", "==="}:
            raise _reject_requirement()
        if not variable_left:
            # A reversed comparison (``"3.13" < python_version``) is valid PEP 508 and equals
            # ``python_version > "3.13"``: ``==``/``!=`` are symmetric and ordered operators
            # invert. ``~=`` (compatible release) has no sound reversed reading, so it — like any
            # other reversed operator — fails closed rather than being silently mis-evaluated.
            reversed_operator = {"<": ">", ">": "<", "<=": ">=", ">=": "<="}
            if operator in {"==", "!="}:
                pass
            elif operator in reversed_operator:
                operator = reversed_operator[operator]
            else:
                raise _reject_requirement()
        lhs, rhs = _ordered_releases(known, literal)
        if operator == "==":
            return lhs == rhs
        if operator == "!=":
            return lhs != rhs
        if operator == "<":
            return lhs < rhs
        if operator == "<=":
            return lhs <= rhs
        if operator == ">":
            return lhs > rhs
        if operator == ">=":
            return lhs >= rhs
        if operator == "~=":
            raw_rhs = _numeric_release(literal)
            release_parts = literal.split(".")
            if len(release_parts) < 2:
                raise _reject_requirement()
            lower_ok = lhs >= rhs
            prefix_width = len(release_parts) - 1
            lhs = lhs + (0,) * max(0, prefix_width - len(lhs))
            padded_rhs = raw_rhs + (0,) * max(0, prefix_width - len(raw_rhs))
            return lower_ok and lhs[:prefix_width] == padded_rhs[:prefix_width]
        raise _reject_requirement()

    if operator not in {"==", "!=", "in", "not in"}:
        raise _reject_requirement()
    left, right = (known, literal) if variable_left else (literal, known)
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    contained = left in right
    return not contained if operator == "not in" else contained


class _MarkerParser:
    def __init__(self, tokens: tuple[_MarkerToken, ...]) -> None:
        self._tokens = tokens
        self._index = 0
        self._depth = 0
        self._comparisons = 0

    @property
    def _token(self) -> _MarkerToken:
        return self._tokens[self._index]

    def _take(self, kind: str) -> _MarkerToken:
        token = self._token
        if token.kind != kind:
            raise _reject_requirement()
        self._index += 1
        return token

    def parse(self) -> bool:
        result = self._parse_or()
        self._take("eof")
        return result

    def _parse_or(self) -> bool:
        result = self._parse_and()
        while self._token.kind == "or":
            self._index += 1
            right = self._parse_and()
            result = result or right
        return result

    def _parse_and(self) -> bool:
        result = self._parse_primary()
        while self._token.kind == "and":
            self._index += 1
            right = self._parse_primary()
            result = result and right
        return result

    def _parse_primary(self) -> bool:
        if self._token.kind == "lparen":
            self._index += 1
            self._depth += 1
            if self._depth > _MAX_MARKER_DEPTH:
                raise _reject_requirement()
            result = self._parse_or()
            self._take("rparen")
            self._depth -= 1
            return result
        return self._parse_comparison()

    def _parse_operand(self) -> _MarkerToken:
        if self._token.kind not in {"name", "string"}:
            raise _reject_requirement()
        token = self._token
        self._index += 1
        return token

    def _parse_comparison(self) -> bool:
        left = self._parse_operand()
        if self._token.kind == "not":
            self._index += 1
            self._take("in")
            operator = "not in"
        elif self._token.kind == "in":
            operator = "in"
            self._index += 1
        else:
            operator = self._take("operator").value
        right = self._parse_operand()
        self._comparisons += 1
        if self._comparisons > _MAX_MARKER_COMPARISONS:
            raise _reject_requirement()
        if left.kind == right.kind:
            raise _reject_requirement()
        if left.kind == "name":
            return _compare_marker(left.value, right.value, operator, variable_left=True)
        return _compare_marker(right.value, left.value, operator, variable_left=False)


def _release_to_version(release: tuple[int, ...]) -> Version | None:
    if not release:
        return None
    try:
        return Version(".".join(str(part) for part in release))
    except InvalidVersion:
        return None


def _specifier_operand_versions(specset: SpecifierSet) -> list[Version]:
    """Each operand's version as a real ``Version`` (wildcards stripped, prereleases kept)."""
    versions: list[Version] = []
    for spec in specset:
        base = spec.version
        while base.endswith(".*"):
            base = base[:-2]
        try:
            versions.append(Version(base))
        except InvalidVersion:
            continue
    return versions


def _opts_into_prereleases(specset: SpecifierSet) -> bool:
    """Whether the set itself opts into prereleases (some operand is a prerelease/dev)."""
    return any(version.is_prerelease for version in _specifier_operand_versions(specset))


def _candidate_versions(specset: SpecifierSet) -> list[Version]:
    """A complete-over-the-grammar set of real versions to witness satisfiability.

    Every candidate is an actually-installable version, so a satisfying candidate is a sound
    proof of satisfiability.  Completeness (no false rejection of a satisfiable set) comes from
    covering, for every operand: the exact operand version (so prerelease/epoch/local pins have
    a witness), the derived release tuple with padded and ±1-per-position neighbours, the global
    extremes, and interior representatives between each consecutive pair of sorted releases (so an
    open interval such as ``>1,<2`` — or a NARROW one like ``>1.2,<1.2.1`` whose only witnesses
    are deeper, e.g. ``1.2.0.1`` — has a witness).
    """
    operands = _specifier_operand_versions(specset)
    releases: set[tuple[int, ...]] = set()
    for version in operands:
        release = version.release
        releases.add(release)
        releases.add(release + (0,))
        releases.add(release + (0, 0))
        for index in range(len(release)):
            for delta in (-1, 1):
                mutated = list(release)
                mutated[index] = max(0, mutated[index] + delta)
                releases.add(tuple(mutated))
    releases.add((0,))
    releases.add((99999, 99999))
    versions: set[Version] = set(operands)
    for release in releases:
        version = _release_to_version(release)
        if version is not None:
            versions.add(version)
    ordered = sorted(versions)
    for lower, upper in zip(ordered, ordered[1:], strict=False):
        if lower >= upper:
            continue
        # A narrow interval (``>1.2,<1.2.1``) is only satisfied by a DEEPER release than
        # ``lower + (1,)`` (which would be ``1.2.1`` == the upper bound). Probe successively
        # deeper interior points (``1.2.1``, ``1.2.0.1``, ``1.2.0.0.1`` ...) so at least one lands
        # strictly inside. Each is a real installable version tested with ``.contains`` below, so
        # extra probes only ever REDUCE over-rejection — they can never mint a false candidate.
        for pad in range(4):
            interior = _release_to_version(lower.release + (0,) * pad + (1,))
            if interior is not None and lower < interior < upper:
                versions.add(interior)
                break
    return sorted(versions)


def _specifier_set_is_satisfiable(specset: SpecifierSet) -> bool:
    """Whether some version pip would actually install satisfies the ENTIRE specifier set.

    Sound and witness-based (not a string heuristic): a bare name (no operands) is trivially
    satisfiable; otherwise the set is satisfiable only if a concrete installable version exists.
    pip installs a final release when it satisfies the set (``prereleases=False``); a prerelease
    is installable only when the set opts into prereleases, so those candidates are considered
    only then (``prereleases=True``).  ``==1.*,!=1.*`` and every other empty intersection have no
    witness and are rejected; ``==1.*,!=1.0.*`` keeps ``1.1`` and is accepted.
    """
    if not list(specset):
        return True
    candidates = _candidate_versions(specset)
    if any(
        not version.is_prerelease and specset.contains(version, prereleases=False)
        for version in candidates
    ):
        return True
    if _opts_into_prereleases(specset):
        return any(
            version.is_prerelease and specset.contains(version, prereleases=True)
            for version in candidates
        )
    return False


def _validated_requirement_name(requirement_text: str) -> str:
    """Validate one registry requirement (name/extras/specifier) via the reference grammar.

    ``packaging`` is the single authoritative PEP 508/440 implementation: it rejects invalid
    names, malformed extras, invalid comparators (including a non-``==``/``!=`` wildcard), and
    URL/path forms.  We additionally reject any direct-reference URL and any specifier set with
    no installable witness version (an empty intersection such as ``==1.*,!=1.*``), since either
    makes the emitting ``pip install`` fail closed.
    """

    try:
        parsed = Requirement(requirement_text)
    except InvalidRequirement as exc:
        raise _reject_requirement() from exc
    if parsed.url is not None:
        raise _reject_requirement()
    if parsed.marker is not None:
        raise _reject_requirement()
    if not _specifier_set_is_satisfiable(parsed.specifier):
        raise _reject_requirement()
    return _normalize_distribution(parsed.name)


def requires_python_supported(specifier: str) -> bool:
    """Whether ``requires-python`` is provably compatible with the emitted image.

    ``True`` only when the entire invariant patch band of ``python:3.13-slim`` satisfies the
    specifier; an incompatible band (e.g. ``<3.13``) or a patch-dependent one (undecidable
    from the invariant minor) is ``False`` so the caller fails closed.  Invalid specifiers
    raise ``ValueError`` (value-free).
    """

    if not isinstance(specifier, str) or len(specifier) > _MAX_REQUIREMENT_LENGTH:
        raise _reject_requirement()
    try:
        specset = SpecifierSet(specifier)
        return all(specset.contains(version, prereleases=True) for version in _IMAGE_PYTHON_BAND)
    except (InvalidSpecifier, InvalidVersion) as exc:
        raise _reject_requirement() from exc


def active_requirement_name(spec: str) -> str | None:
    """Return the canonical name when a registry requirement is active for the image.

    ``None`` means the requirement's environment marker is provably false (an invariant
    marker that does not hold) — the package is legitimately absent.  ``ValueError`` means
    the line is invalid, a URL/path/directive, unsatisfiable, or a marker that references a
    variable that is *not* fixed for the emitted image (so it cannot certify the package).
    Names, extras, and version specifiers use the reference PEP 508/440 grammar; valid
    wildcards (``==1.*``/``!=1.*``), prereleases, extras, and compound constraints are
    accepted, while invalid wildcards and contradictory constraints fail closed.
    """

    try:
        if not isinstance(spec, str) or not spec or len(spec) > _MAX_REQUIREMENT_LENGTH:
            raise _reject_requirement()
        line = _strip_requirement_comment(spec)
        if not line:
            raise _reject_requirement()
        requirement, separator, marker = line.partition(";")
        requirement = requirement.strip()
        if not requirement or requirement.startswith("-"):
            raise _reject_requirement()
        name = _validated_requirement_name(requirement)
        if separator:
            if not marker.strip():
                raise _reject_requirement()
            active = _MarkerParser(_lex_marker(marker.strip())).parse()
            if not active:
                return None
        return name
    except (IndexError, RecursionError):
        raise _reject_requirement() from None


@dataclass(frozen=True)
class _ImportRef:
    root: str
    qualified: str


class _ModuleBindingVisitor(ast.NodeVisitor):
    """Names bound while executing module-level statements; nested scopes are opaque."""

    def __init__(self) -> None:
        self.names: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.append(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.names.append(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.append(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.append(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.names.append(alias.asname or alias.name.split(".", maxsplit=1)[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name != "*":
                self.names.append(alias.asname or alias.name)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is not None:
            self.names.append(node.name)
        for statement in node.body:
            self.visit(statement)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name is not None:
            self.names.append(node.name)
        if node.pattern is not None:
            self.visit(node.pattern)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name is not None:
            self.names.append(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest is not None:
            self.names.append(node.rest)
        self.generic_visit(node)


class _ScopeImport(NamedTuple):
    level: int
    module: str | None
    names: tuple[str, ...]
    # The set of GUARD TIERS (see the ladder below) the enclosing ``try`` bodies SWALLOW for this
    # import, and the set they ABORT (catch but terminate).  A per-tier SET — not a scalar — because
    # real guards are non-monotonic (a missing NAME can be swallowed while a missing MODULE is not).
    # A failure of tier T is optional iff ``T in swallow``.  ``block`` matters only when composing
    # with an outer guard (recursion): an inner ABORT of T must veto an ambient swallow of T.
    swallow: frozenset[int] = frozenset()
    block: frozenset[int] = frozenset()


@dataclass(frozen=True)
class _ImportContext:
    """The install plan plus the immutable workspace module graph the target may import."""

    installed: frozenset[str]
    local_roots: frozenset[str]
    local_dotted: frozenset[str]
    package: tuple[str, ...]
    # Dotted names produced by >=2 distinct workspace files (file-vs-package ``__init__`` or
    # normalized-path duplicates): CPython's binding is not statically decidable, so any import
    # or re-export resolving THROUGH such a name fails closed. ``packages`` are the dotted names
    # backed by a package ``__init__.py`` (so a re-export into one uses the package as its own
    # ``__package__`` when resolving relative imports).
    ambiguous: frozenset[str] = frozenset()
    packages: frozenset[str] = frozenset()


def _dotted_prefixes(dotted: str) -> frozenset[str]:
    """Every dotted-component prefix of ``dotted`` (``a.b.c`` -> ``a``, ``a.b``, ``a.b.c``)."""
    parts = dotted.split(".")
    return frozenset(".".join(parts[: index + 1]) for index in range(len(parts)))


def _resolves_through_ambiguous(dotted: str, ctx: _ImportContext) -> bool:
    """Whether resolving ``dotted`` traverses an ambiguous file/package identity."""
    return bool(ctx.ambiguous & _dotted_prefixes(dotted))


# GUARD TIER ladder — a label per failure mode, ordered by how broad an ``except`` must be to catch
# it, because the import failure modes raise DIFFERENT exceptions along the real class hierarchy
# (BaseException > Exception > ImportError > ModuleNotFoundError).  A guard swallows a failure iff
# that failure's tier is in the guard's swallowed-tier SET — NOT a scalar threshold, because real
# guards are non-monotonic (see :func:`_stack_guard_sets`): a missing NAME (tier 2) can be swallowed
# while a missing MODULE (tier 1) is aborted by an earlier ``except ModuleNotFoundError`` handler.
#   NONE       — sentinel: no abort / unguarded.
#   MODULE     — ``except ModuleNotFoundError``: swallows a missing MODULE (ModuleNotFoundError).
#   NAME       — ``except ImportError`` (or broader within Exception): also a missing NAME.
#   EXCEPTION  — ``except Exception``: also an Exception-class abort (assert / raise RuntimeError).
#   BASE       — ``except BaseException`` / bare: also SystemExit (sys.exit / exit / quit / raise).
#   UNCATCHABLE— strictly above BASE and in NO guard's swallow-set: a process-terminating call that
#                raises no catchable exception (``os._exit`` / ``os.abort`` / ``os.kill(getpid,…)``)
#                or a ``raise`` of a name rebound at module scope (real type statically unknowable).
_TIER_NONE = 0
_TIER_MODULE = 1
_TIER_NAME = 2
_TIER_EXCEPTION = 3
_TIER_BASE = 4
_TIER_UNCATCHABLE = 5

# The exception-class NAMES whose ``except`` clause catches a representative exception of each tier.
# A missing module raises ModuleNotFoundError (tier 1); a missing name a plain ImportError (tier 2);
# an Exception-class abort a subclass of Exception (tier 3); a BASE-class abort a BaseException that
# is NOT an Exception, e.g. SystemExit (tier 4).  Because ModuleNotFoundError is a strict subclass
# of ImportError, ``except ModuleNotFoundError`` does NOT appear at tier 2+; because SystemExit is
# not an Exception, ``except Exception`` does NOT appear at tier 4.
_TIER_CATCHERS: dict[int, frozenset[str]] = {
    _TIER_MODULE: frozenset({"ModuleNotFoundError", "ImportError", "Exception", "BaseException"}),
    _TIER_NAME: frozenset({"ImportError", "Exception", "BaseException"}),
    _TIER_EXCEPTION: frozenset({"Exception", "BaseException"}),
    _TIER_BASE: frozenset({"BaseException"}),
}


_ALL_TIERS = (_TIER_MODULE, _TIER_NAME, _TIER_EXCEPTION, _TIER_BASE)

# Per-tier routing of a BODY failure through one ``try``: does its first-matching handler SWALLOW it
# (catches, non-aborting), ABORT (catches, but its body raises/exits so the failure — or a new one —
# still terminates that ``try``), or does no handler catch it so it PASSes outward?  Kept as an
# explicit three-way outcome because a single scalar "guard tier" cannot represent a NON-monotonic
# guard (``except ModuleNotFoundError: sys.exit()`` / ``except ImportError: x=None`` swallows a
# missing NAME (tier 2) yet ABORTS a missing MODULE (tier 1) — proven against the live runtime).
_ROUTE_PASS = 0
_ROUTE_SWALLOW = 1
_ROUTE_ABORT = 2


def _handler_catches_tier(handler: ast.ExceptHandler, tier: int) -> bool:
    """Whether an ``except`` clause catches a representative exception of ``tier`` (or is bare).

    An UNCATCHABLE-tier failure (``os._exit`` / rebound-name raise) is caught by NO handler — not
    even a bare ``except:`` — as it raises no catchable exception, so this returns False for it.
    """
    catchers = _TIER_CATCHERS.get(tier)
    if catchers is None:  # UNCATCHABLE (or any tier outside the catchable ladder)
        return False
    exc = handler.type
    if exc is None:
        return True
    candidates = exc.elts if isinstance(exc, ast.Tuple) else [exc]
    return any(isinstance(node, ast.Name) and node.id in catchers for node in candidates)


def _try_tier_route(node: ast.Try | ast.TryStar, tier: int) -> int:
    """Route a tier-``tier`` BODY failure through one ``try`` (first-matching handler, CPython).

    The FIRST handler whose type catches that failure's representative exception decides: SWALLOW if
    its body does not unconditionally abort, else ABORT.  A handler that does not catch it (``except
    ModuleNotFoundError`` for a plain ImportError, ``except ValueError`` for anything here) is
    skipped; if none catches, the failure PASSes to any enclosing ``try``.
    """
    for handler in node.handlers:
        if _handler_catches_tier(handler, tier):
            aborts = _statements_unconditionally_abort(handler.body)
            return _ROUTE_ABORT if aborts else _ROUTE_SWALLOW
    return _ROUTE_PASS


def _stack_guard_sets(
    try_stack: Sequence[ast.Try | ast.TryStar],
) -> tuple[frozenset[int], frozenset[int]]:
    """The tiers a stack of enclosing ``try`` bodies SWALLOWs vs ABORTs, per-tier first-decided.

    Walking innermost -> outermost, the first ``try`` that does not PASS a tier decides it (an inner
    ABORT masks any broader outer handler, exactly as CPython routes).  Tiers left un-decided (all
    PASS) are in neither set.  The two sets are disjoint; a failure of tier T is swallowed by this
    stack iff ``T`` is in the SWALLOW set.
    """
    swallow: set[int] = set()
    block: set[int] = set()
    for tier in _ALL_TIERS:
        for node in reversed(try_stack):
            route = _try_tier_route(node, tier)
            if route == _ROUTE_SWALLOW:
                swallow.add(tier)
                break
            if route == _ROUTE_ABORT:
                block.add(tier)
                break
    return frozenset(swallow), frozenset(block)


class _ScopeImportVisitor(ast.NodeVisitor):
    """Imports executed in one scope, without descending into child scopes.

    Each import carries the SWALLOW/ABORT tier sets of the ``try`` bodies enclosing it.  Nested
    ``try`` guards COMPOSE by CPython routing (innermost first, an inner ABORT masks a broader outer
    handler); :func:`_stack_guard_sets` computes the composed sets from the enclosing ``try`` stack.
    A guarded ``try``'s ``else``/``finally``/handler bodies are NOT protected by that ``try`` (an
    exception there is not caught by the same ``try``), so they are visited with it off the stack.

    A statically-dead ``if`` branch is skipped (:meth:`visit_If`) so its never-executed imports are
    not required; ``runs_as_main`` supplies the ``__name__ == '__main__'`` fold for the top-level
    executed module (``None`` = no fold, the pre-existing behavior for every other scope).
    """

    def __init__(self, runs_as_main: bool | None = None) -> None:
        self.imports: list[_ScopeImport] = []
        self._try_stack: list[ast.Try] = []
        self._runs_as_main = runs_as_main

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_If(self, node: ast.If) -> None:
        # A statically-dead branch never executes, so its imports never run and must not be
        # required: descend ONLY the branch the test statically decides (``if False`` /
        # ``if TYPE_CHECKING`` skip their body; ``if __name__ == '__main__'`` folds per
        # ``runs_as_main`` on the top-level module).  A test that needs runtime eval (``None``)
        # is the dynamic boundary -> BOTH branches are visited, so a conditionally-run import is
        # still proven (the pre-existing behavior, since no ``if`` reachability was applied before).
        # The enclosing ``try`` guard stack is instance state, so imports in the descended branch
        # keep their swallow tiers.
        truth = _guard_truthiness(node.test, runs_as_main=self._runs_as_main)
        if truth is True:
            branches: tuple[ast.stmt, ...] = tuple(node.body)
        elif truth is False:
            branches = tuple(node.orelse)
        else:
            branches = (*node.body, *node.orelse)
        for statement in branches:
            self.visit(statement)

    def visit_Import(self, node: ast.Import) -> None:
        swallow, block = _stack_guard_sets(self._try_stack)
        self.imports.extend(_ScopeImport(0, alias.name, (), swallow, block) for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        names = tuple(alias.name for alias in node.names if alias.name != "*")
        swallow, block = _stack_guard_sets(self._try_stack)
        self.imports.append(_ScopeImport(node.level, node.module, names, swallow, block))

    def visit_Try(self, node: ast.Try) -> None:
        self._try_stack.append(node)
        for statement in node.body:
            self.visit(statement)
        self._try_stack.pop()
        # Handlers, ``else``, and ``finally`` are NOT guarded by THIS try (visited with it popped).
        for handler in node.handlers:
            self.visit(handler)
        for statement in (*node.orelse, *node.finalbody):
            self.visit(statement)


def _root_is_shadow_collision(root: str, ctx: _ImportContext) -> bool:
    """Whether ``root`` names BOTH a workspace module and an installed distribution.

    Such a name is a runtime sys.path-order footgun: uvicorn/gunicorn put the app directory
    on ``sys.path[0]``, so an ``import <root>`` binds the *workspace* file and shadows the
    installed distribution of the same name for the whole process.  The name is therefore not
    two things at once — it is exactly the workspace module OR the installed package, never
    both — and neither the installed-package assumption nor a shadow re-implementation can be
    soundly certified from static text.  It is a genuine collision that fails closed.
    """

    return root in ctx.local_roots and _normalize_distribution(root) in ctx.installed


# Proving a workspace-local import recurses into that module's OWN imports; a hard depth ceiling
# plus a visited-set of resolved module names make the recursion terminate and fail closed on any
# unbounded chain (a revisited module is a benign runtime import cycle, out of scope).
_MAX_LOCAL_IMPORT_DEPTH = 32


def _imported_local_modules(
    module_dotted: str, names: tuple[str, ...], ctx: _ImportContext
) -> list[str]:
    """The workspace-local modules an import statement actually imports: the module and any of its
    ``from``-imported names that are themselves submodules (so ``from pkg import sub`` also imports
    ``pkg.sub``)."""

    modules = [module_dotted]
    modules.extend(
        submodule for name in names if (submodule := f"{module_dotted}.{name}") in ctx.local_dotted
    )
    return modules


class _ModuleScopeLiveVisitor(ast.NodeVisitor):
    """Names bound at MODULE scope that survive to import completion (for name-existence only).

    Distinct from :class:`_ModuleBindingVisitor` (which the exact-once target check relies on):
    this excludes names that do not exist when ``from mod import name`` runs — a ``del``'d name and
    an ``except ... as <name>`` handler name (auto-deleted at handler exit) — and does not count
    comprehension targets (they never leak to module scope).  It descends module-scope control flow
    (if/for/while/with/try) but never a function/class/lambda body nor a comprehension scope.
    """

    def __init__(self) -> None:
        self.bound: set[str] = set()
        self.deleted: set[str] = set()
        self.except_as: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.bound.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.bound.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.bound.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_ListComp(self, node: ast.ListComp) -> None:
        return

    def visit_SetComp(self, node: ast.SetComp) -> None:
        return

    def visit_DictComp(self, node: ast.DictComp) -> None:
        return

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        return

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self.bound.add(node.id)
        elif isinstance(node.ctx, ast.Del):
            self.deleted.add(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.bound.add(alias.asname or alias.name.split(".", maxsplit=1)[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name != "*":
                self.bound.add(alias.asname or alias.name)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is not None:
            self.except_as.add(node.name)
        for statement in node.body:
            self.visit(statement)


def _module_scope_live_names(tree: ast.Module) -> frozenset[str]:
    """Module-scope names that are importable (bound, not later ``del``'d nor ``except``-scoped)."""
    visitor = _ModuleScopeLiveVisitor()
    for statement in tree.body:
        visitor.visit(statement)
    return frozenset(visitor.bound - visitor.deleted - visitor.except_as)


def _module_has_children(module_dotted: str, ctx: _ImportContext) -> bool:
    """Whether ``module_dotted`` is a package directory with real submodules in the graph."""
    prefix = module_dotted + "."
    return any(dotted.startswith(prefix) for dotted in ctx.local_dotted)


def _from_import_names_exist(
    module_dotted: str, names: tuple[str, ...], ctx: _ImportContext, sources: Mapping[str, str]
) -> bool:
    """For ``from <local module> import <names>``, whether each name resolves in the module.

    A name resolves when it is a real submodule (``module.name`` in the graph) OR a live module-
    scope name (see :func:`_module_scope_live_names`).  When a name is neither and the module has a
    source, it fails closed — ``import name`` raises ``ImportError`` at boot.  A ``from ... import``
    ``*`` re-export or a module-level ``__getattr__`` can bind names invisibly, but those are not
    statically decidable, so an unlisted name still fails closed conservatively.

    A module with NO source is either a namespace package (a directory with submodules but no
    ``__init__.py``) — for which each name must be a real submodule, else it fails closed — or a
    genuine unit-caller / not-in-graph module with no children, for which existence alone is kept
    (pre-existing behavior).  This applies ONLY to workspace-local modules, never to stdlib or
    installed distributions, whose exported names we cannot and must not require.
    """

    if not names:
        return True
    source = sources.get(module_dotted)
    if source is None:
        if _module_has_children(module_dotted, ctx):
            return all(f"{module_dotted}.{name}" in ctx.local_dotted for name in names)
        return True
    if not isinstance(source, str) or len(source) > _MAX_SOURCE_LENGTH:
        return False
    try:
        module_tree = ast.parse(source, mode="exec")
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
        return False
    live = _module_scope_live_names(module_tree)
    return all(f"{module_dotted}.{name}" in ctx.local_dotted or name in live for name in names)


def _local_chain_is_proven(
    module_dotted: str,
    ctx: _ImportContext,
    sources: Mapping[str, str],
    visited: frozenset[str],
    depth: int,
    ambient: frozenset[int] = frozenset(),
) -> bool:
    """Prove a workspace-local module is import-safe: its OWN module-scope imports all resolve.

    Existence is not import-safety — ``import pkg`` executes ``pkg``'s module body, so a workspace
    module that itself imports a distribution absent from the install plan is a ``ModuleNotFound``
    at boot even though it exists in the graph.  This recurses into the module's supplied source
    and proves every one of its module-scope imports under the module's own package context,
    bounded by ``_MAX_LOCAL_IMPORT_DEPTH`` and cycle-guarded (a revisited module is a benign
    runtime import cycle and counts as proven).  When no source is supplied for the module (unit
    callers that pass no ``sources`` map), existence alone is accepted so the pre-existing behavior
    is preserved unchanged; every ``detect_release`` caller supplies the full workspace sources.

    ``ambient`` is the set of guard tiers under which THIS module is imported — the exception
    breadths an enclosing ``try`` in the importer will swallow.  A transitive failure of tier T is
    caught by that guard (does not crash the importer) iff ``T in ambient``, so it is tolerated;
    otherwise it escapes and fails closed.  An empty ``ambient`` (an unguarded import) is the
    original full strictness: every transitive import must resolve and no module-scope abort is
    tolerated.  Even a guard covering every CATCHABLE tier (``BaseException`` / bare ``except:``)
    must still descend, because an UNCATCHABLE transitive abort (``os._exit`` / a rebound raise)
    escapes it and fails closed.
    """

    if _resolves_through_ambiguous(module_dotted, ctx):
        return False
    if module_dotted in visited:
        return True
    if depth >= _MAX_LOCAL_IMPORT_DEPTH:
        return False
    source = sources.get(module_dotted)
    if source is None:
        return True
    if not isinstance(source, str) or len(source) > _MAX_SOURCE_LENGTH:
        return False
    try:
        module_tree = ast.parse(source, mode="exec")
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
        return False
    # Import-safety is not just resolvable imports: a dependency that unconditionally aborts at
    # module scope (``raise`` / statically-falsy ``assert`` / ``sys.exit``) crashes its importer
    # UNLESS the enclosing guard catches whatever it raises.  An Exception-class abort (tier
    # EXCEPTION) is swallowed only if EXCEPTION is in ``ambient``; a BASE-class abort (sys.exit /
    # raise SystemExit / a raise not provably an ``Exception``) only if BASE is.  Else it escapes ->
    # fail closed (an unguarded importer has empty ``ambient``, so any abort fails).
    abort_tier = _module_abort_tier(module_tree)
    if abort_tier and abort_tier not in ambient:
        return False
    parts = module_dotted.split(".")
    module_ctx = _ImportContext(
        installed=ctx.installed,
        local_roots=ctx.local_roots,
        local_dotted=ctx.local_dotted,
        package=tuple(parts) if module_dotted in ctx.packages else tuple(parts[:-1]),
        ambiguous=ctx.ambiguous,
        packages=ctx.packages,
    )
    return _scope_imports_are_proven(
        module_tree.body, module_ctx, sources, visited | {module_dotted}, depth + 1, ambient
    )


def _import_is_proven(
    entry: _ScopeImport,
    ctx: _ImportContext,
    sources: Mapping[str, str],
    visited: frozenset[str],
    depth: int,
    swallow: frozenset[int] = frozenset(),
) -> bool:
    """Prove one import resolves for the emitted image: stdlib, install plan, or workspace.

    Absolute imports must be a standard-library/builtin module, a distribution in the exact
    install plan, or an existing workspace-local module path.  Relative imports are resolved
    against the target module's package and must land on an existing workspace module (a
    relative import in a top-level module has no parent package and fails closed).

    A root that is BOTH a workspace module and an installed distribution is a shadow collision
    (see :func:`_root_is_shadow_collision`) and fails closed here: it must never be waved
    through as "installed", because at runtime the workspace file shadows the distribution and
    the certified-as-installed assumption is unsound.  A shadow collision resolves WITHOUT raising
    (it binds the wrong module), so no ``try`` guard can rescue it — it fails closed at every tier.

    ``swallow`` is the set of guard tiers this import runs under (its own enclosing ``try`` guards
    composed with any ambient set threaded from an outer guarded importer).  It decides which
    failures are optional: a missing MODULE (ModuleNotFoundError, tier MODULE) is waived iff MODULE
    is in it; a missing NAME (plain ImportError, tier NAME) iff NAME is in it; and the transitive
    chain is proven with this set as its ambient so a transitive failure it cannot swallow still
    fails closed.
    """

    if entry.level == 0:
        if entry.module is None:
            return False
        if _resolves_through_ambiguous(entry.module, ctx):
            return False
        root = entry.module.split(".", maxsplit=1)[0]
        if (
            root in sys.stdlib_module_names
            or root in sys.builtin_module_names
            or root == "__future__"
        ):
            return True
        if _root_is_shadow_collision(root, ctx):
            return False
        if _normalize_distribution(root) in ctx.installed:
            return True
        if root not in ctx.local_roots or entry.module not in ctx.local_dotted:
            # Module absent from the workspace (and not stdlib/installed): a ModuleNotFoundError is
            # swallowed only when the guard swallows tier MODULE.
            return _TIER_MODULE in swallow
        # Module is a PRESENT workspace module.  A missing from-import name raises a PLAIN
        # ImportError (tier NAME); a ModuleNotFoundError-only guard does NOT catch it, so the
        # name-existence check is waived only when the guard swallows tier NAME.
        if _TIER_NAME not in swallow and not _from_import_names_exist(
            entry.module, entry.names, ctx, sources
        ):
            return False
        return all(
            _local_chain_is_proven(module, ctx, sources, visited, depth, swallow)
            for module in _imported_local_modules(entry.module, entry.names, ctx)
        )
    # A relative import of level L resolves ``L - 1`` packages above the target module's own
    # package, so CPython requires ``L <= len(package)``; otherwise it raises at import time
    # ("attempted relative import with no known parent package" / "beyond top-level package").
    # A top-level module has ``package == ()``, so EVERY relative import there fails closed.
    if entry.level > len(ctx.package):
        return False
    base = ctx.package[: len(ctx.package) - (entry.level - 1)]
    if entry.module is not None:
        resolved = ".".join((*base, *entry.module.split(".")))
        if _resolves_through_ambiguous(resolved, ctx):
            return False
        if resolved not in ctx.local_dotted:
            return _TIER_MODULE in swallow
        if _TIER_NAME not in swallow and not _from_import_names_exist(
            resolved, entry.names, ctx, sources
        ):
            return False
        return all(
            _local_chain_is_proven(module, ctx, sources, visited, depth, swallow)
            for module in _imported_local_modules(resolved, entry.names, ctx)
        )
    if not entry.names:
        return False
    resolved_names = [".".join((*base, name)) for name in entry.names]
    if any(_resolves_through_ambiguous(name, ctx) for name in resolved_names):
        return False
    if not all(name in ctx.local_dotted for name in resolved_names):
        # ``from . import <name>`` binds a SUBMODULE; an absent one raises ModuleNotFoundError, so
        # it is waived only when the guard swallows tier MODULE.
        return _TIER_MODULE in swallow
    return all(
        _local_chain_is_proven(name, ctx, sources, visited, depth, swallow)
        for name in resolved_names
    )


def _scope_imports_are_proven(
    body: list[ast.stmt],
    ctx: _ImportContext,
    sources: Mapping[str, str],
    visited: frozenset[str],
    depth: int,
    ambient: frozenset[int] = frozenset(),
    *,
    runs_as_main: bool | None = None,
) -> bool:
    """Prove every import in a scope's body, each under its effective set of swallowed tiers.

    ``ambient`` is the tier set threaded from an outer guarded importer (empty at the unguarded
    target/module scope).  Composition: a failure of tier T is swallowed if the import's own inner
    guards SWALLOW it, OR they PASS it (neither swallow nor abort) AND the ambient swallows it — so
    the effective set is ``entry.swallow | (ambient - entry.block)`` (an inner ABORT of T vetoes an
    ambient swallow of T, because that ``try`` terminates before the outer guard is reached).

    ``runs_as_main`` is threaded to the visitor's ``if`` reachability so the top-level executed
    module folds ``__name__ == '__main__'`` (``None`` for every non-top-level scope = no fold).
    """
    visitor = _ScopeImportVisitor(runs_as_main)
    for statement in body:
        visitor.visit(statement)
    return all(
        _import_is_proven(
            entry, ctx, sources, visited, depth, entry.swallow | (ambient - entry.block)
        )
        for entry in visitor.imports
    )


def _module_binding_counts(tree: ast.Module) -> Counter[str]:
    visitor = _ModuleBindingVisitor()
    for statement in tree.body:
        visitor.visit(statement)
    return Counter(visitor.names)


def _direct_import_aliases(tree: ast.Module, binding_counts: Counter[str]) -> dict[str, _ImportRef]:
    aliases: dict[str, _ImportRef] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                local = alias.asname or alias.name.split(".", maxsplit=1)[0]
                qualified = alias.name if alias.asname else local
                if binding_counts[local] == 1:
                    aliases[local] = _ImportRef(alias.name.split(".", maxsplit=1)[0], qualified)
        elif isinstance(statement, ast.ImportFrom) and statement.level == 0:
            if statement.module is None:
                continue
            root = statement.module.split(".", maxsplit=1)[0]
            for alias in statement.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                if binding_counts[local] == 1:
                    aliases[local] = _ImportRef(root, f"{statement.module}.{alias.name}")
    return aliases


def _resolved_import(expression: ast.expr, aliases: dict[str, _ImportRef]) -> _ImportRef | None:
    attributes: list[str] = []
    current = expression
    while isinstance(current, ast.Attribute):
        attributes.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name) or current.id not in aliases:
        return None
    base = aliases[current.id]
    suffix = ".".join(reversed(attributes))
    return _ImportRef(base.root, base.qualified + (f".{suffix}" if suffix else ""))


# The empty alias map: an unaliased context (no module scope threaded).  Read-only — never mutated —
# so a single shared instance is safe as a default argument.
_NO_IMPORT_ALIASES: dict[str, _ImportRef] = {}


def _terminating_aliases(tree: ast.Module, binding_counts: Counter[str]) -> dict[str, _ImportRef]:
    """Module-scope name -> import reference, for resolving ALIASED terminating calls.

    Extends :func:`_direct_import_aliases` (``import X as Y`` / ``from X import n as Y``, each bound
    EXACTLY once — a rebound name is not trusted, which fails safe against a stale alias) with a
    single ``Y = <dotted import expression>`` assignment: ``Y`` resolves to whatever its value
    resolves to (``e = os._exit`` -> ``os._exit``).  A name bound more than once, or assigned a
    non-dotted/dynamic value, is left unresolved — the dynamic boundary, so no over-rejection.
    """
    aliases = _direct_import_aliases(tree, binding_counts)
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if binding_counts[target.id] != 1 or target.id in aliases:
            continue
        resolved = _resolved_import(statement.value, aliases)
        if resolved is not None:
            aliases[target.id] = resolved
    return aliases


def _installed_names(installed_packages: frozenset[str]) -> frozenset[str]:
    return frozenset(
        _normalize_distribution(package)
        for package in installed_packages
        if isinstance(package, str) and _DISTRIBUTION_RE.fullmatch(package) is not None
    )


def _module_imports_are_proven(
    tree: ast.Module,
    ctx: _ImportContext,
    sources: Mapping[str, str],
    visited: frozenset[str],
    depth: int,
    *,
    runs_as_main: bool | None = None,
) -> bool:
    """Prove the module-scope imports, and — for the TOP-LEVEL executed module — the imports inside
    every module-scope local function it unconditionally calls.

    ``runs_as_main is None`` is the pre-existing behavior used for every transitively-imported
    workspace/library module and every re-export hop: nested scopes stay opaque (a library's
    functions are not run merely by importing it for an attribute).  The two top-level entrypoints —
    a ``python <script>`` (``runs_as_main=True``) and an entrypoint module imported by uvicorn
    (``runs_as_main=False``) — DO run their module body top-to-bottom, so a module-scope call to a
    local function executes that function's body and its imports really fire.  Those are proven by
    :func:`_called_local_imports_are_proven`; the bool value additionally selects the
    ``__name__ == '__main__'`` fold (see :func:`_guard_truthiness`).
    """
    if not _scope_imports_are_proven(
        tree.body, ctx, sources, visited, depth, runs_as_main=runs_as_main
    ):
        return False
    if runs_as_main is None:
        return True
    return _called_local_imports_are_proven(tree, ctx, sources, runs_as_main=runs_as_main)


def _module_scope_local_functions(
    tree: ast.Module,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Module-scope ``def``/``async def`` bound EXACTLY once, keyed by name.

    A name bound more than once at module scope (a re-``def``, or a ``def`` also shadowed by an
    import/assignment/class of the same name) is not trusted as *the* function a bare ``name()``
    call resolves to, so it is excluded — a call to it is left unattributed (the safe direction:
    never over-attribute an import that may not run).
    """
    binding_counts = _module_binding_counts(tree)
    return {
        statement.name: statement
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        and binding_counts[statement.name] == 1
    }


def _body_bound_names(body: list[ast.stmt]) -> frozenset[str]:
    """Names bound at this scope's own level (nested ``def``/``class``/``lambda`` opaque, per
    :class:`_ModuleBindingVisitor`)."""
    visitor = _ModuleBindingVisitor()
    for statement in body:
        visitor.visit(statement)
    return frozenset(visitor.names)


def _bound_names_of(node: ast.AST) -> frozenset[str]:
    """The names a single target node binds (e.g. a comprehension loop target)."""
    visitor = _ModuleBindingVisitor()
    visitor.visit(node)
    return frozenset(visitor.names)


def _scope_local_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    """Names LOCAL to a function: its parameters plus every name its body binds.

    Python makes a name assigned ANYWHERE in a function local to the whole function, so a module-
    scope function whose name is shadowed by such a local is not the callee a bare call in this body
    resolves to.  Excluding these from the matchable set never over-attributes a call that does not
    actually reach the module function (the sound direction — at worst it under-attributes an exotic
    collision, which only ever leaves a candidate, never over-rejects).
    """
    args = function.args
    params = {argument.arg for argument in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    if args.vararg is not None:
        params.add(args.vararg.arg)
    if args.kwarg is not None:
        params.add(args.kwarg.arg)
    return frozenset(params) | _body_bound_names(function.body)


def _certain_calls_in_expr(
    expr: ast.expr, matchable: frozenset[str], *, runs_as_main: bool | None
) -> list[str]:
    """Module-scope local functions an expression is STATICALLY CERTAIN to call when it evaluates.

    Walks ONLY the sub-expressions that certainly evaluate, so a reported call really runs whenever
    ``expr`` runs.  A ``Call`` whose callee is a bare ``Name`` in ``matchable`` is such a call, and
    the recursion follows Python evaluation order: the callee expression and EVERY argument
    (positional, ``*``-unpacked, keyword, ``**``-unpacked) — arguments evaluate before the call;
    both operands of ``BinOp``/``UnaryOp``; a ``Compare``'s ``left`` and its FIRST comparator only
    (a chained ``a < b < c`` short-circuits, so ``c`` is not certain); a ``BoolOp``'s FIRST value
    only (``and``/``or`` short-circuit the rest); an ``IfExp``'s ``test`` always and its taken arm
    per :func:`_guard_truthiness` (the not-taken/uncertain arm is skipped); an f-string's every
    ``FormattedValue``; a display's elements; a subscript's value and index; an attribute/starred/
    await/yield operand; a walrus value.  A comprehension evaluates its OUTERMOST iterable eagerly
    (recursed, in the enclosing scope) but its element only for an EAGER comprehension (list/set/
    dict, never a lazy generator) with a SINGLE generator over a non-empty static literal and no
    ``if`` filter — the one shape whose element provably runs exactly once — walked in the
    comprehension's own scope (its loop targets excluded).  A ``Lambda`` body/defaults are deferred
    (they run only when the lambda is later called), and a bare ``Name``/``Constant`` calls nothing.
    """

    calls: list[str] = []

    def walk(node: ast.expr, names: frozenset[str]) -> None:
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in names:
                calls.append(node.func.id)
            walk(node.func, names)
            for argument in node.args:
                walk(argument, names)
            for keyword_item in node.keywords:
                walk(keyword_item.value, names)
            return
        if isinstance(node, ast.BoolOp):
            if node.values:  # only the first operand is certain; ``and``/``or`` short-circuit
                walk(node.values[0], names)
            return
        if isinstance(node, ast.IfExp):
            walk(node.test, names)  # a ternary always evaluates its test
            truth = _guard_truthiness(node.test, runs_as_main=runs_as_main)
            if truth is True:
                walk(node.body, names)
            elif truth is False:
                walk(node.orelse, names)
            return
        if isinstance(node, ast.BinOp):
            walk(node.left, names)
            walk(node.right, names)
            return
        if isinstance(node, ast.UnaryOp):
            walk(node.operand, names)
            return
        if isinstance(node, ast.Compare):
            walk(node.left, names)
            if node.comparators:  # a chained compare short-circuits later comparators
                walk(node.comparators[0], names)
            return
        if isinstance(node, (ast.Attribute, ast.Starred, ast.Await, ast.YieldFrom)):
            walk(node.value, names)
            return
        if isinstance(node, ast.Yield):
            if node.value is not None:
                walk(node.value, names)
            return
        if isinstance(node, ast.NamedExpr):
            walk(node.value, names)
            return
        if isinstance(node, ast.Subscript):
            walk(node.value, names)
            walk(node.slice, names)
            return
        if isinstance(node, ast.Slice):
            for part in (node.lower, node.upper, node.step):
                if part is not None:
                    walk(part, names)
            return
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            for element in node.elts:
                walk(element, names)
            return
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=False):
                if key is not None:  # ``**mapping`` has a None key; its value still evaluates
                    walk(key, names)
                walk(value, names)
            return
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                walk(value, names)
            return
        if isinstance(node, ast.FormattedValue):
            walk(node.value, names)
            if node.format_spec is not None:
                walk(node.format_spec, names)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            _walk_comprehension(node, names)
            return
        # ``Constant``, ``Name``, ``Lambda``, and any other node evaluate no certain local call.
        return

    def _walk_comprehension(
        node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp, names: frozenset[str]
    ) -> None:
        generators = node.generators
        if not generators:
            return
        first = generators[0]
        walk(first.iter, names)  # the OUTERMOST iterable is evaluated eagerly, in the outer scope
        eager = isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp))
        if (
            not eager
            or len(generators) != 1
            or first.ifs
            or not _iterable_is_nonempty_literal(first.iter)
        ):
            return  # a lazy generator, multi-clause, filtered, or empty/dynamic element is deferred
        inner = names - _bound_names_of(first.target)  # the loop target shadows in the element
        elements = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        for element in elements:
            walk(element, inner)

    walk(expr, matchable)
    return calls


def _record_statement_calls(
    statement: ast.stmt,
    matchable: frozenset[str],
    runs_as_main: bool | None,
    try_stack: list[ast.Try | ast.TryStar],
    sites: list[tuple[str, frozenset[int], frozenset[int]]],
) -> bool:
    """Append ``statement``'s CERTAIN call sites (each with the enclosing guard tiers); return True
    iff control leaves the sequence (``return``/``break``/``continue``/``raise``) so later
    statements are unreachable.  Only statically-certain-to-execute positions are entered — the same
    reachability the abort detector uses, extended to ``try`` bodies (guard-aware), ``finally``
    bodies, ``else`` bodies whose ``try`` body cannot raise, class bodies, and ``def``/``class``
    decorators and ``def`` default values (all evaluated at def/class-execution time)."""

    swallow, block = _stack_guard_sets(try_stack)

    def record(expr: ast.expr | None) -> None:
        if expr is not None:
            for name in _certain_calls_in_expr(expr, matchable, runs_as_main=runs_as_main):
                sites.append((name, swallow, block))

    def record_name(name: str) -> None:
        # A bare-``Name`` decorator ``@deco`` is the IMPLICIT call ``deco(fn)`` — no ``Call`` node.
        if name in matchable:
            sites.append((name, swallow, block))

    def record_decorators(decorators: list[ast.expr]) -> None:
        for decorator in decorators:
            record(decorator)  # calls INSIDE the decorator expression (e.g. ``@register()``)
            if isinstance(decorator, ast.Name):
                record_name(decorator.id)  # the implicit ``decorator(target)`` application

    def descend(
        inner_body: list[ast.stmt],
        inner_matchable: frozenset[str] = matchable,
        inner_stack: list[ast.Try | ast.TryStar] = try_stack,
    ) -> None:
        for inner in inner_body:
            if _record_statement_calls(inner, inner_matchable, runs_as_main, inner_stack, sites):
                break

    if isinstance(statement, ast.Return):
        record(statement.value)
        return True
    if isinstance(statement, (ast.Break, ast.Continue)):
        return True
    if isinstance(statement, ast.Raise):
        record(statement.exc)
        record(statement.cause)
        return True
    if isinstance(statement, ast.Expr):
        record(statement.value)
        return False
    if isinstance(statement, ast.Assign):
        record(statement.value)
        for target in statement.targets:
            record(target)  # a subscript/attribute target evaluates its own sub-expressions
        return False
    if isinstance(statement, ast.AugAssign):
        record(statement.value)
        record(statement.target)
        return False
    if isinstance(statement, ast.AnnAssign):
        record(statement.value)
        record(statement.target)
        return False
    if isinstance(statement, ast.Delete):
        for target in statement.targets:
            record(target)
        return False
    if isinstance(statement, ast.Assert):
        record(statement.test)  # the test always evaluates; the message only if it is falsy
        if statement.msg is not None and _static_truthiness(statement.test) is False:
            record(statement.msg)
        return False
    if isinstance(statement, ast.If):
        record(statement.test)  # an ``if`` always evaluates its test
        truth = _guard_truthiness(statement.test, runs_as_main=runs_as_main)
        if truth is True:
            descend(statement.body)
        elif truth is False:
            descend(statement.orelse)
        return False
    if isinstance(statement, ast.While):
        record(statement.test)  # evaluated at least once to decide entry
        truth = _static_truthiness(statement.test)
        if truth is True:
            descend(statement.body)
        elif truth is False:
            descend(statement.orelse)
        return False
    if isinstance(statement, (ast.For, ast.AsyncFor)):
        record(statement.iter)  # the iterable is always evaluated to obtain the iterator
        if _iterable_is_nonempty_literal(statement.iter):
            descend(statement.body)
        elif _iterable_is_empty_literal(statement.iter):
            descend(statement.orelse)
        return False
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        for item in statement.items:
            record(item.context_expr)  # each context manager is evaluated
        descend(statement.body)
        return False
    if isinstance(statement, ast.Match):
        record(statement.subject)  # the subject is always evaluated
        index = _static_match_index(statement)
        if index is not None:
            descend(statement.cases[index].body)
        return False
    if isinstance(statement, (ast.Try, ast.TryStar)):
        # The body runs under THIS ``try``'s guard added to the stack; ``finally`` always runs (on
        # the OUTER stack, this ``try`` off); ``else`` runs only when the body provably cannot raise
        # (also outer).  Handlers run only if the body raises — not certain — so they are not run.
        descend(statement.body, matchable, [*try_stack, statement])
        descend(statement.finalbody, matchable, try_stack)
        if statement.orelse and _statements_cannot_raise(statement.body):
            descend(statement.orelse, matchable, try_stack)
        return False
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
        record_decorators(statement.decorator_list)
        for default in statement.args.defaults:  # default values evaluate at def-time
            record(default)
        for default in statement.args.kw_defaults:
            if default is not None:
                record(default)
        return False  # the function BODY is deferred (runs only when the function is called)
    if isinstance(statement, ast.ClassDef):
        record_decorators(statement.decorator_list)
        for base in statement.bases:
            record(base)  # base classes are evaluated when the ``class`` statement executes
        for keyword_item in statement.keywords:
            record(keyword_item.value)  # ``metaclass=``/keyword arguments likewise
        # The class body executes NOW, under the same guard; its own bindings shadow module funcs.
        descend(statement.body, matchable - _body_bound_names(statement.body), try_stack)
        return False
    return False


def _certain_call_sites(
    body: list[ast.stmt], matchable: frozenset[str], *, runs_as_main: bool | None
) -> list[tuple[str, frozenset[int], frozenset[int]]]:
    """Every module-scope local function ``body`` is CERTAIN to call, each tagged with the SWALLOW/
    ABORT guard-tier sets (:func:`_stack_guard_sets`) of the ``try`` bodies enclosing its call site.
    The scan of each statement sequence stops when control provably leaves it."""
    sites: list[tuple[str, frozenset[int], frozenset[int]]] = []
    for statement in body:
        if _record_statement_calls(statement, matchable, runs_as_main, [], sites):
            break
    return sites


def _certain_callee_ambients(
    body: list[ast.stmt],
    matchable: frozenset[str],
    caller_ambient: frozenset[int],
    *,
    runs_as_main: bool | None,
) -> dict[str, frozenset[int]]:
    """Map each module-scope local function ``body`` is CERTAIN to call to the guard-tier set its
    imports effectively run behind: the call site's own ``try`` swallow tiers composed with the
    caller's ambient (``swallow | (ambient - block)`` — an inner ABORT vetoes an ambient swallow,
    exactly as :func:`_scope_imports_are_proven` composes per import), INTERSECTED across every call
    site of that function (a function reached unguarded anywhere gets the smallest set)."""
    effective: dict[str, frozenset[int]] = {}
    for name, swallow, block in _certain_call_sites(body, matchable, runs_as_main=runs_as_main):
        composed = swallow | (caller_ambient - block)
        effective[name] = composed if name not in effective else (effective[name] & composed)
    return effective


def _called_scope_imports_are_proven(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    local_functions: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef],
    ctx: _ImportContext,
    sources: Mapping[str, str],
    visited: frozenset[str],
    depth: int,
    *,
    runs_as_main: bool,
    ambient: frozenset[int] = frozenset(),
) -> bool:
    """Prove one CERTAINLY-called function's own imports (behind the guard tiers it runs under),
    then follow the functions IT is certain to call, threading each callee's composed guard tiers.

    ``ambient`` is the set of guard tiers the callers of THIS function run behind (empty for a
    function called unguarded from module scope).  Its body imports are proven WITH that ambient, so
    a transitive absent import is waived exactly when an enclosing ``try`` swallows it (the A1
    ``try``-body case).  Bounded exactly like the workspace-local import chain
    (:func:`_local_chain_is_proven`): a depth ceiling of ``_MAX_LOCAL_IMPORT_DEPTH`` and a
    visited-set of function names make the call-chain recursion terminate and fail closed on any
    over-deep chain; a revisited function is already proven.  The body's imports are proven by the
    SAME per-scope primitive, so its own ``try/except`` guards, ``if False``/``TYPE_CHECKING`` dead
    branches, and transitive workspace-module resolution behave identically to module scope.  A
    module-scope name shadowed by a local in this body is excluded from attribution
    (:func:`_scope_local_names`); a nested inner ``def``/``class``/``lambda`` body stays opaque (it
    runs only if itself called, handled at that call site).
    """
    if depth >= _MAX_LOCAL_IMPORT_DEPTH:
        return False
    if function.name in visited:
        return True
    if not _scope_imports_are_proven(
        function.body, ctx, sources, frozenset(), 0, ambient, runs_as_main=runs_as_main
    ):
        return False
    onward = visited | {function.name}
    matchable = frozenset(local_functions) - _scope_local_names(function)
    callee_ambients = _certain_callee_ambients(
        function.body, matchable, ambient, runs_as_main=runs_as_main
    )
    return all(
        _called_scope_imports_are_proven(
            local_functions[name],
            local_functions,
            ctx,
            sources,
            onward,
            depth + 1,
            runs_as_main=runs_as_main,
            ambient=callee_ambient,
        )
        for name, callee_ambient in callee_ambients.items()
    )


def _called_local_imports_are_proven(
    tree: ast.Module, ctx: _ImportContext, sources: Mapping[str, str], *, runs_as_main: bool
) -> bool:
    """Prove the imports that fire from the top-level module's CERTAINLY-called local functions.

    Starts from the functions module scope is certain to call — including through a class body, a
    decorator, an argument, an f-string, or a guarded ``try`` — and follows each one's own certain
    calls (:func:`_called_scope_imports_are_proven`), threading the composed guard tiers so a call
    reached only under an ``ImportError``-swallowing ``try`` stays waived while one under a
    non-catching handler is required.  An uncalled function — or one reached only from another
    uncalled function — is never proven, so its imports are not required (reachability boundary).
    """
    local_functions = _module_scope_local_functions(tree)
    if not local_functions:
        return True
    callee_ambients = _certain_callee_ambients(
        tree.body, frozenset(local_functions), frozenset(), runs_as_main=runs_as_main
    )
    return all(
        _called_scope_imports_are_proven(
            local_functions[name],
            local_functions,
            ctx,
            sources,
            frozenset(),
            0,
            runs_as_main=runs_as_main,
            ambient=callee_ambient,
        )
        for name, callee_ambient in callee_ambients.items()
    )


def _direct_target_nodes(tree: ast.Module, attribute: str) -> list[ast.stmt]:
    candidates: list[ast.stmt] = []
    for statement in tree.body:
        direct = False
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            direct = statement.name == attribute
        elif isinstance(statement, ast.Assign):
            target_visitor = _ModuleBindingVisitor()
            for target in statement.targets:
                target_visitor.visit(target)
            direct = attribute in target_visitor.names
        elif isinstance(statement, ast.AnnAssign):
            target_visitor = _ModuleBindingVisitor()
            target_visitor.visit(statement.target)
            direct = attribute in target_visitor.names
        elif isinstance(statement, (ast.Import, ast.ImportFrom)):
            target_visitor = _ModuleBindingVisitor()
            target_visitor.visit(statement)
            direct = attribute in target_visitor.names
        if direct:
            candidates.append(statement)
    return candidates


def _assignment_value(statement: ast.stmt, attribute: str) -> ast.expr | None:
    if isinstance(statement, ast.Assign):
        occurrences = [
            target
            for target in statement.targets
            if isinstance(target, ast.Name) and target.id == attribute
        ]
        if len(occurrences) != 1:
            return None
        for target in statement.targets:
            visitor = _ModuleBindingVisitor()
            visitor.visit(target)
            if attribute in visitor.names and not (
                isinstance(target, ast.Name) and target.id == attribute
            ):
                return None
        return statement.value
    if (
        isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
        and statement.target.id == attribute
    ):
        return statement.value
    return None


def _function_has_exact_signature(
    function: ast.FunctionDef | ast.AsyncFunctionDef, expected: tuple[str, ...]
) -> bool:
    arguments = function.args
    return (
        not function.decorator_list
        and not arguments.posonlyargs
        and tuple(argument.arg for argument in arguments.args) == expected
        and all(argument.annotation is None for argument in arguments.args)
        and arguments.vararg is None
        and arguments.kwarg is None
        and not arguments.kwonlyargs
        and not arguments.defaults
        and not arguments.kw_defaults
        and function.returns is None
        and function.type_comment is None
        and not any(
            isinstance(node, (ast.Lambda, ast.Global, ast.Nonlocal)) for node in ast.walk(function)
        )
        and not any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node is not function
            for node in ast.walk(function)
        )
        and not (
            isinstance(function, ast.AsyncFunctionDef)
            and any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in ast.walk(function))
        )
    )


def _target_scope_is_closed(
    target: ast.AST,
    *,
    tree: ast.Module,
    ctx: _ImportContext,
    sources: Mapping[str, str],
    parameter_names: frozenset[str] = frozenset(),
) -> bool:
    body: list[ast.stmt]
    if isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef)):
        body = list(target.body)
    elif isinstance(target, ast.stmt):
        body = [target]
    else:
        body = []
    if body and not _scope_imports_are_proven(body, ctx, sources, frozenset(), 0):
        return False

    module_bindings = frozenset(_module_binding_counts(tree))
    local_visitor = _ModuleBindingVisitor()
    if isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for statement in target.body:
            local_visitor.visit(statement)
    else:
        local_visitor.visit(target)
    local_bindings = frozenset(local_visitor.names) | parameter_names
    permitted_globals = (
        module_bindings
        | frozenset(dir(builtins))
        | frozenset({"__name__", "__file__", "__package__", "__spec__", "__loader__"})
    )
    for node in ast.walk(target):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in local_bindings and node.id not in permitted_globals:
                return False
    return True


def _import_alias_is_mutated(tree: ast.Module, alias: str) -> bool:
    for statement in tree.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            continue
        for node in ast.walk(statement):
            if isinstance(node, ast.Attribute):
                root = node.value
                while isinstance(root, ast.Attribute):
                    root = root.value
                if (
                    isinstance(root, ast.Name)
                    and root.id == alias
                    and isinstance(node.ctx, (ast.Store, ast.Del))
                ):
                    return True
    return False


def _accepted_constructors(server: str) -> frozenset[tuple[str, str]]:
    if server in {"uvicorn", "hypercorn"}:
        return frozenset(
            {
                ("fastapi", "fastapi.FastAPI"),
                ("fastapi", "fastapi.applications.FastAPI"),
                ("starlette", "starlette.Starlette"),
                ("starlette", "starlette.applications.Starlette"),
            }
        )
    return frozenset({("flask", "flask.Flask")})


def _proven_app_construction(
    value: ast.expr | None,
    *,
    tree: ast.Module,
    binding_counts: Counter[str],
    server: str,
    ctx: _ImportContext,
    sources: Mapping[str, str],
) -> bool:
    """Whether an expression is an installed, unmutated ASGI/WSGI framework construction."""

    if not isinstance(value, ast.Call):
        return False
    if any(isinstance(argument, ast.Starred) for argument in value.args) or any(
        keyword_item.arg is None for keyword_item in value.keywords
    ):
        return False
    if any(
        isinstance(node, (ast.Lambda, ast.NamedExpr, ast.Await, ast.Yield, ast.YieldFrom))
        for node in ast.walk(value)
    ):
        return False
    aliases = _direct_import_aliases(tree, binding_counts)
    resolved = _resolved_import(value.func, aliases)
    if resolved is None:
        return False
    constructor_alias = value.func
    while isinstance(constructor_alias, ast.Attribute):
        constructor_alias = constructor_alias.value
    if not isinstance(constructor_alias, ast.Name) or _import_alias_is_mutated(
        tree, constructor_alias.id
    ):
        return False
    if (resolved.root, resolved.qualified) not in _accepted_constructors(server):
        return False
    if _normalize_distribution(resolved.root) not in ctx.installed:
        return False
    return _target_scope_is_closed(value, tree=tree, ctx=ctx, sources=sources)


def _local_assignment_value(body: list[ast.stmt], name: str) -> ast.expr | None:
    """The single, unambiguous value bound to ``name`` by simple assignment in ``body``."""

    found: ast.expr | None = None
    for statement in body:
        if isinstance(statement, ast.Assign):
            occurrences = [
                target
                for target in statement.targets
                if isinstance(target, ast.Name) and target.id == name
            ]
            if occurrences:
                if found is not None or len(occurrences) != 1 or len(statement.targets) != 1:
                    return None
                found = statement.value
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == name
        ):
            if found is not None:
                return None
            found = statement.value
    return found


def _factory_return_is_proven(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    tree: ast.Module,
    binding_counts: Counter[str],
    server: str,
    ctx: _ImportContext,
    sources: Mapping[str, str],
) -> bool:
    """Whether every ``return`` of a zero-argument factory yields a proven app construction."""

    returns = [node for node in ast.walk(function) if isinstance(node, ast.Return)]
    if not returns or any(node.value is None for node in returns):
        return False
    for node in returns:
        value = node.value
        if isinstance(value, ast.Name):
            value = _local_assignment_value(list(function.body), value.id)
        if not _proven_app_construction(
            value, tree=tree, binding_counts=binding_counts, server=server, ctx=ctx, sources=sources
        ):
            return False
    return True


def _dotted_call_name(func: ast.expr) -> str | None:
    parts: list[str] = []
    current: ast.expr = func
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


# ``sys.exit`` / ``exit`` / ``quit`` raise a CATCHABLE ``SystemExit`` (a ``BaseException``) -> tier
# BASE, so a ``BaseException`` / bare guard swallows them.  ``os._exit`` / ``os.abort`` end the
# process immediately with NO catchable exception -> tier UNCATCHABLE, which NO guard can rescue.
_BASE_TERMINATING_CALLS = frozenset({"exit", "quit", "sys.exit"})
_UNCATCHABLE_TERMINATING_CALLS = frozenset({"os._exit", "os.abort"})


def _resolved_call_name(func: ast.expr, aliases: dict[str, _ImportRef]) -> str | None:
    """The callee's dotted name canonicalized through module-scope import aliases.

    ``_o._exit`` (``import os as _o``) -> ``os._exit``; ``e`` (``e = os._exit``) -> ``os._exit``.
    Falls back to the raw dotted name for an unaliased callee (``exit``/``os._exit`` verbatim), so
    the un-aliased matching that already worked is preserved.
    """
    resolved = _resolved_import(func, aliases)
    return resolved.qualified if resolved is not None else _dotted_call_name(func)


def _is_self_kill_call(call: ast.Call, aliases: dict[str, _ImportRef] = _NO_IMPORT_ALIASES) -> bool:
    """Whether ``call`` is ``os.kill(os.getpid(), ...)`` — an uncatchable self-signal."""
    if _resolved_call_name(call.func, aliases) != "os.kill" or not call.args:
        return False
    first = call.args[0]
    if not isinstance(first, ast.Call):
        return False
    return _resolved_call_name(first.func, aliases) in {"os.getpid", "getpid"}


def _is_hashable_cannot_raise_literal(expr: ast.expr) -> bool:
    """Whether ``expr`` is a HASHABLE literal whose build cannot raise — valid as a set element or
    dict key: a ``Constant``, or a non-``Starred`` ``Tuple`` all of whose elements are themselves
    hashable-cannot-raise (a tuple of hashables is hashable).  A ``List``/``Set``/``Dict`` is
    unhashable, and a ``Name``/``Call``/``BinOp`` is not a provable literal -> ``False``.
    """
    if isinstance(expr, ast.Constant):
        return True
    if isinstance(expr, ast.Tuple):
        return all(
            not isinstance(element, ast.Starred) and _is_hashable_cannot_raise_literal(element)
            for element in expr.elts
        )
    return False


def _set_display_is_foldable(node: ast.Set) -> bool:
    """Whether a ``set`` display both builds without raising AND has a statically-known size.

    True iff every element is a hashable non-raising literal (:func:`_is_hashable_cannot_raise_
    literal`): a ``*`` unpack, an unhashable element (``{[1]}`` -> ``TypeError``), or a raising
    build (``{1 / 0}``) all make the set's construction non-static, so it is NOT foldable.  Shared
    by :func:`_static_truthiness` (fold vs defer) and :func:`_is_cannot_raise_literal` (safe vs may-
    raise) so the two stay in exact lock-step for sets — the if-arm and the guard agree.
    """
    return all(_is_hashable_cannot_raise_literal(element) for element in node.elts)


def _dict_display_is_foldable(node: ast.Dict) -> bool:
    """Whether a ``dict`` display both builds without raising AND has a statically-known size.

    True iff there is no ``**`` unpack (a ``None`` key), every key is a hashable non-raising
    literal, and every value is a non-raising literal (values need not be hashable) — so the build
    cannot ``TypeError`` on an unhashable key nor raise evaluating a key/value (``{1: 1 / 0}`` is
    not foldable).  Shared by :func:`_static_truthiness` and :func:`_is_cannot_raise_literal` so the
    two stay in exact lock-step for dicts.
    """
    return all(
        key is not None and _is_hashable_cannot_raise_literal(key) for key in node.keys
    ) and all(_is_cannot_raise_literal(value) for value in node.values)


def _static_truthiness(node: ast.expr) -> bool | None:
    """The bool of an expression when statically decidable, else ``None`` (needs execution).

    PURE-LITERAL BOUNDARY (the crucial stopping point): this folds ONLY pure-literal expressions and
    MUST NEVER evaluate a ``Call``, ``Name``, or ``Attribute`` — doing so requires executing code,
    which is the documented dynamic boundary (same as ``1 / 0``).  So ``if len([1]): raise``, ``if
    flag: raise``, ``if os.environ.get('X'): raise`` are NOT statically decidable -> ``None`` ->
    left undecided; the resulting candidate stays UNVERIFIED (not a static rejection). Track-1
    candidates are explicitly unverified — no boot/readiness probe confirms them here.

    Decidable shapes: a literal ``Constant`` (its ``bool``); an empty tuple/list/dict/set display
    (``False``) vs a non-empty display (``True``); ``not x`` (inverted truthiness); a short-circuit
    ``and``/``or`` over decidable operands; a subscript of a literal tuple/list display by a const
    integer index in range; and a ``Compare`` all of whose operands are literal constants (e.g.
    ``1 == 1`` -> True, ``1 < 2`` -> True, ``"a" == "a"`` -> True).  Everything else — a name, a
    call, an attribute, a display with ``*``/``**`` unpacking, a non-constant compare operand, a
    comprehension — is ``None`` (undecidable) and is left as an UNVERIFIED candidate.

    One name/attribute exception: ``typing.TYPE_CHECKING`` is ``False`` at runtime (only a static
    type-checker ever sets it True), so an ``if TYPE_CHECKING:`` body is statically dead — the
    universal static-analysis convention every type checker applies.  The bare ``TYPE_CHECKING``
    name (the ``from typing import TYPE_CHECKING`` idiom) and the exact ``typing.TYPE_CHECKING``
    attribute fold to ``False``; any OTHER attribute (``x.TYPE_CHECKING``) or a local rebind of the
    name to a truthy value is not folded (the residual dynamic boundary — never a real pattern).
    """

    if isinstance(node, ast.Name):
        return False if node.id == "TYPE_CHECKING" else None
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "TYPE_CHECKING"
        and isinstance(node.value, ast.Name)
        and node.value.id == "typing"
    ):
        return False
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, (ast.Tuple, ast.List)):
        return (
            None
            if any(isinstance(element, ast.Starred) for element in node.elts)
            else bool(node.elts)
        )
    if isinstance(node, ast.Set):
        # A set folds only when its build is static (hashable, non-raising elements); ``{[1]}`` and
        # ``{1 / 0}`` raise on build, so defer -> the guard (via `_is_cannot_raise_literal`) agrees.
        return bool(node.elts) if _set_display_is_foldable(node) else None
    if isinstance(node, ast.Dict):
        # Likewise a dict folds only when no ``**`` unpack and every key/value builds non-raising;
        # ``{1: 1 / 0}`` and ``{[1]: 0}`` defer, keeping the if-arm and the guard in agreement.
        return bool(node.keys) if _dict_display_is_foldable(node) else None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _static_truthiness(node.operand)
        return None if inner is None else (not inner)
    if isinstance(node, ast.BoolOp):
        return _boolop_truthiness(node)
    if isinstance(node, ast.Subscript):
        return _literal_subscript_truthiness(node)
    if isinstance(node, ast.Compare):
        return _literal_compare_truthiness(node)
    return None


def _is_main_guard_test(test: ast.expr) -> bool | None:
    """How ``test`` compares ``__name__`` to ``'__main__'`` (either operand order), else ``None``.

    ``True`` for the ``==`` form (``__name__ == '__main__'``) and ``False`` for the ``!=`` form
    (``__name__ != '__main__'``); ``None`` when ``test`` is not this guard.  The two forms are exact
    negations, so :func:`_guard_truthiness` folds ``==`` to ``runs_as_main`` and ``!=`` to its
    negation for a module whose execution context is known.
    """
    if (
        not isinstance(test, ast.Compare)
        or len(test.ops) != 1
        or not isinstance(test.ops[0], (ast.Eq, ast.NotEq))
    ):
        return None

    def _is_dunder_name(node: ast.expr) -> bool:
        return isinstance(node, ast.Name) and node.id == "__name__"

    def _is_main_literal(node: ast.expr) -> bool:
        return isinstance(node, ast.Constant) and node.value == "__main__"

    left, right = test.left, test.comparators[0]
    if (_is_dunder_name(left) and _is_main_literal(right)) or (
        _is_main_literal(left) and _is_dunder_name(right)
    ):
        return isinstance(test.ops[0], ast.Eq)
    return None


def _guard_truthiness(test: ast.expr, *, runs_as_main: bool | None) -> bool | None:
    """:func:`_static_truthiness` plus the ``__name__ ==/!= '__main__'`` fold for the top module.

    The context-free :func:`_static_truthiness` cannot decide ``__name__ == '__main__'``
    (``__name__`` is a name, not a literal) so it returns ``None`` and the ``if __main__:`` block is
    treated as reachable — the pre-existing behavior that ``runs_as_main is None`` preserves.
    When the caller KNOWS the module's execution context, that comparison folds: a ``python
    <script>`` runs as ``__main__`` (``runs_as_main=True`` -> the block is reachable, so a script's
    ``main()`` call in it is unconditional) while an entrypoint IMPORTED by uvicorn is not
    (``runs_as_main=False`` -> the block is dead, so its dev-only imports never run on import).  The
    inverted ``__name__ != '__main__'`` guard (whose ``else`` holds a script's ``main()`` call)
    folds to the negation, so its live branch is decided identically.
    """
    if runs_as_main is not None:
        form = _is_main_guard_test(test)
        if form is True:  # ``__name__ == '__main__'``
            return runs_as_main
        if form is False:  # ``__name__ != '__main__'`` — the exact negation
            return not runs_as_main
    return _static_truthiness(test)


# Foldable comparison operators.  ``Is``/``IsNot``/``In``/``NotIn`` are absent (identity and
# containment are not decidable from literal text), so a Compare using them folds to None.
_COMPARE_OPS: dict[type[ast.cmpop], Callable[..., object]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


def _literal_compare_truthiness(node: ast.Compare) -> bool | None:
    """Fold a ``Compare`` whose operands are ALL literal constants (Eq/NotEq/ordering only).

    ``Is``/``IsNot``/``In``/``NotIn`` and any non-``Constant`` operand (name/call/attribute/display)
    are undecidable -> ``None``.  A comparison that would raise at runtime (e.g. ``1 < "a"``) is
    likewise ``None`` (deferred), never a static verdict.
    """
    operands = [node.left, *node.comparators]
    if any(not isinstance(operand, ast.Constant) for operand in operands):
        return None
    values = [operand.value for operand in operands if isinstance(operand, ast.Constant)]
    result = True
    for index, op in enumerate(node.ops):
        func = _COMPARE_OPS.get(type(op))
        if func is None:  # Is / IsNot / In / NotIn — not statically foldable here
            return None
        try:
            if not func(values[index], values[index + 1]):
                result = False
        except TypeError:  # e.g. ``1 < "a"`` — undecidable, defer
            return None
    return result


def _iterable_is_nonempty_literal(node: ast.expr) -> bool:
    """Whether ``node`` is a statically NON-EMPTY literal iterable (so a ``for`` body runs >= once).

    Only pure-literal displays and non-empty constant str/bytes count; a ``*``/``**`` unpack or any
    name/call/comprehension is undecidable and returns False (the loop body is not certain to run).
    """
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return bool(node.elts) and not any(isinstance(e, ast.Starred) for e in node.elts)
    if isinstance(node, ast.Dict):
        return bool(node.keys) and all(key is not None for key in node.keys)
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        return len(node.value) > 0
    return False


def _iterable_is_empty_literal(node: ast.expr) -> bool:
    """Whether ``node`` is a statically EMPTY literal iterable (so a ``for`` body never runs).

    Mirrors :func:`_iterable_is_nonempty_literal`: an empty list/tuple/set/dict display or an empty
    constant str/bytes.  Any name/call/comprehension is undecidable and returns False (the body may
    still run).  When empty, the loop skips straight to its ``else`` without executing the body.
    """
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return not node.elts
    if isinstance(node, ast.Dict):
        return not node.keys
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        return len(node.value) == 0
    return False


def _boolop_truthiness(node: ast.BoolOp) -> bool | None:
    values = [_static_truthiness(operand) for operand in node.values]
    if isinstance(node.op, ast.And):
        if any(value is False for value in values):
            return False
        return True if all(value is True for value in values) else None
    if any(value is True for value in values):
        return True
    return False if all(value is False for value in values) else None


def _literal_subscript_truthiness(node: ast.Subscript) -> bool | None:
    if not isinstance(node.value, (ast.Tuple, ast.List)):
        return None
    elements = node.value.elts
    if any(isinstance(element, ast.Starred) for element in elements):
        return None
    index = node.slice
    if (
        not isinstance(index, ast.Constant)
        or isinstance(index.value, bool)
        or not isinstance(index.value, int)
    ):
        return None
    position = index.value
    if -len(elements) <= position < len(elements):
        return _static_truthiness(elements[position])
    return None


# Every builtin whose class is an exception, mapped to the GUARD TIER a ``raise`` of it needs to be
# swallowed: EXCEPTION(3) for an ``Exception`` subclass (ValueError, RuntimeError, AssertionError…),
# BASE(4) for a ``BaseException`` that is NOT an ``Exception`` (SystemExit, KeyboardInterrupt,
# GeneratorExit, BaseException).  Derived from the live class hierarchy — not a hand-kept list.
_BUILTIN_EXCEPTION_TIER: dict[str, int] = {
    name: (_TIER_EXCEPTION if issubclass(obj, Exception) else _TIER_BASE)
    for name in dir(builtins)
    if isinstance(obj := getattr(builtins, name), type) and issubclass(obj, BaseException)
}


def _module_scope_assigned_names(tree: ast.Module) -> frozenset[str]:
    """Every name ASSIGNED / bound at module scope (Store, import, def, class).

    Used to detect a builtin exception name rebound at module scope (``ValueError = SystemExit``):
    once rebound, a ``raise <Name>`` can no longer be trusted to be the builtin, so its tier is
    unknowable.  Reuses the module-scope binding walker (never descends functions/classes).
    """
    visitor = _ModuleScopeLiveVisitor()
    for statement in tree.body:
        visitor.visit(statement)
    return frozenset(visitor.bound)


def _raise_abort_tier(node: ast.Raise, rebound: frozenset[str]) -> int:
    """The min guard tier that swallows a ``raise`` statement's exception.

    A bare ``raise`` at module scope has no active exception, so it raises ``RuntimeError`` — an
    ``Exception`` (tier 3).  A ``raise Name`` / ``raise Name(...)`` of an UN-rebound builtin
    exception is classified by the live hierarchy.  If the name is REBOUND at module scope its real
    type is unknowable (``ValueError = SystemExit`` -> SystemExit escapes ``except Exception``), so
    it is UNCATCHABLE — fail closed.  Any other raise (an attribute ``pkg.Err``, a non-name call
    target) cannot be proven within ``Exception`` and is conservatively BASE(4).
    """
    exc = node.exc
    if exc is None:
        return _TIER_EXCEPTION
    func = exc.func if isinstance(exc, ast.Call) else exc
    if isinstance(func, ast.Name):
        if func.id in rebound:
            return _TIER_UNCATCHABLE
        tier = _BUILTIN_EXCEPTION_TIER.get(func.id)
        if tier is not None:
            return tier
    return _TIER_BASE


def _call_abort_tier(call: ast.Call, aliases: dict[str, _ImportRef] = _NO_IMPORT_ALIASES) -> int:
    """The tier of a process-terminating call expression, or 0 if it is not one.

    ``os._exit`` / ``os.abort`` / ``os.kill(getpid, …)`` terminate with no catchable exception ->
    UNCATCHABLE; ``sys.exit`` / ``exit`` / ``quit`` raise a catchable ``SystemExit`` -> BASE.  The
    callee is first canonicalized through module-scope import aliases (:func:`_resolved_call_name`),
    so ``import os as _o; _o._exit(4)`` and ``from sys import exit as _e; _e(1)`` are caught just
    like their unaliased spellings; a genuinely dynamic callee (``getattr(os, '_exit')()``, a
    conditionally reassigned name) stays unresolved -> tier 0, the documented dynamic boundary.
    """
    name = _resolved_call_name(call.func, aliases)
    if name in _UNCATCHABLE_TERMINATING_CALLS or _is_self_kill_call(call, aliases):
        return _TIER_UNCATCHABLE
    if name in _BASE_TERMINATING_CALLS:
        return _TIER_BASE
    return _TIER_NONE


def _literal_value(expr: ast.expr) -> tuple[bool, object]:
    """The constant VALUE of a pure-literal expression, as ``(is_literal, value)``.

    Folds ONLY an ``ast.Constant`` (its ``.value``); a ``Name``/``Call``/``Attribute`` or any
    display is the dynamic boundary (evaluating it runs code), so it returns ``(False, None)`` and
    the caller leaves it an UNVERIFIED candidate.  Distinct from :func:`_static_truthiness` (which
    yields a *bool*): this yields the value itself so a ``match`` value/singleton pattern can be
    compared against it without ever executing code.
    """
    if isinstance(expr, ast.Constant):
        return True, expr.value
    return False, None


def _pattern_static_match(pattern: ast.pattern, value: object) -> bool | None:
    """Whether a ``case`` pattern statically matches literal subject ``value`` (``None`` = defer).

    Pure-literal only, mirroring the ``match`` runtime: a capture/wildcard (``case _`` / ``case x``)
    always matches; a value pattern ``case <const>`` matches iff ``const == value`` (the runtime
    uses ``==``); a singleton ``case True/False/None`` matches by identity; an OR-pattern matches if
    any alternative matches and fails only if all alternatives fail.  A value pattern over a
    non-constant (``case pkg.OK``) or a sequence/mapping/class/star pattern needs code evaluation or
    structural matching -> ``None`` (deferred), never a static verdict.
    """
    if isinstance(pattern, ast.MatchAs):
        if pattern.pattern is None:  # ``case _`` / ``case name`` — an irrefutable capture
            return True
        return _pattern_static_match(pattern.pattern, value)
    if isinstance(pattern, ast.MatchOr):
        results = [_pattern_static_match(alt, value) for alt in pattern.patterns]
        if any(result is True for result in results):
            return True
        return False if all(result is False for result in results) else None
    if isinstance(pattern, ast.MatchValue):
        if isinstance(pattern.value, ast.Constant):
            return bool(pattern.value.value == value)
        return None
    if isinstance(pattern, ast.MatchSingleton):
        return pattern.value is value
    return None  # MatchSequence / MatchMapping / MatchClass / MatchStar — structural, deferred


def _match_outcome(node: ast.Match) -> tuple[int | None, list[list[ast.stmt]], bool]:
    """Resolve a ``match`` over a pure-literal subject to ``(taken_index, live_bodies, resolved)``.

    Walks the cases IN ORDER, folding each pattern (:func:`_pattern_static_match`) and — when the
    pattern matches — its guard via :func:`_nonraising_truth` (NOT :func:`_static_truthiness`, which
    would fold a short-circuiting ``x and []`` guard to a verdict even though evaluating ``x`` first
    may raise).  Three fully-folded outcomes plus one dynamic:

    * a provably-taken case (pattern matches, guard absent or literally true) -> ``(i, [that body],
      True)`` — only case ``i`` runs;
    * every case provably skipped (pattern fails, or matches with a literally-false guard) ->
      ``(None, [], True)`` — the ``match`` runs no case and, touching only literals, cannot raise;
    * the first undecidable pattern/guard (or a non-literal subject) -> ``(None, [every case body
      from there on], False)`` — the conservative reachable set; ``resolved`` False marks that a
      dynamic subexpression is in play, so no "cannot raise" claim may rest on it.
    """
    is_literal, value = _literal_value(node.subject)
    if not is_literal:
        return None, [case.body for case in node.cases], False
    for index, case in enumerate(node.cases):
        matched = _pattern_static_match(case.pattern, value)
        if matched is None:
            return None, [case.body for case in node.cases[index:]], False
        if matched:
            guard = True if case.guard is None else _nonraising_truth(case.guard)
            if guard is True:
                return index, [case.body], True
            if guard is None:
                return None, [case.body for case in node.cases[index:]], False
            # guard is False: this case is skipped at runtime, keep scanning later cases
    return None, [], True


def _static_match_index(node: ast.Match) -> int | None:
    """The index of the ``case`` that PROVABLY executes for a literal subject, else ``None``.

    Thin view over :func:`_match_outcome`: a provably-taken case index, or ``None`` when the
    ``match`` provably runs NO case OR its taken case cannot be proven (both leave the abort
    undecided, so the abort detector defers).  Pattern AND guard are folded via pure literals.
    """
    return _match_outcome(node)[0]


def _contains_break(body: list[ast.stmt]) -> bool:
    """Whether a bare ``break`` reachable at THIS loop level appears anywhere in ``body``.

    Recurses into ``if``/``with``/``try``/``match`` bodies (a ``break`` there still targets this
    loop) but STOPS at a nested ``for``/``while`` (its ``break`` binds to that inner loop) and at a
    nested ``def``/``async def``/``class`` (a new scope).  Used to tell whether a ``for`` body can
    exit WITHOUT running its ``else`` — if it can, the ``else`` is not certain to run.

    Reachability is folded the SAME way the abort detector folds it, so a statically-DEAD ``break``
    (in an ``if False:`` branch, a non-matching ``case``, or a handler for a body that cannot raise)
    is NOT counted — otherwise it would spuriously block a sound ``for``-``else`` abort claim.
    """
    return any(_stmt_contains_break(statement) for statement in body)


def _stmt_contains_break(statement: ast.stmt) -> bool:
    if isinstance(statement, ast.Break):
        return True
    if isinstance(
        statement,
        (
            ast.For,
            ast.AsyncFor,
            ast.While,
            ast.FunctionDef,
            ast.AsyncFunctionDef,
            ast.ClassDef,
        ),
    ):
        return False  # a nested loop owns its own ``break``; a nested scope cannot ``break`` ours
    if isinstance(statement, ast.If):
        # Fold the test: a statically-dead branch never runs, so its ``break`` can never fire.  A
        # dynamic test recurses BOTH branches (either might run).  ``_static_truthiness`` is sound
        # here — a False fold means the branch does not execute, regardless of any raise it hides.
        truth = _static_truthiness(statement.test)
        if truth is True:
            return _contains_break(statement.body)
        if truth is False:
            return _contains_break(statement.orelse)
        return _contains_break(statement.body) or _contains_break(statement.orelse)
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return _contains_break(statement.body)
    if isinstance(statement, (ast.Try, ast.TryStar)):
        # body/else/finally are (conservatively) reachable; a handler runs ONLY if the body can
        # raise, so when the body provably cannot raise the handlers are DEAD and their ``break``s
        # can never fire.
        if (
            _contains_break(statement.body)
            or _contains_break(statement.orelse)
            or _contains_break(statement.finalbody)
        ):
            return True
        if _statements_cannot_raise(statement.body):
            return False
        return any(_contains_break(handler.body) for handler in statement.handlers)
    if isinstance(statement, ast.Match):
        # Only the case bodies that can actually be TAKEN carry a reachable ``break``.
        _, live_bodies, _ = _match_outcome(statement)
        return any(_contains_break(body) for body in live_bodies)
    return False


def _is_cannot_raise_literal(expr: ast.expr) -> bool:
    """Whether EVALUATING ``expr`` provably cannot raise.

    Provably-safe: a ``Constant``; a list/tuple of such recursively (so ``[1 / 0]`` is rejected —
    its element raises on build); a set/dict whose elements/keys are HASHABLE non-raising literals
    (a ``Constant`` or a hashable tuple like ``(1, 2)``, so the build cannot ``TypeError``) with
    recursively-safe dict values; a boolean ``not`` of such; a
    ``Compare`` of constants that :func:`_static_truthiness` folds without a ``TypeError``
    (``1 == 1`` yes, ``1 < 'a'`` no); a ``BoolOp`` each of whose EVALUATED operands (left to right,
    up to a proven short-circuit) is safe; and a ``Subscript`` of such a literal tuple/list by a
    constant ``int`` (non-``bool``) index that is IN RANGE (mirrors :func:`_literal_subscript_
    truthiness`, plus the container itself must be safe to build).

    May raise -> ``False``: a ``BinOp``/non-``Not`` ``UnaryOp`` (``1 / 0``, ``~'a'``), an
    out-of-range or non-constant-index ``Subscript`` (``(1,)[5]``, ``(9,)[-1]``), a ``Name``
    (``NameError``), a ``Call``, comprehension, ``await``/``yield``, walrus or f-string.

    This is the SINGLE predicate behind every "cannot raise" / non-raising-guard decision, kept in
    lock-step with :func:`_static_truthiness`'s foldable arms restricted to the non-raising subset.
    """
    if isinstance(expr, ast.Constant):
        return True
    if isinstance(expr, (ast.List, ast.Tuple)):
        return all(
            not isinstance(element, ast.Starred) and _is_cannot_raise_literal(element)
            for element in expr.elts
        )
    if isinstance(expr, ast.Set):
        # Lock-step with `_static_truthiness`: safe iff every element is a hashable non-raising
        # literal (a hashable tuple like (1, 2) qualifies; {[1]} and {1 / 0} do not).
        return _set_display_is_foldable(expr)
    if isinstance(expr, ast.Dict):
        # Lock-step likewise: hashable non-raising keys (``{(): 0}`` qualifies) and non-raising
        # values, no ``**`` unpack.
        return _dict_display_is_foldable(expr)
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
        return _is_cannot_raise_literal(expr.operand)  # ``not X`` = negate bool(X); bool() is safe
    if isinstance(expr, ast.Compare):
        # `_static_truthiness` is non-None IFF all operands are constants AND the comparison folds
        # without a TypeError — exactly "evaluating this comparison cannot raise".
        return _static_truthiness(expr) is not None
    if isinstance(expr, ast.BoolOp):
        # Operands evaluate left-to-right, short-circuiting; every operand that CAN run must itself
        # be non-raising.  Stop at a PROVEN short-circuit (``and`` on a false operand / ``or`` on a
        # true one) — later operands are then dead, so their raises never happen.
        for operand in expr.values:
            if not _is_cannot_raise_literal(operand):
                return False
            truth = _static_truthiness(operand)
            if (isinstance(expr.op, ast.And) and truth is False) or (
                isinstance(expr.op, ast.Or) and truth is True
            ):
                return True
        return True
    if isinstance(expr, ast.Subscript):
        # Mirror `_literal_subscript_truthiness`: a constant int (not bool) index, IN RANGE, into a
        # literal tuple/list whose FULL build cannot raise (``(1 / 0,)`` is rejected) -> the
        # indexing provably cannot raise (container builds, index in range).
        if not isinstance(expr.value, (ast.List, ast.Tuple)) or not _is_cannot_raise_literal(
            expr.value
        ):
            return False
        index = expr.slice
        if (
            not isinstance(index, ast.Constant)
            or isinstance(index.value, bool)
            or not isinstance(index.value, int)
        ):
            return False
        return -len(expr.value.elts) <= index.value < len(expr.value.elts)
    return False


def _nonraising_truth(expr: ast.expr) -> bool | None:
    """The truthiness of an expression that :func:`_is_cannot_raise_literal` proves cannot raise.

    Stricter than :func:`_static_truthiness`, whose value alone does NOT imply non-raising: a
    display truthiness ignores a raising element (``[1 / 0]`` folds True yet raises on build), and a
    ``BoolOp`` fold ignores left-to-right order (``(1 < 'a') and False`` folds False yet the first
    operand raises).  By gating on the recursive :func:`_is_cannot_raise_literal`, this folds the
    provably-non-raising subset — constants, safe displays, ``not``/``Compare``/``BoolOp`` of
    literals, and in-range literal subscripts — and returns ``None`` for anything that may raise (a
    ``Name``/``Call``, a raising operator, or a short-circuit over a dynamic operand like
    ``x and []``).  Used where the fold underwrites a POSITIVE "cannot raise" / "dead branch" claim.
    """
    if _is_cannot_raise_literal(expr):
        return _static_truthiness(expr)
    return None


def _statement_cannot_raise(statement: ast.stmt) -> bool:
    """Whether executing ``statement`` provably cannot raise (so a following ``else`` is reached).

    ``pass``; an assignment to plain ``Name`` target(s) of a cannot-raise literal; an annotated
    assignment to a ``Name`` whose ANNOTATION and value are BOTH cannot-raise literals (a
    name/attribute annotation is evaluated at module/class scope and can raise, so it is excluded);
    an expression statement of a cannot-raise literal; an ``if`` whose test folds via
    :func:`_nonraising_truth` and whose LIVE branch cannot raise (a dead branch cannot raise; a
    dynamic test is a name/call that may itself raise -> ``False``); or a ``match`` whose
    :func:`_match_outcome` is fully resolved and whose taken case body (or no-case fall-through)
    cannot raise.  Anything else — a call, an import, a ``raise``, a ``for``/``while``/``try`` (loop
    completion / exception routing is not proven here), a name load — may raise -> ``False``.
    """
    if isinstance(statement, ast.Pass):
        return True
    if isinstance(statement, ast.Assign):
        return all(isinstance(target, ast.Name) for target in statement.targets) and (
            _is_cannot_raise_literal(statement.value)
        )
    if isinstance(statement, ast.AnnAssign):
        return (
            isinstance(statement.target, ast.Name)
            and _is_cannot_raise_literal(statement.annotation)
            and statement.value is not None
            and _is_cannot_raise_literal(statement.value)
        )
    if isinstance(statement, ast.Expr):
        return _is_cannot_raise_literal(statement.value)
    if isinstance(statement, ast.If):
        truth = _nonraising_truth(statement.test)
        if truth is True:
            return _statements_cannot_raise(statement.body)
        if truth is False:
            return _statements_cannot_raise(statement.orelse)
        return False  # dynamic test: evaluating a name/call test can itself raise
    if isinstance(statement, ast.Match):
        taken_index, _, resolved = _match_outcome(statement)
        if not resolved:  # a dynamic pattern/guard (or non-literal subject) may itself raise
            return False
        if taken_index is None:  # provably no case runs; only literals were evaluated
            return True
        return _statements_cannot_raise(statement.cases[taken_index].body)
    return False


def _statements_cannot_raise(body: list[ast.stmt]) -> bool:
    """Whether EVERY statement in ``body`` provably cannot raise (empty body vacuously cannot)."""
    return all(_statement_cannot_raise(statement) for statement in body)


def _for_abort_tier(
    node: ast.For | ast.AsyncFor,
    rebound: frozenset[str],
    aliases: dict[str, _ImportRef] = _NO_IMPORT_ALIASES,
) -> int:
    """The tier at which a ``for`` over a STATIC literal iterable unconditionally aborts (0 if not).

    A statically NON-EMPTY literal runs the body at least once, so an unconditional body abort on
    the first iteration is the abort.  Otherwise, if the body cannot ``break`` out (a ``break``
    would skip the ``else``), the loop is certain to reach its ``else``, so an ``else`` abort is
    unconditional.  A statically EMPTY literal never runs the body, so only the ``else`` runs.
    A non-literal iterable (a name/call/comprehension) is the dynamic boundary -> 0.

    SOUNDNESS NOTE: when the body neither unconditionally aborts nor breaks yet may *conditionally*
    abort (e.g. ``if flag: os._exit()``), the whole loop still unconditionally aborts (either the
    body's conditional abort fires, or the body completes and the ``else`` aborts), so returning a
    non-zero tier is correct for the boolean entrypoint check.  The reported tier is the ``else``'s;
    the only residual imprecision is that a *guarded* dependency importing such a module could see a
    tier lower than a higher-tier conditional body abort (e.g. an uncatchable ``os._exit`` behind a
    dynamic condition).  That shape does not occur in real code and its conditional (dynamic) branch
    is not statically decided (the candidate stays UNVERIFIED), so it is not over-rejected here.
    """
    if _iterable_is_nonempty_literal(node.iter):
        body_tier = _statements_abort_tier(node.body, rebound, aliases)
        if body_tier:
            return body_tier
        if not _contains_break(node.body):
            return _statements_abort_tier(node.orelse, rebound, aliases)
        return _TIER_NONE
    if _iterable_is_empty_literal(node.iter):
        return _statements_abort_tier(node.orelse, rebound, aliases)
    return _TIER_NONE


def _statement_abort_tier(
    statement: ast.stmt,
    rebound: frozenset[str],
    aliases: dict[str, _ImportRef] = _NO_IMPORT_ALIASES,
) -> int:
    """The min guard tier that swallows ``statement`` if it unconditionally aborts, else 0.

    A ``raise`` (see :func:`_raise_abort_tier`), a statically-False ``assert`` (``AssertionError`` —
    an ``Exception``, tier 3), or a process-terminating call (see :func:`_call_abort_tier`) aborts
    directly.  Compound statements are recursed into ONLY where entry is statically CERTAIN — an
    ``if``/``while`` whose test folds via pure-literal :func:`_static_truthiness`, a ``for`` over
    a statically non-empty literal, a ``with`` (body always runs), and a ``try`` (see
    :func:`_try_abort_tier`).  A test/iterable that requires evaluating a name/call/attribute is NOT
    certain -> not recursed (left an UNVERIFIED candidate, the documented dynamic boundary).
    """
    if isinstance(statement, ast.Raise):
        return _raise_abort_tier(statement, rebound)
    if isinstance(statement, ast.Assert) and _static_truthiness(statement.test) is False:
        return _TIER_EXCEPTION
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
        return _call_abort_tier(statement.value, aliases)
    if isinstance(statement, ast.If):
        truth = _static_truthiness(statement.test)
        if truth is True:
            return _statements_abort_tier(statement.body, rebound, aliases)
        if truth is False:
            return _statements_abort_tier(statement.orelse, rebound, aliases)
        return _TIER_NONE
    if isinstance(statement, ast.While):
        truth = _static_truthiness(statement.test)
        if truth is True:
            return _statements_abort_tier(statement.body, rebound, aliases)
        if truth is False:  # the body never runs, so the ``else`` always runs
            return _statements_abort_tier(statement.orelse, rebound, aliases)
        return _TIER_NONE
    if isinstance(statement, (ast.For, ast.AsyncFor)):
        return _for_abort_tier(statement, rebound, aliases)
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return _statements_abort_tier(statement.body, rebound, aliases)
    if isinstance(statement, (ast.Try, ast.TryStar)):
        return _try_abort_tier(statement, rebound, aliases)
    if isinstance(statement, ast.Match):
        index = _static_match_index(statement)
        if index is None:  # subject not literal, or no case provably taken -> dynamic boundary
            return _TIER_NONE
        return _statements_abort_tier(statement.cases[index].body, rebound, aliases)
    return _TIER_NONE


def _try_abort_tier(
    node: ast.Try | ast.TryStar,
    rebound: frozenset[str],
    aliases: dict[str, _ImportRef] = _NO_IMPORT_ALIASES,
) -> int:
    """The tier at which a ``try`` / ``try*`` statement unconditionally aborts (0 if it may finish).

    The ``finally`` body always runs last, so an abort there is unconditional and supersedes.
    Otherwise the body runs: an abort there propagates UNLESS one of this statement's own handlers
    swallows it (first-matching CPython routing) — a matching handler that itself aborts propagates
    its OWN tier (``except V: os._exit()`` -> UNCATCHABLE), a matching clean handler swallows it (no
    abort), and if no handler matches it escapes at its original tier.

    ``try*`` (``ast.TryStar``, ``except*``) shares this shape SOUNDLY for the single unconditional
    abort modelled here: ``except*`` handlers are the same ``ast.ExceptHandler`` nodes and match the
    same types (:func:`_handler_catches_tier`), so a lone abort is caught iff a regular ``except``
    of that type would catch it — the ``ExceptionGroup`` wrapping changes DELIVERY, not whether the
    exception is caught.  ``except*`` cannot be bare, so there is no catch-all handler to model.
    """
    finally_tier = _statements_abort_tier(node.finalbody, rebound, aliases)
    if finally_tier:
        return finally_tier
    body_tier = _statements_abort_tier(node.body, rebound, aliases)
    if not body_tier:
        # The body did not unconditionally abort.  A ``try`` ``else`` runs ONLY when the body
        # completes without raising (a raise routes to a handler and skips the ``else``); so an
        # ``else`` abort is unconditional exactly when the body provably cannot raise.
        if node.orelse and _statements_cannot_raise(node.body):
            return _statements_abort_tier(node.orelse, rebound, aliases)
        return _TIER_NONE
    for handler in node.handlers:
        if _handler_catches_tier(handler, body_tier):
            return _statements_abort_tier(handler.body, rebound, aliases)
    return body_tier


def _statements_abort_tier(
    body: list[ast.stmt],
    rebound: frozenset[str] = frozenset(),
    aliases: dict[str, _ImportRef] = _NO_IMPORT_ALIASES,
) -> int:
    """The min guard tier that swallows the FIRST unconditional abort in ``body`` (0 if none).

    Source order matters: the first unconditionally-aborting statement is the one that actually
    executes, so its tier is the binding constraint.  A ``break``/``continue``/``return`` transfers
    control out of this sequence before any later statement, so it stops the scan with no abort
    (this is how a leading ``break`` in a ``while True`` body correctly means "no unconditional
    abort").
    """
    for statement in body:
        if isinstance(statement, (ast.Break, ast.Continue, ast.Return)):
            return _TIER_NONE
        tier = _statement_abort_tier(statement, rebound, aliases)
        if tier:
            return tier
    return _TIER_NONE


def _statements_unconditionally_abort(body: list[ast.stmt]) -> bool:
    """Whether a statement list unconditionally aborts on execution (any tier)."""

    return _statements_abort_tier(body) != _TIER_NONE


def _module_unconditionally_aborts(tree: ast.Module) -> bool:
    """Whether any top-level statement unconditionally aborts import (before OR after binding)."""

    return _module_abort_tier(tree) != _TIER_NONE


def _module_abort_tier(tree: ast.Module) -> int:
    """The min guard tier that swallows the module's first unconditional module-scope abort (0=off).

    Used in the guarded TRANSITIVE chain: a dependency imported under an ambient tier set is
    abort-safe iff its abort tier is IN that set (the guard catches whatever the abort raises).  An
    UNCATCHABLE abort is in no set, so it fails closed under every guard; an unguarded importer
    passes the empty set, so any abort (tier >= EXCEPTION) still fails closed.
    """

    aliases = _terminating_aliases(tree, _module_binding_counts(tree))
    return _statements_abort_tier(tree.body, _module_scope_assigned_names(tree), aliases)


# A re-exported target may itself re-export; a hard hop ceiling plus a visited-set of resolved
# module names make the recursion terminate and fail closed on any cycle or unbounded chain.
_MAX_REEXPORT_DEPTH = 5


def _resolve_imported_module(
    level: int, module: str | None, package: tuple[str, ...]
) -> str | None:
    """Resolve one ``from ... import`` clause to an absolute dotted workspace module name.

    Mirrors :func:`_import_is_proven`'s level arithmetic exactly: an absolute clause
    (``level == 0``) is the named module verbatim; a relative clause resolves ``level - 1``
    packages above the importing module's own package.  It fails closed (``None``) when the
    clause names no module (``from . import sub`` binds a *module*, never an app attribute) or
    the level has no parent package to climb to — a top-level module (``package == ()``) or a
    level beyond the package root, both of which raise at import time.
    """

    if module is None:
        return None
    if level == 0:
        return module
    if level > len(package):
        return None
    base = package[: len(package) - (level - 1)]
    return ".".join((*base, *module.split(".")))


def _reexport_origin(target: ast.ImportFrom, attribute: str) -> str | None:
    """The exact source name re-exported as ``attribute`` (``None`` for a ``*`` import)."""

    for alias in target.names:
        if alias.name == "*":
            return None
        if (alias.asname or alias.name) == attribute:
            return alias.name
    return None


def _prove_ancestor_packages(
    module_dotted: str, ctx: _ImportContext, sources: Mapping[str, str]
) -> str | None:
    """Prove every ancestor package ``__init__`` a dotted target's import fires runs cleanly.

    CPython imports each ancestor package's ``__init__`` before the submodule, so ``pkg.mod`` runs
    ``pkg/__init__.py`` first: an ancestor that unconditionally aborts (``raise``) or imports an
    unproven dependency makes the target unreachable even though its own source is a valid app.
    For each proper ancestor prefix of ``module_dotted``: an ambiguous identity fails closed (the
    shadow case — a ``pkg.py`` shadowing the ``pkg/`` directory); an ancestor with an ``__init__``
    in the workspace graph must not abort and must have every module-scope import proven under its
    own package context; a namespace-package ancestor (a directory with no ``__init__``) runs no
    code and is fine.  A present-but-unreadable ``__init__`` fails closed.
    """

    parts = module_dotted.split(".")
    for depth in range(1, len(parts)):
        ancestor = ".".join(parts[:depth])
        if ancestor in ctx.ambiguous:
            return _DEPENDENCY_ERROR
        if ancestor not in ctx.packages:
            continue
        ancestor_source = sources.get(ancestor)
        if ancestor_source is None:
            return _DEPENDENCY_ERROR
        if not isinstance(ancestor_source, str) or len(ancestor_source) > _MAX_SOURCE_LENGTH:
            return _SOURCE_ERROR
        try:
            ancestor_tree = ast.parse(ancestor_source, mode="exec")
        except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
            return _SOURCE_ERROR
        if _module_unconditionally_aborts(ancestor_tree):
            return _SHAPE_ERROR
        ancestor_ctx = _ImportContext(
            installed=ctx.installed,
            local_roots=ctx.local_roots,
            local_dotted=ctx.local_dotted,
            package=tuple(parts[:depth]),
            ambiguous=ctx.ambiguous,
            packages=ctx.packages,
        )
        if not _module_imports_are_proven(ancestor_tree, ancestor_ctx, sources, frozenset(), 0):
            return _DEPENDENCY_ERROR
    return None


def _prove_reexport(
    target: ast.stmt,
    attribute: str,
    *,
    server: str,
    factory: bool,
    ctx: _ImportContext,
    sources: Mapping[str, str],
    visited: frozenset[str],
    depth: int,
) -> str | None:
    """Prove a target bound by a workspace-local import by recursing into the referenced module.

    Only ``from <module> import <name>`` re-exports a provable attribute; ``import x`` and
    ``from . import sub`` bind *module* objects (never servable apps) and fail closed.  The
    referenced module must exist in the workspace graph *and* be supplied in ``sources``; the
    re-exported name is then proven there under the identical strict rules (the recursion reuses
    :func:`_prove_module`, never a weaker fork).  Every hop beyond ``_MAX_REEXPORT_DEPTH``, cycle
    (``visited``), missing/unsourced module, unresolvable relative level, or ``*`` import fails
    closed — the whole point being that an unrunnable re-export must never become a candidate.
    """

    if depth >= _MAX_REEXPORT_DEPTH:
        return _DEPENDENCY_ERROR
    if not isinstance(target, ast.ImportFrom):
        return _SHAPE_ERROR
    origin = _reexport_origin(target, attribute)
    if origin is None:
        return _SHAPE_ERROR
    resolved = _resolve_imported_module(target.level, target.module, ctx.package)
    if resolved is None or resolved not in ctx.local_dotted or resolved in visited:
        return _DEPENDENCY_ERROR
    # A re-export that resolves THROUGH an ambiguous file/package identity (e.g. both ``real.py``
    # and ``real/__init__.py`` produce the dotted name ``real``) cannot be soundly certified —
    # CPython deterministically binds the package, but the file's app must never be certified in
    # its place. Fail closed rather than depend on which colliding source happened to be stored.
    if _resolves_through_ambiguous(resolved, ctx):
        return _DEPENDENCY_ERROR
    # A re-export whose resolved module root also names an installed distribution is a shadow
    # collision (see ``_root_is_shadow_collision``): ``uvicorn <mod>:<name>`` puts the app dir
    # on sys.path[0], so ``<root>`` binds the workspace file and shadows the distribution — the
    # shadow re-export is unrunnable (a self-import circular crash) or, at best, path-order
    # ambiguous, and can never be soundly certified. Fail closed instead of recursing.
    if _root_is_shadow_collision(resolved.split(".")[0], ctx):
        return _DEPENDENCY_ERROR
    # CPython imports the resolved module's ancestor packages before it, so a re-export into
    # ``pkg.impl`` must also clear ``pkg/__init__``'s side effects (abort / unproven imports).
    ancestor_error = _prove_ancestor_packages(resolved, ctx, sources)
    if ancestor_error is not None:
        return ancestor_error
    ref_source = sources.get(resolved)
    if ref_source is None:
        return _DEPENDENCY_ERROR
    if not isinstance(ref_source, str) or len(ref_source) > _MAX_SOURCE_LENGTH:
        return _SOURCE_ERROR
    try:
        ref_tree = ast.parse(ref_source, mode="exec")
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
        return _SOURCE_ERROR
    # A referenced package ``__init__`` runs with ``__package__`` set to the package itself, so a
    # relative re-export (``from .impl import app`` in ``pkg/__init__.py``) climbs from ``pkg``,
    # not from ``()``. A plain module's ``__package__`` is its parent (``resolved`` minus its last
    # segment). Getting this right lets a runnable package re-export prove out while still failing
    # closed on missing/cyclic/beyond-top-level targets.
    ref_package = (
        tuple(resolved.split(".")) if resolved in ctx.packages else tuple(resolved.split(".")[:-1])
    )
    ref_ctx = _ImportContext(
        installed=ctx.installed,
        local_roots=ctx.local_roots,
        local_dotted=ctx.local_dotted,
        package=ref_package,
        ambiguous=ctx.ambiguous,
        packages=ctx.packages,
    )
    return _prove_module(
        ref_tree,
        origin,
        server=server,
        factory=factory,
        ctx=ref_ctx,
        sources=sources,
        visited=visited | {resolved},
        depth=depth + 1,
    )


def _prove_module(
    tree: ast.Module,
    attribute: str,
    *,
    server: str,
    factory: bool,
    ctx: _ImportContext,
    sources: Mapping[str, str],
    visited: frozenset[str],
    depth: int,
    runs_as_main: bool | None = None,
) -> str | None:
    """The strict reachability proof for one already-parsed module (also the recursion body).

    Enforces the exact-once binding, every-import-proven, and no-unconditional-abort invariants
    for ``tree`` under its own ``ctx``, then dispatches by target shape: a target bound by an
    import re-exports and recurses via :func:`_prove_reexport`; otherwise the ``--factory``,
    protocol-callable, and framework-construction rules decide it exactly as before.

    ``runs_as_main`` (``False`` for the top-level entrypoint module imported by uvicorn; ``None``
    for every re-export hop, which stays opaque per the transitive-module rule) is threaded only to
    the import proof, so the entrypoint's module-scope-called locals are import-checked while a
    re-exported module's nested scopes remain opaque.
    """

    binding_counts = _module_binding_counts(tree)
    candidates = _direct_target_nodes(tree, attribute)
    if binding_counts[attribute] != 1 or len(candidates) != 1:
        return _BINDING_ERROR
    target = candidates[0]
    if not _module_imports_are_proven(
        tree, ctx, sources, frozenset(), 0, runs_as_main=runs_as_main
    ):
        return _DEPENDENCY_ERROR
    if _module_unconditionally_aborts(tree):
        return _SHAPE_ERROR

    if isinstance(target, (ast.Import, ast.ImportFrom)):
        return _prove_reexport(
            target,
            attribute,
            server=server,
            factory=factory,
            ctx=ctx,
            sources=sources,
            visited=visited,
            depth=depth,
        )

    if factory:
        if not isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return _FACTORY_ERROR
        arguments = target.args
        required = len(arguments.posonlyargs) + (len(arguments.args) - len(arguments.defaults))
        kw_required = sum(1 for default in arguments.kw_defaults if default is None)
        if required > 0 or kw_required > 0 or arguments.vararg is not None:
            return _FACTORY_ERROR
        if target.decorator_list or any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda))
            and node is not target
            for node in ast.walk(target)
        ):
            return _SHAPE_ERROR
        if not _factory_return_is_proven(
            target,
            tree=tree,
            binding_counts=binding_counts,
            server=server,
            ctx=ctx,
            sources=sources,
        ):
            return _SHAPE_ERROR
        parameter_names = frozenset(
            argument.arg
            for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
        )
        if not _target_scope_is_closed(
            target, tree=tree, ctx=ctx, sources=sources, parameter_names=parameter_names
        ):
            return _DEPENDENCY_ERROR
        return None

    if isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if server in {"uvicorn", "hypercorn"}:
            compatible = isinstance(target, ast.AsyncFunctionDef) and _function_has_exact_signature(
                target, ("scope", "receive", "send")
            )
            parameters = frozenset({"scope", "receive", "send"})
        else:
            compatible = isinstance(target, ast.FunctionDef) and _function_has_exact_signature(
                target, ("environ", "start_response")
            )
            parameters = frozenset({"environ", "start_response"})
        if not compatible:
            return _SHAPE_ERROR
        if not _target_scope_is_closed(
            target, tree=tree, ctx=ctx, sources=sources, parameter_names=parameters
        ):
            return _DEPENDENCY_ERROR
        return None

    value = _assignment_value(target, attribute)
    if not _proven_app_construction(
        value, tree=tree, binding_counts=binding_counts, server=server, ctx=ctx, sources=sources
    ):
        return _SHAPE_ERROR
    return None


def python_entrypoint_error(
    source: str,
    attribute: str,
    *,
    server: str,
    factory: bool,
    installed_packages: frozenset[str],
    local_modules: frozenset[str] = frozenset(),
    local_module_paths: frozenset[str] = frozenset(),
    target_package: tuple[str, ...] = (),
    target_module: str = "",
    local_module_sources: Mapping[str, str] | None = None,
    ambiguous_modules: frozenset[str] = frozenset(),
    package_modules: frozenset[str] = frozenset(),
) -> str | None:
    """Prove one exact ``module:attribute`` (or ``--factory`` callable) is a reachable app.

    Reachability, not mere presence: the attribute must bind exactly once at module scope to
    an installed ASGI/WSGI framework construction (FastAPI/Starlette for uvicorn/hypercorn,
    Flask for gunicorn) or an exact protocol callable; a ``--factory`` target must be a
    zero-argument function whose every return is such a construction.  Every module- and
    target-scope import must be a standard-library module, a distribution in the install plan,
    or a proven workspace-local module; a top-level statement that unconditionally aborts
    import (a module-scope ``raise``, falsy ``assert``, or ``sys.exit()``/``exit()``,
    whether before OR after the binding) fails closed.

    A target bound by a workspace-local re-export (``from <module> import <name>``) is proven
    by recursing the *same* strict proof into ``local_module_sources[<resolved module>]`` and
    proving ``<name>`` there.  Recursion resolves relative levels against ``target_package``
    (so a top-level relative re-export stays unproven), only descends into modules that are
    both present in the workspace graph and supplied in ``local_module_sources``, is bounded to
    ``_MAX_REEXPORT_DEPTH`` hops, and is cycle-guarded — every missing module, ``*`` import,
    cycle, or depth overflow fails closed.  A referenced *package* named in ``package_modules``
    (its source is a ``__init__.py``) recurses with its own name as ``__package__``, so a relative
    re-export inside it (``from .impl import app``) climbs correctly and can prove out; a plain
    module recurses with its parent package (the resolved name minus its last segment).  Any hop
    that resolves through an ``ambiguous_modules`` identity (a dotted name produced by both a file
    and a package ``__init__``, a module file that shadows a package directory of the same name, or
    duplicate normalized paths) fails closed, because CPython's binding is not statically decidable
    there.  When ``target_module`` is a dotted name (``a.b.c``), every ancestor package
    ``__init__`` CPython imports first (``a``, ``a.b``) is validated too — it must not
    unconditionally abort and its module-scope imports must all be proven — so an ancestor
    ``pkg/__init__`` that ``raise``\\ s or imports a missing distribution fails closed even when the
    submodule's own source is a valid app.  Callers that do not supply ``local_module_sources``
    (the default) get the pre-existing behavior unchanged: every re-exported target fails closed.

    Static/dynamic boundary (honest scope, not a weakening).  This proof completes STATIC
    structural checks — an exact servable app/callable/factory shape, no unconditional module-scope
    abort (``raise`` / statically-falsy ``assert`` / ``sys.exit``), and full transitive import
    RESOLUTION (every module-, ancestor-, target-, and re-export-scope import resolves to stdlib, an
    installed distribution, or a recursively import-safe workspace module).  It does NOT catch
    DYNAMIC runtime failures that require executing code — a top-level ``1/0``, a ``NameError`` from
    using an undefined variable, ``open()`` of a missing file, or a called function that raises —
    which the static proof leaves UNVERIFIED (not decided here).  It never statically executes
    arbitrary code; every returned error is fixed, bounded, and value-free.
    """

    if (
        not isinstance(attribute, str)
        or not attribute.isidentifier()
        or keyword.iskeyword(attribute)
    ):
        return _ATTRIBUTE_ERROR
    if not isinstance(source, str) or len(source) > _MAX_SOURCE_LENGTH:
        return _SOURCE_ERROR
    try:
        tree = ast.parse(source, mode="exec")
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
        return _SOURCE_ERROR
    if server not in {"uvicorn", "gunicorn", "hypercorn"}:
        return _SHAPE_ERROR

    ctx = _ImportContext(
        installed=_installed_names(installed_packages),
        local_roots=frozenset(local_modules),
        local_dotted=frozenset(local_module_paths),
        package=tuple(target_package),
        ambiguous=frozenset(ambiguous_modules),
        packages=frozenset(package_modules),
    )
    sources = local_module_sources if local_module_sources is not None else {}
    if target_module:
        ancestor_error = _prove_ancestor_packages(target_module, ctx, sources)
        if ancestor_error is not None:
            return ancestor_error
    return _prove_module(
        tree,
        attribute,
        server=server,
        factory=factory,
        ctx=ctx,
        sources=sources,
        visited=frozenset(),
        depth=0,
        # The entrypoint module is IMPORTED by the server (uvicorn/gunicorn), so its body runs
        # top-to-bottom and its module-scope-called locals' imports really execute (checked), while
        # ``__name__ != '__main__'`` -> its ``if __name__ == '__main__':`` block is dead on import
        # (dev-only imports there are not required).
        runs_as_main=False,
    )


def python_script_import_error(
    source: str,
    *,
    installed_packages: frozenset[str],
    local_modules: frozenset[str] = frozenset(),
    local_module_paths: frozenset[str] = frozenset(),
    local_module_sources: Mapping[str, str] | None = None,
    ambiguous_modules: frozenset[str] = frozenset(),
    package_modules: frozenset[str] = frozenset(),
) -> str | None:
    """Prove every module-scope import of a standalone ``python <script>`` resolves.

    This is the import-RESOLUTION half of :func:`python_entrypoint_error`, applied to a script
    that is NOT required to bind a servable ASGI/WSGI app (a one-shot migration runner).  It
    reuses the exact same machinery the entrypoint proof relies on
    (:func:`_module_imports_are_proven` over an :class:`_ImportContext`): every module-scope
    import must resolve to a standard-library/builtin module, a distribution in the exact install
    plan, or a recursively import-safe workspace-local module, with the identical transitive,
    guard-aware (``try/except``), shadow-collision, and ambiguous-identity rules.  Because a
    ``python <script>`` runs as the top-level ``__main__`` with ``package == ()``, every relative
    import fails closed exactly as CPython raises ``ImportError`` for it.

    Deliberately NOT required here (the difference from :func:`python_entrypoint_error`): no
    ASGI/WSGI/protocol app shape, no exact ``module:attribute`` binding, and no unconditional
    module-scope abort check — a migration script legitimately runs to completion and may
    ``sys.exit(0)``.  ONLY that its imports resolve.  Returns a fixed, value-free error string
    when the source is unparseable/oversized or any module-scope import is unbacked by the install
    plan, else ``None``.  Never binds an app, executes user text, or requires an app shape.
    """
    if not isinstance(source, str) or len(source) > _MAX_SOURCE_LENGTH:
        return _SOURCE_ERROR
    try:
        tree = ast.parse(source, mode="exec")
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
        return _SOURCE_ERROR
    ctx = _ImportContext(
        installed=_installed_names(installed_packages),
        local_roots=frozenset(local_modules),
        local_dotted=frozenset(local_module_paths),
        package=(),
        ambiguous=frozenset(ambiguous_modules),
        packages=frozenset(package_modules),
    )
    sources = local_module_sources if local_module_sources is not None else {}
    # A ``python <script>`` runs as the top-level ``__main__``: its module body executes
    # top-to-bottom, so imports inside a local function it unconditionally calls from module scope
    # really run, and its ``if __name__ == '__main__':`` block is reachable (``runs_as_main=True``).
    if not _module_imports_are_proven(tree, ctx, sources, frozenset(), 0, runs_as_main=True):
        return _DEPENDENCY_ERROR
    return None
