#!/usr/bin/env python3
"""auth-kit 1.0.0 seam probe (spec §4.3, §6.2). Host-run ONLY — this file is
never copied into a workspace (D3). Python stdlib only: urllib.request,
json, argparse. Exercises the seam over HTTP, not the internals (those are
hash-proven by component_integrity).

The probe READS the workspace's config/auth.config.json so it honors
whatever the model configured `publicAllowlist` to be — the guarantee being
checked is "the allowlist is enforced as configured", not "the allowlist is
what we shipped" (spec §7.2).

Output contract: exactly one stdout line — the verdict JSON
{"passed": bool, "checks": [...], "summary": str}. Exit 0 even when checks
fail; a nonzero exit means the probe itself could not run (infra error).
"""

from __future__ import annotations

import argparse
import json
import random
import string
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TIMEOUT_S = 10
CONFIG_RELPATH = "src/trusted/auth-kit/config/auth.config.json"

# The credential auth-kit ships in its example config. If a build leaves this
# well-known pair live, the seam has a public backdoor — the probe FAILS closed
# on it (spec: a component cannot reach `verified` carrying a known credential).
SHIPPED_DEFAULT_EMAIL = "dev@example.com"
SHIPPED_DEFAULT_PASSWORD = "dev-password-123"


def _random_probe_path() -> str:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
    return f"/__tc_probe_not_public_{suffix}"


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    cookie: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """One HTTP request. Connection-level failures are reported as status -1
    (never raised) so a single unreachable check degrades that check to
    failed, not the whole probe to a crash."""
    url = base_url.rstrip("/") + path
    data = None
    headers: dict[str, str] = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if cookie is not None:
        headers["Cookie"] = cookie
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
        print(f"auth-kit probe: could not read {config_path}: {exc}", file=sys.stderr)
        return 1

    allowlist: list[str] = config.get("publicAllowlist") or []
    dev_seed = config.get("devSeedUser")

    checks: list[dict[str, Any]] = []

    # (a) unauth_401 — a path definitely NOT in the allowlist must be rejected.
    probe_path = _random_probe_path()
    status, _hdrs, _raw = _request(base_url, probe_path)
    checks.append(
        {
            "name": "unauth_401",
            "passed": status == 401,
            "detail": f"GET {probe_path} -> {status} (want 401)",
        }
    )

    # (b) allowlisted_open — the first EXACT (non-wildcard) allowlist entry
    # must NOT be rejected.
    exact_entries = [e for e in allowlist if not e.endswith("*")]
    if exact_entries:
        open_path = exact_entries[0]
        status, _hdrs, _raw = _request(base_url, open_path)
        checks.append(
            {
                "name": "allowlisted_open",
                "passed": status != 401,
                "detail": f"GET {open_path} -> {status} (want != 401)",
            }
        )
    else:
        checks.append(
            {
                "name": "allowlisted_open",
                "passed": False,
                "detail": "no exact (non-wildcard) publicAllowlist entry configured to probe",
            }
        )

    # (c) login_rejects_bogus — the login endpoint exists and fails closed on a
    # credential that cannot exist. Runs regardless of dev-seed config, so the
    # login seam is never wholly unprobed.
    bogus_email = _random_probe_path().strip("/") + "@probe.invalid"
    bogus_pw = "".join(random.choices(string.ascii_letters + string.digits, k=24))
    status, _hb, _rb = _request(
        base_url, "/auth/login", method="POST", body={"email": bogus_email, "password": bogus_pw}
    )
    checks.append(
        {
            "name": "login_rejects_bogus",
            "passed": status == 401,
            "detail": f"POST /auth/login (nonexistent user) -> {status} (want 401)",
        }
    )

    # (d) no_default_backdoor — the shipped example credential must NOT be a live
    # login. FAILS closed if a build left config.devSeedUser at the shipped
    # default and it authenticates.
    is_shipped_default = (
        isinstance(dev_seed, dict)
        and dev_seed.get("email") == SHIPPED_DEFAULT_EMAIL
        and dev_seed.get("password") == SHIPPED_DEFAULT_PASSWORD
    )
    if is_shipped_default:
        status, _hd, _rd = _request(
            base_url,
            "/auth/login",
            method="POST",
            body={"email": SHIPPED_DEFAULT_EMAIL, "password": SHIPPED_DEFAULT_PASSWORD},
        )
        checks.append(
            {
                "name": "no_default_backdoor",
                "passed": status != 200,
                "detail": (
                    f"shipped-default dev credential login -> {status} "
                    "(want != 200; remove/replace config.devSeedUser before shipping)"
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "no_default_backdoor",
                "passed": True,
                "detail": "config.devSeedUser is not the shipped default credential",
            }
        )

    if dev_seed is not None:
        email = dev_seed.get("email")
        password = dev_seed.get("password")

        status, hdrs, _raw = _request(
            base_url, "/auth/login", method="POST", body={"email": email, "password": password}
        )
        set_cookie = hdrs.get("Set-Cookie", "")
        login_ok = status == 200 and "tc_session" in set_cookie and "httponly" in set_cookie.lower()
        checks.append(
            {
                "name": "login_ok",
                "passed": login_ok,
                "detail": (
                    f"POST /auth/login -> {status}; "
                    f"set-cookie-has-tc_session={'tc_session' in set_cookie}, "
                    f"httponly={'httponly' in set_cookie.lower()}"
                ),
            }
        )
        session_cookie = _extract_session_cookie(set_cookie)

        detail_bits: list[str] = []
        with_ok = False
        if session_cookie:
            status_with, _h, raw_with = _request(base_url, "/auth/session", cookie=session_cookie)
            try:
                parsed = json.loads(raw_with.decode("utf-8"))
            except Exception:  # noqa: BLE001 — a non-JSON body just fails the check
                parsed = {}
            with_ok = status_with == 200 and parsed.get("email") == email
            detail_bits.append(f"with-cookie -> {status_with} email={parsed.get('email')!r}")
        else:
            detail_bits.append("no session cookie captured from login")
        status_without, _h2, _r2 = _request(base_url, "/auth/session")
        without_ok = status_without == 401
        detail_bits.append(f"without-cookie -> {status_without} (want 401)")
        checks.append(
            {
                "name": "session_roundtrip",
                "passed": with_ok and without_ok,
                "detail": "; ".join(detail_bits),
            }
        )

        if session_cookie:
            status_logout, _h3, _r3 = _request(
                base_url, "/auth/logout", method="POST", cookie=session_cookie
            )
            status_after, _h4, _r4 = _request(base_url, "/auth/session", cookie=session_cookie)
            logout_ok = status_logout == 200 and status_after == 401
            detail = f"logout -> {status_logout}; session-after-logout -> {status_after} (want 401)"
        else:
            logout_ok = False
            detail = "no session cookie captured from login"
        checks.append({"name": "logout_kills", "passed": logout_ok, "detail": detail})

    passed = all(c["passed"] for c in checks)
    failing_names = [c["name"] for c in checks if not c["passed"]]
    verdict = {
        "passed": passed,
        "checks": checks,
        "summary": (
            "auth-kit seam probe: all checks passed"
            if passed
            else "auth-kit seam probe: failed " + ", ".join(failing_names)
        ),
    }
    print(json.dumps(verdict))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
