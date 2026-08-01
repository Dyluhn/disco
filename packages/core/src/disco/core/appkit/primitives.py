"""AppKit EPIC N — the PRIMITIVE registry.

Epic E made `generate(app_spec, design_spec)` a single, hard-wired LEAD-GEN
lowering. Epic N generalizes it WITHOUT a giant primitive-parametrized mega-
generator: each AppKit primitive (lead-gen, directory, …) is a small, EXPLICIT
`PrimitiveDefinition` behind this tiny registry, and the public `generate`
DISPATCHES on the resolved primitive. The generator keeps a per-primitive
`generate` callable; the stable, shape-agnostic emitters (design tokens/CSS,
index/package/tsconfig/vite, the section components) are SHARED module functions
both primitives call. This is "per-primitive modules behind a registry", not a
universal CRUD/API/auth DSL — the deliberate anti-over-generalization choice.

A `PrimitiveDefinition` owns exactly four things:

* ``id`` / ``aliases`` — the canonical primitive id (matched against
  ``AppSpec.app_kind``) plus any accepted aliases;
* ``default_app_spec(name, recipe)`` — the sensible default `AppSpec` for this
  primitive when `app_create` is given only a brief (no explicit spec);
* ``prepare_app_spec(app)`` — the airtight normalization `app_create` persists
  (e.g. lead-gen appends the synthesized lead entity); identity by default;
* ``generate(app, design)`` — lower the two specs into the `{path: contents}`
  tree for this primitive's shape.

RESOLUTION is deliberately lenient at the GENERATE boundary and strict at the
TOOL boundary: `resolve_primitive(app_kind)` FALLS BACK to lead-gen for any
unrecognized `app_kind` (so every pre-Epic-N AppSpec — `web_app`, `x`, … — lowers
EXACTLY as before, byte-identical), while `get_primitive(id)` returns None for an
unknown id so `app_create(primitive_id=…)` can REJECT a bogus primitive instead of
silently scaffolding lead-gen.

PURITY / LAYERING: `disco.core` is the leaf package (.importlinter). This module
imports ONLY the stdlib (+ typing-only spec references); the generator IMPORTS
this module and REGISTERS its primitives at import time, so the registry is
populated by the time anything calls `generate` / the tools resolve a primitive.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pydantic import BaseModel

    from .recipes import SiteRecipe

from .spec import AppSpec, DesignSpec

# Canonical primitive ids — matched against `AppSpec.app_kind` (and accepted as
# `app_create(primitive_id=…)`). Kept as constants so the generator, the tools, and
# the verifier never disagree about the spelling.
LEAD_GEN_PRIMITIVE_ID = "lead_gen"
DIRECTORY_PRIMITIVE_ID = "directory"
RECORDS_PRIMITIVE_ID = "records"
LOCAL_LIST_PRIMITIVE_ID = "local_list"
HELLO_PRIMITIVE_ID = "hello"
FORM_PRIMITIVE_ID = "form"
SEO_PRIMITIVE_ID = "seo"
COLLECTION_PRIMITIVE_ID = "collection"
FEATURE_FLAGS_PRIMITIVE_ID = "feature_flags"
BLOG_PRIMITIVE_ID = "blog"
ANALYTICS_PRIMITIVE_ID = "analytics"


@dataclass(frozen=True)
class HostService:
    """A runtime service the generated app calls at request time (e.g. "email.send",
    "ai.chat", "storage.put"). Secrets resolve HOST-SIDE via S-W2; WO-A2 wires it.
    WO-A0 only declares the shape."""

    name: str


@dataclass(frozen=True)
class VerifyCheck:
    """One named leg of a primitive verify run: pass/fail + a short human evidence
    string. WO-A3: the verifier tool maps these 1:1 into its W-45 verdict checks."""

    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True)
class PrimitiveVerifyResult:
    """Result of a primitive's adversarial build-gate verify hook. WO-A3 wires it
    into the build gate via `verify_appkit_app`'s dispatch; `checks` carries the
    per-leg breakdown (defaulted, so pre-WO-A3 constructions are unaffected)."""

    ok: bool
    detail: str = ""
    checks: tuple[VerifyCheck, ...] = ()


PrimitiveLiveVerifier = Callable[
    [str, AppSpec, DesignSpec, Mapping[str, str]],
    Awaitable[PrimitiveVerifyResult],
]


@dataclass(frozen=True)
class PrimitiveDefinition:
    """One AppKit primitive: its id/aliases + the three pure spec→spec / spec→tree
    callables the generator and `app_create` dispatch through. Frozen so a
    registered primitive can't be mutated after registration.

    The WO-A0 extension fields ALL default (`tier`/`host_contract`/`spec_schema`/
    `verify`) so every pre-existing `PrimitiveDefinition(...)` construction — and
    therefore its `generate()` output — is unaffected: `tier="fillable"` (the model
    may author), no host services, no per-primitive spec schema, no extra verify
    hook. Protect that anti-breakage guarantee when adding fields.

    WO-A1 adds `apply_spec` (also defaulted): fold a VALIDATED `spec_schema`
    instance into an existing AppSpec — the `app_add_primitive` seam. A primitive
    is "addable" iff BOTH `spec_schema` and `apply_spec` are set; the AppSpec stays
    the single source of truth and the app's own base primitive regenerates the
    whole tree from the folded spec (no per-addon file overlay — the sandbox
    protocol has no delete, so overlays would strand stale files).

    WO-A3 evolves the `verify` signature to
    ``(AppSpec | None, DesignSpec | None, Mapping[str, str]) -> PrimitiveVerifyResult``:
    the specs are Optional (a missing/unreadable spec still verifies, failing with
    the usual "run app_create first" evidence) and the tree maps relpath → ON-DISK
    text — a missing file is an ABSENT KEY (checks use ``tree.get(path)`` and treat
    None as missing, the same semantics the verifier tool's sandbox reads have).
    Nothing implemented the WO-A0 signature, so this is a free evolution."""

    id: str
    default_app_spec: Callable[[str, SiteRecipe], AppSpec]
    prepare_app_spec: Callable[[AppSpec], AppSpec]
    generate: Callable[[AppSpec, DesignSpec], dict[str, str]]
    aliases: tuple[str, ...] = field(default_factory=tuple)
    tier: Literal["fillable", "template_only"] = "fillable"
    host_contract: tuple[HostService, ...] = ()
    spec_schema: type[BaseModel] | None = None
    verify: (
        Callable[[AppSpec | None, DesignSpec | None, Mapping[str, str]], PrimitiveVerifyResult]
        | None
    ) = None
    apply_spec: Callable[[AppSpec, BaseModel], AppSpec] | None = None
    # Security-classed primitives split verification in two. ``verify`` remains
    # the deterministic, secret-free source/tree verifier. ``live_verify_id``
    # names the host-owned adversarial runner that must ALSO pass before the
    # workspace can ship.  The callback itself is deliberately not stored in the
    # core registry: tools receives it through ToolContext from the host runtime.
    live_verify_id: str | None = None
    # Exact live exploit legs required from the host runner. A partial or renamed
    # result is unknown wiring, not evidence that the security contract passed.
    live_verify_checks: tuple[str, ...] = ()
    # The optional AppSpec metadata field whose presence makes this primitive a
    # mandatory security gate even if its mutable provenance file was deleted.
    security_metadata_field: str | None = None


# The registry, populated by `generator.py` at import time. Keyed by canonical id;
# `_ALIASES` maps every accepted alias to a canonical id so resolution is one hop.
_REGISTRY: dict[str, PrimitiveDefinition] = {}
_ALIASES: dict[str, str] = {}


def _check_live_verify_pairing(defn: PrimitiveDefinition) -> str | None:
    if (defn.live_verify_id is None) != (defn.security_metadata_field is None):
        return (
            f"primitive {defn.id!r} must declare live_verify_id and "
            "security_metadata_field together"
        )
    return None


def _check_live_verify_id_nonblank(defn: PrimitiveDefinition) -> str | None:
    if defn.live_verify_id is not None and not defn.live_verify_id.strip():
        return f"primitive {defn.id!r} has an empty live_verify_id"
    return None


def _check_live_verify_checks_present(defn: PrimitiveDefinition) -> str | None:
    if defn.live_verify_id is None:
        return None
    checks = defn.live_verify_checks
    well_formed = (
        bool(checks)
        and all(name.strip() for name in checks)
        and len(set(checks)) == len(checks)
    )
    if not well_formed:
        return f"primitive {defn.id!r} must declare unique non-empty live_verify_checks"
    return None


def _check_live_verify_checks_absent(defn: PrimitiveDefinition) -> str | None:
    if defn.live_verify_id is None and defn.live_verify_checks:
        return f"primitive {defn.id!r} declares live_verify_checks without live_verify_id"
    return None


def _check_security_metadata_field(defn: PrimitiveDefinition) -> str | None:
    if defn.security_metadata_field is not None and not defn.security_metadata_field.isidentifier():
        return (
            f"primitive {defn.id!r} has invalid security metadata field "
            f"{defn.security_metadata_field!r}"
        )
    return None


# One row per SELF-CONTAINED registration invariant. `register_primitive` just
# walks this table and raises on the first failure — the table-driven shape (vs.
# one long if-chain) is what keeps its own McCabe score low: each predicate is a
# tiny, independently-testable single-purpose check instead of one function
# carrying every branch.
_REGISTRATION_CHECKS: tuple[Callable[[PrimitiveDefinition], str | None], ...] = (
    _check_live_verify_pairing,
    _check_live_verify_id_nonblank,
    _check_live_verify_checks_present,
    _check_live_verify_checks_absent,
    _check_security_metadata_field,
)


def _check_no_duplicate_id(defn: PrimitiveDefinition) -> None:
    if defn.id in _REGISTRY and _REGISTRY[defn.id] is not defn:
        raise ValueError(f"duplicate primitive id: {defn.id!r}")


def _check_no_alias_conflict(defn: PrimitiveDefinition) -> None:
    for alias in defn.aliases:
        owner = _ALIASES.get(alias)
        if owner is not None and owner != defn.id:
            raise ValueError(
                f"primitive alias {alias!r} already maps to {owner!r}, not {defn.id!r}"
            )


def register_primitive(defn: PrimitiveDefinition) -> None:
    """Register a primitive (idempotent for the SAME definition object). A second,
    DIFFERENT definition for an already-claimed id/alias is a hard error — a
    duplicate is a bug, never a silent shadow."""
    for check in _REGISTRATION_CHECKS:
        message = check(defn)
        if message is not None:
            raise ValueError(message)
    _check_no_duplicate_id(defn)
    _check_no_alias_conflict(defn)
    _REGISTRY[defn.id] = defn
    for alias in defn.aliases:
        _ALIASES[alias] = defn.id


def get_primitive(prim_id: str) -> PrimitiveDefinition | None:
    """Strictly look up a primitive by canonical id OR alias — None if unknown.

    The TOOL boundary: `app_create(primitive_id=…)` uses this to REJECT a bogus
    primitive id rather than silently falling back to lead-gen."""
    canonical = _ALIASES.get(prim_id, prim_id)
    return _REGISTRY.get(canonical)


def resolve_primitive(app_kind: str) -> PrimitiveDefinition:
    """Resolve the primitive for an `AppSpec.app_kind`, FALLING BACK to lead-gen for
    any unrecognized kind.

    The GENERATE boundary: every pre-Epic-N AppSpec carries an `app_kind` the
    registry never heard of (`web_app`, `x`, …) yet must lower EXACTLY as before, so
    an unknown kind resolves to the lead-gen primitive (byte-identical output)."""
    found = get_primitive(app_kind)
    if found is not None:
        return found
    lead = _REGISTRY.get(LEAD_GEN_PRIMITIVE_ID)
    if lead is None:  # pragma: no cover — generator always registers lead-gen at import
        raise RuntimeError(
            "the lead_gen primitive is not registered; import disco.core.appkit.generator"
        )
    return lead


def primitive_ids() -> frozenset[str]:
    """The set of registered canonical primitive ids."""
    return frozenset(_REGISTRY)


def required_security_primitives(app: AppSpec | None) -> tuple[PrimitiveDefinition, ...]:
    """Return security primitives implied by the authoritative AppSpec.

    Provenance under ``.disco/primitives`` is useful cross-checking input, but it
    is workspace data and can be deleted by generated/model-authored code.  A
    security gate therefore derives mandatory membership from strict AppSpec
    metadata registered on the primitive definition.  Registry corruption is a
    hard error; callers turn it into a fail-closed verdict.
    """
    if app is None:
        return ()
    required: list[PrimitiveDefinition] = []
    for defn in sorted(_REGISTRY.values(), key=lambda item: item.id):
        field_name = defn.security_metadata_field
        if field_name is not None and getattr(app, field_name, None) is not None:
            required.append(defn)
    return tuple(required)


__all__ = [
    "ANALYTICS_PRIMITIVE_ID",
    "COLLECTION_PRIMITIVE_ID",
    "BLOG_PRIMITIVE_ID",
    "DIRECTORY_PRIMITIVE_ID",
    "FEATURE_FLAGS_PRIMITIVE_ID",
    "FORM_PRIMITIVE_ID",
    "HELLO_PRIMITIVE_ID",
    "LEAD_GEN_PRIMITIVE_ID",
    "LOCAL_LIST_PRIMITIVE_ID",
    "RECORDS_PRIMITIVE_ID",
    "SEO_PRIMITIVE_ID",
    "HostService",
    "PrimitiveDefinition",
    "PrimitiveLiveVerifier",
    "PrimitiveVerifyResult",
    "VerifyCheck",
    "get_primitive",
    "primitive_ids",
    "register_primitive",
    "required_security_primitives",
    "resolve_primitive",
]
