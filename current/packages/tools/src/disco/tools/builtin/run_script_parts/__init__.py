"""Interior modules of `RunProjectScriptTool.run` (`..run_script`).

`run()` runs a buffered batch through three phases the pieces here mirror:

* `outcomes` — the `ToolOutcome` builders (`_fail`, `_success`,
  `_commit_failure`, `_exception_detail`); pure, no sandbox/ctx dependency, and
  shared by `run()` itself and every phase below.
* `state` — `_ScriptState`, the REAL-path-keyed buffer/original/grounding
  bundle `run()` builds once per call, plus the `key`/`disk_read`/`load`
  helpers that used to be closures inside `run()`. Bundling them here (instead
  of passing eight separate dict/set parameters) is what lets the per-op
  handlers below live as ordinary functions instead of nested closures —
  nested closures would keep counting their lines against `run()`'s own
  logical-LOC budget even though the AST-McCabe walk does not descend into
  them (each nested `def` is still physically inside `run()`'s line span).
* `ops` — one handler per operation kind (`ls` / `read` / `replace_text` /
  `save`) plus the dispatcher `run()` calls per operation.
* `prevalidate` — turns the raw `mutated` buffer into the byte-diffed,
  size-capped, syntax-checked set of paths that are actually going to commit.
* `commit` — commits each effective path in order, attributing any handled
  backend failure to a proven `unchanged` / `committed` / `unknown` state
  exactly as the pre-extraction inline loop did.
"""

from __future__ import annotations
