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

Deferred: the real `SecurityAnalyzer` (Security design) and the critic stop-hook
(BoD §24-D3). The `ToolExecutor` is now fulfilled (below).

**Tool System & Sandbox** (`tool-sandbox-contract.md`) — the `tools` package;
**fulfills the loop's `ToolExecutor` boundary** (the last unfulfilled dependency
of the core). Headless-testable against the `process` backend.

| Module | Contract | What it is |
|---|---|---|
| `tools/anatomy.py` | §2/§3 | `ToolDef` (single-source schema), `Tool`, `ToolOutcome`, `ToolContext`, `Capability` |
| `tools/executor.py` | §4 | `DefaultToolExecutor`: validate→repair, always-returns-`ToolResult`, scope boundary, timeout, kill switch |
| `tools/sandbox/` | §5/§7 | `SandboxSpec`/`SandboxInstance`/`SandboxService` + the `process` backend (clean env, workspace jail) + deny-by-default egress policy |
| `tools/secrets.py` | §6 | `SecretsStore`, `CapabilitySet`, `CapabilityBroker` — secrets stay orchestrator-side; tools get mediated capabilities, never credentials |
| `tools/registry.py` | §8 | `ToolRegistry` + research/agent `ToolScope` presets |
| `tools/builtin/` | §9 | file_read/write/edit, shell, code_exec, search/extract (capability-mediated) |

The §11.7 **acceptance gate** drives the real executor + `process` sandbox from
the loop: file_write→file_read round-trips, observations pair by `call_id`, and a
search resolves via a fake capability with the provider key never in context.
The headline security test asserts an injected secret is absent from the
sandbox's env, filesystem, and process list.

Deferred (need external infra / a contract patch): the `e2b`/Firecracker +
`gvisor` backends (need `/dev/kvm`/E2B), the `browser` (Playwright + dual-LLM +
noVNC) and `deploy_preview` tools, real network-egress enforcement under
`process`, the file-encrypted `SecretsStore`, and the MCP catalogue. One contract
ambiguity flagged: §2's `needs: frozenset[Requirement]` references NETWORK/
FILESYSTEM, which aren't in the router's model-`Requirement` enum — resolved here
with a distinct `Capability` enum (patched into the contract at v1.1).

**Retrieval & Grounding** (`retrieval-grounding-contract.md`) — the `retrieval`
package (a sibling of `tools`); the Research surface's engine. Implements the
router's `NLIVerifier`; the `search`/`extract` tools back onto it via the
capability seam.

| Module | Contract | What it is |
|---|---|---|
| `retrieval/models.py` | §2/§3/§5 | `SearchHit`, `ExtractedDoc`, `Passage` (provenance unit), `Retrieval{Request,Result}`, `Claim`/`VerifiedClaim`/`GroundedAnswer` |
| `retrieval/providers.py` | §2 | `SearchProvider`/`ExtractionProvider` protocols (SearXNG/Firecrawl defaults deferred) + explicit-failure status |
| `retrieval/ranking.py` | §3 | RRF, `Reranker`/`Embedder`/`QueryRewriter` protocols + `LexicalReranker`/`HashingEmbedder`/`RouterQueryRewriter` |
| `retrieval/engine.py` | §3 | `DefaultRetrievalEngine`: transform→discover→RRF→extract→rerank, provenance + `all_hits` |
| `retrieval/vectorstore.py` | §4 | `InMemoryVectorStore` (namespace-isolated) + `DefaultCorpusService` (Space silos, ownership) |
| `retrieval/nli.py` | §5.1 | `CrossEncoderNLIVerifier` (implements the router's `NLIVerifier`) |
| `retrieval/grounding.py` | §5 | `GroundingPipeline`: constrained gen → claim extraction → NLI verify → self-correct → honest surfacing |

The §8.5 **acceptance gate** composes four contracts: a Research-scope loop calls
`search`/`extract` (tools) whose capability handlers back onto the engine, then
grounding produces a verified `GroundedAnswer` — with the provider key never in
any observation or the answer. The headline guarantees are mutation-checked:
namespace isolation (a Space query never pulls another corpus) and the
snippet-is-never-cited rule (citations come only from extracted passages).

Deferred ([VERIFY]/external): live SearXNG/Firecrawl HTTP providers + owned
endpoints, the real `bge-m3`/`bge-reranker-v2-m3`/DeBERTa-NLI checkpoints (the
shipped reranker/embedder/NLI are deterministic stubs), and the answer UI
(BoD §13.3, the next thing built).

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

Optional extras live on the member packages (not the workspace root), so install
them with `--package`:

```bash
# Audio overviews (RP-09): bundled in-process Kokoro TTS — kokoro-onnx + lameenc.
# Weights (~0.3 GB) download to ~/.cache/perpleximanus-tts on first use.
uv sync --package perpleximanus-agent-server --extra tts
```

A bare `uv sync --extra tts` errors — the root defines no `tts` extra. Without this
extra installed, the audio-overview tool stays importable but fails soft when run.

## The load-bearing idea

The append-only event log is the single source of truth. `State` (what the loop
knows) and `View` (what the LLM sees) are **pure functions** of the ordered log;
forgetting is done with **condensation tombstones**, never deletion. That one
decision buys replay, resume-after-disconnect, audit, and bounded memory — see
event-state-contract §1 for the invariants this rests on.
