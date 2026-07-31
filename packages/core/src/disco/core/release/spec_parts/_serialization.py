"""Digest + canonical (de)serialization for the release spec / intent.

Extracted from ``spec.py`` to reduce module size; the public facade re-imports
these names unchanged.

``ReleaseSpec`` / ``ReleaseIntent`` (and the two schema-version constants) stay
defined in the parent ``spec.py`` module. This module needs them at RUNTIME
(``ReleaseSpec.model_validate(...)`` etc. are real classmethod calls, not just
type annotations), so — to avoid importing its own parent module at module
scope (which would be a genuine load-time circular import) — it imports them
LAZILY, inside each function body, at the point of use. By the time any of
these functions is actually CALLED, ``spec.py`` has finished importing (it
imports this module at the top, before anything calls back into it), so the
lazy import always resolves. Parameter/return annotations use the real names
under ``TYPE_CHECKING`` only, so basedpyright still type-checks them exactly.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from pydantic import ValidationError

if TYPE_CHECKING:
    from disco.core.release.spec import ReleaseIntent, ReleaseSpec

# All digests are namespaced with their algorithm (`sha256:`) so a future switch
# is self-describing on disk — the same convention AppKit's snapshot digests use.
_ALGO = "sha256"


def spec_digest(spec: ReleaseSpec) -> str:
    """A stable content digest over a ReleaseSpec.

    Canonical JSON — sorted keys, no insignificant whitespace — so two specs that
    are EQUAL as data hash identically regardless of the order fields were
    supplied in or of formatting, and ANY field change changes the digest. This is
    a release's content identity."""
    payload = json.dumps(
        spec.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"{_ALGO}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def serialize_release_spec(spec: ReleaseSpec) -> str:
    """Canonical, human-readable JSON text for a ReleaseSpec.

    REVALIDATES first (a non-construction path such as `model_copy(update=...)` /
    `model_construct(...)` can build an instance that skipped the cross-field
    validators), then dumps canonically: sorted keys + a stable indent + a
    trailing newline. Deterministic, so `serialize → load → serialize` is
    byte-identical."""
    from disco.core.release.spec import ReleaseSpec as _ReleaseSpec

    validated = _ReleaseSpec.model_validate(spec.model_dump(mode="json"))
    return (
        json.dumps(
            validated.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    )


def load_release_spec(data: bytes | str) -> ReleaseSpec:
    """Parse + schema-validate a ReleaseSpec from JSON bytes/str already in hand.

    Version-gated (WO-C7 v1 read policy): a payload declaring a `schema_version`
    NEWER than `RELEASE_SPEC_SCHEMA_VERSION` is REJECTED rather than mis-read under
    the current schema. A v1 (or version-less legacy) payload is read as-is: it
    carries no per-env `consumers`, so each var parses as `None` and the cross-field
    invariants apply the documented v1 rule (sole-ingress default for a
    single-service spec; a multi-service unbound-consumer-less var is refused, never
    fanned out).

    Raises `ValueError` on malformed JSON, a non-object payload, or a newer schema
    version, and a pydantic `ValidationError` if the JSON violates the schema."""
    from disco.core.release.spec import RELEASE_SPEC_SCHEMA_VERSION
    from disco.core.release.spec import ReleaseSpec as _ReleaseSpec

    raw = data.decode("utf-8") if isinstance(data, bytes) else data
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ReleaseSpec is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"ReleaseSpec must be a JSON object, got {type(parsed).__name__}")
    version = parsed.get("schema_version", 1)
    if isinstance(version, int) and not isinstance(version, bool):
        if version > RELEASE_SPEC_SCHEMA_VERSION:
            raise ValueError(
                f"ReleaseSpec declares schema_version {version}, newer than this build "
                f"supports ({RELEASE_SPEC_SCHEMA_VERSION}); it must not be silently "
                "reinterpreted under an older schema."
            )
    return _ReleaseSpec.model_validate(parsed)


class IntentUpgradeError(ValueError):
    """A persisted intent sidecar declares a schema version this build cannot read
    (a NEWER version, or a v1 shape that cannot be migrated without guessing). A
    typed subclass of `ValueError` so callers can distinguish a version-gate refusal
    (`intent_upgrade_required`, §8.11) from a plain malformed sidecar."""


def parse_release_intent(raw: object) -> ReleaseIntent:
    """Parse a PERSISTED release-intent sidecar payload into a `ReleaseIntent`,
    version-gated per §8.11 so an older shape is never silently reinterpreted:

    * ``schema_version == RELEASE_INTENT_SCHEMA_VERSION`` (or a legacy shape carrying
      no version, treated as v1) is validated / migrated deterministically. An OLDER
      shape (a v1/v2 payload, whose fields are a strict SUBSET of v3) migrates by
      dropping the version tag and letting the new v3 fields default — a total,
      deterministic adapter, so the same older bytes always yield the same v3 intent.
    * a NEWER ``schema_version`` (one this build does not know) is REJECTED with
      `IntentUpgradeError` rather than mis-read as the current schema.

    Raises `IntentUpgradeError` for a version-gate refusal (a NEWER schema, or a v1
    shape carrying a field the current schema forbids — an upgrade this build cannot
    perform without guessing) and `ValueError` / pydantic `ValidationError` for a
    genuinely malformed payload."""
    from disco.core.release.spec import RELEASE_INTENT_SCHEMA_VERSION
    from disco.core.release.spec import ReleaseIntent as _ReleaseIntent

    if not isinstance(raw, dict):
        raise ValueError(f"release intent must be a JSON object, got {type(raw).__name__}")
    version_obj: object = raw.get("schema_version", 1)
    if not isinstance(version_obj, int) or isinstance(version_obj, bool):
        raise ValueError(f"release intent schema_version must be an int, got {version_obj!r}")
    if version_obj > RELEASE_INTENT_SCHEMA_VERSION:
        raise IntentUpgradeError(
            f"release intent declares schema_version {version_obj}, newer than this "
            f"build supports ({RELEASE_INTENT_SCHEMA_VERSION}); it cannot be read "
            "without an upgrade and must not be silently reinterpreted."
        )
    if version_obj == RELEASE_INTENT_SCHEMA_VERSION:
        return _ReleaseIntent.model_validate(raw)
    # v1 / v2 / legacy: migrate deterministically — drop the version tag; the older
    # fields are a strict subset of v3, so the new fields default.
    migrated = {key: value for key, value in raw.items() if key != "schema_version"}
    try:
        return _ReleaseIntent.model_validate(migrated)
    except ValidationError as exc:
        # A well-formed legacy shape whose ONLY defect is a field the current schema
        # FORBIDS (`extra_forbidden`) cannot be upgraded without guessing what that
        # removed field meant — that is a version-gate refusal (`intent_upgrade_required`,
        # §8.11), NOT a generic malformed sidecar, and NOT a field to silently drop. A
        # payload carrying any OTHER validation error (a bad env name, a wrong type) is
        # genuinely malformed and is re-raised as the `ValidationError` it is.
        errors = exc.errors()
        if errors and all(err.get("type") == "extra_forbidden" for err in errors):
            raise IntentUpgradeError(
                "release intent declares a legacy (v1) shape carrying field(s) the "
                f"current schema (v{RELEASE_INTENT_SCHEMA_VERSION}) does not define; it "
                "cannot be upgraded without guessing and must not be silently "
                "reinterpreted."
            ) from exc
        raise
