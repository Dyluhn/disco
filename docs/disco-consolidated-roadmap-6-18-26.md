# Disco — consolidated roadmap (reconciled 2026-06-18)

THE single living "what's left" doc. **Reconciled against git + code on 2026-06-18**
(not against older docs or memory) — every status below cites evidence (commit SHA,
file, or "verified this session"). Supersedes all archived per-doc lists. When a status
is uncertain it says so; do not upgrade a `◐`/`verify-live` to `✅` without re-checking.

Legend: ✅ done (evidence) · ◐ partial (gap named) · ○ open · ⬚ blocked/decision-gated ·
🔎 shipped-in-code but needs a live in-app check before trusting.

---

## §0 — Shipped + verified (do not re-litigate)
Each with commit evidence; UI items still owe a live Firefox screenshot (see §4-note).

**Driver / provider reliability — ✅ DONE** (`14fe27b` P1+P2+P3, `e875249` P4). Provider-
pref injection, routing-retry, sticky last-picked model, `config=` footgun warning.
Live-proven on the free model that previously Chutes-errored.

**Slides / artifacts pipeline — ✅ DONE**
- C4 schema experiment → verdict "loose-hybrid" (`034045a`; `slides-experiment-verdict.md`). **Decision resolved.**
- C1/C2 deck schema + generator (`1a93bd3`) · C3 native **editable** PPTX via python-pptx + LibreOffice PDF + 16:9 brand HTML (`cbb9101`) · C8 chart/table layouts (`4662978`) · reconcile onto c1c2 (`b200384`).
- C5 render/preview fixes (`5ca117a`) · C6 `artifact_mode` wired into the build loop (`74019e7`; verified real refs in runtime.py/conversations.py).
- Export formats: PPTX (native-editable, clean), PDF (from PPTX), HTML. PPTX imports cleanly into Google Slides/PowerPoint — that's the editing-elsewhere path.

**Editor selection plumbing (§4.1/4.2) — ✅ DONE** (`2d18c9d`): schema-independent
SelectionOverlay + postMessage bridge + in-frame selection agent, mounted in
AgentCanvas/PreviewPane. (The *edit loop* on top of this is OPEN — see §1.1.)

**Deep-Research polish — ✅ DONE**: F1 keep-searching (`fcb117c`) · DR mid-run
steer/inject (`fa9e3d9`) · FILE-DELIVERY F1/F2 per-file download + feed card
(`c804f55`) · F3 cid threading into report renderers (`d12fe4f`, 🔎 verify the download
actually renders on the Research surface live — truth.md D12 was the gap F3 targets).

**Attach files in the initial box (G1/DR-4) — ✅ DONE** (`99c2058`, merged `235dfa3`).

**noVNC live browser P1–P4 — ✅ DONE** (`f6d2b8f` image, `ac4edef` daemon+route+toggle).
P5 = open (see §1.7).

**Audio (RP-09) — ✅ DONE** (`f65837b` + **Dylan live-tested days ago**).

**Tailscale runthru B1–B7 + PDF export polish — ✅ DONE** (live-verified earlier today).

**This session (2026-06-18):** merged attach2 · fixed node_modules self-symlink +
novnc daemon type error (`62625d2`) · restored deck_patch (`e3cd1d4`) + deckResolver
(`d94ce55`) test suites · pruned 21 stale worktrees · archived 20 superseded plan docs.

---

## §1 — Genuinely open (verified) — the real remaining work

**1.1 Website / app click-to-edit  ○  — THE big one.** *(plan: `surgical-C-trackc-editor.md`)*
Verified this session: the click-to-highlight is mounted, but the **edit loop is open at
the last hop**, and the *site* path is missing its foundation:
  - Nothing stamps the `data-oid` source tags onto built sites → clicking a built-site
    element yields an empty `SourceRef` (no file/line). The **tag pass (§4.3) doesn't exist.**
  - The selection never reaches the agent (`useElementSelect` selection feeds only the
    overlay highlight) → the **source-edit path (§4.4) is unwired.**
  - The §4.5 `DeckEditor` canvas is mounted nowhere; no `LoweredDeck` backend route.
  Slides flavor ≈ one wire from working; **website flavor is a real project** (framework-
  aware build-time tagger + the edit wire + live acceptance). Deck slice plan:
  `deck-editor-integration-plan-6-18-26.md`. Effort: large.

**1.2 Image generation as a real tool  ○.** Verified: only `_PILProceduralBackend`
exists; `DiffusersBackend` slot empty. The C7 *seam* is done (`313d357`), real generation
is not. Need a real backend (`openai-images-compatible` and/or `comfyui` adapter, per the
§3 decision). Same seam already wired. Effort: high.

**1.3 Iterative research mode toggle  ○.** No implementation commits (verified). Use
per-claim NLI verdicts to re-research only weak-sourced claims; rewrite weak
sections/intro/conclusion, preserve strong ones. **Open design problem (needs Dylan):**
coherence when only some sections are rewritten. Effort: high.

**1.4 DR closing-card → agent handoff  ○.** No commits (verified). On report finish,
offer agent actions ("Make slides") that seed a Build conversation with the report +
a pre-approved plan, skip the gate, deliver. Now unblocked (FILE-DELIVERY shipped). Effort: medium.

**1.5 Real two-host podcast  ○.** No commits (verified). Agent-authored dialogue →
multi-voice TTS → mix → optional video → deliver. **Unblocked now** (FILE-DELIVERY F1-3
+ RP-09 both done). Effort: high.

**1.6 FILE-DELIVERY F4 — on-demand export from Deep Research  ○.** Deferred: needs a
DR on-demand-export model, not a workspace DeliverableEvent. Effort: medium.

**1.7 noVNC P5 — live jail / security acceptance  ⬚ hardware-blocked.** The sandbox VM
was destroyed; re-provision to run it. Code (P1–P4) is in. Prereq: D7 gVisor egress allowlist.

---

## §2 — Launch gates (from `north-star.md`)
- **Benchmarks / eval suite  ○** (Dylan mandate "before launch"). Rides the shipped
  `disco verify` surface. Grounding accuracy, agent task success, latency, cost. Med-large.
- **8 GB keyless + bundled model  ◐.** Encoder knobs + ctx 8K shipped as units (#41/#42);
  **open:** replace stale Qwen3-4B with a current tool-call-strong small GGUF (framed as a
  smoke test) + clean-box <8 GB gauntlet pass. Large.
- **License — AGPL vs Apache  ⬚** (Dylan's call). Trivial once decided.
- **Release-eng pack  ◐.** Done: `self-host.md`, `provider-matrix.md`, `disco verify`.
  Open: minimal honest CI, demo gallery, `git tag v0.1.0` + changelog, mobile-responsive
  pass, `SECURITY.md` (disclosure + threat model). Medium.

---

## §3 — Decisions still needed from Dylan
1. **Iterative-mode coherence approach** (§1.3) — the unsolved design bit.
2. **License** — AGPL vs Apache (§2).
3. **Benchmarks scope** — which metrics/datasets are the launch gate (§2).
4. **Image providers v1 set** — confirm `openai-images-compatible | comfyui` (+ keyless
   procedural default) as the real-backend targets (§1.2). The seam already assumes this.
- *Resolved:* slides A/B/C → loose-hybrid (C4). · default driver → sticky last-pick (shipped).

---

## §4 — Engine / correctness / debt backlog (appendix, not launch-blocking, UNVERIFIED)
Not re-verified this pass — treat as candidates, confirm against code before acting:
single stable system prompt (KV-prefix stability) · auto-spill large observations ·
edit-tool hardening residue (demote `file_replace_lines`/`insert_lines`) · deploy_preview
detached serve ceiling · one-feature-per-iteration scope · epochal masking vs KV (design-
blocked) · grammar-constrained tool calls (infra-blocked, llama.cpp `--jinja`) · E7
real-sample DR-lifecycle harness · lint debt (tsc→0, ruff F821/B904, eslint→0) · UX micro-
polish (auto-scroll, footer LiveSignalBar, session-stash) · root hygiene (move
`run_manual*`/root `test_*`/CSVs/`disco.db` out of root) · a few transitive import nits.

**Also re-verify (truth.md "wired-but-inert" flags, 2026-06-16 — may now be addressed):**
DoD evaluator `set_dod_spec` never called outside tests (C1a) · the FILE-DELIVERY cid
render (D12, §0) · E8 egress live-verify.

---

## §5 — Recommended sequence
1. **Decide** §3 items (cheap, unblock work): license, image-providers set, benchmarks scope.
2. **Highest value:** §1.1 website click-to-edit — scope the tagger + edit-wire, build, live-accept.
3. **Unblock products:** §1.2 image-gen real backend · §1.4 DR→agent handoff · §1.5 podcast.
4. **Launch gates in parallel:** benchmarks · 8 GB bundled-model swap · release-eng residue.
5. **Opportunistic:** §1.6 F4 · §4 backlog · §1.7 noVNC P5 (when the VM is rebuilt).

---
*Living plan set after the 2026-06-18 cull: this roadmap · `truth.md` (wired-vs-working
audit) · `surgical-C-trackc-editor.md` + `deck-editor-integration-plan-6-18-26.md` (open
editor work) · `god-function-decomposition-plan.md` (eng-debt) · `north-star.md` ·
`product-ideas-2026-06-17.md` · `slides-experiment-verdict.md` · `self-host.md` ·
`provider-matrix.md` · `design-novnc-live-browser.md` · `architecture.generated.md`.*
