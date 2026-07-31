"""Worker auth-semantics verification, canonical-Worker matching, and the
trusted-tree reconstruction verifiers (Stripe/webhook) that back them.

Extracted from ``deploy.py`` to reduce module complexity; the public facade
re-imports these names unchanged.

``read_workspace_file`` is reached through the ``deploy`` facade (a lazy,
function-local import) rather than a sibling module: it is DEFINED on
``deploy.py`` itself (see ``_workspace.py``'s docstring for why), and deploy.py
imports FROM this module at its own top level, so a module-level reverse import
here would be circular.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from disco.core.appkit.spec import (
    AppSpec,
    DesignSpec,
    load_app_spec_from_bytes,
    load_design_spec_from_bytes,
)
from disco.core.appkit.stripe_primitive import stripe_verify
from disco.core.appkit.webhook_primitive import webhook_verify

from ..models import DeployRefused, RefusalReason
from ._constants import _WORKER_RELPATH
from ._guards import _assert_main_is_canonical


def _admin_token_strong(token: str) -> bool:
    """SEC-4: a deployed admin endpoint is only as safe as its ADMIN_TOKEN. Require a
    HIGH-ENTROPY value: at least 16 characters AND at least 8 distinct characters (so a
    short or low-variety owner-supplied token is rejected). The Worker is expected to
    additionally RATE-LIMIT admin auth attempts (documented in the OWNER guide); a strong
    token plus rate-limiting defeats online guessing."""
    t = token.strip()
    return len(t) >= 16 and len(set(t)) >= 8


def _strip_ts_comments_lite(src: str) -> str:
    """Drop ``/* … */`` block + ``// …`` line comments so the token-leak scan never
    fires on (or is fooled by) commented-out code. Intentionally simple — it may
    over-strip a ``//`` that lives inside a string literal, which only makes the leak
    scan MORE conservative (it can never hide a real sink that way)."""
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", src)


def _assert_worker_auth_verified(workspace: Path) -> None:
    """SEC-4: REQUIRE a positive verification that the staged ``worker/index.ts``
    enforces the admin-token gate BEFORE any real Cloudflare mutation — else
    :class:`DeployRefused` (``WORKER_AUTH_UNVERIFIED``, fail closed). A buggy/malicious
    generated Worker could otherwise ignore ``/admin`` auth or exfiltrate
    ``env.ADMIN_TOKEN``, and the export-readiness gate (GATE 1) does NOT inspect the
    Worker's auth STRUCTURE.

    The verdict reuses the EPIC G ``worker_contract`` static inspector
    (:func:`disco.tools.builtin.verify_appkit_app.inspect_worker`) — the SAME
    battle-tested route-handler/brace-aware parser ``verify_appkit_app`` runs — so the
    deploy can never drift from the verifier. It requires BOTH:

      * ``reads_require_auth`` — GET ``/api/leads`` AND ``/admin`` early-return 401 via the
        auth guard before any DB read, and ``isAuthorized`` actually validates the
        ``Authorization: Bearer`` token against ``env.ADMIN_TOKEN``; and
      * ``fail_closed_without_token`` — the guard denies (``return false``) when
        ADMIN_TOKEN is unset (never defaults open).

    PLUS a focused token-exfiltration scan (:data:`_ADMIN_TOKEN_LEAK_RE`): the Worker must
    never echo ``env.ADMIN_TOKEN`` into a response/serialization sink. The inspector is
    imported FUNCTION-LOCALLY (it pulls the tools layer) so a stale/broken verifier import
    fails the gate CLOSED rather than crashing the deploy."""
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    worker_ts = _deploy.read_workspace_file(workspace / _WORKER_RELPATH, workspace.resolve())
    if not (worker_ts and worker_ts.strip()):
        raise DeployRefused(
            RefusalReason.WORKER_AUTH_UNVERIFIED,
            "No worker/index.ts in the staged deploy tree — cannot verify the admin-token "
            "auth gate. Refusing to deploy an unverified Worker (fail closed).",
        )
    # Security primitives use their authoritative trusted-tree verifiers rather
    # than the legacy lead-specific inspector. The checks repeat post-build.
    if _assert_security_primitive_trusted_trees(workspace):
        return
    try:
        from disco.core.appkit.spec import Entity
        from disco.tools.builtin.verify_appkit_app import inspect_worker
    except ImportError as exc:  # pragma: no cover - defensive; fail CLOSED
        raise DeployRefused(
            RefusalReason.WORKER_AUTH_UNVERIFIED,
            f"The worker-auth verifier could not be loaded ({exc}); refusing to deploy "
            "without a positive auth verdict (fail closed).",
        ) from exc
    # The auth verdict (reads_require_auth / fail_closed_without_token) does not depend on
    # the lead entity — a minimal stand-in is sufficient to drive the static inspection.
    _, auth_model, reasons = inspect_worker(worker_ts, Entity(id="lead", name="Lead"))
    # SEC-4: require the FULL non-bypassable verdict. Beyond the two canonical routes
    # being guarded + fail-closed (reads_require_auth / fail_closed_without_token), the
    # taint/alias-aware checks must pass: env.ADMIN_TOKEN never reaches a leak sink
    # outside isAuthorized (``admin_token_safe`` — supersedes the prior regex scan, which
    # an alias ``const x = env.ADMIN_TOKEN`` bypassed), and EVERY lead-read route is
    # auth-guarded, not just the two canonical ones (``all_lead_reads_guarded`` — closes
    # an extra unauthenticated ``/debug-leads``).
    if not (
        auth_model.reads_require_auth
        and auth_model.fail_closed_without_token
        and auth_model.admin_token_safe
        and auth_model.all_lead_reads_guarded
    ):
        auth_gaps = [
            r
            for r in reasons
            if any(
                k in r
                for k in (
                    "401",
                    "ADMIN_TOKEN",
                    "Bearer",
                    "auth",
                    "guard",
                    "leak",
                    "alias",
                    "debug",
                    "lead",
                )
            )
        ] or reasons
        raise DeployRefused(
            RefusalReason.WORKER_AUTH_UNVERIFIED,
            "The staged worker/index.ts does not provably enforce the admin-token gate on "
            "ALL protected + lead-read routes (fail-closed, with no env.ADMIN_TOKEN leak or "
            f"alias): {'; '.join(auth_gaps)}. Refusing to deploy (fail closed).",
        )


def _normalize_worker_source(src: str) -> str:
    """Normalize a ``worker/index.ts`` for the canonical-match comparison: collapse
    line endings (CRLF/CR → LF), strip per-line TRAILING whitespace, and drop trailing
    blank lines. This tolerates ONLY a benign editor/transport reformat (re-saved with
    CRLF, a trailing newline trimmed); ANY material code change — an added route, a
    leaked-token sink, a reordered statement, a changed identifier — still mismatches.
    Intentionally does NOT normalize interior whitespace/indentation, so a structural
    edit can never be normalized away."""
    lines = src.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).rstrip("\n")


def _load_guarded_app_spec(staged: Path) -> AppSpec:
    """Load the frozen staged AppSpec through the deploy path's guarded reader."""
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    spec_text = _deploy.read_workspace_file(staged / ".disco/appspec.json", staged.resolve())
    if not (spec_text and spec_text.strip()):
        raise DeployRefused(
            RefusalReason.WORKER_NOT_CANONICAL,
            "No .disco/appspec.json in the staged deploy tree — cannot validate "
            "the generated Worker or its deployment bindings (fail closed).",
        )
    try:
        return load_app_spec_from_bytes(spec_text)
    except Exception as exc:
        raise DeployRefused(
            RefusalReason.WORKER_NOT_CANONICAL,
            f"Could not validate the staged app spec ({exc}); refusing to deploy an "
            "unverifiable Worker (fail closed).",
        ) from exc


def _guarded_app_is_stripe(staged: Path) -> bool:
    """Best-effort dispatch hint; airtight spec validation still runs later."""
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    spec_text = _deploy.read_workspace_file(staged / ".disco/appspec.json", staged.resolve())
    if not (spec_text and spec_text.strip()):
        return False
    try:
        return load_app_spec_from_bytes(spec_text).stripe is not None
    except Exception:
        return False


def _guarded_app_spec_hint(staged: Path) -> AppSpec | None:
    """Best-effort dispatch only; each selected verifier reloads fail-closed."""
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    spec_text = _deploy.read_workspace_file(staged / ".disco/appspec.json", staged.resolve())
    if not (spec_text and spec_text.strip()):
        return None
    try:
        return load_app_spec_from_bytes(spec_text)
    except Exception:
        return None


def _assert_security_primitive_trusted_trees(staged: Path) -> bool:
    app = _guarded_app_spec_hint(staged)
    if app is None:
        return False
    selected = False
    if app.stripe is not None:
        _assert_stripe_trusted_tree(staged)
        selected = True
    if app.webhooks is not None:
        _assert_webhook_trusted_tree(staged)
        selected = True
    return selected


def _load_guarded_design_spec(staged: Path) -> DesignSpec:
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    raw = _deploy.read_workspace_file(staged / ".disco/designspec.json", staged.resolve())
    if not (raw and raw.strip()):
        raise DeployRefused(
            RefusalReason.STRIPE_TRUSTED_TREE,
            "Stripe deployment requires .disco/designspec.json for trusted reconstruction.",
        )
    try:
        return load_design_spec_from_bytes(raw)
    except Exception as exc:
        raise DeployRefused(
            RefusalReason.STRIPE_TRUSTED_TREE,
            f"Could not validate the staged Stripe design spec ({exc}).",
        ) from exc


def _assert_stripe_trusted_tree(staged: Path) -> None:
    """Run the exact core Stripe verifier over its on-disk trusted path set."""
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    app = _load_guarded_app_spec(staged)
    if app.stripe is None:
        return
    design = _load_guarded_design_spec(staged)
    try:
        from disco.core.appkit.generator import generate

        paths = set(generate(app, design))
    except Exception as exc:
        raise DeployRefused(
            RefusalReason.STRIPE_TRUSTED_TREE,
            f"Could not reconstruct the trusted Stripe deploy tree ({exc}).",
        ) from exc
    paths.update({".disco/appspec.json", ".disco/primitives/stripe.json"})
    tree: dict[str, str] = {}
    for relpath in sorted(paths):
        text = _deploy.read_workspace_file(staged / relpath, staged.resolve())
        if text is not None:
            tree[relpath] = text
    result = stripe_verify(app, design, tree)
    if result.ok:
        return
    failures = [f"{check.name}: {check.evidence}" for check in result.checks if not check.passed]
    raise DeployRefused(
        RefusalReason.STRIPE_TRUSTED_TREE,
        "Stripe trusted-tree verification failed: " + "; ".join(failures),
    )


def _assert_webhook_trusted_tree(staged: Path) -> None:
    """Run the core webhook verifier over its exact on-disk trusted projection."""
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    app = _load_guarded_app_spec(staged)
    if app.webhooks is None:
        return
    raw_design = _deploy.read_workspace_file(staged / ".disco/designspec.json", staged.resolve())
    if not (raw_design and raw_design.strip()):
        raise DeployRefused(
            RefusalReason.WEBHOOK_TRUSTED_TREE,
            "Webhook deployment requires .disco/designspec.json for trusted reconstruction.",
        )
    try:
        design = load_design_spec_from_bytes(raw_design)
        from disco.core.appkit.generator import generate

        paths = set(generate(app, design))
    except Exception as exc:
        raise DeployRefused(
            RefusalReason.WEBHOOK_TRUSTED_TREE,
            f"Could not reconstruct the trusted webhook deploy tree ({exc}).",
        ) from exc
    paths.update({".disco/appspec.json", ".disco/primitives/webhook.json"})
    tree: dict[str, str] = {}
    for relpath in sorted(paths):
        text = _deploy.read_workspace_file(staged / relpath, staged.resolve())
        if text is not None:
            tree[relpath] = text
    result = webhook_verify(app, design, tree)
    if result.ok:
        return
    failures = [f"{check.name}: {check.evidence}" for check in result.checks if not check.passed]
    raise DeployRefused(
        RefusalReason.WEBHOOK_TRUSTED_TREE,
        "Webhook trusted-tree verification failed: " + "; ".join(failures),
    )


def _assert_worker_is_canonical(staged: Path) -> None:
    """SEC-4 / BUILD-1: REQUIRE the staged ``worker/index.ts`` to BE the canonical Worker
    the AppKit generator deterministically emits from this app's spec — else
    :class:`DeployRefused` (``WORKER_NOT_CANONICAL``, fail closed).

    This is the ROBUST architectural close to the SEC-4 heuristic-taint arms race. Rather
    than try to PROVE arbitrary, custom-authored Worker TypeScript is auth-safe (a losing
    game — every taint/route heuristic can be reworded around: a copy-of-alias token leak,
    a ``url.pathname.startsWith("/debug-leads")`` unauthenticated lead read, switch/regex
    dispatch), we REQUIRE the deployed Worker to equal the ONE Worker the generator
    produces. The generated ``worker/index.ts`` is reconstructed by running the SAME
    registered primitive generator that writes that path for the staged AppSpec. It is
    NAMESPACE-INDEPENDENT (the SEC-30 namespace only renames the Worker/D1 in
    wrangler.toml, never the Worker source). So there is no arbitrary Worker to analyse:
    a leak sink or an extra unauthenticated route is, by construction, NOT the canonical
    Worker and is refused.

    Runs on the POST-BUILD staged tree (after the sandboxed build + dist sync-back, BEFORE
    any Cloudflare mutation), which also closes the BUILD-1 TOCTOU: the pre-build auth gate
    judged the AUTHORED Worker, but the build could EMIT or rewrite ``worker/index.ts`` and
    the sync-back would replace it. The build compiles the FRONTEND into ``dist`` only — it
    must NEVER touch the Worker source — so any post-build deviation (a malicious emit, or
    even a cosmetic rewrite beyond trailing-whitespace/line-ending) → mismatch → refuse.

    The spec is read from the IMMUTABLE STAGED tree (``.disco/appspec.json``, frozen at
    stage time via the guarded reader) through the airtight ``load_app_spec_from_bytes``
    loader (size-capped, schema-validated). A missing/unloadable spec, or a generator that
    can't be imported, fails CLOSED — an unverifiable Worker is never deployed. The
    generator is imported FUNCTION-LOCALLY (it pulls the core layer) so a stale/broken
    import refuses rather than crashing the deploy."""
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    workspace_root = staged.resolve()
    try:
        from disco.core.appkit.generator import generate
        from disco.core.appkit.spec import (
            DesignSpec,
            Palette,
            Typography,
        )
    except ImportError as exc:  # pragma: no cover - defensive; fail CLOSED
        raise DeployRefused(
            RefusalReason.WORKER_NOT_CANONICAL,
            f"The AppKit generator could not be loaded ({exc}); refusing to deploy a "
            "Worker we cannot prove canonical (fail closed).",
        ) from exc
    try:
        app_spec = _load_guarded_app_spec(staged)
        # The worker generators do not read design decisions; pass a fixed internal
        # DesignSpec so reconstruction has no staged input beyond the frozen AppSpec.
        canonical_design = DesignSpec(
            schema_version=1,
            typography=Typography(heading_font="Inter", body_font="Inter"),
            palette=Palette(
                primary="#111111",
                surface="#ffffff",
                text="#111111",
                accent="#2563eb",
            ),
            layout_family="canonical",
            component_style="plain",
            density="comfortable",
        )
        canonical_worker = generate(app_spec, canonical_design)["worker/index.ts"]
    except Exception as exc:
        # Malformed/oversize/schema-invalid spec, an unloadable primitive, or a
        # generator failure: we cannot derive the canonical Worker, so we cannot
        # trust the deployed one.
        raise DeployRefused(
            RefusalReason.WORKER_NOT_CANONICAL,
            f"Could not regenerate the canonical Worker from the staged app spec ({exc}); "
            "refusing to deploy an unverifiable Worker (fail closed).",
        ) from exc
    staged_worker = _deploy.read_workspace_file(staged / _WORKER_RELPATH, workspace_root)
    if not (staged_worker and staged_worker.strip()):
        raise DeployRefused(
            RefusalReason.WORKER_NOT_CANONICAL,
            "No worker/index.ts in the staged deploy tree — refusing to deploy (the "
            "deployed Worker must BE the canonical generated Worker; fail closed).",
        )
    if _normalize_worker_source(staged_worker) != _normalize_worker_source(canonical_worker):
        raise DeployRefused(
            RefusalReason.WORKER_NOT_CANONICAL,
            "The staged worker/index.ts is NOT the canonical Worker the AppKit generator "
            "emits from this app's spec. The deployed Worker must be the pristine generated "
            "one (the build compiles the frontend into ./dist; it must never author or "
            "rewrite the Worker source). Refusing to deploy a non-canonical / custom Worker "
            "(fail closed) — closes the SEC-4 auth-bypass + BUILD-1 post-build-emit vectors.",
        )
    # SEC-1-class: we just proved the STAGED worker/index.ts is canonical — but
    # ``wrangler deploy`` runs whatever the staged wrangler.toml ``main`` points at. TIE
    # them: ``main`` MUST EXACTLY name worker/index.ts, else a look-alike main
    # (``....worker/index.ts``), an absolute path, or a ``..`` escape would deploy a
    # DIFFERENT, UNCHECKED entrypoint while this gate validated the pristine worker
    # (also dodging the SEC-10 ownership preflight). The check uses the immutable STAGED
    # wrangler.toml (TOCTOU-proof) and the SAME pure matcher as export-readiness.
    staged_toml = _deploy.read_workspace_file(staged / "wrangler.toml", workspace_root)
    try:
        staged_cfg = tomllib.loads(staged_toml) if staged_toml else {}
    except tomllib.TOMLDecodeError:
        staged_cfg = {}
    _assert_main_is_canonical(staged_cfg)
