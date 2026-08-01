"""Project scaffolding files shared by the lead-gen tree: package.json,
package-lock.json (pinned from the checked-in lock template), tsconfig.json,
vite.config.ts, the build manifest, .dev.vars.example, and .gitignore.

Split out of `..generator` (verbatim) to keep that module under the
`python_or_harness_module_logical_gt_700` budget. See `generator_parts/__init__.py`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..spec import AppSpec, DesignSpec
from .ids import _comp_name, _iter_sections, _slug, _ts


def _emit_package_json(app: AppSpec, db_name: str) -> str:
    pkg = {
        "name": _slug(app.name),
        "private": True,
        "version": "0.1.0",
        "type": "module",
        "scripts": {
            "dev": "vite",
            "build": "vite build",
            "preview": "vite preview",
            # Epic I — Cloudflare local/remote D1 init + local CF dev. `db:local`
            # applies schema.sql to the on-disk Miniflare D1 used by `wrangler dev`;
            # `db:remote` applies it to the deployed D1; `cf:dev` runs the Worker +
            # built assets locally (workerd) so the owner can verify before deploy.
            "db:local": f"wrangler d1 execute {db_name} --local --file=./schema.sql",
            "db:remote": f"wrangler d1 execute {db_name} --remote --file=./schema.sql",
            "cf:dev": "wrangler dev",
            "deploy": "wrangler deploy",
        },
        "dependencies": {
            "drizzle-orm": "^0.44.2",
            "react": "^18.3.1",
            "react-dom": "^18.3.1",
        },
        "devDependencies": {
            "@vitejs/plugin-react": "^4.3.1",
            "drizzle-kit": "^0.31.4",
            "typescript": "^5.5.4",
            "vite": "^5.4.2",
            # v4.20+ for the array form of assets.run_worker_first (see wrangler.toml).
            "wrangler": "^4.20.0",
        },
    }
    return json.dumps(pkg, indent=2, ensure_ascii=False) + "\n"


# `generator_parts/` lives one level BELOW `appkit/` (unlike the original
# single-file `generator.py`), so the lock template's path needs the extra
# `.parent` to land back on `appkit/lock_template/package-lock.json`.
_LOCK_TEMPLATE_PATH = Path(__file__).parent.parent / "lock_template" / "package-lock.json"
try:
    _LOCK_TEMPLATE = json.loads(_LOCK_TEMPLATE_PATH.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - package defect
    raise RuntimeError("AppKit dependency lock template is unavailable") from exc


def _emit_package_lock_json(app: AppSpec, *, package_json: str | None = None) -> str:
    """Emit the repository-pinned AppKit dependency graph for ``npm ci``.

    Only the root package identity varies per generated application; all
    resolved package versions and integrity hashes are checked-in data.  A
    specialized generated manifest may additionally narrow the root dependency
    sets; npm requires those root declarations to agree exactly with
    ``package.json`` even though the reviewed transitive graph remains shared.
    """
    lock = json.loads(json.dumps(_LOCK_TEMPLATE))
    name = _slug(app.name)
    if not isinstance(lock, dict) or not isinstance(lock.get("packages"), dict):
        raise RuntimeError("AppKit dependency lock template is invalid")
    root = lock["packages"].get("")
    if not isinstance(root, dict):
        raise RuntimeError("AppKit dependency lock template has no root package")
    if package_json is not None:
        try:
            package = json.loads(package_json)
        except json.JSONDecodeError as exc:  # pragma: no cover - generator defect
            raise RuntimeError("generated AppKit package manifest is invalid") from exc
        if not isinstance(package, dict):  # pragma: no cover - generator defect
            raise RuntimeError("generated AppKit package manifest is invalid")
        for field in ("dependencies", "devDependencies"):
            value = package.get(field)
            if value is None:
                root.pop(field, None)
            elif isinstance(value, dict):
                root[field] = value
            else:  # pragma: no cover - generator defect
                raise RuntimeError(f"generated AppKit package manifest has invalid {field}")
    lock["name"] = name
    root["name"] = name
    return json.dumps(lock, indent=2, ensure_ascii=False) + "\n"


def _emit_tsconfig() -> str:
    cfg = {
        "compilerOptions": {
            "target": "ES2020",
            "useDefineForClassFields": True,
            "lib": ["ES2020", "DOM", "DOM.Iterable"],
            "module": "ESNext",
            "skipLibCheck": True,
            "moduleResolution": "bundler",
            "jsx": "react-jsx",
            "strict": True,
            "noEmit": True,
        },
        "include": ["src", "worker"],
    }
    return json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"


def _emit_vite_config() -> str:
    return (
        'import { defineConfig } from "vite";\n'
        'import react from "@vitejs/plugin-react";\n\n'
        "export default defineConfig({\n"
        "  plugins: [react()],\n"
        '  build: { outDir: "dist" },\n'
        "});\n"
    )


def _emit_manifest_ts(app: AppSpec, design: DesignSpec, names: dict[tuple[str, str], str]) -> str:
    """A deterministic digest of BOTH specs + the section inventory. Any spec change
    (structure, content, or design) changes the digest, so the manifest is part of
    the touched set on every mutation — a cheap, single 'specs ⇄ tree are in sync'
    fingerprint the tool diff can rely on."""
    payload = json.dumps(
        {
            "app": app.model_dump(mode="json"),
            "design": design.model_dump(mode="json"),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    sections = [
        {"page": page.id, "section": section.id, "component": _comp_name(names, page, section)}
        for page, section in _iter_sections(app)
    ]
    return (
        "/* Auto-generated build manifest — a digest of the App + Design specs. */\n"
        f"export const SPEC_DIGEST = {_ts(digest)};\n"
        f"export const SECTIONS = {_ts(sections)} as const;\n"
    )


# ---- Cloudflare export deliverables (Epic I) ----------------------------------
#
# Epic E already emits worker/index.ts, schema.sql, and wrangler.toml. Epic I adds
# the CONFIG-COMPLETENESS + owner-deploy layer on top of that single generated tree:
# a secret TEMPLATE (never a real secret), a .gitignore that keeps the real secret
# out of git, and an OWNER_GUIDE that documents the exact local-verify + deploy flow
# the owner runs (Disco never deploys). All deterministic, all design_lint-inert
# (none of these extensions are scanned by the linter).


def _emit_dev_vars_example() -> str:
    """`.dev.vars.example` — the template the owner copies to `.dev.vars` (gitignored)
    so `wrangler dev` has the admin secret locally. Carries NO real secret: ADMIN_TOKEN
    is a placeholder. `.dev.vars` itself is NEVER generated (a real secret must never
    land in the export tree); the verifier fails if a real `.dev.vars` is present."""
    return (
        "# Cloudflare local dev secrets — TEMPLATE ONLY (no real secret here).\n"
        "# Copy this file to .dev.vars (which .gitignore excludes — NEVER commit it) and\n"
        "# replace the value. `wrangler dev` loads .dev.vars to inject secrets locally;\n"
        "# in production set the SAME secret with `wrangler secret put ADMIN_TOKEN`.\n"
        "#\n"
        "# ADMIN_TOKEN gates the admin read-back (GET /api/leads and /admin). Until it is\n"
        "# set the Worker FAILS CLOSED — every read is denied — so leads are never exposed.\n"
        "ADMIN_TOKEN=replace-me\n"
    )


def _emit_gitignore() -> str:
    """A `.gitignore` that keeps build output, dependencies, and — critically — the
    real `.dev.vars` secret file out of version control. `.dev.vars.example` (the
    template) is intentionally NOT ignored so the contract is documented in git."""
    return (
        "# Dependencies\n"
        "node_modules/\n"
        "\n"
        "# Build output\n"
        "dist/\n"
        "\n"
        "# Cloudflare\n"
        ".wrangler/\n"
        "\n"
        "# Local secrets — NEVER commit real secrets. The .dev.vars.example template\n"
        "# IS committed (it documents the contract); the real .dev.vars is not.\n"
        ".dev.vars\n"
    )
