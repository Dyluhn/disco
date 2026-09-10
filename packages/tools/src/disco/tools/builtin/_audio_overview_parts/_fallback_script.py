"""The deterministic, report-derived script and the spoken-duration cap.

Two jobs, both about the shape of the FINISHED script rather than the driver:

``_fallback_turns`` builds a real overview straight from the report text when
the driver produced nothing usable. The report is already a structured digest
(``Query:`` / ``Executive summary:`` / ``Section: <title>`` + prose, as
``report_to_overview_text`` renders it), so an honest walkthrough is a
mechanical transform of it -- headings plus the leading summary sentences,
alternating hosts in podcast mode. It is a LAST RESORT and is never described
to the model: the driver must not learn that a degraded path exists.

``_cap_script_duration`` holds the finished overview to a target listening
time. It trims by dropping whole trailing SENTENCES (and, only if that is not
enough, whole trailing turns) -- never mid-sentence, never mid-word.
"""

from __future__ import annotations

import re

from ._types import Turn, _min_viable_turns, _turn_band

# Kokoro v1.0 at speed 1.0 lands near 155 spoken words per minute; the remote
# OpenAI-compatible voices are within a few percent. Used only to convert the
# target duration into a word budget -- an estimate, never reported as fact.
_WORDS_PER_MINUTE = 155

# The overview is a companion to the report, not a replacement: nine minutes is
# the long end of what people finish. A longer report is summarised down to it.
_TARGET_SECONDS = 540

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT.split(text.strip()) if part.strip()]


def _word_budget(target_seconds: int = _TARGET_SECONDS) -> int:
    return max(1, target_seconds * _WORDS_PER_MINUTE // 60)


def _cap_script_duration(
    turns: list[Turn], mode: str, *, target_seconds: int = _TARGET_SECONDS
) -> tuple[list[Turn], bool]:
    """Hold the script to ``target_seconds`` of speech. Returns (turns, capped).

    Pass 1 shortens each turn to the leading complete sentences that fit its
    share of the word budget (always keeping at least the first sentence, so no
    turn is ever emptied). Pass 2 drops whole turns from the END if the script
    is still over, never below the mode's minimum viable count.
    """
    if not turns:
        return turns, False
    budget = _word_budget(target_seconds)
    spoken = sum(len(turn.text.split()) for turn in turns)
    if spoken <= budget:
        return turns, False

    per_turn = max(25, budget // len(turns))
    trimmed: list[Turn] = []
    for turn in turns:
        sentences = _sentences(turn.text) or [turn.text.strip()]
        kept = [sentences[0]]
        used = len(sentences[0].split())
        for sentence in sentences[1:]:
            words = len(sentence.split())
            if used + words > per_turn:
                break
            kept.append(sentence)
            used += words
        trimmed.append(Turn(speaker=turn.speaker, text=" ".join(kept)))

    floor = _min_viable_turns(mode)
    while len(trimmed) > floor and sum(len(t.text.split()) for t in trimmed) > budget:
        trimmed.pop()
    return trimmed, True


def _report_blocks(report_text: str) -> tuple[str, str, list[tuple[str, str]]]:
    """Split the flattened report into (query, summary, [(heading, body), ...]).

    Tolerates a report that has none of the labels: the whole text then becomes
    one unnamed block, which still narrates.
    """
    query = ""
    summary = ""
    sections: list[tuple[str, str]] = []
    heading = ""
    body: list[str] = []

    def flush() -> None:
        text = " ".join(body).strip()
        if heading or text:
            sections.append((heading, text))
        body.clear()

    for raw_line in report_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Query:"):
            query = line[len("Query:") :].strip()
            continue
        if line.startswith("Executive summary:"):
            summary = line[len("Executive summary:") :].strip()
            continue
        if line.startswith("Section:"):
            flush()
            heading = line[len("Section:") :].strip()
            continue
        body.append(line)
    flush()
    return query, summary, sections


def _fallback_turns(report_text: str, *, mode: str) -> list[Turn]:
    """Build a script from the report itself. Empty list when there is no text.

    Podcast mode alternates the two hosts so the result is still a dialogue;
    single mode narrates throughout. Every sentence comes from the report, so
    the fallback never invents a finding.
    """
    query, summary, sections = _report_blocks(report_text)
    _min_turns, max_turns = _turn_band(mode)

    lines: list[str] = []
    if query:
        lines.append(f"Here's an overview of the research on {query}.")
    if summary:
        lines.extend(_sentences(summary)[:3])
    for heading, body in sections:
        sentences = _sentences(body)[:2]
        if heading and sentences:
            lines.append(f"{heading.rstrip('.')}. {' '.join(sentences)}")
        elif heading:
            lines.append(f"Next, {heading.rstrip('.')}.")
        elif sentences:
            lines.extend(sentences)
    if not lines and report_text.strip():
        lines = _sentences(report_text)[:max_turns]
    if not lines:
        return []
    if len(lines) > 1:
        lines.append("That's the shape of the findings — the full report has the detail.")

    turns: list[Turn] = []
    for position, line in enumerate(lines[:max_turns]):
        speaker = "A" if mode == "single" or position % 2 == 0 else "B"
        turns.append(Turn(speaker=speaker, text=line))
    return turns
