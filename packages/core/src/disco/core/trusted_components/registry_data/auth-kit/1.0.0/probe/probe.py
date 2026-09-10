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

# A live devSeedUser whose password is any of these guessable strings is treated
# as a backdoor too — the shipped default is only ONE such credential, and a
# renamed-but-still-weak dev account is just as exploitable.
WEAK_SEED_PASSWORDS = frozenset(
    {
        SHIPPED_DEFAULT_PASSWORD,
        "password",
        "password123",
        "admin",
        "admin123",
        "changeme",
        "letmein",
        "dev",
        "devpassword",
        "test",
        "test123",
        "123456",
        "12345678",
        "secret",
    }
)


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


def _load_config(workspace: Path) -> dict[str, Any] | None:
    config_path = workspace / CONFIG_RELPATH
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — probe cannot proceed without config: infra error
        print(f"auth-kit probe: could not read {config_path}: {exc}", file=sys.stderr)
        return None


def _check_unauth_401(base_url: str) -> dict[str, Any]:
    # (a) unauth_401 — a path definitely NOT in the allowlist must be rejected.
    probe_path = _random_probe_path()
    status, _hdrs, _raw = _request(base_url, probe_path)
    return {
        "name": "unauth_401",
        "passed": status == 401,
        "detail": f"GET {probe_path} -> {status} (want 401)",
    }


def _check_allowlisted_open(base_url: str, allowlist: list[str]) -> dict[str, Any]:
    # (b) allowlisted_open — the first EXACT (non-wildcard) allowlist entry
    # must NOT be rejected.
    exact_entries = [e for e in allowlist if not e.endswith("*")]
    if exact_entries:
        open_path = exact_entries[0]
        status, _hdrs, _raw = _request(base_url, open_path)
        return {
            "name": "allowlisted_open",
            "passed": status != 401,
            "detail": f"GET {open_path} -> {status} (want != 401)",
        }
    return {
        "name": "allowlisted_open",
        "passed": False,
        "detail": "no exact (non-wildcard) publicAllowlist entry configured to probe",
    }


def _check_login_rejects_bogus(base_url: str) -> dict[str, Any]:
    # (c) login_rejects_bogus — the login endpoint exists and fails closed on a
    # credential that cannot exist. Runs regardless of dev-seed config, so the
    # login seam is never wholly unprobed.
    bogus_email = _random_probe_path().strip("/") + "@probe.invalid"
    bogus_pw = "".join(random.choices(string.ascii_letters + string.digits, k=24))
    status, _hb, _rb = _request(
        base_url, "/auth/login", method="POST", body={"email": bogus_email, "password": bogus_pw}
    )
    return {
        "name": "login_rejects_bogus",
        "passed": status == 401,
        "detail": f"POST /auth/login (nonexistent user) -> {status} (want 401)",
    }


def _is_weak_seed(dev_seed: Any) -> bool:
    return (
        isinstance(dev_seed, dict)
        and isinstance(dev_seed.get("password"), str)
        and dev_seed.get("password") in WEAK_SEED_PASSWORDS
    )


def _check_no_default_backdoor(base_url: str, dev_seed: Any) -> dict[str, Any]:
    # (d) no_default_backdoor — a live devSeedUser with a guessable password (the
    # shipped default OR any common weak password) must NOT authenticate. FAILS
    # closed if such a seed exists and logs in.
    if _is_weak_seed(dev_seed):
        status, _hd, _rd = _request(
            base_url,
            "/auth/login",
            method="POST",
            body={"email": dev_seed.get("email"), "password": dev_seed.get("password")},
        )
        return {
            "name": "no_default_backdoor",
            "passed": status != 200,
            "detail": (
                f"weak/known dev credential login -> {status} "
                "(want != 200; remove/replace config.devSeedUser before shipping)"
            ),
        }
    return {
        "name": "no_default_backdoor",
        "passed": True,
        "detail": "config.devSeedUser is not a known-weak credential",
    }


def _check_login_ok(
    base_url: str, email: Any, password: Any
) -> tuple[dict[str, Any], str | None]:
    status, hdrs, _raw = _request(
        base_url, "/auth/login", method="POST", body={"email": email, "password": password}
    )
    set_cookie = hdrs.get("Set-Cookie", "")
    login_ok = status == 200 and "tc_session" in set_cookie and "httponly" in set_cookie.lower()
    check = {
        "name": "login_ok",
        "passed": login_ok,
        "detail": (
            f"POST /auth/login -> {status}; "
            f"set-cookie-has-tc_session={'tc_session' in set_cookie}, "
            f"httponly={'httponly' in set_cookie.lower()}"
        ),
    }
    return check, _extract_session_cookie(set_cookie)


def _check_session_roundtrip(
    base_url: str, session_cookie: str | None, email: Any
) -> dict[str, Any]:
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
    return {
        "name": "session_roundtrip",
        "passed": with_ok and without_ok,
        "detail": "; ".join(detail_bits),
    }


def _check_logout_kills(base_url: str, session_cookie: str | None) -> dict[str, Any]:
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
    return {"name": "logout_kills", "passed": logout_ok, "detail": detail}


def _dev_seed_checks(base_url: str, dev_seed: dict[str, Any]) -> list[dict[str, Any]]:
    email = dev_seed.get("email")
    password = dev_seed.get("password")
    login_check, session_cookie = _check_login_ok(base_url, email, password)
    roundtrip_check = _check_session_roundtrip(base_url, session_cookie, email)
    logout_check = _check_logout_kills(base_url, session_cookie)
    return [login_check, roundtrip_check, logout_check]


def _run_checks(base_url: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    allowlist: list[str] = config.get("publicAllowlist") or []
    dev_seed = config.get("devSeedUser")
    checks: list[dict[str, Any]] = [
        _check_unauth_401(base_url),
        _check_allowlisted_open(base_url, allowlist),
        _check_login_rejects_bogus(base_url),
        _check_no_default_backdoor(base_url, dev_seed),
    ]
    if dev_seed is not None:
        checks.extend(_dev_seed_checks(base_url, dev_seed))
    return checks


def _verdict(checks: list[dict[str, Any]]) -> dict[str, Any]:
    passed = all(c["passed"] for c in checks)
    failing_names = [c["name"] for c in checks if not c["passed"]]
    return {
        "passed": passed,
        "checks": checks,
        "summary": (
            "auth-kit seam probe: all checks passed"
            if passed
            else "auth-kit seam probe: failed " + ", ".join(failing_names)
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()

    base_url: str = args.base_url
    workspace = Path(args.workspace)
    config = _load_config(workspace)
    if config is None:
        return 1

    checks = _run_checks(base_url, config)
    print(json.dumps(_verdict(checks)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
