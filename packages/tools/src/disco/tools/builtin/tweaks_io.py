"""`.disco/tweaks.json` IO + value reconciliation (P9B).

``.disco/tweaks.json`` persists the app's TweakSpec (P9A definitions + per-field defaults);
``AppSpec.tweaks`` (in ``.disco/appspec.json``) holds the current VALUES. This module bridges
them: read/write the spec, require it, and merge a TweakSpec's defaults into a values bag
(validating + canonicalizing every present value against the spec).

Error contract: a SEMANTIC failure (unknown tweak key, an invalid present value, an invalid
default) raises ``TweaksError`` with the offending key. Backend/IO failures PROPAGATE
unwrapped; a genuinely absent file reads as ``None``; a corrupt file surfaces the underlying
``ValueError``/pydantic ``ValidationError`` for the caller to map (``corrupt_tweakspec``).
"""

from __future__ import annotations

from typing import Any, Mapping

from disco.core.tweaks import TweakSpec

TWEAKS_PATH = ".disco/tweaks.json"


class TweaksError(ValueError):
    """A semantic tweak failure (unknown key / invalid value / invalid default / required-but-absent)."""


async def read_tweakspec(sandbox: Any) -> TweakSpec | None:
    """The persisted TweakSpec, or ``None`` if ``.disco/tweaks.json`` is genuinely absent.

    Uses ``file_exists`` FIRST so a transient read/backend error is NOT mistaken for absence
    (the P7 clobber-class lesson). A present-but-corrupt file raises ``ValidationError``."""
    if not await sandbox.file_exists(TWEAKS_PATH):
        return None
    data = await sandbox.read_file(TWEAKS_PATH)  # propagates a real read error (not → None)
    return TweakSpec.model_validate_json(data)


async def write_tweakspec(sandbox: Any, spec: TweakSpec) -> None:
    await sandbox.write_file(TWEAKS_PATH, (spec.model_dump_json(indent=2) + "\n").encode("utf-8"))


async def require_tweakspec(sandbox: Any) -> TweakSpec:
    """The TweakSpec, or ``TweaksError`` when no ``.disco/tweaks.json`` exists."""
    spec = await read_tweakspec(sandbox)
    if spec is None:
        raise TweaksError(f"no {TWEAKS_PATH} — this app defines no tweaks")
    return spec


def merge_tweak_defaults(spec: TweakSpec, values: Mapping[str, Any]) -> dict[str, Any]:
    """Reconcile ``values`` against ``spec`` → a new dict with deterministic (sorted) key order:

    * every present value for a DEFINED tweak is validated + coerced to its canonical form;
    * a defined tweak ABSENT from ``values`` with a non-None default is filled from that default;
    * a value keyed to an UNDEFINED tweak raises ``TweaksError`` (never keep ungoverned state).

    A present ``None`` counts as PRESENT (validated, not default-filled). Defaults never
    overwrite a present value.
    """
    defined = {f.key for f in spec.fields}
    unknown = sorted(set(values) - defined)
    if unknown:
        raise TweaksError(f"value(s) for undefined tweak(s): {unknown}")

    merged: dict[str, Any] = {}
    for field in spec.fields:
        if field.key in values:
            try:
                merged[field.key] = spec.validate_value(field.key, values[field.key])
            except ValueError as e:
                raise TweaksError(f"invalid value for tweak {field.key!r}: {e}") from e
        elif field.default is not None:
            try:
                merged[field.key] = spec.validate_value(field.key, field.default)
            except ValueError as e:  # a default that doesn't validate is a spec authoring bug
                raise TweaksError(f"invalid default for tweak {field.key!r}: {e}") from e
    return dict(sorted(merged.items()))
