# Deep Research harness — FAIL

- Query: What are the latest innovations in AI as of August 2026, and which advances are most consequential?
- Transport: deep_research / depth=quick
- Events: 37 | probes: 11
- Sources: 0 | citations: 0
- Duration: 267502 ms

## Invariants

- FAIL: final_report_exists
- FAIL: executive_summary_substantive
- PASS: executive_summary_synthesizes_report
- FAIL: no_empty_or_failure_sections
- FAIL: substantive_section_bodies
- FAIL: section_citations_present
- FAIL: citations_resolve
- PASS: no_diagnostic_or_research_process_language
- FAIL: heading_hierarchy_valid
- FAIL: repetition_acceptable
- PASS: stopped_checkpoint_valid
- PASS: bounds_and_errors_recorded
- FAIL: thrash_clean
- PASS: inspect_trace_complete
- FAIL: depth_length_target

## Thrash signals

- FAIL: {'query_count': 11, 'unique_query_count': 11, 'max_repeated_query': 1, 'max_repeated_query_streak': 1, 'max_no_progress_streak': 0, 'retrieval_failure_turns': 0, 'malformed_turns': 2, 'max_repeated_deficiency': 0, 'model_io_count': 12}

## Errors

- report retained hard deficiencies after rework: Report is 2652 words; the assigned range is 1500-2500. Tighten the prose without dropping cited findings (rubric R6).

## Report
