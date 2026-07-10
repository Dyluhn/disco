"""WO-TC4 — auth-kit 1.0.0 LIVE proof (spec §7.2, §9 "Live (mandatory)").

Fixtures are regression only (standing rule) — this boots a REAL `node`
process that mounts the shipped, hash-pinned auth-kit core (via
``TrustedComponentRegistry.default().get("auth-kit").install_tree()``) on top
of a REAL database-kit database, then runs the REGISTRY's own probe
(``probe_source()``, D3: host-run, never installed) as a subprocess against
it — proving the seam-ownership rule (spec §3) actually holds end-to-end:
protection is opt-OUT via `publicAllowlist`, never opt-in per route.

Two scenarios:
  * the happy path: the app handler is wrapped with `createAuthApp` — the
    probe's unauth/login/session/logout checks all PASS.
  * a negative: the SAME app handler is mounted directly on `node:http`,
    WITHOUT `createAuthApp` — nothing guards `/notes`, and the probe's
    `unauth_401` check (a random path outside the allowlist must 401) must
    fail, so the overall verdict is `passed: False`.

Skips cleanly (not a failure) when no usable `node` >= 22 is on PATH (the
runtime contract requires node:sqlite, Node-22+-only), AND when database-kit
is not yet shipped in the registry (it is being authored in parallel; this
skip auto-vanishes once it lands — see disco.core.trusted_components.registry
TrustedComponentRegistry.default()).
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
    if TrustedComponentRegistry.default().get("database-kit") is None:
        return "database-kit is not yet shipped in the registry (parallel WO-TC4 lane)"
    return None


_SKIP_REASON = _skip_reason()
pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=str(_SKIP_REASON))


# ---- fixture server sources --------------------------------------------------

# The app handler both fixtures share: "/" is a public home page, "/notes" is
# a per-user page that must be behind auth. Defined identically in both
# variants so the ONLY difference between the two scenarios is whether
# createAuthApp wraps it — that is exactly the thing the probe must detect.
_APP_HANDLER_JS = """\
function appHandler(req, res) {
  const pathname = new URL(req.url, "http://localhost").pathname;
  if (pathname === "/") {
    res.writeHead(200, { "Content-Type": "text/plain" });
    res.end("home");
    return;
  }
  if (pathname === "/notes") {
    res.writeHead(200, { "Content-Type": "text/plain" });
    res.end("secret notes");
    return;
  }
  res.writeHead(404, { "Content-Type": "text/plain" });
  res.end("not found");
}
"""

# Happy path: database-kit opens/migrates a real sqlite db, auth-kit's own
# migrations run through the SAME runMigrations() the app would use, the dev
# seed user is created, and the handler above is mounted through
# createAuthApp — the seam owns login/logout/session AND guards "/notes".
_SERVER_JS_WRAPPED = (
    """\
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { openDatabase, runMigrations } from "./src/trusted/database-kit/core/db.js";
import { AUTH_MIGRATIONS } from "./src/trusted/auth-kit/core/schema.js";
import { seedDevUser } from "./src/trusted/auth-kit/core/auth.js";
import { createAuthApp } from "./src/trusted/auth-kit/core/middleware.js";

const config = JSON.parse(
  readFileSync("./src/trusted/auth-kit/config/auth.config.json", "utf8")
);

const db = openDatabase(); // database-kit's own default config path
runMigrations(db, AUTH_MIGRATIONS);
seedDevUser(db, config);

"""
    + _APP_HANDLER_JS
    + """
const app = createAuthApp({ db, config, handler: appHandler });
const server = createServer(app);
server.listen(0, "127.0.0.1", () => {
  console.log(`PORT ${server.address().port}`);
});
"""
)

# Negative: the identical handler mounted DIRECTLY on node:http — no
# createAuthApp, so nothing owns the seam. "/notes" is wide open and the
# unauth_401 probe check (a random, definitely-not-allowlisted path) must
# fail because there is no guard to produce the 401 at all.
_SERVER_JS_BARE = (
    """\
import { createServer } from "node:http";

"""
    + _APP_HANDLER_JS
    + """
const server = createServer(appHandler);
server.listen(0, "127.0.0.1", () => {
  console.log(`PORT ${server.address().port}`);
});
"""
)


def _loaded(name: str):
    comp = TrustedComponentRegistry.default().get(name)
    assert comp is not None, f"{name} is not shipped in the registry"
    return comp


def _write_workspace(tmp_path: Path, server_js: str) -> Path:
    """Materialize a minimal app workspace: BOTH kits' shipped install_tree()
    under src/trusted/<name>/, a {"type":"module"} package.json, and the
    fixture server.js at the workspace root."""
    workspace = tmp_path / "workspace"
    for name in ("database-kit", "auth-kit"):
        comp = _loaded(name)
        for relpath, data in comp.install_tree().items():
            target = workspace / "src" / "trusted" / name / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    (workspace / "package.json").write_text(
        json.dumps({"type": "module"}) + "\n", encoding="utf-8"
    )
    (workspace / "server.js").write_text(server_js, encoding="utf-8")
    return workspace


def _boot_node_server(workspace: Path) -> tuple[subprocess.Popen, int]:
    """Boot `node server.js` (cwd=workspace, so both kits' workspace-relative
    default config paths resolve INSIDE tmp_path) bound to a free port (port
    0), and read the actual port back from stdout. Node v22 prints an
    experimental warning for node:sqlite on stderr — that is expected noise,
    never treated as failure."""
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
    raise TimeoutError(
        f"node server.js did not print a PORT line within {_BOOT_TIMEOUT_S}s "
        f"(stdout so far: {buf!r})"
    )


def _wait_http_ready(port: int) -> None:
    """Poll `/` (200 in both fixture variants) until it answers, or
    _BOOT_TIMEOUT_S elapses — the PORT line means "bound", not "routing"."""
    deadline = time.monotonic() + _BOOT_TIMEOUT_S
    last_error = ""
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
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _run_probe(workspace: Path, base_url: str, tmp_path: Path) -> ProbeVerdict:
    comp = _loaded("auth-kit")
    probe_source = comp.probe_source()
    assert probe_source is not None, "auth-kit manifest declares no probe"
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


def test_auth_kit_probe_passes_when_the_seam_is_wired(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path, _SERVER_JS_WRAPPED)
    proc, port = _boot_node_server(workspace)
    try:
        verdict = _run_probe(workspace, f"http://127.0.0.1:{port}", tmp_path)
    finally:
        _kill(proc)

    assert verdict.passed is True, verdict.model_dump()
    names = {c.name for c in verdict.checks}
    assert {"unauth_401", "allowlisted_open", "login_ok", "session_roundtrip", "logout_kills"} <= (
        names
    )
    assert all(c.passed for c in verdict.checks), verdict.model_dump()

    # Sanity: this really is the SAME sqlite file both database-kit's default
    # config and auth-kit's migrations wrote to (proves the fixture actually
    # exercised the real seam, not an in-memory stand-in).
    db_path = workspace / "data" / "app.db"
    assert db_path.is_file()


def test_auth_kit_probe_fails_when_the_seam_is_unguarded(tmp_path: Path) -> None:
    """The negative proof: the identical handler, mounted bare (no
    createAuthApp) — nothing owns the seam, so an unauthenticated request to
    a non-allowlisted path is never rejected. unauth_401 must fail, and that
    must sink the overall verdict — protection is opt-OUT, so an unwrapped
    handler is caught, not silently accepted."""
    workspace = _write_workspace(tmp_path, _SERVER_JS_BARE)
    proc, port = _boot_node_server(workspace)
    try:
        verdict = _run_probe(workspace, f"http://127.0.0.1:{port}", tmp_path)
    finally:
        _kill(proc)

    assert verdict.passed is False, verdict.model_dump()
    failing = {c.name for c in verdict.checks if not c.passed}
    assert "unauth_401" in failing, verdict.model_dump()
