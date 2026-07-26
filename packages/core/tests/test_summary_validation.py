"""Malformed summarizer output — rejection, one repair, truthful fallback (Epic 3).

The `k460000` diagnostic found provider tool-call protocol markup persisted as
summary text and replayed to the model as if it were conversation. The summary
path had exactly one check -- `if not summary.strip()` -- so anything non-empty
was stored verbatim.

The rejection rule is deliberately narrow. A summary of a React build contains
`<div>` and `<Button />`; a summary of a shell session contains `2>&1`. A rule
broad enough to catch protocol markup by its angle brackets would eat all of
that, and a condenser that rejects honest summaries is worse than one that
occasionally passes junk.
"""

from __future__ import annotations

import pytest
from disco.core import (
    ActionEvent,
    EventSource,
    LLMMessage,
    LLMSummarizingCondenser,
    MessageEvent,
    ObservationEvent,
    SqliteEventStore,
    ToolCall,
    ToolResult,
    View,
)
from disco.core.view import _fallback_summary, summary_rejection_reason

CID = "conv"


class _ScriptedSummarizer:
    """Returns scripted outputs in order; records how many times it was asked."""

    def __init__(self, outputs: list[str]) -> None:
        self._outputs = list(outputs)
        self.calls = 0
        self.seen: list[list] = []

    async def summarize(self, messages) -> str:
        self.seen.append(list(messages))
        index = min(self.calls, len(self._outputs) - 1)
        self.calls += 1
        return self._outputs[index]


# ---- the rule itself -------------------------------------------------------


@pytest.mark.parametrize(
    "markup",
    [
        "here is what happened <parameter>x</parameter>",
        "progress </function_calls>",
        '{"tool_calls": [{"id": "call_1"}]}',
        '<invoke name="shell">ls</invoke>',
        "<tool_call>{}</tool_call>",
        '{"tool_call_id": "abc"}',
    ],
)
def test_tool_call_protocol_markup_is_rejected(markup):
    assert summary_rejection_reason(markup) is not None


@pytest.mark.parametrize(
    "legitimate",
    [
        'Built the hero with <div className="hero"> and a <Button /> component.',
        "Ran `grep -r foo . 2>&1 | tee out.log` and fixed the failing case.",
        "Wrote a #!/bin/sh entrypoint and an <html><body> skeleton.",
        "Fixed a bug where </section> was left unclosed in index.html.",
        "GOAL: ship the app. PROGRESS: created index.html, ran the tests.",
        "Used a <template> tag and an <svg viewBox='0 0 1 1'> icon.",
    ],
)
def test_ordinary_code_html_and_shell_content_remains_allowed(legitimate):
    """Overhardening control: these are what a real summary looks like."""
    assert summary_rejection_reason(legitimate) is None


@pytest.mark.parametrize(
    "dressed",
    [
        # DeepSeek DSML, verbatim from Epic-4 seed 460000's persisted summaries.
        '<｜｜DSML｜｜tool_calls>\n<｜｜DSML｜｜invoke name="file_read">',
        '<｜｜DSML｜｜parameter name="path" string="true">REPORT.md</｜｜DSML｜｜parameter>',
        "</｜｜DSML｜｜invoke>",
        "<｜tool▁calls▁begin｜>",
        "<｜tool▁call▁begin｜>",
        # The ASCII pipe form the literal list only half-covered.
        "<|tool_call_begin|>",
    ],
)
def test_provider_dressed_protocol_delimiters_are_rejected(dressed):
    """The rule keyed on a bare `<`, so a provider that decorates its delimiter
    walked straight through it. DeepSeek writes `<｜｜DSML｜｜parameter …>`; the
    keyword is identical and only the dressing differs. 22 of 36 summaries in
    seed 460000 were persisted protocol residue, one of them nothing but a
    `file_read` call, because none of the ASCII literals matched."""
    assert summary_rejection_reason(dressed) is not None


@pytest.mark.parametrize(
    "legitimate",
    [
        # Full-width and box-drawing characters are not protocol markers by
        # themselves -- only dressing on a real protocol KEYWORD is.
        "Compared ranges A｜B｜C and rendered the ▁ separator glyph.",
        "The table used ｜ as a column divider in the generated README.",
        "Added an <input name='invoked_by'> field to the form.",
        "Described the tool_call flow in prose without any markup.",
    ],
)
def test_decoration_without_a_protocol_keyword_remains_allowed(legitimate):
    """Overhardening control for the dressed-delimiter rule: the decoration
    characters are ordinary text on their own, and a keyword appearing anywhere
    other than immediately inside a tag opening (`name='invoked_by'`) is not
    protocol markup.

    Note the pre-existing literal `<parameter` is an unanchored substring, so
    prose about a `<parameters>` element is rejected by the ORIGINAL rule, not
    by this one. That is untouched here: no observed run has ever produced it,
    and widening a settled rule on a hypothetical is not in this fix's scope."""
    assert summary_rejection_reason(legitimate) is None


def test_the_fallback_asserts_only_durable_facts():
    text = _fallback_summary(10, 42)

    assert "10" in text and "42" in text, "it must name the exact dropped range"
    assert "NOT summarized" in text
    # It must NOT invent progress, and must not imply the span was empty.
    assert "unknown rather than as 'nothing happened'" in text


# ---- through the condenser -------------------------------------------------


async def _seed(store: SqliteEventStore, n_pairs: int = 6, body: str = "detail " * 20) -> list:
    """A user instruction + n action/observation pairs -- a condensable history."""
    await store.append(
        CID, MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="TASK"))
    )
    for i in range(n_pairs):
        call = ToolCall(tool_name="shell", arguments={"command": "echo"})
        action = await store.append(CID, ActionEvent(thought=f"step {i}: {body}", tool_call=call))
        await store.append(
            CID,
            ObservationEvent(
                tool_result=ToolResult(
                    call_id=action.id, tool_name="shell", success=True, content=body
                ),
                action_id=action.id,
            ),
        )
    return await store.get_events(CID)


async def _condense_with(outputs: list[str]):
    store = SqliteEventStore(":memory:")
    events = await _seed(store)
    condenser = LLMSummarizingCondenser(keep_head=1, keep_recent=2, min_forget=2)
    summarizer = _ScriptedSummarizer(outputs)
    tombstone = await condenser.condense(events, View.of(events), summarizer=summarizer)
    return tombstone, summarizer


async def test_a_valid_summary_is_accepted_unchanged():
    """Regression: the ordinary path must not change."""
    good = "GOAL: build a site. PROGRESS: wrote index.html and styled the hero."
    tombstone, summarizer = await _condense_with([good])

    assert tombstone is not None
    assert tombstone.summary == good
    assert summarizer.calls == 1, "a valid summary must not trigger a repair"


async def test_one_repair_is_attempted_and_a_good_repair_is_accepted():
    bad = "PROGRESS </function_calls>"
    good = "GOAL: build a site. PROGRESS: wrote index.html."
    tombstone, summarizer = await _condense_with([bad, good])

    assert tombstone is not None
    assert tombstone.summary == good
    assert summarizer.calls == 2, "exactly one repair attempt"
    assert "protocol markup" in summarizer.seen[1][-1].content, (
        "the repair request must tell the summarizer what was wrong"
    )


async def test_two_invalid_responses_produce_one_truthful_fallback_and_no_loop():
    bad = '{"tool_calls": [{"id": "call_1"}]}'
    tombstone, summarizer = await _condense_with([bad, bad])

    assert tombstone is not None
    assert summarizer.calls == 2, "bounded: never more than one repair"
    assert "[host summary]" in tombstone.summary
    assert "NOT summarized" in tombstone.summary
    assert summary_rejection_reason(tombstone.summary) is None, "the fallback itself must be clean"


async def test_rejected_markup_is_never_persisted():
    """The whole point: protocol markup must not reach the event log."""
    bad = "<parameter>rm -rf /</parameter>"
    tombstone, _summarizer = await _condense_with([bad, bad])

    assert tombstone is not None
    assert "<parameter" not in tombstone.summary
    assert "rm -rf" not in tombstone.summary


async def test_an_empty_summary_still_condenses_nothing():
    """Pre-existing contract, unchanged: empty forgets context for nothing."""
    tombstone, summarizer = await _condense_with(["   "])

    assert tombstone is None
    assert summarizer.calls == 1, "emptiness short-circuits before any repair"


async def test_typed_runtime_constraints_survive_the_fallback():
    """A degraded summary must not also cost the host's standing prohibitions.

    The fallback fires exactly when context is least trustworthy, which is the
    worst moment to also drop the rule that stops the model repeating a
    destructive operation.
    """
    from disco.core.events import RuntimeConstraintEvent

    store = SqliteEventStore(":memory:")
    events = await _seed(store)
    await store.append(
        CID,
        RuntimeConstraintEvent(
            constraint_key="sandbox.host_signal_prohibited",
            guidance="killing host processes is not permitted on this backend",
            alternative="use shell_kill_process",
            capability_generation="sandbox-backend:process",
        ),
    )
    events = await store.get_events(CID)

    condenser = LLMSummarizingCondenser(keep_head=1, keep_recent=2, min_forget=2)
    bad = '{"tool_calls": []}'
    tombstone = await condenser.condense(
        events, View.of(events), summarizer=_ScriptedSummarizer([bad, bad])
    )
    assert tombstone is not None
    await store.append(CID, tombstone)

    rendered = "\n".join(m.content for m in View.of(await store.get_events(CID)).messages)

    assert "[host summary]" in rendered, "the truthful fallback was used"
    assert "sandbox.host_signal_prohibited" in rendered, (
        "the constraint must survive the degraded path too"
    )
