"""Interior modules of the tool effect executor (`..executor`).

The executor owns one authority — mediating a validated tool call into exactly
one `ToolResult` — and these modules hold the cohesive pieces it composes:

* `arguments` — annotation introspection and provider-dialect argument repair;
* `validation_message` — the model-facing, self-correcting validation failure;
* `catalog` — the read-only projection of "which tools are callable, and what
  do they declare"; it holds no effects and no lifecycle;
* `invocation` — the pre-execution admission ladder and the ToolResult shapes.

Nothing here reaches back for the executor instance: each function receives the
exact values it needs. `..executor` remains the only public import surface.
"""

from __future__ import annotations
