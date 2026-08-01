"""AppKit generator decomposition parts.

Internal split of `..generator` kept under the
`python_or_harness_module_logical_gt_700` module-size budget (and each callable
under the 100-logical-line / 15-McCabe caps). Every name here is re-exported
from `..generator` (including underscore-prefixed ones — sibling primitive
modules reach into `generator` for private emitters via lazy imports, and tests
reach through the module object too), so this package is an implementation
detail: import from `..generator`, never from these submodules directly.

The five production-template emitters (`_emit_component`, `_emit_form_component`,
`_emit_worker_ts`, `_emit_disco_client_ts`, `_emit_owner_guide_md`) are split as a
PURE VERBATIM PARTITION: each helper below is a straight cut of the original
single string-concatenation, in the same order, with no character added,
removed, or reordered. Byte-identity of `generate()` output is a hard contract
(deploy-shipped Cloudflare TS/MD) and is verified by an external hash-comparison
script against the pre-split bytes, not by any test in this tree alone.
"""

from __future__ import annotations
