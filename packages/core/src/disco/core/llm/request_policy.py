"""Operator-declared wire options, independent of endpoint and model names."""

from __future__ import annotations

import re
from copy import deepcopy

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

_OWNED_FIELDS = frozenset(
    {
        "model",
        "messages",
        "input",
        "tools",
        "stream",
        "max_tokens",
        "max_output_tokens",
        "temperature",
        "response_format",
        "text",
        "tool_choice",
    }
)

_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

# Credentials and transport framing the operator must never override.
_RESERVED_HEADERS = frozenset(
    {
        "content-type",
        "authorization",
        "host",
        "content-length",
        "connection",
        "transfer-encoding",
        "cookie",
    }
)

# Identity headers owned by the adapter's stable session/conversation wiring.
_DISCO_OWNED_HEADERS = frozenset(
    {
        "x-disco-session",
        "x-disco-request",
        "x-disco-conversation",
    }
)


def _check_header_name(name: object, *, field: str) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError(f"{field} header name must be a non-empty string")
    if not _TOKEN_RE.fullmatch(name):
        raise ValueError(f"{field} header name is not a valid token: {name!r}")
    lowered = name.lower()
    if lowered in _RESERVED_HEADERS:
        raise ValueError(f"{field} header is reserved and cannot be overridden: {name!r}")
    if lowered in _DISCO_OWNED_HEADERS:
        raise ValueError(f"{field} header is owned by Disco identity wiring: {name!r}")
    return name


def _check_header_value(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"header value for {name!r} must be a string")
    for char in value:
        if not 32 <= ord(char) <= 126:
            raise ValueError(f"header value for {name!r} must contain printable ASCII characters")
    return value


class RequestPolicy(BaseModel):
    """Explicit extensions for any compatible endpoint; absent means no extension.

    Reasoning dictionaries are opaque JSON declared by the operator, not a
    model registry. None means unspecified; an empty object explicitly means
    the endpoint has no switch for that state. No capability is inferred from
    a URL, provider label, or model identifier.

    Transport dictionaries are explicit and provider-neutral: ``headers``
    carries non-secret static headers (for example a caller-declared
    User-Agent), and ``session_header`` names one extra header that receives
    the existing stable session identity. Both are absent by default, which
    leaves the historical wire headers unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    body: dict[str, JsonValue] = Field(default_factory=dict)
    reasoning_enabled: dict[str, JsonValue] | None = None
    reasoning_disabled: dict[str, JsonValue] | None = None
    default_reasoning: bool | None = None
    require_user_continuation: bool = False
    cache_control: bool = False
    headers: dict[str, str] = Field(default_factory=dict)
    session_header: str | None = None

    @field_validator("body", "reasoning_enabled", "reasoning_disabled")
    @classmethod
    def preserve_request_contract(cls, value):
        if value is not None and (overlap := _OWNED_FIELDS.intersection(value)):
            raise ValueError(f"Request policy cannot override owned fields: {sorted(overlap)}")
        return value

    @field_validator("headers")
    @classmethod
    def validate_static_headers(cls, value: dict[str, str]) -> dict[str, str]:
        seen: set[str] = set()
        for name, header_value in value.items():
            _check_header_name(name, field="headers")
            _check_header_value(header_value, name=name)
            lowered = name.lower()
            if lowered in seen:
                raise ValueError(f"headers contains a case-insensitive duplicate key: {name!r}")
            seen.add(lowered)
        return value

    @field_validator("session_header")
    @classmethod
    def validate_session_header(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _check_header_name(value, field="session_header")
        return value

    @model_validator(mode="after")
    def check_session_header_collision(self) -> RequestPolicy:
        if self.session_header is not None:
            lowered = self.session_header.lower()
            for name in self.headers:
                if name.lower() == lowered:
                    raise ValueError(
                        "session_header collides with a static headers entry: "
                        f"{self.session_header!r}"
                    )
        return self

    def transport_headers(
        self, headers: dict[str, str], *, session: str, conversation_header: str
    ) -> dict[str, str]:
        """Copy owned headers and add explicitly declared transport extensions."""
        result = dict(headers)
        owned = {key.lower() for key in result} | {conversation_header.lower()}
        for name, value in self.headers.items():
            if name.lower() in owned:
                raise ValueError(f"static header collides with an owned header: {name!r}")
            result[name] = value
            owned.add(name.lower())
        if self.session_header is not None and session:
            alias = self.session_header
            if alias.lower() in owned:
                raise ValueError(f"session_header collides with an owned header: {alias!r}")
            result[alias] = session
        return result

    def payload(self, enabled: bool | None) -> dict:
        effective = self.default_reasoning if enabled is None else enabled
        extra = self.reasoning_enabled if effective is True else self.reasoning_disabled
        result = deepcopy(self.body)
        if effective is not None and extra is not None:
            _merge(result, extra)
        return result


def _merge(target: dict, extra: dict) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = deepcopy(value)
