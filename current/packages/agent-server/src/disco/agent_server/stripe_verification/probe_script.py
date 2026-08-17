"""Probe-script owner — local workerd runtime probe via Miniflare.

This module owns the ``_MiniflareRuntimeProbe`` class and the
``_miniflare_probe_script`` generator.  Wrangler's local runtime does not
permit outbound loopback fetches, so this companion workerd process routes
only the configured bus URL through Miniflare's host-owned outbound adapter,
which performs the real HTTPS request to the pre-bound FastAPI bus.
"""

from __future__ import annotations

import asyncio
import json
import select
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType

from .evidence import (
    _BOOT_TIMEOUT_S,
    _HTTP_TIMEOUT_S,
    StripeLiveVerifierError,
    _bundle_entry,
    _cap_text,
    _command_env,
    _CookieJar,
    _free_port,
    _miniflare_entry,
    _set_cookie_headers,
    _terminate_process,
)
from .workerd_lifecycle import _WorkerdApp


class _MiniflareRuntimeProbe:
    """Run the trusted bundle in local workerd with outbound delivery to FastAPI.

    Wrangler's local runtime does not permit outbound loopback fetches. This
    companion workerd process routes only the configured bus URL through
    Miniflare's host-owned outbound adapter, which performs the real HTTPS
    request to the pre-bound FastAPI bus. No response is fabricated.
    """

    def __init__(self, worker: _WorkerdApp, tmp_root: Path, control_token: str) -> None:
        self._worker = worker
        self._tmp_root = tmp_root
        self._control_token = control_token
        self._port: int | None = None
        self._control_port: int | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._script: Path | None = None
        self._cookies = _CookieJar()

    def __enter__(self) -> _MiniflareRuntimeProbe:
        bundle = _bundle_entry(self._worker.bundle_dir)
        miniflare = _miniflare_entry(self._worker._wrangler_bin())
        node = shutil.which("node")
        if node is None:
            raise StripeLiveVerifierError("node executable is required for local workerd probe")
        self._port = _free_port()
        self._control_port = _free_port()
        self._script = self._tmp_root / "runtime-probe.mjs"
        self._script.write_text(
            _miniflare_probe_script(
                bundle,
                miniflare,
                self._port,
                self._control_port,
                self._worker.app_dir / "schema.sql",
                self._worker.ca_cert,
            ),
            encoding="utf-8",
        )
        try:
            self._proc = subprocess.Popen(
                [node, f"--env-file={self._worker.env_path}", str(self._script)],
                cwd=str(self._tmp_root),
                env=_command_env(self._tmp_root, extra_ca_cert=self._worker.ca_cert),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
                pass_fds=(self._worker.env_fd,),
            )
        except OSError as exc:
            raise StripeLiveVerifierError(
                f"failed to start local workerd runtime probe: {exc}"
            ) from exc
        assert self._proc.stdout is not None
        readable, _, _ = select.select([self._proc.stdout], [], [], _BOOT_TIMEOUT_S)
        if not readable:
            self.__exit__(None, None, None)
            raise StripeLiveVerifierError("local workerd runtime probe did not start")
        first_line = self._proc.stdout.readline().strip()
        if first_line != "READY":
            # Node may leave its event loop alive after emitting an asynchronous
            # startup exception.  Drain what has already been written before
            # terminating it so this fail-closed verifier retains the actual
            # runtime diagnostic rather than only its first stack-frame line.
            lines = [first_line]
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                readable, _, _ = select.select([self._proc.stdout], [], [], 0.1)
                if readable:
                    line = self._proc.stdout.readline()
                    if line:
                        lines.append(line.strip())
                        continue
                if self._proc.poll() is not None:
                    break
            _terminate_process(self._proc)
            try:
                _stdout, _stderr = self._proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                _stdout = ""
            self.__exit__(None, None, None)
            raise StripeLiveVerifierError(
                "local workerd runtime probe did not become ready: "
                + _cap_text("\n".join(lines) + "\n" + _stdout)
            )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._proc is not None:
            _terminate_process(self._proc)
            self._proc = None

    def payments_ready(self, admin_token: str) -> bool:
        if self._port is None:
            raise StripeLiveVerifierError("local workerd runtime probe has not been started")
        try:
            status, body = self.request("GET", "/api/stripe/runtime-probe", token=admin_token)
            return status == 200 and json.loads(body) == {"ready": True}
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
            return False

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        token: str | None = None,
    ) -> tuple[int, str]:
        if self._port is None:
            raise StripeLiveVerifierError("local workerd runtime probe has not been started")
        req_headers = dict(headers or {})
        if token is not None:
            req_headers["Authorization"] = f"Bearer {token}"
        cookie_header = self._cookies.header()
        if cookie_header is not None and "Cookie" not in req_headers:
            req_headers["Cookie"] = cookie_header
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._port}{path}",
            data=body,
            headers=req_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as response:
                self._cookies.store(_set_cookie_headers(response.headers))
                return response.status, response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            self._cookies.store(_set_cookie_headers(exc.headers))
            return exc.code, exc.read().decode("utf-8", errors="replace")

    def post_json(
        self, path: str, obj: Mapping[str, object], *, token: str | None = None
    ) -> tuple[int, str]:
        return self.request(
            "POST",
            path,
            json.dumps(obj, separators=(",", ":")).encode("utf-8"),
            {"Content-Type": "application/json"},
            token,
        )

    def get(self, path: str, *, token: str | None = None) -> tuple[int, str]:
        return self.request("GET", path, token=token)

    async def request_async(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        token: str | None = None,
    ) -> tuple[int, str]:
        return await asyncio.to_thread(self.request, method, path, body, headers, token)

    async def post_json_async(
        self, path: str, obj: Mapping[str, object], *, token: str | None = None
    ) -> tuple[int, str]:
        return await asyncio.to_thread(self.post_json, path, obj, token=token)

    async def get_async(self, path: str, *, token: str | None = None) -> tuple[int, str]:
        return await asyncio.to_thread(self.get, path, token=token)

    def d1_count(self, table: str) -> int:
        if table not in {"stripe_events", "stripe_fulfillments", "user_role_grants"}:
            raise StripeLiveVerifierError("invalid local D1 table request")
        if self._control_port is None:
            raise StripeLiveVerifierError("local D1 control service has not been started")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._control_port}/count/{table}",
            headers={"Authorization": f"Bearer {self._control_token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as response:
                payload = json.loads(response.read())
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            raise StripeLiveVerifierError("could not query local D1 state") from exc
        count = payload.get("count") if isinstance(payload, dict) else None
        if not isinstance(count, int):
            raise StripeLiveVerifierError("local D1 count response was invalid")
        return count

    async def d1_count_async(self, table: str) -> int:
        return await asyncio.to_thread(self.d1_count, table)


# ---------------------------------------------------------------------------
# Probe-script generator — split into cohesive sections to stay under 100 logical lines
# ---------------------------------------------------------------------------


def _probe_script_header(miniflare: Path) -> str:
    """Return the import and runtime-binding validation section of the probe script."""
    return f"""import {{ Miniflare }} from {json.dumps(miniflare.as_uri())};
import https from "node:https";
import http from "node:http";
import {{ readFileSync }} from "node:fs";
const required = [
  "ADMIN_TOKEN", "DISCO_SVC_BUS", "DISCO_SVC_TOKEN", "STRIPE_WEBHOOK_SECRET",
  "STRIPE_APP_BINDING_SECRET", "STRIPE_RUNTIME_READY",
];
for (const name of required) {{
  if (typeof process.env[name] !== "string" || process.env[name].length === 0) {{
    throw new Error(`missing runtime binding ${{name}}`);
  }}
}}
"""


def _probe_script_forwarder(ca_cert: Path) -> str:
    """Return the host-bus outbound forwarder section of the probe script."""
    return f"""const bus = new URL(process.env.DISCO_SVC_BUS);
const busCa = readFileSync({json.dumps(str(ca_cert))});
async function forwardToHostBus(request) {{
  const target = new URL(request.url);
  const body = Buffer.from(await request.arrayBuffer());
  return await new Promise((resolve, reject) => {{
    const outbound = https.request({{
      protocol: target.protocol,
      hostname: target.hostname,
      port: target.port,
      path: target.pathname + target.search,
      method: request.method,
      headers: Object.fromEntries(request.headers),
      ca: busCa,
      rejectUnauthorized: true,
    }}, (response) => {{
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => {{
        const headers = new Headers();
        for (const [name, value] of Object.entries(response.headers)) {{
          if (typeof value === "string") headers.set(name, value);
          else if (Array.isArray(value)) headers.set(name, value.join(","));
        }}
        resolve(new Response(Buffer.concat(chunks), {{
          status: response.statusCode || 502,
          headers,
        }}));
      }});
    }});
    outbound.on("error", reject);
    outbound.end(body);
  }});
}}
"""


def _probe_script_miniflare_setup(bundle: Path, port: int, ca_cert: Path) -> str:
    """Return the Miniflare worker setup section of the probe script."""
    return f"""const mf = new Miniflare({{
  host: "127.0.0.1",
  port: {port},
  workers: [{{
    name: "stripe-live-runtime-probe",
    modules: true,
    scriptPath: {json.dumps(str(bundle))},
    compatibilityDate: "2025-01-01",
    d1Databases: {{ DB: "stripe-live-runtime-probe" }},
    outboundService: async (request) => {{
      const target = new URL(request.url);
      if (target.origin !== bus.origin || !target.pathname.startsWith("/_disco/svc/")) {{
        return new Response("blocked outbound", {{ status: 403 }});
      }}
      try {{
        return await forwardToHostBus(request);
      }} catch (error) {{
        return new Response("host bus unavailable", {{ status: 502 }});
      }}
    }},
    bindings: Object.fromEntries(required.map((name) => [name, process.env[name]])),
  }}],
}});
await mf.ready;
"""


def _probe_script_d1_schema(schema: Path) -> str:
    """Return the D1 schema initialization section of the probe script."""
    return f"""const db = await mf.getD1Database("DB");
const schema = readFileSync({json.dumps(str(schema))}, "utf8")
  .split("\\n")
  .filter((line) => !line.trimStart().startsWith("--"))
  .join("\\n");
// D1's Miniflare binding executes one statement per exec call.  The generated,
// trusted schema contains no SQL string literals, so semicolons delimit its DDL
// unambiguously once line comments have been removed above.
for (const statement of schema.split(";")) {{
  if (statement.trim()) {{
    try {{
      await db.prepare(statement).run();
    }} catch (error) {{
      const detail = JSON.stringify(statement);
      throw new Error(
        `generated D1 schema statement failed: ${{detail}}`,
        {{ cause: error }},
      );
    }}
  }}
}}
"""


def _probe_script_control_server(control_port: int) -> str:
    """Return the D1 control HTTP server and signal-handling section."""
    return f"""const controlToken = process.env.DISCO_LIVE_D1_CONTROL_TOKEN;
const tables = new Set(["stripe_events", "stripe_fulfillments", "user_role_grants"]);
const control = http.createServer(async (request, response) => {{
  const table = request.url?.match(/^\\/count\\/([a-z_]+)$/)?.[1];
  if (request.method !== "GET" || request.headers.authorization !== `Bearer ${{controlToken}}`
      || table === undefined || !tables.has(table)) {{
    response.writeHead(403).end();
    return;
  }}
  try {{
    const row = await db.prepare(`SELECT COUNT(*) AS c FROM ${{table}}`).first();
    const count = typeof row?.c === "number" ? row.c : -1;
    response.writeHead(200, {{ "Content-Type": "application/json" }});
    response.end(JSON.stringify({{ count }}));
  }} catch {{
    response.writeHead(500).end();
  }}
}});
await new Promise((resolve) => control.listen({control_port}, "127.0.0.1", resolve));
console.log("READY");
for (const signal of ["SIGTERM", "SIGINT"]) process.on(signal, async () => {{
  await new Promise((resolve) => control.close(resolve));
  await mf.dispose();
  process.exit(0);
}});
setInterval(() => {{}}, 1000);
"""


def _miniflare_probe_script(
    bundle: Path,
    miniflare: Path,
    port: int,
    control_port: int,
    schema: Path,
    ca_cert: Path,
) -> str:
    """Return a non-secret Node harness for the real Worker runtime probe."""
    return (
        _probe_script_header(miniflare)
        + _probe_script_forwarder(ca_cert)
        + _probe_script_miniflare_setup(bundle, port, ca_cert)
        + _probe_script_d1_schema(schema)
        + _probe_script_control_server(control_port)
    )
