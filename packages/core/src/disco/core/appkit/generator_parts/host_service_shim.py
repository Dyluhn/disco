"""Host-service client shim (WO-A2.3): `worker/disco-client.ts` + the Worker Env
augmentation that wires it in.

Split out of `..generator` (verbatim) to keep that module under the
`python_or_harness_module_logical_gt_700` budget, and to bring
`_emit_disco_client_ts` under the 100-logical-line callable cap. See
`generator_parts/__init__.py` for the byte-identity contract this split must
hold: every helper below is a straight, in-order cut of the ORIGINAL single
string concatenation — no character added, removed, or reordered.
Byte-identity is verified externally (hash comparison against the pre-split
generator), not by a test in this tree.

The generated Worker calls host services (email.send, ai.chat, payments.checkout)
over the WO-A2.2 bus endpoint (/_disco/svc/{service}) on the agent-server. The
shim module is Disco-owned, never authored by the model. It reads the bus URL
and bearer token from Worker env bindings (DISCO_SVC_BUS / DISCO_SVC_TOKEN) —
both are host-injected at sandbox provision time, never generated or committed.
The shim validates bus (URL origin only, HTTPS or loopback HTTP), token (legacy a2v0
or current a4v1 Bearer
format), service name (dotted grammar with underscores, max 128 chars), request
payload (JSON object ≤ 64 KiB serialized), and response (stream-bounded to 256
KiB via reader chunks, Content-Type must be application/json, top-level JSON
object required). Errors are sanitized: never echo bus/token/service/body/statusText.
Method/headers/auth/URL are internal — the caller supplies only service + payload.
The shim is emitted only when the resolved primitive has non-empty host_contract.
"""

from __future__ import annotations

_MAX_SERVICE_LEN = 128
_MAX_REQUEST_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 256 * 1024
_BUS_TIMEOUT_MS = 10_000


def _disco_client_prelude(
    max_service_len: int, max_request_bytes: int, max_response_bytes: int, bus_timeout_ms: int
) -> str:
    return (
        "/* Auto-generated Disco host-service client (WO-A2.3) — do NOT hand-edit;\n"
        "   regenerated from .disco/appspec.json. Worker-only code: NEVER import\n"
        "   from src/ (the Vite browser bundle cannot receive env bindings). */\n"
        "\n"
        "const SERVICE_RE = /^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)+$/;\n"
        "const TOKEN_RE = /^(?:a2v0|a4v1)\\.[A-Za-z0-9_-]{22}\\.[A-Za-z0-9_-]{43}$/;\n"
        f"const MAX_SERVICE_LEN = {max_service_len};\n"
        f"const MAX_REQUEST_BYTES = {max_request_bytes};\n"
        f"const MAX_RESPONSE_BYTES = {max_response_bytes};\n"
        f"const BUS_TIMEOUT_MS = {bus_timeout_ms};\n"
        "\n"
        "function statusClass(status: number): string {\n"
        '  if (status >= 500) return "5xx";\n'
        '  if (status >= 400) return "4xx";\n'
        '  return "unexpected";\n'
        "}\n"
        "\n"
        "function isLoopback(hostname: string): boolean {\n"
        '  if (hostname === "localhost" || hostname === "[::1]") return true;\n'
        '  const octets = hostname.split(".");\n'
        '  return octets.length === 4 && octets[0] === "127"\n'
        "    && octets.every((part) => /^(?:0|[1-9][0-9]{0,2})$/.test(part))\n"
        "    && octets.every((part) => Number(part) <= 255);\n"
        "}\n"
        "\n"
        "async function cancelBody(response: Response): Promise<void> {\n"
        "  try { await response.body?.cancel(); } catch { /* sanitized below */ }\n"
        "}\n"
        "\n"
        "async function cancelReader(\n"
        "  reader: ReadableStreamDefaultReader<Uint8Array>,\n"
        "): Promise<void> {\n"
        "  try { await reader.cancel(); } catch { /* sanitized below */ }\n"
        "}\n"
        "\n"
    )


def _disco_client_svc_open_and_validate() -> str:
    return (
        "export async function svc(\n"
        "  env: { DISCO_SVC_BUS?: string; DISCO_SVC_TOKEN?: string },\n"
        "  service: string,\n"
        "  payload: unknown,\n"
        "): Promise<unknown> {\n"
        "  const bus = env.DISCO_SVC_BUS;\n"
        '  if (!bus || typeof bus !== "string") {\n'
        '    throw new Error("host service unavailable");\n'
        "  }\n"
        "  let busUrl: URL;\n"
        "  try { busUrl = new URL(bus); } catch {\n"
        '    throw new Error("host service unavailable");\n'
        "  }\n"
        "  if (\n"
        '    (busUrl.protocol !== "http:" && busUrl.protocol !== "https:")\n'
        "    || busUrl.username || busUrl.password\n"
        "    || busUrl.search || busUrl.hash\n"
        '    || (busUrl.pathname !== "/" && busUrl.pathname !== "")\n'
        '    || (busUrl.protocol === "http:" && !isLoopback(busUrl.hostname))\n'
        "  ) {\n"
        '    throw new Error("host service unavailable");\n'
        "  }\n"
        "\n"
        "  const token = env.DISCO_SVC_TOKEN;\n"
        '  if (!token || typeof token !== "string" || !TOKEN_RE.test(token)) {\n'
        '    throw new Error("host service unavailable");\n'
        "  }\n"
        "\n"
        "  if (\n"
        '    typeof service !== "string"\n'
        "    || service.length === 0\n"
        "    || service.length > MAX_SERVICE_LEN\n"
        "    || !SERVICE_RE.test(service)\n"
        "  ) {\n"
        '    throw new Error("invalid service");\n'
        "  }\n"
        "\n"
        "  if (\n"
        '    typeof payload !== "object"\n'
        "    || payload === null\n"
        "    || Array.isArray(payload)\n"
        "  ) {\n"
        '    throw new Error("invalid payload");\n'
        "  }\n"
        "  let bodyStr: string | undefined;\n"
        "  try {\n"
        "    bodyStr = JSON.stringify(payload);\n"
        "  } catch {\n"
        '    throw new Error("invalid payload");\n'
        "  }\n"
        '  if (typeof bodyStr !== "string") {\n'
        '    throw new Error("invalid payload");\n'
        "  }\n"
        "  let serialized: unknown;\n"
        "  try { serialized = JSON.parse(bodyStr); } catch {\n"
        '    throw new Error("invalid payload");\n'
        "  }\n"
        "  if (\n"
        '    typeof serialized !== "object"\n'
        "    || serialized === null\n"
        "    || Array.isArray(serialized)\n"
        "  ) {\n"
        '    throw new Error("invalid payload");\n'
        "  }\n"
        "  const encoder = new TextEncoder();\n"
        "  const bodyBytes = encoder.encode(bodyStr);\n"
        "  if (bodyBytes.byteLength > MAX_REQUEST_BYTES) {\n"
        '    throw new Error("payload too large");\n'
        "  }\n"
        "\n"
    )


def _disco_client_svc_fetch() -> str:
    return (
        "  const url = `${busUrl.origin}/_disco/svc/${encodeURIComponent(service)}`;\n"
        "\n"
        "  const controller = new AbortController();\n"
        "  const timer = setTimeout(() => controller.abort(), BUS_TIMEOUT_MS);\n"
        "  try {\n"
        "    let response: Response;\n"
        "    try {\n"
        "      response = await fetch(url, {\n"
        '      method: "POST",\n'
        "      headers: {\n"
        '        "Content-Type": "application/json",\n'
        '        "Authorization": `Bearer ${token}`,\n'
        "      },\n"
        '      redirect: "manual",\n'
        "      signal: controller.signal,\n"
        '      cache: "no-store",\n'
        "      body: bodyBytes,\n"
        "      });\n"
        "    } catch {\n"
        '      throw new Error("host service unavailable");\n'
        "    }\n"
        "\n"
        '    const ct = response.headers.get("Content-Type") || "";\n'
        '    const mediaType = ct.split(";", 1)[0].trim().toLowerCase();\n'
        '    if (mediaType !== "application/json") {\n'
        "      await cancelBody(response);\n"
        '      throw new Error("host service error");\n'
        "    }\n"
        "\n"
        "    if (!response.ok) {\n"
        "      await cancelBody(response);\n"
        "      throw new Error(`host service returned ${statusClass(response.status)}`);\n"
        "    }\n"
        "\n"
    )


def _disco_client_svc_read_response() -> str:
    return (
        "    if (!response.body) {\n"
        '      throw new Error("host service error");\n'
        "    }\n"
        "    let reader: ReadableStreamDefaultReader<Uint8Array>;\n"
        "    try { reader = response.body.getReader(); } catch {\n"
        "      await cancelBody(response);\n"
        '      throw new Error("host service error");\n'
        "    }\n"
        "    const chunks: Uint8Array[] = [];\n"
        "    let total = 0;\n"
        "    try {\n"
        "      while (true) {\n"
        "        const { done, value } = await reader.read();\n"
        "        if (done) break;\n"
        "        total += value.byteLength;\n"
        "        if (total > MAX_RESPONSE_BYTES) {\n"
        "          await cancelReader(reader);\n"
        '          throw new Error("host service error");\n'
        "        }\n"
        "        chunks.push(value);\n"
        "      }\n"
        "    } catch {\n"
        "      await cancelReader(reader);\n"
        '      throw new Error("host service error");\n'
        "    }\n"
        "\n"
        "    let text: string;\n"
        "    try {\n"
        '      const decoder = new TextDecoder("utf-8", { fatal: true });\n'
        '      text = "";\n'
        "      for (const chunk of chunks) {\n"
        "        text += decoder.decode(chunk, { stream: true });\n"
        "      }\n"
        "      text += decoder.decode();\n"
        "    } catch {\n"
        '      throw new Error("host service error");\n'
        "    }\n"
        "\n"
        "    let parsed: unknown;\n"
        "    try { parsed = JSON.parse(text); } catch {\n"
        '      throw new Error("host service error");\n'
        "    }\n"
        "    if (\n"
        '      typeof parsed !== "object"\n'
        "      || parsed === null\n"
        "      || Array.isArray(parsed)\n"
        "    ) {\n"
        '      throw new Error("host service error");\n'
        "    }\n"
        "    return parsed;\n"
        "  } finally {\n"
        "    clearTimeout(timer);\n"
        "  }\n"
        "}\n"
    )


def _emit_disco_client_ts() -> str:
    """The generated Worker host-service client shim (WO-A2.3).

    Pure, deterministic, Disco-owned. Every security invariant is baked into the
    emitted code — see the module comment block for the full list. The caller
    cannot supply URL/method/headers/auth; all are internal to this module.
    """
    return (
        _disco_client_prelude(
            _MAX_SERVICE_LEN, _MAX_REQUEST_BYTES, _MAX_RESPONSE_BYTES, _BUS_TIMEOUT_MS
        )
        + _disco_client_svc_open_and_validate()
        + _disco_client_svc_fetch()
        + _disco_client_svc_read_response()
    )


def _augment_worker_env_with_host_svc(worker_ts: str) -> str:
    """Add DISCO_SVC_BUS and DISCO_SVC_TOKEN to the Worker's Env interface.

    Pure: reads a Worker TS string and returns an augmented copy. The Env interface
    is modified in place (env bindings are optional — fail closed when unset).
    """
    block = (
        "  // Host service bus (WO-A2.3): injected by the host at provision time.\n"
        "  DISCO_SVC_BUS?: string;\n"
        "  DISCO_SVC_TOKEN?: string;\n"
    )
    admin_marker = "  ADMIN_TOKEN?: string;\n"
    if admin_marker in worker_ts:
        return worker_ts.replace(admin_marker, admin_marker + block)
    assets_marker = "  ASSETS: { fetch: (req: Request) => Promise<Response> };\n"
    if assets_marker in worker_ts:
        return worker_ts.replace(assets_marker, assets_marker + block)
    msg = "cannot locate Env interface in worker/index.ts"
    raise ValueError(msg)


def _apply_host_services_shim(tree: dict[str, str]) -> None:
    """Add the Disco host-service client shim + env bindings to a generated tree.

    Called from ``generate()`` when the resolved primitive declares a non-empty
    ``host_contract``. Mutates *tree* in place. Adds
    ``worker/disco-client.ts`` and augments the ``worker/index.ts`` Env interface.
    ``DISCO_SVC_BUS`` and ``DISCO_SVC_TOKEN`` are host-injected runtime bindings
    — they are never written to ``wrangler.toml`` or any generated config."""
    worker_ts = tree.get("worker/index.ts")
    if worker_ts is None:
        raise ValueError("host-service primitive must emit worker/index.ts")
    tree["worker/disco-client.ts"] = _emit_disco_client_ts()
    tree["worker/index.ts"] = _augment_worker_env_with_host_svc(worker_ts)
