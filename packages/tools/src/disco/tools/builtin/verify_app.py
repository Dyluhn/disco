"""`verify_web_app` — the build agent's self-test tool (W-45 v1) — compatibility facade.

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

The cohesive implementation lives in the private ``verify_app_parts`` package
(trusted-component folding, preview-target detection, game-interaction, render).
This module is a state-free compatibility facade that re-exports the public
names and owns the ``VerifyWebAppTool`` class so existing importers (the
AppKit verifier, tests) keep working unchanged.

Tools are evidence producers, not verdict authorities: the target probes here
never manufacture or upgrade a typed ``HostVerificationResult``. Their output is
immutable evidence bound for later host verification.
"""

from __future__ import annotations

from typing import Any, Literal

from disco.core import SecurityRisk
from disco.core.effects import EffectCapability

# `_PREVIEW_PORTS` is imported lazily inside the preview-target parts to avoid a
# top-of-module core import cycle; the symbol is re-exported here for any caller
# that imported it from this module.
from disco.core.loop.finish import _PREVIEW_PORTS  # noqa: F401 — re-exported
from disco.core.trusted_components.registry import TrustedComponentRegistry
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..behavior import declares
from ._outcomes import fail_outcome
from .browser import BROWSER_UNAVAILABLE_MSG, BrowserArgs, BrowserTool
from .verify_app_parts._game_interaction import _capture_game_interaction
from .verify_app_parts._preview_target import (
    _detect_preview_url,
    _explicit_url_allowed,
    _probe_http,
    _rejected_explicit_outcome,
)
from .verify_app_parts._render import _render
from .verify_app_parts._trusted_components import (
    _append_eject_banner,
    _fold_checks,
    _fold_trusted_components,
    _make_probe_runner,
    _merge_component_checks,
)


def _verify_web_app_failure_recipe(http_status: int | None = None) -> str:
    """The verify_web_app recipe, PROJECTED from what the probe actually saw.

    Constraint 3: a failure recipe that is identical whether the server was
    unreachable or returned a 500 makes the agent re-derive the diagnosis the
    probe already made. Naming the observed status picks the ONE next move.
    """
    if http_status is None or http_status == 0:
        return (
            "Nothing answered on that URL. Next move: run preview_status to see "
            "whether a preview is running (and preview_logs if it is), start it "
            "if not, then re-run verify_web_app once."
        )
    if http_status >= 500:
        return (
            f"The server answered HTTP {http_status}, so it is running but "
            "erroring. Next move: read preview_logs for the traceback (preview_status "
            "confirms which process is serving), fix it, then re-run verify_web_app "
            "once."
        )
    return (
        f"The server answered HTTP {http_status}. Next move: check "
        "preview_status (is the right entry being served?) and preview_logs, "
        "then re-run verify_web_app once."
    )


_VERIFY_WEB_APP_FAILURE_RECIPE = _verify_web_app_failure_recipe()


def _failure_fingerprint(
    console_errors: list[dict[str, str]], network_failures: list[dict[str, Any]]
) -> str:
    from disco.tools.verify.web_app_probe import _failure_fingerprint as _compute

    return _compute(console_errors, network_failures)


# Re-export compute_verdict for compatibility (tests import it from this module).
from disco.tools.verify.web_app_probe import compute_verdict  # noqa: E402, F401


class VerifyWebAppArgs(BaseModel):
    url: str = Field(
        default="",
        description=(
            "Exact in-sandbox URL returned by preview_start. Leave empty to "
            "auto-detect the running preview server; never guess a fixed port."
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
            "Optionally debug the running web app with a STRUCTURED pass/fail verdict "
            "(not a raw page dump). Checks: server reachable, HTTP 2xx/3xx, no console "
            "errors, no critical network failures, and a meaningful (non-blank) render. "
            "Returns verdict (pass/fail/degraded), the failing console/network details, "
            "a screenshot path, a stable failure_fingerprint, and the exact next_action "
            "to fix. This is limited runtime/render evidence, not semantic completeness or "
            "completion authority. One use spends one of the TWO debug calls shared with "
            "verify_appkit_app and design_lint for the current artifact bytes; only a real "
            "successful artifact mutation resets that allowance. Auto-detects the preview "
            "port if no url is given."
        ),
        args_model=VerifyWebAppArgs,
        needs=frozenset({Capability.NETWORK, Capability.DISPLAY, Capability.SHELL}),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        # The host FINISHED verifier invokes this same operation out of band.
        # Its outer deadline is projected from this declaration so neither path
        # can acquire an independently drifting timeout budget.
        timeout_s=300,
        behavior=declares(EffectCapability.ARTIFACT_VERIFY, planner_safe=False),
    )

    async def run(self, args: VerifyWebAppArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        try:
            url, rejected = await self._resolve_url(args, ctx)
            if rejected is not None:
                return rejected
            reachable, http_status = await self._probe_http(ctx, url)
            structured: dict[str, Any] | None = None
            if reachable:
                structured, terminal = await self._collect_browser_evidence(
                    args, ctx, url, http_status
                )
                if terminal is not None:
                    return terminal
            return await self._verdict_outcome(
                args, ctx, url, reachable, http_status, structured
            )
        except Exception as e:  # noqa: BLE001 — never crash the loop; report a verdict-shaped error
            return fail_outcome(f"verify_web_app error: {e}\n{_VERIFY_WEB_APP_FAILURE_RECIPE}")

    async def _resolve_url(
        self, args: VerifyWebAppArgs, ctx: ToolContext
    ) -> tuple[str, ToolOutcome | None]:
        explicit = (args.url or "").strip()
        if not explicit:
            return await self._detect_preview_url(ctx), None
        # An agent-supplied URL must pass the same backend-aware ownership rule
        # as auto-detection. A reserved or sibling port is never accepted.
        if not await self._explicit_url_allowed(explicit, ctx):
            return explicit, self._rejected_explicit_outcome(explicit)
        return explicit, None

    async def _collect_browser_evidence(
        self,
        args: VerifyWebAppArgs,
        ctx: ToolContext,
        url: str,
        http_status: int,
    ) -> tuple[dict[str, Any] | None, ToolOutcome | None]:
        browser_outcome = await BrowserTool().run(self._browser_args(args, url), ctx)
        if browser_outcome.success and browser_outcome.structured:
            structured = browser_outcome.structured
            if args.medium == "game":
                structured = await self._capture_game_interaction(ctx, structured)
            return structured, None
        browser_details = browser_outcome.structured or {}
        if browser_details.get("browser_unavailable"):
            return None, await self._browser_unavailable_outcome(
                ctx, url, http_status, browser_details
            )
        detail = (
            browser_outcome.error
            or browser_outcome.content
            or "browser returned no diagnostic"
        )
        return None, self._render_probe_failure(url, http_status, detail)

    @staticmethod
    def _browser_args(args: VerifyWebAppArgs, url: str) -> BrowserArgs:
        if args.medium == "mobile":
            return BrowserArgs(
                action="navigate",
                url=url,
                viewport_width=390,
                viewport_height=844,
            )
        return BrowserArgs(action="navigate", url=url)

    async def _browser_unavailable_outcome(
        self,
        ctx: ToolContext,
        url: str,
        http_status: int,
        browser_details: dict[str, Any],
    ) -> ToolOutcome:
        unverifiable: dict[str, Any] = {
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
        startup_diagnostic = browser_details.get("startup_diagnostic")
        if startup_diagnostic:
            unverifiable["startup_diagnostic"] = startup_diagnostic
        # Integrity/dependency/HTTP probes remain available without rendering.
        unverifiable = await self._fold_trusted_components(
            ctx, unverifiable, probes_allowed=True
        )
        return ToolOutcome(
            success=True,
            content=(
                "VERIFY_WEB_APP: UNVERIFIABLE (server reachable)\n"
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

    @staticmethod
    def _render_probe_failure(url: str, http_status: int, detail: str) -> ToolOutcome:
        return fail_outcome(
            (
                f"verify_web_app render probe failed after HTTP {http_status}: "
                f"{detail}\n{_verify_web_app_failure_recipe(http_status)}"
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

    async def _verdict_outcome(
        self,
        args: VerifyWebAppArgs,
        ctx: ToolContext,
        url: str,
        reachable: bool,
        http_status: int,
        structured: dict[str, Any] | None,
    ) -> ToolOutcome:
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
        verdict = await self._fold_trusted_components(
            ctx, verdict, probes_allowed=reachable
        )
        return ToolOutcome(
            success=True,
            content=_render(verdict),
            structured=verdict,
        )

    # ---- trusted-component folding (delegates to verify_app_parts._trusted_components) ----

    async def _fold_trusted_components(
        self, ctx: ToolContext, verdict: dict[str, Any], *, probes_allowed: bool
    ) -> dict[str, Any]:
        return await _fold_trusted_components(
            ctx,
            verdict,
            probes_allowed=probes_allowed,
            registry_factory=TrustedComponentRegistry.default,
        )

    async def _fold_checks(
        self,
        ctx: ToolContext,
        verdict: dict[str, Any],
        lock: Any,
        checks_out: list[dict[str, str]],
        *,
        probes_allowed: bool,
    ) -> dict[str, Any]:
        return await _fold_checks(
            ctx,
            verdict,
            lock,
            checks_out,
            probes_allowed=probes_allowed,
            registry_factory=TrustedComponentRegistry.default,
        )

    @staticmethod
    def _merge_component_checks(
        verdict: dict[str, Any], checks: list[dict[str, str]], *, blocking: bool
    ) -> dict[str, Any]:
        return _merge_component_checks(verdict, checks, blocking=blocking)

    def _make_probe_runner(
        self,
        ctx: ToolContext,
        base_url: str,
        lock: Any,
        registry: TrustedComponentRegistry,
    ):
        return _make_probe_runner(ctx, base_url, lock, registry)

    async def _append_eject_banner(self, ctx: ToolContext, lock: Any, name: str) -> None:
        await _append_eject_banner(ctx, lock, name)

    # ---- game interaction (delegates to verify_app_parts._game_interaction) ----

    async def _capture_game_interaction(
        self, ctx: ToolContext, before: dict[str, Any]
    ) -> dict[str, Any]:
        return await _capture_game_interaction(ctx, before)

    # ---- preview target (delegates to verify_app_parts._preview_target) ----

    async def _explicit_url_allowed(self, url: str, ctx: ToolContext) -> bool:
        return await _explicit_url_allowed(url, ctx)

    def _rejected_explicit_outcome(self, url: str) -> ToolOutcome:
        return _rejected_explicit_outcome(url)

    async def _detect_preview_url(self, ctx: ToolContext) -> str:
        return await _detect_preview_url(ctx)

    async def _probe_http(self, ctx: ToolContext, url: str) -> tuple[bool, int]:
        return await _probe_http(ctx, url)

    # ---- meaningful render (reuses the finish gate's blank-render guard) ----

    @staticmethod
    def _meaningful(structured: dict[str, Any] | None) -> bool:
        if not structured:
            return False
        # REUSE the finish gate's blank-render guard so "served 200 but mounted
        # nothing" is judged identically on both sides of the contract.
        from disco.core.loop.finish import _browser_content_meaningful

        return _browser_content_meaningful(structured)


__all__ = [
    "VerifyWebAppArgs",
    "VerifyWebAppTool",
    "compute_verdict",
]
