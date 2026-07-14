"""`release_declare` — record the TYPED, NAMES-ONLY runtime release intent (WO-5).

Every build mode can DECLARE how its app builds and runs — the start / build argv
LISTS, the ``$PORT``-contract env-var NAME, an optional health path, the required
env-var NAMES, and the stateful resources it needs — as a typed ``ReleaseIntent``
(``disco.core.release.spec``). The host persists it as a ``release-intent.json``
sidecar NEXT TO ``manifest.json``, OUTSIDE the ``workspace/`` tree, so the record
is HOST-OWNED: a workspace file may HINT at release shape, but can never
self-assert this record, and detection (WO-3) consumes the typed intent, not raw
workspace guesses.

Two guarantees make the record safe to consume downstream:

* NAMES ONLY — the payload is value-free BY CONSTRUCTION. ``required_env`` /
  ``port_env`` are env-var NAMES; a ``NAME=value`` smuggle (or a lowercase /
  ``=`` / whitespace / leading-digit name) is REJECTED by ``ReleaseIntent``'s
  validators, and the args schema cannot even EXPRESS an env-VALUE object. No
  secret value is ever accepted, persisted, or echoed back — a rejection names
  the offending FIELD, never the value.
* A CANDIDATE, NOT A VERDICT — the intent is a candidate INPUT to release
  detection, never an assessment / verification claim; the success text says so.

Commands are ARGV LISTS (``["node", "server.js"]``), never shell strings: the
args schema types them as ``list[str]``, so a shell string is structurally
rejected before it ever reaches persistence.

Host resolution (WO-C1): the sidecar is HOST state under the projects root, which
the sandbox does not own. NO sandbox backend exposes the host projects root — the
process (dev) backend's ``workspace_path`` is a private ``tempfile.mkdtemp``
scratch dir (NOT under the projects root), and the container backends hide the
host path entirely (``workspace_path`` is ``None``). So this is an ``in_process``
HOST tool that requests no sandbox filesystem capability and NEVER derives a host
path from ``workspace_path``. It persists host state ONLY through a NARROW
host-owned intent-writer capability the runtime injects into ``ToolContext``: that
closure resolves the ACTIVE configured ``ProjectStore`` at INVOCATION time (so a
custom ``projects_root`` — even one changed after the executor was built — owns
where the sidecar lands) and writes atomically. The tool receives no raw root and
has no fallback store; when the capability is absent it fails closed and persists
nothing.
"""

from __future__ import annotations

from disco.core import SecurityRisk
from disco.core.release.command_grammar import (
    check_declaration_argv,
    check_no_inline_secret_cli,
    check_no_positional_credential,
)
from disco.core.release.spec import EnvVarDecl, ReleaseIntent, ResourceDecl, RuntimeStrategy
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..anatomy import ToolContext, ToolDef, ToolOutcome
from ..release_intent import ReleaseIntentWriteError

# A STATIC, value-free rejection for a command that fails the runtime grammar
# (WO-C5 §9.11/§9.2/§9.4). It names the RULE and never echoes the offending argv —
# a rejected token can itself carry a secret, so the tool's output stays hygienic.
_GRAMMAR_REJECTION = (
    "release intent rejected — a start/build command did not parse through the "
    "runtime grammar: an accepted command must head with a supported runtime "
    "(node/npm/npx/yarn/pnpm, python, or uvicorn/gunicorn/hypercorn) and use known "
    "flags only; an arbitrary executable/shell, an unknown flag, or an inline literal "
    "value is rejected. Pass any secret or value as a WHOLE ${NAME} reference to a "
    "declared env var (in required_env or the $PORT contract), never inline."
)

# A STATIC, value-free rejection for a resource `migrate_cmd` inline secret (WO-C5 #3
# / F5). The runtime-head grammar does NOT apply to a migration tool (alembic /
# wrangler), but a credential on a secret-bearing flag must be a declared ${NAME} ref.
_MIGRATE_SECRET_REJECTION = (
    "release intent rejected — a resource migrate_cmd carries an inline secret on a "
    "credential-bearing flag (--token / --password / --api-key / --secret / "
    "--credential / --access-token / …). A migration credential must be a WHOLE ${NAME} "
    "reference to a declared env var, never an inline literal value."
)


class ReleaseDeclareArgs(BaseModel):
    # extra="forbid": an unknown field (e.g. a value-bearing `{"env": [...]}` blob)
    # is a rejected declaration, never a silently-ignored one.
    model_config = ConfigDict(extra="forbid")

    start_cmd: list[str] = Field(
        default_factory=list,
        description=(
            'How to START the app, as an argv LIST (e.g. ["node", "server.js"] or '
            '["npm", "start"]) — NEVER a shell string. Each element is one exec token.'
        ),
    )
    build_cmd: list[str] = Field(
        default_factory=list,
        description=(
            "How to BUILD the app before starting, as an argv LIST (e.g. "
            '["npm", "run", "build"]) — NEVER a shell string. Omit when there is no build step.'
        ),
    )
    install_cmd: list[str] = Field(
        default_factory=list,
        description=(
            "How to INSTALL runtime dependencies before build/start, as an argv LIST "
            '(e.g. ["npm", "ci"] or ["pip", "install", "-r", "requirements.txt"]) — NEVER '
            "a shell string. Omit to let detection derive a safe default (npm ci/install "
            "for a package.json, pip install for a requirements.txt) when unambiguous."
        ),
    )
    runtime: RuntimeStrategy | None = Field(
        default=None,
        description=(
            "The runtime strategy the app is built/run as (node / python / static / "
            "dev_server / container). Omit to infer it from the start command; declare "
            '"static" for a prebuilt or build-then-serve site that has no long-running '
            "process."
        ),
    )
    package_manager: str | None = Field(
        default=None,
        description='The package manager the install uses (e.g. "npm", "pip") — a NAME only.',
    )
    lockfile: str | None = Field(
        default=None,
        description=(
            "The workspace-relative lockfile the install pins to (e.g. "
            '"package-lock.json"), so `npm ci` can be used. Omit when there is none.'
        ),
    )
    output_dir: str | None = Field(
        default=None,
        description=(
            'Where a static build\'s output lands (e.g. "dist"), served as the site '
            "root — a workspace-relative directory, never absolute or traversing."
        ),
    )
    env: list[EnvVarDecl] = Field(
        default_factory=list,
        description=(
            "Scoped env-var declarations (NAMES ONLY): each carries a scope (build or "
            "runtime), requiredness, and secret class. A typed superset of required_env "
            "(which stays a runtime/required shorthand). NEVER a value."
        ),
    )
    port_env: str = Field(
        default="PORT",
        description=(
            "The env-var NAME through which the host tells the app which port to bind "
            '(the $PORT contract) — an UPPERCASE NAME like "PORT", never a port NUMBER '
            "or a value."
        ),
    )
    health_path: str | None = Field(
        default=None,
        description='Optional HTTP health path that returns 200 when ready, e.g. "/healthz".',
    )
    required_env: list[str] = Field(
        default_factory=list,
        description=(
            "The env-var NAMES the app requires at runtime — NAMES ONLY (e.g. "
            '["DATABASE_URL", "STRIPE_API_KEY"]). NEVER a value: a "NAME=value" entry is '
            "rejected. The host injects the secret material out-of-band at deploy time."
        ),
    )
    resources: list[ResourceDecl] = Field(
        default_factory=list,
        description="Stateful resources the app needs to provision (v1: a sqlite database).",
    )


def _rejection_reason(exc: ValidationError) -> str:
    """A model-facing rejection reason that names the offending FIELD paths and the
    NAMES-ONLY / argv-LIST rules, but NEVER echoes the rejected input value — a
    smuggled ``NAME=value`` must not be leaked back through the error text (the
    ReleaseIntent validators embed the value in their own messages, so we surface
    only ``loc`` + static guidance)."""
    fields = sorted(
        {".".join(str(part) for part in err.get("loc", ())) or "(root)" for err in exc.errors()}
    )
    return (
        "release intent rejected — invalid field(s): "
        + ", ".join(fields)
        + ". Record NAMES only, argv LISTS only, no values: required_env / port_env must be "
        "bare UPPERCASE env-var NAMES (no '=', whitespace, lower-case, or leading digit); "
        "start_cmd / build_cmd must be argv LISTS of non-empty tokens, never shell strings."
    )


class ReleaseDeclareTool:
    definition = ToolDef(
        name="release_declare",
        description=(
            "Declare how this app builds and runs so the platform can release it: the "
            "start/build argv LISTS, the $PORT-contract env-var NAME, an optional health "
            "path, the required env-var NAMES (NAMES ONLY — never a secret value), and any "
            "stateful resources. The declaration is recorded HOST-SIDE as a candidate input "
            "to release detection — it is NOT a verification claim, and a workspace file can "
            'never self-assert it. Commands are argv lists (e.g. ["node", "server.js"]), '
            "never shell strings. Re-declaring overwrites the prior intent atomically."
        ),
        args_model=ReleaseDeclareArgs,
        # WO-C1: an in_process HOST tool. It writes host state through the injected
        # intent-writer capability, NOT through the sandbox, so it requests no
        # sandbox filesystem capability.
        needs=frozenset(),
        base_risk=SecurityRisk.LOW,
        runs_in="in_process",
        read_only=False,
    )

    async def run(self, args: ReleaseDeclareArgs, ctx: ToolContext) -> ToolOutcome:
        # Build the typed, NAMES-ONLY intent. ReleaseIntent's validators are the
        # security boundary: they reject a `NAME=value` env smuggle, an empty argv
        # token, a non-name port_env, etc. A rejected declaration persists NOTHING.
        try:
            intent = ReleaseIntent(
                runtime=args.runtime,
                build_cmd=tuple(args.build_cmd),
                install_cmd=tuple(args.install_cmd),
                start_cmd=tuple(args.start_cmd),
                package_manager=args.package_manager,
                lockfile=args.lockfile,
                output_dir=args.output_dir,
                port_env=args.port_env,
                health_path=args.health_path,
                required_env=tuple(args.required_env),
                env=tuple(args.env),
                resources=tuple(args.resources),
            )
        except ValidationError as exc:
            return ToolOutcome(
                success=False, error="invalid_release_intent", content=_rejection_reason(exc)
            )

        # WO-C5 §9.11: the DECLARATION-boundary runtime grammar. ReleaseIntent's own
        # validators guaranteed token hygiene (no metacharacter / substitution / inline
        # assignment / partial interpolation survived); the grammar now proves the
        # start/build commands parse through a runtime-specific shape — a supported
        # executable + known-safe flags — and that the ONLY value a flag carries is a
        # WHOLE DECLARED ${NAME} reference. This is the check that rejects an arbitrary
        # executable/shell, an unknown flag, and every inline secret CLI form
        # (`--token VALUE` / `--password VALUE` / URL userinfo), NOT a secret-flag
        # blacklist. A rejection persists NOTHING and echoes NO value.
        declared = (
            frozenset(intent.required_env) | {var.name for var in intent.env} | {intent.port_env}
        )
        try:
            check_declaration_argv(intent.start_cmd, declared_names=declared, field="start_cmd")
            check_declaration_argv(intent.build_cmd, declared_names=declared, field="build_cmd")
        except ValueError:
            return ToolOutcome(
                success=False, error="invalid_release_intent", content=_GRAMMAR_REJECTION
            )

        # R2 (G03): an explicit install_cmd legitimately heads with a NON-runtime-start
        # executable (`pip`, `npm`, `yarn`), so the runtime-START grammar
        # (`check_declaration_argv`) must NOT gate it — it would false-reject `pip
        # install`. It gets the SAME head-agnostic credential rails a migrate_cmd does
        # (token hygiene already ran in ReleaseIntent's validators): no inline secret on
        # a credential flag, no bare positional / `config set` literal credential. A
        # rejection persists NOTHING and echoes NO value.
        try:
            check_no_inline_secret_cli(
                intent.install_cmd, declared_names=declared, field="install_cmd"
            )
            check_no_positional_credential(
                intent.install_cmd, declared_names=declared, field="install_cmd"
            )
        except ValueError:
            return ToolOutcome(
                success=False, error="invalid_release_intent", content=_GRAMMAR_REJECTION
            )

        # WO-C5 #3 F5: a resource `migrate_cmd` is lowered verbatim into the compose
        # migrate-service command + release.json, so an inline secret there ships in the
        # bundle. The runtime-head grammar does NOT apply (a migration heads with
        # alembic / wrangler), but the same HEAD-AGNOSTIC inline-secret hygiene must — a
        # credential-bearing flag's value must be a whole declared ${NAME} reference.
        try:
            for resource in intent.resources:
                check_no_inline_secret_cli(
                    resource.migrate_cmd, declared_names=declared, field="migrate_cmd"
                )
                # WO-C5 #3 / G02: also reject a bare POSITIONAL literal credential (or a
                # `config set <credential-key> <literal>` role form) in a migrate_cmd —
                # the runtime-head grammar does not apply to a migration tool, but a
                # positional secret would ship verbatim in the migrate command +
                # release.json exactly like a flag secret.
                check_no_positional_credential(
                    resource.migrate_cmd, declared_names=declared, field="migrate_cmd"
                )
        except ValueError:
            return ToolOutcome(
                success=False, error="invalid_release_intent", content=_MIGRATE_SECRET_REJECTION
            )

        # WO-C1: persist ONLY through the runtime-injected, host-owned intent writer,
        # which resolves the ACTIVE configured ProjectStore at invocation time. No
        # capability ⇒ fail closed (a standalone executor carries no host writer); the
        # tool has no fallback store and never derives a host path itself.
        writer = ctx.release_intent_writer
        if writer is None:
            return ToolOutcome(
                success=False,
                error="intent_writer_unavailable",
                content=(
                    "release intent was not recorded: this runtime did not provide the "
                    "host-owned intent-writer capability, so nothing was persisted."
                ),
            )
        try:
            await writer(ctx.conversation_id, ctx.owner_id, intent)
        except ReleaseIntentWriteError as exc:
            # Typed, fail-closed failure (invalid/unavailable root, owner mismatch, or a
            # persistence failure). ZERO bytes were persisted; the message is root/path/
            # secret-free by construction.
            return ToolOutcome(success=False, error=exc.code, content=str(exc))

        # Success output is env NAMES + non-sensitive structural metadata ONLY — never
        # the full argv, resource URLs, filesystem roots, or the sidecar path (WO-C1
        # crit 8).
        names = list(intent.required_env)
        return ToolOutcome(
            success=True,
            content=(
                "Recorded release intent (NAMES only — no values): "
                f"required_env NAMES={names}, port_env={intent.port_env!r}, "
                f"health_path={intent.health_path!r}. "
                "This is a CANDIDATE input to release detection, not a verification claim."
            ),
            structured={
                "port_env": intent.port_env,
                "health_path": intent.health_path,
                "required_env": names,
                "resource_ids": [resource.id for resource in intent.resources],
                "is_verification_claim": False,
            },
        )


__all__ = ["ReleaseDeclareArgs", "ReleaseDeclareTool"]
