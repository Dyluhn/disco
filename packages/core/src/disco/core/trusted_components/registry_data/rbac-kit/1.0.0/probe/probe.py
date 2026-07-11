#!/usr/bin/env python3
"""rbac-kit 1.0.0 seam probe (spec §7.3). Host-run ONLY — never copied into a
workspace (D3). Python stdlib only. Exercises the ROLE seam over HTTP against
the composed app (auth-kit wrapping rbac-kit wrapping the app handler):

  * unauth_role_route_denied — a role-gated route with no session is denied.
  * member_forbidden        — a logged-in user WITHOUT the role is 403 on it,
                              AND a forged `X-Role`/cookie claim does not help
                              (the role is decided server-side, spec §7.3).
  * admin_allowed           — a logged-in user WITH the role is let through.

The member/admin checks run only when config.devSeedRoleUsers names both a
user lacking the gated role and a user holding it (the shipped default is
null — no accounts — exactly like auth-kit's devSeedUser).

Output contract: one stdout line, the verdict JSON
{"passed": bool, "checks": [...], "summary": str}. Exit 0 even when checks
fail; nonzero exit means the probe itself could not run.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TIMEOUT_S = 10
CONFIG_RELPATH = "src/trusted/rbac-kit/config/rbac.config.json"


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    cookie: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    url = base_url.rstrip("/") + path
    data = None
    headers: dict[str, str] = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if cookie is not None:
        headers["Cookie"] = cookie
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        hdrs = dict(exc.headers.items()) if exc.headers else {}
        return exc.code, hdrs, raw
    except urllib.error.URLError as exc:
        return -1, {}, str(exc).encode("utf-8")


def _extract_session_cookie(set_cookie_header: str) -> str | None:
    if not set_cookie_header:
        return None
    first_attr = set_cookie_header.split(";", 1)[0].strip()
    name, sep, value = first_attr.partition("=")
    if not sep or name.strip() != "tc_session":
        return None
    return f"tc_session={value.strip()}"


def _login(base_url: str, email: object, password: object) -> str | None:
    if not isinstance(email, str) or not isinstance(password, str):
        return None
    status, hdrs, _raw = _request(
        base_url, "/auth/login", method="POST", body={"email": email, "password": password}
    )
    if status != 200:
        return None
    return _extract_session_cookie(hdrs.get("Set-Cookie", ""))


def _concrete_route(pattern: str) -> str:
    """A concrete path for a roleRoutes key: strip a trailing '*' wildcard."""
    return pattern[:-1] if pattern.endswith("*") else pattern


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()

    base_url: str = args.base_url
    workspace = Path(args.workspace)
    config_path = workspace / CONFIG_RELPATH
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — probe cannot proceed without config: infra error
        print(f"rbac-kit probe: could not read {config_path}: {exc}", file=sys.stderr)
        return 1

    role_routes: dict[str, str] = config.get("roleRoutes") or {}
    seeds = config.get("devSeedRoleUsers") or []

    checks: list[dict[str, Any]] = []

    if not role_routes:
        checks.append(
            {
                "name": "role_routes_configured",
                "passed": False,
                "detail": "config.roleRoutes is empty — nothing for rbac-kit to guard",
            }
        )
        _emit(checks)
        return 0

    # The first configured role route is the one we probe.
    pattern, required_role = next(iter(role_routes.items()))
    route = _concrete_route(pattern)

    # (a) unauth_role_route_denied — no session must not reach a role-gated route.
    status, _h, _r = _request(base_url, route)
    checks.append(
        {
            "name": "unauth_role_route_denied",
            "passed": status in (401, 403),
            "detail": f"GET {route} (no session) -> {status} (want 401/403)",
        }
    )

    # Partition seeds into one WITHOUT the required role (member) and one WITH it.
    def _roles(seed: dict[str, Any]) -> list[str]:
        r = seed.get("roles")
        return r if isinstance(r, list) else []

    member = next(
        (s for s in seeds if isinstance(s, dict) and required_role not in _roles(s)), None
    )
    admin = next((s for s in seeds if isinstance(s, dict) and required_role in _roles(s)), None)

    if member is not None:
        member_cookie = _login(base_url, member.get("email"), member.get("password"))
        # Forge a client-side role claim every way a naive guard might trust.
        forged = {"X-Role": required_role, "X-Roles": required_role, "Role": required_role}
        status_plain, _hp, _rp = _request(base_url, route, cookie=member_cookie)
        status_forged, _hf, _rf = _request(
            base_url, route, cookie=member_cookie, extra_headers=forged
        )
        # Case variation: a case-insensitive downstream router (Express default)
        # must not let an upper-cased route dodge the gate.
        status_case, _hu, _ru = _request(base_url, route.upper(), cookie=member_cookie)
        checks.append(
            {
                "name": "member_forbidden",
                "passed": (member_cookie is not None)
                and status_plain == 403
                and status_forged == 403
                and status_case == 403,
                "detail": (
                    f"member GET {route} -> {status_plain}; "
                    f"forged {list(forged)} -> {status_forged}; "
                    f"case {route.upper()} -> {status_case} (want 403/403/403); "
                    f"logged_in={member_cookie is not None}"
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "member_forbidden",
                "passed": True,
                "detail": "no non-privileged seed user configured to probe (skipped)",
            }
        )

    if admin is not None:
        admin_cookie = _login(base_url, admin.get("email"), admin.get("password"))
        status_admin, _ha, _ra = _request(base_url, route, cookie=admin_cookie)
        checks.append(
            {
                "name": "admin_allowed",
                "passed": (admin_cookie is not None) and status_admin not in (401, 403),
                "detail": (
                    f"admin GET {route} -> {status_admin} (want not 401/403); "
                    f"logged_in={admin_cookie is not None}"
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "admin_allowed",
                "passed": True,
                "detail": "no privileged seed user configured to probe (skipped)",
            }
        )

    _emit(checks)
    return 0


def _emit(checks: list[dict[str, Any]]) -> None:
    passed = all(c["passed"] for c in checks)
    failing_names = [c["name"] for c in checks if not c["passed"]]
    verdict = {
        "passed": passed,
        "checks": checks,
        "summary": (
            "rbac-kit seam probe: all checks passed"
            if passed
            else "rbac-kit seam probe: failed " + ", ".join(failing_names)
        ),
    }
    print(json.dumps(verdict))


if __name__ == "__main__":
    raise SystemExit(main())
