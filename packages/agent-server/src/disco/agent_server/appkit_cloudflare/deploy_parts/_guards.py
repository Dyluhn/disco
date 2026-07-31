"""Pre-deploy tree guards: the sole-config-source, plaintext-secret,
secret-shaped-file, wrangler-allowlist, and adopt-schema-safety asserts.

Extracted from ``deploy.py`` to reduce module complexity; the public facade
re-imports these names unchanged.

``read_workspace_file`` is reached through the ``deploy`` facade (a lazy,
function-local import) rather than a sibling module: it is DEFINED on
``deploy.py`` itself (see ``_workspace.py``'s docstring for why), and deploy.py
imports FROM this module at its own top level, so a module-level reverse import
here would be circular.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

from disco.core.appkit.local_verify import main_points_at_worker_entry

from ..models import DeployRefused, RefusalReason
from ._constants import (
    _ALLOWED_WRANGLER_KEYS,
    _ALT_CONFIG_SCAN_SKIP_DIRS,
    _ALT_WRANGLER_CONFIG_NAMES,
    _DENY_DEPLOY_FILE_RE,
    _DEPLOY_TEMPLATE_SUFFIX_RE,
    _DESTRUCTIVE_SQL_RE,
    _SCANNED_TEXT_SUFFIXES,
    _SECRET_NAME_RE,
    _SECRET_SCAN_MAX_BYTES,
    _SECRET_VALUE_RES,
    _WRANGLER_STATE_DIR,
)
from ._staging import _iter_tree_files

# ---- SEC (config-source bypass): wrangler.toml is the SOLE config source ------


def _find_alt_wrangler_configs(tree: Path) -> list[str]:
    """Return the tree-relative paths of EVERY alternate wrangler config source under
    *tree*: a ``.wrangler`` entry (dir/file/symlink — the redirect vector) or a
    ``wrangler.json``/``wrangler.jsonc`` file, at ANY depth. Walks with
    ``followlinks=False`` and matches on the NAME only (lstat-free — never dereferences a
    symlink target), pruning the regenerated/internal subtrees so a dependency's own config
    can't false-positive. ``wrangler.toml`` is NOT matched (it is the sole permitted
    source)."""
    try:
        tree_root = tree.resolve()
    except OSError:
        tree_root = tree
    offenders: list[str] = []
    for root, dirs, names in os.walk(tree):
        dirs[:] = [d for d in dirs if d not in _ALT_CONFIG_SCAN_SKIP_DIRS]
        root_path = Path(root)
        for entry in (*dirs, *names):
            if entry == _WRANGLER_STATE_DIR or entry.lower() in _ALT_WRANGLER_CONFIG_NAMES:
                p = root_path / entry
                try:
                    offenders.append(p.relative_to(tree_root).as_posix())
                except ValueError:
                    offenders.append(entry)
    return offenders


def _assert_sole_wrangler_config(*trees: Path) -> None:
    """REFUSE the deploy if ANY alternate wrangler config source exists in *trees* — a
    ``wrangler.json``/``wrangler.jsonc`` or a ``.wrangler`` (redirect) dir anywhere. The
    deploy gates (SEC-5 allowlist, canonical-worker match, SEC-10 ownership) read/validate
    ONLY ``wrangler.toml``, but ``wrangler deploy`` resolves ``wrangler.json`` → ``.jsonc``
    → ``.toml`` by precedence AND honors a ``.wrangler/deploy/config.json`` redirect to an
    ARBITRARY config — so an alt source would deploy an UNCHECKED main/name/routes/account
    that bypasses every gate. AppKit emits ONLY ``wrangler.toml``, so any alt config is an
    injection → :class:`DeployRefused` (``ALT_WRANGLER_CONFIG``, fail closed). Scanned on
    the IMMUTABLE STAGED tree (the bytes wrangler actually runs against — TOCTOU-proof) AND
    on the LIVE workspace (which catches a planted ``.wrangler`` redirect that STAGING
    excludes — exclude + assert-absent, defence in depth)."""
    offenders = sorted({rel for tree in trees for rel in _find_alt_wrangler_configs(tree)})
    if offenders:
        raise DeployRefused(
            RefusalReason.ALT_WRANGLER_CONFIG,
            "An alternate wrangler config source is present in the deploy tree "
            f"({offenders}). AppKit generates ONLY wrangler.toml, but `wrangler deploy` "
            "resolves wrangler.json/.jsonc BEFORE wrangler.toml and honors a "
            ".wrangler/deploy/config.json redirect — an alt config would deploy an "
            "UNCHECKED worker/name/routes/account that bypasses the deploy gates. "
            "wrangler.toml is the sole config source — refusing to deploy (fail closed).",
        )


def _assert_no_ancestor_wrangler_config(cwd: Path) -> None:
    """Close the OUT-OF-TREE wrangler config-redirect bypass. ``_assert_sole_wrangler_config``
    scans the staged copy + the live workspace, but ``wrangler deploy`` discovers a
    ``.wrangler/deploy/config.json`` redirect by walking UP from its CWD — and the deploy runs
    wrangler with ``cwd=<staged>`` where ``<staged>`` is a ``mkdtemp`` dir under the staging
    root (e.g. ``/tmp/disco-deploy-stage-*``). So a PRE-EXISTING ``.wrangler`` at an ANCESTOR of
    that cwd — e.g. a planted ``/tmp/.wrangler/deploy/config.json`` pointing at
    ``/tmp/evil/wrangler.jsonc`` — sits OUTSIDE both scanned trees yet is honored by wrangler's
    upward walk, re-pointing it at an arbitrary config/main/account (the ``--config`` pin may
    NOT override a ``.wrangler`` redirect).

    Defence: walk EVERY directory from *cwd* (the exact cwd wrangler runs in) UP TO the
    filesystem root and REFUSE (:class:`DeployRefused` ``ANCESTOR_WRANGLER_CONFIG``, fail
    closed) if any of them contains a ``.wrangler`` entry (dir/file/symlink — the redirect
    lives at ``.wrangler/deploy/config.json``). Each candidate is ``os.lstat``'d — NEVER
    dereferenced: a ``.wrangler`` symlink is itself an offender and must not be followed to a
    host path. The clean case (no ancestor ``.wrangler`` anywhere above the staged cwd) passes
    and deploys. Paired with the controlled :func:`_deploy_stage_root` (so an operator can put
    staging under a path with verified-clean ancestors), this fails closed even when the
    staging root's ancestors are shared (a multi-tenant ``/tmp``)."""
    try:
        start = cwd.resolve()
    except OSError:
        start = cwd
    offenders: list[str] = []
    for ancestor in (start, *start.parents):
        candidate = ancestor / _WRANGLER_STATE_DIR
        try:
            os.lstat(candidate)  # lstat: presence only, NEVER dereference the .wrangler link
        except OSError:
            continue  # not present here (FileNotFoundError) or unreadable — keep walking up
        offenders.append(str(candidate))
    if offenders:
        raise DeployRefused(
            RefusalReason.ANCESTOR_WRANGLER_CONFIG,
            "A `.wrangler` config dir exists ABOVE the staged deploy cwd "
            f"({offenders}). `wrangler deploy` discovers a `.wrangler/deploy/config.json` "
            "redirect by walking UP from its cwd, so an ancestor `.wrangler` — outside both "
            "the staged copy and the live workspace — could re-point the deploy at an "
            "arbitrary config/main/account that bypasses every gate (and that `--config` may "
            "not override). Refusing to deploy with an ancestor wrangler config reachable "
            "from the deploy cwd (fail closed).",
        )


def _assert_no_plaintext_secrets(workspace: Path) -> None:
    """Refuse a deploy whose tree carries a PLAINTEXT secret (SEC-17/SEC-18). Extends
    the ``.dev.vars`` secret-safety check (Epic I) to the rest of the deploy tree:

      * a ``.env`` file present in the deploy tree with a secret-NAMED assignment
        (``API_TOKEN=...``) — ``.env`` is not a Cloudflare secret mechanism and would
        ship plaintext;
      * a ``[vars]`` table in ``wrangler.toml`` with a secret-NAMED key bound to a
        non-empty string — ``[vars]`` are PUBLIC, build-time-inlined plaintext, so a
        secret there is published.

    Fail CLOSED (the dedicated ``PLAINTEXT_SECRET_REFUSED`` reason) so a real secret
    never reaches the public edge."""
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    env_text = _deploy.read_workspace_file(workspace / ".env", workspace.resolve())
    if env_text:
        for line in env_text.splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, _, value = raw.partition("=")
            if value.strip().strip("\"'") and _SECRET_NAME_RE.search(key):
                raise DeployRefused(
                    RefusalReason.PLAINTEXT_SECRET_REFUSED,
                    "A plaintext secret was found in a deployed .env file "
                    f"({key.strip()}). Move it to `wrangler secret put` — refusing to "
                    "deploy a plaintext secret (fail closed).",
                )
    toml_text = _deploy.read_workspace_file(workspace / "wrangler.toml", workspace.resolve())
    if toml_text:
        try:
            cfg = tomllib.loads(toml_text)
        except tomllib.TOMLDecodeError:
            cfg = {}
        varz = cfg.get("vars")
        if isinstance(varz, dict):
            for key, value in varz.items():
                if isinstance(value, str) and value.strip() and _SECRET_NAME_RE.search(str(key)):
                    raise DeployRefused(
                        RefusalReason.PLAINTEXT_SECRET_REFUSED,
                        f"A plaintext secret was found in wrangler.toml [vars] ({key}). "
                        "[vars] are PUBLIC build-time values; use `wrangler secret put` "
                        "instead — refusing to deploy a plaintext secret (fail closed).",
                    )


def _collect_secret_scan_paths(workspace: Path, served_root: Path) -> list[Path]:
    """Every path :func:`_assert_no_secret_shaped_files` must scan: the deploy tree
    (minus the globally-skipped roots) PLUS the resolved served-assets tree walked
    DIRECTLY — so the bytes Cloudflare PUBLISHES are ALWAYS scanned even if some
    other path resolved a served dir into a skipped area (defense in depth). A set
    dedupes the overlap (the common case where ``served_root`` is a normal subdir)."""
    seen: set[Path] = set()
    scan_paths: list[Path] = []
    for path in _iter_tree_files(workspace):
        if path not in seen:
            seen.add(path)
            scan_paths.append(path)
    if served_root.is_dir():
        for sroot, _sdirs, snames in os.walk(served_root, followlinks=False):
            for sname in snames:
                p = Path(sroot) / sname
                if p not in seen:
                    seen.add(p)
                    scan_paths.append(p)
    return sorted(scan_paths)


def _assert_file_not_credential_named(path: Path, served_root: Path) -> bool:
    """SEC-17: refuse *path* if its NAME is credential-shaped and the name-deny check
    applies (inside the served tree unconditionally; outside it, unless a
    template/placeholder suffix exempts it). Returns whether the name is
    credential-shaped at all — the caller still content-scans an exempted template."""
    name = path.name
    is_credential_name = _DENY_DEPLOY_FILE_RE.search(name) is not None
    if not is_credential_name:
        return False
    in_served_tree = path == served_root or path.is_relative_to(served_root)
    if in_served_tree or not _DEPLOY_TEMPLATE_SUFFIX_RE.search(name):
        where = (
            "the served assets directory (it would publish as a PUBLIC asset)"
            if in_served_tree
            else "the deploy tree"
        )
        raise DeployRefused(
            RefusalReason.PLAINTEXT_SECRET_REFUSED,
            f"A credential-bearing file is present in {where}: {name}. "
            "Such files (private keys, .env, .npmrc, …) must never be published — "
            "a .example/.sample/.template suffix does NOT exempt a credential-named "
            "file inside the served assets dir. Refusing to deploy (fail closed).",
        )
    return True


def _assert_file_has_no_secret_value(path: Path, workspace_root: Path) -> None:
    """SEC-18: refuse *path* if its CONTENT carries a high-confidence secret-shaped
    value, size-capped so a giant minified bundle can't turn the scan into a DoS."""
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    try:
        if path.stat().st_size > _SECRET_SCAN_MAX_BYTES:
            return
    except OSError:
        return
    text = _deploy.read_workspace_file(path, workspace_root)
    if not text:
        return
    for pat in _SECRET_VALUE_RES:
        if pat.search(text):
            rel = path.relative_to(workspace_root).as_posix()
            raise DeployRefused(
                RefusalReason.PLAINTEXT_SECRET_REFUSED,
                f"A high-confidence plaintext secret was found in a deployed file "
                f"({rel}). Move it to `wrangler secret put` — refusing to publish a "
                "plaintext secret to the public edge (fail closed).",
            )


def _assert_scanned_path_is_safe(path: Path, served_root: Path, workspace_root: Path) -> None:
    """Apply the name-deny check, then (for a recognised text suffix OR a
    credential-stem file that survived the name check) the content-value scan."""
    is_credential_name = _assert_file_not_credential_named(path, served_root)
    if path.suffix.lower() not in _SCANNED_TEXT_SUFFIXES and not is_credential_name:
        return
    _assert_file_has_no_secret_value(path, workspace_root)


def _assert_no_secret_shaped_files(workspace: Path) -> None:
    """SEC-17/18: a WHOLE-TREE plaintext-secret scan of the (immutable, staged) deploy
    tree, beyond the ``.env`` / ``[vars]`` NAME check in :func:`_assert_no_plaintext_secrets`:

      * SEC-17 (deployable-file DENYLIST): REFUSE any credential-bearing file by name
        (``.pem``/``.key``/``id_rsa``/``.npmrc``/``.env*``/``.dev.vars``/…) anywhere in the
        tree — these never belong in a published app; and
      * SEC-18 (secret-SHAPED VALUE scan): REFUSE when any scanned TEXT file's CONTENT
        carries a high-confidence secret-shaped token (PEM key, AWS/GitHub/Slack/Stripe/
        Cloudflare/Anthropic/OpenAI/Google key, JWT) — a real secret pasted into source
        or config would otherwise ship plaintext to the public edge.

    SEC-17 (served-tree scope, P1): the template/placeholder exemption
    (:data:`_DEPLOY_TEMPLATE_SUFFIX_RE`) is honoured ONLY OUTSIDE the served-assets
    directory (the resolved ``[assets].directory`` — the bytes Cloudflare publishes as
    PUBLIC static assets). A credential-named file INSIDE that served tree is denied
    OUTRIGHT — EVEN with a ``.example``/``.sample``/``.template`` suffix — because a
    credential-named PUBLIC asset has no legitimate purpose; this closes the
    ``dist/secrets.env.example`` public-leak (an exempted-by-name, never-content-scanned
    template carrying a real ``sk-…``/``cfut_…`` token uploaded as a public asset). The
    legitimate exemption is the canonical app's ROOT ``.dev.vars.example`` placeholder,
    which lives OUTSIDE the served dir and CF never serves.

    SEC-18 (exempted-file content scan, belt-and-suspenders): a credential-stem template
    that survives the name denial (a ``.example``/``.sample`` OUTSIDE the served tree) is
    STILL content-scanned for a real secret VALUE — even though its ``.example`` suffix is
    not in :data:`_SCANNED_TEXT_SUFFIXES`. A genuine placeholder (``changeme``,
    ``replace-me``, ``your-token-here``) passes; a real token (``sk-…``/``cfut_…``) embedded
    in a root ``.dev.vars.example`` is caught.

    Fail CLOSED (the dedicated ``PLAINTEXT_SECRET_REFUSED`` reason, matching
    :func:`_assert_no_plaintext_secrets`). Reads route through the guarded reader.
    Runs on the STAGED copy, so the tree is already symlink/hardlink-free."""
    workspace_root = workspace.resolve()
    # The served-assets subtree (resolved the SAME way the deploy does — see
    # :func:`_deploy_asset_dir_rel`), i.e. the exact dir wrangler uploads PUBLICLY.
    # Resolved through the parent facade's OWN binding (not a direct import) so a
    # test/operator patch of ``deploy._deploy_asset_dir_rel`` is actually observed
    # here (mirroring the pattern in ``core/release/local_compose_parts``). Computed
    # lexically under *workspace* so it lines up with the (already symlink-free) staged
    # paths from :func:`_iter_tree_files`. May raise the deploy's own ASSET_DIR refusals on
    # an absolute/``..``/root assets dir — that is a deploy refusal too (fail closed).
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    served_root = workspace / _deploy._deploy_asset_dir_rel(workspace)
    for path in _collect_secret_scan_paths(workspace, served_root):
        _assert_scanned_path_is_safe(path, served_root, workspace_root)


def _assert_wrangler_allowlisted(workspace: Path) -> None:
    """SEC-5: parse wrangler.toml and ENFORCE the top-level-key allowlist before deploy.
    A key outside :data:`_ALLOWED_WRANGLER_KEYS` (``routes``/custom domains/``build``
    hooks/cron ``triggers``/``vars``/extra bindings/``account_id``) would let a generated
    app broaden its own blast radius past the lead-capture contract — or, for
    ``account_id``, redirect the deploy to a DIFFERENT account than the owner confirmed —
    so REFUSE (fail closed). A malformed TOML is left to the export-readiness gate (which
    fails closed on it)."""
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    text = _deploy.read_workspace_file(workspace / "wrangler.toml", workspace.resolve())
    if not text:
        return
    try:
        cfg = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return
    extra = sorted(k for k in cfg if k not in _ALLOWED_WRANGLER_KEYS)
    if extra:
        raise DeployRefused(
            RefusalReason.WRANGLER_CONFIG_REJECTED,
            "wrangler.toml has unexpected top-level keys that broaden the deploy blast "
            f"radius: {extra}. Custom routes/domains, build hooks, cron triggers, "
            "[vars], and extra bindings are not allowed — refusing to deploy "
            "(fail closed).",
        )
    # SEC-1-class: ``main`` is allowlisted as a KEY above, but its VALUE decides which
    # file ``wrangler deploy`` runs as the Worker. The canonical-worker match gate
    # (_assert_worker_is_canonical) validates ``worker/index.ts`` — so ``main`` MUST name
    # EXACTLY that file, else a look-alike (``....worker/index.ts``), an absolute path, or
    # a ``..`` escape would deploy a DIFFERENT, unchecked Worker (and dodge the SEC-10
    # ownership preflight). Exact-match (no lstrip) — fail closed.
    _assert_main_is_canonical(cfg)


def _assert_main_is_canonical(cfg: dict[str, object]) -> None:
    """SEC-1-class: REQUIRE wrangler.toml ``main`` to EXACTLY name the canonical worker
    entry (``worker/index.ts``) — else :class:`DeployRefused` (``MAIN_NOT_CANONICAL``,
    fail closed). ``wrangler deploy`` runs whatever ``main`` points at, while the
    canonical-worker match gate validates ``worker/index.ts``; this ties the two so the
    file we canonical-check IS the file wrangler runs. Reuses the SINGLE pure matcher
    (:func:`main_points_at_worker_entry`) shared with the export-readiness gate (no
    ``lstrip`` — ``"....worker/index.ts"`` / absolute / ``..`` all refuse)."""
    main = cfg.get("main")
    if not main_points_at_worker_entry(main):
        raise DeployRefused(
            RefusalReason.MAIN_NOT_CANONICAL,
            "wrangler.toml `main` does not EXACTLY name the canonical worker entry "
            f'(main = "worker/index.ts"); got {main!r}. `wrangler deploy` runs whatever '
            "`main` points at, but the canonical-worker gate validates worker/index.ts — a "
            "non-canonical main (absolute path, '..' escape, or a look-alike dir like "
            "'....worker/index.ts') would deploy a DIFFERENT, unchecked Worker. Refusing "
            "(fail closed).",
        )


def _assert_schema_safe_for_adopt(workspace: Path) -> None:
    """SEC-11/CORR-9: before applying ``schema.sql`` to an ADOPTED D1 database, validate
    that EVERY statement is idempotent ``CREATE`` DDL (``CREATE TABLE IF NOT EXISTS`` /
    ``CREATE INDEX`` / ``CREATE VIEW`` / ``CREATE TRIGGER``). A destructive or
    data-mutating statement (DROP/DELETE/ALTER/UPDATE/INSERT/…) against an owner's
    existing database is REFUSED (fail closed) — a fresh ``d1 create`` is unaffected
    (empty DB), but adoption must never run owner-data-destroying SQL."""
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    text = _deploy.read_workspace_file(workspace / "schema.sql", workspace.resolve()) or ""
    cleaned = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("--"))
    for stmt in cleaned.split(";"):
        s = stmt.strip()
        if not s:
            continue
        if _DESTRUCTIVE_SQL_RE.match(s):
            raise DeployRefused(
                RefusalReason.SCHEMA_UNSAFE,
                "schema.sql contains a destructive/data-mutating statement that would "
                f"run against the ADOPTED D1 database: {s.splitlines()[0][:80]!r}. "
                "Refusing — an adopted DB accepts only idempotent CREATE DDL "
                "(fail closed).",
            )
        if not re.match(r"^CREATE\b", s, re.IGNORECASE):
            raise DeployRefused(
                RefusalReason.SCHEMA_UNSAFE,
                "schema.sql contains a non-CREATE statement that would run against the "
                f"ADOPTED D1 database: {s.splitlines()[0][:80]!r}. Refusing (fail "
                "closed).",
            )
