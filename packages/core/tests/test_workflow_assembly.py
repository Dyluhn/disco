"""WPP-2 tests: kernel-neutral prompt assembly — order, included-once, determinism,
and end-to-end composition with the WPP-1 pack + CXT-4 context-pack render."""

from __future__ import annotations

from disco.core.contract import BuildContractRegistry, ContractKind
from disco.core.events import LLMMessage, PlanEvent, PlanStep
from disco.core.loop.context_builder import build_context_pack, render_context_pack
from disco.core.workflows import (
    PromptPackRegistry,
    assemble_workflow_prompt,
    parse_prompt_pack,
)

_PREFIX = "You are Disco Build. Operate inside the rails."


def _pack():
    return parse_prompt_pack("p", "## Role\nbuild it\n")


def _turns():
    return [
        LLMMessage(role="user", content="make a landing page"),
        LLMMessage(role="assistant", content="on it"),
    ]


def test_assembly_order() -> None:
    msgs = assemble_workflow_prompt(
        system_prefix=_PREFIX,
        prompt_pack=_pack(),
        context_pack_block="<context-pack>\nGoal (v1): ship\n</context-pack>",
        recent_turns=_turns(),
    )
    assert [m.role for m in msgs] == ["system", "system", "user", "user", "assistant"]
    assert msgs[0].content == _PREFIX  # stable prefix first
    assert "## Role" in msgs[1].content  # workflow pack second
    assert "<context-pack>" in msgs[2].content  # context pack third
    assert msgs[3].content == "make a landing page"  # then live turns
    assert msgs[4].content == "on it"


def test_each_structured_part_appears_once() -> None:
    msgs = assemble_workflow_prompt(
        system_prefix=_PREFIX,
        prompt_pack=_pack(),
        context_pack_block="<context-pack>\nx\n</context-pack>",
        recent_turns=_turns(),
    )
    blob = "\n".join(m.content for m in msgs)
    assert blob.count("<context-pack>") == 1
    assert blob.count("## Role") == 1
    assert blob.count(_PREFIX) == 1


def test_deterministic_kernel_neutral() -> None:
    # the kernel-neutrality guarantee: same inputs → identical messages
    kwargs = dict(
        system_prefix=_PREFIX,
        prompt_pack=_pack(),
        context_pack_block="<context-pack>\nx\n</context-pack>",
        recent_turns=_turns(),
    )
    assert assemble_workflow_prompt(**kwargs) == assemble_workflow_prompt(**kwargs)


def test_optional_parts_omitted_cleanly() -> None:
    # no pack, no context block → just the prefix + turns (no empty messages)
    msgs = assemble_workflow_prompt(system_prefix=_PREFIX, recent_turns=_turns())
    assert [m.role for m in msgs] == ["system", "user", "assistant"]
    # an empty context block is omitted (falsy)
    msgs2 = assemble_workflow_prompt(system_prefix=_PREFIX, context_pack_block="", recent_turns=())
    assert len(msgs2) == 1 and msgs2[0].role == "system"


def test_end_to_end_pack_plus_contextpack() -> None:
    # compose the real WPP-1 pack + the CXT-4 context-pack render for a contract
    c = BuildContractRegistry.default().get(ContractKind.STATIC_SITE)
    assert c is not None and c.prompt_pack is not None
    pack = PromptPackRegistry().require(c.prompt_pack)
    events = [PlanEvent(summary="ship the page", steps=[PlanStep(title="hero")])]
    cp_block = render_context_pack(build_context_pack(events, todo_text="- [ ] hero"))

    msgs = assemble_workflow_prompt(
        system_prefix=_PREFIX,
        prompt_pack=pack,
        context_pack_block=cp_block,
        recent_turns=[LLMMessage(role="user", content="build it")],
    )
    blob = "\n".join(m.content for m in msgs)
    assert "ready_for_static_site_verification" in blob  # the pack's verify rule
    assert "ship the page" in blob  # the context pack's goal
    assert "build it" in blob  # the live turn
