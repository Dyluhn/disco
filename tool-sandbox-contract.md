# Technical Design Contract — Tool System & Sandbox

**Document type:** Detailed Technical Design (Contract Spec)
**Subsystem:** Tool System & Secure Execution — BoD §10 (tools) + §11 (sandbox)
**Status:** v1.0 — authoritative contract
**Depends on:**
- Event & State contract (v1.2) — `ToolCall`, `ToolResult` (the loop's action/observation correlation), `SecurityRisk`.
- LLM Router contract (v1.1) — `ToolSpec` (the provider-neutral tool description the model sees), `Requirement`.
- Agent Loop contract (v1.1) — the `ToolExecutor` boundary this document **fulfills** (`execute(ToolCall)->ToolResult`, `available_tools()->[ToolSpec]`), and the `SecurityAnalyzer` boundary this document **feeds** (it scores `ActionEvent`s whose tool calls originate here).
**Consumed by:** the agent loop (via `ToolExecutor`), the Research/Agent surfaces (which select tool scopes), the security subsystem (which scores tool actions), and the retrieval subsystem (whose search/extract tools are registered here).
**Builds on the OpenHands lessons (re-implemented):** the spec-vs-instance sandbox split, secrets-outside-the-sandbox, dedicated file tools over shell redirection, and CodeAct as the preferred action representation. Cited inline as `[OH]`.

> **Implementation note (perpleximanus build).** This subsystem is implemented in the `tools` package and fulfills the loop's `ToolExecutor`. Two deviations were made and are flagged in-code for a contract patch:
> 1. **§2 `ToolDef.needs`** is typed `frozenset[Requirement]` and references NETWORK/FILESYSTEM, but the router's `Requirement` enum holds only model capabilities. These are execution-environment capabilities — a distinct concept — so the build introduces a `Capability` enum (NETWORK/FILESYSTEM/DISPLAY). **This contract should be patched to reference `Capability`.**
> 2. **§2 `Tool.run(args: BaseModel, ...)`** is typed `args: Any` in the build so concrete tools can narrow `args` to their specific `args_model` without tripping Python's contravariant-parameter rule; the executor guarantees a validated instance. Conceptually still a validated `args_model`.
> Deferred (need external infra): the `e2b`/Firecracker + `gvisor` backends, the `browser` and `deploy_preview` tools, real egress enforcement under the `process` backend, the file-encrypted `SecretsStore`, and the MCP catalogue.

---

## 0. What this document is (and is not)

**Is:** the binding contract for (a) the **tool system** — how a tool is defined, validated, and executed, and how the loop discovers what tools it may call; and (b) the **sandbox** — how untrusted, agent-generated work is isolated, what crosses the boundary, and how secrets and egress are controlled. It **fulfills the loop's `ToolExecutor` dependency** (the last unfulfilled boundary in the core) and pins the §11 security constraints to concrete enforcement points.

**Is not:** the agent loop (it consumes this), the security analyzer's *scoring* internals (a separate design — this defines where scoring plugs in and what the sandbox enforces regardless), the retrieval pipeline's internals (§9 — its search/extract tools are registered here but specified there), or the specific deploy/preview implementation. Interior choices below the contract line (which HTTP client a tool uses, exact image contents, warm-pool sizing) are the builder's.

**Conventions** (identical to prior contracts): illustrative Python 3.12 + Pydantic v2; field names/types/signatures **normative**, bodies illustrative. **[CONTRACT]** = relied-upon guarantee; **[INTERIOR]** = builder's free choice; **[VERIFY]** = confirm at build (E2B/Firecracker availability, image contents, library versions).

---

## 1. Foundational principles [CONTRACT]

1. **The sandbox interior is hostile** (BoD Principle 4, the load-bearing Manus lesson). No secret, credential, or internal config is ever present anywhere the agent-run code can read it — not in env vars, not in files, not in the process list. The orchestrator holds secrets; the sandbox receives only narrow, short-lived, scoped capabilities (§6).
2. **Deny-by-default egress.** A sandbox reaches *nothing* on the network unless a per-task allowlist grants a specific destination (§7). This is enforced outside the guest (by an egress proxy / network policy), not by trusting in-guest configuration.
3. **Every tool call is validated before execution, and auto-repaired on malformed input** (BoD §10.1/§10.3). The reliability of the whole system rests on tool-call robustness — the research showed a tool-success rate going from ~7% to ~100% by tightening schema + harness with no model change. Validation is a contract, not a nicety.
4. **One tool call in, one result out** — the executor fulfills the loop's `ToolCall → ToolResult` boundary exactly: a single call yields a single result (success or failure), correlated by `call_id`. The loop turns that into the event log's action/observation pairing.
5. **Spec vs instance** `[OH]`. A *sandbox spec* (template: image, tools, limits, egress policy) is distinct from a *sandbox instance* (a running, lifecycle-managed environment created from a spec). Tools execute against an instance.
6. **Risk is assessed before execution, by the security boundary** (BoD §17 / loop §5). The executor does not decide policy; it presents each proposed tool call for scoring and honors the loop's confirmation gate. But the sandbox enforces hard isolation *regardless* of any risk score (defense in depth — a mis-scored action still can't escape the box or read a secret).
7. **Tools are registered, scoped, and pluggable.** The available toolset is a registry; surfaces (Research vs Agent) expose different *scopes* of it (BoD §8); external tools join via MCP (§10). The loop sees only `available_tools()` for its surface.
8. **The executor is transport- and backend-agnostic.** The loop neither knows nor cares whether a tool runs in-process, in a local subprocess, or in a Firecracker microVM. Backends are swappable behind one interface (§5).

---

## 2. Tool anatomy [CONTRACT]

Every tool is three separable things (BoD §10.1): a **schema** (what the model sees and how arguments are validated), a **validator/repair** wrapper, and an **executor** (what actually runs). This separation is what lets the same tool run in different sandbox backends and lets validation be uniform across all tools. See `tools/anatomy.py`.

## 3. Validation & auto-repair [CONTRACT]

Before any tool runs, its `ToolCall.arguments` are validated against the tool's `args_model`. On failure, the executor does **not** crash the step — it returns a structured, model-readable error so the loop's next iteration can correct it. Unknown tool → `unknown_tool`; bad args → `invalid_arguments` with the Pydantic errors + schema; never execute a coerced/partial call. Repair is the loop's job (the failed result re-enters the View); identical repeated failures trip the stuck detector. See `tools/executor.py`.

## 4. The Executor — fulfilling the loop's `ToolExecutor` [CONTRACT]

`execute()` always returns a `ToolResult` (success or a structured failure) and never raises to the loop; `ToolResult.call_id == ToolCall.call_id`; `available_tools()` returns only the current scope's tools. See `tools/executor.py::DefaultToolExecutor`.

## 5. The Sandbox — spec, instance, backends [CONTRACT]

`SandboxSpec` (template) vs `SandboxInstance` (running) vs `SandboxService` (factory). Backends: `process` (dev), `e2b`/Firecracker (prod, [VERIFY] /dev/kvm), `gvisor` (fallback), `remote` (future). Instances carry `owner_id`/`conversation_id`, never shared across owners; Linux-only. See `tools/sandbox/`.

## 6. Secrets, capabilities & the execution context [CONTRACT]

No secret is ever readable from inside the sandbox. Secrets live only in the orchestrator's `SecretsStore`. Tools needing a credential receive a scoped, short-lived *capability* (a mediated handle) via `CapabilitySet`, never the credential; the privileged call runs orchestrator-side. Ungranted capability → `denied`. The kill switch (§6.4) revokes capabilities + egress and destroys the instance. See `tools/secrets.py`.

## 7. Egress control [CONTRACT] — deny-by-default

Empty `egress_allow` ⇒ no network; per-task allowlist; enforced outside the guest. The `process` backend models the policy (`SandboxSpec.egress_allowed`) with a documented weaker guarantee; `e2b`/`gvisor` enforce for real.

## 8. Tool registry & scoping [CONTRACT]

`ToolScope` per surface (research excludes world-affecting tools; agent gets the full set). `available_tools()` and the executor honor scope as the first-line boundary. See `tools/registry.py`.

## 9. The core toolset (v1) [CONTRACT]

`browser`, `shell`, `file_read`/`file_write`/`file_edit` (dedicated file tools, not shell redirection), `code_exec` (CodeAct), `search`/`extract` (capability-mediated, provider key out of the box), `deploy_preview`. The build ships file/shell/code_exec/search/extract; browser/deploy_preview deferred. See `tools/builtin/`.

## 10. MCP (external tools) [CONTRACT boundary]

External MCP tools adapt into `ToolDef`/`Tool` like any other — same validation, scoping, secrets/egress discipline. v1: seam only.

## 11. Test plan [CONTRACT — defines correctness]

Headless; the `process` backend (or a fake) for tests. Covers: anatomy/validation (11.1), executor↔loop boundary (11.2), sandbox spec/instance/backend (11.3), secrets/capabilities incl. the no-secret-in-box headline (11.4), egress deny-by-default (11.5), risk seam & kill switch (11.6), and the cross-contract acceptance gate (11.7). See `packages/tools/tests/`.

## 12. What this contract hands to / expects from each subsystem

- **Agent loop:** fulfills its `ToolExecutor` boundary.
- **Security:** feeds the `SecurityAnalyzer` (base_risk + raw shell command surfaced) and honors the `ConfirmationPolicy`; sandbox isolation is independent of scoring.
- **Retrieval:** `search`/`extract` tools registered here reach providers via the §6 capability mechanism.
- **Router:** tools' `to_spec()` produces the `ToolSpec`s; the OpenRouter key is a §6 secret.
- **Agent-server:** owns the `SandboxInstance` lifecycle; routes the kill switch.

## 13. Build order & sequencing

Phase-3 subsystem and the last unfulfilled dependency of the loop. Build order: anatomy → validation/executor → process backend + file tools → secrets/capabilities (+ no-secret test) → egress → registry/scoping → core toolset → e2b backend (after /dev/kvm check) → risk seam + kill switch → §11.7 gate → MCP seam.

### Appendix — open items
- **[VERIFY] E2B/Firecracker on the Proxmox host** — the `/dev/kvm` check gates `e2b`.
- **[VERIFY] base image contents.**
- **[OPEN] browser driver choice** (browser-use vs Stagehand vs raw Playwright).
- **[OPEN] warm-pool sizing.**
- **[OPEN] CapabilitySet granularity** — start minimal (search/extract/deploy), grow.
- **Next contracts:** Retrieval/grounding (§9 of the BoD) and the Security analyzer detail.
