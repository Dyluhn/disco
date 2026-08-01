"""`SandboxReader` — the file-access collaborator for `verify_appkit_app`.

Loading the AppSpec/DesignSpec, assembling the on-disk verify tree, and the
raw byte/text/existence probes are sandbox IO, not verification logic. This
class holds NO verification state; it is a plain collaborator the tool HOLDS
(`self._sandbox = SandboxReader()`) and calls through
(`self._sandbox.read_text(ctx, relpath)`), never a mixin. Extracted from
`VerifyAppKitAppTool` verbatim (logic unchanged).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from disco.core.appkit import (
    APPSPEC_RELPATH,
    DESIGNSPEC_RELPATH,
    generate,
    load_app_spec_from_bytes,
    load_design_spec_from_bytes,
)
from disco.core.appkit.primitives import required_security_primitives
from disco.core.appkit.spec import AppSpec, DesignSpec

from .constants import _COMPONENTS_DIR, _LEGACY_TREE_PATHS, _PRIMITIVES_RELDIR

if TYPE_CHECKING:
    from ...anatomy import ToolContext


class SandboxReader:
    """File-access collaborator: everything that reads spec/tree bytes from the
    sandbox, isolated from verification logic."""

    async def load_app(self, ctx: ToolContext) -> AppSpec | None:
        data = await self.read_bytes(ctx, APPSPEC_RELPATH)
        if data is None:
            return None
        try:
            return load_app_spec_from_bytes(data)
        except Exception:  # noqa: BLE001 — invalid spec → treated as absent (checks fail cleanly)
            return None

    async def load_design(self, ctx: ToolContext) -> DesignSpec | None:
        data = await self.read_bytes(ctx, DESIGNSPEC_RELPATH)
        if data is None:
            return None
        try:
            return load_design_spec_from_bytes(data)
        except Exception:  # noqa: BLE001 — invalid spec → treated as absent (checks fail cleanly)
            return None

    async def assemble_tree(
        self, ctx: ToolContext, app: AppSpec | None, design: DesignSpec | None
    ) -> dict[str, str]:
        """The relpath → ON-DISK text tree the primitive verify hooks inspect (a
        missing file is an ABSENT key). You are verifying DISK, not the projection:
        when app AND design are loadable, `generate(app, design)` supplies the PATH
        SET and each path's CONTENT is read from the sandbox. The legacy fixed path
        set (the lead-gen/directory reads the pre-dispatch tool performed directly)
        is ALWAYS included so the ported checks see exactly what the old sandbox
        reads saw — including the run-app_create-first failures when app/design are
        missing — plus every on-disk `src/components/*.tsx` (components may be
        model-edited beyond the projection)."""
        assert ctx.sandbox is not None
        paths: set[str] = set(_LEGACY_TREE_PATHS)
        paths.add(APPSPEC_RELPATH)
        if app is not None and design is not None:
            try:
                paths.update(generate(app, design))
            except Exception:  # noqa: BLE001 — a projection failure must not kill verify
                pass  # the legacy path set below still drives the ported checks
        for security_prim in required_security_primitives(app):
            paths.add(f"{_PRIMITIVES_RELDIR}/{security_prim.id}.json")
        try:
            names = await ctx.sandbox.list_dir(_COMPONENTS_DIR)
        except Exception:  # noqa: BLE001 — no components dir
            names = []
        paths.update(f"{_COMPONENTS_DIR}/{name}" for name in names if name.endswith(".tsx"))
        tree: dict[str, str] = {}
        for path in sorted(paths):
            text = await self.read_text(ctx, path)
            if text is not None:
                tree[path] = text
        return tree

    async def read_bytes(self, ctx: ToolContext, relpath: str) -> bytes | None:
        assert ctx.sandbox is not None
        try:
            if not await ctx.sandbox.file_exists(relpath):
                return None
            return await ctx.sandbox.read_file(relpath)
        except Exception:  # noqa: BLE001 — unreadable → absent
            return None

    async def read_text(self, ctx: ToolContext, relpath: str) -> str | None:
        data = await self.read_bytes(ctx, relpath)
        return data.decode("utf-8", errors="replace") if data is not None else None

    async def file_exists(self, ctx: ToolContext, relpath: str) -> bool:
        assert ctx.sandbox is not None
        try:
            return bool(await ctx.sandbox.file_exists(relpath))
        except Exception:  # noqa: BLE001 — absence probe must never raise
            return False

    async def dir_exists(self, ctx: ToolContext, relpath: str) -> bool:
        assert ctx.sandbox is not None
        try:
            await ctx.sandbox.list_dir(relpath)
            return True
        except Exception:  # noqa: BLE001 — absent or not a directory
            return False
