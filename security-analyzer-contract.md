# Technical Design Contract — Security Analyzer & Confirmation

**Document type:** Detailed Technical Design (Contract Spec)
**Subsystem:** Security Risk Analysis & Confirmation Policy — BoD §17
**Status:** v1.0 — authoritative contract
**Depends on:**
- Event & State contract (v1.2) — `SecurityRisk` (the risk enum, defined there), `ActionEvent` (carries the proposed tool call + `self_assessed_risk`), the event log (the audit trail is a `get_events()` query).
- Agent Loop contract (v1.1) — the `SecurityAnalyzer` and `ConfirmationPolicy` boundaries this document **fulfills** (`assess(action)->SecurityRisk`, `should_confirm(risk)->bool`); the two-phase confirmation gate (loop §5) is the loop machinery this feeds.
- Tool/Sandbox contract (v1.1) — supplies the `ActionEvent`s to score (carrying the tool's `base_risk`), guarantees the **shell tool surfaces its raw command** for scoring, and provides the **hard isolation that holds regardless of any risk score** (defense in depth).
**Consumed by:** the agent loop (calls `assess` then `should_confirm` before executing any action, loop §5), the surfaces (which pick a `ConfirmationPolicy` per BoD §8), and the UI (which renders the confirmation prompt and the audit trail).
**Builds on the OpenHands lessons (re-implemented):** the `SecurityRisk` enum, pluggable `SecurityAnalyzer`, pluggable `ConfirmationPolicy`, and `ConfirmRisky(threshold, confirm_unknown)`. Cited inline as `[OH]`.

---

## 0. What this document is (and is not)

**Is:** the binding contract for **how a proposed action's risk is scored** and **how that score becomes a confirm/allow decision** — the internals behind the `SecurityAnalyzer` and `ConfirmationPolicy` boundaries the loop already gates on. It is the last core *design* contract; after it, the remaining work is build/capability, not architecture. It fulfills the loop's two security boundaries and pins the analyzer strategies (rule-based, LLM-based, ensemble), the confirmation policies, and the audit trail.

**Is not:** the loop's confirmation *machinery* (the loop owns propose→wait→execute, §5 there — this only supplies the *decision*), the sandbox's hard isolation (the tool/sandbox contract owns that — and crucially, isolation holds *independent* of any score here), or the prompt-injection *content defenses* in the browser tool (those live with the browser tool; this contract governs the action-risk gate). Interior choices below the contract line (exact rule lists, the scoring model's prompt, score-cache mechanics) are the builder's.

**Conventions** (identical to prior contracts): illustrative Python 3.12 + Pydantic v2; field names/types/signatures **normative**, bodies illustrative. **[CONTRACT]** = relied-upon guarantee; **[INTERIOR]** = builder's free choice; **[VERIFY]** = confirm at build.

---

## 1. Foundational principles [CONTRACT]

1. **Risk is scored before execution, every action, no exception** (BoD §17 / loop §5). The loop calls `assess()` on the proposed `ActionEvent` *before* the tool runs; nothing executes un-scored. This is the gate, and it sits at the loop level — not bolted onto the UI.
2. **Two independent signals, not one** (BoD §17.2). The agent **self-assesses** its own action's risk (in-prompt, carried on `ActionEvent.self_assessed_risk`), *and* an independent `SecurityAnalyzer` scores it. The decision uses the analyzer's score — the self-assessment is an input and an audit signal, never the sole basis. A model that under-rates its own risky action cannot thereby wave it through.
3. **The analyzer decides risk; the policy decides confirmation.** Clean separation: `SecurityAnalyzer.assess()` returns a *risk level* (what); `ConfirmationPolicy.should_confirm()` returns a *gate decision* (whether to stop for a human). Surfaces swap the policy (Research = never confirm, Agent = confirm risky) without touching the analyzer.
4. **Confirm-on-UNKNOWN is the safe default.** When risk can't be determined, the default policy treats it as requiring confirmation — uncertainty resolves toward caution, not toward silent execution.
5. **Scoring is advisory to the gate, never a substitute for hard isolation.** A mis-scored action that slips the gate still cannot escape the sandbox or read a secret (tool/sandbox contract owns that). The score governs *human-in-the-loop friction*, not containment. These are independent layers; a failure in this one does not breach the other.
6. **Defense in depth via ensemble.** No single analyzer is trusted as complete. A rule-based analyzer (fast, deterministic, catches known-dangerous patterns) and an LLM-based analyzer (catches novel/contextual risk) compose into an ensemble that takes the *most cautious* verdict. The cheap deterministic check is never skipped in favor of the expensive one.
7. **Every risk decision is observable** (BoD §17.6). The score, its rationale, the contributing analyzers, the self-assessment, and the confirmation outcome are recorded as first-class audit events on the log. The security posture is queryable, not opaque.

---

## 2. The risk vocabulary [CONTRACT]

Reuses the event contract's `SecurityRisk` (not redefined here):

```python
# from core.events import SecurityRisk
#   class SecurityRisk(str, Enum): UNKNOWN | LOW | MEDIUM | HIGH
```

**[CONTRACT] ordering semantics:** `LOW < MEDIUM < HIGH` are ordered and comparable. **`UNKNOWN` is NOT comparable** — it is not "below LOW"; it is a distinct "could not determine" state that policies handle explicitly (principle 4). Any comparison logic must treat `UNKNOWN` as a special case, never silently coerce it to a level. A helper makes this safe:

```python
_ORDER = {SecurityRisk.LOW: 1, SecurityRisk.MEDIUM: 2, SecurityRisk.HIGH: 3}

def at_or_above(risk: SecurityRisk, threshold: SecurityRisk) -> bool:
    """[CONTRACT] True iff risk is a known level >= threshold. UNKNOWN returns
    False here (it is handled separately by the policy, never ranked)."""
    if risk == SecurityRisk.UNKNOWN or threshold == SecurityRisk.UNKNOWN:
        return False
    return _ORDER[risk] >= _ORDER[threshold]

def max_risk(a: SecurityRisk, b: SecurityRisk) -> SecurityRisk:
    """[CONTRACT] The MORE cautious of two risks, for ensemble combination.
    HIGH dominates everything. UNKNOWN dominates LOW/MEDIUM (uncertainty is
    treated cautiously) but NOT HIGH (a known-HIGH is the most cautious)."""
    if SecurityRisk.HIGH in (a, b): return SecurityRisk.HIGH
    if SecurityRisk.UNKNOWN in (a, b): return SecurityRisk.UNKNOWN
    if SecurityRisk.MEDIUM in (a, b): return SecurityRisk.MEDIUM
    return SecurityRisk.LOW
```

**[CONTRACT] `max_risk` is the ensemble combiner** (§4): combining verdicts always yields the most cautious, with `HIGH` strictly dominant and `UNKNOWN` treated as more cautious than known-low/medium (you don't relax the gate because one analyzer was unsure).

---

## 3. The SecurityAnalyzer — fulfilling the loop's boundary [CONTRACT]

This is the boundary the loop depends on. It scores a proposed `ActionEvent` before execution.

```python
from typing import Protocol
# from core.events import ActionEvent, SecurityRisk

class RiskAssessment(BaseModel):
    """[CONTRACT] The full result of scoring — richer than the bare SecurityRisk
    the loop boundary returns, for audit (§7) and UI. assess() returns the
    SecurityRisk; assess_detailed() returns this."""
    model_config = ConfigDict(frozen=True)
    risk: SecurityRisk
    rationale: str                          # human-readable why (for audit/UI)
    analyzer: str                           # which analyzer produced this
    # per-analyzer breakdown when this is an ensemble result (§4)
    contributors: list["RiskAssessment"] = Field(default_factory=list)
    # the agent's own self-assessment, carried through for the audit trail
    self_assessed: SecurityRisk = SecurityRisk.UNKNOWN

class SecurityAnalyzer(Protocol):
    """[CONTRACT — fulfills agent-loop contract §3] Scores a proposed action's
    risk BEFORE execution. The loop calls assess(); the richer assess_detailed()
    feeds audit/UI. May refine (override) the action's self_assessed_risk."""
    name: str
    def assess(self, action: ActionEvent) -> SecurityRisk: ...
    def assess_detailed(self, action: ActionEvent) -> RiskAssessment: ...
```

**[CONTRACT] guarantees the loop relies on:**
- `assess()` is **synchronous and total** — it always returns a `SecurityRisk` for any `ActionEvent`, never raises. (An analyzer that errors internally returns `UNKNOWN` with a rationale noting the failure — fail toward caution, not toward crash-or-allow.)
- `assess()` never executes or mutates the action — it only reads and scores. It is a pure inspection.
- The analyzer **may refine the self-assessment**: it reads `action.self_assessed_risk` as one input but its returned score is authoritative for the gate (principle 2).
- An LLM-based analyzer that needs async work runs it behind a sync-safe boundary, OR the loop awaits a detailed variant — but the **contract boundary the loop calls is sync** (matching the loop contract's `assess(action) -> SecurityRisk`). If an analyzer is inherently async, expose an `aassess()` and have the loop await it; the rule-based default is sync and cheap. [VERIFY] which the loop uses at integration; default to the cheap sync rule-based analyzer on the hot path and reserve async LLM scoring for actions the rule-based analyzer flags as non-trivial.

---

## 4. Analyzer strategies [CONTRACT for the boundary; INTERIOR for the rules] (BoD §17.2)

Three analyzers, composable. The interface above is the contract; the specific rules/prompts below are `[INTERIOR]` and tunable — but the *strategy roles* and the ensemble behavior are contractual.

### 4.1 RuleBasedAnalyzer [CONTRACT role; INTERIOR rules]
Fast, deterministic, no model call. Catches known-dangerous patterns by inspecting the action's tool and arguments. **This is the always-on analyzer on the hot path.**
- For the **`shell` tool** specifically (which the tool contract guarantees surfaces its raw command), a **shell-command parser** inspects the command and scores known-dangerous shapes HIGH/MEDIUM (e.g., destructive filesystem operations, privilege changes, network exfiltration patterns, package-install-then-execute). [INTERIOR] the rule list; [CONTRACT] the shell command is available to score and the parser is a `SecurityAnalyzer`.
- For other tools, it factors the tool's static `base_risk` (tool/sandbox §2) and argument shape (e.g., a `file_write` outside the workspace, a `deploy` action, a `browser` action that submits a form vs. reads a page).
- **[CONTRACT]:** the rule-based analyzer is **never skipped**. It is the deterministic floor under every action.

### 4.2 LLMBasedAnalyzer [CONTRACT role; INTERIOR prompt]
Catches novel/contextual risk a rule list misses — judges the action *in context* (the goal, the recent events). Uses a model via the router (a capability profile — note: with the router lobotomized to manual selection, this is whichever model is assigned; cost-legible like everything else). **[CONTRACT]:** it is **advisory and additive** — it can only raise caution via the ensemble (`max_risk`), never *lower* what the rule-based analyzer flagged. An LLM that rates a rm -rf as LOW cannot override the rule-based HIGH. (This protects against the model being talked down by injected content — principle: the model is untrusted, BoD §17.3.)

### 4.3 EnsembleAnalyzer [CONTRACT]
Composes analyzers and returns the **most cautious** verdict via `max_risk` (§2). The default ensemble = RuleBased (always) + optionally LLMBased (for actions the rule-based scores at or above a configurable trigger, to avoid an LLM call on every trivial read). **[CONTRACT]:**
- The combined risk is `max_risk` over all contributors — defense in depth, no analyzer can relax another's caution.
- `assess_detailed()` returns the full `contributors` breakdown for audit.
- If any analyzer errors, it contributes `UNKNOWN` (cautious), never silently drops out.

**[CONTRACT] hot-path discipline:** the rule-based analyzer runs on every action (cheap, sync). The LLM analyzer runs only when the rule-based result and config say it's warranted — so trivial read-only actions aren't taxed with a model call, and the gate stays responsive. This is the performance contract for the security layer.

---

## 5. The ConfirmationPolicy — fulfilling the loop's boundary [CONTRACT]

The second boundary the loop depends on. It turns a risk level into a gate decision. The analyzer says *how risky*; the policy says *whether to stop for a human*.

```python
class ConfirmationPolicy(Protocol):
    """[CONTRACT — fulfills agent-loop contract §3] Given a risk level, decide
    whether the action requires human confirmation before executing. Pure,
    sync, total — never raises, never executes."""
    name: str
    def should_confirm(self, risk: SecurityRisk) -> bool: ...
```

### 5.1 The policies [CONTRACT] `[OH: AlwaysConfirm / NeverConfirm / ConfirmRisky]`

```python
class NeverConfirm:
    """[CONTRACT] Never gates. Used by the Research surface, where the tool
    scope is read-only by construction so risk is LOW by design (BoD §8.1)."""
    name = "never_confirm"
    def should_confirm(self, risk: SecurityRisk) -> bool:
        return False

class AlwaysConfirm:
    """[CONTRACT] Gates every action. Maximum oversight (e.g., a 'careful mode')."""
    name = "always_confirm"
    def should_confirm(self, risk: SecurityRisk) -> bool:
        return True

class ConfirmRisky:
    """[CONTRACT] Gates when risk is at/above a threshold, OR when risk is
    UNKNOWN and confirm_unknown is set. The Agent-surface default. [OH]"""
    name = "confirm_risky"
    def __init__(self, threshold: SecurityRisk = SecurityRisk.HIGH,
                 confirm_unknown: bool = True):
        self.threshold = threshold
        self.confirm_unknown = confirm_unknown
    def should_confirm(self, risk: SecurityRisk) -> bool:
        if risk == SecurityRisk.UNKNOWN:
            return self.confirm_unknown          # confirm-on-UNKNOWN default (principle 4)
        return at_or_above(risk, self.threshold) # uses the safe comparator (§2)
```

**[CONTRACT] guarantees:**
- `should_confirm()` is pure/sync/total — the loop calls it right after `assess()` (loop §5 step i) and stops to confirm iff it returns True.
- **`ConfirmRisky` confirms on UNKNOWN by default** (principle 4) — uncertainty gates rather than passes. Set `confirm_unknown=False` only deliberately.
- The threshold is the tuning knob: `HIGH` (gate only the most dangerous) vs `MEDIUM` (gate moderately risky too). This is BoD §24-D6 — tune empirically once real tasks run; the *contract* fixes the mechanism, not the threshold value.

### 5.2 Per-surface defaults [CONTRACT] (BoD §8)
The surface picks the policy at loop construction (loop §5 / §8):
- **Research surface → `NeverConfirm`.** Read-only tool scope means actions are LOW by construction; gating would be pointless friction (BoD §8.1).
- **Agent surface → `ConfirmRisky(threshold=HIGH, confirm_unknown=True)`.** State-changing tools mean risky actions get a human gate, and uncertainty gates too (BoD §8.2).
- A user-facing **"careful mode"** can swap the Agent surface to `AlwaysConfirm` or `ConfirmRisky(threshold=MEDIUM)` — a settings/runtime choice, not a code change.

**[CONTRACT]:** the policy is the *only* thing that differs between surfaces on the security axis — same analyzer, same loop machinery, different gate. This is "one core, many surfaces" (BoD §3.1) applied to security.

---

## 6. How it composes with the loop (the gate, end to end) [CONTRACT]

This restates the loop's §5 from the security side, to make the seam unambiguous. Per action, before execution (loop §5 steps h–i):

1. The agent proposes an action; the loop builds the `ActionEvent` carrying `self_assessed_risk`.
2. The loop calls `analyzer.assess(action)` → a `SecurityRisk` (the ensemble's most-cautious verdict, §4).
3. The loop calls `policy.should_confirm(risk)`.
4. **If True:** the loop emits the proposed `ActionEvent` + `StatusEvent(WAITING_FOR_CONFIRMATION)` (→ `pending_action_id`) and **stops** — no execution. The UI renders the proposed action + the `RiskAssessment.rationale` so the human decides with context. On `confirm`, the loop executes exactly that pending action; on `reject`, it records denial and resumes (loop §5).
5. **If False:** the action executes normally.

**[CONTRACT] the division of labor:** this contract supplies steps 2–3 (the *decision*); the loop owns steps 1, 4, 5 (the *machinery*); the sandbox (tool contract) owns containment *regardless* of the decision. Three independent layers.

---

## 7. Audit trail [CONTRACT] (BoD §17.6)

The security posture is observable, not opaque (principle 7). Security-relevant facts are recorded on the event log (event contract §7.1), queryable as a view.

**[CONTRACT] what is recorded for every gated/scored action:**
- The `RiskAssessment` (final risk, rationale, contributing analyzers' verdicts, the agent's `self_assessed` risk) — attached to or emitted alongside the `ActionEvent`. [INTERIOR] whether this rides in `ActionEvent.meta`, a dedicated audit event, or both — but it MUST be reconstructable from the log.
- The **confirmation outcome** (confirmed / rejected / not-gated) — derivable from the `StatusEvent(WAITING_FOR_CONFIRMATION)` + the subsequent `confirm`/`reject` (loop §5 / event wire §7.3).
- For **`reject`**, the denial is itself an event the agent sees next View (loop §5) — so a refused action is in the permanent record and influences subsequent steps.

**[CONTRACT]:** the audit trail is a `get_events()` query (event §6) filtered to security-relevant events — there is no separate audit store. Anything the security layer decides is in the one append-only log, with the rest of the conversation, replayable.

---

## 8. Relationship to the other security layers [CONTRACT boundary]

This contract is **one of three independent security layers** (BoD §17); it does not subsume the others, and it must not be relied on to do their job:

- **This layer (action-risk gate):** scores proposed actions, gates risky ones for human confirmation. Governs *HIL friction*. Owned here.
- **Hard isolation (sandbox):** the microVM containment, secrets-never-inside, deny-by-default egress (tool/sandbox §5–§7). Holds **regardless of any score here** — a mis-scored HIGH action that slips the gate still can't escape or read a secret. Owned by the tool/sandbox contract.
- **Prompt-injection content defense (browser tool):** the dual-LLM read/act split, treating page content as untrusted data not commands (BoD §17.3). Owned with the browser tool. *Note the interaction:* §4.2 deliberately forbids the LLM analyzer from *lowering* a rule-based verdict precisely so that injected content can't talk the risk gate down — the two layers reinforce each other, but neither is the other's substitute.

**[CONTRACT]:** these layers are independent and defense-in-depth. A breach or miss in any one is contained by the others; none is load-bearing alone (principle 5). The kill switch (BoD §13.6) sits above all three — network-level capability/token revocation — and is the ultimate stop.

---

## 9. Test plan [CONTRACT — defines correctness]

Headless; no real model, no sandbox. Analyzers and policies are pure functions over `ActionEvent`s, so they test cleanly in `core`. The subsystem is done when these pass.

### 9.1 Risk vocabulary
- `at_or_above` orders LOW<MEDIUM<HIGH correctly and returns False for any UNKNOWN comparison (never ranks UNKNOWN).
- `max_risk`: HIGH dominates all; UNKNOWN dominates LOW/MEDIUM but not HIGH; LOW only when both low. Table-driven over all pairs.

### 9.2 RuleBasedAnalyzer
- Known-dangerous shell commands score HIGH/MEDIUM (table-driven over a representative dangerous-command set surfaced as the raw command); benign reads score LOW.
- A `file_write` outside the workspace, a `deploy`, a form-submitting `browser` action score above a read-only `search`/`file_read`.
- Errors internally → returns UNKNOWN with a rationale, never raises.
- Always runs (assert it's invoked for every action in an ensemble, even when an LLM analyzer is present).

### 9.3 LLMBasedAnalyzer (faked model)
- With a fake model, a contextually-risky action it rates HIGH raises the ensemble verdict.
- **Cannot lower a rule-based verdict:** a fake LLM rating LOW on an action the rule-based scored HIGH yields HIGH (ensemble = `max_risk`). This is the injection-resistance test.
- Runs only when warranted (assert no LLM call on a trivial read when config gates it behind a rule-based trigger).

### 9.4 ConfirmationPolicy
- `NeverConfirm` → False for every risk including HIGH.
- `AlwaysConfirm` → True for every risk including LOW.
- `ConfirmRisky(HIGH, confirm_unknown=True)` → True for HIGH and UNKNOWN, False for LOW/MEDIUM.
- `ConfirmRisky(MEDIUM)` → True for MEDIUM and HIGH.
- `confirm_unknown=False` → UNKNOWN does not gate (deliberate opt-out) — assert it must be explicitly set.

### 9.5 Two-signal independence
- The agent's `self_assessed_risk` does **not** by itself clear an action: an action self-assessed LOW but scored HIGH by the analyzer gates under `ConfirmRisky`. (The model cannot wave itself through — principle 2.)

### 9.6 Cross-contract integration (acceptance gate)
- With the real loop (loop §5) and fakes elsewhere: an Agent-surface loop with `ConfirmRisky` + the rule-based analyzer, given a HIGH-risk action, emits the proposed `ActionEvent` + `WAITING_FOR_CONFIRMATION` and does **not** execute; `confirm` executes exactly that action; `reject` records denial and resumes. A LOW action executes without gating. A Research-surface loop with `NeverConfirm` never gates. Asserts this contract's decision drives the loop's gate, and that the `RiskAssessment` is reconstructable from the log (audit, §7).

---

## 10. What this contract hands to / expects from each subsystem

- **Agent loop (loop contract):** this **fulfills** both security boundaries — `SecurityAnalyzer.assess(action)->SecurityRisk` and `ConfirmationPolicy.should_confirm(risk)->bool`. The loop calls them in §5 steps h–i and owns the propose→wait→execute machinery. With this, the loop's last two unfulfilled boundaries are filled (the `ToolExecutor` was filled by the tool/sandbox contract; these two by this one) — **the core is now fully closed at the contract level.**
- **Tool/Sandbox (tool contract):** supplies the `ActionEvent`s (with `base_risk`), guarantees the shell tool surfaces its raw command for the rule-based analyzer, and provides the hard isolation that holds independent of any score here.
- **Event/State (event contract):** `SecurityRisk` comes from there; the `RiskAssessment` and confirmation outcomes are recorded on the log; the audit trail is a `get_events()` query.
- **LLM Router (router contract):** the optional `LLMBasedAnalyzer` calls a model through the router (manual model selection, post-lobotomy) — additive caution only.
- **UI (BoD §13):** renders the confirmation prompt (the proposed action + `RiskAssessment.rationale`) so the human decides with context, and can surface the audit trail. The kill switch (BoD §13.6) sits above this layer.
- **Surfaces (BoD §8):** pick the `ConfirmationPolicy` (Research = `NeverConfirm`, Agent = `ConfirmRisky`) at loop construction; the analyzer is shared.

## 11. Build order (this subsystem) & sequencing

This is a **Phase-3-adjacent** subsystem — it hardens the Agent surface — and it is the **last core design contract**. It does not block the Phase-1 remainder or retrieval live-wiring (those don't gate on it); it should be built before/with the Agent surface (Phase 3), where `ConfirmRisky` actually bites.

1. The risk vocabulary helpers (`at_or_above`, `max_risk`) + `RiskAssessment` (§2, §3); tests 9.1.
2. `RuleBasedAnalyzer` with the shell-command parser + tool/argument rules (§4.1); tests 9.2. (This alone makes the Agent surface meaningfully safe.)
3. The `ConfirmationPolicy` implementations (§5) + per-surface defaults; tests 9.4/9.5.
4. `EnsembleAnalyzer` + the (faked-for-now) `LLMBasedAnalyzer` with the no-lowering guarantee (§4.2/§4.3); tests 9.3.
5. The audit recording (§7).
6. The §9.6 cross-contract integration test — the acceptance gate.

**Then** the Agent surface (Phase 3) can run with a real risk gate: risky actions stop for confirmation, the human decides with rationale in view, and every decision is on the audited log.

---

### Appendix — open items specific to this contract
- **[VERIFY] sync vs async analyzer on the hot path (§3).** The loop boundary is sync; the rule-based default is sync and cheap. If the LLM analyzer is used inline, decide at integration whether the loop awaits an async variant or the LLM scoring is deferred to flagged actions only. Default: rule-based sync on the hot path.
- **[OPEN §24-D6] confirmation threshold.** `HIGH` vs `MEDIUM` for the Agent surface default — tune empirically once real agent tasks run.
- **[INTERIOR] the rule lists and the LLM analyzer's prompt** — tunable; start conservative (over-flag rather than under-flag) and relax with experience.
- **[INTERIOR] score caching** — identical actions may be scored once and cached; must not change the verdict.
- **Relationship to the kill switch (BoD §13.6):** the kill switch is network-level revocation above all three security layers; it is owned with the agent-server/UI, not here, but this contract's audit trail records its activation as an event.
- **The core is now fully specified.** With this contract, every boundary the agent loop named is fulfilled and every core subsystem has a contract. Remaining work is build/capability (live adapters, real sandbox backend, the Agent surface, retrieval live-wiring), not architecture.
