import base64
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

from playwright.sync_api import sync_playwright

# _live_view is shipped alongside the daemon by browser.py as
# /workspace/.pmx/_live_view.py.  The sys.path insert makes it importable
# without a package install; the try/except keeps unit tests that don't
# have the sandbox image from failing on import.
sys.path.insert(0, "/workspace/.pmx")
try:
    import _live_view  # type: ignore[import]  # shipped alongside by browser.py
except ImportError:
    _live_view = None  # not available (unit tests / missing binary)

# Flag: has the browser been restarted in headed mode for live view?
_live_headed: bool = False

# Configuration
PORT = 8901
WORKSPACE_ROOT = os.environ.get("DISCO_WORKSPACE", os.environ.get("PMX_WORKSPACE", "/workspace"))
SCREENSHOT_DIR = os.path.join(WORKSPACE_ROOT, ".pmx/screenshots")
MAX_CONSOLE = 200
# B7: bound the captured network-failure ring the same way the console is bounded,
# so a page that hammers a dead endpoint can't grow capture without limit.
MAX_NETWORK = 100
MAX_TEXT = 4000
MAX_ELEMENTS = 120
# W6: click timeout cut from Playwright's 30s default to a few seconds so a
# div-based dock/button that is briefly un-clickable doesn't stall a full turn.
CLICK_TIMEOUT_MS = 3000

class BrowserState:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.console_logs = []
        # B7: failed/4xx-5xx network requests are otherwise invisible to the agent.
        self.network_fails = []
        self.screenshot_seq = 0

    def start(self, display: str | None = None):
        self.playwright = sync_playwright().start()
        # gVisor is the isolation boundary, hence --no-sandbox is acceptable HERE only.
        if display:
            import os as _os
            _os.environ["DISPLAY"] = display
            self.browser = self.playwright.chromium.launch(
                headless=False,
                args=["--no-sandbox", f"--display={display}"],
            )
        else:
            self.browser = self.playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 800})
        self.page = self.context.new_page()
        self.page.on("console", self._add_console)
        self.page.on("pageerror", self._add_pageerror)
        # B7: capture failed requests (DNS/connection/abort) and 4xx/5xx responses
        # so the agent can see *why* a page it is debugging is broken.
        self.page.on("requestfailed", self._add_request_failed)
        self.page.on("response", self._add_response)

    def _add_console(self, msg):
        # B7: keep msg.location ({url, lineNumber, columnNumber}) so the agent gets a
        # source:line, not just bare text. location is a property; guard defensively.
        entry = {"level": msg.type, "text": msg.text}
        location = getattr(msg, "location", None)
        if location:
            entry["location"] = location
        self.console_logs.append(entry)
        if len(self.console_logs) > MAX_CONSOLE:
            self.console_logs.pop(0)

    def _add_pageerror(self, err):
        # B7: keep err.stack (the most useful field for tracing a crash) alongside
        # the message instead of dropping it.
        entry = {"level": "error", "text": err.message}
        stack = getattr(err, "stack", None)
        if stack:
            entry["stack"] = stack
        self.console_logs.append(entry)
        if len(self.console_logs) > MAX_CONSOLE:
            self.console_logs.pop(0)

    def _add_request_failed(self, request):
        # B7: a request that never got a response (DNS, refused, aborted, timeout).
        # request.failure is the error text (or None on some engines).
        failure = getattr(request, "failure", None)
        self._record_network({
            "method": request.method,
            "url": request.url,
            "failure": failure or "failed",
        })

    def _add_response(self, response):
        # B7: a response that *did* arrive but with an error status (4xx/5xx).
        status = response.status
        if status >= 400:
            self._record_network({
                "method": response.request.method,
                "url": response.url,
                "status": status,
            })

    def _record_network(self, entry):
        self.network_fails.append(entry)
        if len(self.network_fails) > MAX_NETWORK:
            self.network_fails.pop(0)

    def clear_console(self):
        self.console_logs = []
        # B7: a fresh navigation starts a fresh network-failure ledger.
        self.network_fails = []

state = BrowserState()

class BrowserHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            if state.page:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")
            else:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"Not Ready")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "Invalid JSON"}, 400)
            return

        action = params.get("action")
        try:
            result = self._handle_action(action, params)
            self._send_json(result)
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

    def _handle_action(self, action, params):
        page = state.page
        if not page:
            return {"ok": False, "error": "Browser not initialized"}

        screenshot_path = None

        if action == "navigate":
            url = params.get("url")
            if not url:
                return {"ok": False, "error": "URL required for navigate"}
            state.clear_console()
            page.goto(url, wait_until="load")
            page.wait_for_timeout(500) # Settle time
        elif action == "screenshot":
            pass # Just take a screenshot at the end
        elif action == "click":
            # W6: click supports index (data-pmx-index attr), CSS selector, or
            # visible text — whichever the agent provides. Timeout cut to a few
            # seconds so a momentarily-unclickable element doesn't stall a turn.
            index = params.get("index")
            css_selector = params.get("selector", "")
            click_text = params.get("click_text", "")
            if index is not None:
                page.click(
                    f"[data-pmx-index='{index}']",
                    timeout=CLICK_TIMEOUT_MS,
                )
            elif css_selector:
                page.click(css_selector, timeout=CLICK_TIMEOUT_MS)
            elif click_text:
                # Playwright text= selector: finds element whose visible text
                # contains click_text (case-insensitive prefix match by default).
                page.click(f"text={click_text}", timeout=CLICK_TIMEOUT_MS)
            else:
                return {"ok": False, "error": "index, selector, or click_text required for click"}
            page.wait_for_timeout(500)
        elif action == "fill":
            index = params.get("index")
            text = params.get("text", "")
            if index is None:
                return {"ok": False, "error": "Index required for fill"}
            page.fill(f"[data-pmx-index='{index}']", text)
        elif action == "submit":
            index = params.get("index")
            if index is None:
                return {"ok": False, "error": "Index required for submit"}
            selector = f"[data-pmx-index='{index}']"
            element = page.query_selector(selector)
            if element:
                tag_name = element.evaluate("el => el.tagName.toLowerCase()")
                if tag_name == "input" or tag_name == "textarea":
                    page.focus(selector)
                    page.keyboard.press("Enter")
                else:
                    page.click(selector)
            page.wait_for_timeout(500)
        elif action == "back":
            page.go_back()
            page.wait_for_timeout(500)
        elif action == "console_view":
            return {
                "ok": True,
                "url": page.url,
                "title": page.title(),
                "console": state.console_logs,
                "network": state.network_fails,
                "elements": [],
                "text": "",
                "screenshot_path": None
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
                _live_headed = True
            return {"ok": True, "novnc_port": 6080, "display": ":1"}
        elif action == "live_touch":
            # Heartbeat from the frontend while the live view is open — refresh the idle
            # watchdog so an actively-watched session is not reaped after 600s.
            if _live_view is not None:
                _live_view.touch()
            return {"ok": True, "live": _live_view.is_live() if _live_view is not None else False}
        elif action == "live_stop":
            if _live_view is not None:
                _live_view.teardown()
            return {"ok": True}
        else:
            return {"ok": False, "error": f"Unknown action: {action}"}

        # Common data for most actions
        if action not in ("console_view", "live_start", "live_stop", "live_touch"):
            elements = self._get_elements(page)
            text = page.evaluate(
                "() => (document.body.innerText || document.body.textContent || '').trim()"
            ).strip()[:MAX_TEXT]

            state.screenshot_seq += 1
            os.makedirs(SCREENSHOT_DIR, exist_ok=True)
            screenshot_rel_path = f".pmx/screenshots/{state.screenshot_seq:04d}-{action}.png"
            screenshot_full_path = os.path.join(WORKSPACE_ROOT, screenshot_rel_path)
            page.screenshot(path=screenshot_full_path, full_page=params.get("full_page", False))
            screenshot_path = screenshot_rel_path

            res = {
                "ok": True,
                "url": page.url,
                "title": page.title(),
                "console": state.console_logs,
                "network": state.network_fails,
                "elements": elements,
                "text": text,
                "screenshot_path": screenshot_path,
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
    state.start()
    server = HTTPServer(('127.0.0.1', PORT), BrowserHandler)
    print(f"Browser daemon listening on 127.0.0.1:{PORT}")
    server.serve_forever()

if __name__ == "__main__":
    run()
