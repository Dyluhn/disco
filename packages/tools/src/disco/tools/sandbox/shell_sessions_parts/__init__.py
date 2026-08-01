"""Interior modules of `ShellSessionManager` (`..shell_sessions`).

`ShellSessionManager` owns ONE authority — persistent named shell sessions
backed by tmux — and composes it from cohesive pieces this package holds:

* `text` — pure marker/output parsing (no tmux, no state): stripping a raw
  pane delta down to the command's real output, building the private
  completion-record dispatch, and detecting a trailing shell `&`.
* `exec_dispatch` — running one `exec()` round-trip against a session and
  attributing its outcome to the persistent-server registry.
* `rematerialize` — C3 replay of the persistent-server registry onto a fresh
  instance after a sandbox recreate, and revoking one tracked generation.

`exec_dispatch` reads two module-level tuning constants
(`_EXEC_WAIT_S`/`_POLL_S`) and `_BACKGROUND_OWNER_ATTEMPTS`/
`_BACKGROUND_OWNER_INTERVAL_S`) that tests monkeypatch directly on
`..shell_sessions`. It resolves them through the imported PARENT MODULE at
call time (`shell_sessions._EXEC_WAIT_S`), never via a name captured once at
import top, so the patch still takes effect when the call originates here
instead of the class body.
"""

from __future__ import annotations
