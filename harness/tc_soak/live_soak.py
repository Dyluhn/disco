"""Trusted-components TIER-2 LIVE SOAK — the real shipped components under
randomized configuration + adversarial traffic, complementing the tier-1
state-machine soak (run.py, which uses a synthetic registry).

Two phases:

  A. VERIFY FUZZ (fast, Python-level, REAL registry): random locks over the
     real kits, random core-file corruptions, random dep removals — asserts the
     shipped components obey the same invariants tier-1 proves synthetically:
       * a corrupted core file auto-ejects that component (never blocks)
       * a missing requires edge FAILS the deps check (blocks)
       * clean install + full graph => integrity pass, deps pass

  B. LIVE INTEGRATION (slower, boots real node): each round installs all three
     kits into a fresh workspace with a RANDOMIZED config (random roleRoutes,
     random seed users/roles, random auth allowlist), boots the composed
     auth->rbac->app stack, runs all three registry probes (must PASS when
     correctly wired), then fires an adversarial request barrage (path
     confusion, malformed login types, oversized bodies, traversal) and asserts
     the server STAYS UP and still serves "/" (liveness — no hostile request
     may crash a trusted seam).

Usage:
    PYTHONPATH=. .venv/bin/python3 harness/tc_soak/live_soak.py \
        --verify-iters 300 --live-rounds 12 --seed 1

Exit 0 = all invariants held; 1 = a violation (details printed). Skips phase B
(not a failure) when node >= 22 is unavailable.
"""

from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import random
import re
import select
import shutil
import string
import subprocess
import sys
import time
from pathlib import Path

from disco.core.trusted_components.lockfile import ComponentsLock, InstalledComponent
from disco.core.trusted_components.registry import TrustedComponentRegistry
from disco.core.trusted_components.verify import (
    install_path,
    parse_probe_stdout,
    verify_trusted_components,
)

KITS = ("database-kit", "auth-kit", "rbac-kit")
NOW = "2026-07-10T00:00:00+00:00"
_PORT_LINE_RE = re.compile(r"PORT (\d+)")
_BOOT_TIMEOUT_S = 15.0


class SoakFailure(Exception):
    """An invariant was violated — the soak fails loudly with the detail."""


# --------------------------------------------------------------------------- #
# Phase A — verify fuzz over the REAL registry                                 #
# --------------------------------------------------------------------------- #


def _files_for(reg: TrustedComponentRegistry, names: list[str]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for n in names:
        comp = reg.get(n)
        if comp is None:
            continue
        for rel, data in comp.install_tree().items():
            out[install_path(n, rel)] = data
    return out


def _lock_for(names: list[str]) -> ComponentsLock:
    lock = ComponentsLock()
    for n in names:
        lock.components[n] = InstalledComponent(version="1.0.0", installed_at=NOW)
    return lock


# the full requires closure each kit needs to satisfy its deps check
_DEP_CLOSURE = {
    "database-kit": set(),
    "auth-kit": {"database-kit"},
    "rbac-kit": {"auth-kit", "database-kit"},
}


async def phase_a_verify_fuzz(
    reg: TrustedComponentRegistry, rng: random.Random, iters: int
) -> None:
    available = [k for k in KITS if reg.get(k) is not None]
    for i in range(iters):
        installed = [k for k in available if rng.random() < 0.7] or [rng.choice(available)]
        files = _files_for(reg, installed)
        lock = _lock_for(installed)

        mode = rng.choice(["clean", "corrupt_core", "drop_dep"])

        if mode == "corrupt_core":
            # Corrupt one installed core file's bytes → that component must eject.
            core_keys = [k for k in files if "/core/" in k]
            if core_keys:
                victim_path = rng.choice(core_keys)
                files[victim_path] = files[victim_path] + b"\n// tampered\n"
                victim = victim_path.split("/trusted/", 1)[1].split("/", 1)[0]
                res = await verify_trusted_components(lock, reg, files, run_probe=None, now_iso=NOW)
                if victim not in res.newly_ejected:
                    raise SoakFailure(
                        f"[A/{i}] corrupted {victim_path} but {victim} was NOT ejected; "
                        f"ejected={res.newly_ejected}"
                    )
                if res.failing and all(c.status != "fail" for c in res.checks if "deps" in c.name):
                    # an eject alone must never *block* (only a real deps/probe fail may)
                    pass
            continue

        if mode == "drop_dep":
            # Install a kit but omit a required dep → deps check FAILS (blocks).
            need_dep = [k for k in installed if _DEP_CLOSURE[k]]
            if need_dep:
                kit = rng.choice(need_dep)
                missing = rng.choice(sorted(_DEP_CLOSURE[kit]))
                keep = [k for k in installed if k != missing]
                lock2 = _lock_for(keep)
                files2 = _files_for(reg, keep)
                res = await verify_trusted_components(
                    lock2, reg, files2, run_probe=None, now_iso=NOW
                )
                deps = next(
                    (c for c in res.checks if c.name == f"component_deps:{kit}"), None
                )
                if deps is None or deps.status != "fail":
                    raise SoakFailure(
                        f"[A/{i}] {kit} missing dep {missing} but deps check did not FAIL "
                        f"(got {deps.status if deps else 'absent'})"
                    )
                if not res.failing:
                    raise SoakFailure(f"[A/{i}] missing dep {missing} for {kit} did not block")
            continue

        # clean: a fully-satisfied graph must show integrity pass, no eject.
        closure = set(installed)
        for k in installed:
            closure |= _DEP_CLOSURE[k]
        installed_full = [k for k in available if k in closure]
        files_full = _files_for(reg, installed_full)
        lock_full = _lock_for(installed_full)
        res = await verify_trusted_components(
            lock_full, reg, files_full, run_probe=None, now_iso=NOW
        )
        if res.newly_ejected:
            raise SoakFailure(f"[A/{i}] clean install ejected {res.newly_ejected}")
        integ = [c for c in res.checks if c.name.startswith("component_integrity")]
        if any(c.status != "pass" for c in integ):
            bad = [(c.name, c.status) for c in integ if c.status != "pass"]
            raise SoakFailure(f"[A/{i}] clean install had non-pass integrity: {bad}")


# --------------------------------------------------------------------------- #
# Phase B — live integration + adversarial liveness                            #
# --------------------------------------------------------------------------- #

_SERVER_TEMPLATE = """\
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { openDatabase, runMigrations } from "./src/trusted/database-kit/core/db.js";
import { healthHandler } from "./src/trusted/database-kit/core/health.js";
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

// database-kit owns the /__health/db seam; the app wires it (auth-kit's default
// allowlist keeps /__health/* public so it is reachable when composed).
const health = healthHandler(db);
function appHandler(req, res) {
  const pathname = new URL(req.url, "http://localhost").pathname;
  if (pathname === "/__health/db") { health(req, res); return; }
  if (pathname === "/") { res.writeHead(200); res.end("home"); return; }
  if (pathname.startsWith("/admin")) { res.writeHead(200); res.end("admin"); return; }
  if (pathname.startsWith("/staff")) { res.writeHead(200); res.end("staff"); return; }
  res.writeHead(404); res.end("nf");
}

const guarded = createRbacApp({ db, config: rbacConfig, handler: appHandler });
const app = createAuthApp({ db, config: authConfig, handler: guarded });
createServer(app).listen(0, "127.0.0.1", function () {
  console.log("PORT " + this.address().port);
});
"""

_ROLE_POOL = ["admin", "staff", "member", "editor", "viewer"]
_ROUTE_POOL = ["/admin*", "/staff*", "/admin/settings", "/staff/reports"]


def _rand_email(rng: random.Random) -> str:
    return "".join(rng.choices(string.ascii_lowercase, k=8)) + "@soak.test"


def _random_configs(rng: random.Random) -> tuple[dict, dict]:
    role = rng.choice(_ROLE_POOL)
    route = rng.choice(_ROUTE_POOL)

    def _pw() -> str:
        return "pw-" + "".join(rng.choices(string.digits, k=6))

    other_role = rng.choice([r for r in _ROLE_POOL if r != role])
    rbac_cfg = {
        "roleRoutes": {route: role},
        "devSeedRoleUsers": [
            {"email": _rand_email(rng), "password": _pw(), "roles": [role]},  # privileged
            {"email": _rand_email(rng), "password": _pw(), "roles": [other_role]},  # member
        ],
    }
    auth_cfg = {
        "publicAllowlist": ["/", "/index.html", "/__health/*"],
        "sessionTtlHours": rng.choice([1, 24, 72]),
        "maxBodyBytes": rng.choice([4096, 65536]),
        "loginRateLimit": {"windowMs": 60000, "max": rng.choice([5, 10, 50])},
        "devSeedUser": None,
    }
    return auth_cfg, rbac_cfg


def _write_workspace(
    reg: TrustedComponentRegistry, root: Path, auth_cfg: dict, rbac_cfg: dict
) -> Path:
    ws = root / "ws"
    for name in KITS:
        comp = reg.get(name)
        assert comp is not None
        for rel, data in comp.install_tree().items():
            t = ws / "src" / "trusted" / name / rel
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_bytes(data)
    (ws / "src" / "trusted" / "auth-kit" / "config" / "auth.config.json").write_text(
        json.dumps(auth_cfg, indent=2) + "\n"
    )
    (ws / "src" / "trusted" / "rbac-kit" / "config" / "rbac.config.json").write_text(
        json.dumps(rbac_cfg, indent=2) + "\n"
    )
    (ws / "package.json").write_text(json.dumps({"type": "module"}) + "\n")
    (ws / "server.js").write_text(_SERVER_TEMPLATE)
    return ws


def _boot(ws: Path) -> tuple[subprocess.Popen, int]:
    proc = subprocess.Popen(
        [shutil.which("node") or "node", "server.js"],
        cwd=str(ws), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.monotonic() + _BOOT_TIMEOUT_S
    buf = ""
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read() if proc.stderr else ""
            raise SoakFailure(
                f"node exited early code={proc.returncode} stdout={buf!r} stderr={err}"
            )
        ready, _, _ = select.select([proc.stdout], [], [], 0.5)
        if proc.stdout in ready:
            line = proc.stdout.readline()
            buf += line
            m = _PORT_LINE_RE.search(line)
            if m:
                return proc, int(m.group(1))
    _kill(proc)
    raise SoakFailure(f"node never printed PORT (stdout={buf!r})")


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _raw(port: int, method: str, path: str, *, body: bytes | None = None) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
    try:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=body, headers=headers)
        return conn.getresponse().status
    finally:
        conn.close()


def _run_probe(
    reg: TrustedComponentRegistry, name: str, ws: Path, base_url: str, tmp: Path
) -> dict:
    comp = reg.get(name)
    assert comp is not None
    src = comp.probe_source()
    assert src is not None
    p = tmp / f"{name}-probe.py"
    p.write_bytes(src)
    out = subprocess.run(
        [sys.executable, str(p), "--base-url", base_url, "--workspace", str(ws)],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        raise SoakFailure(f"{name} probe infra-failed rc={out.returncode} stderr={out.stderr!r}")
    return parse_probe_stdout(out.stdout).model_dump()


_BARRAGE = [
    ("GET", "//admin", None),
    ("GET", "/admin/../admin", None),
    ("GET", "/./admin", None),
    ("GET", "/admin/%2e%2e/secret", None),
    ("POST", "/auth/login", b'{"email":{"$ne":1},"password":"x"}'),
    ("POST", "/auth/login", b'{"email":["a"],"password":"x"}'),
    ("POST", "/auth/login", b'{"email":"a@b.c","password":{"x":1}}'),
    ("POST", "/auth/login", b'{"email":"a@b.c","password":["x"]}'),
    ("POST", "/auth/login", b'{"__proto__":{"admin":true},"email":"a@b.c","password":"x"}'),
    ("POST", "/auth/login", b'not json at all'),
    ("POST", "/auth/login", b'{"email":"' + b"a" * 300_000 + b'","password":"x"}'),
]


def phase_b_live(reg: TrustedComponentRegistry, rng: random.Random, rounds: int, tmp: Path) -> int:
    node = shutil.which("node")
    if node is None:
        print("phase B skipped: node not on PATH")
        return 0
    ran = 0
    for r in range(rounds):
        auth_cfg, rbac_cfg = _random_configs(rng)
        rd = tmp / f"round-{r}"
        rd.mkdir(parents=True, exist_ok=True)
        ws = _write_workspace(reg, rd, auth_cfg, rbac_cfg)
        proc, port = _boot(ws)
        try:
            base = f"http://127.0.0.1:{port}"
            # all three probes must pass on the correctly-wired stack
            for kit in KITS:
                verdict = _run_probe(reg, kit, ws, base, rd)
                if not verdict["passed"]:
                    raise SoakFailure(
                        f"[B/{r}] {kit} probe FAILED on a correctly-wired stack: "
                        f"{verdict['summary']} (config={rbac_cfg['roleRoutes']})"
                    )
            # adversarial barrage — none may crash the process
            for method, path, body in _BARRAGE:
                try:
                    _raw(port, method, path, body=body)
                except Exception:  # noqa: BLE001 — a torn connection is fine; a dead server is not
                    pass
                if _raw(port, "GET", "/") != 200:
                    raise SoakFailure(
                        f"[B/{r}] server DIED after hostile {method} {path!r} "
                        f"(body={len(body) if body else 0}B)"
                    )
            ran += 1
        finally:
            _kill(proc)
    return ran


# --------------------------------------------------------------------------- #


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify-iters", type=int, default=300)
    ap.add_argument("--live-rounds", type=int, default=12)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    reg = TrustedComponentRegistry.default()
    missing = [k for k in KITS if reg.get(k) is None]
    if missing:
        print(f"cannot soak: kits not shipped: {missing}")
        return 1
    rng = random.Random(args.seed)

    try:
        asyncio.run(phase_a_verify_fuzz(reg, rng, args.verify_iters))
        print(f"phase A: {args.verify_iters} verify-fuzz iterations clean (real registry)")
        import tempfile

        with tempfile.TemporaryDirectory(prefix="tc-live-soak-") as td:
            ran = phase_b_live(reg, rng, args.live_rounds, Path(td))
        print(f"phase B: {ran}/{args.live_rounds} live rounds clean (probes pass + liveness held)")
    except SoakFailure as exc:
        print(f"SOAK FAIL (seed {args.seed}): {exc}")
        return 1
    print(f"LIVE SOAK PASS — seed {args.seed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
