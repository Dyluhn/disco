"""`verify_web_app` — the build agent's self-test tool (W-45 v1).

The build agent used to "verify" a web build by opening the page in the headless
browser and eyeballing the console — but a raw page observation is EVIDENCE, not
a VERDICT, so the agent re-loaded the same page 25-40 times without ever
concluding. This tool closes that gap: given the running preview it runs the
DETERMINISTIC user-centered checks (server reachable + HTTP 2xx/3xx + no console
errors + no critical network failures + meaningful render) and returns a single
structured PASS/FAIL/DEGRADED verdict the finish gate can consume directly.

Reuse (no reinvention):
  * `BrowserTool` (browser.py) — drives the headless navigate + console/network/
    screenshot capture through the same sandbox browser daemon the agent uses.
  * `port_owners` / preview-port detection (sandbox/port_owner.py, server.py) —
    finds the live preview without the agent having to name a URL.
  * `_browser_content_meaningful` (core/loop/finish.py) — the SAME blank-render
    guard the finish gate already trusts, so "served 200 but mounted nothing"
    reads identically here.

The `failure_fingerprint` (a stable hash of the top console-error signatures +
critical network failures) is THE loop-breaker key: the finish gate caches a
verdict by it, so re-verifying WITHOUT a productive edit returns the same
fingerprint and the gate can mark the run STUCK instead of reloading forever.
"""

from __future__ import annotations

import hashlib
import shlex
from datetime import UTC, datetime
from typing import Any, Literal

from disco.core import SecurityRisk
from disco.core.effects import EffectCapability
from disco.core.loop.finish import _PREVIEW_PORTS
from disco.core.trusted_components.lockfile import (
    LOCKFILE_RELPATH,
    LockfileCorrupt,
    dump_lock,
    parse_lock,
)
from disco.core.trusted_components.registry import TrustedComponentRegistry
from disco.core.trusted_components.verify import (
    PROBE_TIMEOUT_S,
    ProbeVerdict,
    install_path,
    parse_probe_stdout,
    verify_trusted_components,
)
from disco.tools.verify.web_app_probe import (
    _failure_fingerprint as _compute_failure_fingerprint,
)
from disco.tools.verify.web_app_probe import (
    compute_verdict,
)
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..behavior import declares
from ._outcomes import fail_outcome
from .browser import BROWSER_UNAVAILABLE_MSG, BrowserArgs, BrowserTool

# `_PREVIEW_PORTS` (imported above): SINGLE SOURCE OF TRUTH lives in core's finish
# gate so the gate's preview detection (P1-1) and this tool's auto-detect stay
# byte-identical (8000 is Disco's canonical user-visible port; NOVNC_PORT is
# deliberately excluded — it's the live-view bridge, never the app under test).
_VERIFY_WEB_APP_FAILURE_RECIPE = (
    "Check preview_status (is a preview running?) and preview_logs, then re-run "
    "verify_web_app once."
)


def _failure_fingerprint(
    console_errors: list[dict[str, str]], network_failures: list[dict[str, Any]]
) -> str:
    return _compute_failure_fingerprint(console_errors, network_failures)


def _render(verdict: dict[str, Any]) -> str:
    """Stable, agent-facing text. DELIBERATELY excludes the screenshot path and any
    per-call sequence number so an identical failure renders byte-identically — the
    semantic no-progress breaker keys on (success, content)."""
    state = "pass" if verdict["passed"] else "not passing"
    lines = [
        f"VERIFY_WEB_APP: {verdict['verdict'].upper()} ({state})",
        f"url: {verdict['url']}  http_status: {verdict['http_status']}",
        f"fingerprint: {verdict['failure_fingerprint']}",
        f"summary: {verdict['summary']}",
    ]
    if verdict["console_errors"]:
        lines.append(f"console_errors ({len(verdict['console_errors'])}):")
        for e in verdict["console_errors"][:5]:
            where = f"  @ {e['source']}" if e["source"] else ""
            lines.append(f"  - {e['text']}{where}")
    if verdict["network_failures"]:
        lines.append(f"network_failures ({len(verdict['network_failures'])}):")
        for n in verdict["network_failures"][:5]:
            marker = n.get("status") or n.get("failure") or "failed"
            lines.append(f"  - {n['method']} {n['url']} -> {marker}")
    if verdict["next_action"]:
        lines.append(f"next_action: {verdict['next_action']}")
    return "\n".join(lines)


class VerifyWebAppArgs(BaseModel):
    url: str = Field(
        default="",
        description=(
            "Preview URL to verify (e.g. http://127.0.0.1:8000/). Leave empty to "
            "auto-detect the running preview server."
        ),
    )
    medium: Literal["web", "deck", "mobile", "game"] = Field(
        default="web",
        description=(
            "Artifact medium hint from the finish gate. Defaults to web; mobile uses "
            "a 390x844 viewport; game performs click/keyboard interaction evidence."
        ),
    )


class VerifyWebAppTool:
    """[CONTRACT boundary] Self-test the running web deliverable and return a
    structured PASS/FAIL verdict. Runs the browser through the sandbox (network +
    display granted, like the browser tool)."""

    definition = ToolDef(
        name="verify_web_app",
        description=(
            "Self-test the running web app and return a STRUCTURED pass/fail verdict "
            "(not a raw page dump). Checks: server reachable, HTTP 2xx/3xx, no console "
            "errors, no critical network failures, and a meaningful (non-blank) render. "
            "Returns verdict (pass/fail/degraded), the failing console/network details, "
            "a screenshot path, a stable failure_fingerprint, and the exact next_action "
            "to fix. Call this ONCE after a change to decide if the build is done — do "
            "not reload the page repeatedly. Auto-detects the preview port if no url given."
        ),
        args_model=VerifyWebAppArgs,
        needs=frozenset({Capability.NETWORK, Capability.DISPLAY, Capability.SHELL}),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        behavior=declares(EffectCapability.ARTIFACT_VERIFY, planner_safe=False),
    )

    async def run(self, args: VerifyWebAppArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        try:
            explicit = (args.url or "").strip()
            if explicit:
                # An agent-supplied url must pass the SAME backend-aware rule as
                # auto-detect (Bug 7 explicit-url bypass): on a shared host it is only
                # honored if it is the conversation's OWN served port — never a
                # reserved control/UI port (8000/8800/5173) or a sibling's. Otherwise
                # it is NOT this build's preview → not-serving/undetectable (→ honest
                # path), never a false PASS against the wrong app.
                if not await self._explicit_url_allowed(explicit, ctx):
                    return self._rejected_explicit_outcome(explicit)
                url = explicit
            else:
                url = await self._detect_preview_url(ctx)
            reachable, http_status = await self._probe_http(ctx, url)

            structured: dict[str, Any] | None = None
            if reachable:
                # Server is up — drive the headless browser for the render/console/
                # network evidence (REUSE the browser tool + its daemon capture).
                browser_args = (
                    BrowserArgs(
                        action="navigate",
                        url=url,
                        viewport_width=390,
                        viewport_height=844,
                    )
                    if args.medium == "mobile"
                    else BrowserArgs(action="navigate", url=url)
                )
                browser_outcome = await BrowserTool().run(browser_args, ctx)
                if browser_outcome.success and browser_outcome.structured:
                    structured = browser_outcome.structured
                    if args.medium == "game":
                        structured = await self._capture_game_interaction(ctx, structured)
                elif (browser_outcome.structured or {}).get("browser_unavailable"):
                    # ROOT-3 — no browser daemon on this backend. The server IS
                    # reachable (HTTP probe passed); we simply cannot run the
                    # render/console checks here. Return a TERMINAL verdict the agent
                    # treats as done-with-verification, NOT a misleading blank-render
                    # DEGRADED that it would try to "fix" forever.
                    unverifiable = {
                        "verdict": "unverifiable",
                        "passed": False,
                        "url": url,
                        "http_status": http_status,
                        "browser_unavailable": True,
                        "failure_fingerprint": "browser_unavailable",
                        "summary": (
                            f"server reachable at {url} (HTTP {http_status}); "
                            f"{BROWSER_UNAVAILABLE_MSG}"
                        ),
                        "next_action": "",
                    }
                    startup_diagnostic = (browser_outcome.structured or {}).get(
                        "startup_diagnostic"
                    )
                    if startup_diagnostic:
                        unverifiable["startup_diagnostic"] = startup_diagnostic
                    # WO-TC3: the render checks can't run here, but the component
                    # checks CAN (integrity/deps need only bytes; probes are
                    # HTTP-level and the server IS reachable) — never skip them.
                    unverifiable = await self._fold_trusted_components(
                        ctx, unverifiable, probes_allowed=True
                    )
                    return ToolOutcome(
                        success=True,
                        content=(
                            "VERIFY_WEB_APP: " + ("UNVERIFIABLE (server reachable)") + "\n"
                            f"url: {url}  http_status: {http_status}\n"
                            f"summary: {unverifiable['summary']}"
                            + (
                                f"\nnext_action: {unverifiable['next_action']}"
                                if unverifiable["next_action"]
                                else ""
                            )
                        ),
                        structured=unverifiable,
                    )
                else:
                    # The HTTP server answered, but the render infrastructure did
                    # not produce browser evidence.  ``structured=None`` is not an
                    # empty DOM: feeding it to compute_verdict fabricated a
                    # DEGRADED "blank page" finding and sent the model chasing its
                    # healthy app.  Preserve the infrastructure failure as a failed
                    # tool outcome, with an explicit machine-readable distinction.
                    detail = (
                        browser_outcome.error
                        or browser_outcome.content
                        or "browser returned no diagnostic"
                    )
                    return fail_outcome(
                        (
                            f"verify_web_app render probe failed after HTTP {http_status}: "
                            f"{detail}\n{_VERIFY_WEB_APP_FAILURE_RECIPE}"
                        ),
                        structured={
                            "verdict": "unverifiable",
                            "passed": False,
                            "url": url,
                            "http_status": http_status,
                            "render_probe_failed": True,
                            "browser_error": detail,
                        },
                    )

            meaningful = self._meaningful(structured)
            if args.medium == "game":
                meaningful = meaningful or bool((structured or {}).get("canvas_count"))
            verdict = compute_verdict(
                url=url,
                reachable=reachable,
                http_status=http_status,
                structured=structured,
                meaningful=meaningful,
            )
            if args.medium == "game" and structured is not None:
                verdict["game_interaction"] = structured.get("game_interaction") or {}
            if args.medium != "web":
                verdict["medium"] = args.medium
            # WO-TC3: fold the trusted-component checks into THIS verdict — the
            # finish gate consumes it, so components are enforced at every finish
            # with zero gate changes. Probes only run against a reachable server.
            verdict = await self._fold_trusted_components(ctx, verdict, probes_allowed=reachable)
            return ToolOutcome(
                success=True,  # the verdict ran; pass/fail lives in structured
                content=_render(verdict),
                structured=verdict,
            )
        except Exception as e:  # noqa: BLE001 — never crash the loop; report a verdict-shaped error
            return fail_outcome(f"verify_web_app error: {e}\n{_VERIFY_WEB_APP_FAILURE_RECIPE}")

    async def _fold_trusted_components(
        self, ctx: ToolContext, verdict: dict[str, Any], *, probes_allowed: bool
    ) -> dict[str, Any]:
        """WO-TC3 — append component_integrity/deps/probe checks to the W-45
        verdict. No lockfile → verdict unchanged (zero cost for ordinary builds).
        Blocking component failures flip the verdict to FAIL with a teaching
        next_action; ejects are honest relabels (persisted, never blocking).
        Failures extend failure_fingerprint so the gate's STUCK detection keys
        on component state."""
        sandbox = ctx.sandbox
        assert sandbox is not None
        try:
            has_lock = await sandbox.file_exists(LOCKFILE_RELPATH)
        except Exception:  # noqa: BLE001 — backend can't answer existence: no
            # component could have been installed through the same protocol
            # either, so there is nothing to verify. Honest no-op.
            return verdict
        if not has_lock:
            return verdict
        checks_out: list[dict[str, str]] = []
        try:
            lock = parse_lock(await sandbox.read_file(LOCKFILE_RELPATH))
        except LockfileCorrupt as exc:
            # A corrupt lockfile can make NO claims — and a build carrying one
            # must not finish quietly, so it is a blocking failure with the fix
            # named (fail-closed, teach the exit).
            checks_out.append(
                {
                    "name": "component_lockfile",
                    "status": "fail",
                    "evidence": (
                        f"{exc} — fix or delete {LOCKFILE_RELPATH} (deleting drops "
                        "all verified-component claims), then re-verify."
                    ),
                }
            )
            return self._merge_component_checks(verdict, checks_out, blocking=True)

        if not lock.components:
            return verdict

        try:
            return await self._fold_checks(
                ctx, verdict, lock, checks_out, probes_allowed=probes_allowed
            )
        except Exception as exc:  # noqa: BLE001 — a broken component verifier must
            # never silently PASS a build that carries a lockfile (fail-closed).
            checks_out.append(
                {
                    "name": "component_verify_error",
                    "status": "fail",
                    "evidence": f"component verification crashed: {exc}",
                }
            )
            return self._merge_component_checks(verdict, checks_out, blocking=True)

    async def _fold_checks(
        self,
        ctx: ToolContext,
        verdict: dict[str, Any],
        lock: Any,
        checks_out: list[dict[str, str]],
        *,
        probes_allowed: bool,
    ) -> dict[str, Any]:
        sandbox = ctx.sandbox
        assert sandbox is not None
        registry = TrustedComponentRegistry.default()

        file_bytes: dict[str, bytes | None] = {}
        for name, entry in lock.components.items():
            if entry.ejected:
                continue
            comp = registry.get(name, entry.version)
            if comp is None:
                continue  # verify() records the registry-version-missing eject
            for relpath in comp.manifest.files:
                target = install_path(name, relpath)
                file_bytes[target] = (
                    await sandbox.read_file(target) if await sandbox.file_exists(target) else None
                )

        base_url = str(verdict.get("url") or "")
        run_probe = (
            self._make_probe_runner(ctx, base_url, lock, registry)
            if probes_allowed and base_url
            else None
        )
        result = await verify_trusted_components(
            lock,
            registry,
            file_bytes,
            run_probe=run_probe,
            now_iso=datetime.now(UTC).isoformat(),
        )

        if result.newly_ejected and result.lock is not None:
            from .files import _atomic_write

            await _atomic_write(sandbox, LOCKFILE_RELPATH, dump_lock(result.lock))
        # Re-assert the eject banner for EVERY ejected component (idempotent), not
        # only the newly-ejected ones. A banner removed by a revert-from-source or
        # a failed reinstall would otherwise never come back — leaving an ejected
        # component's GUIDE looking trusted, a false affordance. _append_eject_banner
        # is a no-op when the banner is already present.
        banner_lock = result.lock if result.lock is not None else lock
        if banner_lock is not None:
            for nm, entry in banner_lock.components.items():
                if entry.ejected:
                    await self._append_eject_banner(ctx, banner_lock, nm)

        checks_out.extend(
            {"name": c.name, "status": c.status, "evidence": c.evidence} for c in result.checks
        )
        # Skipped probes BLOCK on the finish-path verdict (fail-closed) whenever
        # probes were supposed to be runnable; on a probes-not-allowed call the
        # skip is honest and non-blocking (the render verdict already failed).
        blocking = bool(result.failing) or (probes_allowed and bool(result.skipped_probes))
        if checks_out:
            material = (verdict.get("failure_fingerprint") or "") + result.fingerprint_material()
            verdict["failure_fingerprint"] = hashlib.sha256(material.encode("utf-8")).hexdigest()[
                :16
            ]
        return self._merge_component_checks(verdict, checks_out, blocking=blocking)

    @staticmethod
    def _merge_component_checks(
        verdict: dict[str, Any], checks: list[dict[str, str]], *, blocking: bool
    ) -> dict[str, Any]:
        if not checks:
            return verdict
        verdict["component_checks"] = checks
        ejected = [c for c in checks if c["status"] == "ejected"]
        bad = [c for c in checks if c["status"] in ("fail", "skipped")]
        if blocking and bad:
            first = next((c for c in checks if c["status"] == "fail"), bad[0])
            verdict["passed"] = False
            verdict["verdict"] = "fail"
            verdict["summary"] = f"trusted components: {first['evidence']}"
            verdict["next_action"] = first["evidence"]
        elif ejected:
            note = "; ".join(c["evidence"] for c in ejected[:2])
            verdict["summary"] = f"{verdict.get('summary', '')} [components: {note}]".strip()
        return verdict

    def _make_probe_runner(
        self,
        ctx: ToolContext,
        base_url: str,
        lock: Any,
        registry: TrustedComponentRegistry,
    ):
        """Probes execute INSIDE the sandbox (the preview URL is sandbox-local),
        re-materialized fresh from the HOST registry on every run — a workspace
        copy is never trusted, so 'fixing the check' is impossible (D3). Probes
        are python3-stdlib-only by authoring rule."""

        async def run(name: str) -> ProbeVerdict | None:
            sandbox = ctx.sandbox
            assert sandbox is not None
            entry = lock.components[name]
            comp = registry.get(name, entry.version)
            if comp is None:
                return None
            src = comp.probe_source()
            if src is None:
                return None
            probe_path = f".disco/tc-probe/{name}/probe.py"
            await sandbox.write_file(probe_path, src)
            cmd = (
                f"python3 {shlex.quote(probe_path)} "
                f"--base-url {shlex.quote(base_url)} --workspace ."
            )
            res = await sandbox.exec_shell(cmd, timeout_s=PROBE_TIMEOUT_S)
            if res.exit_code != 0:
                tail = (res.stderr or res.stdout or "")[-300:]
                raise RuntimeError(f"probe exited {res.exit_code}: {tail}")
            return parse_probe_stdout(res.stdout)

        return run

    async def _append_eject_banner(self, ctx: ToolContext, lock: Any, name: str) -> None:
        sandbox = ctx.sandbox
        assert sandbox is not None
        record = next((e for e in reversed(lock.ejects) if e.name == name), None)
        guide_path = install_path(name, "GUIDE.md")
        if record is None or not await sandbox.file_exists(guide_path):
            return
        guide = await sandbox.read_file(guide_path)
        if b"**Ejected" in guide:
            return  # idempotent — the banner is already present
        banner = (
            f"\n\n---\n\n> ⚠ **Ejected {record.at}** ({record.reason}) — this copy "
            f"diverged from the registry and is now custom code you own. Upgrades and "
            f"the verified badge no longer apply."
            + (f" Diverged: {', '.join(record.diverged_files)}." if record.diverged_files else "")
            + "\n"
        ).encode("utf-8")
        await sandbox.write_file(guide_path, guide + banner)

    async def _capture_game_interaction(
        self, ctx: ToolContext, before: dict[str, Any]
    ) -> dict[str, Any]:
        interaction: dict[str, Any] = {
            "before_screenshot_path": str(before.get("screenshot_path") or ""),
            "steps": [],
        }
        latest = before
        probes = [
            BrowserArgs(action="click", selector="canvas"),
            BrowserArgs(action="press", key="Space"),
            BrowserArgs(action="press", key="ArrowRight"),
            BrowserArgs(action="screenshot"),
        ]
        for probe in probes:
            outcome = await BrowserTool().run(probe, ctx)
            step: dict[str, Any] = {"action": probe.action, "success": outcome.success}
            if probe.action == "press":
                step["key"] = probe.key
            if outcome.success and outcome.structured:
                latest = outcome.structured
                step["screenshot_path"] = str(latest.get("screenshot_path") or "")
            elif outcome.error:
                step["error"] = outcome.error[:300]
            interaction["steps"].append(step)
        interaction["after_screenshot_path"] = str(latest.get("screenshot_path") or "")
        return {**latest, "game_interaction": interaction}

    @staticmethod
    def _meaningful(structured: dict[str, Any] | None) -> bool:
        if not structured:
            return False
        # REUSE the finish gate's blank-render guard so "served 200 but mounted
        # nothing" is judged identically on both sides of the contract.
        from disco.core.loop.finish import _browser_content_meaningful

        return _browser_content_meaningful(structured)

    async def _explicit_url_allowed(self, url: str, ctx: ToolContext) -> bool:
        """Validate an agent-supplied explicit verify url against the backend-aware
        rule. ISOLATED backend → honored as-is (today's behavior). SHARED host →
        honored ONLY if the url's port is conversation-owned + non-reserved (probed
        live, including the explicit port even if outside the canonical preview set)."""
        assert ctx.sandbox is not None
        from disco.core.loop.preview_target import backend_shares_host_network

        host_shared = backend_shares_host_network(ctx.sandbox)
        if not host_shared:
            return True
        from disco.core.loop.preview_target import (
            PortOwnership,
            explicit_target_allowed,
            target_url_port,
        )

        from ..sandbox.port_owner import port_owners

        port = target_url_port(url)
        if port is None:
            return False  # no explicit port → cannot confirm it is a served preview
        ports = sorted(set(_PREVIEW_PORTS) | {port})
        try:
            owners = await port_owners(ctx.sandbox, ports)
        except Exception:  # noqa: BLE001 — probe failure → cannot confirm ownership → reject
            owners = {}
        owned = {
            p: PortOwnership(pid=o.pid, session=o.session)
            for p, o in owners.items()
            if o is not None
        }
        return explicit_target_allowed(
            port=port,
            host_shared=True,
            owned=owned,
            conversation_id=str(getattr(ctx.sandbox, "conversation_id", "") or ""),
        )

    def _rejected_explicit_outcome(self, url: str) -> ToolOutcome:
        """A not-serving verdict for an explicit url that is NOT this build's preview
        on the shared host (a reserved control/UI port, or a foreign/unowned port).
        Verdict-shaped exactly like a real not-serving result so the finish gate routes
        it to the honest-unverifiable path — never a false PASS — with a CLEAR reason."""
        from disco.core.loop.preview_target import reserved_control_ports, target_url_port

        port = target_url_port(url)
        if port is not None and port in reserved_control_ports():
            reason = (
                f"{url} is a RESERVED control/UI port ({port}: the agent-server, "
                "app-server, or frontend) — not this build's preview, so it was NOT "
                "verified."
            )
        else:
            reason = (
                f"{url} is not a port this build serves on (not owned by this "
                "conversation) — it was NOT verified as your app."
            )
        verdict = compute_verdict(
            url=url, reachable=False, http_status=0, structured=None, meaningful=False
        )
        verdict["summary"] = reason
        verdict["next_action"] = (
            "Serve your build on its own port and verify that, or leave url empty to "
            "auto-detect your served preview."
        )
        return ToolOutcome(success=True, content=_render(verdict), structured=verdict)

    async def _detect_preview_url(self, ctx: ToolContext) -> str:
        """Find the live preview with the SAME backend-aware resolver the finish gate
        uses (`preview_target.resolve_preview_port`).

        On a SHARED-host backend (process/local — `sandbox.workspace_path` is set)
        the agent-server's `:8000`, the app-server's `:8800`, and the UI's Vite
        `:5173` are NOT the build's app (Bug 7): the resolver returns ONLY a
        CONVERSATION-OWNED non-reserved port, else None. Here None ⇒ UNDETECTABLE — we
        return "" and do NOT blind-guess a port: probing an arbitrary port (5173 = the
        Vite UI, or a sibling conversation's server) would be a FALSE PASS against the
        wrong app. An empty url makes `_probe_http` report "not serving", which routes
        the gate to the honest-unverifiable path. On an ISOLATED backend (gVisor/
        Podman) `:8000` IS the app, so the legacy "first owned, else 8000" holds."""
        assert ctx.sandbox is not None
        from disco.core.loop.preview_target import (
            PortOwnership,
            active_managed_preview_ports,
            resolve_preview_port,
        )

        from ..sandbox.port_owner import port_owners

        preview_ports = tuple(
            dict.fromkeys((*_PREVIEW_PORTS, *active_managed_preview_ports(ctx.sandbox)))
        )
        try:
            owners = await port_owners(ctx.sandbox, list(preview_ports))
        except Exception:  # noqa: BLE001 — detection failure → resolver default
            owners = {}

        from disco.core.loop.preview_target import backend_shares_host_network

        host_shared = backend_shares_host_network(ctx.sandbox)
        owned = {
            p: PortOwnership(pid=o.pid, session=o.session)
            for p, o in owners.items()
            if o is not None
        }
        chosen = resolve_preview_port(
            host_shared=host_shared,
            owned=owned,
            conversation_id=str(getattr(ctx.sandbox, "conversation_id", "") or ""),
            preview_ports=preview_ports,
        )
        if chosen is None:
            # Shared-host backend with no conversation-owned preview → UNDETECTABLE.
            # Return "" rather than guess a port: a guess could verify the UI / a
            # sibling app (false pass). "" → probe reports not-serving → honest path.
            return ""
        return f"http://127.0.0.1:{chosen}/"

    async def _probe_http(self, ctx: ToolContext, url: str) -> tuple[bool, int]:
        """GET the URL inside the sandbox; return (reachable, http_status). A
        connection failure → (False, 0). Server-free + portable (urllib)."""
        assert ctx.sandbox is not None
        script = (
            "import urllib.request as U\n"
            "try:\n"
            f"    r=U.urlopen({url!r},timeout=10)\n"
            "    print(getattr(r,'status',None) or r.getcode())\n"
            "except Exception:\n"
            "    print(0)\n"
        )
        try:
            res = await ctx.sandbox.exec_shell(f"python3 -c {shlex.quote(script)}", timeout_s=15)
        except Exception:  # noqa: BLE001 — sandbox/transport error → unreachable
            return False, 0
        out = (res.stdout or "").strip().splitlines()
        code = 0
        for line in reversed(out):
            try:
                code = int(line.strip())
                break
            except ValueError:
                continue
        return (code > 0, code)


__all__ = [
    "VerifyWebAppArgs",
    "VerifyWebAppTool",
    "compute_verdict",
]
