"""Prompt assembly (WPP-2).

The Build loop assembles the model prompt from the SAME ordered parts every time:

    stable system prefix
    → WorkflowPromptPack (the mode-specific operating manual, WPP-1)
    → ContextPack block (the compact run state, CXT-4 render_context_pack)
    → recent turns (the live user/assistant messages)
    → [allowed tool schema — supplied to the driver as its `tools`, not a message]

Pure + deterministic: identical inputs → identical messages (the kernel-neutrality
guarantee). Each structured part appears EXACTLY once and the stable prefix never
reorders, so the prompt prefix stays cache-stable.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..events import LLMMessage
from .prompt_pack import PromptPack


def assemble_workflow_prompt(
    *,
    system_prefix: str,
    prompt_pack: PromptPack | None = None,
    context_pack_block: str | None = None,
    recent_turns: Sequence[LLMMessage] = (),
) -> list[LLMMessage]:
    """Assemble the ordered message list both kernels send to the driver.

    - ``system_prefix``: the stable system prompt (cache-stable head).
    - ``prompt_pack``: the workflow pack for the active contract (its rendered manual).
    - ``context_pack_block``: the CXT-4 rendered ``<context-pack>`` block (run state).
    - ``recent_turns``: the live conversation turns, appended verbatim.
    The allowed tool schema is NOT a message — the driver receives it as `tools`.
    """
    msgs: list[LLMMessage] = [LLMMessage(role="system", content=system_prefix)]
    if prompt_pack is not None:
        msgs.append(LLMMessage(role="system", content=prompt_pack.render()))
    if context_pack_block:
        msgs.append(LLMMessage(role="user", content=context_pack_block))
    msgs.extend(recent_turns)
    return msgs


def render_messages_as_text(msgs: Sequence[LLMMessage]) -> str:
    """Render assembled messages for text-only drivers/sidecars.

    The message order is preserved exactly, with role-labeled separators so a
    text-only consumer can still see the same section boundaries as the structured
    message list.
    """
    return "\n\n".join(f"--- {msg.role} ---\n{msg.content}" for msg in msgs)
