# Disco release video — v4 production brief

This lane exists to shoot v4. Everything below is grounded in the real code (verified),
so the narration must stay accurate — no over/under-claiming (v3's main flaw).

## Fixes carried from v3 feedback
- 1080p60 (v3 was 720p30). Add muted-autoplay captions. Fix the "Point" headline clipping.
- Kill jargon: say "styled to match your brand," never "house style."
- Show the app **in motion** (agent building, text streaming, deck flipping) — not just stills.
- Zoom to legible focal points instead of full dense pages.

## Chapters

### 0. Title (~2s)
"Disco" card. Tighten.

### 1. DEEP RESEARCH  ← the weak chapter; rebuild
Show, in order:
- **SourcePicker** toggling the four real sources: **Web (ddgs), News (Google News), arXiv, Semantic Scholar**.
- The **recency** control: **week / month** freshness scoping.
- Report streams with **citation chips** (real `Citation.tsx`).
- Hover a claim → **supported / weak / unsupported** labels. This is the headline differentiator:
  a **separate ~300M NLI cross-encoder** checks every atomic claim against its cited passage —
  the model does not grade its own work. Unsupported claims are self-corrected or dropped.
- **PDF export** rendering clearly (fix v3's poor PDF shot).
- **Audio**: flip **podcast (two-host dialogue)** ↔ **single (one narrator)**.
- **Depth tiers** (STANDARD_DEEP … exhaustive) if there's a UI affordance.

Narration: "Choose your sources — web, news, arXiv, Semantic Scholar — and how far back to look.
It writes a cited report, then a separate verifier checks every claim against its source and labels
what's supported and what isn't. Export a clean PDF, or turn it into a two-host podcast or a single narrator."

### 2. SLIDES  ← keep, it's the strongest
"generated from the report, palette-locked, editable PPTX." (Plain language.)

### 3. BUILD  ← recut for honesty; foreground anti-slop
- Show a real working site AND a real structured app (a **form** or **records** primitive with a live
  backend — not just a landing page). Keep claims concrete; don't imply "build anything."
- Foreground the **anti-slop**: `design_lint` (the slop scanner) flags the LLM-median tells —
  **emoji-as-icons** (≥3 = flagged), Inter/Geist monoculture, gradient-text headings,
  centered-hero/3-cards/CTA cliché — and only when unjustified.
- Show the **export**: standalone HTML (deterministic bundler) / PDF.

Narration: "It builds real, working sites — and forms and databases that actually run, not mockups.
And it's built to dodge the AI look: no emoji-icons, no gradient-text hero, no three-card template —
a slop scanner catches the tells before you ship. Export the whole thing as standalone HTML. It's yours."

### 4. AGENT  ← new; v3 skipped it
The same agent driving a real computer: the cockpit's **Terminal/shell**, **live Preview**,
**browser automation**, **Python kernel** — all sandboxed.

Narration: "The same agent runs a real machine — a shell, a live preview, a browser, a Python kernel — all sandboxed."

### 5. THE THESIS  ← new closing chapter; the omissions
On-brand text cards:
- **Open source — Apache-2.0.**
- **Runs on your hardware with open-weight models — Qwen 27B / 35B class** (loop driver; smaller models for RAG/rewrite; a ~300M verifier).
- **Sandboxed four ways** (process / rootless-podman / gVisor VM / remote) + **deny-by-default egress allowlist**; secrets never leave the host.
- **Encoders + voices bundled in, optional.**
- **Your data stays yours.**

Narration: "Open source. Runs on your own hardware with open-weight models — Qwen 27B and 35B class.
Sandboxed four ways, with deny-by-default networking. Encoders and voices bundled in, optional.
Nothing leaves your box."

### 6. End card
Keep "disco — I learn / SELF-HOSTED · OPEN-WEIGHT · YOURS."

## Reproducible pipeline (suggested)
Playwright/Firefox captures of the exact screens above → Kokoro or Piper narration (bundled TTS) →
ffmpeg assemble at 1080p60. (Firefox only on this host — it rasterizes text/oklch; headless Chromium doesn't.)
