"""Interior modules of `..archive` (workspace snapshot / rehydrate / zip).

`archive.py` remains the public surface (`snapshot_workspace`,
`rehydrate_workspace`, `zip_workspace`, `aiter_zip_workspace`,
`WorkspaceArchiveError`, `SnapshotResult`, `is_runtime_secret_path`). These
modules hold the cohesive interior pieces it composes, split out to keep the
parent module's logical-line and cyclomatic-complexity budget in check:

* `preserve_copy` — fd-anchored, no-follow copy of unreadable/host-owned
  prior paths into a fresh staging tree (`_copy_preserved_snapshot_paths`).
* `bulk_archive` — validates and materializes a backend-provided tar archive
  without trusting member paths or types (`_write_bulk_archive`).
* `snapshot_walk` — the portable list_dir/read_file fallback walk plus the
  host-owned-path scrub and final staged-tree accounting used by
  `snapshot_workspace`.

Every guard, exception type, and error message string these modules carry is
reproduced verbatim (and in original order) from the code they were split
out of.
"""

from __future__ import annotations
