"""`OWNER_GUIDE.md` for the lead-gen primitive — owner-run deploy INSTRUCTIONS.

Split out of `..generator` (verbatim) to keep that module under the
`python_or_harness_module_logical_gt_700` budget, and to bring
`_emit_owner_guide_md` under the 100-logical-line callable cap. See
`generator_parts/__init__.py` for the byte-identity contract this split must
hold: every helper below is a straight, in-order cut of the ORIGINAL single
string concatenation — no character added, removed, or reordered.
Byte-identity is verified externally (hash comparison against the pre-split
generator), not by a test in this tree.
"""

from __future__ import annotations

from ..spec import AppSpec, Entity
from .ids import _html_text


def _owner_guide_intro(app_name: str, db_name: str) -> str:
    return (
        f"# Deploy guide — {app_name}\n"
        "\n"
        "This is a Cloudflare-ready lead-gen app: a Vite/React SPA served by Cloudflare\n"
        "Workers Static Assets, with a Worker (`worker/index.ts`) that captures leads\n"
        "into a D1 database and serves an auth-gated admin read-back.\n"
        "\n"
        "**Disco generated and LOCALLY verified this app** (design-clean; valid D1\n"
        "schema; a structurally-correct, parameterized, fail-closed lead/admin worker\n"
        "contract). Disco does **not** deploy — deploying to your Cloudflare account is\n"
        "the steps below, which **you** run. Nothing here sends data anywhere until you\n"
        "do.\n"
        "\n"
        "## What Disco verified locally vs what you deploy\n"
        "\n"
        "- **Verified locally (no Cloudflare account needed):** the design lint is clean;\n"
        "  `schema.sql` is valid SQLite that round-trips a lead row and enforces NOT NULL;\n"
        "  the Worker's structure enforces the lead/admin contract (public POST insert is\n"
        "  parameterized; reads are Bearer-gated and fail closed when `ADMIN_TOKEN` is\n"
        "  unset); the SPA routes and sections render.\n"
        "- **Drizzle schema layer:** `src/db/schema.ts` is the typed Drizzle source of\n"
        "  truth used by the Worker; `schema.sql` remains the D1 migration applied by\n"
        "  `wrangler d1 execute`. Both are generated from the same lead entity, so the\n"
        "  typed schema and migration stay in sync by construction.\n"
        "- **You deploy:** create the real D1 database, load the schema, set the\n"
        "  `ADMIN_TOKEN` secret, build the SPA, and publish the Worker.\n"
        "\n"
        "Actual end-to-end runtime behaviour on Cloudflare's edge is only proven once you\n"
        "run the local CF emulation (`npm run cf:dev`) and/or deploy — see below.\n"
        "\n"
        "## Prerequisites\n"
        "\n"
        "- A [Cloudflare account](https://dash.cloudflare.com/sign-up) (the free plan\n"
        "  covers Workers + D1).\n"
        "- [Node.js](https://nodejs.org/) 18+ and npm.\n"
        "- Wrangler (installed as a dev dependency): `npm install`, then `npx wrangler\n"
        "  login` to authenticate.\n"
        "\n"
        "## 1. Create the D1 database\n"
        "\n"
        "```sh\n"
        f"npx wrangler d1 create {db_name}\n"
        "```\n"
        "\n"
        "Copy the printed `database_id` into `wrangler.toml`, replacing\n"
        "`REPLACE_WITH_D1_DATABASE_ID`.\n"
        "\n"
    )


def _owner_guide_verify_and_deploy(db_name: str) -> str:
    return (
        "## 2. Verify locally with the Cloudflare emulator (recommended before deploy)\n"
        "\n"
        "This runs the real Worker + built assets against a local Miniflare D1 — the\n"
        "closest you can get to production without deploying.\n"
        "\n"
        "```sh\n"
        "cp .dev.vars.example .dev.vars     # then edit ADMIN_TOKEN in .dev.vars\n"
        "npm install\n"
        "npm run build                      # build the SPA into ./dist\n"
        "npm run db:local                   # apply schema.sql to the local D1\n"
        "npm run cf:dev                     # serve the Worker + assets locally\n"
        "```\n"
        "\n"
        "With `wrangler dev` running, smoke-test the lead flow:\n"
        "\n"
        "```sh\n"
        "# public lead submission → 201\n"
        "curl -i -X POST http://localhost:8787/api/leads \\\n"
        '  -H "Content-Type: application/json" \\\n'
        '  -d \'{"name":"Ada","email":"ada@example.com","message":"hi"}\'\n'
        "\n"
        "# admin read-back WITHOUT the token → 401 (fails closed)\n"
        "curl -i http://localhost:8787/api/leads\n"
        "\n"
        "# admin read-back WITH the token → 200 + the lead\n"
        'curl -i http://localhost:8787/api/leads -H "Authorization: Bearer <ADMIN_TOKEN>"\n'
        "```\n"
        "\n"
        "## 3. Deploy to Cloudflare\n"
        "\n"
        "```sh\n"
        f"npx wrangler d1 execute {db_name} --remote --file=./schema.sql  # load the schema\n"
        "npx wrangler secret put ADMIN_TOKEN                 # set the admin read-back secret\n"
        "npm run build                                       # build the SPA\n"
        "npx wrangler deploy                                 # publish the Worker + assets\n"
        "```\n"
        "\n"
        "After deploy, visit `/admin` on your `*.workers.dev` URL (or your custom domain)\n"
        "and enter the `ADMIN_TOKEN` to view captured leads.\n"
        "\n"
    )


def _owner_guide_secrets() -> str:
    return (
        "## Secrets, rotation, and rollback\n"
        "\n"
        "- **Never commit secrets.** `.dev.vars` (your real local secret) is gitignored;\n"
        "  only `.dev.vars.example` (the placeholder template) belongs in git. The\n"
        "  production secret lives only in Cloudflare (`wrangler secret put`).\n"
        "- **Rotate** the admin token by re-running `wrangler secret put ADMIN_TOKEN`\n"
        "  with a new value; the old token stops working immediately.\n"
        "- **Roll back** a bad deploy from the Cloudflare dashboard\n"
        "  (Workers & Pages → your Worker → Deployments → roll back) or by re-running\n"
        "  `npx wrangler deploy` from a known-good checkout.\n"
        "- If `ADMIN_TOKEN` is ever unset, the Worker denies all reads (fail closed) —\n"
        "  set it again to restore admin access. Public lead submission is unaffected.\n"
    )


def _emit_owner_guide_md(app: AppSpec, lead: Entity, db_name: str) -> str:
    """`OWNER_GUIDE.md` — owner-run deploy INSTRUCTIONS (Epic I3). This is documentation,
    NOT execution: Disco generates + locally verifies the app, but the owner runs every
    Cloudflare command. It states what was verified locally vs what the owner deploys,
    the prerequisites, the local-CF verify path, the remote deploy path, and the secret/
    rollback notes. Deterministic for a given (app, lead)."""
    app_name = _html_text(app.name)
    return (
        _owner_guide_intro(app_name, db_name)
        + _owner_guide_verify_and_deploy(db_name)
        + _owner_guide_secrets()
    )
