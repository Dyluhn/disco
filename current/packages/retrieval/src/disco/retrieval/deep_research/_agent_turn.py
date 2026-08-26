"""Single model turn and precise parse retry."""

from __future__ import annotations

from typing import Any

from disco.core import LLMMessage
from disco.core.llm import LLMRouter

from ._agent_parsing import Turn, parse_turn


async def one_model_turn(
    router: LLMRouter,
    system_prompt: str,
    user_message: str,
    *,
    expect_brief: bool,
    namespace: str,
    complete_turn: Any,
    record_turn_io: Any,
    first_schema: str,
    later_schema: str,
) -> tuple[Turn | None, str | None]:
    messages = [
        LLMMessage(role="system", content=system_prompt),
        LLMMessage(role="user", content=user_message),
    ]
    text, response, latency_ms = await complete_turn(router, messages, namespace=namespace)
    parsed, error = parse_turn(text, expect_brief=expect_brief)
    record_turn_io(
        namespace,
        messages,
        text,
        response,
        parsed=parsed,
        error=error,
        attempt=1,
        latency_ms=latency_ms,
    )
    if parsed is not None:
        return parsed, None
    schema = first_schema if expect_brief else later_schema
    retry_messages = [
        *messages,
        LLMMessage(
            role="user",
            content=(
                f"Your response could not be used: {error}. Reply again with ONE strict "
                f"JSON object matching exactly this schema: {schema}"
            ),
        ),
    ]
    text, response, latency_ms = await complete_turn(router, retry_messages, namespace=namespace)
    parsed, error = parse_turn(text, expect_brief=expect_brief)
    record_turn_io(
        namespace,
        retry_messages,
        text,
        response,
        parsed=parsed,
        error=error,
        attempt=2,
        latency_ms=latency_ms,
    )
    return parsed, error
