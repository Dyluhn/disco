"""Sandbox spec IO shared by every AppKit tool.

Reads and parses the two `.disco` specs (`AppSpec`, `DesignSpec`), and lists
existing AppKit add-on provenance records — the shared, airtight IO layer the
mutation tools build their validated patches on top of.
"""

from __future__ import annotations

from disco.core.appkit import (
    APPSPEC_RELPATH,
    DESIGNSPEC_RELPATH,
    load_app_spec_from_bytes,
    load_design_spec_from_bytes,
)
from disco.core.appkit.spec import AppSpec, DesignSpec

from ...anatomy import ToolContext
from .errors import _AppKitError

_PRIMITIVES_RELDIR = ".disco/primitives"


async def _read_spec_bytes(ctx: ToolContext, relpath: str) -> bytes | None:
    assert ctx.sandbox is not None
    try:
        return await ctx.sandbox.read_file(relpath)
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 — unreadable is never absence
        raise _AppKitError(f"cannot read {relpath}: {type(exc).__name__}: {exc}") from exc


async def _primitive_record_paths(ctx: ToolContext) -> set[str]:
    """List existing AppKit add-on provenance without treating IO failure as empty."""
    assert ctx.sandbox is not None
    try:
        entries = await ctx.sandbox.list_dir(_PRIMITIVES_RELDIR)
    except FileNotFoundError:
        return set()
    except Exception as exc:  # noqa: BLE001 - cleanup ownership must be known before overwrite
        raise _AppKitError(
            f"cannot inspect {_PRIMITIVES_RELDIR} before app creation: {type(exc).__name__}: {exc}"
        ) from exc

    records: set[str] = set()
    prefix = _PRIMITIVES_RELDIR + "/"
    for entry in entries:
        normalized = str(entry).replace("\\", "/")
        if normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized.startswith(prefix):
            candidate = normalized
        elif "/" not in normalized:
            candidate = prefix + normalized
        else:
            continue
        if candidate.endswith(".json"):
            records.add(candidate)
    return records


async def _load_app_spec(ctx: ToolContext) -> AppSpec:
    data = await _read_spec_bytes(ctx, APPSPEC_RELPATH)
    if data is None:
        raise _AppKitError(f"no {APPSPEC_RELPATH} in the workspace — run app_create first.")
    try:
        return load_app_spec_from_bytes(data)
    except Exception as exc:  # noqa: BLE001
        raise _AppKitError(f"{APPSPEC_RELPATH} is invalid: {exc}") from exc


async def _load_design_spec(ctx: ToolContext) -> DesignSpec:
    data = await _read_spec_bytes(ctx, DESIGNSPEC_RELPATH)
    if data is None:
        raise _AppKitError(f"no {DESIGNSPEC_RELPATH} in the workspace — run app_create first.")
    try:
        return load_design_spec_from_bytes(data)
    except Exception as exc:  # noqa: BLE001
        raise _AppKitError(f"{DESIGNSPEC_RELPATH} is invalid: {exc}") from exc
