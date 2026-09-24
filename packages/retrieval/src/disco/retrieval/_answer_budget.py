"""Pre-dispatch budgets for Standard Search, using declared role capacities."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from disco.core.events import LLMMessage
from disco.core.llm import CapabilityProfile, CompletionRequest, LLMContextWindowExceeded, ModelRole

from .models import Passage


def _limit(router: Any, name: str, role: ModelRole) -> int | None:
    read = getattr(router, name, None)
    value = read(role) if callable(read) else None
    return value if type(value) is int and value > 0 else None


def _message_capacity(messages: Sequence[LLMMessage]) -> int:
    # Deliberately budget one UTF-8 byte per token, plus bounded chat framing.
    # An English chars/4 estimate undercounts source IDs, URLs, Unicode and small
    # vocabularies. This is conservative packing, not a claimed exact tokenizer.
    return 128 + sum(64 + len((message.content or "").encode("utf-8")) for message in messages)


def _budgets(router: Any, role: ModelRole, requested_output: int) -> tuple[int | None, int]:
    context = _limit(router, "context_token_limit", role)
    declared_output = _limit(router, "output_token_limit", role)
    output = min(requested_output, declared_output or requested_output)
    if context is not None:
        output = min(output, max(1, context // 4))
    return context, output


def _prefix(text: str, byte_limit: int) -> str:
    return text.encode("utf-8")[: max(0, byte_limit)].decode("utf-8", errors="ignore")


def _fit_passages(
    passages: Sequence[Passage],
    render: Callable[[Sequence[Passage]], list[LLMMessage]],
    input_limit: int,
) -> list[Passage]:
    if _message_capacity(render([])) > input_limit:
        raise LLMContextWindowExceeded(
            "The configured answer context cannot fit the question and required instructions; "
            "use a larger context or shorten the question. No inference was sent."
        )
    selected: list[Passage] = []
    for passage in passages:
        cost = _message_capacity(render([*selected, passage]))
        if cost <= input_limit:
            selected.append(passage)
            continue
        available = len(passage.text.encode("utf-8")) - (cost - input_limit)
        excerpt = _prefix(passage.text, available)
        if excerpt.strip():
            selected.append(
                passage.model_copy(
                    update={
                        "text": excerpt,
                        "char_end": passage.char_start + len(excerpt)
                        if passage.char_start is not None
                        else None,
                    }
                )
            )
    if passages and not selected:
        raise LLMContextWindowExceeded(
            "The configured answer context has no room for readable source evidence; "
            "use a larger context or shorten the question. No inference was sent."
        )
    return selected


def answer_request(
    router: Any,
    passages: Sequence[Passage],
    render: Callable[[Sequence[Passage]], list[LLMMessage]],
    *,
    think: bool,
    attempt: int,
) -> tuple[CompletionRequest, list[Passage]]:
    requested = 4096 if think else 1200
    if attempt > 1:
        requested = max(4096, requested * 2)
    context, output = _budgets(router, ModelRole.RAG_ANSWERER, requested)
    selected = (
        list(passages) if context is None else _fit_passages(passages, render, context - output)
    )
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        messages=render(selected),
        temperature=0.0,
        enable_thinking=think if attempt == 1 else False,
        max_tokens=output,
        attempt=attempt,
        stream=True,
    ), selected


def followup_request(router: Any, query: str, answer: str, attempt: int) -> CompletionRequest:
    context, output = _budgets(router, ModelRole.SUMMARIZER, 160 if attempt == 1 else 2048)

    def messages(excerpt: str) -> list[LLMMessage]:
        return [
            LLMMessage(role="system", content="Suggest concise follow-up research questions."),
            LLMMessage(
                role="user",
                content=(
                    "Given this question and answer, suggest 3 short follow-up questions, "
                    f"one per line, no numbering.\n\nQ: {query}\n\nA: {excerpt}"
                ),
            ),
        ]

    excerpt = answer[:1500]
    if context is not None:
        remaining = context - output - _message_capacity(messages(""))
        if remaining < 0:
            raise LLMContextWindowExceeded("Configured follow-up context cannot fit the question.")
        excerpt = _prefix(excerpt, remaining)
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.SUMMARIZER),
        messages=messages(excerpt),
        temperature=0.3,
        max_tokens=output,
        enable_thinking=False,
        attempt=attempt,
    )
