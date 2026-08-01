# base64/ipaddress/re/deque/urlsplit are no longer called directly in this
# module — their call sites moved into `_browser_daemon_parts/*.py` along
# with the code that used them. Kept as explicit self-re-exports (ruff-clean,
# verified empirically in Epic 10-B) purely so every name this module bound
# before the split is still bound after it.
import base64 as base64
import hashlib
import ipaddress as ipaddress
import json
import os
import re as re
import secrets
import signal
import sys
from collections import deque as deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlsplit as urlsplit

from playwright.sync_api import sync_playwright

from ._browser_daemon_parts import dispatch as _dispatch
from ._browser_daemon_parts import dom_signals as _dom_signals
from ._browser_daemon_parts.dom_signals import (
    _count_visible_semantic_elements as _count_visible_semantic_elements,
)
from ._browser_daemon_parts.dom_signals import _visible_dom_text as _visible_dom_text
from ._browser_daemon_parts.lane_lifecycle import LaneLifecycle
from ._browser_daemon_parts.nonce_ledger import NonceLedger
from ._browser_daemon_parts.protocol_helpers import _TOKEN_RE as _TOKEN_RE
from ._browser_daemon_parts.protocol_helpers import is_token as _is_token
from ._browser_daemon_parts.protocol_helpers import page_kind as _page_kind
from ._browser_daemon_parts.protocol_helpers import positive_epoch as _positive_epoch
from ._browser_daemon_parts.render_probe import BrowserRendererUnavailable
from ._browser_daemon_parts.render_probe import text_renderer_ready as _text_renderer_ready

# Process sandboxes export the real jailed workspace path; container backends
# use the common /workspace guest path.
WORKSPACE_ROOT = os.environ.get("DISCO_WORKSPACE", os.environ.get("PMX_WORKSPACE", "/workspace"))

# _live_view is shipped alongside the daemon by browser.py as
# /workspace/.pmx/_live_view.py.  The sys.path insert makes it importable
# without a package install; the try/except keeps unit tests that don't
# have the sandbox image from failing on import.
sys.path.insert(0, os.path.join(WORKSPACE_ROOT, ".pmx"))
try:
    import _live_view  # type: ignore[import]  # shipped alongside by browser.py
except ImportError:
    _live_view = None  # not available (unit tests / missing binary)

# Flag: has the browser been restarted in headed mode for live view?
_live_headed: bool = False

# Configuration
try:
    PORT = int(os.environ.get("DISCO_BROWSER_PORT", "8901"))
except ValueError:
    PORT = 8901
if not 0 <= PORT <= 65535:
    PORT = 8901
PORT_FILE = os.path.join(WORKSPACE_ROOT, ".pmx/browser-port")
INSTANCE_ID = hashlib.sha256(WORKSPACE_ROOT.encode()).hexdigest()
SCREENSHOT_DIR = os.path.join(WORKSPACE_ROOT, ".pmx/screenshots")
MAX_CONSOLE = 200
# B7: bound the captured network-failure ring the same way the console is bounded,
# so a page that hammers a dead endpoint can't grow capture without limit.
MAX_NETWORK = 100
MAX_TEXT = 4000
MAX_ELEMENTS = 120
# B-F: SPAs (React/Vite) mount the DOM AFTER `load` via JS hydration, so a single
# read taken right after the fixed settle can capture an empty body/elements when
# the mount lands a few hundred ms late. Retry-until-content: poll the capture a
# bounded number of times, breaking as soon as text OR elements appear. A
# genuinely-blank page still returns empty after the cap (no false stall). We do
# NOT switch navigate to wait_until="networkidle" because Disco serves live apps
# with websockets/HMR, where networkidle can hang the turn indefinitely.
CAPTURE_RETRY_MAX = 8
CAPTURE_RETRY_INTERVAL_MS = 250
# W6: click timeout cut from Playwright's 30s default to a few seconds so a
# div-based dock/button that is briefly un-clickable doesn't stall a full turn.
CLICK_TIMEOUT_MS = 3000
DAEMON_INSTANCE_ID = secrets.token_hex(16)
_LANES = frozenset({"agent", "host_verifier"})
_SYNC_ACTIONS = frozenset({"screenshot", "click", "fill", "press", "submit", "console_view"})
_LEGACY_GENERATION = "0" * 32
_RECENT_NONCE_LIMIT = 256

# W-46: rich sites behind a WAF (Akamai/CF) fingerprint the headless browser and
# serve the lite/challenge variant — egress is fine, the *fingerprint* is the tell.
# A realistic desktop-Chrome UA (no "HeadlessChrome") makes the request/headers look
# like a normal browser. Keep this a recent, plausible stable-channel Chrome string.
_REALISTIC_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# W-46: injected into every page BEFORE its own scripts run, to erase the cheap
# JS-visible headless tells: navigator.webdriver===true, an empty plugins array,
# and a missing/odd languages list. HONEST LIMIT: this closes ~80% of bot checks
# (the header/webdriver heuristics); sophisticated anti-bot (Akamai/CF behavioral
# scoring, canvas/WebGL fingerprinting, TLS JA3) can STILL challenge — this is the
# high-value, low-cost evasion, not a full stealth stack.
_STEALTH_INIT_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
"""


# BrowserRendererUnavailable, _text_renderer_ready, _is_token, _positive_epoch,
# and _page_kind now live in _browser_daemon_parts (imported above with the
# original names preserved) — split out to keep this module under its size
# budget. None of them read instance state, and _page_kind is the only one an
# external test calls by this module's dotted path, so a plain (non-`x as x`)
# aliased import is enough: every one of them is also still called from code
# in THIS file (BrowserState.start, BrowserHandler._protocol/_freshness), so
# ruff sees real use, not a bare re-export.


class BrowserPageState:
    """State owned by one of the daemon's two fixed browser lanes."""

    def __init__(self):
        self.page = None
        self.console_logs = []
        self.network_fails = []
        self.document_content_type = ""
        self.executor_generation = None
        self.synchronized_epoch = None

    def reset_freshness(self):
        self.executor_generation = None
        self.synchronized_epoch = None

    def clear_diagnostics(self):
        """Clear this lane's console/network history and forget its main-document type."""
        self.console_logs = []
        self.network_fails = []
        self.document_content_type = ""

    def reset_for_new_page(self):
        """Clear diagnostics + freshness ahead of binding a fresh page to this lane."""
        self.console_logs = []
        self.network_fails = []
        self.reset_freshness()


class BrowserState:
    def __init__(self):
        self.playwright = None
        self.browser = None
        # The agent and host verifier never share a BrowserContext. Contexts are
        # the Playwright ownership boundary for cookies, local/session storage,
        # cache, and service workers; separate pages alone are insufficient.
        self.context = None
        self.host_context = None
        self._lanes = {name: BrowserPageState() for name in _LANES}
        # Lane page-provisioning/generation-recycling and nonce replay-defense
        # are each a distinct authority from Playwright process ownership —
        # split into their own owners (PY-0729: this class was over the
        # public-method budget with them inlined).
        self._lane_lifecycle = LaneLifecycle(self)
        self._nonces = NonceLedger(_RECENT_NONCE_LIMIT)
        self.screenshot_seq = 0
        self.render_ready = False
        self.render_error = ""

    # Compatibility properties for existing callers/tests: the historical
    # single page is exactly the fixed agent lane.
    @property
    def page(self):
        return self._lanes["agent"].page

    @page.setter
    def page(self, value):
        self._lanes["agent"].page = value

    @property
    def console_logs(self):
        return self._lanes["agent"].console_logs

    @console_logs.setter
    def console_logs(self, value):
        self._lanes["agent"].console_logs = value

    @property
    def network_fails(self):
        return self._lanes["agent"].network_fails

    @network_fails.setter
    def network_fails(self, value):
        self._lanes["agent"].network_fails = value

    def lane(self, name):
        if name not in _LANES:
            raise ValueError("unknown browser lane")
        return self._lanes[name]

    def _bind_page(self, lane_name, page):
        lane = self.lane(lane_name)
        lane.page = page
        page.on("console", lambda msg, name=lane_name: self._add_console_for(name, msg))
        page.on("pageerror", lambda err, name=lane_name: self._add_pageerror_for(name, err))
        page.on(
            "requestfailed",
            lambda request, name=lane_name: self._add_request_failed_for(name, request),
        )
        page.on("response", lambda response, name=lane_name: self._add_response_for(name, response))
        return lane

    # Page-provisioning, host-lane close, and generation-recycling now live on
    # `self._lane_lifecycle` (see `_browser_daemon_parts/lane_lifecycle.py`) —
    # none of the three had a caller outside this file, so there is no
    # compatibility method left behind here.

    def _new_context(self):
        if self.browser is None:
            raise RuntimeError("Browser not initialized")
        context = self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=_REALISTIC_UA,
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        context.add_init_script(_STEALTH_INIT_SCRIPT)
        return context

    def start(self, display: str | None = None):
        self.playwright = sync_playwright().start()
        # gVisor is the isolation boundary, hence --no-sandbox is acceptable HERE only.
        # W-46: --disable-blink-features=AutomationControlled removes the Blink flag
        # that otherwise sets navigator.webdriver=true and trips WAF bot heuristics.
        launch_args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        # S-W5: agent/browser sandboxes reach the public web through the
        # private/non-global-denying sidecar, not a raw bridge. Playwright does
        # not reliably inherit HTTP_PROXY into Chromium, so wire it explicitly.
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        launch_kwargs = {}
        executable_path = os.environ.get("DISCO_BROWSER_EXECUTABLE")
        if executable_path:
            launch_kwargs["executable_path"] = executable_path
        if proxy_url:
            launch_kwargs["proxy"] = {
                "server": proxy_url,
                "bypass": "localhost,127.0.0.1,[::1]",
            }
        if display:
            import os as _os

            _os.environ["DISPLAY"] = display
            launch_args.append(f"--display={display}")
            self.browser = self.playwright.chromium.launch(
                headless=False, args=launch_args, **launch_kwargs
            )
        else:
            self.browser = self.playwright.chromium.launch(
                headless=True, args=launch_args, **launch_kwargs
            )
        # W-46: a realistic desktop-Chrome context — UA without "HeadlessChrome", a
        # locale/timezone, and an Accept-Language header — so a server fingerprinting
        # the request sees a normal browser. Same sandbox egress; only the fingerprint
        # changes (lite.cnn worked already; full CNN's WAF keyed on these tells).
        self.context = self._new_context()
        self.host_context = None
        calibration_page = self.context.new_page()
        try:
            if not _text_renderer_ready(calibration_page):
                self.render_error = (
                    "browser renderer unavailable: Chromium could not lay out ordinary "
                    "system-font text; painted browser evidence is unavailable"
                )
                raise BrowserRendererUnavailable(self.render_error)
        finally:
            calibration_page.close()
        # A restart creates two empty bounded lanes, with the host-verifier page
        # lazy so ordinary browsing pays for one page only.
        self._lanes = {name: BrowserPageState() for name in _LANES}
        self._nonces.clear()
        self._bind_page("agent", self.context.new_page())
        self.render_ready = True
        self.render_error = ""

    def stop(self):
        """Close Playwright in ownership order; safe after partial startup."""
        errors = []
        resources: list[tuple[str, Any | None]] = [("page", self.page)]
        host_page = self._lanes["host_verifier"].page
        if host_page is not None:
            resources.append(("host_verifier_page", host_page))
        resources.extend(
            (
                ("host_verifier_context", self.host_context),
                ("context", self.context),
                ("browser", self.browser),
            )
        )
        for resource_name, resource in resources:
            if resource is not None:
                try:
                    resource.close()
                except Exception as exc:
                    errors.append(f"{resource_name}:{type(exc).__name__}")
        for lane in self._lanes.values():
            lane.page = None
            lane.console_logs = []
            lane.network_fails = []
            lane.reset_freshness()
        self.context = None
        self.host_context = None
        self.browser = None
        playwright, self.playwright = self.playwright, None
        if playwright is not None:
            try:
                playwright.stop()
            except Exception as exc:
                errors.append(f"playwright:{type(exc).__name__}")
        if errors:
            # Teardown must continue through every owner, but it must not silently
            # hide partial cleanup. Keep the diagnostic bounded and data-free.
            print("Browser daemon cleanup errors: " + ", ".join(errors), file=sys.stderr)

    def _add_console(self, msg):
        self._add_console_for("agent", msg)

    def _add_console_for(self, lane_name, msg):
        # B7: keep msg.location ({url, lineNumber, columnNumber}) so the agent gets a
        # source:line, not just bare text. location is a property; guard defensively.
        entry = {"level": msg.type, "text": msg.text}
        location = getattr(msg, "location", None)
        if location:
            entry["location"] = location
        logs = self.lane(lane_name).console_logs
        logs.append(entry)
        if len(logs) > MAX_CONSOLE:
            logs.pop(0)

    def _add_pageerror(self, err):
        self._add_pageerror_for("agent", err)

    def _add_pageerror_for(self, lane_name, err):
        # B7: keep err.stack (the most useful field for tracing a crash) alongside
        # the message instead of dropping it.
        entry = {"level": "error", "text": err.message}
        stack = getattr(err, "stack", None)
        if stack:
            entry["stack"] = stack
        logs = self.lane(lane_name).console_logs
        logs.append(entry)
        if len(logs) > MAX_CONSOLE:
            logs.pop(0)

    def _add_request_failed(self, request):
        self._add_request_failed_for("agent", request)

    def _add_request_failed_for(self, lane_name, request):
        # B7: a request that never got a response (DNS, refused, aborted, timeout).
        # request.failure is the error text (or None on some engines).
        failure = getattr(request, "failure", None)
        self._record_network_for(
            lane_name,
            {
                "method": request.method,
                "url": request.url,
                "failure": failure or "failed",
            },
        )

    def _add_response(self, response):
        self._add_response_for("agent", response)

    def _add_response_for(self, lane_name, response):
        lane = self.lane(lane_name)
        request = getattr(response, "request", None)
        try:
            is_main_document = (
                request is not None
                and request.resource_type == "document"
                and (
                    lane.page is None
                    or getattr(lane.page, "main_frame", None) is None
                    or request.frame == lane.page.main_frame
                )
            )
        except Exception:
            is_main_document = False
        if is_main_document:
            try:
                headers = response.headers
            except Exception:
                headers = {}
            raw_content_type = headers.get("content-type", "") if isinstance(headers, dict) else ""
            lane.document_content_type = (
                raw_content_type if isinstance(raw_content_type, str) else ""
            )
        # B7: a response that *did* arrive but with an error status (4xx/5xx).
        status = response.status
        if status >= 400:
            self._record_network_for(
                lane_name,
                {
                    "method": response.request.method,
                    "url": response.url,
                    "status": status,
                },
            )

    def _record_network(self, entry):
        self._record_network_for("agent", entry)

    def _record_network_for(self, lane_name, entry):
        failures = self.lane(lane_name).network_fails
        failures.append(entry)
        if len(failures) > MAX_NETWORK:
            failures.pop(0)

    def clear_console(self):
        self.lane("agent").clear_diagnostics()


state = BrowserState()


# `_visible_dom_text` and `_count_visible_semantic_elements` — the
# DOM-analysis helpers formerly here — now live in
# `_browser_daemon_parts/dom_signals.py` (imported above with the original
# names preserved): their embedded JS templates pushed both this module and
# (for `_count_visible_semantic_elements`) the callable itself over budget.


class BrowserHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            if state.page and state.render_ready:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(INSTANCE_ID.encode("ascii"))
            else:
                self.send_response(503)
                self.end_headers()
                detail = state.render_error or "browser renderer not ready"
                self.wfile.write(detail.encode("utf-8", errors="replace")[:512])
        elif self.path == "/identity":
            if state.page and state.render_ready:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(DAEMON_INSTANCE_ID.encode("ascii"))
            else:
                self.send_response(503)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            self._send_json(
                {
                    "ok": False,
                    "error": "Invalid JSON",
                    "error_class": "freshness_protocol_invalid",
                    "error_reason": "invalid_json",
                },
                400,
            )
            return

        if not isinstance(params, dict):
            self._send_json(
                {
                    "ok": False,
                    "error": "browser request must be a JSON object",
                    "error_class": "freshness_protocol_invalid",
                    "error_reason": "invalid_request",
                },
                400,
            )
            return

        action = params.get("action")
        try:
            result = self._handle_action(action, params)
            self._send_json(result)
        except Exception as e:
            self._send_json(
                {
                    "ok": False,
                    "error": f"browser daemon internal error: {type(e).__name__}",
                    "error_class": "browser_daemon_unavailable",
                    "error_reason": "internal_error",
                },
                500,
            )

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    @staticmethod
    def _error(error_class, error_reason, message, freshness=None):
        result = {
            "ok": False,
            "error": str(message or error_reason)[:512],
            "error_class": error_class,
            "error_reason": error_reason,
        }
        if freshness is not None:
            result["freshness"] = freshness
        return result

    @staticmethod
    def _protocol(params):
        protocol_keys = {
            "workspace_epoch",
            "executor_generation",
            "browser_lane",
            "request_nonce",
        }
        present = protocol_keys & set(params)
        if not present:
            # Compatibility for direct epoch-zero daemon clients. The shipped
            # BrowserTool always sends v1 metadata and refuses an unversioned
            # response once freshness is security/reliability relevant.
            return "agent", _LEGACY_GENERATION, secrets.token_hex(16), None, None
        if present != protocol_keys:
            return None, None, None, None, "incomplete freshness request"
        lane_name = params.get("browser_lane")
        generation = params.get("executor_generation")
        nonce = params.get("request_nonce")
        epoch = params.get("workspace_epoch")
        expected_daemon_id = params.get("expected_daemon_instance_id")
        if not isinstance(lane_name, str) or lane_name not in _LANES:
            return None, None, None, None, "invalid browser lane"
        if not _is_token(generation):
            return None, None, None, None, "invalid executor generation"
        if not _is_token(nonce):
            return None, None, None, None, "invalid request nonce"
        if epoch is not None and not _positive_epoch(epoch):
            return None, None, None, None, "invalid workspace epoch"
        if expected_daemon_id is not None and not _is_token(expected_daemon_id):
            return None, None, None, None, "invalid expected daemon identity"
        if expected_daemon_id is not None and expected_daemon_id != DAEMON_INSTANCE_ID:
            return None, None, None, None, "daemon instance identity mismatch"
        if not state._nonces.accept(nonce):
            return None, None, None, None, "replayed request nonce"
        return lane_name, generation, nonce, epoch, None

    @staticmethod
    def _freshness(lane_name, generation, nonce, requested_epoch, sync_performed):
        lane = state.lane(lane_name)
        # F-29: `page.url` is a live Playwright property and raises once the
        # transport is dead. The acknowledgement must survive exactly that
        # state — it is how an internal-error reply stays protocol-valid. An
        # unreadable page has content of unknown vintage: "uninitialized".
        try:
            page_kind = _page_kind(getattr(lane.page, "url", ""))
        except Exception:
            page_kind = "uninitialized"
        return {
            "schema_version": 1,
            "daemon_instance_id": DAEMON_INSTANCE_ID,
            "executor_generation": generation,
            "lane": lane_name,
            "request_nonce": nonce,
            "requested_epoch": requested_epoch,
            "synchronized_epoch": lane.synchronized_epoch,
            "sync_performed": sync_performed,
            "page_kind": page_kind,
        }

    @staticmethod
    def _selector_for(params):
        index = params.get("index")
        css_selector = params.get("selector", "")
        click_text = params.get("click_text", "")
        if index is not None:
            return f"[data-pmx-index='{index}']"
        if css_selector:
            return css_selector
        if click_text:
            return f"text={click_text}"
        return None

    def _action_locator(self, page, selector, freshness):
        if not selector:
            return None, self._error(
                "browser_action_failed",
                "selector_not_found",
                "browser action requires an element target",
                freshness,
            )
        try:
            locator = page.locator(selector).first
            if locator.count() == 0:
                return None, self._error(
                    "browser_action_failed",
                    "selector_not_found",
                    "browser action target was not found",
                    freshness,
                )
            if not locator.is_visible():
                return None, self._error(
                    "browser_action_failed",
                    "not_visible",
                    "browser action target is not visible",
                    freshness,
                )
            if not locator.is_enabled():
                return None, self._error(
                    "browser_action_failed",
                    "disabled",
                    "browser action target is disabled",
                    freshness,
                )
            # Playwright's actionability trial detects overlays, hit-target
            # interception, instability, and other blocked interactions without
            # changing page state. Classification does not parse exception prose.
            locator.click(trial=True, timeout=CLICK_TIMEOUT_MS)
            return locator, None
        except Exception:
            return None, self._error(
                "browser_action_failed",
                "interaction_blocked",
                "browser action target is blocked or not actionable",
                freshness,
            )

    def _navigate(
        self,
        page,
        lane,
        url,
        *,
        lane_name,
        generation,
        nonce,
        requested_epoch,
    ):
        """Load `url` into this lane. Returns an error payload, or None on success.

        Split out of `_handle_action` so the dispatcher stays under its size cap
        and so the one action OUTSIDE `_SYNC_ACTIONS` has its epoch handling in
        one readable place.
        """
        if not url:
            return self._error(
                "browser_action_failed",
                "navigation_failed",
                "URL required for navigate",
                self._freshness(lane_name, generation, nonce, requested_epoch, False),
            )
        try:
            lane.clear_diagnostics()
            page.goto(url, wait_until="load")
            page.wait_for_timeout(500)  # Settle time
        except Exception:
            # Nothing loaded, so this lane's content is of UNKNOWN vintage. A
            # stale epoch here would read as a synchronized lane; clearing it
            # makes the next sync action reload, so the lane self-heals. The
            # client waives the synchronized claim for THIS reason only — see
            # `browser.py::_freshness_response_error`.
            lane.synchronized_epoch = None
            return self._error(
                "browser_action_failed",
                "navigation_failed",
                f"browser navigation failed: could not load {str(url)[:200]}",
                self._freshness(lane_name, generation, nonce, requested_epoch, False),
            )
        lane.synchronized_epoch = requested_epoch
        return None

    def _handle_action(self, action, params):
        if not isinstance(params, dict):
            return self._error(
                "freshness_protocol_invalid",
                "invalid_request",
                "browser request must be an object",
            )
        lane_name, generation, nonce, requested_epoch, protocol_error = self._protocol(params)
        if protocol_error is not None:
            return self._error(
                "freshness_protocol_invalid",
                "invalid_request",
                protocol_error,
            )
        # F-29 (counted P1, seed 600041): an exception past this point used to
        # bubble to do_POST's generic catch and reply WITHOUT the freshness
        # acknowledgement, which the client then reported as a caller-side
        # "freshness acknowledgement schema mismatch" — destroying this
        # daemon's real verdict. The protocol fields are parsed now, so every
        # reply from here on can and must acknowledge. A dead Playwright
        # transport (e.g. the workspace's own process cleanup killed the
        # driver) is additionally healed ONCE per request by restarting the
        # browser stack: the workspace owns its processes; the daemon owns
        # recovering its own transport.
        try:
            return self._dispatch_parsed(
                action, params, lane_name, generation, nonce, requested_epoch
            )
        except Exception as first_error:
            error = first_error
            if self._heal_dead_transport():
                try:
                    return self._dispatch_parsed(
                        action, params, lane_name, generation, nonce, requested_epoch
                    )
                except Exception as retry_error:
                    error = retry_error
            return self._error(
                "browser_daemon_unavailable",
                "internal_error",
                f"browser daemon internal error: {type(error).__name__}",
                self._freshness(lane_name, generation, nonce, requested_epoch, False),
            )

    @staticmethod
    def _heal_dead_transport():
        """Restart the browser stack once if the Playwright transport is dead.

        Returns True only when the transport was actually down AND a fresh
        stack started. An exception thrown over a LIVE transport is a genuine
        action bug and must surface as itself, never trigger a restart — the
        restart exists for exactly one observed family: the workspace killed
        the driver/browser processes out from under the daemon.
        """
        global _live_headed
        try:
            alive = state.browser is not None and state.browser.is_connected()
        except Exception:
            alive = False
        if alive:
            return False
        try:
            state.stop()
        except Exception:
            pass
        try:
            state.start()
        except Exception as exc:
            # Leave an honest terminal state: later requests report the
            # renderer unavailable instead of raising through do_POST.
            state.render_ready = False
            if not state.render_error:
                state.render_error = f"browser stack restart failed: {type(exc).__name__}"
            return False
        # A heal restarts headless; a previously headed live view degrades
        # honestly until the next explicit live_start.
        _live_headed = False
        return True

    def _dispatch_parsed(self, action, params, lane_name, generation, nonce, requested_epoch):
        """Real per-command dispatch table + the shared pre/post-action steps
        around it now live in `_browser_daemon_parts/dispatch.py` (PY-0732/
        PY-0733/PY-0731/PY-0734: this was a 322-logical-line / mccabe-66
        if/elif chain). `state` and every module global a test can
        monkeypatch are read here (this method's own module scope) and
        passed down explicitly, so a replaced `daemon.state` or
        `daemon._live_headed` is still honored. `_live_headed` is a module
        global only THIS module can rebind — the parts module returns the
        (possibly updated) value instead of mutating it directly.
        """
        global _live_headed
        response, _live_headed = _dispatch.dispatch_parsed(
            self,
            state,
            action,
            params,
            lane_name,
            generation,
            nonce,
            requested_epoch,
            live_view=_live_view,
            live_headed=_live_headed,
            workspace_root=WORKSPACE_ROOT,
            screenshot_dir=SCREENSHOT_DIR,
            max_text=MAX_TEXT,
            click_timeout_ms=CLICK_TIMEOUT_MS,
            capture_retry_max=CAPTURE_RETRY_MAX,
            capture_retry_interval_ms=CAPTURE_RETRY_INTERVAL_MS,
            sync_actions=_SYNC_ACTIONS,
            page_kind=_page_kind,
        )
        return response

    def _get_elements(self, page):
        return _dom_signals.get_elements(page, max_elements=MAX_ELEMENTS)


def run():
    server = None
    bound_port = None

    def _stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        state.start()
        server = HTTPServer(("127.0.0.1", PORT), BrowserHandler)
        bound_port = int(server.server_port)
        os.makedirs(os.path.dirname(PORT_FILE), mode=0o700, exist_ok=True)
        tmp_port_file = f"{PORT_FILE}.{os.getpid()}.tmp"
        with open(tmp_port_file, "w", encoding="ascii") as handle:
            handle.write(f"{bound_port}\n")
        os.chmod(tmp_port_file, 0o600)
        os.replace(tmp_port_file, PORT_FILE)
        print(f"Browser daemon listening on 127.0.0.1:{bound_port}")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.server_close()
        if bound_port is not None:
            try:
                with open(PORT_FILE, encoding="ascii") as handle:
                    recorded_port = int(handle.read().strip())
                if recorded_port == bound_port:
                    os.unlink(PORT_FILE)
            except (FileNotFoundError, OSError, ValueError):
                pass
        state.stop()


if __name__ == "__main__":
    run()
