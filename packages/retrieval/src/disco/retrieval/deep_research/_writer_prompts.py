"""Generation-side prompt text for `deep_research.writer`.

The prompts that ask a model to PRODUCE report prose: the writing rules, the
whole-report instruction, the cut-off continuation, the one structural re-ask,
the section-scoped rework, and the re-asks used when a call comes back with
nothing usable. The review-side prompts live in `_review_prompts.py`, where the rubric
and the reviewer's JSON contract are read together.

The writer is a funnel, not a tunnel. Nothing here assigns a length, a section
count, or a list of sources to work through: the evidence and the question
decide what the report covers, and the model decides how long that takes to
say. The walls are the ones a report can actually fall through — an uncited
fact, a sentence the reader has already read, a report that narrates its own
research — and each is named back to the writer once, with the exact text and
the exact fix.
"""

from __future__ import annotations

# Written in ordinary case on purpose: a rule shouted in capitals was copied
# into the prose ("The company has PROMISED a turnkey cost…" in a shipped
# report), so the examples read the way the report should.
WRITING_RULES = (
    "1. Synthesize, don't summarize. Connect, compare, and weigh across "
    "sources. Where sources converge, say so and cite the agreement [[s3]] "
    "[[s7]]. Where they diverge — different numbers, different timelines, "
    "conflicting claims — surface the disagreement explicitly with both "
    "citations. Do not write paragraph after paragraph of 'Source A says X "
    "[[s4]]. Source B says Y [[s9]].', and do not stack sourced facts and "
    "stop: a paragraph that lists what is known is not finished until it says "
    "what those facts add up to — which carries the most weight, what they "
    "settle, what they leave open. Connect the cited claims into an argument "
    "that answers the question.\n\n"
    "2. Match the strength of a claim to its source. Attribute vendor claims, "
    "forecasts, disputed claims, and consequential secondary reporting that "
    "has not been verified against its original source. A product announcement "
    "is not demonstrated performance; a stated design life is not observed "
    "service life. Name the organization, author, or document when known. "
    "Unknown authorship is a reason for caution, not permission to assert a "
    "claim as fact. Honor the requested evidence standard: when primary "
    "documentation is requested, a secondary account alone does not satisfy it. "
    "Use an admitted original when its text supports the claim; otherwise "
    "narrow the claim and disclose the limitation, or omit an unreliable detail. "
    "Do not cite a primary page merely because its title matches. A summary "
    "and its original are not independent corroboration.\n\n"
    "3. Measured, analytical register. Cut these words and any like them: "
    "'transformative,' 'revolutionary,' 'poised to revolutionize,' 'pivotal,' "
    "'game-changing,' 'breakthrough,' 'paradigm shift,' 'cutting-edge.' "
    "Describe, weigh, and qualify. The reader is an analyst, not a marketer.\n\n"
    "4. Foreground tension and uncertainty. Where the field disagrees, where "
    "projected timelines slip, where the evidence is one-sided — surface it, "
    "don't smooth it over. The gap between announcements and shipping reality "
    "is often the finding. Tensions go in the body, not in footnotes.\n\n"
    "5. Stay grounded. Every factual claim — including list items, table "
    "cells, and callouts — carries its [[id]] citation, placed right after "
    "the claim it supports: a sentence that joins two facts from two sources "
    "carries a citation after each ('the U.S. fleet passed 26 GW in 2024 "
    "[[s1]] and added 57.6 GWh in 2025 [[s40]]'), and reads as one sentence, "
    "not as two list entries. One citation per claim is the "
    "norm: the source the claim rests on. Several "
    "belong only on a sentence that itself compares those sources or reports "
    "their agreement; a chain of ids after a plain statement of fact is not "
    "support, it is noise the reader has to step over. An inference that goes "
    "beyond what any single source says is written so the reader can tell it "
    "is yours — in your own words, not a stock opening phrase repeated through "
    "the report — and cites the sources it is drawn from. Evidence of one "
    "example establishes at least one, not the only example or a global total. "
    "Preserve dates, populations, product versions, and applicability conditions. "
    "State material limits on what the answer establishes in plain language: "
    "'Available evidence does not establish long-term field performance' is "
    "a limitation, not a claim that no field performance exists. Do not turn "
    "missing coverage into a finding of absence. Do not narrate tool use or "
    "internal checks; that style rule never requires concealing uncertainty. "
    "A number taken from a NOTES finding carries that finding's stated "
    "condition in the same sentence or table cell, and where a source's "
    "contrary notes conflict with how the draft frames that source, the draft "
    "says so.\n\n"
    "6. Visualize when it aids comprehension. Use a compact markdown table to "
    "line entities up across the same attributes. A table carries the values "
    "the prose does not walk through one by one: where the prose has already "
    "said it, the table is a repeat; where the table says it, the prose "
    "points at the table and moves on to what the comparison means. Build "
    "tables only from values that actually appear in the cited sources — "
    "never invent, estimate, or round-fill a data point — and cite every "
    "factual row. Do not emit fenced chart blocks; their data cannot be "
    "verified sentence by sentence.\n\n"
    "7. Vary structure to fit the content: short lists where items are "
    "parallel, a one-line blockquote where a finding carries the load, and "
    "tables per rule 6. Default to analytical prose — these are accents, "
    "not a checklist.\n\n"
    "8. End with the strongest supportable answer to the question. For an "
    "assessment or recommendation, explain material uncertainty and evidence "
    "that would change the judgment. Do not manufacture doubt about directly "
    "established facts or add a formulaic ending to a narrow factual answer. "
    "Rank contenders only for a comparative question; "
    "for historical, predictive, or descriptive questions, use the natural "
    "equivalent. Do not merely repeat the executive summary.\n\n"
    "9. Write for a reader. Organize each section into clear paragraphs that "
    "make the argument easy to follow. Every sentence should be "
    "comprehensible in one pass while preserving the qualifications and "
    "connections the analysis needs. Avoid both dense unbroken blocks and a "
    "run of isolated one-fact sentences each closed by a citation. Put the "
    "executive summary's bottom line first. A "
    "finding is established once, in the section where it does its work; a "
    "later section that needs it refers back to it rather than stating it "
    "again — the same figure or the same body's position appearing in three "
    "sections is three sections that have not decided which of them owns "
    "it.\n\n"
    "10. The report agrees with itself. A fact stated in one part is not "
    "contradicted in another. When the sources disagree about a fact, the "
    "disagreement is written once, as a disagreement, with both citations, "
    "and every later mention keeps that status — the conclusion never states "
    "flatly what the body established as contested."
)

REPORT_PROMPT = (
    "You are writing a complete analytical research report answering one "
    "question from an assembled evidence pool.\n\n"
    "Question: {query}\n\n"
    "{accepted_steering}"
    "Researcher's brief (how the question was read and what was "
    "investigated):\n{brief}\n\n"
    "{coverage_instruction}"
    "{untested_angles}"
    "EVIDENCE (use ONLY these sources — every factual claim carries [[id]] "
    "citations, the id being the short label in brackets at the start of a "
    "source):\n{evidence}\n\n"
    "{citation_ids}"
    "STRUCTURE: open with a concise executive summary (no "
    "heading), then write '## '-headed sections — as many as the evidence and "
    "the question call for, no more — and end with a concluding section. The "
    "three parts have different jobs, and a reader who reads all three never "
    "reads the same sentence twice. The executive summary states the answer "
    "and the findings that carry it, its subject named in the first sentence: "
    "a reader who stops there has the bottom line. The body establishes those "
    "findings: the evidence for each, how it was weighed, where it conflicts, "
    "what remains open. The last section is where the report ends up once the "
    "findings are weighed together — the judgment at its earned confidence, "
    "any decisive uncertainty, and what would change a consequential judgment; it is "
    "not the summary said again, and it needs no mandatory heading named "
    "'Judgment'. Length is yours to judge: write what the evidence supports "
    "and stop; do not pad, and do not work through sources that do not bear "
    "on the question. A source that does not help answer the question is not "
    "cited. Nothing comes after the concluding section.\n\n"
    "WRITE THE REPORT. Follow these rules carefully — they are what "
    "distinguish analysis from a sourced summary:\n\n{rules}"
)

# The citation contract stated as a closed set, filled from the run's alias
# range (`_citation_aliases`). The rules above are unchanged — this only says
# WHICH ids are the ids, because a model handed sixty opaque handles stops
# copying them and numbers the sources itself. Naming the range up front makes
# the model's indexing instinct the CORRECT behaviour instead of a fabrication.
CITATION_ID_CONTRACT = (
    "CITATION IDS: the only valid citation ids are {ids} — the short label in "
    "brackets at the start of each source above. Copy a label exactly as it "
    "appears. Never number the sources yourself and never write an id of any "
    "other form.\n\n"
)

# Appended to the unresolvable-citation finding. A model that invented its own
# scheme has already been told its ids resolve to nothing; the wall only works
# if it comes with the angle, so the line names the valid range. No [[...]]
# markers here on purpose — the finding quotes the broken markers back as the
# ids to CORRECT, and a valid id in that list would read as one of them.
VALID_CITATION_IDS = " The valid citation ids are {ids}."

# ---------------------------------------------------------------------------
# Continuation — ONE writer continuing ONE report, only when it was cut off.
# ---------------------------------------------------------------------------
#
# `finish_reason == "length"` means the reply hit the output ceiling mid-flow,
# so the report so far is replayed as the writer's own assistant turn and this
# asks only for what comes after it, resuming at the exact character it
# stopped on. A reply that ended CLEANLY is a finished report: the model judged
# its own length, and nothing here second-guesses that.

# The report so far is already replayed as the writer's own assistant turn, but
# a long replayed turn is exactly where a local model loses the thread and
# re-opens with a framing sentence it has already written. Putting the LAST
# paragraph directly in the instruction, next to the no-restate rule, lowers
# the repair load.
CONTINUATION_LAST_PARAGRAPH = (
    "THE LAST PARAGRAPH OF THE REPORT SO FAR, verbatim — do not restate any "
    "sentence already written:\n{paragraph}\n\n"
)

CONTINUATION_INSTRUCTION = (
    "Your previous reply was cut off by the output limit. Continue EXACTLY "
    "where you stopped — do not repeat any earlier text and do not add a "
    "preamble. If you were mid-sentence, finish the sentence; if you were "
    "mid-table, finish the table.\n\n"
    "{last_paragraph}"
    "Continue the report from immediately after the text above and finish it: "
    "complete the section in progress, write any further '## ' sections the "
    "evidence and the question still call for, and end with the concluding "
    "synthesis. Do NOT restate, re-summarize, or rewrite anything already "
    "written, do not repeat a heading that already appears, and do not "
    "announce that you are continuing. Every factual claim — including "
    "list items and table cells — still carries its [[id]] citation, resolving "
    "to the evidence above, and the measured analytical register is unchanged."
)

# ---------------------------------------------------------------------------
# Structure — the ONE whole-report re-ask for a draft with no usable shape.
# ---------------------------------------------------------------------------
#
# A report the product cannot split into an executive summary and '## '
# sections cannot be reviewed section by section or shipped as a ReportEvent.
# That is a shape problem, not a prose problem, so the re-ask names the shape
# and asks for the same content in it. The prose stays the model's.

STRUCTURE_NO_SECTIONS = "it has no '## '-headed sections"
STRUCTURE_NO_SUMMARY = "it opens with a '## ' heading instead of a heading-free executive summary"

STRUCTURE_REASK = (
    "Your reply is not in the required shape: {problem}. Return the COMPLETE "
    "report — the same analysis and the same [[id]] citations — in this shape: "
    "a concise executive summary with no heading, then '## '-"
    "headed sections, ending with the concluding synthesis. Change the markup "
    "and the arrangement, not the findings, and add nothing that is not in the "
    "evidence above."
)

# ---------------------------------------------------------------------------
# Rework — ONE section-scoped repair.
# ---------------------------------------------------------------------------
#
# Every finding is tied to one part of the report and quotes the text it is
# about, so the writer rewrites only the parts that have findings and returns
# each of them whole. The parts without findings are never re-generated: a
# whole-report rewrite re-argues prose that already passed and reintroduces
# the class of defect it was sent to fix (two consecutive whole-report reworks
# in an earlier batch each invented fresh unresolvable citations while clearing
# the previous ones). The system splices the returned parts back by heading
# and never edits a sentence itself — a sentence cut by a program breaks the
# flow around it; a sentence rewritten by the writer does not.

REWORK_INSTRUCTION = (
    "Your draft has been reviewed. Each finding below is tied to one part of "
    "the report and quotes the text it is about. Rewrite ONLY the parts named "
    "here, and return each one COMPLETE — every sentence of that part, revised "
    "where a finding calls for it and otherwise kept as it stands — under its "
    "exact heading. A finding marked 'Add this missing section' authorizes "
    "that new heading and states its placement; write the missing coverage "
    "from the admitted evidence within this same revision. "
    "Where a finding quotes a sentence, that sentence "
    "changes: reworded, re-cited, or replaced with analysis the evidence "
    "supports. A readability finding is met by clarifying the quoted passage "
    "while preserving supported qualifications and relationships. Combine or "
    "divide prose as the thought requires; there is no sentence-length, "
    "paragraph-count, or word-count target. Do "
    "not return the parts that are not named: they remain in the final report. "
    "A sentence that already stands in one of those parts stays there; do not "
    "copy or paraphrase it into a returned part. Refer to an earlier finding "
    "only when making a new connection. Every factual claim in a rewritten "
    "part — retained or newly introduced — carries its citation at the claim. "
    "Synthesis may use the citations already in its paragraph; a standalone "
    "synthesis paragraph cites the sources it draws from. Do not add unrequested parts, "
    "do not rename a heading, and introduce no citation that is not in the "
    "evidence above.\n\n"
    "FINDINGS:\n{findings}\n\n"
    "Before returning, check each named task requirement against the revised "
    "parts and retained draft; do not leave a missing angle or an unfinished "
    "thought behind. Return the rewritten parts, each beginning with its '## ' heading line "
    "exactly as given (the executive summary under '## Executive summary'), "
    "and nothing else — no preamble, no commentary, no diff."
)

# One part's heading line in the findings list and in the returned rework.
REWORK_SUMMARY_HEADING = "Executive summary"

# ---------------------------------------------------------------------------
# Empty-response re-asks.
# ---------------------------------------------------------------------------

# The ONE bounded re-ask for a generation call that returned nothing usable —
# an HTTP 200 whose content is empty once the model's internal reasoning is
# stripped. This is the malformed-turn pattern the research loop already uses:
# name what came back, name what is required, and re-send. It is a transport-
# adjacent repair, not a review pass.
EMPTY_REPORT_REASK = (
    "Your previous reply contained NO report text: after its internal "
    "reasoning was removed, the response was EMPTY. What is required is the "
    "COMPLETE report as your visible answer, in the format already specified "
    "— a concise executive summary with no heading, then "
    "'## '-headed sections, with every factual claim carrying its [[id]] "
    "citation that resolves to the evidence above. Write the report itself "
    "now. Do not reply with reasoning, a plan, an apology, or an explanation "
    "of what went wrong."
)

# The same re-ask for a REWORK call that returned nothing. The parts it was
# asked for are named again so the reply is the rework, not a fresh report.
EMPTY_REWORK_REASK = (
    "Your previous reply contained NO report text: after its internal "
    "reasoning was removed, the response was EMPTY. What is required is the "
    "rewritten parts named in the findings above — each COMPLETE, under its "
    "'## ' heading exactly as given — and nothing else. Write them now. Do not "
    "reply with reasoning, a plan, an apology, or an explanation of what went "
    "wrong."
)

# Missing parts and rejected fragments share the same bounded replacement slot.
# Name the actual missing heading or citation failure; keep the original draft
# until the complete requested replacement passes those structural checks.
FRAGMENT_REWORK_REASK = (
    "Your previous reply ({words} words) did not supply the complete requested "
    "replacement: {parts}. The original parts are kept as they were. What is "
    "required is each part named in the findings above rewritten COMPLETE — "
    "its full prose, every factual claim carrying its [[id]] citation that "
    "resolves to the evidence above, under its '## ' heading exactly as given "
    "— and nothing else. Write the whole parts now. Do not reply with "
    "reasoning, a plan, an apology, or a partial sentence."
)

# The same ONE bounded re-ask for a REVIEW call whose content channel came back
# empty. Observed live on two hosted providers: the reviewer reasoned for its
# whole token allowance and returned an HTTP 200 with no JSON. Same repair —
# name what arrived, name what is required, re-send.
EMPTY_REVIEW_REASK = (
    "Your previous reply contained NO verdict: after its internal reasoning "
    "was removed, the response was EMPTY. What is required is ONE strict JSON "
    "object as your visible answer, in the schema already specified: "
    '{"passes": true|false, "failures": [{"rubric": "R3", "section": "<the '
    "exact '## ' heading of the part, or 'executive summary'>\", \"where\": "
    '"<short quote from the draft>", "fix": "<the specific change that makes '
    'it pass>"}]}. Emit that JSON object now, with nothing before or after it. '
    "Do not reply with reasoning, a plan, an apology, or an explanation of "
    "what went wrong."
)

# …and for a review reply that arrived but did not parse. The parse error is
# quoted so the reviewer can see what was wrong with the bytes it sent.
MALFORMED_REVIEW_REASK = (
    "Your previous reply was not a valid verdict: {error}. What is required "
    "is ONE strict JSON object as your visible answer, in the schema already "
    'specified: {{"passes": true|false, "failures": [{{"rubric": "R3", '
    "\"section\": \"<the exact '## ' heading of the part, or 'executive "
    'summary\'>", "where": "<short quote from the draft>", "fix": "<the '
    'specific change that makes it pass>"}}]}}. Emit that JSON object now, '
    "with nothing before or after it — no code fence, no commentary."
)

__all__ = [
    "CITATION_ID_CONTRACT",
    "CONTINUATION_INSTRUCTION",
    "CONTINUATION_LAST_PARAGRAPH",
    "EMPTY_REPORT_REASK",
    "EMPTY_REVIEW_REASK",
    "EMPTY_REWORK_REASK",
    "FRAGMENT_REWORK_REASK",
    "MALFORMED_REVIEW_REASK",
    "REPORT_PROMPT",
    "REWORK_INSTRUCTION",
    "REWORK_SUMMARY_HEADING",
    "STRUCTURE_NO_SECTIONS",
    "STRUCTURE_NO_SUMMARY",
    "STRUCTURE_REASK",
    "VALID_CITATION_IDS",
    "WRITING_RULES",
]
