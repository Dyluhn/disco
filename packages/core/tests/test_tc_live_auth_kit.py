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

import http.client
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


# auth-kit ships devSeedUser=null (no backdoor by default), so the happy-path
# probe's login/session/logout checks only run when a build configures a dev
# user. This is a NON-default credential — proving both the full login flow AND
# that no_default_backdoor passes for a real (non-shipped) credential.
_TEST_DEV_USER = {"email": "tester@example.com", "password": "s3cret-not-default"}


def _write_workspace(
    tmp_path: Path, server_js: str, *, config_override: dict | None = None
) -> Path:
    """Materialize a minimal app workspace: BOTH kits' shipped install_tree()
    under src/trusted/<name>/, a {"type":"module"} package.json, and the
    fixture server.js at the workspace root. `config_override`, when given, is
    merged onto the installed auth-kit config (the free config surface a build
    edits) so a test can enable a dev user without shipping one by default."""
    workspace = tmp_path / "workspace"
    for name in ("database-kit", "auth-kit"):
        comp = _loaded(name)
        for relpath, data in comp.install_tree().items():
            target = workspace / "src" / "trusted" / name / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    if config_override:
        cfg_path = workspace / "src" / "trusted" / "auth-kit" / "config" / "auth.config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg.update(config_override)
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
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
    workspace = _write_workspace(
        tmp_path, _SERVER_JS_WRAPPED, config_override={"devSeedUser": _TEST_DEV_USER}
    )
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


def _raw_request(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    """Send `method path` with the path LITERAL — http.client does not
    canonicalize the request target, so a "//notes" reaches the server as-is
    (urllib would rewrite it). Returns (status, body)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
    try:
        hdrs = dict(headers or {})
        if body is not None:
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def test_auth_kit_blocks_path_confusion_and_survives_malformed_input(tmp_path: Path) -> None:
    """S4 CRITICAL regressions, proven live against the real seam:

    #1 auth bypass — a protocol-relative "//notes" must NOT read as the public
       "/" and reach the guarded handler; it is 401, and the process stays up.
    #2 unauthenticated crash — a JSON object/array where a string is expected
       (email or password) must be a clean 401, never a thrown, unhandled
       rejection that kills the whole node process. Proven by continuing to
       serve "/" (200) after each hostile request.
    """
    workspace = _write_workspace(
        tmp_path, _SERVER_JS_WRAPPED, config_override={"devSeedUser": _TEST_DEV_USER}
    )
    proc, port = _boot_node_server(workspace)
    try:
        # baseline: the guarded route really is guarded on the normal path.
        assert _raw_request(port, "GET", "/notes")[0] == 401

        # #1 — protocol-relative path confusion must not bypass the guard.
        status, _ = _raw_request(port, "GET", "//notes")
        assert status == 401, f"//notes bypassed the guard -> {status}"
        # backslash and dot-segment variants resolve to the same guarded route.
        assert _raw_request(port, "GET", "/../notes")[0] == 401
        assert _raw_request(port, "GET", "/./notes")[0] == 401
        assert _raw_request(port, "GET", "/")[0] == 200  # process alive

        # #1b — ENCODED dot-segments through the /__health/* wildcard (the S4
        # re-review CRITICAL): "%2e%2e" must not read as public here while the
        # wrapped handler's new URL() resolves it to /notes. Rejected (400),
        # never 200. "/__health/db" is public in the shipped allowlist, so the
        # climb target is a real protected route reached ONLY via the desync.
        for climb in (
            "/__health/%2e%2e/notes",
            "/__health/%2E%2E/notes",
            "/__health/x/%2e%2e/%2e%2e/notes",
            "/__health%2f%2e%2e/notes",
            # double / over-encoded variants (convergence PoC): must not leak
            # even if a downstream router decodes more than once.
            "/__health/%252e%252e/notes",
            "/__health/%25%32%65%25%32%65/notes",
            "/__health/%c0%ae%c0%ae/notes",
        ):
            st, body = _raw_request(port, "GET", climb)
            assert b"secret" not in body.lower(), f"climb {climb!r} leaked protected content"
            assert st != 200, f"encoded-dot climb {climb!r} reached the handler -> {st}"

        # #2 — malformed credential TYPES must not crash the process.
        for hostile in (
            b'{"email":{"$ne":1},"password":"x"}',
            b'{"email":["a"],"password":"x"}',
            b'{"email":"real@user.test","password":{"x":1}}',
            b'{"email":"real@user.test","password":["x"]}',
        ):
            status, _ = _raw_request(port, "POST", "/auth/login", body=hostile)
            assert status == 401, f"malformed login {hostile!r} -> {status} (want 401)"
            assert _raw_request(port, "GET", "/")[0] == 200, (
                f"process died after malformed login {hostile!r}"
            )

        # #8 — an oversized body is refused (413), process still alive.
        big = b'{"email":"' + b"a" * 200_000 + b'","password":"x"}'
        status, _ = _raw_request(port, "POST", "/auth/login", body=big)
        assert status == 413, f"oversized body -> {status} (want 413)"
        assert _raw_request(port, "GET", "/")[0] == 200

        # sanity: a genuine login still works after all the hostile traffic.
        status, _ = _raw_request(
            port,
            "POST",
            "/auth/login",
            body=json.dumps(_TEST_DEV_USER).encode("utf-8"),
        )
        assert status == 200, f"legit login broke after hostile traffic -> {status}"
    finally:
        _kill(proc)


def _raw_headers(port: int, method: str, path: str, *, body: bytes | None = None,
                 headers: dict[str, str] | None = None) -> tuple[int, dict[str, str]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
    try:
        hdrs = dict(headers or {})
        if body is not None:
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        out = {k.lower(): v for k, v in resp.getheaders()}
        resp.read()
        return resp.status, out
    finally:
        conn.close()


def test_auth_kit_forged_xff_cannot_bypass_rate_limit_and_413_closes(tmp_path: Path) -> None:
    """S4 re-review HIGH + MEDIUM-HIGH, proven live:

    * A spoofed X-Forwarded-For must NOT mint a fresh rate-limit bucket per
      request (trustProxy defaults off), so a burst of login attempts with
      distinct forged XFF still trips the throttle (429).
    * A 413 (oversized body) response carries Connection: close so the socket
      is not left hung waiting for the undelivered remainder.
    """
    workspace = _write_workspace(
        tmp_path,
        _SERVER_JS_WRAPPED,
        config_override={
            "devSeedUser": _TEST_DEV_USER,
            "loginRateLimit": {"windowMs": 60000, "max": 3},
        },
    )
    proc, port = _boot_node_server(workspace)
    try:
        # 413 FIRST (before the small rate-limit window is exhausted): oversized
        # body → 413 with Connection: close.
        big = b'{"email":"' + b"a" * 200_000 + b'","password":"x"}'
        st, hdrs = _raw_headers(port, "POST", "/auth/login", body=big)
        assert st == 413, f"oversized body -> {st} (want 413)"
        assert hdrs.get("connection", "").lower() == "close", (
            f"413 did not close the connection; headers={hdrs}"
        )

        # Now the forged-XFF burst: distinct spoofed XFF per request must still
        # be throttled (they all count against the real peer 127.0.0.1).
        statuses = []
        for i in range(8):
            code, _ = _raw_request(
                port,
                "POST",
                "/auth/login",
                body=b'{"email":"nobody@x.test","password":"wrong"}',
                headers={"X-Forwarded-For": f"10.0.0.{i}"},
            )
            statuses.append(code)
        assert 429 in statuses, f"forged XFF bypassed the rate limit; statuses={statuses}"
    finally:
        _kill(proc)
