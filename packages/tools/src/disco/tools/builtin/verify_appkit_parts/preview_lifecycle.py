"""`PreviewLifecycle` — the managed-Vite-preview collaborator for
`verify_appkit_app`.

Starting, building, fetching and stopping a Vite dev/built preview is a
different authority from verifying an app's structure: this class owns that
lifecycle end to end and is HELD by `VerifyAppKitAppTool`
(`self._preview = PreviewLifecycle(self._sandbox)`), never inherited from.
Extracted from `VerifyAppKitAppTool` verbatim (logic unchanged) — including
the pure `_is_vite_app_tree` / served-preview classifiers, which must stay
importable from `verify_appkit_app` for the existing test surface.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .constants import (
    _BUILT_PREVIEW_NAME,
    _PACKAGE_RELPATH,
    _VITE_BUILD_TIMEOUT_S,
    _VITE_CONFIG_RELPATH,
    _VITE_PREVIEW_COMMAND,
    APPKIT_LIVE_PREVIEW_NAME,
    APPKIT_VITE_PACKAGE_SHA_RELPATH,
)

if TYPE_CHECKING:
    from ...anatomy import ToolContext
    from .sandbox_reader import SandboxReader


@dataclass(frozen=True)
class _PreviewProbe:
    status: int
    content_type: str
    body: str
    error: str = ""


@dataclass(frozen=True)
class _BuildResult:
    ok: bool
    evidence: str = ""


@dataclass(frozen=True)
class _PreparedPreview:
    url: str
    stop_name: str | None = None
    failure_evidence: str | None = None
    runtime: dict[str, Any] | None = None


def _is_vite_app_tree(package_json: str | None, has_vite_config: bool) -> bool:
    """True for the generated React/Vite app shape the browser verifier must build.

    The trigger is intentionally narrow: package.json must be valid JSON with a
    ``devDependencies.vite`` entry and the workspace must carry ``vite.config.ts``.
    """
    if not package_json or not has_vite_config:
        return False
    try:
        pkg = json.loads(package_json)
    except json.JSONDecodeError:
        return False
    if not isinstance(pkg, dict):
        return False
    dev_deps = pkg.get("devDependencies")
    return isinstance(dev_deps, dict) and "vite" in dev_deps


_UNBUILT_VITE_ENTRY_RE = re.compile(
    r"""<script\b(?=[^>]*\btype=["']module["'])(?=[^>]*\bsrc=["']/src/[^"']+)""",
    re.IGNORECASE,
)
_BUILT_VITE_BUNDLE_RE = re.compile(
    r"""<script\b(?=[^>]*\btype=["']module["'])(?=[^>]*\bsrc=["'][^"']*/assets/[^"']+\.js["'])""",
    re.IGNORECASE,
)


def _served_preview_uses_built_bundle(index_html: str) -> bool | None:
    """Classify a served Vite index page.

    ``False`` means the preview is definitely the source tree (for example,
    ``/src/main.tsx``). ``True`` means it is definitely built Vite output
    (hashed ``/assets/*.js`` bundle). ``None`` means the body is not recognizable
    enough to force a platform rebuild.
    """
    if _UNBUILT_VITE_ENTRY_RE.search(index_html) or "/src/main.tsx" in index_html:
        return False
    if _BUILT_VITE_BUNDLE_RE.search(index_html):
        return True
    return None


def _served_preview_is_vite_dev(index_html: str) -> bool:
    """Recognize Vite's real development transform, not a raw source server."""

    return "/@vite/client" in index_html and _served_preview_uses_built_bundle(index_html) is False


def _tail(text: str, *, limit: int = 2000) -> str:
    stripped = (text or "").strip()
    if len(stripped) <= limit:
        return stripped
    return "..." + stripped[-limit:]


def _exec_failure_evidence(step: str, res: Any) -> str:
    reason = (
        "timed out"
        if bool(getattr(res, "timed_out", False))
        else (f"exit {getattr(res, 'exit_code', 'unknown')}")
    )
    stderr = _tail(str(getattr(res, "stderr", "") or ""))
    stdout = _tail(str(getattr(res, "stdout", "") or ""))
    tail = stderr or stdout or "(no stderr/stdout captured)"
    return f"platform Vite build failed during `{step}` ({reason}); stderr tail: {tail}"


class PreviewLifecycle:
    """Owns the managed Vite dev/built preview: probing/building/starting it for
    the browser checks, and stopping any preview it started."""

    def __init__(self, sandbox: SandboxReader) -> None:
        self._sandbox = sandbox

    async def prepare_for_browser_checks(
        self, ctx: ToolContext, requested_url: str
    ) -> _PreparedPreview:
        """Use the real managed Vite runtime, or build a managed runtime if absent.

        A Vite development response is accepted only when it carries Vite's
        ``/@vite/client`` transform; a raw static server exposing ``/src/*.tsx`` is
        still rejected. A missing, failing, raw-source, or opaque probe triggers a
        platform build and managed compiled preview before browser checks.
        """
        assert ctx.sandbox is not None
        package_json = await self._sandbox.read_text(ctx, _PACKAGE_RELPATH)
        if not _is_vite_app_tree(package_json, await ctx.sandbox.file_exists(_VITE_CONFIG_RELPATH)):
            return _PreparedPreview(url=(requested_url or "").strip())

        managed = self._canonical_managed_vite_preview(ctx)
        build = await self.ensure_build(ctx, package_json or "")
        if not build.ok:
            return _PreparedPreview(
                url=managed.url if managed is not None else "",
                failure_evidence=build.evidence,
                runtime=managed.runtime if managed is not None else None,
            )
        if managed is not None:
            probe = await self._fetch_preview_index(ctx, managed.url)
            if (
                probe is None
                or probe.error
                or not (
                    _served_preview_is_vite_dev(probe.body)
                    or _served_preview_uses_built_bundle(probe.body) is True
                )
            ):
                evidence = (
                    probe.error
                    if probe is not None and probe.error
                    else "canonical managed Vite runtime did not serve a recognizable Vite page"
                )
                return _PreparedPreview(
                    url=managed.url,
                    failure_evidence=evidence,
                    runtime=managed.runtime,
                )
            return managed
        return await self._start_built_preview(ctx)

    @staticmethod
    def _canonical_managed_vite_preview(ctx: ToolContext) -> _PreparedPreview | None:
        """Return the one host-owned AppKit manager selection, ignoring caller URLs."""

        manager = getattr(ctx.sandbox, "_preview_manager", None)
        session = manager.canonical_session() if manager is not None else None
        if session is None:
            return None
        if getattr(session, "name", None) not in {
            APPKIT_LIVE_PREVIEW_NAME,
            _BUILT_PREVIEW_NAME,
        }:
            return None
        port = getattr(session, "port", None)
        if type(port) is not int:
            return None
        data = session.to_dict()
        if not isinstance(data, dict) or data.get("status") not in {"running", "unavailable"}:
            return None
        return _PreparedPreview(url=f"http://127.0.0.1:{port}/", runtime=data)

    async def _fetch_preview_index(self, ctx: ToolContext, base_url: str) -> _PreviewProbe | None:
        """Fetch the current preview's root from inside the sandbox.

        Returns ``None`` only when the sandbox fake/output is not the JSON frame this
        helper emits; real network errors are framed as ``error`` so they remain loud
        enough to trigger a platform build.
        """
        assert ctx.sandbox is not None
        url = base_url.rstrip("/") + "/"
        script = (
            "import json, urllib.request as U\n"
            "try:\n"
            f"    r=U.urlopen({url!r},timeout=10)\n"
            "    body=r.read(200000).decode('utf-8','replace')\n"
            "    print(json.dumps({'status': getattr(r, 'status', None) or r.getcode(), "
            "'content_type': r.headers.get('content-type',''), 'body': body}))\n"
            "except Exception as e:\n"
            "    print(json.dumps({'status': 0, 'content_type': '', 'body': '', "
            "'error': str(e)}))\n"
        )
        try:
            res = await ctx.sandbox.exec_shell(f"python3 -c {shlex.quote(script)}", timeout_s=15)
        except Exception:  # noqa: BLE001 — let browser checks handle opaque fakes
            return None
        try:
            data = json.loads(str(getattr(res, "stdout", "") or ""))
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        return _PreviewProbe(
            status=int(data.get("status") or 0),
            content_type=str(data.get("content_type") or ""),
            body=str(data.get("body") or ""),
            error=str(data.get("error") or ""),
        )

    async def ensure_build(self, ctx: ToolContext, package_json: str) -> _BuildResult:
        """Run the bounded platform-owned Vite build inside the sandbox.

        ``npm ci`` is skipped only when a node_modules directory exists and the cache
        marker contains the exact current package.json SHA. ``npm run build`` always
        runs because the source tree may have changed while dependencies did not.
        """
        assert ctx.sandbox is not None
        package_sha = hashlib.sha256(package_json.encode("utf-8")).hexdigest()
        node_modules_exists = await self._sandbox.dir_exists(ctx, "node_modules")
        marker = (await self._sandbox.read_text(ctx, APPKIT_VITE_PACKAGE_SHA_RELPATH) or "").strip()
        need_ci = not (node_modules_exists and marker == package_sha)
        started = time.monotonic()

        if need_ci:
            # The generator emits its reviewed package-lock.json with package.json.
            # Never fall back to `npm install`: that would resolve mutable dependency
            # ranges during verification and make the trusted bundle non-deterministic.
            has_lock = await self._sandbox.file_exists(ctx, "package-lock.json")
            if not has_lock:
                return _BuildResult(
                    False,
                    (
                        "generated Vite tree is missing package-lock.json; "
                        "refusing mutable npm install"
                    ),
                )
            install_cmd = "npm ci --no-audit --no-fund"
            try:
                ci = await ctx.sandbox.exec_shell(install_cmd, timeout_s=_VITE_BUILD_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001 — loud verdict evidence
                return _BuildResult(
                    False, f"platform Vite build could not run `{install_cmd}`: {exc}"
                )
            if getattr(ci, "exit_code", 1) != 0 or bool(getattr(ci, "timed_out", False)):
                return _BuildResult(False, _exec_failure_evidence(install_cmd, ci))
            try:
                await ctx.sandbox.write_file(
                    APPKIT_VITE_PACKAGE_SHA_RELPATH, (package_sha + "\n").encode("utf-8")
                )
            except Exception:  # noqa: BLE001 — cache marker failure must not hide build evidence
                pass

        elapsed = int(time.monotonic() - started)
        remaining = max(1, _VITE_BUILD_TIMEOUT_S - elapsed)
        try:
            build = await ctx.sandbox.exec_shell("npm run build", timeout_s=remaining)
        except Exception as exc:  # noqa: BLE001 — loud verdict evidence
            return _BuildResult(False, f"platform Vite build could not run `npm run build`: {exc}")
        if getattr(build, "exit_code", 1) != 0 or bool(getattr(build, "timed_out", False)):
            return _BuildResult(False, _exec_failure_evidence("npm run build", build))
        if not await self._sandbox.file_exists(ctx, "dist/index.html"):
            return _BuildResult(
                False,
                "platform Vite build reported success but did not produce dist/index.html",
            )
        return _BuildResult(True)

    async def _start_built_preview(self, ctx: ToolContext) -> _PreparedPreview:
        """Serve the compiled Vite app with Vite's preview server under platform port
        ownership, then return the in-sandbox URL the browser verifier can reach."""
        try:
            from ..preview import _manager

            mgr = _manager(ctx)
            session = await mgr.start(
                command=_VITE_PREVIEW_COMMAND,
                name=_BUILT_PREVIEW_NAME,
                supervise=True,
            )
        except Exception as exc:  # noqa: BLE001 — route/section fail loudly
            return _PreparedPreview(
                url="",
                failure_evidence=(
                    "platform Vite build succeeded, but serving the built app failed "
                    f"while starting `{_VITE_PREVIEW_COMMAND}`: {exc}"
                ),
            )

        raw_status = getattr(session, "status", "")
        status = getattr(raw_status, "value", str(raw_status))
        port = getattr(session, "port", None)
        if status not in {"running", "unavailable"} or not isinstance(port, int):
            detail = str(getattr(session, "detail", "") or "preview did not become healthy")
            return _PreparedPreview(
                url="",
                failure_evidence=(
                    "platform Vite build succeeded, but the built-app preview failed "
                    f"to become healthy (status={status or 'unknown'}): {detail}"
                ),
            )
        return _PreparedPreview(
            url=f"http://127.0.0.1:{port}/",
            stop_name=_BUILT_PREVIEW_NAME,
            runtime=(session.to_dict() if callable(getattr(session, "to_dict", None)) else None),
        )

    async def stop(self, ctx: ToolContext, name: str) -> None:
        try:
            from ..preview import _manager

            await _manager(ctx).stop(name)
        except Exception:  # noqa: BLE001 — cleanup must not mask the verifier result
            pass
