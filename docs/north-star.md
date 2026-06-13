# Disco — North Star: a fresh system can install it and actually use it

**Compiled 2026-06-13 from the Fable-led shakedown** (live driving of the real app),
the **fresh-box install gauntlet** (a clean Debian VM, keyless, zero homelab deps), and
the **RAM teardown**. This is the guiding document. When a decision is ambiguous, it
serves the one goal below.

> **The goal:** a stranger downloads Disco, runs one command on a normal machine, and
> *uses every surface* — keyless — without troubleshooting. "Done" means a human drove
> the real assembled app and watched it work. Green mocks never earn that word.

---

## 1. The lesson that reframes everything (read first)

Every bug that reached the user this iteration lived at the **provider/dependency seam**
— where bundled-vs-optional dependencies (search, extraction, the sandbox socket, the
secret store, the model endpoints, the encoders) plug into the pipeline. **Our tests
mocked exactly that seam away.** A unit test *assumes* the seam, so it can never test it.
The only test that exercises a contract is one where **both real sides show up.**

Consequences, now standing policy:

1. **The gauntlet is the gate.** A real fresh-box install (`docker compose up` on a
   machine that has never seen Disco) + driving every surface through the real
   UI/API is the definition of "release-ready." It becomes `make gauntlet`. It found
   in 20 minutes what months of green suites missed.
2. **"Verified" requires the real app.** No feature is done on the strength of mocks.
   UI claims end with a screenshot of the running app; backend claims end with a live
   run. The phrase "verified end-to-end" is banned unless a human (or the gauntlet)
   actually went end to end.
3. **Show the work (heartbeat principle).** A system that surfaces what it's doing
   turns seam failures into visible, seconds-to-diagnose events instead of silent
   hangs. Apply it everywhere a wait can look like a death.
4. **Whitelist, don't blacklist, at every render/throughput boundary.** Internal event
   names leaked into the UI because the renderer printed anything it didn't recognize.
   Surface only what's explicitly mapped; drop the rest.
5. **Honest framing of demo-grade things.** A bundled demo model that fumbles, an
   offline fixture mode, a no-isolation backend — each must say what it is, loudly.

---

## 2. Fable's release-readiness scan — the standing plan

### The anchor question: should the test scaffold ship?
- **Test files (`*.test.tsx`) don't ship** — nothing imports them; verified absent from
  `dist`. No action.
- **Fixtures DO ship, unconditionally** — the live-vs-fixture switch reads
  `window.__PMX_ENV` at *runtime*, so esbuild can't tree-shake the fixture branch.
- **Decision:** keep fixtures (offline demo is a real feature), but (a) **make demo mode
  loud** — a persistent "Demo data — no backend" badge, since a failed `/env.js` today
  silently shows canned data; and (b) **fix the typecheck honestly** with a
  `tsconfig.build.json` that excludes tests but KEEPS fixtures (they ship, so they must
  typecheck), restoring a real typechecked image build. Do not strip fixtures.
- The Dockerfile comment claiming the type errors are "test-only" is **false** — 28 of 39
  `tsc -b` errors are in shipping files.

### Prioritized fix list (by leverage)
| # | Item | Effort | Status |
|---|---|---|---|
| 1 | Auto-preview TOCTOU race → de-flake `make test` | S | **open** (#25 area) |
| 2 | pdf/docx export shadow (downloaded markdown) | S | ✅ done `ca792a7` |
| 3 | Scrub homelab LAN IPs from the shipped bundle | S | ✅ done `ca792a7` |
| 4 | Drive shipping-file `tsc` errors to 0 → `tsconfig.build.json` + demo badge | M | **open** |
| 5 | Minimal honest CI (`make test` + build/vitest; tsc/eslint as labeled non-required) | M | **open** |
| 6 | Ruff/eslint debt — fix the real ones (F821, B904), ignore cosmetic test E501 | S–M | **open** |
| 7 | `PMX_` → `DISCO_` rename before public release (compat fallback for the secret key; keep `pmx-data` volume) | M | **open** |

### Other release-blockers the scan surfaced
- `make test` is **flaky-red** (the preview race) — a CI hazard either way.
- `eslint` is also red (67) → `npm run lint` is a second dishonest-green trap.
- ~1,795 leaked `/tmp/pmx-sbx-*` workspace dirs (process-sandbox `destroy` cleans tmux
  but not workspaces).

---

## 3. The shakedown — live bugs found by *using* the app (all fixed)

Five surfaced just by trying two surfaces. Each fixed AND verified on the running app:

| Bug | Root cause | Commit |
|---|---|---|
| Build dead (`Docker unreachable`) | Sandbox pointed at a nonexistent docker socket, not the rootless podman one | fixed live; **defects logged #25** |
| Searches never marked complete | Concurrent gather; observations stamped with "last action" → wrong search | `67c1b59` (6/6 correlated, was 1/6) |
| Charts render as code blocks | DR reports are raw markdown; renderer never lifted ` ```chart ` fences | `ca792a7` |
| Model pill silently ignored | Pill honored post-approval but **not at decompose** (bare router) | `67c1b59` |
| Approve/Confirm silently dropped after a restart | `_loops.get()` no-opped instead of lazy-composing | `67c1b59` |
| `section_done` leaking as a raw row | Trace printed any unmapped action verbatim (blacklist) | `b55941f` (now whitelist) |
| A working DR *looks* dead for 30–60s | No "now" indicator during local-model synthesis | `827ba20` heartbeat |

**#25 (open):** sandbox Settings changes don't hot-apply (cached service keeps the old
socket until restart), AND a failing docker client **blocks the asyncio event loop**
(the server went fully unresponsive, needed SIGKILL). Wrap sandbox client calls in an
executor / circuit-breaker; hot-rebuild the sandbox service on config change or say a
restart is required.

---

## 4. The gauntlet verdict — fresh Debian VM, keyless, zero homelab deps

### What genuinely works out of the box (the spine, proven on a foreign box)
Install + build of all 4 services · model-catalogue seeding from `PMX_DRIVER_*` ·
**auto-generated `PMX_SECRET_KEY`** so key entry works first-try (`can_store:true`) ·
the OpenRouter key save/use/delete flow · **the build sandbox through the mounted docker
socket** (`file_list` clean, sibling container spawned, seeded `local` backend correct).

### What's broken at the seam
1. **🔴 Keyless grounded answer is impossible on 8 GB — and dies *silently*.** Encoders
   (~4 GB) load on top of the LLM (6.6 GB @ 32K ctx) → kernel OOM-kills the stack; the
   WS closes after one frame and the query *vanishes*, no error frame. Even
   `disco verify` exits 137. **A 16 GB box likely succeeds; 8 GB hits a wall the README
   never warned about.** → see §5; tracked as **#26**.
2. **🟠 Model cache mounted at the wrong path** — `pmx-models:/root/.cache/llama.cpp`
   but `-hf` writes to `/root/.cache/huggingface`, so the 2.4 GB GGUF lived in the
   container layer and re-downloaded on every recreate. **✅ fixed** (mount `/root/.cache`).
3. **🟠 No frontend healthcheck** (only service without one). **✅ fixed**.
4. **🟠 RAM need buried** in `self-host.md`, not the README. (Doc edit held — see §5;
   the real fix is to make it *fit*, not to document a 16 GB floor.)
5. **Honest caveat:** the bundled 4B hallucinated a fictional task on turn 2 of a
   trivial build request. The demo model is the *first thing a stranger sees* and it
   fumbles — see §6.

### Measured footprint
Images ≈ 5.3 GB (sandbox:base 3.14 GB is the bulk) · runtime RAM steady ≈ LLM 3.25 GiB
@8K / 6.6 GiB @32K, **+~4.0 GB transient** on the first grounded answer (encoders) ·
first-run keyless downloads: 2.4 GB GGUF + ~1 GB encoders.

### Maintainer changes the gauntlet demanded (status)
- [x] `llm` volume → `/root/.cache` (model survives recreate)
- [x] frontend healthcheck
- [ ] **Make the keyless path fit** (the §5 RAM work — supersedes "document 16 GB")
- [ ] Encoder OOM guard: stream an honest error frame instead of dying mid-WS
- [ ] `.env.example` ctx lever note (seed-only caveat) — partially drafted, held
- [ ] Re-frame the bundled 4B as a smoke test, not a capable default

---

## 5. The RAM teardown — make the keyless tier *fit* (the real fix)

The default keyless run needs **~10.8 GB peak** and OOMs 8 GB. It doesn't have to.

| Consumer | What it is today | RAM |
|---|---|---|
| LLM | Qwen3-4B Q4 @ **32K ctx** | ~6.6 GB (≈2.9 GB @ 8K — the delta is pure KV) |
| Embedder | `intfloat/multilingual-e5-large` (560M, **production-sized**) | part of ~4 GB |
| Reranker | `jinaai/jina-reranker-v2-base-multilingual` (~278M) | part of ~4 GB |
| NLI | *reuses the reranker* — free | 0 |
| servers | app + agent | ~0.2 GB |

The demo ships **large, multilingual, production-grade** encoders and a 32K context a 4B
will never use. Levers, ranked by GB-saved ÷ quality-cost ÷ effort:

1. **Lite encoders for the bundled tier** — small ONNX siblings (`multilingual-e5-small`
   /`bge-small` + `jina-reranker-v1-tiny`/`ms-marco-MiniLM-L-6`). **~4 GB → ~0.8 GB.**
   Negligible quality cost for a *demo*. Make `PMX_EMBED_MODEL`/`PMX_RERANK_MODEL` real
   env knobs (they're hardcoded constants today).
2. **Default ctx 32K → 8K.** **−3.7 GB**, zero quality cost for a 4B demo, one number.
3. **Quantize the KV cache** (`-ctk q8_0 -ctv q8_0`; the R9700 recipe uses q4_0).
   Negligible quality loss, one flag.

**Target: ~10.8 GB → ~4 GB peak** — comfortable on 8 GB, viable on 6 GB.

**The design = the 3-tier provider model you already built, applied to encoders:**
`lite` (small ONNX, the *bundled default*) → `standard` (today's e5-large/jina-base) →
`remote` (TEI sidecars — the mini-PC). One env line scales up. The bundled path becomes
"fits a laptop"; the *real* product stays "BYO 24–32B + big encoders."

**Backstop (#26):** even with lite defaults, guard the lazy encoder load against
available memory and stream an honest error frame — never let the agent-server get
OOM-killed mid-WS with the user seeing nothing.

---

## 6. The bundled model question — what is the small Qwen *for*?

Its one job: make `docker compose up` produce a **working, keyless** instance — prove the
loop lights up end to end with no account. It is **not** the product (the wedge is BYO
24–32B / OpenRouter). The gauntlet proved it's not usable for real work (hallucinated a
task), and it's the 6.6 GB that breaks the keyless promise on 8 GB.

The tension: **the model that exists to guarantee the keyless promise is the one that
breaks it (RAM) and undersells it (fumbles).**

Standing position: the 4B earns its slot **only if** the keyless tier (a) *fits* (§5) and
(b) is **honestly framed as a smoke test** ("this confirms the install works — assign
your real model in Settings"), and (c) the model itself is **current-gen and
tool-call-strong** (tool-calling is the one capability the loop can't survive without;
4B is roughly the floor where it stays reliable). The bundled `Qwen3-4B-Instruct-2507` is
~11 months old (the workstation runs Qwen3.6-27B) — a previous-gen model as the literal
first impression. **Replace it with a current small GGUF, but only after confirming it
pulls keyless and passes `disco verify` (config → completion → tool-calling).** Never
bless a bundled model on "it downloads," the exact mistake that put the old one there.

---

## 7. Open decisions (these set everything downstream)

1. **Encoder tier:** lite-by-default (→ ~4 GB, knobs to scale up) **vs** keep
   production-grade retrieval bundled and point the RAM-short at BYO endpoints. *(Sets
   the RAM budget.)*
2. **Bundled model's role:** remain the zero-config default (right-sized + smoke-framed)
   **vs** make BYO-endpoint the headline path with the 4B as an explicit fallback. *(Sets
   what the first 15 minutes look like.)*
3. **Which current small GGUF** fills the slot — to be chosen and *verified*, not asserted.

---

## 8. Definition of done (for "a stranger can install and use it")

The gauntlet, green, on a clean **8 GB** box, keyless:
- `docker compose up -d --build` → all services healthy, model cached once (survives
  recreate).
- A grounded answer returns **with citations** — no OOM, no silent vanish.
- Deep Research runs through plan-approval to a **charted** report with a live heartbeat
  and correctly-marked steps.
- A Build/Agent task spins a real sandbox and lists/edits files.
- An OpenRouter key can be entered, used, and cleared.
- `disco verify` passes.
- Nothing requires the operator to read a log or edit a config to recover.

When that run is green and screenshotted, Disco is installable and usable on a fresh
system. Until then, it isn't — regardless of what the unit suites say.

---

*Commits this iteration: `a0c2431` compose deploy · `cb7645c` SECURITY · `4cd0ea4`
pmx/disco verify · `7d84e5d` Apache-2.0 + no-relicense promise · `5ba15df` rebrand →
Disco · `52e62fd` mobile · `c0e16a5` messaging bridge · `67c1b59` DR correlation/pill/
approve · `ca792a7` charts/export/LAN-IPs · `827ba20` heartbeat · `b55941f` trace leak ·
(uncommitted) compose model-cache + frontend healthcheck. Open tasks: #16, #21, #25, #26,
plus the scan list §2.*
