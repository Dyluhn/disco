"""Shared path/config constants for the AppKit strict verifier + its
collaborators (`SandboxReader`, `PreviewLifecycle`, the `checks` batteries,
and the sealed-output identity hash). Extracted from `verify_appkit_app.py`
verbatim (values unchanged) — this is data, not logic, so the split here is
purely about module size, not behavior. The parent facade
(`verify_appkit_app.py`) re-imports the names its own public surface + tests
require unchanged.
"""

from __future__ import annotations

from disco.core.appkit import CF_EXPORT_FILES, STATIC_CF_EXPORT_FILES

_SCHEMA_RELPATH = "schema.sql"
_DRIZZLE_SCHEMA_RELPATH = "src/db/schema.ts"
_PACKAGE_RELPATH = "package.json"
_WORKER_RELPATH = "worker/index.ts"
_COMPONENTS_DIR = "src/components"
_VITE_CONFIG_RELPATH = "vite.config.ts"
APPKIT_VITE_PACKAGE_SHA_RELPATH = ".disco/appkit-vite-package.sha256"
_VITE_BUILD_TIMEOUT_S = 300
_SEALED_OUTPUT_MAX_FILES = 1024
_SEALED_OUTPUT_MAX_FILE_BYTES = 16 * 1024 * 1024
_SEALED_OUTPUT_MAX_TOTAL_BYTES = 64 * 1024 * 1024
APPKIT_LIVE_PREVIEW_NAME = "appkit-live-vite"
_BUILT_PREVIEW_NAME = "appkit-built-vite"
_VITE_PREVIEW_COMMAND = "npx vite preview --host 0.0.0.0 --port {port} --strictPort"
# Where app_add_primitive persists applied-primitive provenance records (keep in
# sync with app_kit._PRIMITIVES_RELDIR) — the A3.2 enforcement input.
_PRIMITIVES_RELDIR = ".disco/primitives"
# The fixed path set the pre-dispatch verifier read straight from the sandbox for
# its lead-gen/directory check bundles. ALWAYS folded into the verify tree so the
# ported (now-pure) checks see exactly what those direct reads saw — even when the
# app/design specs are missing/unreadable (the run-app_create-first failures) or a
# file exists on disk without appearing in the current projection.
_LEGACY_TREE_PATHS: tuple[str, ...] = tuple(
    dict.fromkeys(
        (
            _SCHEMA_RELPATH,
            _DRIZZLE_SCHEMA_RELPATH,
            _PACKAGE_RELPATH,
            _WORKER_RELPATH,
            "src/api/client.ts",
            "src/hooks/useSubmit.ts",
            *CF_EXPORT_FILES,
            *STATIC_CF_EXPORT_FILES,
            ".dev.vars",
        )
    )
)
