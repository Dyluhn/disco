"""Frozen, versioned Starter Recipe schema and its bounded registry entry.

A Starter Recipe is a *distinct* input concept from a Reference Pack (context),
a Library Recipe (construction), an AppKit composition, a Freeform composition,
a target, a persisted profile, the host catalog, or the StarterKitRegistry.
Starter Recipes *scaffold*: their semantics are scaffold-only and distinct from
the Reference Pack ``CONTEXT`` mount and the Library Recipe ``CONSTRUCTION``
mount.

This module freezes the Starter Recipe record — including a bounded, typed,
schema-owned representation of its value surface — and a deterministic,
fail-closed registry that applies the frozen ``R-LATENT/v1`` classification to
that typed value surface at registration.  It does not add user selection,
prompt injection, direct-reference workflow, persistence/ejection, provider,
runtime-effect, Library Recipe, trust evaluation, precedence, provenance/BOM,
or guided-authoring channels; those are later slices.

The record carries its engine, profile, target, trust, required-capability,
scaffold mount, and ``R-LATENT/v1`` binding explicitly — no scope is implicit in
a label.  The existing ``BuiltinInputCatalog`` and ``StarterKitRegistry`` are
observed only as immutable host context and are never attached to, selected
through, persisted by, or executed through this module.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field

from .builtin_inputs import BuiltinInputMount
from .builtin_profiles import (
    APPKIT_ENGINE_ID,
    APPKIT_PROFILE_ID,
    BUILTIN_CAPABILITIES,
    FREEFORM_ENGINE_ID,
    FREEFORM_PROFILE_ID,
    WEB_TARGET_ID,
)
from .contracts import ComponentId, FrozenModel, TrustLevel

SUPPORTED_STARTER_RECIPE_SCHEMA_VERSION: Literal[1] = 1

# The frozen R-LATENT/v1 lexicon and scanner rule set a Starter Recipe value
# surface is bound to and classified against at registration.  This is a
# schema-owned, bounded classifier over the typed value surface; it is not a
# separate runtime scanner or effect channel.
SUPPORTED_R_LATENT_VERSION: Literal["R-LATENT/v1"] = "R-LATENT/v1"

_STARTER_RECIPE_NAMESPACE = "disco_starter"

# The canonical construction engines and the target-owned built-in target.  A
# Starter Recipe declares exactly one of each explicitly; Next.js targets use
# Freeform as their engine, but their identity stays target-owned and is not
# folded into this seam.
_KNOWN_ENGINES = frozenset({APPKIT_ENGINE_ID.canonical, FREEFORM_ENGINE_ID.canonical})
_KNOWN_TARGETS = frozenset({WEB_TARGET_ID.canonical})

# A known build profile's exact construction engine, so an engine/profile scope
# mismatch is detected deterministically rather than read from a label.
_ENGINE_FOR_PROFILE = {
    APPKIT_PROFILE_ID.canonical: APPKIT_ENGINE_ID.canonical,
    FREEFORM_PROFILE_ID.canonical: FREEFORM_ENGINE_ID.canonical,
}


class StarterRecipeError(ValueError):
    """A Starter Recipe registration or resolution failed closed."""


# ---------------------------------------------------------------------------
# Bounded, typed value surface (schema-owned, closed to the frozen R-LATENT v1
# Starter cases).  No ``dict[str, Any]`` escape is exposed.
# ---------------------------------------------------------------------------


class StarterRecipeValues(FrozenModel):
    """The bounded, typed value surface of a Starter Recipe.

    These fields bind exactly the value fields the frozen R-LATENT/v1 Starter
    Recipe corpus cases use (``name``, ``extra_args``, ``init``, ``note``,
    ``payload``, ``src``, ``build_command``).  ``payload`` is a bounded tuple of
    scalar payload entries rather than an unbounded mapping, so the value
    surface stays typed and closed.  ``build_command`` is the schema-declared
    command field (R-LATENT ``LC6``); it is a legitimate command surface, not a
    latent effect, and is never treated as latent by the classifier.
    """

    name: str = ""
    extra_args: str = ""
    init: str = ""
    note: str = ""
    payload: tuple[str, ...] = ()
    src: str = ""
    build_command: str = ""


class StarterClassification(FrozenModel):
    """One deny-wins classification of a Starter Recipe value surface."""

    verdict: Literal[
        "clean",
        "parameter_smuggling",
        "hidden_executable",
        "path_escape",
        "credential_leak",
        "network_egress",
        "unbounded_input",
    ]
    rule: str | None = None
    r_latent_version: str = SUPPORTED_R_LATENT_VERSION


# Bounded rule caps (mirror the frozen R-LATENT/v1 corpus values).
_MAX_VALUE_BYTES = 4096
_MAX_DOC_BYTES = 256 * 1024

# A conservative set of command verbs that turn a meta-character into a real
# shell chain (R1), so a lone ``;``/``&&`` in prose (LC2) stays clean while a
# genuine chain (``; rm``, ``&& curl``, ``| sh``) is rejected.
_CHAIN_VERBS = (
    "rm",
    "curl",
    "wget",
    "sh",
    "bash",
    "python",
    "python3",
    "node",
    "npm",
    "npx",
    "mkdir",
    "cd",
    "echo",
    "cat",
    "chmod",
    "chown",
    "touch",
    "cp",
    "mv",
    "dd",
    "nc",
    "perl",
    "ruby",
)
_CHAIN_TOKEN = "(?:{})".format("|".join(re.escape(v) for v in _CHAIN_VERBS))

_SHELL_CHAIN_RE = re.compile(
    r"""(;\s*"""
    + _CHAIN_TOKEN
    + r"""|&&\s*"""
    + _CHAIN_TOKEN
    + r"""|\|\|\s*"""
    + _CHAIN_TOKEN
    + r"""|\|\s*"""
    + _CHAIN_TOKEN
    + r"""|\$\('|`|^\s*\|\s*"""
    + _CHAIN_TOKEN
    + r""")"""
)
_EXEC_HEAD_RE = re.compile(
    r"""^\s*(node|npm|npx|python|python3|curl|wget|sh|bash|/bin/sh|/bin/bash|./)\s+""",
    re.IGNORECASE,
)
_BARE_OPTION_RE = re.compile(
    r"""^\s*(--|-\s*[ecx]|--eval|--exec|-c|-x|--force|--network)\s*$""",
    re.IGNORECASE,
)
_SCRIPT_PROLOGUE_RE = re.compile(r"#!|require\(|import\(|<script>|\beval\(", re.IGNORECASE)
_CALLBACK_RE = re.compile(
    r"""^\s*(\([^)]*\)\s*=>|=>|function\s*\(|\blambda\b|\bdef\s+\w+\s*\(|__call__)""",
    re.IGNORECASE,
)
_PATH_ESCAPE_RE = re.compile(r"(\.\./|^(?:\/|[a-zA-Z]:[\\\/])|file://)")
_SECRET_VALUE_RE = re.compile(
    r"(secret|token|password|passwd|credential|private|apikey|api[_-]?key)\s*[=:]\s*\S+",
    re.IGNORECASE,
)
_NETWORK_EGRESS_RE = re.compile(
    r"(wss?://[^\s'\"\)]+|://[^\s'\"\)]+|socket://|connect\(|fetch\()",
    re.IGNORECASE,
)

# Annotation/documentation fields are treated as data (R-LATENT LC4), so a
# hyperlink there is data, not runtime egress.  Credentials (R8) are never
# exempted, even in a doc field.
_DOC_FIELDS = frozenset(
    {"note", "summary", "description", "notes", "docs", "label", "comment", "annotations"}
)


def _classify_scalar(value: str, *, doc_field: bool) -> StarterClassification | None:
    """Classify one scalar string with deny-wins ordering (R5/R6, then R1-R4, then R7-R9)."""
    if len(value.encode("utf-8")) > _MAX_VALUE_BYTES:
        return StarterClassification(verdict="unbounded_input", rule="R10")
    trimmed = value.strip()
    if _SCRIPT_PROLOGUE_RE.search(trimmed):
        return StarterClassification(verdict="hidden_executable", rule="R5")
    if _CALLBACK_RE.match(trimmed):
        return StarterClassification(verdict="hidden_executable", rule="R6")
    if _SHELL_CHAIN_RE.search(trimmed):
        return StarterClassification(verdict="parameter_smuggling", rule="R1")
    if _EXEC_HEAD_RE.match(trimmed):
        return StarterClassification(verdict="parameter_smuggling", rule="R2")
    if _BARE_OPTION_RE.match(trimmed):
        return StarterClassification(verdict="parameter_smuggling", rule="R3")
    if _PATH_ESCAPE_RE.search(trimmed):
        return StarterClassification(verdict="path_escape", rule="R7")
    if _SECRET_VALUE_RE.search(trimmed):
        return StarterClassification(verdict="credential_leak", rule="R8")
    if not doc_field and _NETWORK_EGRESS_RE.search(trimmed):
        return StarterClassification(verdict="network_egress", rule="R9")
    return None


def classify_starter_values(values: StarterRecipeValues) -> StarterClassification:
    """Apply the frozen ``R-LATENT/v1`` classification to a typed value surface.

    Deny-wins.  The declared command field ``build_command`` is the schema's own
    declared command surface (R-LATENT ``LC6``) and is never treated as latent.
    A whole value surface that exceeds the bounded document cap (R11) fails
    closed even when every individual scalar is ordinary and bounded.
    """
    doc = values.model_dump_json().encode("utf-8")
    if len(doc) > _MAX_DOC_BYTES:
        return StarterClassification(verdict="unbounded_input", rule="R11")

    for payload_entry in values.payload:
        if len(payload_entry.encode("utf-8")) > _MAX_VALUE_BYTES:
            return StarterClassification(verdict="unbounded_input", rule="R10")

    for field, value in (
        ("name", values.name),
        ("extra_args", values.extra_args),
        ("init", values.init),
        ("note", values.note),
        ("src", values.src),
    ):
        result = _classify_scalar(value, doc_field=field in _DOC_FIELDS)
        if result is not None:
            return result
    for payload_entry in values.payload:
        result = _classify_scalar(payload_entry, doc_field=False)
        if result is not None:
            return result
    return StarterClassification(verdict="clean", rule=None)


class StarterRecipe(FrozenModel):
    """A versioned Starter Recipe record with explicit scaffold scope.

    ``id`` is the recipe's own distinct typed identity.  ``engine``, ``profile``,
    and ``target`` are the exact construction engine, build profile, and
    target-owned target this recipe is scoped to, and ``required_capabilities``
    is its explicit capability requirement.  ``trust`` is the recipe's trust
    tier, ``scaffold_mount`` is the host ``SCAFFOLD`` mount this scaffold-only
    recipe is bound to, ``r_latent_version`` is the frozen R-LATENT lexicon
    version its value surface is classified against, and ``values`` is the
    bounded, typed value surface.  Every scope is explicit here; none is
    inferred from a label.
    """

    id: ComponentId
    schema_version: int = SUPPORTED_STARTER_RECIPE_SCHEMA_VERSION
    engine: ComponentId
    profile: ComponentId
    target: ComponentId
    trust: TrustLevel
    required_capabilities: frozenset[str] = frozenset()
    scaffold_mount: BuiltinInputMount = BuiltinInputMount.SCAFFOLD
    r_latent_version: str = SUPPORTED_R_LATENT_VERSION
    values: StarterRecipeValues = Field(default_factory=StarterRecipeValues)


class StarterRecipeRegistry:
    """Sole owner of frozen, host-scoped Starter Recipe records.

    Registration is deterministic and fail-closed.  A record is admitted only
    when its schema version is supported, its identity is a distinct Starter
    Recipe ID (never a Reference Pack, Library Recipe, AppKit, Freeform, target,
    persisted-profile, or host-catalog identity), its engine/profile/target
    scope is coherent and known, its trust is an explicit tier, its required
    capability scope does not widen the host ceiling, it is bound to the frozen
    ``R-LATENT/v1`` vocabulary and the host ``SCAFFOLD`` mount, and its typed
    value surface classifies clean under ``R-LATENT/v1``.  Duplicate identities
    and any scope or classification violation raise with a typed reason; exact
    resolution returns the record or fails closed.
    """

    def __init__(self) -> None:
        self._recipes: dict[str, StarterRecipe] = {}

    def register(self, recipe: StarterRecipe) -> None:
        if type(recipe) is not StarterRecipe:
            raise StarterRecipeError("starter recipe registration requires exact frozen data")
        if recipe.schema_version != SUPPORTED_STARTER_RECIPE_SCHEMA_VERSION:
            raise StarterRecipeError(
                f"unsupported starter recipe schema version: {recipe.schema_version}"
            )
        if recipe.r_latent_version != SUPPORTED_R_LATENT_VERSION:
            raise StarterRecipeError(
                "unsupported R-LATENT binding: "
                f"{recipe.r_latent_version} is not {SUPPORTED_R_LATENT_VERSION}"
            )
        if recipe.scaffold_mount is not BuiltinInputMount.SCAFFOLD:
            raise StarterRecipeError("a starter recipe must mount at the host scaffold axis")
        if recipe.id.namespace != _STARTER_RECIPE_NAMESPACE:
            raise StarterRecipeError(
                "starter recipe id must use the distinct disco_starter namespace"
            )
        # The namespace guard above already forbids every Reference Pack
        # (disco_refpack), host-catalog (disco_builtin), AppKit/Freeform/target/
        # persisted-profile (disco or model_role) identity, so a Starter Recipe
        # can never reuse one of those IDs as its own.
        key = recipe.id.canonical
        if key in self._recipes:
            raise StarterRecipeError(f"duplicate starter recipe id: {key}")
        self._validate_scope(recipe)
        classification = classify_starter_values(recipe.values)
        if classification.verdict != "clean":
            raise StarterRecipeError(
                f"starter recipe value surface rejected by {classification.r_latent_version}: "
                f"{classification.verdict} ({classification.rule})"
            )
        self._recipes[key] = recipe

    @staticmethod
    def _validate_scope(recipe: StarterRecipe) -> None:
        if recipe.engine.canonical not in _KNOWN_ENGINES:
            raise StarterRecipeError(
                f"starter recipe engine {recipe.engine.canonical} "
                "is not a known construction engine"
            )
        if recipe.profile.canonical not in _ENGINE_FOR_PROFILE:
            raise StarterRecipeError(
                f"starter recipe profile {recipe.profile.canonical} is not a known build profile"
            )
        if _ENGINE_FOR_PROFILE[recipe.profile.canonical] != recipe.engine.canonical:
            raise StarterRecipeError(
                "starter recipe engine/profile scope mismatch: "
                f"{recipe.profile.canonical} requires "
                f"{_ENGINE_FOR_PROFILE[recipe.profile.canonical]}, "
                f"got {recipe.engine.canonical}"
            )
        if recipe.target.canonical not in _KNOWN_TARGETS:
            raise StarterRecipeError(
                f"starter recipe target {recipe.target.canonical} is not a known target"
            )
        widening = sorted(recipe.required_capabilities - BUILTIN_CAPABILITIES)
        if widening:
            raise StarterRecipeError(
                "starter recipe capability widening denied: " + ", ".join(widening)
            )

    def resolve(self, recipe_id: ComponentId) -> StarterRecipe:
        recipe = self._recipes.get(recipe_id.canonical)
        if recipe is None:
            raise StarterRecipeError(f"starter recipe {recipe_id.canonical} is not registered")
        return recipe

    def ids(self) -> tuple[str, ...]:
        """Deterministic registered identities, sorted by canonical ID."""
        return tuple(sorted(self._recipes))
