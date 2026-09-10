"""Paste-a-config MCP import — parse the standard ``mcpServers`` JSON blob.

Every MCP server README ships one of a handful of JSON shapes:

    {"mcpServers": {"name": {"command": "npx", "args": [...], "env": {...}}}}
    {"servers": {...}}                       (VS Code / Copilot variant)
    {"name": {"command": ...}}               (the bare inner map)
    {"command": "npx -y @scope/server"}      (a single server, no wrapper)
    {"my_api": {"url": "https://...", "headers": {"Authorization": "..."}}}

This module turns any of those into candidate rows for the EXISTING create
path (`McpConfigService.create_mcp_server`) — it never persists anything
itself. Secrets policy: pasted literal env/header values are NEVER stored in
the config; each becomes a SecretStore entry under a generated ref name and
the config carries only the ref (the same secret-ref machinery the stdio pool
and HTTP client already resolve). ``${VAR}`` placeholders become the ref
``VAR`` with no value to store.

Everything here is module-private except :func:`parse_mcp_import`; the wire
DTO stays a plain dict assembled by `McpConfigService.import_mcp_config`, so
this feature adds no scanned public surface on the Python side.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any
from urllib.parse import urlsplit

# Keys a README server block may use for the remote endpoint.
_URL_KEYS = ("url", "serverUrl", "httpUrl", "endpoint")
# Transport spellings seen in the wild → our canonical transport.
_TRANSPORT_ALIASES = {
    "stdio": "stdio",
    "http": "streamable_http",
    "streamable-http": "streamable_http",
    "streamable_http": "streamable_http",
    "streamablehttp": "streamable_http",
}
_NAME_OK_RE = re.compile(r"^[a-z0-9_]+$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER_RE = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?$")
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*$")
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


class McpImportError(ValueError):
    """The pasted text cannot be interpreted as MCP server config at all."""


def _strip_markdown_fences(text: str) -> str:
    """Drop ```json fences so a README block can be pasted whole."""
    lines = text.strip().splitlines()
    kept = [line for line in lines if not _FENCE_RE.match(line.strip())]
    return "\n".join(kept).strip()


def _loads_tolerant(text: str) -> Any:
    """json.loads with one mercy pass for trailing commas (a common paste)."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        relaxed = _TRAILING_COMMA_RE.sub(r"\1", text)
        if relaxed != text:
            try:
                return json.loads(relaxed)
            except json.JSONDecodeError:
                pass
        raise McpImportError(
            f"not valid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"
        ) from exc


def _looks_like_server(obj: dict) -> bool:
    if "command" in obj or any(k in obj for k in _URL_KEYS):
        return True
    return isinstance(obj.get("type") or obj.get("transport"), str)


def _extract_server_map(doc: Any) -> dict[str, dict]:
    """Find the {name: server} map inside whatever wrapper was pasted."""
    if not isinstance(doc, dict):
        raise McpImportError(
            "top level must be a JSON object — got "
            f"{type(doc).__name__}; expected the {{\"mcpServers\": ...}} shape"
        )
    for wrapper in ("mcpServers", "servers", "mcp_servers"):
        inner = doc.get(wrapper)
        if isinstance(inner, dict):
            if not inner:
                raise McpImportError(f'"{wrapper}" is present but contains no servers')
            return {str(k): v for k, v in inner.items()}
    if _looks_like_server(doc):
        return {"": doc}
    if doc and all(isinstance(v, dict) for v in doc.values()):
        return {str(k): v for k, v in doc.items()}
    raise McpImportError(
        'no servers found: expected {"mcpServers": {...}}, a name → server '
        'map, or a single server object with "command" or "url"'
    )


def _normalize_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", name.strip().lower()).strip("_")
    return re.sub(r"_{2,}", "_", slug)


def _derive_name(server: dict) -> str:
    """A name for the wrapper-less single-server paste, from its target."""
    command = server.get("command")
    if isinstance(command, list) and command:
        command = command[0]
    if isinstance(command, str) and command.strip():
        tokens = command.split()
        # Prefer the package/script over a generic launcher (npx/uvx/node...).
        candidate = tokens[-1] if len(tokens) > 1 else tokens[0]
        args = server.get("args")
        if isinstance(args, list):
            for arg in args:
                if isinstance(arg, str) and not arg.startswith("-"):
                    candidate = arg
                    break
        return _normalize_name(candidate.rsplit("/", 1)[-1])
    for key in _URL_KEYS:
        raw_url = server.get(key)
        if isinstance(raw_url, str) and raw_url.strip():
            return _normalize_name(urlsplit(raw_url.strip()).hostname or "")
    return ""


def _detect_transport(server: dict) -> tuple[str | None, str | None]:
    """Return (transport, error). SSE and unknown transports are named errors."""
    declared = server.get("type") or server.get("transport")
    if isinstance(declared, str) and declared.strip():
        key = declared.strip().lower()
        if key == "sse":
            return None, (
                "SSE transport is not supported; if the server offers a "
                "streamable HTTP endpoint, use that URL instead"
            )
        transport = _TRANSPORT_ALIASES.get(key)
        if transport is None:
            return None, f"unknown transport {declared!r}; must be stdio or streamable HTTP"
        return transport, None
    if "command" in server:
        return "stdio", None
    if any(isinstance(server.get(k), str) for k in _URL_KEYS):
        return "streamable_http", None
    return None, (
        'needs either "command" (stdio server) or "url" (remote streamable '
        "HTTP server)"
    )


def _secret_ref_for(server_name: str, key: str) -> str:
    slug = _normalize_name(key) or "value"
    return f"mcp_{server_name}_{slug}"


def _parse_secret_map(
    raw: Any,
    *,
    field: str,
    server_name: str,
    warnings: list[str],
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Map a pasted env/headers dict to {key: secret_ref} + secrets to store.

    A ``${VAR}`` placeholder references the existing secret ``VAR``; any other
    non-empty literal is queued for the SecretStore under a generated ref so a
    raw API key never lands in the persisted config.
    """
    refs: dict[str, str] = {}
    pending: list[dict[str, Any]] = []
    if raw is None:
        return refs, pending
    if not isinstance(raw, dict):
        warnings.append(f'"{field}" must be an object of key → value; ignored')
        return refs, pending
    for key, value in raw.items():
        key = str(key)
        text = "" if value is None else str(value)
        placeholder = _PLACEHOLDER_RE.match(text.strip())
        if placeholder:
            refs[key] = placeholder.group(1)
            continue
        ref = _secret_ref_for(server_name, key)
        refs[key] = ref
        if text.strip():
            pending.append({"ref": ref, "source_key": key, "value": text})
        else:
            warnings.append(
                f'{field} {key!r} has no value; store a secret named {ref!r} '
                "(Settings → secrets) before connecting"
            )
    return refs, pending


def _stdio_target(server: dict, warnings: list[str]) -> tuple[str, list[str] | None] | None:
    """Return (executable-or-command-line, explicit_args) for the create DTO."""
    command = server.get("command")
    args = server.get("args")
    if isinstance(command, list):
        if not command or not str(command[0]).strip():
            return None
        rest = [str(part) for part in command[1:]]
        explicit = rest + [str(a) for a in args] if isinstance(args, list) else rest
        return str(command[0]), explicit
    if not isinstance(command, str) or not command.strip():
        return None
    if isinstance(args, list):
        return command.strip(), [str(a) for a in args]
    if args is not None:
        warnings.append('"args" must be a list; ignored')
    # No args given: keep the whole line — the service shlex-splits it, the
    # same path the manual Settings box uses.
    try:
        shlex.split(command)
    except ValueError as exc:
        warnings.append(f"command line could not be parsed: {exc}")
    return command.strip(), None


def _resolve_row_name(name: str, raw: dict, warnings: list[str]) -> str | None:
    """The normalized [a-z0-9_] server name for a row — None when undecidable."""
    original_name = name
    if not name:
        name = _derive_name(raw)
        if name:
            warnings.append(f"no server name in the paste; using {name!r}")
    normalized = _normalize_name(name)
    if normalized and normalized != name and original_name:
        warnings.append(f"server name {original_name!r} normalized to {normalized!r}")
    return normalized or None


def _dropped_key_warnings(raw: dict, warnings: list[str]) -> None:
    """Name every meaningful key the import DOES NOT honor, instead of
    silently eating it."""
    for cwd_key in ("cwd", "workingDirectory", "working_dir"):
        if cwd_key in raw:
            warnings.append(
                f"{cwd_key!r} is not supported (stdio servers run in the app "
                "server's working directory); ignored"
            )
    if raw.get("disabled") is True:
        warnings.append('"disabled": true ignored — imported servers start enabled')


def _finish_stdio_row(row: dict[str, Any], raw: dict, warnings: list[str]) -> dict[str, Any]:
    target = _stdio_target(raw, warnings)
    if target is None:
        return {"name": row["name"], "error": 'stdio server needs a non-empty "command"'}
    row["url"], row["args"] = target
    env_refs, pending = _parse_secret_map(
        raw.get("env"), field="env", server_name=row["name"], warnings=warnings
    )
    row["env"] = env_refs or None
    row["secrets"] = pending
    if raw.get("headers"):
        warnings.append('"headers" only applies to remote servers; ignored')
    return row


def _finish_http_row(row: dict[str, Any], raw: dict, warnings: list[str]) -> dict[str, Any]:
    url = next(
        (raw[k].strip() for k in _URL_KEYS if isinstance(raw.get(k), str) and raw[k].strip()),
        None,
    )
    if url is None:
        return {"name": row["name"], "error": 'remote server needs a non-empty "url"'}
    row["url"], row["args"] = url, None
    header_refs, pending = _parse_secret_map(
        raw.get("headers"), field="header", server_name=row["name"], warnings=warnings
    )
    row["headers"] = header_refs or None
    row["secrets"] = pending
    if raw.get("env"):
        warnings.append('"env" only applies to stdio servers; ignored')
    return row


def _parse_one_server(name: str, raw: Any) -> dict[str, Any]:
    """One candidate row: either a config proposal or a named error."""
    if not isinstance(raw, dict):
        return {"name": name or "(unnamed)", "error": "server entry must be a JSON object"}

    warnings: list[str] = []
    resolved = _resolve_row_name(name, raw, warnings)
    if resolved is None:
        return {
            "name": name or "(unnamed)",
            "error": "could not determine a server name; wrap the config as "
            '{"mcpServers": {"your_name": {...}}}',
        }

    transport, transport_error = _detect_transport(raw)
    if transport_error is not None:
        return {"name": resolved, "error": transport_error}

    _dropped_key_warnings(raw, warnings)
    row: dict[str, Any] = {
        "name": resolved,
        "transport": transport,
        "warnings": warnings,
        "secrets": [],
    }
    if transport == "stdio":
        return _finish_stdio_row(row, raw, warnings)
    return _finish_http_row(row, raw, warnings)


def parse_mcp_import(text: str) -> list[dict[str, Any]]:
    """Parse a pasted MCP config blob into candidate server rows.

    Returns one row per server: either a proposal
    ``{name, transport, url, args, env, headers, warnings, secrets}`` (where
    ``secrets`` carries ``{ref, source_key, value}`` entries whose ``value``
    must go to the SecretStore, never the config) or a refusal
    ``{name, error}``. Raises :class:`McpImportError` when the paste is not
    interpretable at all — the message names exactly what is missing.
    """
    cleaned = _strip_markdown_fences(text or "")
    if not cleaned:
        raise McpImportError("nothing to import — paste the server's JSON config")
    doc = _loads_tolerant(cleaned)
    server_map = _extract_server_map(doc)
    return [_parse_one_server(str(name), raw) for name, raw in server_map.items()]
