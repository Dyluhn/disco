"""WO-TC4 — database-kit 1.0.0 LIVE proof (spec §7.1, §9 "Live (mandatory)").

Fixtures are regression only (standing rule) — this boots a REAL `node`
process that mounts the shipped, hash-pinned `core/db.js` + `core/health.js`
verbatim (via ``TrustedComponentRegistry.default().get("database-kit").
install_tree()``), then runs the REGISTRY's own probe (``probe_source()``,
D3: host-run, never installed) as a subprocess against it — proving the
`/__health/db` seam (spec §3 seam-ownership) actually works end-to-end, not
just that the fixture JSON parses.

Two scenarios:
  * the happy path: db opens, one app migration runs, the probe PASSES.
  * a negative: the REAL (hash-pinned) health handler is wired to a
    deliberately broken db handle — its own try/except fires a genuine 500,
    and the probe must report ``passed: False``.

Skips cleanly (not a failure) when no usable `node` >= 22 is on PATH — the
runtime contract requires node:sqlite, which is Node-22+-only.
"""

from __future__ import annotations

import json
import re
import select
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from disco.core.trusted_components.registry import TrustedComponentRegistry
from disco.core.trusted_components.verify import ProbeVerdict, parse_probe_stdout

_BOOT_TIMEOUT_S = 15.0
_HTTP_TIMEOUT_S = 10.0
_PROBE_SUBPROCESS_TIMEOUT_S = 30.0
_PORT_LINE_RE = re.compile(r"PORT (\d+)")


def _node_major_version(node: str) -> int | None:
    try:
        out = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=10, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"v(\d+)\.", out.stdout.strip())
    return int(m.group(1)) if m else None


def _skip_reason() -> str | None:
    node = shutil.which("node")
    if node is None:
        return "node not found on PATH"
    major = _node_major_version(node)
    if major is None or major < 22:
        return f"node >= 22 required for node:sqlite (found major={major!r})"
    return None


_SKIP_REASON = _skip_reason()
pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=str(_SKIP_REASON))


# ---- fixture server sources --------------------------------------------------

_SERVER_JS_OK = """\
import { createServer } from "node:http";
import { openDatabase, runMigrations } from "./src/trusted/database-kit/core/db.js";
import { healthHandler } from "./src/trusted/database-kit/core/health.js";

const db = openDatabase();
runMigrations(db, [
  { version: 1, sql: "CREATE TABLE notes(id INTEGER PRIMARY KEY, body TEXT NOT NULL)" },
]);
const health = healthHandler(db);

const server = createServer((req, res) => {
  if (req.url === "/__health/db") {
    health(req, res);
    return;
  }
  if (req.url === "/") {
    res.writeHead(200, { "Content-Type": "text/plain" });
    res.end("home");
    return;
  }
  res.writeHead(404);
  res.end();
});

server.listen(0, "127.0.0.1", () => {
  console.log(`PORT ${server.address().port}`);
});
"""

# Negative fixture: simulates "the migrations were killed" by wiring the REAL,
# hash-pinned healthHandler to a db handle whose exec()/prepare() always
# throw. This exercises health.js's own try/catch failure path (a genuine
# 500), rather than faking a 500 in fixture code — the strongest form of the
# negative proof.
_SERVER_JS_BROKEN = """\
import { createServer } from "node:http";
import { healthHandler } from "./src/trusted/database-kit/core/health.js";

const brokenDb = {
  exec() {
    throw new Error("db unavailable: migrations were removed");
  },
  prepare() {
    throw new Error("db unavailable: migrations were removed");
  },
};
const health = healthHandler(brokenDb);

const server = createServer((req, res) => {
  if (req.url === "/__health/db") {
    health(req, res);
    return;
  }
  if (req.url === "/") {
    res.writeHead(200, { "Content-Type": "text/plain" });
    res.end("home");
    return;
  }
  res.writeHead(404);
  res.end();
});

server.listen(0, "127.0.0.1", () => {
  console.log(`PORT ${server.address().port}`);
});
"""


def _loaded_database_kit():
    registry = TrustedComponentRegistry.default()
    comp = registry.get("database-kit")
    assert comp is not None, "database-kit is not shipped in the registry"
    return comp


def _write_workspace(tmp_path: Path, server_js: str) -> Path:
    """Materialize a minimal app workspace: the shipped install_tree() under
    src/trusted/database-kit/, a {"type":"module"} package.json, and the
    fixture server.js at the workspace root."""
    comp = _loaded_database_kit()
    workspace = tmp_path / "workspace"
    tree = comp.install_tree()
    assert set(tree) == {"core/db.js", "core/health.js", "config/db.config.json", "GUIDE.md"}
    for relpath, data in tree.items():
        target = workspace / "src" / "trusted" / "database-kit" / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (workspace / "package.json").write_text(
        json.dumps({"type": "module"}) + "\n", encoding="utf-8"
    )
    (workspace / "server.js").write_text(server_js, encoding="utf-8")
    return workspace


def _boot_node_server(workspace: Path) -> tuple[subprocess.Popen, int]:
    """Boot `node server.js` (cwd=workspace, so the component's default
    workspace-relative config path resolves INSIDE tmp_path) bound to a free
    port (port 0), and read the actual port back from stdout."""
    proc = subprocess.Popen(
        [shutil.which("node") or "node", "server.js"],
        cwd=str(workspace),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        port = _read_port(proc)
        _wait_http_ready(port)
    except BaseException:
        _kill(proc)
        raise
    return proc, port


def _read_port(proc: subprocess.Popen) -> int:
    deadline = time.monotonic() + _BOOT_TIMEOUT_S
    buf = ""
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            stderr = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(
                f"node server.js exited early (code={proc.returncode}); stdout={buf!r} "
                f"stderr:\n{stderr}"
            )
        remaining = max(0.0, deadline - time.monotonic())
        ready, _, _ = select.select([proc.stdout], [], [], min(0.5, remaining))
        if proc.stdout in ready:
            line = proc.stdout.readline()
            if not line:
                continue
            buf += line
            m = _PORT_LINE_RE.search(line)
            if m:
                return int(m.group(1))
    raise TimeoutError(f"node server.js did not print a PORT line within {_BOOT_TIMEOUT_S}s "
                        f"(stdout so far: {buf!r})")


def _wait_http_ready(port: int) -> None:
    """Poll `/` (always 200 in both fixture variants) until it answers, or
    _BOOT_TIMEOUT_S elapses — the PORT line means "bound", not "routing"."""
    deadline = time.monotonic() + _BOOT_TIMEOUT_S
    last_error: str = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
        time.sleep(0.1)
    raise TimeoutError(f"server on port {port} never answered '/' (last error: {last_error})")


def _kill(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()


def _run_probe(workspace: Path, base_url: str, tmp_path: Path) -> ProbeVerdict:
    comp = _loaded_database_kit()
    probe_source = comp.probe_source()
    assert probe_source is not None, "database-kit manifest declares no probe"
    probe_path = tmp_path / "probe.py"
    probe_path.write_bytes(probe_source)

    python3 = shutil.which("python3") or sys.executable
    result = subprocess.run(
        [python3, str(probe_path), "--base-url", base_url, "--workspace", str(workspace)],
        capture_output=True,
        text=True,
        timeout=_PROBE_SUBPROCESS_TIMEOUT_S,
    )
    assert result.returncode == 0, (
        f"probe.py exited {result.returncode} (spec: exit!=0 is an infra error); "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return parse_probe_stdout(result.stdout)


# ---- the live tests -----------------------------------------------------------


def test_database_kit_probe_passes_against_a_live_migrated_server(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path, _SERVER_JS_OK)
    proc, port = _boot_node_server(workspace)
    try:
        verdict = _run_probe(workspace, f"http://127.0.0.1:{port}", tmp_path)
    finally:
        _kill(proc)

    assert verdict.passed is True, verdict.model_dump()
    names = {c.name for c in verdict.checks}
    assert {
        "health_http_200",
        "health_body_is_json",
        "health_ok_true",
        "health_migration_version",
    } <= names
    assert all(c.passed for c in verdict.checks)

    # Sanity: the migration really ran and is queryable through the SAME db
    # file the server wrote to (proves openDatabase's relative-path + mkdir
    # behavior actually landed a real sqlite file, not just an in-memory stub).
    db_path = workspace / "data" / "app.db"
    assert db_path.is_file()


def test_database_kit_probe_fails_when_the_health_route_is_broken(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path, _SERVER_JS_BROKEN)
    proc, port = _boot_node_server(workspace)
    try:
        verdict = _run_probe(workspace, f"http://127.0.0.1:{port}", tmp_path)
    finally:
        _kill(proc)

    assert verdict.passed is False, verdict.model_dump()
    failing = {c.name for c in verdict.checks if not c.passed}
    assert "health_http_200" in failing
