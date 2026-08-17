"""C5 — MEMORY write-through read-back (`SandboxSession._recover_pmx_memory`).

Reads `.disco/MEMORY.md` (legacy `.pmx/` as a fallback) off the live instance
after a mid-session recreate and stages its facts in
`session._recovered_memory_facts` for the agent loop to drain via
`take_recovered_memory_facts`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..session import SandboxSession

_LOG = logging.getLogger(__name__)


async def recover_pmx_memory(session: SandboxSession) -> None:
    """C5 — read `.disco/MEMORY.md` (legacy `.pmx/` as a fallback) from the LIVE
    instance and stage its facts in `session._recovered_memory_facts` for the agent
    loop to drain. Called from `_recreate` AFTER the user's on_recreate hook has
    run and the fresh box is up.

    A missing file (no prior `remember` calls → no facts to recover) is
    a normal, silent no-op: the cache stays None and the loop sees no
    recovery. A present-but-malformed file is logged and ignored: a
    bad mirror must not break the next step. A transient I/O error is
    also best-effort (a dead box can't recover, but the loop will
    surface the error via the next tool call anyway).
    """
    data = None
    for mem_path in (session._MEMORY_PATH, session._LEGACY_MEMORY_PATH):
        try:
            data = await session.read_file(mem_path)
            break
        except (FileNotFoundError, NotADirectoryError):
            continue  # try the legacy path, then give up
        except Exception:  # noqa: BLE001 — read flakiness on a fresh box is best-effort
            _LOG.debug("disco memory read-back failed (no facts recovered)", exc_info=True)
            session._recovered_memory_facts = None
            return
    if data is None:
        # No mirror (new or legacy) on the new box — nothing to recover. Leave
        # cache None (a fresh box / no prior remember) so the loop sees clean state.
        session._recovered_memory_facts = None
        return
    if not data:
        session._recovered_memory_facts = None
        return
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — decode error
        session._recovered_memory_facts = None
        return
    facts: list[tuple[str, str]] = []
    current_scope = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            current_scope = line[3:].strip()
            continue
        # Skip the leading comment / headings — they're prose, not facts.
        if line.startswith("# "):
            continue
        # List item: `- <snippet>`. The write-through only ever produces
        # `- ` list items under `## <scope>` headings, so this parser
        # matches the writer exactly. (An empty line just advances.)
        if line.startswith("- "):
            snippet = line[2:].strip()
            if snippet:
                facts.append((current_scope, snippet))
    session._recovered_memory_facts = facts or None
