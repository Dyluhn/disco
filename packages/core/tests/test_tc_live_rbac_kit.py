"""WO-TC5 — rbac-kit 1.0.0 LIVE proof (spec §7.3). Boots a REAL `node` process
that mounts all THREE kits composed exactly as the GUIDE prescribes —
database-kit db + auth-kit session guard wrapping rbac-kit's role guard wrapping
the app handler — then runs the REGISTRY's own probe (host-run, D3) against it.

Proves the spec §7.3 property: role checks are server-side. A logged-in member
(no admin role), even forging an `X-Role: admin` header, is 403 on `/admin`;
an admin is let through. The negative mounts the SAME app WITHOUT rbac's guard,
so the member reaches `/admin` — the probe's member_forbidden check then fails,
proving the guard is what produces the 403 (not the app).

Skips cleanly when node >= 22 is absent, or when the three kits are not all
shipped in the registry.
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

_MEMBER = {"email": "member@example.com", "password": "member-pw-9911", "roles": ["member"]}
_ADMIN = {"email": "admin@example.com", "password": "admin-pw-7722", "roles": ["admin"]}
_SEED_ROLE_USERS = [_MEMBER, _ADMIN]


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
    reg = TrustedComponentRegistry.default()
    missing = [n for n in ("database-kit", "auth-kit", "rbac-kit") if reg.get(n) is None]
    if missing:
        return f"kits not yet shipped in the registry: {missing}"
    return None


_SKIP_REASON = _skip_reason()
pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=str(_SKIP_REASON))


# The app handler routes CASE-INSENSITIVELY (lower-casing the path), exactly like
# Express's default router — this is the condition under which a case-sensitive
# role guard would be bypassable by "/ADMIN". The guard must gate it anyway.
_APP_HANDLER_JS = """\
function appHandler(req, res) {
  const pathname = new URL(req.url, "http://localhost").pathname.toLowerCase();
  if (pathname === "/") {
    res.writeHead(200, { "Content-Type": "text/plain" });
    res.end("home");
    return;
  }
  if (pathname === "/admin") {
    res.writeHead(200, { "Content-Type": "text/plain" });
    res.end("admin area");
    return;
  }
  res.writeHead(404, { "Content-Type": "text/plain" });
  res.end("not found");
}
"""

_MOUNT_PREAMBLE = """\
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { openDatabase, runMigrations } from "./src/trusted/database-kit/core/db.js";
import { AUTH_MIGRATIONS } from "./src/trusted/auth-kit/core/schema.js";
import { seedDevUser } from "./src/trusted/auth-kit/core/auth.js";
import { createAuthApp } from "./src/trusted/auth-kit/core/middleware.js";
import { RBAC_MIGRATIONS } from "./src/trusted/rbac-kit/core/schema.js";
import { seedRoleUsers } from "./src/trusted/rbac-kit/core/roles.js";
import { createRbacApp } from "./src/trusted/rbac-kit/core/guard.js";

const authConfig = JSON.parse(
  readFileSync("./src/trusted/auth-kit/config/auth.config.json", "utf8")
);
const rbacConfig = JSON.parse(
  readFileSync("./src/trusted/rbac-kit/config/rbac.config.json", "utf8")
);

const db = openDatabase();
runMigrations(db, AUTH_MIGRATIONS);
runMigrations(db, RBAC_MIGRATIONS);
seedDevUser(db, authConfig);
seedRoleUsers(db, rbacConfig);

"""

# Correctly composed: rbac's role guard wraps the app, auth's session guard wraps
# that. This is the arrangement the probe must find PASSING.
_SERVER_JS_WRAPPED = (
    _MOUNT_PREAMBLE
    + _APP_HANDLER_JS
    + """
const guarded = createRbacApp({ db, config: rbacConfig, handler: appHandler });
const app = createAuthApp({ db, config: authConfig, handler: guarded });
const server = createServer(app);
server.listen(0, "127.0.0.1", () => {
  console.log(`PORT ${server.address().port}`);
});
"""
)

# Negative: the rbac guard is NOT mounted — the app handler sits directly behind
# auth-kit. A logged-in member therefore reaches /admin, so member_forbidden
# fails and the verdict is False. Proves the 403 comes from rbac's guard.
_SERVER_JS_NO_GUARD = (
    _MOUNT_PREAMBLE
    + _APP_HANDLER_JS
    + """
const app = createAuthApp({ db, config: authConfig, handler: appHandler });
const server = createServer(app);
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
    workspace = tmp_path / "workspace"
    for name in ("database-kit", "auth-kit", "rbac-kit"):
        comp = _loaded(name)
        for relpath, data in comp.install_tree().items():
            target = workspace / "src" / "trusted" / name / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    # Enable the seed users so the probe can exercise member-vs-admin (ships null).
    cfg_path = workspace / "src" / "trusted" / "rbac-kit" / "config" / "rbac.config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["devSeedRoleUsers"] = _SEED_ROLE_USERS
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    (workspace / "package.json").write_text(json.dumps({"type": "module"}) + "\n", encoding="utf-8")
    (workspace / "server.js").write_text(server_js, encoding="utf-8")
    return workspace


def _boot_node_server(workspace: Path) -> tuple[subprocess.Popen, int]:
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
        f"node server.js did not print a PORT line within {_BOOT_TIMEOUT_S}s ({buf!r})"
    )


def _wait_http_ready(port: int) -> None:
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
    comp = _loaded("rbac-kit")
    probe_source = comp.probe_source()
    assert probe_source is not None, "rbac-kit manifest declares no probe"
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
        f"probe.py exited {result.returncode}; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return parse_probe_stdout(result.stdout)


# ---- the live tests -----------------------------------------------------------


def test_rbac_kit_probe_passes_when_the_role_guard_is_wired(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path, _SERVER_JS_WRAPPED)
    proc, port = _boot_node_server(workspace)
    try:
        verdict = _run_probe(workspace, f"http://127.0.0.1:{port}", tmp_path)
    finally:
        _kill(proc)

    assert verdict.passed is True, verdict.model_dump()
    names = {c.name for c in verdict.checks}
    assert {"unauth_role_route_denied", "member_forbidden", "admin_allowed"} <= names
    assert all(c.passed for c in verdict.checks), verdict.model_dump()


def test_rbac_kit_probe_fails_when_the_role_guard_is_absent(tmp_path: Path) -> None:
    """Negative: same app, auth-kit only (no rbac guard). The member reaches
    /admin, so member_forbidden fails and the verdict is False — the 403 in the
    positive case genuinely comes from rbac's guard, not the app handler."""
    workspace = _write_workspace(tmp_path, _SERVER_JS_NO_GUARD)
    proc, port = _boot_node_server(workspace)
    try:
        verdict = _run_probe(workspace, f"http://127.0.0.1:{port}", tmp_path)
    finally:
        _kill(proc)

    assert verdict.passed is False, verdict.model_dump()
    failing = {c.name for c in verdict.checks if not c.passed}
    assert "member_forbidden" in failing, verdict.model_dump()


def _rbac_login(port: int, user: dict) -> str | None:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
    try:
        conn.request(
            "POST",
            "/auth/login",
            body=json.dumps(user).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        set_cookie = resp.getheader("Set-Cookie") or ""
        resp.read()
        if resp.status != 200:
            return None
        return set_cookie.split(";", 1)[0]
    finally:
        conn.close()


def _rbac_get(port: int, path: str, cookie: str) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
    try:
        conn.request("GET", path, headers={"Cookie": cookie})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def test_rbac_kit_member_cannot_climb_via_case_matrix_or_encoding(tmp_path: Path) -> None:
    """Convergence regression: a member must not reach /admin by case variation,
    matrix params, or percent-encoding — the guard normalizes all of them before
    matching roleRoutes. Uses the case-insensitive app handler (Express-like)."""
    workspace = _write_workspace(tmp_path, _SERVER_JS_WRAPPED)
    proc, port = _boot_node_server(workspace)
    try:
        member = _rbac_login(port, _MEMBER)
        admin = _rbac_login(port, _ADMIN)
        assert member and admin, f"login failed member={member} admin={admin}"
        # sanity: admin reaches /admin, member is blocked on the canonical path.
        assert _rbac_get(port, "/admin", admin)[0] == 200
        assert _rbac_get(port, "/admin", member)[0] == 403
        for climb in (
            "/ADMIN",
            "/Admin",
            "/aDmIn",
            "/%41dmin",
            "/%41DMIN",
            "/admin;x",
            "/admin;jsessionid=1",
            "/admin/%252e%252e/admin",
        ):
            st, body = _rbac_get(port, climb, member)
            assert st == 403, f"member reached {climb!r} -> {st} {body[:40]!r}"
    finally:
        _kill(proc)
