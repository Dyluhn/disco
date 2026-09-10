"""Deep Research kickoff, retrieval, execution, and reporting."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any, Literal

from disco.core import (
    DEFAULT_OWNER_ID,
    Event,
    EventSource,
    MessageEvent,
    NoOpCondenser,
    ReportEvent,
    ResearchCheckpointEvent,
)
from disco.core.llm import (
    DefaultLLMRouter,
    ModelExecutionPolicy,
    OperatingMode,
    RouterSummarizer,
)
from disco.core.loop import AgentLoop, NeverConfirm, RouterAgent
from disco.core.security import RuleBasedAnalyzer
from disco.core.think import strip_think_spans
from disco.retrieval.deep_research import DepthTier, SavedPool
from disco.retrieval.models import Passage
from disco.retrieval.ranking import Embedder

from .build_loop_components import _NoToolExecutor
from .deep_research_provider import DeepResearchProvider
from .deep_research_state import DeepResearchLiveState, DeepResearchState

if TYPE_CHECKING:
    from disco.core.store.sqlite import SqliteEventStore

    from .driver_runtime import DriverPreflight, DriverRuntime
    from .lifecycle_command_service import LifecycleCommandService
    from .run_registry import CancellationRegistry
    from .runtime_settings import RuntimeSettings
    from .space_service import SpaceService

_LOG = logging.getLogger(__name__)

_FOLLOW_UP_SOURCE_CHARS = 12_000
_FOLLOW_UP_REPORT_CHARS = 12_000

#: How many saved passages the follow-up prompt QUOTES. The budget above used
#: to be apportioned across the whole corpus, and a real report keeps 90–152
#: passages: that bought 3–47 characters of each passage's own text, so the
#: block was a list of ids with a fragment of a title attached and the model
#: answered from the report sections instead. Sixteen leaves ~700 characters
#: each — a paragraph the answer can actually be drawn from.
_FOLLOW_UP_SOURCE_PASSAGES = 16

#: The scoring the selection below uses, identical to
#: ``retrieval.ranking.LexicalReranker``'s: count the query's words that occur
#: in the passage. That class itself does not fit here — it is async, takes
#: ``Passage`` models rather than the saved dicts, and returns an order with no
#: scores — and reaching it from a synchronous prompt builder would cost more
#: code than the two lines it saves.
_WORD = re.compile(r"[a-z0-9]+")

#: A ``[[id]]`` the QUESTION itself names. Whatever else the ranking prefers, a
#: reader who asks about a passage by id has to be given that passage.
_NAMED_PASSAGE = re.compile(r"\[\[([^\]\s]+)\]\]")


def _clean_model_text(text: str | None) -> str:
    return strip_think_spans(text or "")


def _bounded_context_blocks(blocks: list[tuple[str, str]], max_chars: int) -> str:
    """Fit every labeled item into one bounded prompt block.

    Text is apportioned across the remaining items, so one oversized early item
    can never hide the source labels and excerpts that follow it.
    """
    if not blocks or max_chars <= 0:
        return ""
    separator = "\n---\n"
    separator_chars = len(separator) * (len(blocks) - 1)
    label_budget = max(1, (max_chars - separator_chars) // len(blocks) - 1)
    normalized = [(label.strip()[: min(384, label_budget)], text.strip()) for label, text in blocks]
    fixed_chars = sum(len(label) + 1 for label, _text in normalized)
    fixed_chars += separator_chars
    remaining = max(0, max_chars - fixed_chars)
    rendered: list[str] = []
    for index, (label, body) in enumerate(normalized):
        slots_left = len(normalized) - index
        body_budget = remaining // slots_left
        excerpt = _bounded_excerpt(body, body_budget)
        rendered.append(f"{label}\n{excerpt}".rstrip())
        remaining -= len(excerpt)
    return separator.join(rendered)[:max_chars]


def _bounded_excerpt(body: str, budget: int) -> str:
    """Keep both ends of long evidence so late qualifications survive."""

    if budget <= 0:
        return ""
    if len(body) <= budget:
        return body
    marker = "\n[excerpt middle omitted]\n"
    if budget <= len(marker):
        return body[:budget]
    available = budget - len(marker)
    head = available * 2 // 3
    tail = available - head
    return f"{body[:head].rstrip()}{marker}{body[-tail:].lstrip()}"


def _select_follow_up_passages(
    passages: list[dict[str, Any]], follow_up_query: str
) -> list[dict[str, Any]]:
    """The saved passages this follow-up gets to quote — most relevant first.

    A SORT, not a filter, so a question with no word in common with anything
    ("summarise this") still gets passages: every score is then 0, the sort is
    stable, and what comes back is the corpus's own order — which is
    cited-passages-first. The positional prefix is the floor of this rule
    rather than a second branch beside it, and an empty selection is not
    reachable from a non-empty corpus.
    """
    named = set(_NAMED_PASSAGE.findall(follow_up_query))
    query_words = set(_WORD.findall(follow_up_query.lower()))

    def rank(indexed: tuple[int, dict[str, Any]]) -> tuple[int, int, int]:
        index, passage = indexed
        pinned = 0 if str(passage.get("id", "")) in named else 1
        overlap = len(query_words & set(_WORD.findall(str(passage.get("text", "")).lower())))
        return (pinned, -overlap, index)

    ranked = sorted(enumerate(passages), key=rank)
    return [passage for _, passage in ranked[:_FOLLOW_UP_SOURCE_PASSAGES]]


def _build_grounding_block(passages: list[dict[str, Any]], follow_up_query: str) -> str:
    """Build the bounded source block: the passages this question reaches for,
    each with a real excerpt.

    The budget is spent on the SELECTION, not spread across the whole saved
    corpus. Spread across 152 passages it bought 3 characters of each one's own
    text, so the block named every source and quoted none of them — and an
    answer told to cite only the supplied passages then cited ids it had never
    been shown the text of. Grounding is unaffected: the verifier still checks
    the answer against the FULL corpus, so what counts as supported is the same
    as it was.
    """
    blocks = []
    for passage in _select_follow_up_passages(passages, follow_up_query):
        pid = str(passage.get("id", ""))[:128]
        source = str(passage.get("source_title") or passage.get("source_url") or "")[:240]
        blocks.append((f"[[{pid}]] ({source})", str(passage.get("text", ""))))
    corpus_header = (
        f"Saved source corpus: {len(passages)} passages. The {len(blocks)} most "
        "relevant to this follow-up are quoted below."
    )
    body_budget = max(0, _FOLLOW_UP_SOURCE_CHARS - len(corpus_header) - 1)
    body = _bounded_context_blocks(blocks, body_budget)
    return f"{corpus_header}\n{body}"[:_FOLLOW_UP_SOURCE_CHARS]


def _build_report_block(report: ReportEvent) -> str:
    """Keep the report's own sections visible when a follow-up asks about them."""
    blocks = [(f"## {section.title[:300]}", section.markdown) for section in report.sections]
    return _bounded_context_blocks(blocks, _FOLLOW_UP_REPORT_CHARS)


def _build_follow_up_prompt(
    prior_report: ReportEvent,
    passages: list[dict[str, Any]],
    follow_up_query: str,
    *,
    history: str = "",
) -> str:
    grounding = _build_grounding_block(passages, follow_up_query)
    report_sections = _build_report_block(prior_report)
    history_block = (
        f"--- EARLIER FOLLOW-UP EXCHANGES ---\n{history}\n--- END EARLIER EXCHANGES ---\n\n"
        if history
        else ""
    )
    return (
        f"You are answering a follow-up question about a research report. "
        f"The original query was: {prior_report.query}\n\n"
        f"The report summary: {prior_report.summary}\n\n"
        f"Below are the report sections the user is referring to.\n\n"
        f"--- REPORT SECTIONS ---\n{report_sections}\n"
        f"--- END REPORT ---\n\n"
        # The block is a selection now, so this line says so: it used to
        # promise "the source passages the report was grounded on", which on a
        # 152-passage report described a list of ids with no text behind them.
        f"Below are the passages from the report's saved corpus that bear most "
        f"on this follow-up. Use them to answer it. Cite sources "
        f"with [[passage_id]] markers.\n\n"
        f"--- SOURCE PASSAGES ---\n{grounding}\n"
        f"--- END SOURCES ---\n\n"
        f"{history_block}"
        f"Follow-up question: {follow_up_query}"
    )


class DeepResearchService:
    """Deep Research + live-research-stream logic.

    The service constructor takes explicit named owners — never a whole
    runtime. The owners are the narrow collaborators the service actually
    uses: the event store, lifecycle commands, driver runtime, settings,
    driver preflight, space service, cancellation registry, the Deep Research
    provider (which owns provider/secret/origin resolution), the project
    runtime service (for space-id validation), and the optional Deep Research
    state.
    """

    def __init__(
        self,
        *,
        store: SqliteEventStore,
        lifecycle_commands: LifecycleCommandService,
        drivers: DriverRuntime,
        settings: RuntimeSettings,
        preflight: DriverPreflight,
        spaces: SpaceService,
        cancellations: CancellationRegistry,
        provider: DeepResearchProvider,
        state: DeepResearchState | None = None,
        live_state: DeepResearchLiveState | None = None,
    ) -> None:
        self._store = store
        self._lifecycle_commands = lifecycle_commands
        self._drivers = drivers
        self._settings = settings
        self._preflight = preflight
        self._spaces = spaces
        self._cancellations = cancellations
        self._provider = provider
        self._state = state or DeepResearchState()
        self._live_state = live_state or DeepResearchLiveState()

    def enqueue_steer(self, conversation_id: str, text: str) -> bool:
        return self._live_state.enqueue_steer(conversation_id, text)

    def inject_source(self, conversation_id: str, passage: Passage) -> bool:
        return self._live_state.inject_source(conversation_id, passage)

    def has_live_run(self, conversation_id: str) -> bool:
        """True while the deep-research ENGINE is executing this conversation.

        The conversation's `AgentLoop` exists for this surface but never runs
        it (see `_compose_deep_research_loop`), so a control that winds the
        loop down is speaking about nothing while this is true. Stop uses it to
        pick the branch that can actually end the run.
        """
        return self._live_state.is_live(conversation_id)

    def forget(self, conversation_id: str) -> None:
        self._state.forget(conversation_id)
        self._live_state.forget(conversation_id)

    def add_upload_passages(
        self,
        conversation_id: str,
        passages: list[Passage],
    ) -> None:
        self._state.add_upload_passages(conversation_id, passages)

    def get_upload_passages(self, conversation_id: str) -> list[Passage]:
        return self._state.upload_passages(conversation_id)

    def embedder(self) -> Embedder:
        """Return the live research embedder for Space corpus ingestion."""
        return self._provider.embedder()

    def _validated_space_ids(
        self,
        space_ids: frozenset[str],
        *,
        owner_id: str | None,
        include_unclaimed_legacy: bool = False,
    ) -> tuple[frozenset[str], tuple[str, ...]]:
        return self._spaces.validated_space_ids(
            space_ids,
            owner_id=owner_id,
            include_unclaimed_legacy=include_unclaimed_legacy,
        )

    def _compose_deep_research_loop(
        self,
        conversation_id: str,
        router: DefaultLLMRouter,
        agent: RouterAgent,
        *,
        driver_context_window: int | None = None,
    ) -> AgentLoop:
        """Compose the loop frame for Deep Research. The loop itself doesn't drive
        the research — `_run_with_persistence` short-circuits `loop.run()` for
        this surface and runs the phase dispatcher (which launches the v2
        engine) directly. The AgentLoop exists only because every surface's
        kick resolves one; it never runs for Deep Research (v2, decision #7:
        no plan gate — the old approval flow this loop used to host is gone).
        No tools, no risk gate, no condenser — Deep Research's iteration
        lives in the engine, not the loop."""
        # Deep Research intentionally uses the standard tier: it is a read-only
        # pipeline (no tool execution), so the weak-model assist compensations
        # (F-features) are not applicable. Explicit model_policy= satisfies the
        # production-path EXPLICIT-pass gate (no silent default).
        return AgentLoop(
            conversation_id,
            self._store,
            agent,
            _NoToolExecutor(),
            router,
            RuleBasedAnalyzer(),
            NeverConfirm(),  # no risky action gate — read-only research
            NoOpCondenser(),  # the engine manages its own corpus; no View condense
            RouterSummarizer(router),
            mode=OperatingMode.PLANNING,
            planning_tools=frozenset({"submit_plan"}),
            model_policy=ModelExecutionPolicy.standard(),
            driver_context_window=driver_context_window,
        )

    def _depth_for(self, conversation_id: str) -> DepthTier:
        return self._state.depth_for(conversation_id)

    def set_depth(self, conversation_id: str, tier: str | None) -> None:
        self._state.set_depth(conversation_id, tier)

    # ── DR-3 recency window (E2) ──────────────────────────────────────────────

    def set_recency(self, conversation_id: str, window: str | None) -> None:
        self._state.set_recency(conversation_id, window)

    def _recency_for(self, conversation_id: str) -> Literal["month", "week"] | None:
        return self._state.recency_for(conversation_id)

    def _research(self, search_override: Any | None = None) -> dict[str, Any]:
        return self._provider.research(search_override=search_override)

    def _search_override_for_sources(self, sources: Sequence[str] | None) -> Any | None:
        return self._provider.search_override_for_sources(sources)

    async def _preflight_encoders(
        self, deps: dict[str, Any], *, required: tuple[str, ...]
    ) -> str | None:
        """W-33: validate the REQUIRED Deep Research encoders are configured AND
        reachable BEFORE the run starts. Remote encoders degrade SILENTLY — a
        TeiReranker on an HTTP error returns input order, a SidecarNLIVerifier
        returns neutral, a None embedder yields no vectors — so an empty/unreachable
        remote URL (the `encoders.remote=true` + empty-url misconfig) produces a
        quietly-wrong report. Returns None when every required encoder is usable,
        else a VERBOSE reason string NAMING the exact encoder.

        Bundled/in-process encoders have no `probe()` — they load lazily and raise
        EncoderUnavailable on a RAM failure (surfaced at use), so they are treated
        as present here. Only the live HTTP clients (TeiReranker / OpenAIEmbedder /
        SidecarNLIVerifier) carry a `probe()` that we await."""
        from disco.retrieval.local_encoders import EncoderUnavailable

        for name in required:
            client = deps.get(name)
            if client is None:
                return (
                    f"Deep Research needs the {name}, but it isn't connected "
                    f"(no {name} is configured). Set it in Settings -> Encoders, "
                    "or switch encoders to in-process (local)."
                )
            probe = getattr(client, "probe", None)
            if probe is None:
                continue  # bundled/in-process encoder — present (raises on RAM fail)
            try:
                await probe()
            except EncoderUnavailable as exc:
                return str(exc)
        return None

    async def research_stream(
        self,
        query: str,
        *,
        model_override: str | None = None,
        drop_weak: bool = False,
        domains_deny: frozenset[str] = frozenset(),
        think: bool = False,
        conversation_id: str | None = None,
        space_ids: frozenset[str] = frozenset(),
        owner_id: str | None = None,
        include_unclaimed_legacy: bool = False,
        sources: Sequence[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream a live grounded answer as the UI's research frames (state →
        token… → final). See ``stream_parts.prepare`` for the pre-stream
        preflight/composition (driver + encoders + MCP + seed passages); this
        method owns only the part that must actually be a generator — the
        token stream itself, plus the re-scope controls (`domains_deny`,
        `drop_weak`, `think`) that shape it."""
        from ._deep_research_service_parts import stream as stream_parts

        error, prepared = await stream_parts.prepare(
            self,
            conversation_id,
            model_override=model_override,
            think=think,
            space_ids=space_ids,
            owner_id=owner_id,
            include_unclaimed_legacy=include_unclaimed_legacy,
            sources=sources,
        )
        if error is not None:
            yield error
            return
        assert prepared is not None

        from disco.retrieval import RouterQueryRewriter
        from disco.retrieval.streaming import stream_research_answer

        async for frame in stream_research_answer(
            query,
            router=prepared.router,
            search=prepared.search,
            extraction=prepared.extraction,
            reranker=prepared.deps["reranker"],
            nli=prepared.deps["nli"],
            # F1: keep-searching on no-answer. Pass the rewriter and allow up to 2
            # extra rounds when the first answer has zero supported claims.  The
            # RouterQueryRewriter reuses the existing QUERY_REWRITER role prompt so
            # no new prompt is introduced.  max_research_rounds=1 is the default, so
            # all existing callers remain byte-identical until this wiring opts them in.
            rewriter=RouterQueryRewriter(prepared.router),
            max_research_rounds=2,
            domains_deny=domains_deny,
            drop_weak=drop_weak,
            think=think,
            # G1/DR-4 F3: seed the rerank step with any pre-attached upload passages.
            seed_passages=prepared.seed_passages,
            corpus_ids=prepared.space_ids,
            embedder=prepared.deps.get("embedder"),
            vector_store=self._spaces.space_vector_store(),
        ):
            yield frame

    async def _maybe_run_deep_research(self, conversation_id: str) -> None:
        """The Deep Research phase dispatcher. See ``dispatch_parts.run_phase``
        for the full phase table (resume → follow-up → kickoff) this drives
        every turn — v2 (decision #7) is gateless: the first user message
        launches execution directly, with no plan proposal or approval step.
        All actions persist via the event store; the WS surface streams them.
        Failures surface as ErrorEvent on the log — never raise out of the
        background task."""
        from ._deep_research_service_parts import dispatch as dispatch_parts

        await dispatch_parts.run_phase(self, conversation_id)

    async def _execute_deep_research(
        self,
        conversation_id: str,
        *,
        resume_from: ResearchCheckpointEvent | None = None,
    ) -> None:
        """Run the v2 engine on the conversation's research question (the
        first user message + any pre-run additions), emitting the brief and
        Action/Observation events as the agent works, ending with a
        ReportEvent + StatusEvent(FINISHED). See ``execute_parts.run_execute``
        for the full sequencing.

        `resume_from` is a prior stopped run's ResearchCheckpointEvent: its
        gathered evidence + issued queries are carried into the engine so
        resume continues from the checkpoint instead of redoing the searches
        that already ran."""
        from ._deep_research_service_parts import execute as execute_parts

        await execute_parts.run_execute(self, conversation_id, resume_from=resume_from)

    async def write_from_pool(self, conversation_id: str, pool: SavedPool) -> None:
        """Write the report again from a SAVED evidence pool — no research.

        The pool file is everything ``writer.write_report`` reads, so this is
        how a writer change is evaluated against a run that already happened
        instead of by running the ~90-minute research loop again. Same emit
        path, same report assembly, same terminal ReportEvent + FINISHED. See
        ``write_from_pool_parts.run_write_from_pool``."""
        from ._deep_research_service_parts import write_from_pool as write_from_pool_parts

        await write_from_pool_parts.run_write_from_pool(self, conversation_id, pool)

    @staticmethod
    def _has_fresh_user_message(events: list[Event], reports: list[ReportEvent]) -> bool:
        """True when a USER message arrived AFTER the latest ReportEvent —
        a follow-up question the user asked on a finished report."""
        if not reports:
            return False
        last_report_seq = reports[-1].seq or 0
        for e in reversed(events):
            if (
                isinstance(e, MessageEvent)
                and e.source == EventSource.USER
                and (e.seq or 0) > last_report_seq
            ):
                return True
        return False

    async def _follow_up_deep_research(
        self,
        conversation_id: str,
        events: list[Event],
        prior_report: ReportEvent,
    ) -> None:
        """Run a follow-up synthesis on an existing deep-research report,
        reusing its passages as grounding (RP-13 — same event-stream-append
        pattern as RP-08's scheduled-task re-injection). See
        ``followup_parts.run_follow_up`` for the full sequencing; the
        grounding-block/prompt builders stay in this module (see
        ``_build_follow_up_prompt`` above) alongside the ``[[{pid}]]``
        citation format a source-scan regression test pins to this file."""
        from ._deep_research_service_parts import followup as followup_parts

        await followup_parts.run_follow_up(self, conversation_id, events, prior_report)

    async def export_report(
        self,
        conversation_id: str,
        fmt: str,
        *,
        owner_id: str = DEFAULT_OWNER_ID,
    ) -> tuple[bytes, str, str] | None:
        """Export the latest ReportEvent from a conversation as MD or PDF.

        Returns (payload_bytes, media_type, filename_extension) on success,
        or None when no ReportEvent exists for this conversation (the caller
        maps None → 404). Raises ValueError for unknown `fmt` (the caller
        maps ValueError → 400).

        The endpoint is generic over ReportEvent — today only deep_research
        conversations emit one; standard research and build do not."""
        from .report_export import export_report as _export

        events = await self._store.get_events(conversation_id)
        reports = [e for e in events if isinstance(e, ReportEvent)]
        if not reports:
            return None
        # The latest report (deep research emits only one; safe for future
        # multi-report conversations).
        report = reports[-1]
        title = await self._store.get_title(conversation_id)
        payload, media_type, ext = _export(report, fmt, title=title)
        return payload, media_type, ext
