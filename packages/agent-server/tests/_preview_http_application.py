"""Application used by sealed HTTP roundtrip tests; no Disco imports in the guest."""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Application(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self.reply(200, b'{"ready":true}')
            return
        cookie = self.headers.get("Cookie", "")
        status = 200 if "app_session=accepted" in cookie else 401
        body = json.dumps(
            {"cookie": cookie, "path": self.path, "count": self.server.count}
        ).encode()
        self.reply(status, body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path.startswith("/login"):
            self.server.count += 1
            self.reply(
                201,
                body,
                [
                    ("Set-Cookie", "app_session=accepted; HttpOnly; Path=/"),
                    ("Set-Cookie", "app_theme=dark; Path=/"),
                    ("Set-Cookie", "disco_session=forged; Path=/"),
                    ("Set-Cookie", "disco_preview_cap=forged; Path=/"),
                    ("Set-Cookie", "disco_path_preview_bad=forged; Path=/"),
                    ("Set-Cookie", "disco_local_preview_bad=forged; Path=/"),
                    ("X-App-Repeat", "one"),
                    ("X-App-Repeat", "two"),
                ],
            )
            return
        if "app_session=accepted" not in self.headers.get("Cookie", ""):
            self.reply(401, b"denied")
            return
        self.server.count += 1
        self.reply(200, body)

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", int(sys.argv[1])), Application)
    server.count = 0
    server.serve_forever()
