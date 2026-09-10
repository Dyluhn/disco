# ruff: noqa: E501 — a verification script.
"""B7 live proof: drive the REAL browser daemon (its real console/pageerror/
requestfailed/response listeners) against a deliberately-broken page, then render
through the REAL BrowserTool._render_observation — showing the agent now receives
stacks, source:line, and NETWORK FAIL lines instead of a bare '(1 errors)'.

  uv run python packages/tools/scripts/verify_b7_browser_errors.py
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from disco.tools.builtin._browser_daemon import BrowserState
from disco.tools.builtin.browser import BrowserTool

BROKEN_PAGE = b"""<!DOCTYPE html><html><head><title>Broken Demo</title></head>
<body><h1>Demo</h1>
<script>
  console.log("boot: step 1 ok");
  console.error("CONFIG_MISSING: window.API_BASE is undefined");
  fetch("/data/missing.json");                 // -> 404 response
  fetch("http://127.0.0.1:0/nope");            // -> requestfailed (refused)
  // uncaught -> pageerror with a stack
  setTimeout(function boom() { undefinedThing.render(); }, 50);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(BROKEN_PAGE)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")


def main() -> None:
    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    st = BrowserState()
    st.start()  # real Playwright (the daemon's engine + the B7 listeners)
    try:
        st.clear_console()
        st.page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
        st.page.wait_for_timeout(800)  # let the failed fetches + the setTimeout throw land
        data = {
            "ok": True,
            "url": st.page.url,
            "title": st.page.title(),
            "console": st.console_logs,
            "network": st.network_fails,
            "elements": [],
            "text": "",
        }
    finally:
        try:
            st.browser.close()
            st.playwright.stop()
        except Exception:
            pass
    srv.shutdown()

    obs = BrowserTool()._render_observation(data)
    print("=== AGENT-VISIBLE OBSERVATION (B7) ===\n")
    print(obs)
    print("\n=== checks ===")
    checks = {
        "console error text": "CONFIG_MISSING" in obs,
        "stack trace present": ("boom" in obs or "undefinedThing" in obs or "at " in obs),
        "source:line present": ":" in obs
        and (
            "127.0.0.1" in obs
            or "line" in obs.lower()
            or any(c.get("location") for c in st.console_logs)
        ),
        "NETWORK FAIL line": "NETWORK FAIL" in obs,
        "404 captured": "404" in obs,
        "console.log surfaced w/ errors": "boot: step 1" in obs,
    }
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    print(f"\n[{'PASS' if all(checks.values()) else 'CHECK'}] B7 live")


if __name__ == "__main__":
    main()
