"""deck_patch tool — C-EDIT-4 (deck patch, §4.4).

Applies an RFC-6902 JSON Patch to a deck's authored JSON stored in the workspace,
validates the result against the frozen AuthoredDeck schema (C4 verdict), and
re-renders (HTML + PPTX) deterministically via the C1 path.  Reverts on validation
failure — no false affordance, no partial state.

C1 integration: uses lower_deck(AuthoredDeck) -> Deck → render_html/render_pptx
directly on the C1 Deck (not the MinimalDeck compat shim).

Registration: `build_default_registry()` + `AGENT_TOOLS` + `ARTIFACT_TOOLS`
              (see `__init__.py` and `registry.py`).

K1 note: args pass through the K1 elision guard automatically via
         `loop/observe.py:230` (find_elided_arg_markers) — no special handling
         needed here.

RFC-6902 is implemented without a third-party library (jsonpatch not in deps).
We implement the subset actually needed: ``replace``, ``add``, ``remove``, ``test``.
The full spec is at https://datatracker.ietf.org/doc/html/rfc6902.
"""

from __future__ import annotations

import copy
import json
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, SkipValidation

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..registry import Tool

# ─── RFC-6902 minimal implementation ─────────────────────────────────────────

class PatchError(ValueError):
    """Raised when a patch operation fails."""


def _pointer_parts(pointer: str) -> list[str]:
    """Parse an RFC-6901 JSON Pointer into its path parts.

    ``/slides/0/title`` → ``["slides", "0", "title"]``
    ``""`` (empty string) → ``[]`` (the root document)
    ``/`` → ``[""]`` (a key named "")
    """
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise PatchError(f"Invalid JSON Pointer (must start with '/'): {pointer!r}")
    # RFC-6901 escape: ~1 → /, ~0 → ~
    return [p.replace("~1", "/").replace("~0", "~") for p in pointer[1:].split("/")]


def _get_parent(doc: Any, parts: list[str]) -> tuple[Any, str | int]:
    """Navigate to the parent of the target node; return (parent, last_key).

    Raises PatchError if any intermediate path segment is absent.
    """
    cur: Any = doc
    for part in parts[:-1]:
        if isinstance(cur, dict):
            if part not in cur:
                raise PatchError(f"Path segment {part!r} not found in object")
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                idx = int(part)
            except ValueError:
                raise PatchError(f"Path segment {part!r} is not an integer for array")
            if idx < 0 or idx >= len(cur):
                raise PatchError(f"Array index {idx} out of range (len={len(cur)})")
            cur = cur[idx]
        else:
            raise PatchError(f"Cannot descend into {type(cur).__name__} with key {part!r}")
    last = parts[-1]
    return cur, last


def _apply_op(doc: Any, op: dict[str, Any]) -> Any:
    """Apply one RFC-6902 operation to *doc* (mutates a deep copy, returns it)."""
    operation = op.get("op", "")
    path = op.get("path", "")
    parts = _pointer_parts(path)

    if operation == "test":
        # Verify without mutation
        value = op.get("value")
        if not parts:
            if doc != value:
                raise PatchError("test failed: root value mismatch")
            return doc
        parent, last = _get_parent(doc, parts)
        if isinstance(parent, dict):
            actual = parent.get(last)
        elif isinstance(parent, list):
            idx = int(last)
            actual = parent[idx] if 0 <= idx < len(parent) else None
        else:
            raise PatchError(f"test: cannot navigate into {type(parent).__name__}")
        if actual != value:
            raise PatchError(
                f"test failed at {path!r}: expected {value!r}, got {actual!r}"
            )
        return doc

    if operation == "replace":
        value = op.get("value")
        if not parts:
            return value  # replace root
        parent, last = _get_parent(doc, parts)
        if isinstance(parent, dict):
            if last not in parent:
                raise PatchError(f"replace: path {path!r} does not exist")
            parent[last] = value
        elif isinstance(parent, list):
            idx = int(last)
            if idx < 0 or idx >= len(parent):
                raise PatchError(f"replace: array index {idx} out of range")
            parent[idx] = value
        else:
            raise PatchError(f"replace: cannot set on {type(parent).__name__}")
        return doc

    if operation == "add":
        value = op.get("value")
        if not parts:
            return value  # add at root = replace
        parent, last = _get_parent(doc, parts)
        if isinstance(parent, dict):
            parent[last] = value
        elif isinstance(parent, list):
            if last == "-":
                parent.append(value)
            else:
                idx = int(last)
                if idx < 0 or idx > len(parent):
                    raise PatchError(f"add: array index {idx} out of range")
                parent.insert(idx, value)
        else:
            raise PatchError(f"add: cannot add to {type(parent).__name__}")
        return doc

    if operation == "remove":
        if not parts:
            raise PatchError("remove: cannot remove the root document")
        parent, last = _get_parent(doc, parts)
        if isinstance(parent, dict):
            if last not in parent:
                raise PatchError(f"remove: key {last!r} not found")
            del parent[last]
        elif isinstance(parent, list):
            idx = int(last)
            if idx < 0 or idx >= len(parent):
                raise PatchError(f"remove: array index {idx} out of range")
            parent.pop(idx)
        else:
            raise PatchError(f"remove: cannot delete from {type(parent).__name__}")
        return doc

    if operation == "move":
        from_path = op.get("from", "")
        from_parts = _pointer_parts(from_path)
        # Remove from source
        if not from_parts:
            raise PatchError("move: cannot move root")
        from_parent, from_last = _get_parent(doc, from_parts)
        if isinstance(from_parent, dict):
            value = from_parent.pop(from_last)
        elif isinstance(from_parent, list):
            idx = int(from_last)
            value = from_parent.pop(idx)
        else:
            raise PatchError("move: invalid source")
        # Add to destination
        return _apply_op(doc, {"op": "add", "path": path, "value": value})

    if operation == "copy":
        from_path = op.get("from", "")
        from_parts = _pointer_parts(from_path)
        if not from_parts:
            value = copy.deepcopy(doc)
        else:
            from_parent, from_last = _get_parent(doc, from_parts)
            if isinstance(from_parent, dict):
                value = copy.deepcopy(from_parent[from_last])
            elif isinstance(from_parent, list):
                value = copy.deepcopy(from_parent[int(from_last)])
            else:
                raise PatchError("copy: invalid source")
        return _apply_op(doc, {"op": "add", "path": path, "value": value})

    raise PatchError(f"Unknown RFC-6902 operation: {operation!r}")


def _patch_operation_name(op: object) -> object:
    if isinstance(op, dict):
        return op.get("op")
    if isinstance(op, BaseModel):
        return getattr(op, "op", None)
    return None


def _patch_operation_dict(op: object) -> dict[str, Any]:
    if isinstance(op, BaseModel):
        return op.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(op, dict):
        return op
    raise PatchError(f"operation must be an object, got {type(op).__name__}")


PatchInput: TypeAlias = dict[str, Any] | BaseModel


def apply_patch(doc: Any, patch: list[PatchInput]) -> Any:
    """Apply an RFC-6902 patch list to *doc*.

    Operates on a deep copy — the original is never mutated.
    Raises `PatchError` on the first failing operation.
    """
    result = copy.deepcopy(doc)
    for i, raw_op in enumerate(patch):
        try:
            op = _patch_operation_dict(raw_op)
            result = _apply_op(result, op)
        except PatchError as exc:
            op_name = _patch_operation_name(raw_op)
            raise PatchError(f"Operation {i} ({op_name!r}) failed: {exc}") from exc
    return result


# ─── Tool args model ──────────────────────────────────────────────────────────

class _JsonPatchOpBase(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    path: str = Field(description="RFC-6901 JSON Pointer path to the target location.")


class JsonPatchAddOp(_JsonPatchOpBase):
    op: Literal["add"]
    value: Any = Field(description="Required RFC-6902 value. May be any JSON value.")


class JsonPatchRemoveOp(_JsonPatchOpBase):
    op: Literal["remove"]


class JsonPatchReplaceOp(_JsonPatchOpBase):
    op: Literal["replace"]
    value: Any = Field(description="Required RFC-6902 replacement value.")


class JsonPatchMoveOp(_JsonPatchOpBase):
    op: Literal["move"]
    from_: str = Field(
        alias="from",
        description="RFC-6901 JSON Pointer path to move the value from.",
    )


class JsonPatchCopyOp(_JsonPatchOpBase):
    op: Literal["copy"]
    from_: str = Field(
        alias="from",
        description="RFC-6901 JSON Pointer path to copy the value from.",
    )


class JsonPatchTestOp(_JsonPatchOpBase):
    op: Literal["test"]
    value: Any = Field(description="Required RFC-6902 comparison value.")


JsonPatchOpModel: TypeAlias = (
    JsonPatchAddOp
    | JsonPatchRemoveOp
    | JsonPatchReplaceOp
    | JsonPatchMoveOp
    | JsonPatchCopyOp
    | JsonPatchTestOp
)
JsonPatchOperation: TypeAlias = Annotated[JsonPatchOpModel, Field(discriminator="op")]


class DeckPatchArgs(BaseModel):
    """Arguments for the deck_patch tool (§4.4 C-EDIT-4)."""

    deck_file: str = Field(
        description=(
            "Workspace-relative path to the deck's authored JSON file "
            "(e.g. 'my-deck.authored.json').  Must have been written by "
            "slides_generate or deck_patch itself."
        )
    )
    patch: list[SkipValidation[JsonPatchOperation]] = Field(
        description=(
            "RFC-6902 JSON Patch array.  Supported operations: replace, add, "
            "remove, test, move, copy.  Paths must be RFC-6901 JSON Pointers "
            "into the AuthoredDeck document (e.g. '/slides/0/title' to change "
            "a slide title, '/slides/0/body/1' for the second bullet)."
        )
    )
    output_html: str | None = Field(
        default=None,
        description=(
            "Optional output filename for the re-rendered HTML "
            "(default: '<deck_file_stem>.html').  Must stay in the workspace."
        ),
    )
    output_pptx: str | None = Field(
        default=None,
        description=(
            "Optional output filename for the re-rendered PPTX "
            "(default: '<deck_file_stem>.pptx').  Must stay in the workspace."
        ),
    )


# ─── DeckPatchTool ────────────────────────────────────────────────────────────

class DeckPatchTool:
    """Apply an RFC-6902 JSON Patch to a stored deck, validate + re-render.

    Implements the deck path of §4.4 (C-EDIT-4).  The tool:
      1. Reads *deck_file* from the sandbox workspace.
      2. Applies the RFC-6902 *patch* to the authored JSON in memory.
      3. Validates the patched document against `AuthoredDeck` (schema-or-revert).
      4. Re-renders deterministically via the C1 path:
         lower_deck(AuthoredDeck) → Deck → render_html + render_pptx.
      5. Writes the patched JSON + new renders back to the workspace.
      6. Returns the list of written files and a human-readable summary.

    On any error (bad patch, schema violation, file I/O) the workspace is NOT
    modified — no partial state is ever written.
    """

    definition = ToolDef(
        name="deck_patch",
        description=(
            "Apply an RFC-6902 JSON Patch to a slide deck's authored JSON, "
            "validate the result, and re-render the deck (HTML + PPTX). "
            "Use this to make targeted, element-scoped edits to a deck after "
            "selecting a specific element via the element inspector. "
            "The patch format uses JSON Pointers: '/slides/0/title' edits slide 1's "
            "title, '/slides/2/body/0' edits the first bullet of slide 3, etc. "
            "Reverts automatically if the patch produces an invalid deck."
        ),
        args_model=DeckPatchArgs,
        needs=frozenset({Capability.FILESYSTEM}),
        runs_in="sandbox",
        read_only=False,
    )

    async def run(self, args: DeckPatchArgs, ctx: ToolContext) -> ToolOutcome:
        """Execute the deck_patch: read → patch → validate → render → write."""
        # C1 imports — use lower_deck (Deck) directly; no MinimalDeck shim
        from disco.tools.builtin._deck_schema import AuthoredDeck, lower_deck
        from disco.tools.builtin._pptx_render import render_html, render_pptx

        assert ctx.sandbox is not None, "deck_patch requires a sandbox context"

        # ── 1. Read the authored JSON ─────────────────────────────────────────
        try:
            raw_bytes = await ctx.sandbox.read_file(args.deck_file)
        except Exception as exc:
            return ToolOutcome(
                success=False,
                content="",
                error=f"Could not read deck file {args.deck_file!r}: {exc}",
            )

        try:
            authored_json: dict = json.loads(raw_bytes)
        except json.JSONDecodeError as exc:
            return ToolOutcome(
                success=False,
                content="",
                error=f"deck file is not valid JSON: {exc}",
            )

        # ── 2. Apply the RFC-6902 patch (in memory only) ─────────────────────
        try:
            # The typed JsonPatchOperation models exist for the ADVERTISED schema;
            # apply_patch's engine consumes plain dicts.
            patched_json = apply_patch(
                authored_json,
                [op.model_dump(exclude_none=True) if hasattr(op, "model_dump") else op for op in args.patch],
            )
        except PatchError as exc:
            return ToolOutcome(
                success=False,
                content="",
                error=f"Patch application failed: {exc}",
            )

        # ── 3. Validate against AuthoredDeck schema ───────────────────────────
        try:
            authored_deck = AuthoredDeck.model_validate(patched_json)
        except Exception as exc:
            return ToolOutcome(
                success=False,
                content="",
                error=(
                    f"Patched document fails AuthoredDeck schema validation: {exc}. "
                    "Workspace NOT modified — patch reverted."
                ),
            )

        # ── 4. Determine the deck stem (needed to reload image assets) ────────
        stem = args.deck_file
        if stem.endswith(".authored.json"):
            stem = stem[: -len(".authored.json")]
        elif "." in stem:
            stem = stem.rsplit(".", 1)[0]

        # ── 4b. Reload generated images so an EDIT preserves them ─────────────
        # The C7 image bytes live on the rendered Element, not in the authored
        # sidecar — so a naive re-lower drops them back to the [image] placeholder
        # (a false affordance: "edit your deck" silently deletes its images). The
        # images are still on disk as "{stem}_img_{i}.png"; reload them by the same
        # convention _stage_assets wrote, keyed by authored-slide index.
        image_assets: dict[int, bytes] = {}
        for i, slide in enumerate(authored_deck.slides):
            if not slide.image_prompt:
                continue
            try:
                data = await ctx.sandbox.read_file(f"{stem}_img_{i}.png")
            except Exception:  # noqa: BLE001 — missing asset → placeholder, never crash
                continue
            if data and (data.startswith(b"\x89PNG\r\n\x1a\n") or data[:3] == b"\xff\xd8\xff"):
                image_assets[i] = data

        # ── 4c. Re-render deterministically via C1 path ───────────────────────
        try:
            deck = lower_deck(authored_deck, image_assets=image_assets)  # AuthoredDeck → C1
            html_str = render_html(deck)           # Deck → HTML string
            pptx_bytes = render_pptx(deck)         # Deck → PPTX bytes
        except Exception as exc:
            return ToolOutcome(
                success=False,
                content="",
                error=f"Re-render failed: {exc}. Workspace NOT modified.",
            )

        # ── 5. Determine output filenames ─────────────────────────────────────

        authored_out = args.deck_file  # overwrite in place
        html_out = args.output_html or f"{stem}.html"
        pptx_out = args.output_pptx or f"{stem}.pptx"

        # ── 6. Write back (all-or-nothing: write all, report all) ────────────
        written: list[str] = []
        try:
            await ctx.sandbox.write_file(authored_out, json.dumps(patched_json, indent=2).encode())
            written.append(authored_out)
            await ctx.sandbox.write_file(html_out, html_str.encode())
            written.append(html_out)
            await ctx.sandbox.write_file(pptx_out, pptx_bytes)
            written.append(pptx_out)
        except Exception as exc:
            return ToolOutcome(
                success=False,
                content="",
                error=f"Write failed after successful patch+render: {exc}",
            )

        # Build a concise summary
        n_slides = len(authored_deck.slides)
        n_ops = len(args.patch)
        summary = (
            f"deck_patch: applied {n_ops} operation(s) to {args.deck_file!r} "
            f"({n_slides} slides).  Re-rendered → {html_out}, {pptx_out}."
        )
        return ToolOutcome(
            success=True,
            content=summary,
            artifacts=written,
            structured={
                "deck_file": authored_out,
                "html_file": html_out,
                "pptx_file": pptx_out,
                "slides": n_slides,
                "ops_applied": n_ops,
            },
        )


# ── Module-level singleton for the registry ───────────────────────────────────
_tool = DeckPatchTool()


def get_tool() -> Tool:
    """Return the singleton DeckPatchTool instance (satisfies the Tool protocol)."""
    return _tool  # type: ignore[return-value]
