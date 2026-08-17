"""Interior modules of `SandboxSession` (`..session`).

`SandboxSession` (tool-sandbox-contract §5) owns ONE authority — a task-scoped,
self-healing sandbox wrapper — and composes it from several larger,
independently-cohesive behaviors this package holds instead of the class body:

* `recreate` — replace a mid-session-dead instance with a fresh one and run
  every best-effort recovery hook (on_recreate rehydrate, C5 memory
  read-back, C3 server rematerialize).
* `memory_recovery` — the C5 `.disco/MEMORY.md` write-through read-back.
* `preview` — the BP-02 static auto-preview (W6 subdir detection + launch).
* `services` — BP-G9 multi-service tracking (`ensure_service`).
* `fetch_inside` — the Fix 2 (B-E) in-sandbox liveness proxy.
* `lifecycle` — session teardown (`destroy`).

Every function takes the live `SandboxSession` (or instance) explicitly and
reads/writes state through that reference — `session.foo`, never a value
captured earlier or a name imported once at module top — because tests
monkeypatch attributes directly on the live session/instance, and the patch
must still take effect when the call happens to originate from one of these
modules instead of the class body itself.
"""

from __future__ import annotations
