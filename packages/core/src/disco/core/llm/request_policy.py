"""Operator-declared wire options, independent of endpoint and model names."""

from __future__ import annotations

from copy import deepcopy

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

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


class RequestPolicy(BaseModel):
    """Explicit extensions for any compatible endpoint; absent means no extension.

    Reasoning dictionaries are opaque JSON declared by the operator, not a
    model registry. None means unspecified; an empty object explicitly means
    the endpoint has no switch for that state. No capability is inferred from
    a URL, provider label, or model identifier.
    """

    model_config = ConfigDict(extra="forbid")

    body: dict[str, JsonValue] = Field(default_factory=dict)
    reasoning_enabled: dict[str, JsonValue] | None = None
    reasoning_disabled: dict[str, JsonValue] | None = None
    default_reasoning: bool | None = None
    require_user_continuation: bool = False
    cache_control: bool = False

    @field_validator("body", "reasoning_enabled", "reasoning_disabled")
    @classmethod
    def preserve_request_contract(cls, value):
        if value is not None and (overlap := _OWNED_FIELDS.intersection(value)):
            raise ValueError(f"Request policy cannot override owned fields: {sorted(overlap)}")
        return value

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
