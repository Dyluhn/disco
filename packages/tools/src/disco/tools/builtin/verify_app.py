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
import re
import shlex
from typing import Any

from disco.core import SecurityRisk
from disco.core.loop.finish import _PREVIEW_PORTS
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from .browser import BROWSER_UNAVAILABLE_MSG, BrowserArgs, BrowserTool

# `_PREVIEW_PORTS` (imported above): SINGLE SOURCE OF TRUTH lives in core's finish
# gate so the gate's preview detection (P1-1) and this tool's auto-detect stay
# byte-identical (8000 is Disco's canonical user-visible port; NOVNC_PORT is
# deliberately excluded — it's the live-view bridge, never the app under test).

# Network failures that are NEVER load-bearing for "does the app work" — a missing
# favicon or a blocked analytics beacon must not fail an otherwise-good build.
_IGNORABLE_NETWORK = (
    "favicon",
    "/analytics",
    "google-analytics",
    "googletagmanager",
    "gtag/js",
    "/__vite_ping",
    "hot-update.json",
    "/sockjs-node",
)


def _source_of(entry: dict[str, Any]) -> str:
    """Render a console entry's location dict into a `url:line:col` string."""
    loc = entry.get("location") or {}
    url = loc.get("url")
    if not url:
        return ""
    line = loc.get("lineNumber")
    if line is None:
        return str(url)
    col = loc.get("columnNumber")
    return f"{url}:{line}:{col}" if col is not None else f"{url}:{line}"


def _console_errors(console: list[dict[str, Any]]) -> list[dict[str, str]]:
    """error-level / uncaught console entries only (warnings handled separately)."""
    out: list[dict[str, str]] = []
    for c in console:
        if c.get("level") == "error":
            out.append(
                {
                    "text": str(c.get("text", "")),
                    "source": _source_of(c),
                    "stack": str(c.get("stack", "") or ""),
                }
            )
    return out


def _console_warnings(console: list[dict[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for c in console:
        if c.get("level") == "warning":
            out.append({"text": str(c.get("text", "")), "source": _source_of(c)})
    return out


def _is_ignorable_network(entry: dict[str, Any]) -> bool:
    url = str(entry.get("url", "")).lower()
    return any(token in url for token in _IGNORABLE_NETWORK)


def _critical_network_failures(network: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """4xx/5xx responses + hard request failures, minus the favicon/analytics
    allowlist. These are the network failures that mean the app is broken."""
    out: list[dict[str, Any]] = []
    for n in network:
        if _is_ignorable_network(n):
            continue
        status = n.get("status")
        failure = n.get("failure")
        is_http_error = isinstance(status, int) and not isinstance(status, bool) and status >= 400
        if failure or is_http_error:
            out.append(
                {
                    "method": str(n.get("method", "GET")),
                    "url": str(n.get("url", "")),
                    "status": status if isinstance(status, int) else None,
                    "failure": str(failure) if failure else None,
                }
            )
    return out


def _normalize_error_text(text: str) -> str:
    """Stabilize an error signature so the SAME bug fingerprints identically across
    reloads: lowercase, collapse whitespace, and strip volatile numerics (line/col
    offsets, hex addresses, hashed asset names) that differ run-to-run."""
    t = text.strip().lower()
    t = re.sub(r"0x[0-9a-f]+", "0x", t)  # hex addresses
    t = re.sub(r"[0-9a-f]{8,}", "", t)  # content hashes (asset fingerprints)
    t = re.sub(r"\d+", "", t)  # line/col numbers, counts
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _path_of(url: str) -> str:
    """Path component of a URL (drop scheme/host/query) for a stable network sig."""
    u = re.sub(r"^[a-z]+://[^/]+", "", str(url))
    return u.split("?", 1)[0].split("#", 1)[0]


def _failure_fingerprint(
    console_errors: list[dict[str, str]], network_failures: list[dict[str, Any]]
) -> str:
    """Stable 16-hex hash of the top console-error signatures + critical network
    failures. Identical failures → identical fingerprint (the loop-breaker key);
    a different bug → different fingerprint. Empty inputs → a fixed 'clean' hash
    (never collides with a real failure set)."""
    sigs: list[str] = []
    for e in console_errors[:5]:
        sigs.append("E:" + _normalize_error_text(e["text"]))
    for n in network_failures[:5]:
        marker = str(n.get("status") or n.get("failure") or "fail")
        sigs.append(f"N:{marker}:{_path_of(n.get('url', ''))}")
    raw = "|".join(sorted(sigs)) if sigs else "CLEAN"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def compute_verdict(
    *,
    url: str,
    reachable: bool,
    http_status: int,
    structured: dict[str, Any] | None,
    meaningful: bool,
) -> dict[str, Any]:
    """Pure verdict builder — deterministic checks decide pass/fail/degraded.

    Order of judgement (first failing gate wins the verdict):
      1. server reachable + HTTP 2xx/3xx           → else FAIL (not serving)
      2. no console errors / critical network fails → else FAIL (runtime broken)
      3. meaningful (non-blank) render              → else DEGRADED (mounted nothing)
      4. all pass                                   → PASS

    Vision is ADVISORY only and never runs here in v1 (it is escalation for
    layout/blank-canvas judgement); the field is reported as unused so a normal
    pass never depends on it."""
    s = structured or {}
    console = s.get("console", []) or []
    network = s.get("network", []) or []
    console_errors = _console_errors(console)
    console_warnings = _console_warnings(console)
    network_failures = _critical_network_failures(network)
    title = str(s.get("title", "") or "")
    text = str(s.get("text", "") or "")
    elements = s.get("elements", []) or []
    visible_text_chars = len(text.strip())
    elements_count = len(elements) if isinstance(elements, list) else 0
    screenshot_path = str(s.get("screenshot_path", "") or "")
    fingerprint = _failure_fingerprint(console_errors, network_failures)

    http_ok = isinstance(http_status, int) and 200 <= http_status < 400

    if not reachable or not http_ok:
        passed, verdict = False, "fail"
        shown = http_status if reachable else "no response"
        summary = f"App not serving: {url} returned {shown}."
        next_action = (
            "Start the dev server on the preview port (e.g. `python3 -m http.server 8000` "
            "for static files, or your framework's dev command) and confirm it returns HTTP 200."
        )
    elif console_errors or network_failures:
        passed, verdict = False, "fail"
        if console_errors:
            first = console_errors[0]
            where = f" @ {first['source']}" if first["source"] else ""
            summary = (
                f"App loaded (HTTP {http_status}) but threw a console error: "
                f"{first['text']}{where}"
            )
            next_action = f"Fix the console error: {first['text']}"
        else:
            nf = network_failures[0]
            marker = nf.get("status") or nf.get("failure") or "failed"
            summary = (
                f"App loaded (HTTP {http_status}) but a request failed: "
                f"{nf['method']} {nf['url']} -> {marker}"
            )
            next_action = (
                f"Fix the failing request {nf['method']} {nf['url']} ({marker}) — "
                "the endpoint/asset is missing or erroring."
            )
    elif not meaningful:
        passed, verdict = False, "degraded"
        summary = (
            f"App served HTTP {http_status} with a clean console but rendered no visible "
            f"content (blank page — {visible_text_chars} text chars, {elements_count} elements)."
        )
        next_action = (
            "The page mounts nothing a user can see — check that the app renders into the "
            "DOM (an SPA may be failing to hydrate, or the root element is empty)."
        )
    else:
        passed, verdict = True, "pass"
        summary = (
            f"{url} served HTTP {http_status} with {visible_text_chars} chars of visible "
            f"content, {elements_count} interactive elements, and no console/network errors."
        )
        next_action = ""

    return {
        "passed": passed,
        "verdict": verdict,
        "url": url,
        "http_status": http_status,
        "title": title,
        "meaningful_content": meaningful,
        "visible_text_chars": visible_text_chars,
        "elements_count": elements_count,
        "console_errors": console_errors,
        "console_warnings": console_warnings,
        "network_failures": network_failures,
        "screenshot_path": screenshot_path,
        "vision": {"used": False, "passed": None, "notes": []},
        "failure_fingerprint": fingerprint,
        "summary": summary,
        "next_action": next_action,
    }


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
                browser_outcome = await BrowserTool().run(
                    BrowserArgs(action="navigate", url=url), ctx
                )
                if browser_outcome.success and browser_outcome.structured:
                    structured = browser_outcome.structured
                elif (browser_outcome.structured or {}).get("browser_unavailable"):
                    # ROOT-3 — no browser daemon on this backend. The server IS
                    # reachable (HTTP probe passed); we simply cannot run the
                    # render/console checks here. Return a TERMINAL verdict the agent
                    # treats as done-with-verification, NOT a misleading blank-render
                    # DEGRADED that it would try to "fix" forever.
                    return ToolOutcome(
                        success=True,
                        content=(
                            f"VERIFY_WEB_APP: UNVERIFIABLE (server reachable)\n"
                            f"url: {url}  http_status: {http_status}\n"
                            f"summary: server reachable at {url} (HTTP {http_status}); "
                            f"{BROWSER_UNAVAILABLE_MSG}"
                        ),
                        structured={
                            "verdict": "unverifiable",
                            "passed": True,
                            "url": url,
                            "http_status": http_status,
                            "browser_unavailable": True,
                            "summary": (
                                f"server reachable at {url} (HTTP {http_status}); "
                                f"{BROWSER_UNAVAILABLE_MSG}"
                            ),
                            "next_action": "",
                        },
                    )

            meaningful = self._meaningful(structured)
            verdict = compute_verdict(
                url=url,
                reachable=reachable,
                http_status=http_status,
                structured=structured,
                meaningful=meaningful,
            )
            return ToolOutcome(
                success=True,  # the verdict ran; pass/fail lives in structured
                content=_render(verdict),
                structured=verdict,
            )
        except Exception as e:  # noqa: BLE001 — never crash the loop; report a verdict-shaped error
            return ToolOutcome(
                success=False, content="", error=f"verify_web_app error: {e}"
            )

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
        host_shared = getattr(ctx.sandbox, "workspace_path", None) is not None
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
        from disco.core.loop.preview_target import PortOwnership, resolve_preview_port

        from ..sandbox.port_owner import port_owners

        try:
            owners = await port_owners(ctx.sandbox, list(_PREVIEW_PORTS))
        except Exception:  # noqa: BLE001 — detection failure → resolver default
            owners = {}

        host_shared = getattr(ctx.sandbox, "workspace_path", None) is not None
        owned = {
            p: PortOwnership(pid=o.pid, session=o.session)
            for p, o in owners.items()
            if o is not None
        }
        chosen = resolve_preview_port(
            host_shared=host_shared,
            owned=owned,
            conversation_id=str(getattr(ctx.sandbox, "conversation_id", "") or ""),
            preview_ports=_PREVIEW_PORTS,
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
            res = await ctx.sandbox.exec_shell(
                f"python3 -c {shlex.quote(script)}", timeout_s=15
            )
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
