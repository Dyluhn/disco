"""Pure revision/coverage accounting for model-facing resource context.

Compatibility facade: the implementation now lives in :mod:`resource_receipts`,
the single bounded owner. This module re-exports every public/private name,
signature, receipt, and context/resource result so consumers are unchanged.

This module deliberately distinguishes three facts that the prior context
pipeline conflated:

* a resource revision existed on disk;
* some range was delivered in a historical turn; and
* the exact range is physically present in the request being assembled now.

Only the third fact permits a read body to be replaced by a pointer.  The event
log remains append-only; these helpers select the smallest lossless set of read
observations for rendering and never mutate persisted events.
"""

from __future__ import annotations

from .resource_receipts import (  # noqa: F401 — re-exported for back-compat
    PROMPT_READ_MAX_RENDERED_BYTES,
    PROMPT_READ_MAX_RESOURCES,
    WORKSPACE_FILE_NAMESPACE,
    ReadReceiptRecord,
    canonical_workspace_identifier,
    coverage_is_subset,
    current_prompt_coverage_lines,
    essential_read_call_ids,
    essential_read_pair_seqs,
    latest_read_revision_by_resource,
    merge_spans,
    read_receipt_records,
    receipt_covered_in_current_prompt,
    rendered_content_matches_receipt,
    select_essential_read_records,
    select_prompt_read_records,
    snapshot_receipt,
    snapshot_window_receipt,
    workspace_file_key,
)
from .resource_receipts import (
    _record_order as _record_order,
)
from .resource_receipts import (
    _workspace_read_receipts as _workspace_read_receipts,
)
