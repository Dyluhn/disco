"""Interior modules of `FindAndEditTool.run` (`..find_and_edit`).

`find_and_edit.py` keeps the driver-LLM plumbing (`_call_llm`, `_LLMResponse`,
`_decide_match`, ...) — tests patch `find_and_edit._call_llm` and read
`find_and_edit.httpx` directly, so those names stay anchored on the parent
module. This package holds the two phases either side of the LLM decision:

* `scan` — read every selected file, mark it grounded, and collect each
  non-empty regex match as a `_Match` alongside a per-file result record.
* `apply` — turn accepted decisions into grouped `ExactReplaceEdit`s, commit
  them per file, and summarize the resulting match statuses.
"""

from __future__ import annotations
