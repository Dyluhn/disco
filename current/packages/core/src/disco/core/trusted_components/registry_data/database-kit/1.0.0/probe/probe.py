"""database-kit 1.0.0 — host-run seam probe (spec §4.3, D3).

STDLIB ONLY (urllib.request, json, argparse) — this file is never installed
into a workspace and never imports anything from Disco, so it stays runnable
in any harness. Checks the `/__health/db` seam the kit owns: GET it, expect
HTTP 200 with a JSON body `{"ok": true, "migration_version": <int >= 0>}`.

Prints EXACTLY ONE line to stdout: the verdict JSON
`{"passed": bool, "checks": [{"name", "passed", "detail"}...], "summary": str}`
(disco.core.trusted_components.verify.parse_probe_stdout reads the LAST
stdout line). Exits 0 even when checks fail — a nonzero exit means the probe
itself could not run, which is a distinct, host-side "infra error".
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

TIMEOUT_S = 10


def _get(url: str) -> tuple[int | None, str]:
    """GET `url`, returning (status, body). status is None on a connection-
    level failure (refused, DNS, timeout) — never raises."""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, str(exc)


def run(base_url: str, workspace: str) -> dict:
    del workspace  # unused for database-kit (no config surface to honor here)
    checks: list[dict] = []
    url = base_url.rstrip("/") + "/__health/db"

    status, body = _get(url)

    if status != 200:
        checks.append(
            {
                "name": "health_http_200",
                "passed": False,
                "detail": f"GET {url} -> {status!r} (expected 200); body: {body[:300]!r}",
            }
        )
        return _verdict(checks)
    checks.append({"name": "health_http_200", "passed": True, "detail": f"GET {url} -> 200"})

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        checks.append(
            {
                "name": "health_body_is_json",
                "passed": False,
                "detail": f"body is not valid JSON: {exc}; body: {body[:300]!r}",
            }
        )
        return _verdict(checks)
    checks.append({"name": "health_body_is_json", "passed": True, "detail": "body parsed as JSON"})

    ok = parsed.get("ok") if isinstance(parsed, dict) else None
    if ok is not True:
        checks.append(
            {
                "name": "health_ok_true",
                "passed": False,
                "detail": f"body.ok = {ok!r} (expected true); body: {body[:300]!r}",
            }
        )
    else:
        checks.append({"name": "health_ok_true", "passed": True, "detail": "body.ok is true"})

    version = parsed.get("migration_version") if isinstance(parsed, dict) else None
    version_ok = isinstance(version, int) and not isinstance(version, bool) and version >= 0
    checks.append(
        {
            "name": "health_migration_version",
            "passed": version_ok,
            "detail": (
                f"body.migration_version = {version!r} (>= 0 integer)"
                if version_ok
                else f"body.migration_version = {version!r} — expected a non-negative integer"
            ),
        }
    )

    return _verdict(checks)


def _verdict(checks: list[dict]) -> dict:
    passed = all(c["passed"] for c in checks)
    n_pass = sum(1 for c in checks if c["passed"])
    summary = f"database-kit probe: {n_pass}/{len(checks)} checks passed."
    return {"passed": passed, "checks": checks, "summary": summary}


def main() -> None:
    parser = argparse.ArgumentParser(description="database-kit seam probe")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()

    verdict = run(args.base_url, args.workspace)
    print(json.dumps(verdict))


if __name__ == "__main__":
    main()
