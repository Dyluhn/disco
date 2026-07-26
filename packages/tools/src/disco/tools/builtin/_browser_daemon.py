import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import signal
import sys
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

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
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
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


class BrowserRendererUnavailable(RuntimeError):
    """Chromium launched, but cannot produce trustworthy painted text evidence."""


def _text_renderer_ready(page) -> bool:
    """Calibrate ordinary system-font layout on a disposable browser page."""
    try:
        page.set_content(
            '<!doctype html><span id="disco-render-probe" '
            'style="font:16px sans-serif">Disco renderer probe</span>'
        )
        result = page.evaluate("""
            () => {
                const element = document.getElementById('disco-render-probe');
                const node = element && element.firstChild;
                if (!element || !node || !(element.innerText || '').trim()) return false;
                const range = document.createRange();
                range.selectNodeContents(node);
                return Array.from(range.getClientRects()).some(
                    rect => rect.width > 0 && rect.height > 0
                );
            }
        """)
    except Exception:
        return False
    return result is True


def _is_token(value) -> bool:
    return isinstance(value, str) and _TOKEN_RE.fullmatch(value) is not None


def _positive_epoch(value) -> bool:
    return type(value) is int and value > 0


def _page_kind(url) -> str:
    value = str(url or "")
    if not value or value == "about:blank":
        return "uninitialized"
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
    except ValueError:
        return "external"
    # Reload authority is intentionally narrower than URL reachability: only a
    # literal HTTP(S) loopback origin can represent the local preview. A
    # loopback-looking file/custom/javascript URL is still external.
    if parsed.scheme.lower() not in {"http", "https"} or host is None:
        return "external"
    if host.lower() == "localhost":
        return "local_preview"
    try:
        return "local_preview" if ipaddress.ip_address(host).is_loopback else "external"
    except ValueError:
        # Literal host parsing only. Never use DNS to enlarge the local trust class.
        return "external"


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
        self._recent_nonce_order = deque()
        self._recent_nonces = set()
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

    def ensure_lane_page(self, lane_name):
        lane = self.lane(lane_name)
        if lane.page is None:
            if lane_name == "host_verifier":
                if self.host_context is None:
                    self.host_context = self._new_context()
                context = self.host_context
            else:
                context = self.context
            if context is None:
                raise RuntimeError("Browser context not initialized")
            self._bind_page(lane_name, context.new_page())
        return lane

    def close_lane(self, lane_name):
        if lane_name != "host_verifier":
            raise ValueError("only the host verifier lane may be closed independently")
        lane = self.lane(lane_name)
        page, lane.page = lane.page, None
        context, self.host_context = self.host_context, None
        errors = []
        if page is not None:
            try:
                page.close()
            except Exception as exc:
                errors.append(exc)
        if context is not None:
            try:
                context.close()
            except Exception as exc:
                errors.append(exc)
        lane.console_logs = []
        lane.network_fails = []
        lane.reset_freshness()
        if errors:
            raise errors[0]

    def reset_lane_for_generation(self, lane_name, generation):
        lane = self.ensure_lane_page(lane_name)
        if lane.executor_generation in {None, generation}:
            lane.executor_generation = generation
            return lane
        # A new executor generation cannot inherit page/history/freshness from
        # its predecessor. Recreate only this fixed lane; the other lane remains
        # untouched. The host lane rotates its entire isolated context so no
        # origin state survives between verifier generations.
        if lane_name == "host_verifier":
            self.close_lane("host_verifier")
            lane = self.ensure_lane_page("host_verifier")
            lane.executor_generation = generation
            return lane
        old_page, lane.page = lane.page, None
        if old_page is not None:
            old_page.close()
        lane.console_logs = []
        lane.network_fails = []
        lane.reset_freshness()
        lane.executor_generation = generation
        return self.ensure_lane_page(lane_name)

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

    def accept_nonce(self, nonce):
        if nonce in self._recent_nonces:
            return False
        self._recent_nonces.add(nonce)
        self._recent_nonce_order.append(nonce)
        while len(self._recent_nonce_order) > _RECENT_NONCE_LIMIT:
            expired = self._recent_nonce_order.popleft()
            self._recent_nonces.discard(expired)
        return True

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
        self._recent_nonce_order.clear()
        self._recent_nonces.clear()
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
        self.clear_lane_diagnostics("agent")

    def clear_lane_diagnostics(self, lane_name):
        lane = self.lane(lane_name)
        lane.console_logs = []
        # B7: a fresh navigation starts a fresh network-failure ledger.
        lane.network_fails = []
        # Main-document response metadata must be rebound by that fresh
        # navigation; never carry a previous document's content type forward.
        lane.document_content_type = ""


state = BrowserState()


# DOM-analysis helpers, extracted from BrowserHandler. They read no instance
# state -- they take a page and return what a human would actually see -- so
# they were module functions living in a class, and that pushed the handler
# over its size budget.


def _visible_dom_text(page) -> str:
    """Return bounded exact text from rendered, non-hidden DOM text nodes.

    ``innerText`` is the right user-facing summary, but browsers apply CSS
    ``text-transform`` to it. Exact content claims also need the authored
    node text, coupled to browser-owned visibility and geometry rather than
    raw HTML/source bytes. Hidden, transparent, clipped, zero-area, and
    ``aria-hidden`` nodes are excluded.
    """
    try:
        value = page.evaluate(r"""
            () => {
                const output = [];
                const transparent = value => value === 'transparent' ||
                    /^rgba\(.*,[ ]*0(?:\.0+)?\)$/.test(value);
                // A transparent fill over a background CLIPPED TO TEXT is
                // the gradient-heading idiom: the glyphs are painted by the
                // background and ARE visible. Transparent fill WITHOUT that
                // clip is still cloaked copy and stays excluded.
                const paintsTextViaBackground = style =>
                    (style.webkitBackgroundClip === 'text' ||
                     style.backgroundClip === 'text') &&
                    (style.backgroundImage || 'none') !== 'none';
                const hiddenText = style =>
                    (transparent(style.color) ||
                     transparent(style.webkitTextFillColor || '')) &&
                    !paintsTextViaBackground(style);
                const intersects = (first, second) =>
                    Math.min(first.right, second.right) >
                        Math.max(first.left, second.left) &&
                    Math.min(first.bottom, second.bottom) >
                        Math.max(first.top, second.top);
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT
                );
                while (walker.nextNode()) {
                    const node = walker.currentNode;
                    const raw = (node.nodeValue || '').replace(/\s+/g, ' ').trim();
                    if (!raw) continue;
                    const parent = node.parentElement;
                    if (!parent || parent.closest('[aria-hidden="true"]')) continue;

                    let visible = true;
                    const clippingAncestors = [];
                    for (let current = parent; current; current = current.parentElement) {
                        const style = window.getComputedStyle(current);
                        if (style.display === 'none' ||
                            style.visibility === 'hidden' ||
                            style.visibility === 'collapse' ||
                            Number(style.opacity) === 0 ||
                            hiddenText(style) ||
                            style.clipPath === 'inset(100%)') {
                            visible = false;
                            break;
                        }
                        if (/(hidden|clip|scroll|auto)/.test(
                            `${style.overflow} ${style.overflowX} ${style.overflowY}`
                        )) {
                            clippingAncestors.push(current.getBoundingClientRect());
                        }
                    }
                    if (!visible) continue;

                    const range = document.createRange();
                    range.selectNodeContents(node);
                    const rendered = Array.from(range.getClientRects()).some(rect => {
                        if (rect.width <= 0 || rect.height <= 0) return false;
                        return clippingAncestors.every(clip => intersects(rect, clip));
                    });
                    if (rendered) output.push(raw);
                }
                return output.join('\n').slice(0, 4000);
            }
        """)
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _count_visible_semantic_elements(page) -> int:
    """Count rendered, content-bearing DOM semantics.

    The interactive element walker deliberately ignores ordinary headings and
    paragraphs.  Without a separate signal, a valid small page such as
    ``<h1>Live Server Up</h1>`` is indistinguishable from an empty SPA shell to
    the finish and verification gates.  Keep this signal deliberately narrow:
    only headings count.  Short paragraphs/list items can be transient loading
    placeholders, and unpainted canvas or broken media must not manufacture a
    pass.  Hidden, zero-area, and off-viewport headings do not count, and a
    heading must contain non-whitespace text.
    """
    try:
        count = page.evaluate(r"""
            () => {
                const viewportWidth = window.innerWidth ||
                    document.documentElement.clientWidth;
                const viewportHeight = window.innerHeight ||
                    document.documentElement.clientHeight;
                const transparent = value => value === 'transparent' ||
                    /^rgba\(.*,[ ]*0(?:\.0+)?\)$/.test(value);
                // A transparent fill over a background CLIPPED TO TEXT is
                // the gradient-heading idiom: the glyphs are painted by the
                // background and ARE visible. Transparent fill WITHOUT that
                // clip is still cloaked copy and stays excluded.
                const paintsTextViaBackground = style =>
                    (style.webkitBackgroundClip === 'text' ||
                     style.backgroundClip === 'text') &&
                    (style.backgroundImage || 'none') !== 'none';
                const hiddenText = style =>
                    (transparent(style.color) ||
                     transparent(style.webkitTextFillColor || '')) &&
                    !paintsTextViaBackground(style);
                const intersect = (box, left, top, right, bottom) => ({
                    left: Math.max(box.left, left),
                    top: Math.max(box.top, top),
                    right: Math.min(box.right, right),
                    bottom: Math.min(box.bottom, bottom),
                });
                return Array.from(document.querySelectorAll(
                    'h1, h2, h3, h4, h5, h6'
                )).filter(el => {
                    if (typeof el.checkVisibility === 'function' &&
                        !el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) {
                        return false;
                    }
                    const style = window.getComputedStyle(el);
                    if (style.visibility === 'hidden' || style.display === 'none' ||
                        style.pointerEvents === 'none' || Number(style.opacity) === 0 ||
                        hiddenText(style)) {
                        return false;
                    }

                    // A DOM box can exist while all of its text is clipped. Walk
                    // actual text ranges, then intersect each range with the
                    // viewport and every clipping ancestor.
                    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                    const textNodes = [];
                    while (walker.nextNode()) {
                        if ((walker.currentNode.textContent || '').trim()) {
                            textNodes.push(walker.currentNode);
                        }
                    }
                    return textNodes.some(node => {
                        const textParent = node.parentElement || el;
                        if (typeof textParent.checkVisibility === 'function' &&
                            !textParent.checkVisibility({
                                checkOpacity: true, checkVisibilityCSS: true
                            })) {
                            return false;
                        }
                        const nodeStyle = window.getComputedStyle(textParent);
                        if (nodeStyle.visibility === 'hidden' ||
                            nodeStyle.display === 'none' ||
                            nodeStyle.pointerEvents === 'none' ||
                            Number(nodeStyle.opacity) === 0 ||
                            hiddenText(nodeStyle)) {
                            return false;
                        }
                        const range = document.createRange();
                        range.selectNodeContents(node);
                        return Array.from(range.getClientRects()).some(rect => {
                            if (rect.width <= 0 || rect.height <= 0) return false;
                            let box = intersect(
                                rect, 0, 0, viewportWidth, viewportHeight
                            );
                            for (let ancestor = textParent; ancestor;
                                 ancestor = ancestor.parentElement) {
                                const ancestorStyle = window.getComputedStyle(ancestor);
                                if (ancestorStyle.clipPath !== 'none' ||
                                    ancestorStyle.clip !== 'auto') {
                                    return false; // clipping geometry is not safely inferable
                                }
                                const ancestorRect = ancestor.getBoundingClientRect();
                                if (ancestorStyle.overflowX !== 'visible') {
                                    box.left = Math.max(box.left, ancestorRect.left);
                                    box.right = Math.min(box.right, ancestorRect.right);
                                }
                                if (ancestorStyle.overflowY !== 'visible') {
                                    box.top = Math.max(box.top, ancestorRect.top);
                                    box.bottom = Math.min(box.bottom, ancestorRect.bottom);
                                }
                            }
                            const width = box.right - box.left;
                            const height = box.bottom - box.top;
                            const visibleRatio = (width * height) / (rect.width * rect.height);
                            if (width < 2 || height < 2 || visibleRatio < 0.25) return false;

                            // Geometry alone cannot prove the text is not fully
                            // covered by another element. At least one sampled
                            // point must resolve to the text parent or one of its
                            // ancestors (never an opaque covering descendant).
                            const points = [
                                [0.5, 0.5], [0.25, 0.25], [0.75, 0.25],
                                [0.25, 0.75], [0.75, 0.75],
                            ];
                            return points.some(([x, y]) => {
                                const hit = document.elementFromPoint(
                                    box.left + width * x, box.top + height * y
                                );
                                return hit === textParent ||
                                    (hit !== null && hit.contains(textParent));
                            });
                        });
                    });
                }).length;
            }
        """)
    except Exception:
        return 0
    # JavaScript data is untrusted.  In particular, bool is an int subclass in
    # Python and must not become a fabricated positive count.
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return 0
    return count


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
        if not state.accept_nonce(nonce):
            return None, None, None, None, "replayed request nonce"
        return lane_name, generation, nonce, epoch, None

    @staticmethod
    def _freshness(lane_name, generation, nonce, requested_epoch, sync_performed):
        lane = state.lane(lane_name)
        return {
            "schema_version": 1,
            "daemon_instance_id": DAEMON_INSTANCE_ID,
            "executor_generation": generation,
            "lane": lane_name,
            "request_nonce": nonce,
            "requested_epoch": requested_epoch,
            "synchronized_epoch": lane.synchronized_epoch,
            "sync_performed": sync_performed,
            "page_kind": _page_kind(getattr(lane.page, "url", "")),
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
        if not state.render_ready:
            return self._error(
                "browser_daemon_unavailable",
                "renderer_unavailable",
                state.render_error or "Browser renderer not initialized",
            )
        if action == "_close_lane":
            if lane_name != "host_verifier":
                return self._error(
                    "freshness_protocol_invalid",
                    "invalid_lane_close",
                    "the agent browser lane cannot be closed by this operation",
                    self._freshness(lane_name, generation, nonce, requested_epoch, False),
                )
            state.close_lane("host_verifier")
            return {
                "ok": True,
                "freshness": self._freshness(lane_name, generation, nonce, requested_epoch, False),
            }

        lane = state.reset_lane_for_generation(lane_name, generation)
        page = lane.page
        if page is None:
            return self._error(
                "browser_daemon_unavailable",
                "renderer_unavailable",
                "Browser renderer not initialized",
            )
        if lane.synchronized_epoch is not None and (
            requested_epoch is None or requested_epoch < lane.synchronized_epoch
        ):
            return self._error(
                "freshness_protocol_invalid",
                "epoch_regression",
                "workspace epoch regressed",
                self._freshness(lane_name, generation, nonce, requested_epoch, False),
            )

        vw = params.get("viewport_width")
        vh = params.get("viewport_height")
        if type(vw) is int and type(vh) is int:
            page.set_viewport_size({"width": vw, "height": vh})

        screenshot_path = None
        sync_performed = False

        pending_epoch = requested_epoch is not None and requested_epoch != lane.synchronized_epoch
        if action in _SYNC_ACTIONS:
            kind = _page_kind(getattr(page, "url", ""))
            if kind == "uninitialized":
                return self._error(
                    "browser_session_uninitialized",
                    "missing_loaded_page",
                    "browser page has no loaded document; navigate before this action",
                    self._freshness(lane_name, generation, nonce, requested_epoch, False),
                )
        if action in _SYNC_ACTIONS and pending_epoch:
            kind = _page_kind(getattr(page, "url", ""))
            if kind == "local_preview":
                try:
                    state.clear_lane_diagnostics(lane_name)
                    page.reload(wait_until="load")
                    page.wait_for_timeout(500)
                except Exception:
                    return self._error(
                        "freshness_sync_failed",
                        "reload_failed",
                        "local preview could not be synchronized",
                        self._freshness(lane_name, generation, nonce, requested_epoch, False),
                    )
                # Acknowledge immediately after reload. A later action failure
                # must not cause the next recovery action to reload again.
                lane.synchronized_epoch = requested_epoch
                sync_performed = True
            else:
                # Workspace bytes cannot affect a literal non-loopback page.
                # Acknowledge without reloading it so external browsing state is
                # never destroyed by local mutations.
                lane.synchronized_epoch = requested_epoch

        if action == "navigate":
            url = params.get("url")
            if not url:
                return self._error(
                    "browser_action_failed",
                    "navigation_failed",
                    "URL required for navigate",
                    self._freshness(lane_name, generation, nonce, requested_epoch, False),
                )
            try:
                state.clear_lane_diagnostics(lane_name)
                page.goto(url, wait_until="load")
                page.wait_for_timeout(500)  # Settle time
                lane.synchronized_epoch = requested_epoch
            except Exception:
                return self._error(
                    "browser_action_failed",
                    "navigation_failed",
                    "browser navigation failed",
                    self._freshness(lane_name, generation, nonce, requested_epoch, False),
                )
        elif action == "screenshot":
            pass  # Just take a screenshot at the end
        elif action == "click":
            freshness = self._freshness(
                lane_name, generation, nonce, requested_epoch, sync_performed
            )
            locator, error = self._action_locator(page, self._selector_for(params), freshness)
            if error is not None:
                return error
            assert locator is not None
            try:
                locator.click(timeout=CLICK_TIMEOUT_MS)
                page.wait_for_timeout(500)
            except Exception:
                return self._error(
                    "browser_action_failed",
                    "interaction_blocked",
                    "browser click could not be completed",
                    freshness,
                )
        elif action == "press":
            key = params.get("key", "")
            if not key:
                return self._error(
                    "browser_action_failed",
                    "interaction_blocked",
                    "key required for press",
                    self._freshness(lane_name, generation, nonce, requested_epoch, sync_performed),
                )
            try:
                page.keyboard.press(key)
                page.wait_for_timeout(500)
            except Exception:
                return self._error(
                    "browser_action_failed",
                    "interaction_blocked",
                    "browser key press could not be completed",
                    self._freshness(lane_name, generation, nonce, requested_epoch, sync_performed),
                )
        elif action == "fill":
            index = params.get("index")
            if index is None:
                return self._error(
                    "browser_action_failed",
                    "selector_not_found",
                    "Index required for fill",
                    self._freshness(lane_name, generation, nonce, requested_epoch, sync_performed),
                )
            freshness = self._freshness(
                lane_name, generation, nonce, requested_epoch, sync_performed
            )
            locator, error = self._action_locator(page, self._selector_for(params), freshness)
            if error is not None:
                return error
            assert locator is not None
            try:
                locator.fill(params.get("text", ""))
            except Exception:
                return self._error(
                    "browser_action_failed",
                    "interaction_blocked",
                    "browser fill could not be completed",
                    freshness,
                )
        elif action == "submit":
            index = params.get("index")
            if index is None:
                return self._error(
                    "browser_action_failed",
                    "selector_not_found",
                    "Index required for submit",
                    self._freshness(lane_name, generation, nonce, requested_epoch, sync_performed),
                )
            freshness = self._freshness(
                lane_name, generation, nonce, requested_epoch, sync_performed
            )
            locator, error = self._action_locator(page, self._selector_for(params), freshness)
            if error is not None:
                return error
            assert locator is not None
            try:
                tag_name = locator.evaluate("el => el.tagName.toLowerCase()")
                if tag_name == "input" or tag_name == "textarea":
                    locator.focus()
                    page.keyboard.press("Enter")
                else:
                    locator.click(timeout=CLICK_TIMEOUT_MS)
                page.wait_for_timeout(500)
            except Exception:
                return self._error(
                    "browser_action_failed",
                    "interaction_blocked",
                    "browser submit could not be completed",
                    freshness,
                )
        elif action == "back":
            try:
                page.go_back()
                page.wait_for_timeout(500)
                # History may restore an old cached local document. Reload that
                # destination once so a successful back cannot fabricate same-
                # epoch freshness.
                if pending_epoch and _page_kind(page.url) == "local_preview":
                    state.clear_lane_diagnostics(lane_name)
                    page.reload(wait_until="load")
                    page.wait_for_timeout(500)
                    sync_performed = True
                lane.synchronized_epoch = requested_epoch
            except Exception:
                return self._error(
                    "browser_action_failed",
                    "navigation_failed",
                    "browser history navigation failed",
                    self._freshness(lane_name, generation, nonce, requested_epoch, sync_performed),
                )
        elif action == "console_view":
            return {
                "ok": True,
                "url": page.url,
                "title": page.title(),
                "console": lane.console_logs,
                "network": lane.network_fails,
                "elements": [],
                "text": "",
                "screenshot_path": None,
                "freshness": self._freshness(
                    lane_name, generation, nonce, requested_epoch, sync_performed
                ),
            }
        elif action == "live_start":
            global _live_headed
            if _live_view is None:
                return {"ok": False, "error": "live_view module not available"}
            # Ensure Xvfb + VNC stack is up
            ok = _live_view.ensure_live()
            if not ok:
                return {"ok": False, "error": "Failed to start live view stack"}
            # If browser is headless, restart it headed on :1
            if not _live_headed:
                if state.browser is not None:
                    try:
                        state.browser.close()
                    except Exception:
                        pass
                if state.playwright is not None:
                    try:
                        state.playwright.stop()
                    except Exception:
                        pass
                state.start(display=":1")
                lane = state.reset_lane_for_generation(lane_name, generation)
                page = lane.page
                _live_headed = True
            return {
                "ok": True,
                "novnc_port": 6080,
                "display": ":1",
                "freshness": self._freshness(
                    lane_name, generation, nonce, requested_epoch, sync_performed
                ),
            }
        elif action == "live_touch":
            # Heartbeat from the frontend while the live view is open — refresh the idle
            # watchdog so an actively-watched session is not reaped after 600s.
            if _live_view is not None:
                _live_view.touch()
            return {
                "ok": True,
                "live": _live_view.is_live() if _live_view is not None else False,
                "freshness": self._freshness(
                    lane_name, generation, nonce, requested_epoch, sync_performed
                ),
            }
        elif action == "live_stop":
            if _live_view is not None:
                _live_view.teardown()
            return {
                "ok": True,
                "freshness": self._freshness(
                    lane_name, generation, nonce, requested_epoch, sync_performed
                ),
            }
        else:
            return self._error(
                "browser_action_failed",
                "unknown_action",
                "Unknown browser action",
                self._freshness(lane_name, generation, nonce, requested_epoch, sync_performed),
            )

        # Common data for most actions
        if action not in ("console_view", "live_start", "live_stop", "live_touch"):
            # B-F: capture-before-render race — read elements + text, but retry up to
            # CAPTURE_RETRY_MAX times until the page has SOMETHING (text or elements),
            # so a late SPA hydration (mount > the fixed settle) is not returned as a
            # permanent empty observation. ~CAPTURE_RETRY_MAX * INTERVAL worst case.
            elements = []
            text = ""
            for _attempt in range(CAPTURE_RETRY_MAX):
                elements = self._get_elements(page)
                text = page.evaluate(
                    "() => (document.body.innerText || document.body.textContent || '').trim()"
                ).strip()[:MAX_TEXT]
                if text or elements:
                    break
                # Don't sleep after the final read — a genuinely-blank page returns
                # empty immediately at the cap rather than burning a trailing wait.
                if _attempt < CAPTURE_RETRY_MAX - 1:
                    page.wait_for_timeout(CAPTURE_RETRY_INTERVAL_MS)

            state.screenshot_seq += 1
            os.makedirs(SCREENSHOT_DIR, exist_ok=True)
            screenshot_rel_path = f".pmx/screenshots/{state.screenshot_seq:04d}-{action}.png"
            screenshot_full_path = os.path.join(WORKSPACE_ROOT, screenshot_rel_path)
            page.screenshot(path=screenshot_full_path, full_page=params.get("full_page", False))
            screenshot_path = screenshot_rel_path

            # AppKit EPIC G: surface the data-appkit-section markers present in the
            # RENDERED DOM so verify_appkit_app's section_coverage check can prove each
            # AppSpec section actually mounted. Additive + harmless to normal builds
            # (an app with no markers returns []). Port-scope miss found live 2026-07-03:
            # the verifier read `appkit_sections` but nothing ever emitted it here.
            try:
                appkit_sections = page.evaluate(
                    "() => Array.from(document.querySelectorAll('[data-appkit-section]'))"
                    ".map(e => e.getAttribute('data-appkit-section'))"
                )
            except Exception:
                appkit_sections = []
            try:
                canvas_count = page.evaluate("() => document.querySelectorAll('canvas').length")
            except Exception:
                canvas_count = 0
            visible_semantic_elements = _count_visible_semantic_elements(page)
            visible_dom_text = _visible_dom_text(page)

            res = {
                "ok": True,
                "url": page.url,
                "title": page.title(),
                "console": lane.console_logs,
                "network": lane.network_fails,
                "elements": elements,
                "text": text,
                "appkit_sections": appkit_sections,
                "canvas_count": canvas_count,
                # Exact authored text from rendered, non-hidden text nodes.
                # Unlike innerText, this is not rewritten by CSS text-transform,
                # so exact-content claims retain their case while still requiring
                # structured browser visibility/geometry evidence.
                "visible_dom_text": visible_dom_text,
                # Captured from the Playwright main-document Response event, not
                # from page JavaScript (which can shadow document.contentType).
                "document_content_type": lane.document_content_type,
                "visible_semantic_elements": visible_semantic_elements,
                "screenshot_path": screenshot_path,
                "freshness": self._freshness(
                    lane_name, generation, nonce, requested_epoch, sync_performed
                ),
            }

            if params.get("include_screenshot_b64"):
                with open(screenshot_full_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                res["screenshot_b64"] = b64

            return res

    def _get_elements(self, page):
        # W6: broaden the element walker beyond standard interactive elements.
        # We now also index:
        #   - <div>, <span>, <li> elements that have onclick handlers (already
        #     covered by [onclick] but this ensures we capture them with the
        #     extended selector set)
        #   - elements whose computed CSS cursor is 'pointer' (the idiomatic
        #     signal for "this is clickable" in modern SPAs and design-system
        #     components that use divs as buttons)
        # The walker assigns data-pmx-index to each found element so the
        # click action can reach it by index without knowing the CSS path.
        return page.evaluate(f"""
            () => {{
                // Standard interactive elements — always indexed.
                const standardSelectors = [
                    'a', 'button', 'input', 'select', 'textarea',
                    '[role="button"]', '[onclick]'
                ];
                const standardSet = new Set(
                    Array.from(document.querySelectorAll(standardSelectors.join(',')))
                );

                // Extended: block/inline elements with cursor:pointer that aren't
                // already covered by the standard set.
                const extendedTags = ['div', 'span', 'li', 'td', 'th', 'label',
                                      'article', 'section', 'header', 'nav', 'aside'];
                const pointerCandidates = Array.from(
                    document.querySelectorAll(extendedTags.join(','))
                ).filter(el => {{
                    if (standardSet.has(el)) return false;  // already in standard set
                    return window.getComputedStyle(el).cursor === 'pointer';
                }});

                // Union: standard first (preserve ordering), then pointer extras.
                const allElements = [
                    ...Array.from(standardSet),
                    ...pointerCandidates,
                ]
                .filter(el => {{
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0 &&
                           window.getComputedStyle(el).visibility !== 'hidden' &&
                           window.getComputedStyle(el).display !== 'none';
                }})
                .slice(0, {MAX_ELEMENTS});

                return allElements.map((el, i) => {{
                    const index = i + 1;
                    el.setAttribute('data-pmx-index', index.toString());
                    const tag = el.tagName.toLowerCase();
                    let text = (el.innerText || el.textContent || el.value ||
                                el.placeholder || "").trim().replace(/\\n/g, " ");
                    if (text.length > 80) text = text.substring(0, 77) + "...";
                    return `${{index}}[:] <${{tag}}>${{text}}</${{tag}}>`;
                }});
            }}
        """)


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
