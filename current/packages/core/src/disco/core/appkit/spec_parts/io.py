"""AppKit spec IO helpers — split out of `..spec` to keep that module under the
`python_or_harness_module_logical_gt_700` budget.

Pure JSON read/write under the workspace's `.disco/` dir. Schema-validated on
load. No new persistence API — `.disco/` rides the existing snapshot/rehydrate.

VERBATIM relocation: every name below is re-exported from `..spec` at its
original module path, so `from disco.core.appkit.spec import save_app_spec`
(and every other name here, public or underscore-prefixed) keeps working
unchanged. See `..spec`'s (top-of-module) import block.

`AppSpec`/`DesignSpec` are imported LOCALLY inside each function that needs
them at runtime (never at module scope): `..spec` imports this module, so a
module-level `from ..spec import AppSpec, DesignSpec` here would be a real
import cycle rather than the load-order dance a bottom-of-module import can
paper over. Each function's own annotations stay lazy strings (module has
`from __future__ import annotations`); only the bodies that actually touch
`AppSpec`/`DesignSpec` at call time need the local import, and by then `..spec`
has finished executing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from ..spec import AppSpec, DesignSpec

_DISCO_DIR = ".disco"
_APPSPEC_NAME = "appspec.json"
_DESIGNSPEC_NAME = "designspec.json"

# Workspace-RELATIVE POSIX paths to the two specs. The Epic E tools persist the
# specs through the SANDBOX API (the workspace may be a container with no host
# view), so they need the relative path, not a host `Path`. Single-source here so
# the tool layer and `design_lint` never drift from the on-disk layout.
APPSPEC_RELPATH = f"{_DISCO_DIR}/{_APPSPEC_NAME}"
DESIGNSPEC_RELPATH = f"{_DISCO_DIR}/{_DESIGNSPEC_NAME}"

# A spec is small structured JSON, never a data dump. Cap the on-disk file before
# we read/parse it so a hostile/corrupt `.disco/*.json` can't blow up memory (a
# JSON bomb): we stat first and refuse anything over this size.
# Public so a pre-read size probe (e.g. design_lint's in-sandbox `wc -c` boundary
# check) can bound against the SAME cap the loader enforces — single-source, no drift.
MAX_DESIGNSPEC_BYTES = 256 * 1024  # 256 KiB
_MAX_SPEC_BYTES = MAX_DESIGNSPEC_BYTES


def appspec_path(workspace_root: str | Path) -> Path:
    """Path to the AppSpec JSON under the workspace's `.disco/` dir."""
    return Path(workspace_root) / _DISCO_DIR / _APPSPEC_NAME


def designspec_path(workspace_root: str | Path) -> Path:
    """Path to the DesignSpec JSON under the workspace's `.disco/` dir."""
    return Path(workspace_root) / _DISCO_DIR / _DESIGNSPEC_NAME


def _serialize[SpecT: BaseModel](model: BaseModel, *, expected: type[SpecT]) -> str:
    # REVALIDATE before serializing: `frozen=True` + tuple fields block in-place mutation,
    # but a non-construction path (`model_copy(update=...)`, `model_construct(...)`, or a
    # direct `__dict__` edit) can build a model instance that SKIPPED the field +
    # cross-field validators (e.g. a duplicate route). Re-running `model_validate` on the
    # model's own JSON dump re-applies every validator, so what we emit is ALWAYS
    # schema-valid regardless of how the in-memory model was constructed. A tampered spec
    # raises ValidationError OUT here and is never persisted; we dump the VALIDATED form
    # (not the unvalidated input).
    # Validate against the schema the DESTINATION implies (`expected`), NOT `type(model)` —
    # otherwise a wrong-typed instance (e.g. a DesignSpec handed to save_app_spec) would
    # validate against its own schema and land at the wrong path.
    validated = expected.model_validate(model.model_dump(mode="json"))
    serialized = json.dumps(validated.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    # Symmetry with the loader's `_MAX_SPEC_BYTES` cap: refuse to EMIT a spec we could
    # never READ back. A spec can be schema-valid yet serialize huge (e.g. many long
    # string fields), which would then be rejected on load — a write/read asymmetry that
    # strands an unreadable file on disk. Reject it here instead (UTF-8 byte length).
    size = len(serialized.encode("utf-8"))
    if size > _MAX_SPEC_BYTES:
        raise ValueError(
            f"serialized {expected.__name__} is too large ({size} bytes > "
            f"{_MAX_SPEC_BYTES} byte cap); refusing to write an unreadable spec"
        )
    return serialized


def serialize_app_spec(spec: AppSpec) -> str:
    """Airtight-validated JSON text for an AppSpec (revalidated + size-capped).

    The serialization half of `save_app_spec`, exposed so a caller that persists
    through the SANDBOX (not a host `Path`) gets the SAME revalidate + write/read
    byte-cap guarantee — single-source, no second copy of the airtight logic."""
    from ..spec import AppSpec

    return _serialize(spec, expected=AppSpec)


def serialize_design_spec(spec: DesignSpec) -> str:
    """Airtight-validated JSON text for a DesignSpec (revalidated + size-capped)."""
    from ..spec import DesignSpec

    return _serialize(spec, expected=DesignSpec)


def _save[SpecT: BaseModel](model: BaseModel, path: Path, *, expected: type[SpecT]) -> Path:
    serialized = _serialize(model, expected=expected)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(serialized, encoding="utf-8")
    return path


def _load_json(path: Path, *, kind: str) -> dict[str, object]:
    if not path.exists():
        raise FileNotFoundError(f"no {kind} at {path}")
    size = path.stat().st_size
    if size > _MAX_SPEC_BYTES:
        raise ValueError(
            f"{kind} at {path} is too large ({size} bytes > {_MAX_SPEC_BYTES} byte cap); "
            "refusing to parse"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{kind} at {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{kind} at {path} must be a JSON object, got {type(data).__name__}")
    return data


def save_app_spec(workspace_root: str | Path, spec: AppSpec) -> Path:
    """Write `spec` to the workspace's `.disco/appspec.json` (creating `.disco/`)."""
    from ..spec import AppSpec

    return _save(spec, appspec_path(workspace_root), expected=AppSpec)


def save_design_spec(workspace_root: str | Path, spec: DesignSpec) -> Path:
    """Write `spec` to the workspace's `.disco/designspec.json` (creating `.disco/`)."""
    from ..spec import DesignSpec

    return _save(spec, designspec_path(workspace_root), expected=DesignSpec)


def load_app_spec(workspace_root: str | Path) -> AppSpec:
    """Read + schema-validate the AppSpec from the workspace's `.disco/`.

    Raises FileNotFoundError if absent, ValueError on malformed JSON, and a pydantic
    ValidationError if the JSON violates the schema."""
    from ..spec import AppSpec

    return AppSpec.model_validate(_load_json(appspec_path(workspace_root), kind="AppSpec"))


def load_design_spec(workspace_root: str | Path) -> DesignSpec:
    """Read + schema-validate the DesignSpec from the workspace's `.disco/`."""
    from ..spec import DesignSpec

    return DesignSpec.model_validate(_load_json(designspec_path(workspace_root), kind="DesignSpec"))


def _parse_spec_bytes(data: bytes | str, *, kind: str) -> dict[str, object]:
    """Shared airtight parse for the from-bytes loaders: enforce the SAME
    `_MAX_SPEC_BYTES` cap BEFORE json.loads (so a hostile blob held in hand can't
    become a memory/time bomb), then require a JSON object."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if len(raw) > _MAX_SPEC_BYTES:
        raise ValueError(
            f"{kind} is too large ({len(raw)} bytes > {_MAX_SPEC_BYTES} byte cap); "
            "refusing to parse"
        )
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{kind} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{kind} must be a JSON object, got {type(parsed).__name__}")
    return parsed


def load_app_spec_from_bytes(data: bytes | str) -> AppSpec:
    """Parse + schema-validate an AppSpec from raw bytes/str ALREADY in hand.

    The airtight loader for callers holding the file's bytes rather than a host
    path (the Epic E tools read `.disco/appspec.json` through the sandbox API).
    Enforces the SAME `_MAX_SPEC_BYTES` cap as the on-disk loader BEFORE json.loads."""
    from ..spec import AppSpec

    return AppSpec.model_validate(_parse_spec_bytes(data, kind="AppSpec"))


def load_design_spec_from_bytes(data: bytes | str) -> DesignSpec:
    """Parse + schema-validate a DesignSpec from raw bytes/str ALREADY in hand.

    The airtight loader for callers that hold the file's bytes rather than a
    local filesystem path (e.g. the sandboxed `design_lint` walk, which reads
    `.disco/designspec.json` through the sandbox API). Enforces the SAME
    `_MAX_SPEC_BYTES` cap as the on-disk loader BEFORE `json.loads`, so a hostile
    or corrupt spec can't become a memory/time bomb. Raises ValueError on an
    oversize blob or malformed JSON, and pydantic ValidationError on a schema
    violation — exactly like `load_design_spec`."""
    from ..spec import DesignSpec

    return DesignSpec.model_validate(_parse_spec_bytes(data, kind="DesignSpec"))


def load_specs(workspace_root: str | Path) -> tuple[AppSpec, DesignSpec]:
    """Load both specs from the workspace `.disco/` dir."""
    return load_app_spec(workspace_root), load_design_spec(workspace_root)
