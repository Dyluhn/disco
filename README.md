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

The **wire layer** (event-state-contract §7, checklist items 7–8) lives in the
`agent-server` package: a FastAPI WebSocket endpoint (`state` + `event` frames,
reconnect/replay via `last_seq`, the pending-message path) and the §7.5 REST
surface (create / send / history / state / list), both thin adapters over the
core `EventStore` — `core` itself stays server-free. `WSServerFrame`/
`WSClientFrame` are core data types (`core/wire.py`). This completes the Phase 0
"walking skeleton" for the event/state spine.

Deferred to later phases: the real `LLMSummarizingCondenser` (Phase 1), the
agent loop, tools, retrieval, and UI (BoD §22).

**LLM Router boundary** (`llm-router-contract.md`) — the Phase-1 subsystem the
loop and condenser depend on. Capability-based, provider-neutral model access;
it *implements* the two functions the spine left waiting on
(`is_context_window_exceeded`, the `Summarizer`). Fully headless-testable behind
a faked provider.

| Module | Contract | What it is |
|---|---|---|
| `llm/types.py` | §2/§4 | CapabilityProfile, CompletionRequest/Response, RoutingDecision, StreamChunk |
| `llm/errors.py` | §6 | typed `LLMError` hierarchy + `is_context_window_exceeded` |
| `llm/provider.py` | §3 | `ModelProvider` protocol (live HTTP adapters deferred — see below) |
| `llm/config.py` | §7 | `RouterConfig` + the §7 starting role assignments (placeholder ids) |
| `llm/policy.py` | §5 | `ThresholdOverflowPolicy` — the five overflow rules + local-only roles |
| `llm/routing.py` | §4 | `DefaultLLMRouter`: resolution, prompt injection, retry/escalation, cost governance, decision emission |
| `llm/prompts.py` | §8 | `PromptProvider` + family×mode selection |
| `llm/summarizer.py` | §9.1 | `RouterSummarizer` (satisfies the spine's `Summarizer`) |
| `llm/nli.py` | §9.2 | `NLIVerifier` protocol + stub (real cross-encoder built with grounding) |

Deferred: the live `ollama`/`llamacpp`/`openrouter` HTTP adapters (network +
the operator's real model ids/keys, all `[VERIFY]`); the whole router is proven
headless against a faked provider, so adding an adapter is purely implementing
`ModelProvider` + classifying that provider's context-window error.

**Agent loop & orchestration** (`agent-loop-contract.md`) — the core control
loop, and the first subsystem requiring *both* prior contracts. Headless and
transport-agnostic: an explicit status state machine over one-action-per-
iteration steps, with the event log as the only source of truth.

| Module | Contract | What it is |
|---|---|---|
| `loop/boundaries.py` | §3 | `Agent`/`ToolExecutor`/`SecurityAnalyzer`/`ConfirmationPolicy`/`StopHook` protocols + `AgentStep` |
| `loop/engine.py` | §2,§4,§5,§7,§8 | `AgentLoop`: state machine, FIFO lock, two-phase confirmation, condensation/hard-reset wiring, steering, control ops |
| `loop/stuck.py` | §6 | `StuckDetector` (four patterns via `event_content_eq`) |
| `loop/agent.py` | §3 | `RouterAgent` — the concrete `Agent` wrapping the router |
| `loop/policies.py` | §5 | `NeverConfirm`/`AlwaysConfirm`/`ConfirmRisky` + provisional analyzers (pending the Security contract) |

The §10.9 **acceptance gate** composes all three contracts end-to-end with
fakes (loop + event-state + router), asserting the log replays to the same
`ConversationState` and `request_id` flows into `ActionEvent.llm_response_id`.

Deferred: the real `ToolExecutor` (the next contract — Tool/Sandbox), the real
`SecurityAnalyzer` (Security design), and the critic stop-hook (BoD §24-D3).

## Layout

```
.
├── basis-of-design.md          # cornerstone design document
├── event-state-contract.md     # the spine's binding contract
├── pyproject.toml              # uv workspace root (virtual; dev toolchain)
└── packages/                   # uv workspace (BoD §5, four-layer topology)
    ├── core/                   # "the brain": no server, no UI, importable
    │   ├── src/perpleximanus/core/
    │   └── tests/              # the contract's §8 correctness suite
    ├── agent-server/           # per-conversation runtime: WebSocket + REST (§7)
    ├── tools/                  # the action space (placeholder until Tool/Sandbox)
    └── app-server/             # user-facing orchestrator (placeholder)
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
