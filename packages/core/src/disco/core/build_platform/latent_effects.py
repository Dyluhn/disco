"""Frozen ``R-LATENT/v1`` latent-effect classification for Build Platform inputs.

This module is the schema-owned validation guard the ``16-R1`` research lexicon
was written for.  It is **static and bounded**: it inspects declared input values
and returns a verdict.  It opens no scanner service, runtime, subprocess,
network, filesystem, provider, or prompt channel, and it never executes an input.

The vocabulary is the frozen ``R-LATENT/v1`` lexicon: exactly the twelve rules
``R1``-``R12`` and the six legitimate-configuration controls ``LC1``-``LC6``.
Per the lexicon's §6, the same version applies to all three input kinds that
carry user/owner content — Starter Recipes, Library Recipes, and Reference Packs
— so this module owns the classification once and each schema declares only the
*surface* of its own fields.

A field's :class:`ValueSurface` is what makes a control legitimate rather than
latent.  A build invocation is clean in a declared ``COMMAND`` field (``LC6``)
and a documentation link is clean in a ``DOCUMENTATION`` field (``LC4``), while
the same bytes in a plain ``DATA`` field are not.  Surfaces are declared by the
schema; nothing is inferred from a field's name, and this module names no
target: Core stays target-neutral, so no engine or platform vocabulary appears
here even in an example.

Two precedence facts, recorded because they are not obvious from the lexicon
text alone:

* ``LC2`` is implemented as a discriminator, not an exemption list.  A shell
  metacharacter is latent only when an executable token follows it, which is
  what separates a chained command from prose that merely contains ``;``.
* A value whose entire content is an embedded script — a leading ``#!/``
  prologue — is classified ``hidden_executable`` (``R5``) even though the shell
  syntax *inside* that script would otherwise fire ``R1`` first under the
  lexicon's §3 family order.  The script is one finding, and its own internal
  syntax is not independent evidence of parameter smuggling.  This is the only
  reading that satisfies both the frozen lexicon and the frozen corpus, whose
  ``POS-007`` case expects ``R5``.

The documented residual in the lexicon's §7 stands unchanged: this is not an
interpreter, it models no complete grammar, and it makes no claim about effects
that appear only when a consuming engine actually executes a value.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from enum import Enum
from typing import Literal

from pydantic import Field

from .contracts import FrozenModel

R_LATENT_LEXICON_VERSION: Literal["R-LATENT/v1"] = "R-LATENT/v1"
R_LATENT_SCANNER_RULESET: Literal["r-latent/v1"] = "r-latent/v1"

# The lexicon's §4.4 bounded caps.  ``_MAX_DOC_BYTES`` mirrors the existing
# whole-tree scan cap in ``release/detect_parts/_constants.py``.
MAX_VALUE_BYTES = 4096
MAX_DOCUMENT_BYTES = 256 * 1024
MAX_DEPTH = 64


class LatentEffect(str, Enum):
    """The lexicon's §3 classification vocabulary, in deny-wins family order."""

    CLEAN = "clean"
    PARAMETER_SMUGGLING = "parameter_smuggling"
    HIDDEN_EXECUTABLE = "hidden_executable"
    PATH_ESCAPE = "path_escape"
    CREDENTIAL_LEAK = "credential_leak"
    NETWORK_EGRESS = "network_egress"
    UNBOUNDED_INPUT = "unbounded_input"


class ValueSurface(str, Enum):
    """A field's declared purpose, which fixes which controls are legitimate.

    ``DATA`` is the strict default: every rule applies.  The remaining surfaces
    name a legitimate control from the lexicon's §5 and relax exactly the rules
    that control makes non-latent — never more.
    """

    DATA = "data"
    COMMAND = "command"
    DOCUMENTATION = "documentation"
    PROJECT_PATH = "project_path"
    STRUCTURED = "structured"
    ENV_NAME_MAP = "env_name_map"


#: Surfaces whose declared shape is a single scalar.  A non-scalar arriving at
#: one of these is ``R4`` parameter overload.
_SCALAR_SURFACES = frozenset(
    {
        ValueSurface.DATA,
        ValueSurface.COMMAND,
        ValueSurface.DOCUMENTATION,
        ValueSurface.PROJECT_PATH,
    }
)

#: ``LC6`` — a schema's own declared command surface is not "latent".  Only the
#: command-shaped families are relaxed; path, credential, network, and bounded
#: rules stay active on a command field.
_COMMAND_RELAXED_RULES = frozenset({"R1", "R2", "R3", "R5", "R6"})

#: ``LC4`` — a link in a documentation/annotation field is data, not egress.
_DOCUMENTATION_RELAXED_RULES = frozenset({"R9"})


class LatentEffectError(ValueError):
    """An input failed the frozen ``R-LATENT`` classification, closed."""


class LatentVerdict(FrozenModel):
    """One classification, bound to the lexicon version that produced it.

    A verdict is only comparable within its own ``lexicon_version``; the lexicon
    pins this explicitly so a ``v1`` verdict is never read as a ``v2`` verdict.
    """

    classification: LatentEffect
    rule: str | None = None
    field: str | None = None
    lexicon_version: str = R_LATENT_LEXICON_VERSION
    ruleset: str = R_LATENT_SCANNER_RULESET

    @property
    def clean(self) -> bool:
        return self.classification is LatentEffect.CLEAN

    def reason(self) -> str:
        """A named, actionable rejection reason — never a bare boolean."""
        if self.clean:
            return "clean"
        return (
            f"{self.classification.value} in field '{self.field}' "
            f"(rule {self.rule}, {self.ruleset})"
        )


# --------------------------------------------------------------------------
# Bounded vocabularies
# --------------------------------------------------------------------------

#: Executable tokens for the ``LC2`` discriminator and ``R2``.  Deliberately
#: finite: an unbounded "looks like a command" heuristic is what the lexicon's
#: §7 conservative-bias residual is written against.
_COMMAND_TOKENS = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "dash",
        "ksh",
        "node",
        "deno",
        "bun",
        "python",
        "python2",
        "python3",
        "perl",
        "ruby",
        "php",
        "curl",
        "wget",
        "nc",
        "ncat",
        "ssh",
        "scp",
        "rsync",
        "rm",
        "mv",
        "cp",
        "cat",
        "chmod",
        "chown",
        "kill",
        "dd",
        "tar",
        "eval",
        "exec",
        "export",
        "source",
        "sudo",
        "env",
        "base64",
        "npm",
        "npx",
        "pnpm",
        "yarn",
        "pip",
        "pip3",
        "git",
        "make",
    }
)

_COMMAND_PATH_PREFIXES = ("./", "/bin/", "/usr/bin/", "/usr/local/bin/", "/sbin/")

#: ``R1`` — sequences that *interpolate*.  These are latent unconditionally:
#: there is no reading of ``$(`` or a backtick pair as inert prose.
_INTERPOLATION_SEQUENCES = ("$(", "`", "${")

#: ``R1`` — sequences that *chain*.  Latent only when an executable token
#: follows, which is the ``LC2`` discriminator.  Longest first so ``&&`` is
#: matched before ``&`` would be, and ``||`` before ``|``.
_CHAIN_SEQUENCES = ("&&", "||", ";", "|", "\n")

#: ``R3`` — bare switches that would reinterpret the surrounding invocation.
_BARE_FLAGS = frozenset({"--", "-e", "--eval", "-c", "--exec", "-x", "--force", "--network"})

#: ``R5`` — a full script prologue at the head of a value.
_SCRIPT_PROLOGUES = ("#!/", "require(", "import(")

#: ``R5`` — script markers anywhere in a value.
_SCRIPT_MARKERS = ("<script>", "eval(", "```")

#: ``R6`` — executable callback declarations where the schema expects data.
_CLOSURE_MARKERS = ("=>", "function(", "lambda ", "def ", "__call__")

#: ``R7`` — path escapes out of the declared project/output root.
_PATH_SCHEMES = ("file://",)

#: ``R8`` — a secret-shaped ASSIGNMENT.  A bare secret *name* is deliberately
#: NOT sufficient: rejecting every value that merely contains the word "token"
#: is the over-rejection the playbook's step 7 forbids, so the lexicon's R8 is
#: read here as an assignment or a credential-shaped run, never a keyword hit.
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:secret|token|password|passphrase|api[_-]?key|key)\b\s*[:=]\s*"
    r"['\"]?([A-Za-z0-9][A-Za-z0-9._+/=-]{7,})"
)
_LONG_HEX_RE = re.compile(r"\b[A-Fa-f0-9]{32,}\b")
_LONG_BASE64_RE = re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")
_NETWORK_URL_RE = re.compile(r"(?i)\b(?:https?|wss?)://\S+")
_NETWORK_SOCKET_RE = re.compile(r"\b[a-z0-9][a-z0-9.-]*:\d{2,5}\b")
_NETWORK_CALL_RE = re.compile(r"\b(?:fetch|connect|XMLHttpRequest|urlopen)\s*\(")

#: ``LC3`` — a declared environment-variable NAME (never a value).  Mirrors the
#: existing ``_JS_ENV_*`` / ``_DOTENV_NAME_RE`` grammar.
_ENV_NAME_REFERENCE_RE = re.compile(
    r"^(?:process\.env\.[A-Z][A-Z0-9_]*|import\.meta\.env\.[A-Z][A-Z0-9_]*|\$\{?[A-Z][A-Z0-9_]*\}?)$"
)


def _first_token(text: str) -> str:
    """The leading whitespace/quote-trimmed token of ``text``, or ``''``."""
    head = text.lstrip().lstrip("'\"").lstrip()
    parts = head.split(maxsplit=1)
    return parts[0].rstrip("'\";") if parts else ""


def _is_command_token(token: str) -> bool:
    if not token:
        return False
    if token in _COMMAND_TOKENS:
        return True
    return token.startswith(_COMMAND_PATH_PREFIXES)


def _starts_embedded_script(value: str) -> bool:
    """``R5``'s whole-value form: the value *is* a script, prologue and all."""
    return value.lstrip().startswith(_SCRIPT_PROLOGUES)


# --------------------------------------------------------------------------
# The twelve rules — one predicate each, in lexicon order
# --------------------------------------------------------------------------


def _r1_shell_interp_in_parameter(value: str) -> bool:
    if any(seq in value for seq in _INTERPOLATION_SEQUENCES):
        return True
    for sequence in _CHAIN_SEQUENCES:
        index = value.find(sequence)
        while index >= 0:
            if _is_command_token(_first_token(value[index + len(sequence) :])):
                return True
            index = value.find(sequence, index + len(sequence))
    return False


def _r2_exec_keyword_in_parameter(value: str) -> bool:
    return _is_command_token(_first_token(value))


def _r3_hidden_flag_in_parameter(value: str) -> bool:
    return value.strip() in _BARE_FLAGS


def _r5_embedded_script(value: str) -> bool:
    if _starts_embedded_script(value):
        return True
    return any(marker in value for marker in _SCRIPT_MARKERS)


def _r6_callback_closure(value: str) -> bool:
    return any(marker in value for marker in _CLOSURE_MARKERS)


def _r7_path_escape_parameter(value: str) -> bool:
    if "../" in value or value.startswith("/"):
        return True
    return any(scheme in value for scheme in _PATH_SCHEMES)


def _r8_credential_value_embedded(value: str) -> bool:
    # A secret-shaped ASSIGNMENT is latent on its own; a long hex/base64 run is
    # latent whether or not a secret NAME accompanies it, because the run is the
    # credential-shaped evidence the lexicon's R8 names.  The name pattern is
    # therefore not a precondition, and this must not be written as one.
    if _SECRET_ASSIGNMENT_RE.search(value):
        return True
    return bool(_LONG_HEX_RE.search(value) or _LONG_BASE64_RE.search(value))


def _r9_network_target_declared(value: str) -> bool:
    if _NETWORK_URL_RE.search(value) or _NETWORK_CALL_RE.search(value):
        return True
    return bool(_NETWORK_SOCKET_RE.search(value))


#: The scalar rule table, in the lexicon's deny-wins family order.  Ordering
#: lives here, in data, so a reader can check it against §3 without reading
#: control flow.
_SCALAR_RULES: tuple[tuple[str, LatentEffect, Callable[[str], bool]], ...] = (
    ("R1", LatentEffect.PARAMETER_SMUGGLING, _r1_shell_interp_in_parameter),
    ("R2", LatentEffect.PARAMETER_SMUGGLING, _r2_exec_keyword_in_parameter),
    ("R3", LatentEffect.PARAMETER_SMUGGLING, _r3_hidden_flag_in_parameter),
    ("R5", LatentEffect.HIDDEN_EXECUTABLE, _r5_embedded_script),
    ("R6", LatentEffect.HIDDEN_EXECUTABLE, _r6_callback_closure),
    ("R7", LatentEffect.PATH_ESCAPE, _r7_path_escape_parameter),
    ("R8", LatentEffect.CREDENTIAL_LEAK, _r8_credential_value_embedded),
    ("R9", LatentEffect.NETWORK_EGRESS, _r9_network_target_declared),
)


def _relaxed_rules(surface: ValueSurface) -> frozenset[str]:
    """The rules a declared legitimate control makes non-latent — never more."""
    if surface is ValueSurface.COMMAND:
        return _COMMAND_RELAXED_RULES
    if surface is ValueSurface.DOCUMENTATION:
        return _DOCUMENTATION_RELAXED_RULES
    return frozenset()


def _verdict(effect: LatentEffect, rule: str, field: str | None) -> LatentVerdict:
    return LatentVerdict(classification=effect, rule=rule, field=field)


def _clean(field: str | None = None) -> LatentVerdict:
    return LatentVerdict(classification=LatentEffect.CLEAN, field=field)


def _scan_scalar(field: str, value: str, surface: ValueSurface) -> LatentVerdict:
    """Apply the scalar rule table to one declared value."""
    if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
        return _verdict(LatentEffect.UNBOUNDED_INPUT, "R10", field)
    if surface is ValueSurface.ENV_NAME_MAP and _ENV_NAME_REFERENCE_RE.fullmatch(value.strip()):
        return _clean(field)
    # A value that *is* a script is one finding; see the module docstring.
    if "R5" not in _relaxed_rules(surface) and _starts_embedded_script(value):
        return _verdict(LatentEffect.HIDDEN_EXECUTABLE, "R5", field)
    relaxed = _relaxed_rules(surface)
    for rule, effect, predicate in _SCALAR_RULES:
        if rule in relaxed:
            continue
        if predicate(value):
            return _verdict(effect, rule, field)
    return _clean(field)


def _scan_structured(field: str, value: object, surface: ValueSurface, depth: int) -> LatentVerdict:
    """Walk bounded structured data, applying ``R12`` then the scalar rules."""
    if depth > MAX_DEPTH:
        return _verdict(LatentEffect.UNBOUNDED_INPUT, "R12", field)
    if isinstance(value, str):
        return _scan_scalar(field, value, surface)
    if isinstance(value, Mapping):
        for key, item in value.items():
            verdict = _scan_structured(f"{field}.{key}", item, surface, depth + 1)
            if not verdict.clean:
                return verdict
        return _clean(field)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for index, item in enumerate(value):
            verdict = _scan_structured(f"{field}[{index}]", item, surface, depth + 1)
            if not verdict.clean:
                return verdict
        return _clean(field)
    return _clean(field)


def _is_scalar(value: object) -> bool:
    return isinstance(value, (str, bool, int, float)) or value is None


def classify_value(field: str, value: object, surface: ValueSurface) -> LatentVerdict:
    """Classify one declared field value under the frozen ``R-LATENT/v1`` rules.

    ``surface`` is the schema's declaration of what the field is *for*; it is
    never inferred from the value or the field name.  A scalar-declared surface
    receiving a list or mapping is ``R4`` parameter overload.
    """
    if value is None:
        return _clean(field)
    if surface in _SCALAR_SURFACES and not _is_scalar(value):
        return _verdict(LatentEffect.PARAMETER_SMUGGLING, "R4", field)
    if isinstance(value, str):
        return _scan_scalar(field, value, surface)
    if _is_scalar(value):
        return _clean(field)
    return _scan_structured(field, value, surface, depth=1)


def document_bytes(values: Mapping[str, object]) -> int:
    """The bounded byte size of a whole input document, for ``R11``."""
    return len(json.dumps(values, sort_keys=True, default=str).encode("utf-8"))


def classify_document(
    values: Mapping[str, object],
    surfaces: Mapping[str, ValueSurface],
) -> LatentVerdict:
    """Classify a whole declared value document, fail-closed and deterministic.

    ``R11`` is evaluated first because it is a property of the document rather
    than of any one field: a document can exceed the cap while every individual
    value remains ordinary and bounded, which is exactly the frozen corpus's
    ``POS-015`` case.  Fields are then scanned in sorted order so a document
    with two latent values always reports the same one.
    """
    if document_bytes(values) > MAX_DOCUMENT_BYTES:
        return _verdict(LatentEffect.UNBOUNDED_INPUT, "R11", None)
    for field in sorted(values):
        surface = surfaces.get(field)
        if surface is None:
            return _verdict(LatentEffect.PARAMETER_SMUGGLING, "R4", field)
        verdict = classify_value(field, values[field], surface)
        if not verdict.clean:
            return verdict
    return _clean()


def require_clean(values: Mapping[str, object], surfaces: Mapping[str, ValueSurface]) -> None:
    """Fail closed with a named reason unless the whole document is clean."""
    verdict = classify_document(values, surfaces)
    if not verdict.clean:
        raise LatentEffectError(verdict.reason())


class StructuredEntry(FrozenModel):
    """One node of the bounded, typed structured-value representation.

    This is deliberately *not* an unbounded ``dict[str, Any]`` escape hatch.  A
    structured value is a tree of typed, immutable entries: a scalar, a tuple of
    scalars, or named children.  Nothing here can carry a callable, a handle, or
    an arbitrary object, so a structured field cannot become an execution
    channel — and ``R12`` still bounds how deep the tree may go.
    """

    key: str = Field(min_length=1, max_length=160)
    value: str | int | float | bool | None = None
    values: tuple[str | int | float | bool, ...] = ()
    children: tuple[StructuredEntry, ...] = ()

    def as_plain(self) -> object:
        """Project this entry to the plain shape the classifier walks."""
        if self.children:
            return {child.key: child.as_plain() for child in self.children}
        if self.values:
            return list(self.values)
        return self.value


def structured_document(entries: Sequence[StructuredEntry]) -> dict[str, object]:
    """Project bounded structured entries to a plain mapping for classification."""
    return {entry.key: entry.as_plain() for entry in entries}
