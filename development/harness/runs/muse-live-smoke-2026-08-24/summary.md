# Deep Research harness — FAIL

- Query: What are the latest innovations in AI as of August 2026, and which advances are most consequential?
- Transport: deep_research / depth=quick
- Events: 5 | probes: 0
- Sources: 0 | citations: 0
- Duration: 3449 ms

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
- PASS: thrash_clean
- FAIL: inspect_trace_complete
- FAIL: depth_length_target

## Thrash signals

- PASS: {'query_count': 0, 'unique_query_count': 0, 'max_repeated_query': 0, 'max_repeated_query_streak': 0, 'max_no_progress_streak': 0, 'retrieval_failure_turns': 0, 'malformed_turns': 0, 'max_repeated_deficiency': 0, 'model_io_count': 0}

## Errors

- Driver 'prov-opencode-go-muse-spark-1-2-contributor' rejected the API key: provider prov-opencode-go-muse-spark-1-2-contributor returned HTTP 401 type=authentication_error — Unauthorized
- ReportContractError: 1 validation error for ReportEvent
  Value error, a finished report requires a non-empty executive summary [type=value_error, input_value={'kind': 'report', 'query...], 'pending_probes': []}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.13/v/value_error

## Report
