# Issues & requests Dylan surfaced — 2026-06-15/16

Durable, tracked copy of everything surfaced during live testing this session. Mirrors
`.omo/evidence/live/BACKLOG.md` (sections C–G), which has the full file:line detail. Nothing here
has been executed — all note-only per instruction. Status tags: 🔴 bug · 🟡 feature/spec · 🟢 polish
· ✅ resolved/answered · ⚙️ design.

## Deep Research / report surface
- **C1** 🔴 Export: only MD works; **PDF & DOCX fail**. Backend is fine (all 3 render); frontend
  `deepResearch.ts:85` POSTs a bare relative URL instead of the agent-server base, so it misses the
  server. Capability probe uses the right base, so buttons light up but the action fails. ~1-line fix.
- **C2** 🟡 No UI control to **listen** to a report (TTS). Audio overview is only an agent tool +
  a Settings panel; no "Listen" affordance on a finished report.
- **D1** 🟡 **"Need More?" card** (replaces the follow-up card): Ask-a-Follow-Up (expands in place) /
  Export as… (dark-glass modal + **working file-path selector**) / Audio Overview (accurate %, then
  Play + Export). Plus wire the existing top-of-report export buttons through the same fixed path.
- **D1-DESIGN** ⚙️ Token-based, dark/light-mode-aware styling spec. Open decision: the glass look
  **deviates** from the design system's stated "never glass" rule (theme.css:12).
- **D1-ROBUSTNESS** 🔴 The card must stay fully interactive after **every** button/state is active
  at once (no one-way latches).
- **D2** 🔴 On re-open, the report **title shows literally "(resumed)"** instead of the real title.
  Should show the full title + status as a *suffix*, for every status — never replace the title.

## Build mode
- **E1** 🟢 Agent constantly narrates **"I can see all the files"** — artifact of the per-turn
  workspace-snapshot injection (#28). Dampen via prompt (tell it the snapshot is silent context).
- **E2** 🔴 **Preview pane stays blank** during a build. Backend detects the dev server, but the pane
  defaults to a client-side "rendered" mode that can't run a Vite app and never falls through to live.
- **E3** 🔴 **Can't access the built site.** Live URL is `{cid8}-{port}.localhost:8000` — Firefox
  doesn't resolve `*.localhost`. A browser-agnostic `/preview-app/` route exists but the iframe
  doesn't use it. (Inline screenshots work because the *agent* reaches it from inside the sandbox.)
- **E4** 🔴⚙️ **Export "download failed" + Manifest does nothing.** `projects_root` points at a dead
  path (old `disco build/`). REVISED canonical fix: storage is opt-in + brittle; make it zero-config
  & self-healing — computed default (`DISCO_DATA_DIR/projects` or OS data dir) → `mkdir -p` →
  override-with-warn-fallback → snapshot-on-finish. "The path is never wrong on any machine."
- **E5** 🔴 Agent **denies execution tools** (browser/slides/etc.) during planning, because the
  planning prompt only lists read tools while the engine gate hides the rest. Fix = add a
  capability-awareness block to the `flavor=="agent"` prompt; keep the gate. (Same blind spot in Build.)
- **E6** 🟡 **Live browser streaming** to the app, across all sandbox backends. Reuse the existing
  screenshot/ephemeral channel (not per-container ports): v1 higher-cadence screenshots, v2 CDP
  screencast. Also: the pane is screenshot-only, not live navigation — fix the "you'll watch it" copy.
- **E7** 🔴 **Artifacts pane**: listing is robust (event-sourced) + uses the right base, BUT
  post-finish downloads inherit E4 (storage), and the download allowlist is **`.xlsx`-only** so
  slides/images/audio are listed-but-not-downloadable.
- **E8** ✅ Audio **"remote" Settings reveals no field** → verified **wired end-to-end** (frontend
  current, backend GET/PUT round-trips, both servers share `disco-config.json`, tool has the Speaches
  dispatch). Root cause = **stale browser bundle** (user saw the old "remote" toggle); fix = hard
  refresh. Remaining real gap: live audio acceptance vs a real Speaches server still untested (#16/#21).

## Agent surface / architecture
- **(answered)** ✅ What the Agent surface is for: the general-task framing of the same engine as
  Build (same loop/scope/sandbox; only copy + canvas + skills scope differ).
- **F0** ⚙️ **Don't fully separate Build & Agent** (would impose a 2× tax on the 6,446-line engine).
  Instead formalize the existing seam (`flavor=="agent"` prompt, `AgentCanvas`, surface-branched
  scope) + a **Build-byte-identical regression guard** so "don't touch Build" is CI-enforced. This is
  the foundation step before any Agent revision (E5/E6).

## Cross-cutting audits (Dylan-requested)
- **Code organization** ⚙️ Monoliths (`engine.py` 6,446 / `runtime.py` 3,836), one real import cycle
  (tools→agent-server), undeclared deps, root clutter. Full report: `docs/audit-2026-06-15-organization-and-licenses.md`.
- **License audit** 🔴 CRITICAL: the **default full-tier reranker is CC-BY-NC-4.0** (non-commercial);
  swap default to `bge-reranker-v2-m3` (MIT). Harvest sources all permissive/ideas-only. Same report.
