"""AppKit spec decomposition parts.

Internal split of `..spec` kept under the `python_or_harness_module_logical_gt_700`
module-size budget. Every name here is re-exported from `..spec` (including
underscore-prefixed ones some tests reach through the module object for) — this
package is an implementation detail, never imported directly by anything outside
`..spec` itself.
"""

from __future__ import annotations
