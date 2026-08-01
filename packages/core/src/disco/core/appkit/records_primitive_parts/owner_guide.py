"""OWNER_GUIDE.md emitters for the records primitive (base + auth variants).

Extracted verbatim from ``records_primitive.py`` to reduce module size; the
parent module re-imports every name here unchanged (see its module docstring).
"""

from __future__ import annotations

from ..spec import AppSpec


def _emit_records_owner_guide_md(app: AppSpec, db_name: str) -> str:
    from ..generator import _html_text

    app_name = _html_text(app.name)
    return (
        f"# Deploy guide - {app_name}\n"
        "\n"
        "This is a Cloudflare-ready records app: a Vite/React SPA plus a Worker with\n"
        "public POST inserts and Bearer-gated GET list endpoints for each entity.\n"
        "\n"
        "## Verify locally\n"
        "\n"
        "```sh\n"
        "cp .dev.vars.example .dev.vars\n"
        "npm install\n"
        "npm run build\n"
        "npm run db:local\n"
        "npm run cf:dev\n"
        "```\n"
        "\n"
        "The generated `schema.sql` and `migrations/0001_init.sql` contain the same\n"
        "FK-ordered init schema. Public inserts use `/api/<table>`; list reads use the\n"
        "same path with `Authorization: Bearer <ADMIN_TOKEN>`.\n"
        "\n"
        "## Deploy\n"
        "\n"
        "```sh\n"
        f"npx wrangler d1 create {db_name}\n"
        f"npx wrangler d1 execute {db_name} --remote --file=./schema.sql\n"
        "npx wrangler secret put ADMIN_TOKEN\n"
        "npm run build\n"
        "npx wrangler deploy\n"
        "```\n"
    )


def _emit_records_auth_owner_guide_md(app: AppSpec, db_name: str) -> str:
    from ..generator import _html_text

    app_name = _html_text(app.name)
    first_role = app.roles[0]
    return (
        f"# Deploy guide - {app_name}\n"
        "\n"
        "This is a Cloudflare-ready records app with per-user sessions and RBAC.\n"
        "All record reads and writes require login; per-entity read/write roles narrow\n"
        "access further.\n"
        "\n"
        "## Verify locally\n"
        "\n"
        "```sh\n"
        "cp .dev.vars.example .dev.vars\n"
        "npm install\n"
        "npm run build\n"
        "npm run db:local\n"
        "npm run cf:dev\n"
        "```\n"
        "\n"
        "Create users through `POST /api/register` with `Authorization: Bearer\n"
        "<ADMIN_TOKEN>`. The first declared role is the bootstrap/admin role:\n"
        f" `{first_role}`.\n"
        "\n"
        "## Authorization scope\n"
        "\n"
        "This scaffold enforces role-based route access, such as approver-only\n"
        "approval writes. It does not yet enforce per-row ownership: any\n"
        "authenticated user can read or insert on entities without role gates, and\n"
        "foreign-key fields are trusted from the request body. For multi-tenant use,\n"
        "add ownership checks that link users to records and derive owner fields from\n"
        "the authenticated session.\n"
        "\n"
        "## Deploy\n"
        "\n"
        "```sh\n"
        f"npx wrangler d1 create {db_name}\n"
        f"npx wrangler d1 execute {db_name} --remote --file=./schema.sql\n"
        "npx wrangler secret put ADMIN_TOKEN\n"
        "npm run build\n"
        "npx wrangler deploy\n"
        "```\n"
    )
