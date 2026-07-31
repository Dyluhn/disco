"""Shared constants, type aliases, and compiled patterns for the Cloudflare deploy
planner/executor.

Extracted from ``deploy.py`` to reduce module complexity; the public facade
re-imports the names that are part of the compatibility surface (``CF_TOKEN_SECRET``
/ ``CF_ACCOUNT_SECRET`` are genuinely public; everything else here is private and
consumed only by sibling ``deploy_parts`` modules).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

DeployMutationHook = Callable[[], Awaitable[None]]

_HOST_TOKEN_LIFECYCLE_MUTATIONS = frozenset(
    {
        "host_token_candidate_mint_attempted",
        "host_token_candidate_minted",
        "host_token_rotation_finish_attempted",
        "host_token_rotation_finished",
        "host_token_candidate_revoke_attempted",
        "host_token_candidate_revoked",
    }
)

# ---- credential slots --------------------------------------------------------

#: SecretStore name for the encrypted Cloudflare API token (O1). NEVER plaintext
#: on disk; NEVER logged. Least-privilege scope documented in the OWNER guide:
#: Account "Workers Scripts:Edit" + "D1:Edit" only — no Zone/DNS/Routes.
CF_TOKEN_SECRET = "DISCO_CLOUDFLARE_API_TOKEN"
#: SecretStore name for the account id. Not itself a secret, but stored in the
#: same keyed store so a connection is one decryptable unit (and so we never
#: touch the shared ConfigStore from here).
CF_ACCOUNT_SECRET = "DISCO_CLOUDFLARE_ACCOUNT_ID"

#: Env var names wrangler reads. The token is injected here at execute time only.
_WRANGLER_TOKEN_ENV = "CLOUDFLARE_API_TOKEN"
_WRANGLER_ACCOUNT_ENV = "CLOUDFLARE_ACCOUNT_ID"

#: The ONLY base env vars passed through to the TOKEN-BEARING wrangler subprocess
#: (SEC-32 — deploy env minimization). The server's full env (with its secrets) is
#: NEVER handed to a deploy subprocess; only this minimal, non-sensitive allowlist
#: plus the CF token + a sanitised PATH + a throwaway HOME (overlaid via
#: ``_deploy_env``) reaches the wrangler steps. The UNTRUSTED ``npm run build`` no
#: longer runs as a host subprocess at all — it runs in an isolating sandbox (see
#: ``sandbox_build.SandboxBuildBackend``) with no host env / host FS, so a
#: malicious build script has nothing to exfiltrate.
#:
#: DELIBERATELY EXCLUDED from the token-bearing env (SEC-32): ``HOME`` (would let a
#: planted ``~/.npmrc`` / ``~/.wrangler`` redirect to host creds — overridden to a
#: throwaway dir instead), ``NODE_OPTIONS`` (``--require`` would PRELOAD arbitrary
#: JS into the token-bearing process), and ``npm_config_*`` (a poisoned registry /
#: cache could exfiltrate the token or inject code). ``PATH`` IS forwarded but is
#: SANITISED of any entry inside the workspace/staging tree so an attacker-planted
#: ``node_modules/.bin`` can never shadow a real tool.
_WRANGLER_ENV_ALLOWLIST: tuple[str, ...] = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TEMP",
    "TMP",
    "TERM",
    "SHELL",
    "USER",
    "LOGNAME",
    "NODE_ENV",
)

#: An operator-pinned, ABSOLUTE path to a trusted ``wrangler`` binary (SEC-3). When
#: set, it overrides PATH resolution entirely — the most robust way to guarantee the
#: deploy never runs a workspace-resolved ``wrangler``.
_WRANGLER_BIN_ENV = "DISCO_WRANGLER_BIN"

#: Operator/test override for the directory holding the cross-process deploy
#: lockfiles. MUST be a server-controlled path OUTSIDE the untrusted workspace.
_DEPLOY_LOCK_DIR_ENV = "DISCO_DEPLOY_LOCK_DIR"

#: Operator/test override for the PARENT directory the immutable per-deploy staging
#: trees are created under. The staging root determines the ANCESTOR chain
#: ``wrangler deploy`` walks UP from its cwd to discover a
#: ``.wrangler/deploy/config.json`` redirect, so it MUST be a server-controlled path
#: whose ancestors are verified clean. MUST live OUTSIDE the untrusted workspace.
#: Defaults to the system temp dir; an operator can point it at a path with
#: guaranteed-clean ancestors.
_DEPLOY_STAGE_DIR_ENV = "DISCO_DEPLOY_STAGE_DIR"

#: Workspace-relative AppSpec — the digest anchor (an app must exist to deploy).
_APPSPEC_RELPATH = ".disco/appspec.json"
#: Where the non-secret deployment records live (idempotency + teardown audit).
_DEPLOY_RECORD_DIR = ".disco/cloudflare/deployments"

#: The placeholder the generated ``wrangler.toml`` ships with — substituted with
#: the REAL D1 database_id (from ``wrangler d1 create``/adopt) before deploy (P1-3).
_D1_ID_PLACEHOLDER = "REPLACE_WITH_D1_DATABASE_ID"
#: A Cloudflare D1 database id is a UUID. Used to pull the real id out of the
#: ``d1 create`` / ``d1 list`` output so the placeholder can be substituted.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

#: Top-level subtrees + files the deploy-tree digest IGNORES (and that staging never
#: copies — :func:`_stage_deploy_tree` prunes the same set). ``node_modules`` and
#: ``.git`` are regenerated / non-authored; ``.disco`` is internal disco state
#: (deployment records + the appspec, which is digested separately so it stays
#: covered); ``.wrangler`` is wrangler's LOCAL state dir — it is NEVER an AppKit-authored
#: input, and it can carry a ``deploy/config.json`` REDIRECT that points wrangler at an
#: arbitrary alt config (the config-source bypass), so it is excluded from staging so a
#: redirect can't even be present (the alt-config guard ALSO refuses if one is found —
#: exclude + assert-absent, defence in depth); ``.dev.vars`` is the real LOCAL secret —
#: never deployed, and it must not bind the plan to secret content. EVERYTHING else the
#: build+deploy consumes is hashed.
_TREE_SKIP_DIRS = frozenset({"node_modules", ".git", ".disco", ".wrangler"})
_TREE_SKIP_FILES = frozenset({".dev.vars"})

# ---- SEC (config-source bypass): wrangler.toml is the SOLE config source ------

#: wrangler's local state DIR — never an AppKit-authored input. It can carry a
#: ``deploy/config.json`` REDIRECT (``{"configPath": "../../evil/wrangler.jsonc"}``) that
#: points ``wrangler deploy`` at an arbitrary config, so its mere presence is an injection.
_WRANGLER_STATE_DIR = ".wrangler"
#: ALTERNATE wrangler config FILE names (case-insensitive). ``wrangler deploy`` resolves
#: ``wrangler.json`` → ``wrangler.jsonc`` → ``wrangler.toml`` IN THAT ORDER, so a planted
#: ``wrangler.json``/``.jsonc`` is read BEFORE (and instead of) the gated ``wrangler.toml``.
_ALT_WRANGLER_CONFIG_NAMES = frozenset({"wrangler.json", "wrangler.jsonc"})
#: Regenerated/non-authored subtrees pruned from the alt-config scan so a DEPENDENCY's own
#: ``wrangler.json`` can't false-positive. ``.wrangler`` is DELIBERATELY NOT pruned — the
#: scan must flag it (it is excluded from STAGING separately, via ``_TREE_SKIP_DIRS``).
_ALT_CONFIG_SCAN_SKIP_DIRS = frozenset({"node_modules", ".git", ".disco"})

#: Heuristic: a ``[vars]`` key or ``.env`` assignment NAME that denotes a secret.
#: A secret value belongs in ``wrangler secret put`` (encrypted at the edge), NEVER
#: as a plaintext ``[vars]`` entry (public, build-time inlined) or a deployed
#: ``.env`` file.
_SECRET_NAME_RE = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIAL|"
    r"ACCESS[_-]?KEY|CLIENT[_-]?SECRET|AUTH)",
    re.IGNORECASE,
)

#: SEC-17: credential-bearing FILE shapes that must NEVER ship in a deploy tree — a
#: deployable-file DENYLIST (the conservative complement of an allowlist: it refuses the
#: high-confidence dangerous types without false-positiving on the open-ended set of
#: legitimate built web-asset types). Matched on the file NAME (basename), case-insensitive.
_DENY_DEPLOY_FILE_RE = re.compile(
    r"(?:^|[._-])(?:"
    # ``.env`` + ANY suffix variant — a STEM match, not a fixed suffix set: ``.env``,
    # ``.env.local``, ``.env.production.local``, ``.env.prod-1`` (digits/hyphens), … The
    # app reads secrets from CF bindings, so NO dotenv file ever belongs in the deploy
    # tree; a missed variant (``.env.production.local``) would otherwise ship as a PUBLIC
    # static asset (SEC-17 .env.production.local public-asset leak).
    r"env(?:\..*)?"
    # ``.dev.vars`` + ANY suffix (``.dev.vars.production``, ``.dev.vars.staging``, …) —
    # the real local secret, never deployed.
    r"|dev\.vars(?:\..*)?"
    r"|npmrc|netrc"  # registry / machine creds
    r"|id_rsa|id_dsa|id_ecdsa|id_ed25519"  # SSH private keys
    r")$|\.(?:pem|key|p12|pfx|ppk|jks|keystore|asc|gpg|pgp)$",
    re.IGNORECASE,
)

#: Conventional NON-secret placeholder/template suffixes: a file whose basename ENDS in
#: one of these (``.dev.vars.example``, ``.env.sample``, ``.env.dist``, …) is a committed
#: TEMPLATE with no real values — the canonical generated app ships a ROOT
#: ``.dev.vars.example`` — and is EXEMPT from the credential-file denylist. Real-value
#: variants (``.env.production.local``, ``.dev.vars.production``) carry no such marker and
#: stay denied. (Only relaxes the ``.env``/``.dev.vars`` stem branch: ``.pem``/``id_rsa``/…
#: match only when they are the FINAL segment, so a ``*.example`` name never reaches them.)
#:
#: SEC-17 (P1): this exemption is SCOPED — :func:`_assert_no_secret_shaped_files` honours it
#: ONLY OUTSIDE the served ``[assets].directory`` AND only after content-scanning the file
#: for a real secret value. A credential-named template INSIDE the served (PUBLIC) tree, or
#: ANY template carrying a real ``sk-…``/``cfut_…`` token, is still REFUSED — the suffix
#: alone never makes a credential-named public asset (or a token-bearing file) safe.
_DEPLOY_TEMPLATE_SUFFIX_RE = re.compile(
    r"\.(?:example|sample|template|tmpl|tpl|dist)$",
    re.IGNORECASE,
)

#: SEC-18: high-confidence secret-SHAPED VALUE patterns scanned across staged TEXT files
#: (beyond the ``.env`` / ``[vars]`` NAME-based check). These are provider-prefixed /
#: structurally-unmistakable tokens with ~zero false-positive rate, so a generated app's
#: ordinary content never trips them; a real key embedded in any deployed source/config
#: does. (Entropy-only heuristics are deliberately NOT used here — they would false-positive
#: on minified JS / hashes in built assets.)
_SECRET_VALUE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),  # PEM private key block
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),  # AWS temporary access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),  # GitHub PAT / token
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),  # GitLab PAT
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),  # Slack token
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),  # Anthropic key
    re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),  # OpenAI-style key
    re.compile(r"\bsk_live_[A-Za-z0-9]{20,}\b"),  # Stripe live secret key
    re.compile(r"\bcfut_[A-Za-z0-9]{20,}\b"),  # Cloudflare API token
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),  # Google API key
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # JWT
)

#: File extensions whose CONTENT is scanned for secret-shaped values. Text source/config
#: types only — binary assets (images/fonts) are skipped, and a basename match in
#: :data:`_DENY_DEPLOY_FILE_RE` is refused outright before content scanning.
_SCANNED_TEXT_SUFFIXES = frozenset(
    {
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".json",
        ".toml",
        ".yaml",
        ".yml",
        ".env",
        ".vars",
        ".txt",
        ".md",
        ".html",
        ".css",
        ".sql",
        ".sh",
        ".cfg",
        ".ini",
    }
)
#: Cap per-file scan size so a giant minified bundle / source map cannot turn the scan
#: into a DoS (a real key sits in the first chunk of any committed config/source anyway).
_SECRET_SCAN_MAX_BYTES = 2_000_000

#: SEC-5: the ONLY top-level wrangler.toml keys an AppKit deploy permits. Anything
#: else broadens the blast radius and is REFUSED before deploy: ``routes``/``route``
#: (custom domains / zone routes), ``build`` (a build hook running arbitrary code on
#: the host at deploy), ``triggers`` (cron), ``vars`` (public plaintext — secrets must
#: use ``wrangler secret put``), and every extra binding (``kv_namespaces``,
#: ``r2_buckets``, ``queues``, ``services``, ``durable_objects``, ``hyperdrive``, …).
#:
#: ``account_id`` is DELIBERATELY EXCLUDED (config-source bypass, P1): wrangler resolves
#: the CONFIG ``account_id`` when present, so a planted ``account_id =
#: "other-account-the-token-can-access"`` would target a DIFFERENT account than the owner
#: confirmed. The account comes ONLY from the confirmed SecretStore (injected as the
#: ``CLOUDFLARE_ACCOUNT_ID`` env at execute time) — never a config override. AppKit's
#: generated wrangler.toml carries no ``account_id``, so any is an injection → refuse.
_ALLOWED_WRANGLER_KEYS = frozenset(
    {
        "name",
        "main",
        "compatibility_date",
        "compatibility_flags",
        "assets",
        "run_worker_first",
        "d1_databases",
        "workers_dev",
        "observability",
        "minify",
        "no_bundle",
    }
)

#: SEC-11/CORR-9: a statement at the start of a schema.sql statement that would MUTATE
#: or destroy data on an ADOPTED (pre-existing, owner-owned) D1 database. Only idempotent
#: ``CREATE`` DDL is allowed against an adopted DB.
_DESTRUCTIVE_SQL_RE = re.compile(
    r"^(DROP|DELETE|TRUNCATE|ALTER|UPDATE|INSERT|REPLACE|PRAGMA|ATTACH|DETACH|VACUUM)\b",
    re.IGNORECASE,
)

# ---- SEC-10-B: server-controlled, SIGNED ownership records -------------------

#: Reserved SecretStore slot for the server-side HMAC key that SIGNS deploy/ownership
#: records (SEC-10-B). The key lives in the ENCRYPTED SecretStore — which is stored
#: OUTSIDE the untrusted workspace (``$XDG_CONFIG_HOME/disco`` / ``DISCO_SECRETS``) and is
#: unreadable by the sandboxed build agent — so a record's ownership claim cannot be
#: FORGED by planting a JSON file in the digest-SKIPPED ``.disco`` dir. A planted /
#: unsigned / foreign-keyed record fails signature verification and authorizes NOTHING;
#: only a record THIS server actually wrote (after a real successful mutation) carries a
#: valid signature.
_OWNERSHIP_HMAC_KEY_SECRET = "disco.appkit.ownership_hmac_key"  # noqa: S105 - a key NAME, not a secret

# ---- SEC-4: worker AUTH-semantics verification (pre-deploy, fail closed) ------

#: The generated Worker entry point — the file ``wrangler deploy`` bundles + publishes.
_WORKER_RELPATH = "worker/index.ts"

#: SEC-4 token-exfiltration scan: ``env.ADMIN_TOKEN`` must NEVER flow into a response
#: body / serialization SINK. Matches a sink opener (``new Response(`` / ``json(`` /
#: ``JSON.stringify(`` / a template-literal ``${…}`` interpolation) followed — within the
#: same expression (not crossing a ``;``) — by a direct ``env.ADMIN_TOKEN`` read. The
#: canonical Worker references ``env.ADMIN_TOKEN`` only as ``const expected =
#: env.ADMIN_TOKEN`` (no sink), so this catches a Worker that echoes the admin token to
#: the public edge while leaving the legitimate auth read untouched.
_ADMIN_TOKEN_LEAK_RE = re.compile(
    r"(?:new\s+Response\s*\(|(?<![\w.])json\s*\(|JSON\.stringify\s*\(|\$\{)"
    r"[^;]{0,400}?\benv\.ADMIN_TOKEN\b",
    re.DOTALL,
)

# ---- SEC-9: Cloudflare-safe resource-name validation -------------------------

#: A Cloudflare-safe Worker / D1 name: starts with an alphanumeric, then
#: alphanumerics / hyphens / underscores, at most 63 chars total. Cloudflare rejects
#: names outside this shape (and a leading hyphen / odd punctuation could be parsed as
#: a flag or collide); validate BEFORE plan confirmation so a bad name never reaches a
#: wrangler invocation.
_CF_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")

#: SEC-10-A: the ONLY nonzero ``wrangler deployments list`` outcomes we accept as a
#: TRUSTWORTHY "Worker absent" result — a DOCUMENTED Cloudflare not-found signal. Anything
#: else (auth/permission/transport failure, an old/!json-capable wrangler, a malformed
#: invocation) is AMBIGUOUS and must fail closed rather than be read as "no Worker exists".
#: Cloudflare error code 10007 is ``workers.api.error.script_not_found``; the textual
#: variants cover wrangler's human-readable not-found phrasings.
_WORKER_NOT_FOUND_RE = re.compile(
    r"(?:"
    r"workers\.api\.error\.script_not_found"
    r"|script_not_found"
    r"|\[code:\s*10007\]"
    r"|\bcode[:\s]+10007\b"
    r"|(?:script|worker)\b[^.\n]{0,16}?\bnot\s+found"
    r"|could\s+not\s+find\s+(?:a\s+)?(?:script|worker)"
    r")",
    re.IGNORECASE,
)
