"""Injectable Deep Research run/observation harness.

This module deliberately lives outside the product packages.  It observes the
wire protocols that are already used by the server and reuses ``Cassette`` for
offline runs.  A transport only has to return the ordered JSON frames it saw;
the observer, report writer, and invariants are shared by live, replay, and
test-fake runs.

Examples::

    PYTHONPATH=. python -m harness.research_harness --transport replay \
      --cassette development/harness/cassettes/research_demo.jsonl \
      --query "What is vcrpy?" --output-dir /tmp/dr-run

    PYTHONPATH=. python -m harness.research_harness --transport live \
      --surface deep_research --depth exhaustive --recency week \
      --query "What changed this week in ..." --output-dir /tmp/dr-live

The JSONL event log is intentionally an outside-observer record: it contains
wire frames and cassette seams, not private product objects or credentials.

The implementation lives in :mod:`harness.research_harness_parts` (split for
the architecture size/complexity budget); THIS module is the stable import
surface — import the original names from here.  The private helpers are
re-exported deliberately: the harness tests exercise them directly.
"""

from __future__ import annotations

from .research_harness_parts._checks import (
    _BODY_PROCESS_LANGUAGE,
    _DEPTH_WORD_TARGETS,
    _FAILURE_WORDS,
    _MARKDOWN_HEADING,
    _PUNCTUATION_ONLY,
    _SECTION_MIN_WORDS,
    _SUMMARY_DIAGNOSTIC_LANGUAGE,
    _depth_word_bounds,
    _failure_text,
    _heading_hierarchy_is_valid,
    _is_stopped_checkpoint,
    _repetition_is_acceptable,
    _report_prose,
    _report_word_count,
    _section_is_substantive,
    _word_count,
    check_invariants,
    normalize_report,
)
from .research_harness_parts._cli import (
    DEFAULT_ACCEPTANCE_CORPUS,
    _main_async,
    _parse_args,
    main,
)
from .research_harness_parts._observe import (
    _CITATION,
    _DECK_TIMEOUT_S,
    _DEPTH_TIMEOUTS_S,
    _FOOTNOTE_CITATION,
    _SECRET_KEYS,
    _SECRET_VALUE,
    Observation,
    ObservationEvent,
    ResearchRequest,
    _citation_ids,
    _phase_counts,
    _walk_fields,
    phase_for,
    redact,
)
from .research_harness_parts._quality_metrics import report_quality_metrics
from .research_harness_parts._run import (
    SCHEMA_VERSION,
    HarnessFailure,
    ResearchRunResult,
    render_report_markdown,
    render_summary_markdown,
    run_batch,
    run_harness,
    write_artifacts,
)
from .research_harness_parts._search_io import collect_search_io, render_search_timeline
from .research_harness_parts._thrash import analyze_thrash
from .research_harness_parts._transports import (
    DeckCorrelationTransport,
    FakeTransport,
    LiveWebSocketTransport,
    ReplayTransport,
    ResearchTransport,
    _connect,
    _conversation_status,
    _is_terminal_research_frame,
    _transport_for,
)

__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_ACCEPTANCE_CORPUS",
    "ResearchRequest",
    "ObservationEvent",
    "Observation",
    "ResearchTransport",
    "DeckCorrelationTransport",
    "FakeTransport",
    "ReplayTransport",
    "LiveWebSocketTransport",
    "HarnessFailure",
    "ResearchRunResult",
    "run_harness",
    "run_batch",
    "normalize_report",
    "check_invariants",
    "render_report_markdown",
    "write_artifacts",
    "render_summary_markdown",
    "analyze_thrash",
    "report_quality_metrics",
    "collect_search_io",
    "render_search_timeline",
    "redact",
    "phase_for",
    "main",
    # Private helpers kept importable at their original names (tests use them).
    "_BODY_PROCESS_LANGUAGE",
    "_CITATION",
    "_DEPTH_TIMEOUTS_S",
    "_DECK_TIMEOUT_S",
    "_DEPTH_WORD_TARGETS",
    "_depth_word_bounds",
    "_FAILURE_WORDS",
    "_FOOTNOTE_CITATION",
    "_MARKDOWN_HEADING",
    "_PUNCTUATION_ONLY",
    "_SECRET_KEYS",
    "_SECRET_VALUE",
    "_SECTION_MIN_WORDS",
    "_SUMMARY_DIAGNOSTIC_LANGUAGE",
    "_citation_ids",
    "_connect",
    "_conversation_status",
    "_is_terminal_research_frame",
    "_failure_text",
    "_heading_hierarchy_is_valid",
    "_is_stopped_checkpoint",
    "_main_async",
    "_parse_args",
    "_phase_counts",
    "_repetition_is_acceptable",
    "_report_prose",
    "_report_word_count",
    "_section_is_substantive",
    "_transport_for",
    "_walk_fields",
    "_word_count",
]

if __name__ == "__main__":
    raise SystemExit(main())
