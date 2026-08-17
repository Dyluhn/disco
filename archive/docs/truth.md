# truth.md — adversarial Opus re-audit of the verified register (2026-06-16)

Two Opus-4.8 auditors independently re-checked all 57 code-verified verdicts in
`docs/backlog-verified-2026-06-16.md` against the live tree, applying the **"wired vs
working"** test: a feature can be REGISTERED + TESTED yet INERT in production (stub
default, gated-OFF that never flips, a flag nothing sets, a code path no caller reaches,
a frontend prop no production caller passes). A DONE that is inert is **not** done.

## ⚠ Headline — the auditors caught what the harness AND I missed

The register said **45 DONE**. The re-audit downgrades **6 of those to PARTIAL/inert** —
all the same "wired-but-inert false-affordance" class as the C20 stub I'd flagged:

| Item | Register | Truth | Why it's inert in production |
|---|---|---|---|
| **C1a/C1b/C1c** (DoD evaluator) | ✅ DONE "highest-leverage win" | 🟡 **PARTIAL — INERT** | The store, fresh-context judge, and finish-gate wiring are all real + 33 tests, **but `set_dod_spec` is never called outside tests** → `get_dod_spec` always returns None → judge never runs → finish gate is permanently the legacy no-op. The capture half (create the spec at `submit_plan`) was never built. |
| **D9** image generation | ✅ DONE | 🟡 **PARTIAL** | Only backend is `_PILProceduralBackend` (geometric patterns). Ask for "a cat" → a procedural pattern. Real `DiffusersBackend` slot "left empty by design"; torch not installed. Registration + binary-safe write are honest; the *generative capability* is a placeholder. |
| **D12** in-block download | ✅ DONE | 🟡 **PARTIAL** | `cid` threading + tests are real, but the SOLE production caller of `AnswerDocument` (`ResearchSurface.tsx:121`) passes **no cid** → the download never renders in production. |
| **E8** podman/local egress | ✅ DONE | 🟡 **PARTIAL** | Filtered→proxied wiring is real, but the method docstring self-admits "**[E8 — live-verify on VM 202 pending]**" — same VM-unverified class as D7. |
| **F6** patch-spiral detector | 🟡 PARTIAL | 🟡 **PARTIAL (worse)** | Not just "action half missing": the whole `rewrite_directive` is **unconsumed** — `StuckDetector` is built without `assist=` (directive always None) and only `is_stuck()` is ever called. Fully inert, same class as C20. |

**The systemic finding:** *"wired but inert"* is a recurring pattern in this codebase —
C20, the C1 DoD chain, D9, D12, F6 are all registered + unit-tested yet activated by **no
production path**. It is the **same root cause** as the silent-drop bug that started this
whole thread: the unit of verification was the component (does this code, with its mocks,
pass?), never the **end-to-end production path** (does a real run actually reach and use
it?). Tests that mock the seam, or exercise a code path no caller reaches, prove the part
in isolation and nothing about the whole.

## Corrected tally (was 45/6/3/2/1)

**✅ 39 DONE · 🟡 12 PARTIAL · 🔲 3 OPEN · ⚪ 2 MOOT · 🚧 1 BLOCKED-external** → genuinely
shipped-and-live ≈ **68%** (down from the register's claimed 79%). The drop is entirely the
6 inert features above.

## ✅ Resolved since this audit (2026-06-16, later same day)

Track A "release honesty" cleared (4 items): **A1**+**A3** (`dcf2cea` — required
typecheck:build gate now green), **A5b** (`fce2379` — shipping `no-explicit-any`→0),
**A6** (`febac78` — verify scripts on `disco_env`; `0613b14` corrected an erroneous
volume revert back to the authorized `disco-data`). Updated tally: **✅ 43 DONE · 🟡 9
PARTIAL · 🔲 2 OPEN · ⚪ 2 MOOT · 🚧 1 BLOCKED**.

Also fixed this session (NOT in the 57-item register — found via the full-suite run):
the cross-package **pytest conftest name-collision** that deterministically failed 6
tests in CI's required `pytest -m "not integration"` invocation (`596a798` — renamed
the colliding pure-helper conftests to `*_fakes.py`; also ordered the DR wall-clock
tiers, quick 600→300). Full suite now **1837 passed / 0 failed**. Plus whole-tree
basedpyright driven to **0** (`a11288e`), `DISCO_INSPECT` request-trace layer
(`4df444e`), nightly live-e2e CI (`65b77fa`), and `CLAUDE.md` (`025796a`, `b492d37`).

**Still open from the 6 highest-value fixes below: C1, C20, F6, D12, D9, + the
standing production-reachability gate. SEC-2 (token half) not started.**

## What held up (auditor-confirmed, real + reachable)
- **Track A/B:** all verdicts confirmed. (A6 nuance: the `disco-data` volume rename is *authorized* per Dylan's 2026-06-13 rebrand reversal, **not** a defect — the only real A6 gap is the 3 `verify_*.py` scripts that read `PMX_LOCAL_*` literally.)
- **Track C (the genuinely-shipped core):** C2 wake-retry, C3 dev-server rehydrate, C5 MEMORY.md, C6 recitation, C7 escape jitter, C9 live-window, C10 turn-based keep_recent, C11 recover_span, C13 exec-prefill (gated), C14 DR isolation, C15 idle-kernel cull (instantiated in prod), C16 pointer-flush, C18 plan-step predicates, C21 small-model prompt — **all confirmed real and reachable.**
- **Track D:** D2 slides viewer, D4 audio mixer (+live-proven), D5 toggle, D6 DatasourceEvent (real producer+consumer), D8 (console-error verify, honestly partial), D10 cassette projection, D11 tasks dashboard (real backend `running_conversation_ids()` + NavRail badge) — confirmed.
- **Track E:** E5 `_safe_reload` bounded thread, E6 ApprovalDiff real new-hash threaded to UI — confirmed.
- **Track F (gated assist tier — confirmed live for the intended local-small-model deployments via `_is_small_assist_default`):** F3 read-before-write, F4 bootstrap, F5 thinking-budget (both halves engine-driven), F7 (pressure-aware via a large-file *size* proxy, honestly documented), F8 mid-turn trunc, F9 read-dedup — confirmed. HS-02 anchored template, HS-03 re-grounding, HS-07 ThinkTool, HS-08 invalid-tool reroute invariant — confirmed end-to-end.

## Per-item audit (CONFIRMED unless noted) — file:line truth

**Track A:** A1 ✅ **DONE** (`dcf2cea` — `ProjectStorageSaveInput` narrowing; typecheck:build green). A2 CONFIRMED DONE. A3 ✅ **DONE** (resolved by A1 — required typecheck gate now green). A4 CONFIRMED DONE (session.py:114/284/527). A5a CONFIRMED DONE (ruff clean). A5b ✅ **DONE** (`fce2379` — 13 shipping `no-explicit-any`→0 via `ChartDatum`/`TokenUsage`; the tightening caught 2 latent bugs the `any` hid). A6 ✅ **DONE** (`febac78` verify scripts→`disco_env` + `disco-sandbox:base` default; volume correctly LEFT as `disco-data` — an erroneous pmx-data revert was corrected in `0613b14` per this doc's "authorized, not a defect" ruling).

**Track B:** B1a/B1b CONFIRMED DONE (env knobs + lite tier reach the loader). B2/B4 CONFIRMED MOOT (no bundled model, compose:131). B3 CONFIRMED DONE (OOM guard caught in 2 real WS paths: streaming.py:437, app.py:1251). B5 CONFIRMED DONE (sweep wired runtime.py:1643).

**Track C:** C1a **DOWNGRADE→PARTIAL** (spec never created in prod). C1b CONFIRMED-component / **INERT end-to-end**. C1c CONFIRMED-wiring / **INERT** (gate always legacy no-op). C9,C10,C6,C16,C18,C2,C3,C5,C7,C11,C13,C14,C15,C21 CONFIRMED DONE (each reachable in prod — file:line in register, re-confirmed). C20 CONFIRMED PARTIAL (stub, no override).

**Track D:** D2 CONFIRMED DONE. D3 CONFIRMED BLOCKED-external. D4 CONFIRMED DONE. D5 CONFIRMED DONE. D6 CONFIRMED DONE (app.py:484 real emit). D7 CONFIRMED OPEN. D8 CONFIRMED PARTIAL. D9 **DOWNGRADE→PARTIAL** (procedural placeholder). D10 CONFIRMED DONE. D11 CONFIRMED DONE. D12 **DOWNGRADE→PARTIAL** (no prod caller threads cid).

**Track E:** E5 CONFIRMED DONE. E6 CONFIRMED DONE. E7 CONFIRMED OPEN (_ScriptedRouter still in test_deep_research.py). E8 **DOWNGRADE→PARTIAL** (live-verify pending).

**Track F/HS:** F3,F4,F5,F7(caveat),F8,F9 CONFIRMED DONE (gated, engine-driven, live for intended models). F6 CONFIRMED PARTIAL (rewrite_directive unconsumed — fully inert). HS-02,HS-03,HS-07,HS-08 CONFIRMED DONE.

## The 6 highest-value fixes the audit surfaces (beyond the register's "what's left")
1. **Make the C1 DoD chain LIVE** — call `set_dod_spec` at `submit_plan`/task-start so the fresh-context evaluator actually gates real finishes. This is the single highest-leverage inert feature (the "false-done / verify-thrash" root fix the whole arch-rebuild was about).
2. **C20** — wire a real read-only subagent in `_run_fanout`, or stop advertising `delegate_explore`.
3. **F6** — pass `assist=` into the `StuckDetector` ctor and consume `.evaluate()`/`rewrite_directive` in the loop, or drop the dead code.
4. **D12** — thread `cid` into `AnswerDocument` from `ResearchSurface`, or remove the in-block download affordance there.
5. **D9** — either install a real diffusion backend or relabel the tool as "procedural placeholder" in the agent-facing description (it's honest in the docstring but the tool is offered).
6. **A standing gate** — an "is this reachable in production?" check (not just "do tests pass?") for any feature claimed DONE. This is the durable fix for the whole inert-feature class.

*Auditors: 2× Opus-4.8, read-only, ~290 tool-uses combined. Tally: 53 CONFIRMED · 4 DOWNGRADE (C1a, D9, D12, E8) + 2 sharpened-inert (C1b/c chain, F6). 0 refuted-to-better.*

---

## SEC-2 (TOKEN HALF ONLY) — authenticate the Jupyter Kernel Gateway

**Scope of THIS task:** add per-sandbox authentication to the kernel gateway so an
unauthenticated caller (external peer, or another session's sandbox) cannot drive a
kernel. **Explicitly OUT of scope here:** the `--ip 0.0.0.0` / `0.0.0.0`-publish
*exposure reduction*. That half is topology-sensitive and deferred — a loopback bind
**breaks the shipped compose**, where the agent-server runs in a bridge-network
container and reaches the sandbox's `8899` via `host.docker.internal` (the host
gateway), NOT loopback (verified: `compose.yaml:78,88` `DISCO_PREVIEW_HOST=host.docker.internal`
→ `runtime.py:273` → `gvisor.py:342` → `_container.py:385`). **Do not change the
bind, the published-port set, or `internal_port_mapping`'s host in this task.**

**Why the token alone closes the cross-session-contamination goal (in this topology):**
the gateway runs *inside each sandbox* (tmux `__kernel`, `kernel.py:330`), so its
`--auth_token` is only observable from within that same sandbox — never by another
session. So a per-sandbox token is a sound cross-session gate on its own; it is NOT a
substitute for not-exposing the port (that's the separate deferred half), and it is not
"permanent" without the regression test below.

### ⚠ Correction to the original SEC-2 note — the token must NOT be `token_urlsafe()` per object
`_ensure_gateway` (`kernel.py:314-318`) **probes for an already-running gateway and
reuses it** (lazy start). After suspend/resume, dev-server re-materialize (C3), or an
agent-server restart, a *fresh* `GatewayKernel` will reconnect to a *surviving* gateway.
A random per-instance token would not match the token the running gateway was launched
with → the object is locked out of its own kernel. **The token must be STABLE and
re-derivable per sandbox instance**, so any `GatewayKernel` for that sandbox computes the
same value.

**Token source (recommended):** a stateless derivation, no new persisted state —
`token = hmac_sha256(key=DISCO_SECRET_KEY, msg=f"kernel-gateway:{sandbox_id}")` →
`urlsafe`/hex, 32+ chars. Properties: stable for the gateway's life (keyed on the sandbox
*instance* id — a new container ⇒ new gateway ⇒ new token, self-consistent), re-derivable
after object/process recreation, and secret from other sandboxes (they have neither the
master key — confirmed by SEC-1 that interiors never get host secrets — nor each other's
id-keyed value). **Fallback when `DISCO_SECRET_KEY` is unset (dev):** a module-level
`secrets.token_bytes(32)` generated once per agent-server process, HMAC'd with the sandbox
id (stable within the process; the cross-restart case self-heals via the 403 handler
below).

### Exact changes — file:line (current tree)
All sites are in `packages/tools/src/disco/tools/sandbox/kernel.py`, class `GatewayKernel`:

1. **Token helper + store it on the instance.** Add a `_gateway_token()` (the derivation
   above) and set `self._token = _gateway_token(self._sandbox.id)` in `__init__` (`:285-292`).
   Needs `import hmac, hashlib, secrets`. Confirm `self._sandbox.id` is the stable
   instance/container id (it is the same object `internal_port_mapping` hangs off).

2. **Launch — pass the token to the server** (`:326-329`): append
   `--KernelGatewayApp.auth_token={self._token}` to the `jupyter kernelgateway` command.
   (Token is visible only in the sandbox-interior process table = same-session; acceptable.)

3. **Every HTTP call gets `headers={"Authorization": f"token {self._token}"}`** — all six:
   - `:316` pre-probe `GET {url}/api` (the "already running?" check)
   - `:345` readiness-poll `GET {url}/api`
   - `:361` `POST {url}/api/kernels` (create)
   - `:510` `POST .../interrupt`
   - `:516` `POST .../restart`
   - `:538` `DELETE .../kernels/{id}` (shutdown)

4. **Both WebSocket connects get the token** — `:367-368` (start) and `:523-525` (restart
   reconnect). Pass `Authorization: token {self._token}` via the `websockets.connect`
   header kwarg — **verify the kwarg name for the pinned `websockets` version** (`extra_headers`
   ≤ v13 vs `additional_headers` ≥ v14; check `pyproject.toml`). Portable fallback if the
   header path is fragile: append `?token={self._token}` to `ws_url` (gateway accepts the
   `token` query param; note it may appear in debug logs).

5. **Handle the probe-mismatch edge case** at the `:316` pre-probe: with auth, an
   unauthenticated/mismatched `GET /api` returns **403**, not 200. Current code treats
   non-200 as "not running" → it would try to relaunch onto a taken port. So: on a **403**
   from the pre-probe (a gateway is running but our token doesn't match — e.g. master key
   rotated, or dev fallback after a process restart), **kill the `__kernel` tmux session and
   relaunch** with the current token (self-heals; costs only in-flight kernel state). A
   **200** = reuse; a **connection error** = not running, launch normally.

### Definition of done (this task)
- All 8 call sites + launch carry the token; pre-probe 403 path handled.
- **No change** to the bind / `0.0.0.0` / published-port set / `internal_port_mapping` host.
- **Regression test (the durability layer):** spawn two sandboxes on the live compose;
  assert (a) an **unauthenticated** `GET /api` → **403**; (b) sandbox-A (open egress) **cannot**
  create/drive sandbox-B's kernel (no token); (c) the legit agent-server path (with the
  derived token) still executes a cell **and** still works **after a `GatewayKernel`
  re-instantiation against the surviving gateway** (the reconnect invariant — the bug the
  random-token approach would have shipped).
- **Live proof on the real compose** (bridge-network agent-server reaching via
  `host.docker.internal`), not just unit mocks — same fix-and-prove bar as SEC-1.
