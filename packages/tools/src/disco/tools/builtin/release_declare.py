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

Host resolution: the sidecar is HOST state under the projects root, which the
sandbox does not own. NO sandbox backend exposes the host projects root — the
process (dev) backend's ``workspace_path`` is a private ``tempfile.mkdtemp``
scratch dir (NOT under the projects root), and the container backends hide the
host path entirely (``workspace_path`` is ``None``). So we resolve via the
ProjectStore's own default-root resolution — the SAME ``DISCO_DATA_DIR``-derived
root the runtime uses to persist ``manifest.json`` for the standard deployment —
so ``read_release_intent`` (and the later release endpoint) find this sidecar
next to it. LIMITATION (honest): a deployment that sets a CUSTOM ``projects_root``
in settings (one that differs from the ``DISCO_DATA_DIR`` default) is not yet
reachable from a tool, because ``ToolContext`` does not carry the runtime's
configured root; closing that gap needs the runtime to thread its configured root
(or the ProjectStore) into ``ToolContext`` — a separate WO. The tool must NEVER
guess the root from a sandbox scratch path.
"""

from __future__ import annotations

from disco.core import SecurityRisk
from disco.core.release.spec import ReleaseIntent, ResourceDecl
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..projects.store import ProjectStore, StorageError


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
            'How to BUILD the app before starting, as an argv LIST (e.g. '
            '["npm", "ci"]) — NEVER a shell string. Omit when there is no build step.'
        ),
    )
    port_env: str = Field(
        default="PORT",
        description=(
            'The env-var NAME through which the host tells the app which port to bind '
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


def _resolve_store() -> ProjectStore:
    """Resolve the HOST ProjectStore that owns this conversation's project dir.

    NO sandbox backend exposes the host projects root, so there is nothing safe to
    derive it from: the process (dev) backend's ``workspace_path`` is a private
    ``tempfile.mkdtemp`` scratch dir (NOT under the projects root — its grandparent
    is the ``mkdtemp`` PARENT, e.g. ``/tmp``), and the container backends hide the
    host path entirely (``workspace_path`` is ``None``). So we always use
    ``ProjectStore("")``, whose resolution yields the default
    (``DISCO_DATA_DIR``-derived) root — the SAME root the runtime uses to persist
    ``manifest.json`` for the standard deployment, so a later ``read_release_intent``
    finds this sidecar next to it.

    LIMITATION (honest): a deployment that sets a CUSTOM ``projects_root`` in
    settings — one that differs from the ``DISCO_DATA_DIR`` default — is not yet
    reachable here, because ``ToolContext`` does not carry the runtime's configured
    root. Closing that gap requires threading the configured root (or the
    ProjectStore) into ``ToolContext`` — a separate WO. The tool must NEVER derive
    the root from a sandbox scratch path: the old ``workspace_path.parent.parent``
    guess wrote the sidecar to ``<mkdtemp>/<cid>/`` on the process backend — a wrong,
    store-invisible ``/tmp`` location."""
    return ProjectStore("")


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
        needs=frozenset({Capability.FILESYSTEM}),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        read_only=False,
    )

    async def run(self, args: ReleaseDeclareArgs, ctx: ToolContext) -> ToolOutcome:
        # Build the typed, NAMES-ONLY intent. ReleaseIntent's validators are the
        # security boundary: they reject a `NAME=value` env smuggle, an empty argv
        # token, a non-name port_env, etc. A rejected declaration persists NOTHING.
        try:
            intent = ReleaseIntent(
                build_cmd=tuple(args.build_cmd),
                start_cmd=tuple(args.start_cmd),
                port_env=args.port_env,
                health_path=args.health_path,
                required_env=tuple(args.required_env),
                resources=tuple(args.resources),
            )
        except ValidationError as exc:
            return ToolOutcome(
                success=False, error="invalid_release_intent", content=_rejection_reason(exc)
            )

        store = _resolve_store()
        try:
            path = store.write_release_intent(ctx.conversation_id, intent)
        except StorageError as exc:
            return ToolOutcome(success=False, error="persist_failed", content=str(exc))

        names = list(intent.required_env)
        return ToolOutcome(
            success=True,
            content=(
                "Recorded release intent (NAMES only — no values): "
                f"start={list(intent.start_cmd)}, build={list(intent.build_cmd)}, "
                f"port_env={intent.port_env!r}, health_path={intent.health_path!r}, "
                f"required_env NAMES={names}. "
                "This is a CANDIDATE input to release detection, not a verification claim."
            ),
            structured={
                "start_cmd": list(intent.start_cmd),
                "build_cmd": list(intent.build_cmd),
                "port_env": intent.port_env,
                "health_path": intent.health_path,
                "required_env": names,
                "resource_ids": [resource.id for resource in intent.resources],
                "sidecar_path": str(path),
                "is_verification_claim": False,
            },
        )


__all__ = ["ReleaseDeclareArgs", "ReleaseDeclareTool"]
