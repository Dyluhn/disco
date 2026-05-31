# perpleximanus

A self-hosted **Research + Agent** platform — a Perplexity-class answer engine
and a Manus-class autonomous agent, built as **one agent core with two scoped
surfaces** over an **append-only event log**.

> Design authority: [`basis-of-design.md`](./basis-of-design.md) (the cornerstone)
> and [`event-state-contract.md`](./event-state-contract.md) (the spine's binding
> contract). When code and prose disagree, those documents win.

## Status

**Phase 0 — the Event & State spine.** This is the foundation every other
subsystem binds to: the typed event hierarchy, the append-only `EventStore`,
pure `State`/`View` reconstruction, the condenser seam, and content-equality for
stuck detection. It builds and is fully tested **headless — no LLM, no server**
(basis-of-design §5.1, §20).

Implemented (event-state-contract §10, steps 1–6):

| Module | Contract | What it is |
|---|---|---|
| `events.py` | §2 | Event hierarchy, value objects, the discriminated `Event` union |
| `migration.py` | §4.1 | `migrate_event` — forward-only, lossless schema evolution |
| `state.py` | §3 | `ConversationState.reconstruct` — pure projection of the log |
| `view.py` | §5 | `View.of` (tombstone-applying) + `Condenser`/`Summarizer` seams |
| `equality.py` | §6.3 | `event_content_eq` — semantic equality ignoring volatile fields |
| `store/` | §6 | `EventStore` protocol + dependency-free `SqliteEventStore` |

Deferred to the server package / later phases: the WebSocket + REST wire layer
(§7, contract steps 7–8), the real `LLMSummarizingCondenser` (Phase 1), the
agent loop, tools, retrieval, and UI (BoD §22).

## Layout

```
.
├── basis-of-design.md          # cornerstone design document
├── event-state-contract.md     # the spine's binding contract
├── pyproject.toml              # uv workspace root (virtual; dev toolchain)
└── packages/
    └── core/                   # "the brain": no server, no UI, importable
        ├── src/perpleximanus/core/
        └── tests/              # the contract's §8 correctness suite
```

`perpleximanus` is a PEP 420 namespace package, so future workspace members
(`perpleximanus.tools`, `.agent_server`, `.app_server` — BoD §5) join the same
namespace without a rename.

## Develop

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
uv sync --all-packages     # provision the venv + install every workspace member
uv run pytest              # the headless contract test suite (49 tests)
uv run ruff check .        # lint
uv run ruff format .       # format
```

## The load-bearing idea

The append-only event log is the single source of truth. `State` (what the loop
knows) and `View` (what the LLM sees) are **pure functions** of the ordered log;
forgetting is done with **condensation tombstones**, never deletion. That one
decision buys replay, resume-after-disconnect, audit, and bounded memory — see
event-state-contract §1 for the invariants this rests on.
