"""BW-05 (single-bracket citation normalization) + BW-07 (table repair +
truncation-continuation join)."""

from __future__ import annotations

from typing import Any

from disco.core.llm import (
    CallContext,
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    TokenUsage,
)
from disco.retrieval.deep_research.decompose import SubQuestion
from disco.retrieval.deep_research.gather import GatherLegContext, SubQuestionResult
from disco.retrieval.deep_research.synthesis import (
    _normalize_citations,
    _repair_tables,
    synthesize_section,
)
from disco.retrieval.models import Passage
from disco.retrieval.vectorstore import InMemoryVectorStore

# ---- BW-05: bare [id] -> [[id]] ----------------------------------------


def test_normalize_promotes_known_single_bracket():
    md = "The market grew sharply [f1161a_p2] last year."
    out = _normalize_citations(md, {"f1161a_p2"})
    assert "[[f1161a_p2]]" in out
    assert "[f1161a_p2]" not in out.replace("[[f1161a_p2]]", "")


def test_normalize_leaves_already_doubled_alone():
    md = "Already cited [[abc_p1]] here."
    out = _normalize_citations(md, {"abc_p1"})
    assert out == md  # no triple-bracket mangling


def test_normalize_leaves_unknown_prose_brackets():
    md = "See footnote [1] and the link [text](http://x) and [note]."
    out = _normalize_citations(md, {"abc_p1"})
    assert out == md  # none of these ids are known passages


def test_normalize_mixed():
    md = "A [src1] and B [[src2]] and prose [aside]."
    out = _normalize_citations(md, {"src1", "src2"})
    assert "[[src1]]" in out
    assert "[[src2]]" in out
    assert "[aside]" in out  # unknown id untouched


def test_normalize_empty_ids_noop():
    md = "Nothing [x] to do."
    assert _normalize_citations(md, set()) == md


# ---- BW-07: malformed table repair -------------------------------------


def test_repair_injects_missing_delimiter_and_pipes():
    # No leading/trailing pipes, NO |---| delimiter row -> remark would fail.
    md = "Option | Pros | Cons\nA | fast | costly\nB | cheap | slow"
    out = _repair_tables(md)
    lines = [ln for ln in out.split("\n") if ln.strip()]
    assert lines[0] == "| Option | Pros | Cons |"
    # second line must now be a delimiter row
    assert set(lines[1].replace("|", "").replace(" ", "")) <= {"-"}
    assert "| A | fast | costly |" in out
    assert "| B | cheap | slow |" in out


def test_repair_preserves_valid_table():
    md = "| A | B |\n| --- | --- |\n| 1 | 2 |"
    out = _repair_tables(md)
    assert "| A | B |" in out
    assert "| 1 | 2 |" in out
    # exactly one delimiter row
    assert out.count("---") >= 1


def test_repair_leaves_prose_with_single_pipe():
    md = "Throughput is measured in tokens | second in this benchmark."
    out = _repair_tables(md)
    assert out == md  # lone pipe line is not a table


def test_repair_does_not_touch_code_fences():
    md = "```\na | b | c\n```\nafter"
    out = _repair_tables(md)
    assert out == md


def test_repair_pads_ragged_rows():
    md = "H1 | H2 | H3\nx | y"
    out = _repair_tables(md)
    assert "| x | y |  |" in out  # padded to 3 columns


# ---- BW-07: truncation -> continuation join (finish_reason == "length") ---


class _FakeNLI:
    """Deterministic NLI stub — every non-empty claim is 'entail'."""

    def entail(self, premise: str, hypothesis: str) -> str:
        return "entail" if premise and hypothesis else "neutral"

    def score(self, premise: str, hypothesis: str) -> float:
        return 1.0 if self.entail(premise, hypothesis) == "entail" else 0.0


class _ScriptedRouter(LLMRouter):
    """Replays a queue of (text, finish_reason) for the rag_answerer role so a
    test can drive synthesize_section's truncation/continuation path."""

    def __init__(self, script: list[tuple[str, str]]) -> None:
        self._script = list(script)
        self.calls = 0

    async def complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> CompletionResponse:
        self.calls += 1
        text, finish = (
            self._script.pop(0) if self._script else ("(exhausted)", "stop")
        )
        return CompletionResponse(
            text=text,
            tool_calls=[],
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason=finish,  # type: ignore[arg-type]
            model_used="fake",
            routing=None,
        )


def _passage() -> Passage:
    return Passage(
        id="p1",
        source_url="http://example.test/x",
        source_title="Source X",
        text="Alpha scored 90 and Beta scored 85 on the benchmark.",
    )


async def _run_synth(router: LLMRouter) -> str:
    sub = SubQuestionResult(
        subq=SubQuestion(title="How do the models compare?"),
        passages=[_passage()],
    )

    async def _emit(_kind: str, _payload: dict[str, Any]) -> None:
        return None

    section = await synthesize_section(
        sub,
        router=router,
        embedder=None,
        vector_store=InMemoryVectorStore(),
        namespace="ns",
        nli=_FakeNLI(),
        section_id="s1",
        top_k_for_section=4,
        emit=_emit,
        leg_context=GatherLegContext(
            subq_id="sq1",
            namespace="ns",
            call_context=CallContext(conversation_id="conv_trunc"),
        ),
    )
    return section.markdown


async def test_truncation_continues_mid_table_row_without_loss() -> None:
    """A section cut mid-table-row (`finish_reason=="length"`) is continued from
    the exact cut point and glued WITHOUT a spurious separator: the half-written
    cell completes (``| Alpha | 9`` + ``0 |`` -> ``| Alpha | 90 |``) — no merged
    rows, no lost or duplicated content."""
    router = _ScriptedRouter([
        (
            "## Findings\n\nResults [[p1]]:\n\n"
            "| Model | Score |\n| --- | --- |\n| Alpha | 9",
            "length",
        ),
        ("0 |\n| Beta | 85 |", "stop"),
    ])
    md = await _run_synth(router)

    assert router.calls == 2  # exactly one continuation call
    # the split number was rejoined into a single cell (no loss, no spurious gap)
    assert "| Alpha | 90 |" in md
    assert "| Beta | 85 |" in md
    # rows were NOT merged together (the pre-fix bug glued "9" + "0 |\n| Beta")
    assert "| Alpha | 90 || Beta" not in md
    # no duplication of the continued content
    assert md.count("Beta") == 1
    assert md.count("Alpha") == 1
    # section no longer terminates mid-table: it ends on a closed pipe row
    assert md.rstrip().endswith("|")


async def test_truncation_at_row_boundary_starts_a_fresh_line() -> None:
    """When the cut lands on a clean line boundary (the cut text ends with a
    newline), the continuation begins on a FRESH line — a new row never merges
    into the previous one. This is the case the pre-fix strip()-then-glue logic
    broke (it produced ``| Alpha | 90 || Beta | 85 |``)."""
    router = _ScriptedRouter([
        (
            "Intro [[p1]].\n\n"
            "| Model | Score |\n| --- | --- |\n| Alpha | 90 |\n",
            "length",
        ),
        ("| Beta | 85 |", "stop"),
    ])
    md = await _run_synth(router)

    assert router.calls == 2
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
    router = _ScriptedRouter([
        (
            "Intro [[p1]].\n\n"
            "| Model | Score |\n| --- | --- |\n| Alpha | 90 |",  # NO trailing \n
            "length",
        ),
        ("| Beta | 85 |", "stop"),
    ])
    md = await _run_synth(router)

    assert router.calls == 2
    assert "| Alpha | 90 |" in md
    assert "| Beta | 85 |" in md
    # the row-boundary bug: two complete rows fused into one via "||"
    assert "||" not in md
    assert "| Alpha | 90 || Beta" not in md
    # the two rows were joined on a fresh line, in order, with no loss
    assert "90 |\n| Beta | 85 |" in md
    assert md.count("Beta") == 1
    assert md.count("Alpha") == 1


async def test_no_continuation_when_first_response_completes() -> None:
    """A normal (`finish_reason=="stop"`) section makes exactly one call — the
    truncation guard never fires."""
    router = _ScriptedRouter([
        ("A complete section about Alpha [[p1]].", "stop"),
    ])
    md = await _run_synth(router)

    assert router.calls == 1
    assert "Alpha" in md


async def test_truncation_guard_is_bounded() -> None:
    """A model that keeps returning `length` is bounded: the guard continues at
    most twice (3 router calls total), then stops with the accumulated text."""
    router = _ScriptedRouter([
        ("part-1 [[p1]]", "length"),
        (" part-2", "length"),
        (" part-3", "length"),
        (" part-4", "length"),
    ])
    md = await _run_synth(router)

    assert router.calls == 3  # initial + 2 bounded continuations
    assert "part-1" in md
    assert "part-3" in md
    assert "part-4" not in md  # the 4th would-be call never happens
