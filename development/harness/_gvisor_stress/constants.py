"""Frozen constants for the gVisor stress harness."""

from __future__ import annotations

import re

IMAGE = "disco-sandbox:base"


OWNER_ID = "gvisor-stress"


PER_EXEC_TIMEOUT_S = 5


PER_EXEC_OUTER_TIMEOUT_S = 30


CREATE_TIMEOUT_S = 90


HEALTHCHECK_TIMEOUT_S = 35


CLI_TIMEOUT_S = 5


LEAK_CHECK_TIMEOUT_S = 2


PROBE_TIMEOUT_S = 2


PROBE_INTERVAL_S = 0.10


MAX_EVENT_SAMPLES = 100


MAX_CONTAINERS = 128


MAX_EXECS_PER_CONTAINER = 1_000_000


MAX_CONCURRENCY_PER_CONTAINER = 64


MIN_DURATION_CAP_S = 10.0


DEATH_WORDS = re.compile(r"\b(?:dead|died)\b", re.IGNORECASE)


_ENGINE_API_HELPER = r"""
import http.client
import json
import socket
import sys
import urllib.parse

socket_path, operation, payload_json = sys.argv[1:4]
payload = json.loads(payload_json)

class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=10)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)

def request(method, target, accepted):
    connection = UnixHTTPConnection(socket_path)
    try:
        connection.request(method, target)
        response = connection.getresponse()
        body = response.read()
    except OSError as exc:
        print(f"Docker Engine socket error: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(4)
    finally:
        connection.close()
    if response.status not in accepted:
        text = body.decode("utf-8", "replace")
        print(f"Docker Engine HTTP {response.status}: {text}", file=sys.stderr)
        raise SystemExit(3)
    return body

if operation == "version":
    data = json.loads(request("GET", "/version", {200}))
    print(data.get("Version", ""))
elif operation == "inspect":
    container_id = urllib.parse.quote(payload["container_id"], safe="")
    data = json.loads(request("GET", f"/containers/{container_id}/json", {200}))
    print(json.dumps(data.get("State", {}), separators=(",", ":")))
elif operation == "ps":
    filters = json.dumps({"label": [payload["label"]]}, separators=(",", ":"))
    query = urllib.parse.urlencode({"all": "1", "filters": filters})
    data = json.loads(request("GET", f"/containers/json?{query}", {200}))
    for entry in data:
        names = entry.get("Names") or []
        name = (names[0] if names else "").lstrip("/")
        print(f"{entry.get('Id', '')}\t{name}\t{entry.get('Status', '')}")
elif operation == "volumes":
    filters = json.dumps({"label": [payload["label"]]}, separators=(",", ":"))
    query = urllib.parse.urlencode({"filters": filters})
    data = json.loads(request("GET", f"/volumes?{query}", {200}))
    for entry in data.get("Volumes") or []:
        print(f"{entry.get('Name', '')}\t{entry.get('Driver', '')}")
elif operation == "rm":
    for raw_id in payload["container_ids"]:
        container_id = urllib.parse.quote(raw_id, safe="")
        request("DELETE", f"/containers/{container_id}?force=1&v=1", {204, 404})
        print(raw_id)
elif operation == "rm_volumes":
    for raw_name in payload["volume_names"]:
        name = urllib.parse.quote(raw_name, safe="")
        request("DELETE", f"/volumes/{name}?force=1", {204, 404})
        print(raw_name)
else:
    print(f"unknown Engine helper operation: {operation}", file=sys.stderr)
    raise SystemExit(2)
"""


COMMANDS: tuple[tuple[str, str], ...] = (
    ("echo", "echo ok"),
    ("true", "true"),
    ("python", "python3 -c 'print(6 * 7)'"),
)
