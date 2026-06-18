import base64
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

from playwright.sync_api import sync_playwright

# Configuration
PORT = 8901
WORKSPACE_ROOT = os.environ.get("DISCO_WORKSPACE", os.environ.get("PMX_WORKSPACE", "/workspace"))
SCREENSHOT_DIR = os.path.join(WORKSPACE_ROOT, ".pmx/screenshots")
MAX_CONSOLE = 200
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
        self.screenshot_seq = 0

    def start(self):
        self.playwright = sync_playwright().start()
        # gVisor is the isolation boundary, hence --no-sandbox is acceptable HERE only.
        self.browser = self.playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 800})
        self.page = self.context.new_page()
        self.page.on("console", self._add_console)
        self.page.on("pageerror", self._add_pageerror)

    def _add_console(self, msg):
        self.console_logs.append({"level": msg.type, "text": msg.text})
        if len(self.console_logs) > MAX_CONSOLE:
            self.console_logs.pop(0)

    def _add_pageerror(self, err):
        self.console_logs.append({"level": "error", "text": err.message})
        if len(self.console_logs) > MAX_CONSOLE:
            self.console_logs.pop(0)

    def clear_console(self):
        self.console_logs = []

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
                "elements": [],
                "text": "",
                "screenshot_path": None
            }
        else:
            return {"ok": False, "error": f"Unknown action: {action}"}

        # Common data for most actions
        if action != "console_view":
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
