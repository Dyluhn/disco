"""MCP server config — RP-05 rung A (the configuration surface).

McpServerConfig is the per-server Pydantic model the Settings UI writes and the
agent-server reads. McpSettings is the top-level block on RouterConfig (placed in
core/llm/config.py — same shared ConfigStore as the model catalogue).
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from disco.core import SecurityRisk
from pydantic import BaseModel, Field, field_validator, model_validator

# SecretRef = a key name in the SecretsStore — never a raw value (§6).
# The loader resolves it at pool-start time; the raw secret never touches the config.
SecretRef = str

# Server-name regex: only [a-z0-9_] — LibreChat's naming bugs make this
# non-negotiable (the brief is explicit).
_SERVER_NAME_RE = re.compile(r"^[a-z0-9_]+$")

# Transport values we accept (the brief forbids SSE).
_VALID_TRANSPORTS: frozenset[str] = frozenset({"stdio", "streamable_http"})


class McpServerConfig(BaseModel):
    """One MCP server — the shape the UI writes and the pool reads."""

    model_config = {"frozen": True, "hide_input_in_errors": True}

    name: str
    transport: Literal["stdio", "streamable_http"]

    # ---- stdio ---------------------------------------------------------------
    command: list[str] | None = None
    args: list[str] | None = None
    env: dict[str, SecretRef] | None = None  # key -> SecretsStore key name

    # ---- streamable_http -----------------------------------------------------
    url: str | None = None  # https-only in non-dev profiles
    headers: dict[str, SecretRef] | None = None  # key -> SecretsStore key name
    allowed_hosts: list[str] | None = None

    # ---- lifecycle ------------------------------------------------------------
    enabled: bool = True
    allowed_tools: list[str] | None = None  # None = all
    risk_tier: SecurityRisk  # REQUIRED; not inferred (the brief is explicit)
    description_hash: str | None = None  # filled by the approval flow

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not _SERVER_NAME_RE.match(v):
            raise ValueError(
                f"MCP server name {v!r} must match [a-z0-9_] pattern; "
                f"no hyphens, dots, or uppercase"
            )
        return v

    @field_validator("transport")
    @classmethod
    def _reject_sse(cls, v: str) -> str:
        if v not in _VALID_TRANSPORTS:
            if v == "sse":
                raise ValueError("SSE transport is not supported in MCP v1; use streamable_http")
            raise ValueError(f"Unknown transport {v!r}; must be stdio or streamable_http")
        return v

    @field_validator("risk_tier", mode="before")
    @classmethod
    def _normalize_risk_tier(cls, v: object) -> object:
        # The Settings DTO persists lower-case wire values while SecurityRisk's
        # enum values are upper-case. Accept both representations so the exact
        # config the operator approved can be loaded by the agent server.
        return v.upper() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _validate_transport_target(self) -> McpServerConfig:
        if self.transport == "stdio":
            if not self.command or not self.command[0].strip():
                raise ValueError("stdio transport requires a non-empty command")
            return self
        raw = self.url or ""
        try:
            parsed = urlsplit(raw)
            hostname = parsed.hostname
            # Accessing ``port`` makes urllib reject malformed/non-numeric
            # ports instead of leaving the failure for a later connection.
            port = parsed.port
        except ValueError as exc:
            raise ValueError(
                "streamable_http URL must be an absolute HTTP(S) URL without "
                "userinfo, whitespace, or a fragment"
            ) from exc
        if (
            not raw
            or raw != raw.strip()
            or any(character.isspace() or ord(character) < 32 for character in raw)
            or "\\" in raw
            or "#" in raw
            or parsed.scheme not in {"http", "https"}
            or hostname is None
            or port is not None and not 1 <= port <= 65535
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError(
                "streamable_http URL must be an absolute HTTP(S) URL without "
                "userinfo, whitespace, or a fragment"
            )
        return self


class McpSettings(BaseModel):
    """The MCP block on RouterConfig — off by default."""

    model_config = {"frozen": True}

    enabled: bool = False  # off by default — opt-in
    servers: dict[str, McpServerConfig] = Field(default_factory=dict)
    max_active_schemas: int = 20  # cap; beyond this, tool_search is exposed
