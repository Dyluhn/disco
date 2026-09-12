"""`python -m disco.agent_server.doctor` — one command that says what is wrong.

Runs inside the agent-server container (`podman compose exec agent-server python -m
disco.agent_server.doctor`) or from a checkout. Every check prints PASS / WARN / FAIL /
SKIP with the real reason; the process exits non-zero iff a check FAILED. `--bundle
PATH` also writes a redacted JSON support bundle for a bug report: build identity, the
checks, the effective DISCO_* configuration with secret values removed, both health
bodies, the sandbox probe, and the shape (never the content) of the last conversations.

    config       — build identity, DISCO_* variables read (values redacted), legacy PMX_* shadows
    agent-server — GET /health; degraded checks are named
    app-server   — GET /api/health (set --app-url when it is not http://app-server:8800)
    sandbox      — GET /api/sandbox/health through a loopback session: backend + endpoint
    data disk    — free space where DISCO_DB lives
    database     — sqlite quick_check on DISCO_DB
    secret key   — DISCO_SECRET_KEY / DISCO_AUTH_SECRET(_FILE) present
    driver model — `python -m disco.agent_server.verify --quick` (skip with --no-model)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from disco.core.build_info import build_info
from disco.core.env import disco_env
from disco.core.evidence.schema import redact

DEFAULT_AGENT_URL = "http://127.0.0.1:8000"
DEFAULT_APP_URL = "http://app-server:8800"
DISK_WARN_BYTES = 2 * 1024**3
DISK_FAIL_BYTES = 512 * 1024**2
SECRET_ENV_NAMES = ("DISCO_SECRET_KEY", "DISCO_AUTH_SECRET", "DISCO_AUTH_SECRET_FILE")
MODEL_CHECK_TIMEOUT_S = 240
BUNDLE_CONVERSATIONS = 10
BUNDLE_EVENTS_PER_CONVERSATION = 200


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # PASS | WARN | FAIL | SKIP
    detail: str


def _shorten(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Doctor:
    def __init__(
        self,
        *,
        agent_url: str = DEFAULT_AGENT_URL,
        app_url: str = DEFAULT_APP_URL,
        transport: httpx.BaseTransport | None = None,
        env: dict[str, str] | None = None,
        run_model_check: bool = True,
    ) -> None:
        self.agent_url = agent_url.rstrip("/")
        self.app_url = app_url.rstrip("/")
        self.env = dict(os.environ if env is None else env)
        self.run_model_check = run_model_check
        self._client = httpx.Client(transport=transport, timeout=10.0)
        self.health: dict[str, Any] = {}
        self.sandbox: dict[str, Any] | None = None
        self.conversations: list[dict[str, Any]] = []
        self._session: dict[str, str] | None = None

    # --- checks -----------------------------------------------------------------

    def check_config(self) -> Check:
        return check_config(self.env)

    def check_agent_server(self) -> Check:
        body, error = self._get_json(f"{self.agent_url}/health")
        if body is None:
            return Check("agent-server", "FAIL", f"unreachable at {self.agent_url}/health: {error}")
        self.health["agent-server"] = body
        if body.get("status") == "ok":
            return Check("agent-server", "PASS", f"ok, version {body.get('version', '?')}")
        failing = {k: v for k, v in (body.get("checks") or {}).items() if str(v).startswith("error")}
        return Check("agent-server", "FAIL", f"degraded: {failing or body}")

    def check_app_server(self) -> Check:
        body, error = self._get_json(f"{self.app_url}/api/health")
        if body is None:
            return Check(
                "app-server", "WARN", f"not reachable from here at {self.app_url} ({error}); pass --app-url"
            )
        self.health["app-server"] = body
        return Check("app-server", "PASS", f"ok, version {body.get('version', '?')}")

    def check_sandbox(self) -> Check:
        headers = self._loopback_session()
        if headers is None:
            return Check("sandbox", "SKIP", "no loopback session (run inside the agent-server container)")
        try:
            resp = self._client.get(f"{self.agent_url}/api/sandbox/health", headers=headers)
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return Check("sandbox", "FAIL", f"probe failed: {exc}")
        self.sandbox = body
        if body.get("reachable"):
            return Check("sandbox", "PASS", f"{body.get('backend', '?')} reachable")
        return Check("sandbox", "FAIL", _shorten(f"{body.get('backend', '?')}: {body.get('detail', 'unreachable')}"))

    def check_disk(self) -> Check:
        return check_disk(self.env)

    def check_database(self) -> Check:
        return check_database(self.env)

    def check_secret_key(self) -> Check:
        return check_secret_key(self.env)

    def check_driver_model(self) -> Check:
        if not self.run_model_check:
            return Check("driver model", "SKIP", "--no-model")
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "disco.agent_server.verify", "--quick"],
                capture_output=True,
                text=True,
                timeout=MODEL_CHECK_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Check("driver model", "FAIL", f"disco-verify --quick did not finish in {MODEL_CHECK_TIMEOUT_S}s")
        tail = " | ".join(line for line in (proc.stdout + proc.stderr).splitlines()[-4:] if line.strip())
        return Check("driver model", "PASS" if proc.returncode == 0 else "FAIL", _shorten(tail or f"exit {proc.returncode}"))

    def run(self) -> list[Check]:
        return [
            self.check_config(),
            self.check_agent_server(),
            self.check_app_server(),
            self.check_sandbox(),
            self.check_disk(),
            self.check_database(),
            self.check_secret_key(),
            self.check_driver_model(),
        ]

    # --- bundle -----------------------------------------------------------------

    def bundle(self, checks: list[Check]) -> dict[str, Any]:
        return redact(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "build": build_info().as_dict(),
                "checks": [asdict(c) for c in checks],
                "env": redacted_env(self.env),
                "health": self.health,
                "sandbox": self.sandbox,
                "conversations": self._conversation_shapes(),
            }
        )

    def _conversation_shapes(self) -> list[dict[str, Any]]:
        """Status and event shape of the latest conversations — kinds and tool names, no content."""
        headers = self._loopback_session()
        if headers is None:
            return []
        listing, _ = self._get_json(f"{self.agent_url}/conversations?limit={BUNDLE_CONVERSATIONS}", headers=headers)
        ids = listing.get("conversation_ids", []) if isinstance(listing, dict) else []
        shapes: list[dict[str, Any]] = []
        for cid in [str(c) for c in ids if c][:BUNDLE_CONVERSATIONS]:
            state, _ = self._get_json(f"{self.agent_url}/conversations/{cid}/state", headers=headers)
            events, _ = self._get_json(
                f"{self.agent_url}/conversations/{cid}/events?limit={BUNDLE_EVENTS_PER_CONVERSATION}",
                headers=headers,
            )
            status = (state or {}).get("execution_status") or (state or {}).get("status") if isinstance(state, dict) else None
            items = events.get("events", []) if isinstance(events, dict) else []
            shapes.append({"conversation_id": cid, "status": status, "events": [_event_shape(e) for e in items]})
        return shapes

    # --- transport helpers ---------------------------------------------------------

    def _get_json(self, url: str, **kwargs: Any) -> tuple[Any, str]:
        """The JSON body when there is one (a 503 health body still says why), else the error."""
        try:
            resp = self._client.get(url, **kwargs)
        except httpx.HTTPError as exc:
            return None, _shorten(str(exc), 120)
        try:
            return resp.json(), ""
        except ValueError:
            return None, f"HTTP {resp.status_code}"

    def _loopback_session(self) -> dict[str, str] | None:
        """Mint a loopback session once; the cookie lives on the client, the CSRF header here."""
        if self._session is not None:
            return self._session
        try:
            resp = self._client.post(
                f"{self.agent_url}/api/auth/mint",
                json={"pairing_token": ""},
                headers={"Origin": self.agent_url},
            )
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        body = resp.json()
        self._client.cookies.update(resp.cookies)
        self._session = {"Origin": self.agent_url, "X-Disco-CSRF": str(body.get("csrf_token", ""))}
        return self._session


# --- local checks: no HTTP, usable from the doctor and from /api/diagnostics -----------


def check_config(env: dict[str, str]) -> Check:
    info = build_info()
    names = sorted(k for k in env if k.startswith("DISCO_"))
    shadows = sorted(k for k in env if k.startswith("PMX_") and "DISCO_" + k[4:] not in env)
    detail = f"build {info.label()} ({info.source}); {len(names)} DISCO_* variables set"
    if shadows:
        return Check("config", "WARN", f"{detail}; legacy names still in use: {', '.join(shadows)}")
    return Check("config", "PASS" if info.commit != "unknown" else "WARN", detail)


def check_disk(env: dict[str, str]) -> Check:
    db = env.get("DISCO_DB", "disco.db")
    where = Path(env.get("DISCO_DATA_DIR") or Path(db).parent or ".")
    try:
        usage = shutil.disk_usage(where)
    except OSError as exc:
        return Check("data disk", "FAIL", f"{where}: {exc}")
    detail = f"{usage.free / 1024**3:.1f} GB free at {where}"
    if usage.free < DISK_FAIL_BYTES:
        return Check("data disk", "FAIL", detail)
    return Check("data disk", "WARN" if usage.free < DISK_WARN_BYTES else "PASS", detail)


def check_database(env: dict[str, str]) -> Check:
    db = env.get("DISCO_DB", "disco.db")
    if not Path(db).exists():
        return Check("database", "WARN", f"{db} does not exist yet")
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5) as conn:
            result = conn.execute("PRAGMA quick_check").fetchone()
            events = conn.execute("SELECT COUNT(*) FROM events").fetchone()
    except sqlite3.Error as exc:
        return Check("database", "FAIL", f"{db}: {exc}")
    verdict = str(result[0]) if result else "no result"
    size_mb = Path(db).stat().st_size / 1024**2
    detail = f"{db}: quick_check {verdict}, {events[0] if events else '?'} events, {size_mb:.0f} MB"
    return Check("database", "PASS" if verdict == "ok" else "FAIL", detail)


def check_secret_key(env: dict[str, str]) -> Check:
    present = [name for name in SECRET_ENV_NAMES if env.get(name)]
    if present:
        return Check("secret key", "PASS", f"{', '.join(present)} set")
    return Check(
        "secret key",
        "WARN",
        "none of DISCO_SECRET_KEY / DISCO_AUTH_SECRET set; encrypted settings will not survive a restart",
    )


def local_checks(env: dict[str, str]) -> list[Check]:
    """The checks that need no network: what `/api/diagnostics` can answer in-process."""
    return [check_config(env), check_disk(env), check_database(env), check_secret_key(env)]


def redacted_env(env: dict[str, str]) -> dict[str, Any]:
    return redact({k: v for k, v in env.items() if k.startswith(("DISCO_", "PMX_"))})


def _event_shape(event: dict[str, Any]) -> dict[str, Any]:
    tool_call = event.get("tool_call") or {}
    result = event.get("tool_result") or {}
    shape: dict[str, Any] = {"seq": event.get("seq"), "kind": event.get("kind")}
    if tool_call.get("tool_name"):
        shape["tool"] = tool_call["tool_name"]
    if result:
        shape["success"] = result.get("success")
        if result.get("error"):
            shape["error"] = _shorten(result["error"], 160)
    if event.get("kind") in ("agent_error", "error"):
        shape["error"] = _shorten(event.get("error") or event.get("detail") or "", 160)
    if event.get("kind") == "status":
        shape["status"] = event.get("status")
    return shape


def render(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = [f"{c.status:<4} {c.name:<{width}}  {c.detail}" for c in checks]
    failed = sum(c.status == "FAIL" for c in checks)
    lines.append("")
    lines.append("all checks passed" if not failed else f"{failed} check(s) FAILED")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m disco.agent_server.doctor", description=(__doc__ or "").split("\n\n")[0]
    )
    ap.add_argument("--agent-url", default=disco_env("DOCTOR_AGENT_URL") or DEFAULT_AGENT_URL)
    ap.add_argument("--app-url", default=disco_env("DOCTOR_APP_URL") or DEFAULT_APP_URL)
    ap.add_argument("--no-model", action="store_true", help="skip the driver-model check")
    ap.add_argument("--bundle", metavar="PATH", help="also write a redacted JSON support bundle")
    args = ap.parse_args(argv)
    doctor = Doctor(agent_url=args.agent_url, app_url=args.app_url, run_model_check=not args.no_model)
    checks = doctor.run()
    print(render(checks))
    if args.bundle:
        path = Path(args.bundle)
        path.write_text(json.dumps(doctor.bundle(checks), indent=2, default=str))
        print(f"support bundle written to {path} — attach it to the bug report (secrets are redacted)")
    return 1 if any(c.status == "FAIL" for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
