"""AppKit EPIC O — the PRODUCTION sandboxed build backend (P0-1/P0-2).

The deploy's build step runs WORKSPACE/AGENT-controlled code (``package.json``'s
``"build"`` script). Running that as the same OS user — even with a sanitized env
— is NOT a complete secret boundary: the script can READ host secrets off the
filesystem (the SecretStore files, ``~/.config/disco``, provider keys). So the
build runs INSIDE Disco's existing sandbox (the gVisor/container infra the
deck-PDF export uses for ``soffice``), with NO access to host secrets/filesystem.

Flow (mirrors the transient-sandbox pattern of ``_render_deck_pdf_in_sandbox``):

  1. Spin a THROWAWAY sandbox from the Build spec (filtered egress: the npm
     registry is reachable so ``npm ci`` can install, but nothing host-side is).
  2. Push the HASH-COVERED deploy tree in (exactly the files the plan_hash binds —
     ``package.json`` + the lockfile + all source; never ``node_modules``/``.git``/
     ``.disco``/``.dev.vars``).
  3. Run ``npm ci && npm run build`` in the box — ``npm ci`` is DETERMINISTIC,
     installing EXACTLY from the hash-covered ``package-lock.json`` (P0-2), not a
     bare build on arbitrary pre-installed ``node_modules``.
  4. Sync the built ``./dist`` back to the host workspace so the trusted
     ``wrangler deploy`` (which carries the CF token, but the untrusted build does
     NOT) publishes an artifact derived only from hash-covered inputs.
  5. Tear the box down.

The CF token NEVER enters this sandbox — only the trusted wrangler steps (run on
the host, see ``deploy.py``) ever see it.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import uuid as _uuid
from pathlib import Path
from typing import TYPE_CHECKING

from disco.core import DEFAULT_OWNER_ID
from disco.core.env import disco_env
from disco.tools import Capability

from .deploy import (
    ensure_workspace_dir,
    read_workspace_file,
    workspace_contained,
    write_workspace_file,
)
from .models import DeployRefused
from .wrangler import BuildResult

if TYPE_CHECKING:
    from ..runtime import ConversationRuntime

_log = logging.getLogger(__name__)

#: DEPLOY-GRADE isolation backends — a real boundary between the workspace/agent
#: controlled build and host secrets/filesystem. ``gvisor`` is a user-space kernel
#: (strong); ``podman`` is rootless containers behind a remote-host boundary. The
#: ``process`` backend runs tools directly on the host (same user, host FS) → NOT a
#: boundary. ``local`` is a SHARED-host-kernel container (runc): container-grade but
#: same host → NOT deploy-grade for an untrusted build by default (it is allowed
#: ONLY behind an explicit high-risk override, see ``allow_local_isolation``). A
#: deploy that would build on a non-deploy-grade backend is REFUSED before any
#: sandbox is created.
_DEPLOY_GRADE_BACKENDS: frozenset[str] = frozenset({"gvisor", "podman"})

#: Env switch (``DISCO_APPKIT_DEPLOY_ALLOW_LOCAL=1``/``true``/``yes``/``on``) that
#: turns the weak shared-kernel ``local`` backend into an EXPLICIT, opt-in
#: deploy-grade override for trusted local use. Never the silent default.
_ALLOW_LOCAL_ENV_SUFFIX = "APPKIT_DEPLOY_ALLOW_LOCAL"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _is_deploy_grade(name: str | None, *, allow_local: bool) -> bool:
    """True only when *name* is a deploy-grade isolation backend (``gvisor``/
    ``podman``), or ``local`` AND the explicit high-risk override is set. Everything
    else (``process``, ``None``, an unknown backend) is refused — fail closed, never
    assume isolation we cannot name."""
    if name in _DEPLOY_GRADE_BACKENDS:
        return True
    return bool(name == "local" and allow_local)


#: The npm registry host a deploy build's egress allowlist MUST be able to reach (so
#: ``npm ci`` can install). Its reachability proves the resolved spec is a REAL build
#: allowlist (the registry/CDN posture), not an arbitrary or accidentally-empty one.
_REQUIRED_EGRESS_HOST = "registry.npmjs.org"


def _is_overbroad_egress_entry(entry: str) -> bool:
    """True when an allowlist *entry* is too BROAD for a deploy build — open-ended
    egress even under a nominally "filtered" posture.

    Rejected: a wildcard (any ``*``); an empty / ``.``-only entry; a leading-dot
    SUFFIX that covers only a bare public suffix / single label (``.com``, ``.org``,
    ``.io``) — which would open egress to an ENTIRE TLD. A legitimate suffix names at
    least a registrable domain (``.npmjs.org`` / ``.githubusercontent.com`` → two or
    more labels) and an exact host (``registry.npmjs.org``) is fine."""
    e = entry.strip().lower()
    if not e or "*" in e:
        return True
    if e.startswith("."):
        labels = [p for p in e.strip(".").split(".") if p]
        # `.com` → ["com"] (one label) is a bare TLD → too broad; require ≥ 2 labels.
        return len(labels) < 2
    return False


def _spec_is_filtered_egress(spec: object) -> bool:
    """A deploy build MUST run under FILTERED egress (the allowlisting proxy: the npm
    registry is reachable so ``npm ci`` installs, nothing host-side is). Verify the
    resolved spec is filtered rather than ASSUMING it. Reject:

    * an OPEN-network spec (full ``NETWORK`` capability);
    * an EMPTY allowlist;
    * a WILDCARD / overly-broad allowlist (a bare-TLD suffix like ``.com`` or any
      ``*`` entry) — a deploy build must not have open-ended egress (SEC-21);
    * an allowlist that cannot reach the expected npm registry host — i.e. NOT a real
      build allowlist.

    Fail CLOSED when the spec cannot be introspected (no ``egress_allow``/``permitted``)."""
    permitted = getattr(spec, "permitted", None)
    egress = getattr(spec, "egress_allow", None)
    if permitted is None or egress is None:
        return False
    if Capability.NETWORK in permitted:  # open network → not filtered
        return False
    if not egress:
        return False
    # SEC-21: reject any wildcard / bare-TLD-suffix entry — open-ended egress is never
    # acceptable for an untrusted deploy build, even under the "filtered" posture.
    if any(_is_overbroad_egress_entry(e) for e in egress):
        return False
    # The allowlist must actually reach the npm registry (so this IS a build allowlist,
    # not an arbitrary one). Use the spec's own matcher when available so a leading-dot
    # suffix (``.npmjs.org``) or an exact host both satisfy it; fall back to membership.
    allowed = getattr(spec, "egress_allowed", None)
    if callable(allowed):
        try:
            return bool(allowed(_REQUIRED_EGRESS_HOST))
        except Exception:  # noqa: BLE001 — a broken matcher → fail closed
            return False
    return _REQUIRED_EGRESS_HOST in egress


#: The DEFAULT build output directory (vite ``build.outDir`` / wrangler ``[assets]``
#: ``directory = "./dist"`` in the generated app). Synced back to the host so
#: ``wrangler deploy`` finds the built SPA. The app may configure a different
#: directory (e.g. ``build/``) — ``build()``/``_sync_output_back`` accept it as the
#: optional ``asset_dir`` param.
_BUILD_OUTPUT_DIR = "dist"

#: Caps on the synced build output — reject an oversized / file-bomb output before
#: it is read or written back to the host (a deploy artifact, not an archive).
_MAX_OUTPUT_FILES = 20_000
_MAX_OUTPUT_BYTES = 256 * 1024 * 1024  # 256 MiB total

#: Top-level subtrees/files NOT pushed into the build sandbox — identical to the
#: deploy-tree digest's skip set (``deploy._TREE_SKIP_DIRS``/``_TREE_SKIP_FILES``),
#: so the box sees EXACTLY the hash-covered inputs and nothing else.
_PUSH_SKIP_DIRS: frozenset[str] = frozenset({"node_modules", ".git", ".disco"})
_PUSH_SKIP_FILES: frozenset[str] = frozenset({".dev.vars"})

#: Ceiling for the install+build (npm ci can be slow). Generous but bounded.
_BUILD_TIMEOUT_S = 600


class _BuildOutputRejected(Exception):
    """Base for EVERY reason the sandbox build's output is rejected (escape, missing/
    unreadable output, or over-cap). Raised by
    :func:`SandboxBuildBackend._sync_output_back` and converted by :meth:`build` to a
    FAILED (un-synced) :class:`BuildResult` so the deploy aborts before any Cloudflare
    mutation and nothing bad is written back to the host workspace."""


class _BuildOutputEscape(_BuildOutputRejected):
    """A sandboxed build emitted an output path that resolves OUTSIDE the asset dir
    (``./dist``). An untrusted build can never write outside it onto host files."""


class _BuildOutputUnavailable(_BuildOutputRejected):
    """The build's output could not be listed (``find`` failed), was EMPTY (no
    artifacts to deploy), or a listed file read back as ``None``. Treated as a BUILD
    FAILURE — fail closed rather than silently deploying a missing/empty artifact."""


class _BuildOutputTooLarge(_BuildOutputRejected):
    """The build emitted too many files or too many bytes (a file-bomb / runaway
    output). Rejected before it is written back to the host workspace."""


def _contained_dest(
    rel: str, workspace: Path, dist_root: Path, workspace_root: Path
) -> Path | None:
    """Resolve the host destination for a sandbox-listed build-output *rel* and
    return it ONLY when it stays STRICTLY inside BOTH the resolved ``dist_root``
    AND the resolved ``workspace_root``.

    Returns ``None`` (reject) for anything that would write outside ``./dist`` or
    outside the workspace: an absolute path, any ``..`` traversal component, or a
    symlink that escapes either root (``Path.resolve`` follows existing symlinks,
    so a resolved dest that is not relative to the resolved dist root — or, via an
    intermediate-dir symlink, not relative to the resolved workspace root — is
    rejected). The dist root itself is rejected too (a synced entry is always a
    file *under* dist). Anchoring to the workspace root as well means a planted
    intermediate symlink that points inside ``dist_root`` but whose resolved target
    leaves the workspace can never validate."""
    p = Path(rel)
    if p.is_absolute() or any(part == ".." for part in p.parts):
        return None
    dest = (workspace / rel).resolve()
    if dest == dist_root or not dest.is_relative_to(dist_root):
        return None
    if not dest.is_relative_to(workspace_root):
        return None
    return dest


class _PushPathEscape(Exception):
    """A workspace file selected for the PUSH (host → build sandbox) resolves
    OUTSIDE the workspace root — i.e. it is (or traverses) a SYMLINK pointing at a
    host file (a SecretStore file, ``~/.config/disco``, a provider key). Reading it
    would dereference the symlink and push host-secret content into the build
    sandbox (and potentially the deployed artifact). Raised by
    :func:`SandboxBuildBackend.build` and converted to a FAILED build so nothing is
    read through the escaping link and the deploy aborts before any mutation."""


def _push_src_contained(full: Path, workspace_root: Path) -> bool:
    """The PUSH-side containment check — a thin alias over the SHARED guard
    (:func:`deploy.workspace_contained`) so the host→sandbox push and the
    planner/tree-digest reads (``deploy._tree_digest``) use ONE check and can NEVER
    drift. See that helper for the semantics: a workspace entry that is (or sits
    under) a symlink escaping the workspace resolves OUTSIDE *workspace_root* and is
    rejected, so we never ``read_bytes()`` through it; a normal regular file inside
    the workspace is allowed; fail closed on any resolution error."""
    return workspace_contained(full, workspace_root)


def _deployable_relpaths(workspace: Path) -> list[str]:
    """Every hash-covered file under *workspace* as a workspace-relative POSIX
    path, minus the skipped subtrees/files. Same selection the plan_hash digests."""
    import os

    out: list[str] = []
    for root, dirs, names in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _PUSH_SKIP_DIRS]
        for name in names:
            if name in _PUSH_SKIP_FILES:
                continue
            rel = (Path(root) / name).relative_to(workspace).as_posix()
            out.append(rel)
    return sorted(out)


class SandboxBuildBackend:
    """PRODUCTION :class:`BuildBackend` — runs the untrusted build in an isolating
    Disco sandbox. Constructed from the live runtime; used only by the owner's real
    deploy (never the test suite, which injects a fake)."""

    def __init__(
        self,
        runtime: ConversationRuntime,
        *,
        owner_id: str = DEFAULT_OWNER_ID,
        allow_local_isolation: bool = False,
    ) -> None:
        self._runtime = runtime
        self._owner_id = owner_id
        #: High-risk operator opt-in: treat the weak shared-kernel ``local`` backend
        #: as deploy-grade. OFF by default — gVisor/podman only.
        self._allow_local = allow_local_isolation

    @property
    def isolates(self) -> bool:
        """The deploy.py PRE-gate predicate (a best-effort, separate lookup). NOTE:
        :meth:`build` does NOT trust this — it re-resolves the service ONCE and
        re-verifies the SAME instance's isolation kind (closing the check→use race),
        so a settings flip between this gate and the build cannot run on the host."""
        try:
            return _is_deploy_grade(
                self._runtime.sandbox_backend_name(), allow_local=self._allow_local
            )
        except Exception:  # noqa: BLE001 — any lookup failure → not isolating → refuse
            return False

    async def build(
        self,
        workspace: Path,
        *,
        install_cmd: str,
        build_cmd: str,
        asset_dir: str = _BUILD_OUTPUT_DIR,
    ) -> BuildResult:
        # ISOLATION RACE (SEC-19/CORR-20): resolve the sandbox service EXACTLY ONCE
        # and verify ITS isolation kind on the SAME instance we then build on — the
        # old code did a separate `sandbox_backend_name()` lookup for the gate and
        # another `_sandbox_service_now()` for the build, so a settings race
        # (gvisor→process) between them could run the untrusted build on the HOST.
        # One fetch, one verify, one use.
        # CORR-20: the SERVICE lookup itself can raise (no backend configured, a
        # provider import error) — convert it to a structured failed BuildResult, never
        # let it escape as a 500 to the deploy caller.
        try:
            svc = self._runtime._sandbox_service_now()
        except Exception as exc:  # noqa: BLE001 — surface as a failed build, not a 500
            _log.warning("appkit deploy build: sandbox service lookup failed: %s", exc)
            return BuildResult(
                returncode=-1,
                stdout="",
                stderr=f"[deploy build failed: could not resolve sandbox service: {exc}]",
                isolated=False,
            )
        backend_name = getattr(svc, "name", None)
        if not _is_deploy_grade(backend_name, allow_local=self._allow_local):
            # Defense in depth — the executor/deploy gate already refused, but never
            # run the untrusted build on a non-deploy-grade backend (process, the weak
            # shared-kernel `local` without override, or an unknown/None kind).
            return BuildResult(
                returncode=-1,
                stdout="",
                stderr=(
                    f"[deploy build refused: sandbox backend {backend_name!r} is not "
                    "deploy-grade isolation (require gVisor or podman; `local` is "
                    "shared-kernel and opt-in only). Refusing to build on the host.]"
                ),
                isolated=False,
            )
        # Filtered-egress Build spec: the npm registry is reachable so `npm ci`
        # installs; nothing host-side is. ENFORCE it — a deploy build must not run
        # with open egress (don't assume the default held).
        # CORR-20: the SPEC lookup can also raise (bad env/config) — structured failed
        # BuildResult, not an escaping 500.
        try:
            spec = self._runtime._build_sandbox_spec(surface="build")
        except Exception as exc:  # noqa: BLE001 — surface as a failed build, not a 500
            _log.warning("appkit deploy build: build sandbox spec lookup failed: %s", exc)
            return BuildResult(
                returncode=-1,
                stdout="",
                stderr=f"[deploy build failed: could not resolve build sandbox spec: {exc}]",
                isolated=False,
            )
        if not _spec_is_filtered_egress(spec):
            return BuildResult(
                returncode=-1,
                stdout="",
                stderr=(
                    "[deploy build refused: the build sandbox egress is not FILTERED "
                    "(open network or empty allowlist). A deploy build must run behind "
                    "the egress allowlist.]"
                ),
                isolated=False,
            )
        cid = f"appkit-deploy-build-{_uuid.uuid4().hex[:12]}"
        # SANDBOX CREATE failure → structured failed BuildResult, not a 500 (CORR-20).
        try:
            instance = await svc.create(spec, owner_id=self._owner_id, conversation_id=cid)
        except Exception as exc:  # noqa: BLE001 — surface as a failed build, not a 500
            _log.warning("appkit deploy build: sandbox create failed: %s", exc)
            return BuildResult(
                returncode=-1,
                stdout="",
                stderr=f"[deploy build failed: could not create sandbox: {exc}]",
                isolated=False,
            )
        try:
            workspace_root = workspace.resolve()
            for rel in _deployable_relpaths(workspace):
                full = workspace / rel
                if not _push_src_contained(full, workspace_root):
                    # P0 (PUSH side): a workspace SYMLINK escaping the workspace
                    # (→ a host secret) would dereference on read_bytes and push
                    # host-secret content INTO the build sandbox/artifact. FAIL
                    # CLOSED — reject the whole build, never read through it.
                    return BuildResult(
                        returncode=-1,
                        stdout="",
                        stderr=(
                            "[build input rejected: a workspace path resolves "
                            f"outside the workspace (symlink escape): {rel}]"
                        ),
                        isolated=True,
                    )
                # Read through the ONE guarded reader (containment already re-checked
                # by it) so the push never dereferences an escaping symlink and no
                # raw workspace read exists outside the choke point.
                data = read_workspace_file(full, workspace_root, text=False)
                await instance.write_file(rel, data if data is not None else b"")
            res = await instance.exec_shell(
                f"{install_cmd} && {build_cmd}", timeout_s=_BUILD_TIMEOUT_S
            )
            if getattr(res, "timed_out", False):
                return BuildResult(
                    returncode=res.exit_code or -1,
                    stdout=res.stdout,
                    stderr=(res.stderr or "") + "\n[build timed out]",
                    isolated=True,
                )
            if res.exit_code == 0:
                try:
                    await self._sync_output_back(instance, workspace, asset_dir=asset_dir)
                except _BuildOutputRejected as exc:
                    # The untrusted build's output is bad (escapes the asset dir, is
                    # missing/unreadable, or is over-cap). FAIL CLOSED — reject the
                    # whole build; never sync, never let the deploy proceed off it.
                    return BuildResult(
                        returncode=-1,
                        stdout=res.stdout,
                        stderr=(res.stderr or "") + f"\n[build output rejected: {exc}]",
                        isolated=True,
                    )
            return BuildResult(
                returncode=res.exit_code,
                stdout=res.stdout,
                stderr=res.stderr,
                isolated=True,
            )
        except Exception as exc:  # noqa: BLE001 — sandbox write/exec/read errors
            # CORR-20: a sandbox op (write/exec/read) raising must become a structured
            # FAILED build, not escape as a 500 to the deploy caller.
            _log.warning("appkit deploy build: sandbox op failed: %s", exc)
            return BuildResult(
                returncode=-1,
                stdout="",
                stderr=f"[deploy build failed: sandbox error during build: {exc}]",
                isolated=True,
            )
        finally:
            # Teardown + volume cleanup. SURFACE failures (log) rather than swallow
            # silently — a leaked deploy-build box/volume is an operability + cost
            # leak (SEC-24/CORR-21).
            try:
                await instance.destroy()
            except Exception as exc:  # noqa: BLE001 — best-effort, but never silent
                _log.warning("appkit deploy build: sandbox teardown failed for %s: %s", cid, exc)
                try:
                    # Belt-and-suspenders: sweep any container + egress sidecar +
                    # volumes left for this conversation.
                    await svc.destroy_by_conversation(cid)
                except Exception as exc2:  # noqa: BLE001
                    _log.warning("appkit deploy build: volume sweep failed for %s: %s", cid, exc2)

    async def _sync_output_back(
        self, instance: object, workspace: Path, *, asset_dir: str = _BUILD_OUTPUT_DIR
    ) -> None:
        """Replace the host workspace's *asset_dir* (default ``./dist``; the app may
        configure another, e.g. ``build/``) with the freshly-built tree from the box
        so the trusted ``wrangler deploy`` publishes only hash-covered output.

        ATOMIC + CAPPED + FAIL-CLOSED replace (CORR-16/17/18/19, SEC-22/23):

        * The CONFIGURED ``asset_dir`` is synced (validated to be a safe RELATIVE
          path — no absolute / ``..`` segment).
        * Output is enumerated with ``find … -print0`` (NULL-delimited, so a filename
          containing a newline can't corrupt the listing — SEC-23).
        * A ``find`` that FAILS (non-zero exit), an EMPTY output (no artifacts), or a
          file that reads back as ``None`` is a BUILD FAILURE (raises
          :class:`_BuildOutputUnavailable`) — never a silent success on missing
          output.
        * File-count + total-byte CAPS reject a runaway / file-bomb output
          (:class:`_BuildOutputTooLarge`).
        * Every listed path is validated to stay STRICTLY inside BOTH the resolved
          asset-dir root AND the resolved workspace root BEFORE it is read
          (:func:`_contained_dest`) — an absolute path, a ``..`` traversal, or a
          symlink (the asset-dir root itself, planted in the live tree, OR an
          intermediate dir) that escapes is REJECTED (:class:`_BuildOutputEscape`).
        * The validated bytes are staged into a FRESH sibling dir via the ONE guarded
          writer, then the stale host asset dir is CLEARED and the staged tree is
          moved into place with an atomic ``os.replace`` — stale files never survive,
          and a partially-written tree is never visible.

        SECURITY: the asset-dir / per-file containment is anchored to the resolved
        WORKSPACE ROOT (not merely the asset dir), and the asset dir must be a REAL
        directory (``is_symlink`` via lstat, NOT ``resolve`` — which would FOLLOW and
        MASK an in-workspace link) so build output lands ONLY in the real asset dir.
        Validation runs against the LIVE host tree BEFORE anything is cleared, so a
        planted symlink is still caught."""
        workspace_root = workspace.resolve()
        asset_rel = Path(asset_dir)
        if (
            asset_rel.is_absolute()
            or any(p == ".." for p in asset_rel.parts)
            or not asset_rel.parts
        ):
            raise _BuildOutputEscape(
                f"invalid asset dir (must be a relative path inside the workspace): {asset_dir!r}"
            )
        out_dir = workspace / asset_rel
        # The asset dir must be a REAL directory. lstat (``is_symlink``), NOT
        # ``resolve`` — resolve would FOLLOW an in-workspace link and MASK it, silently
        # broadening the sync target. A symlinked asset dir (in-workspace OR escaping)
        # is REJECTED.
        if out_dir.is_symlink():
            raise _BuildOutputEscape(
                f"{asset_dir} is a symlink; build output must land in the real ./{asset_dir}"
            )
        # Anchor to the workspace root: a now-rejected escaping link kept as
        # belt-and-suspenders (never write build output outside the workspace).
        if out_dir.exists() and not workspace_contained(out_dir, workspace_root):
            raise _BuildOutputEscape(f"{asset_dir} resolves outside the workspace")
        out_root = out_dir.resolve()
        listing = await instance.exec_shell(  # type: ignore[attr-defined]
            f"find {shlex.quote(asset_dir)} -type f -print0", timeout_s=60
        )
        if listing.exit_code != 0:
            # FAIL CLOSED: could not list the build output — NOT a silent success.
            raise _BuildOutputUnavailable(
                f"could not list build output under {asset_dir} (find exit={listing.exit_code})"
            )
        entries = [e for e in listing.stdout.split("\0") if e]
        if len(entries) > _MAX_OUTPUT_FILES:
            raise _BuildOutputTooLarge(
                f"build output has {len(entries)} files (cap {_MAX_OUTPUT_FILES})"
            )
        # PASS 1 — validate + read EVERY entry against the LIVE host tree (so a planted
        # symlink is caught) before anything is cleared. Read into a staging list.
        staged: list[tuple[str, bytes]] = []
        total_bytes = 0
        for entry in entries:
            rel = entry[2:] if entry.startswith("./") else entry
            if not rel:
                continue
            # Lexical + resolved guard (absolute / ``..`` / escaping-resolved) BEFORE read.
            if _contained_dest(rel, workspace, out_root, workspace_root) is None:
                raise _BuildOutputEscape(rel)
            data = await instance.read_file(rel)  # type: ignore[attr-defined]
            if data is None:
                # FAIL CLOSED: a listed output file that reads back as None is a build
                # failure, never an empty-file silent success.
                raise _BuildOutputUnavailable(f"build output file unreadable: {rel}")
            total_bytes += len(data)
            if total_bytes > _MAX_OUTPUT_BYTES:
                raise _BuildOutputTooLarge(f"build output exceeds {_MAX_OUTPUT_BYTES} bytes")
            staged.append((rel, data))
        if not staged:
            # An empty asset dir = nothing to deploy = a broken build. Fail closed.
            raise _BuildOutputUnavailable(f"build produced no output files under {asset_dir}")
        # PASS 2 — stage into a FRESH sibling dir via the ONE guarded writer, then
        # CLEAR the stale host asset dir and atomically move the staged tree in.
        stage_root = workspace / f".disco-build-out-{_uuid.uuid4().hex[:12]}"
        try:
            os.makedirs(stage_root)
            for rel, data in staged:
                # Guarded writer: LSTATs every component (refuses a symlinked one) —
                # no raw mkdir/write of a workspace path lives outside it.
                try:
                    write_workspace_file(rel, data, stage_root)
                except DeployRefused as exc:
                    raise _BuildOutputEscape(rel) from exc
            staged_out = stage_root / asset_rel
            if asset_rel.parent.parts:
                ensure_workspace_dir(asset_rel.parent, workspace_root)
            # Clear the stale host asset dir (a real, contained dir per the checks
            # above); `os.replace` of a dir onto a non-empty dir is not allowed.
            if out_dir.exists():
                shutil.rmtree(out_dir)
            os.replace(staged_out, out_dir)  # atomic same-FS rename
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)


def build_backend_for_runtime(runtime: ConversationRuntime | None) -> SandboxBuildBackend | None:
    """Construct the production sandboxed build backend from the runtime, or None
    when no runtime is available (→ the executor refuses a real deploy). The weak
    shared-kernel ``local`` backend is deploy-grade ONLY when the explicit
    ``DISCO_APPKIT_DEPLOY_ALLOW_LOCAL`` override is set — never the silent default."""
    if runtime is None:
        return None
    allow_local = (disco_env(_ALLOW_LOCAL_ENV_SUFFIX, "") or "").strip().lower() in _TRUTHY
    return SandboxBuildBackend(runtime, allow_local_isolation=allow_local)


__all__ = ["SandboxBuildBackend", "build_backend_for_runtime"]
