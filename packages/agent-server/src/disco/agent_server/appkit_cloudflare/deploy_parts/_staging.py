"""Workspace reading + digests, and the served-assets-directory resolution +
deploy-time symlink scan.

Extracted from ``deploy.py`` to reduce module complexity; the public facade
re-imports these names unchanged. ``_stage_deploy_tree`` itself (the immutable
staged copy builder) stays defined directly on the ``deploy`` facade rather than
here — see that function's docstring on ``deploy.py`` for why.

MONKEYPATCH NOTE: ``_deploy_asset_dir_rel`` is one of the four names tests
monkeypatch directly on the ``deploy`` facade module. Every OTHER caller of it
(including :func:`_run_real_deploy` in ``_execute_phases.py`` and
:func:`_assert_no_secret_shaped_files` in ``_guards.py``) resolves it through the
parent module's OWN binding at call time so a patch on ``deploy._deploy_asset_dir_rel``
is actually observed (mirroring the pattern in ``core/release/local_compose_parts``).

``read_workspace_file`` itself is imported from the ``deploy`` facade (not a
sibling ``deploy_parts`` module) because it is DEFINED there — see ``_workspace.py``'s
docstring. deploy.py defines it before importing this module, so the reverse
reference resolves against the already-partially-initialised facade.
"""

from __future__ import annotations

import hashlib
import os
import tomllib
from pathlib import Path, PurePosixPath

from disco.core.appkit.local_verify import CF_EXPORT_FILES

from ..models import DeployRefused, RefusalReason
from ._constants import _APPSPEC_RELPATH, _TREE_SKIP_DIRS, _TREE_SKIP_FILES

# ---- workspace reading + digests (pure, read-only) ---------------------------


def _read_export_files(workspace: Path) -> dict[str, str | None]:
    """Read the CF export deliverables + the optional real ``.dev.vars`` from the
    workspace, exactly as ``verify_appkit_app`` feeds ``cloudflare_export_ready``.

    P0 (round 6): EVERY read here goes through the one containment-guarded reader
    (``read_workspace_file``). The ``.dev.vars`` (the secret-safety input) and
    each ``CF_EXPORT_FILES`` deliverable could be a SYMLINK escaping the workspace
    (→ a host secret); the guard refuses it BEFORE any dereference, so this
    export-readiness path — called before ``_tree_digest`` — can never read a host
    secret through an escaping link."""
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    workspace_root = workspace.resolve()
    files: dict[str, str | None] = {}
    for rel in CF_EXPORT_FILES:
        files[rel] = _deploy.read_workspace_file(workspace / rel, workspace_root)
    files[".dev.vars"] = _deploy.read_workspace_file(workspace / ".dev.vars", workspace_root)
    return files


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iter_tree_files(workspace: Path) -> list[Path]:
    """Every deploy-relevant file under *workspace* (recursive), minus the skipped
    subtrees/files. Sorted so the digest is order-independent + stable.

    ``os.walk`` does NOT follow symlinked directories (``followlinks=False``), so a
    symlinked subtree is never descended into; a symlink-to-FILE, however, appears
    in ``names`` and IS returned here — the containment guard in ``_tree_digest``
    (the shared ``workspace_contained`` check) rejects it before any read."""
    found: list[Path] = []
    for root, dirs, names in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _TREE_SKIP_DIRS]
        for name in names:
            if name in _TREE_SKIP_FILES:
                continue
            found.append(Path(root) / name)
    return sorted(found)


def _tree_digest(workspace: Path) -> str:
    """A stable digest over the ENTIRE deployable app tree — EVERY file the
    build+deploy actually consumes: ``package.json``, the lockfile, ALL
    client/worker source, ``tsconfig``/``vite`` config, the schema, and any
    pre-built output — NOT merely the ``CF_EXPORT_FILES`` deliverables. The
    confirmation phrase is bound (via ``plan_hash``) to this digest, so a drift to
    ANY build-affecting file — including the workspace-controlled build script
    (``package.json`` ``"build"``) and the client source ``npm run build``
    compiles — changes the hash and INVALIDATES a stale confirmation. Excludes
    regenerated/non-authored trees + the local secret (see ``_TREE_SKIP_DIRS`` /
    ``_TREE_SKIP_FILES``).

    P0 (round 5/6): EVERY file read here goes through the SINGLE guarded reader
    (``read_workspace_file``), which applies the SAME containment guard the
    push uses. An escaping symlink (→ a host secret) is REFUSED — the reader raises
    :class:`DeployRefused` and the symlink target's bytes are NEVER dereferenced
    into the digest/plan_hash. So no host-secret read can happen on the planner
    path, before the push guard ever runs."""
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    workspace_root = workspace.resolve()
    h = hashlib.sha256()
    for path in _iter_tree_files(workspace):
        rel = path.relative_to(workspace).as_posix()
        # Raises DeployRefused(WORKSPACE_SYMLINK_ESCAPE) on an escaping symlink,
        # BEFORE any read — fail closed rather than hash a host secret into plan_hash.
        data = _deploy.read_workspace_file(path, workspace_root, text=False)
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(data if data is not None else b"<unreadable>")
        h.update(b"\0")
    return h.hexdigest()


def _spec_digest(workspace: Path) -> str:
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    # Guarded read: a ``.disco/appspec.json`` that is an escaping symlink refuses
    # (WORKSPACE_SYMLINK_ESCAPE) before any dereference.
    text = _deploy.read_workspace_file(workspace / _APPSPEC_RELPATH, workspace.resolve())
    return _digest_text(text or "")


# ---- served-assets directory resolution + deploy-time symlink scan -----------


def _deploy_asset_dir_rel(workspace: Path) -> str:
    """The workspace-relative static-assets directory ``wrangler deploy`` uploads
    (the ``[assets] directory`` in wrangler.toml — e.g. ``./dist``). This is the EXACT
    tree wrangler walks and FOLLOWS symlinks into when it publishes, so the resolution
    here must match wrangler's byte-for-byte (SEC-1/CORR-1/CORR-13).

    The previous ``lstrip("./")`` normalisation was unsafe: it stripped leading ``.``
    and ``/`` *characters*, so ``"../x"`` became ``"x"``, ``"/x"`` became ``"x"``, and
    ``".git"`` became ``"git"`` — DESYNCING our pre-deploy symlink scan from the tree
    wrangler actually reads. This version resolves the value EXACTLY:

      * a leading ``./`` and ``.`` path-segments are dropped (``"./dist"`` == ``dist``);
      * an ABSOLUTE path or any ``..`` segment is REFUSED (fail closed);
      * ``"."`` (the whole workspace) resolves to the root and is scanned as-is;
      * every other name (incl. ``.git``) is kept verbatim.

    Defaults to ``dist`` only when wrangler.toml is absent/unparseable or omits the
    key. Read through the guarded reader so an escaping wrangler.toml symlink still
    fails closed."""
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    text = _deploy.read_workspace_file(workspace / "wrangler.toml", workspace.resolve())
    if not text:
        return "dist"
    try:
        cfg = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return "dist"
    assets = cfg.get("assets")
    if not isinstance(assets, dict):
        return "dist"
    directory = assets.get("directory")
    if not isinstance(directory, str) or not directory.strip():
        return "dist"
    raw = directory.strip()
    p = PurePosixPath(raw)
    if p.is_absolute():
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            f"The wrangler [assets] directory is an ABSOLUTE path: {raw}. Refusing to "
            "deploy — the assets dir must be inside the workspace (fail closed).",
        )
    parts = [seg for seg in p.parts if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            f"The wrangler [assets] directory escapes the workspace via '..': {raw}. "
            "Refusing to deploy (fail closed).",
        )
    if not parts:
        # SEC-1/CORR-16: the assets dir resolves to the workspace ROOT (``.``/``./``).
        # Publishing the whole workspace would ship source + stale/non-built files (and
        # ``wrangler deploy`` would upload them). Refuse — assets must be a real built
        # subdirectory (e.g. ``dist``).
        raise DeployRefused(
            RefusalReason.ASSET_DIR_UNSAFE,
            f"The wrangler [assets] directory resolves to the workspace root ({raw!r}). "
            "Refusing to publish source/stale files — set it to the built output dir "
            "(e.g. ./dist).",
        )
    # SEC-17 (P1, the .disco/public bypass): the served [assets].directory must NOT be —
    # or be NESTED UNDER — an internal/regenerated root the deploy-tree scan globally skips
    # (``_TREE_SKIP_DIRS``: .disco/node_modules/.git/.wrangler). Such a served dir hides the
    # PUBLISHED bytes from :func:`_assert_no_secret_shaped_files` (it walks via
    # :func:`_iter_tree_files`, which prunes those roots), so wrangler could upload a
    # reassembled ``sk-…``/``cfut_…`` token as a PUBLIC asset that the scan never sees. The
    # served assets must be a normal built directory (the generated app uses ``dist``);
    # a served dir buried inside a skipped location is an injection → REFUSE (fail closed).
    if any(seg in _TREE_SKIP_DIRS for seg in parts):
        raise DeployRefused(
            RefusalReason.ASSET_DIR_UNSAFE,
            f"The wrangler [assets] directory is nested under an internal/skipped root "
            f"({raw!r}): served assets must be a normal built directory (e.g. ./dist), not "
            "hidden inside .disco/node_modules/.git/.wrangler — the secret-shaped-file scan "
            "skips those roots, so a served dir there would publish unscanned bytes. "
            "Refusing to deploy (fail closed).",
        )
    return PurePosixPath(*parts).as_posix()


def assert_deploy_tree_symlink_free(workspace: Path, asset_dir_rel: str) -> Path:
    """P0 (round 9): SCAN the EXACT directory ``wrangler deploy`` uploads (the
    ``[assets] directory``, e.g. ``./dist``) for ANY symlink — file OR directory, at
    ANY depth — and REFUSE (:class:`DeployRefused`, fail closed) if one is present.
    Called IMMEDIATELY before invoking ``wrangler deploy`` (after the build + the
    sync-back has regenerated dist), so it judges the tree wrangler actually uploads.

    WHY this is needed despite the read-class (round 6) + write-class (round 8) +
    sync-back (round 7) guards: ``wrangler deploy`` FOLLOWS symlinks when it uploads
    ``./dist``, so a PREEXISTING symlink under dist (planted in the workspace, or in a
    subtree the sync-back never overwrites) would have its TARGET — a host secret —
    published to the PUBLIC Cloudflare site. The plan-time ``_tree_digest`` CANNOT
    catch it: it runs BEFORE the build regenerates dist, and ``os.walk`` with
    ``followlinks=False`` SILENTLY SKIPS symlinked subtrees (never descends, never
    hashes), so the symlink is invisible to both the digest and the containment guard
    yet still uploaded. The dangerous read is performed by wrangler, not our code, so
    only a deploy-time, full-tree scan of the upload directory closes it.

    The scan walks with ``followlinks=False`` but DETECTS symlinks via ``is_symlink``
    (lstat) on BOTH the directory names and the file names at every level — it never
    silently skips a link, it FAILS on it. Each path component of *asset_dir_rel*
    itself is also lstat-checked (a symlinked ``dist`` root, or any intermediate, is
    refused before the walk). A clean, symlink-free dist passes and returns the
    absolute asset dir; ANY symlink anywhere → ``DeployRefused`` and the deploy never
    fires. Done at DEPLOY time (not just plan time) to defeat TOCTOU."""
    workspace_root = workspace.resolve()
    rel = Path(asset_dir_rel)
    if rel.is_absolute() or any(part == ".." for part in rel.parts):
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            f"The deploy assets directory is absolute or escapes via '..': "
            f"{asset_dir_rel}. Refusing to deploy — fail closed.",
        )
    # lstat each component of the asset dir path: a symlinked dist root (or any
    # intermediate) is rejected before we even walk it.
    asset_dir = workspace_root
    for part in rel.parts:
        asset_dir = asset_dir / part
        if asset_dir.is_symlink():
            raise DeployRefused(
                RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                f"The deploy assets directory component is a symlink: {part} "
                f"(in {asset_dir_rel}). wrangler would follow it on upload and "
                "publish the link target to the PUBLIC site — refusing to deploy "
                "(fail closed).",
            )
    # CORR-1: the resolved assets dir must be an EXISTING directory — the scan must
    # run on the EXACT tree wrangler uploads, not a phantom path.
    if not (asset_dir.exists() and asset_dir.is_dir()):
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            f"The deploy assets directory does not exist or is not a directory: "
            f"{asset_dir_rel}. Refusing to deploy (fail closed).",
        )
    # Walk the ENTIRE asset tree; reject ANY symlink (dir OR file) at any depth.
    for root, dirs, names in os.walk(asset_dir, followlinks=False):
        root_path = Path(root)
        for entry in (*dirs, *names):
            candidate = root_path / entry
            if candidate.is_symlink():
                kind = "DIRECTORY" if entry in dirs else "FILE"
                try:
                    shown = candidate.relative_to(workspace_root).as_posix()
                except ValueError:
                    shown = candidate.name
                raise DeployRefused(
                    RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                    f"A symlinked {kind} is present in the deploy tree: {shown}. "
                    "wrangler follows symlinks on upload — it would publish the link "
                    "target (a possible host secret) to the PUBLIC Cloudflare site. "
                    "Refusing to deploy (fail closed); remove the symlink and redeploy.",
                )
    return asset_dir
