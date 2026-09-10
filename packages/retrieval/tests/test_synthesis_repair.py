"""Report-writer text mechanics: the repairs and formatters the whole-report
writer runs on model output.

BW-05 (single-bracket citation normalization), BW-07 (malformed-table repair +
the truncation-continuation join), the evidence-pool formatter, and the
readable-summary paragraph contract. All of it is pure text mechanics or a
bounded provider retry — none of it deletes prose (decision #5, no scissors).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from disco.core import LLMMessage
from disco.core.llm import (
    CompletionRequest,
    CompletionResponse,
    LLMError,
    LLMRouter,
    LLMTransientError,
    StreamChunk,
    TokenUsage,
)
from disco.retrieval.deep_research._synthesis_parts import repair_tables
from disco.retrieval.deep_research._writer_parts import (
    format_evidence_pool,
    normalize_citations,
    readable_summary,
)
from disco.retrieval.deep_research.writer import _MAX_CONTINUATIONS, _continue_if_cut_off, _Draft
from disco.retrieval.models import Passage

# ---- BW-05: bare [id] -> [[id]] ----------------------------------------


def test_normalize_promotes_known_single_bracket():
    md = "The market grew sharply [f1161a_p2] last year."
    out = normalize_citations(md, {"f1161a_p2"})
    assert "[[f1161a_p2]]" in out
    assert "[f1161a_p2]" not in out.replace("[[f1161a_p2]]", "")


def test_normalize_leaves_already_doubled_alone():
    md = "Already cited [[abc_p1]] here."
    out = normalize_citations(md, {"abc_p1"})
    assert out == md  # no triple-bracket mangling


def test_normalize_leaves_unknown_prose_brackets():
    md = "See footnote [1] and the link [text](http://x) and [note]."
    out = normalize_citations(md, {"abc_p1"})
    assert out == md  # none of these ids are known passages


def test_normalize_mixed():
    md = "A [src1] and B [[src2]] and prose [aside]."
    out = normalize_citations(md, {"src1", "src2"})
    assert "[[src1]]" in out
    assert "[[src2]]" in out
    assert "[aside]" in out  # unknown id untouched


def test_normalize_empty_ids_noop():
    md = "Nothing [x] to do."
    assert normalize_citations(md, set()) == md


# ---- BW-07: malformed table repair -------------------------------------


def test_repair_injects_missing_delimiter_and_pipes():
    # No leading/trailing pipes, NO |---| delimiter row -> remark would fail.
    md = "Option | Pros | Cons\nA | fast | costly\nB | cheap | slow"
    out = repair_tables(md)
    lines = [ln for ln in out.split("\n") if ln.strip()]
    assert lines[0] == "| Option | Pros | Cons |"
    # second line must now be a delimiter row
    assert set(lines[1].replace("|", "").replace(" ", "")) <= {"-"}
    assert "| A | fast | costly |" in out
    assert "| B | cheap | slow |" in out


def test_repair_preserves_valid_table():
    md = "| A | B |\n| --- | --- |\n| 1 | 2 |"
    out = repair_tables(md)
    assert "| A | B |" in out
    assert "| 1 | 2 |" in out
    # exactly one delimiter row
    assert out.count("---") >= 1


def test_repair_leaves_prose_with_single_pipe():
    md = "Throughput is measured in tokens | second in this benchmark."
    out = repair_tables(md)
    assert out == md  # lone pipe line is not a table


def test_repair_does_not_touch_code_fences():
    md = "```\na | b | c\n```\nafter"
    out = repair_tables(md)
    assert out == md


def test_repair_pads_ragged_rows():
    md = "H1 | H2 | H3\nx | y"
    out = repair_tables(md)
    assert "| x | y |  |" in out  # padded to 3 columns


# ---- BW-07: truncation -> continuation join (finish_reason == "length") ---


class _ScriptedRouter(LLMRouter):
    """Replays a queue of (text, finish_reason) continuation responses and
    counts the continuation calls the guard actually made."""

    def __init__(self, script: list[tuple[str, str]]) -> None:
        self._script = list(script)
        self.calls = 0

    async def complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> CompletionResponse:
        del context
        self.calls += 1
        text, finish = self._script.pop(0) if self._script else ("(exhausted)", "stop")
        return CompletionResponse(
            text=text,
            tool_calls=[],
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason=finish,  # type: ignore[arg-type]
            model_used="fake",
            request_id=request.request_id,
            routing=None,
        )

    async def stream_complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> AsyncIterator[StreamChunk]:
        async def gen() -> AsyncIterator[StreamChunk]:
            yield StreamChunk(done=True, final=await self.complete(request, context=context))

        return gen()


async def _emit(_kind: str, _payload: dict[str, Any]) -> None:
    return None


async def _continue(script: list[tuple[str, str]]) -> tuple[str, int]:
    """Run the truncation guard over `script[0]` as the writer's first response
    and the rest as its continuations. Returns (markdown, continuation calls)."""
    markdown, calls, _truncated = await _continue_with_state(script)
    return markdown, calls


async def _continue_with_state(
    script: list[tuple[str, str]],
) -> tuple[str, int, bool]:
    """As `_continue`, also returning whether the report is STILL truncated."""
    head_text, head_finish = script[0]
    router = _ScriptedRouter(script[1:])
    # Only `finish_reason == "length"` may drive a continuation.
    draft, _rounds = await _continue_if_cut_off(
        router,
        [LLMMessage(role="user", content="WRITE THE REPORT")],
        _Draft(markdown=head_text, cut_off=head_finish == "length"),
        rounds_used=0,
        max_tokens=1_000,
        conversation_id=None,
        emit=_emit,
        should_cancel=None,
    )
    return draft.markdown, router.calls, draft.cut_off


async def test_truncation_continues_mid_table_row_without_loss() -> None:
    """A report cut mid-table-row (`finish_reason=="length"`) is continued from
    the exact cut point and glued WITHOUT a spurious separator: the half-written
    cell completes (``| Alpha | 9`` + ``0 |`` -> ``| Alpha | 90 |``) — no merged
    rows, no lost or duplicated content."""
    md, calls = await _continue(
        [
            (
                "## Findings\n\nResults [[p1]]:\n\n| Model | Score |\n| --- | --- |\n| Alpha | 9",
                "length",
            ),
            ("0 |\n| Beta | 85 |", "stop"),
        ]
    )

    assert calls == 1  # exactly one continuation call
    # the split number was rejoined into a single cell (no loss, no spurious gap)
    assert "| Alpha | 90 |" in md
    assert "| Beta | 85 |" in md
    # rows were NOT merged together (the pre-fix bug glued "9" + "0 |\n| Beta")
    assert "| Alpha | 90 || Beta" not in md
    # no duplication of the continued content
    assert md.count("Beta") == 1
    assert md.count("Alpha") == 1
    # the report no longer terminates mid-table: it ends on a closed pipe row
    assert md.rstrip().endswith("|")


async def test_truncation_at_row_boundary_starts_a_fresh_line() -> None:
    """When the cut lands on a clean line boundary (the cut text ends with a
    newline), the continuation begins on a FRESH line — a new row never merges
    into the previous one. This is the case the pre-fix strip()-then-glue logic
    broke (it produced ``| Alpha | 90 || Beta | 85 |``)."""
    md, calls = await _continue(
        [
            (
                "Intro [[p1]].\n\n| Model | Score |\n| --- | --- |\n| Alpha | 90 |\n",
                "length",
            ),
            ("| Beta | 85 |", "stop"),
        ]
    )

    assert calls == 1
    assert "| Alpha | 90 |" in md
    assert "| Beta | 85 |" in md
    # the boundary bug: two rows fused on one line
    assert "| Alpha | 90 || Beta" not in md
    assert "90 |\n| Beta | 85 |" in md  # joined on a fresh line, in order
    assert md.count("Beta") == 1


async def test_truncation_after_complete_row_without_newline_joins_fresh_line() -> None:
    """The residual BW-07 case: the cut lands AFTER a complete table row but
    BEFORE its trailing newline — the prev chunk ends on a row-closing pipe
    (``…| Alpha | 90 |``, no ``\\n``) and the continuation opens a new row with a
    leading pipe (``| Beta | 85 |``). A direct glue would fuse them into one row
    (``| Alpha | 90 || Beta | 85 |``); the adjacent ``||`` is the tell. The join
    must insert a newline so each row stays distinct."""
    md, calls = await _continue(
        [
            (
                "Intro [[p1]].\n\n| Model | Score |\n| --- | --- |\n| Alpha | 90 |",
                "length",
            ),
            ("| Beta | 85 |", "stop"),
        ]
    )

    assert calls == 1
    assert "| Alpha | 90 |" in md
    assert "| Beta | 85 |" in md
    # the row-boundary bug: two complete rows fused into one via "||"
    assert "||" not in md
    assert "90 |\n| Beta | 85 |" in md
    assert md.count("Beta") == 1
    assert md.count("Alpha") == 1


async def test_no_continuation_when_first_response_completes() -> None:
    """A normal (`finish_reason=="stop"`) report makes NO continuation call —
    the truncation guard never fires."""
    md, calls = await _continue([("A complete report about Alpha [[p1]].", "stop")])

    assert calls == 0
    assert md == "A complete report about Alpha [[p1]]."


async def test_truncation_guard_is_bounded() -> None:
    """A model that keeps returning `length` is bounded: the guard continues at
    most `_MAX_CONTINUATIONS` times, then stops with the accumulated text."""
    md, calls = await _continue(
        [
            ("part-1 [[p1]]", "length"),
            (" part-2", "length"),
            (" part-3", "length"),
            (" part-4", "length"),
            (" part-5", "length"),
        ]
    )

    assert calls == _MAX_CONTINUATIONS == 3
    assert "part-1" in md
    assert "part-4" in md
    assert "part-5" not in md  # the 4th would-be call never happens


async def test_exhausted_guard_reports_the_report_is_still_truncated() -> None:
    """The bounded guard tells the caller when it gave up mid-report, so a
    cut-off draft can never be shipped as if it were finished."""
    _md, calls, truncated = await _continue_with_state(
        [
            ("part-1 [[p1]]", "length"),
            (" part-2", "length"),
            (" part-3", "length"),
            (" part-4", "length"),
        ]
    )

    assert calls == 3
    assert truncated is True


async def test_a_completed_continuation_reports_no_truncation() -> None:
    _md, calls, truncated = await _continue_with_state(
        [("part-1 [[p1]]", "length"), (" and the rest.", "stop")]
    )

    assert calls == 1
    assert truncated is False


class _FailingRouter(LLMRouter):
    """A router whose own retries are already spent: it raises LLMError."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> CompletionResponse:
        del request, context
        self.calls += 1
        raise LLMTransientError("provider unavailable after retries")

    async def stream_complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> AsyncIterator[StreamChunk]:
        async def gen() -> AsyncIterator[StreamChunk]:
            yield StreamChunk(done=True, final=await self.complete(request, context=context))

        return gen()


async def test_provider_failure_during_continuation_surfaces_instead_of_truncating() -> None:
    """The router already retried below this loop, so an LLMError here is a
    genuine run failure. It must propagate as the run's named error rather than
    silently leaving a report that stops mid-sentence."""
    router = _FailingRouter()

    with pytest.raises(LLMError):
        await _continue_if_cut_off(
            router,
            [LLMMessage(role="user", content="WRITE THE REPORT")],
            _Draft(markdown="part-1 [[p1]]", cut_off=True),
            rounds_used=0,
            max_tokens=1_000,
            conversation_id=None,
            emit=_emit,
            should_cancel=None,
        )

    assert router.calls == 1


# ---- the evidence pool the writer reads -------------------------------------


def test_section_evidence_keeps_tail_qualifications_and_url():
    passage = Passage(
        id="p1",
        source_url="https://example.com/release",
        source_title="Release report",
        text="A" * 2_000 + " The release was limited to a private preview.",
    )
    formatted = format_evidence_pool([passage], char_budget=800)
    assert "https://example.com/release" in formatted
    assert "private preview" in formatted
    assert "middle omitted" in formatted


# ---- the executive summary's paragraph contract ------------------------------


def test_successful_summary_is_split_when_provider_ignores_paragraph_contract():
    summary = readable_summary(
        "First supported finding [[one]]. Second supported finding [[two]]. "
        "Third supported finding [[three]]. Fourth supported finding [[four]]."
    )

    assert summary.count("\n\n") == 3
    assert "[[one]]" in summary
    assert "[[four]]" in summary


def test_successful_summary_preserves_existing_markdown_paragraphs():
    summary = readable_summary(
        "**Bottom line.** The first finding is supported [[one]].\n\n"
        "The qualification is also supported [[two]]."
    )

    assert summary == (
        "**Bottom line.** The first finding is supported [[one]].\n\n"
        "The qualification is also supported [[two]]."
    )
