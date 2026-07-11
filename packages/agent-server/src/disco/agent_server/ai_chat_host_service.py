"""WO-A4 metered ``ai.chat`` host service.

The generated app receives only a scoped bus capability. Provider credentials,
model routing, and usage accounting stay on the host. This adapter deliberately
offers no tools, model override, provider parameters, or arbitrary metadata.
"""

from __future__ import annotations

from typing import Any, Literal

from disco.core.host_services import (
    HostServiceContext,
    HostServiceDefinition,
    HostServiceUsage,
    register_host_service,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

AI_CHAT_SERVICE_NAME = "ai.chat"
_MAX_MESSAGES = 32
_MAX_MESSAGE_BYTES = 8_192
_MAX_TOTAL_MESSAGE_BYTES = 32_768
_MAX_OUTPUT_TOKENS = 4_096
_HOST_PROMPT_RESERVE_TOKENS = 2_048


class AiChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)

    @model_validator(mode="after")
    def _bounded_content(self) -> AiChatMessage:
        if len(self.content.encode("utf-8")) > _MAX_MESSAGE_BYTES:
            raise ValueError("ai.chat message is too large")
        return self


class AiChatPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    messages: list[AiChatMessage] = Field(min_length=1, max_length=_MAX_MESSAGES)
    max_tokens: int = Field(default=512, ge=1, le=_MAX_OUTPUT_TOKENS)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)

    @model_validator(mode="after")
    def _bounded_conversation(self) -> AiChatPayload:
        if not any(message.role == "user" for message in self.messages):
            raise ValueError("ai.chat requires a user message")
        if sum(len(message.content.encode("utf-8")) for message in self.messages) > (
            _MAX_TOTAL_MESSAGE_BYTES
        ):
            raise ValueError("ai.chat conversation is too large")
        return self


def estimate_ai_chat_usage(payload: dict[str, Any]) -> HostServiceUsage:
    """Conservatively reserve input bytes plus bounded host-prompt overhead."""
    raw_messages = payload.get("messages")
    message_bytes = 0
    if isinstance(raw_messages, list):
        for raw in raw_messages[:_MAX_MESSAGES]:
            if isinstance(raw, dict):
                content = raw.get("content")
                if isinstance(content, str):
                    message_bytes += min(len(content.encode("utf-8")), _MAX_MESSAGE_BYTES)
    raw_max = payload.get("max_tokens", 512)
    output = (
        raw_max
        if isinstance(raw_max, int)
        and not isinstance(raw_max, bool)
        and 1 <= raw_max <= _MAX_OUTPUT_TOKENS
        else _MAX_OUTPUT_TOKENS
    )
    # A byte is a safe upper bound for tokenizer tokens in the caller content;
    # the fixed reserve covers the host-owned role prompt and message framing.
    return HostServiceUsage(
        input_tokens=message_bytes + _HOST_PROMPT_RESERVE_TOKENS,
        output_tokens=output,
    )


def read_ai_chat_usage(result: dict[str, Any]) -> HostServiceUsage:
    raw = result.get("usage")
    if not isinstance(raw, dict):
        raise ValueError("ai.chat result omitted usage")
    input_tokens = raw.get("input_tokens")
    output_tokens = raw.get("output_tokens")
    if (
        not isinstance(input_tokens, int)
        or isinstance(input_tokens, bool)
        or input_tokens < 0
        or not isinstance(output_tokens, int)
        or isinstance(output_tokens, bool)
        or output_tokens < 0
    ):
        raise ValueError("ai.chat result contained invalid usage")
    return HostServiceUsage(input_tokens=input_tokens, output_tokens=output_tokens)


async def _ai_chat_handler(payload: dict[str, Any], ctx: HostServiceContext) -> dict[str, Any]:
    complete = ctx.ai_chat_complete
    if complete is None:
        return {"ok": False, "error": "ai_unavailable"}
    return await complete(payload)


AI_CHAT_SERVICE = HostServiceDefinition(
    name=AI_CHAT_SERVICE_NAME,
    handler=_ai_chat_handler,
    description="Generate one bounded, tool-free chat completion through host-owned inference.",
    payload_schema=AiChatPayload,
    estimate_usage=estimate_ai_chat_usage,
    read_usage=read_ai_chat_usage,
    timeout_s=90.0,
)
register_host_service(AI_CHAT_SERVICE)


__all__ = [
    "AI_CHAT_SERVICE",
    "AI_CHAT_SERVICE_NAME",
    "AiChatMessage",
    "AiChatPayload",
    "estimate_ai_chat_usage",
    "read_ai_chat_usage",
]
