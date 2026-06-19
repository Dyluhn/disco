# Design: live browser view — lean noVNC tier (E6 v2)

Status: PLANNED (not started). Author: autonomous session 2026-06-16. Supersedes the
"E6 deferred backend" framing — see findings below.

## TL;DR
Ship a **two-tier** browser view on the Agent surface:
- **Tier 1 (default, already shipped):** a per-action screenshot reel. This is already at
  parity with OpenHands (~50k★) and cline (~62k★), both verified to do exactly this. Leave it
  as-is — no changes.
- **Tier 2 (opt-in, this doc):** a **lean noVNC live view** — the browser running *headed* on a
  bare Xvfb, streamed via x11vnc/TigerVNC → noVNC, embedded as an iframe through the EXISTING
  preview proxy. Gated behind a setting + **lazy-started** so it costs ~nothing unless a user is
  actively watching.

Chosen over CDP screencast because: CDP = light runtime but a **from-scratch frame-transport
channel** (the hard part, per sandbox backend + egress proxy); noVNC = heavier runtime but
**reuses the port-proxy plumbing we already built in E3** ("expose a port → iframe a URL"). For
Disco specifically, noVNC is the lower-*new-code* path, and it's the pattern the biggest OSS
agent app actually chose.

## What the OSS world actually does (verified in code, not assumed)
- **cline** `apps/vscode/src/services/browser/BrowserSession.ts:441` — `page.screenshot()` per
  action, base64 **webp** (png fallback), `<img>`. No stream.
- **OpenHands main repo** `frontend/.../browser/browser.tsx` + `observations.ts` — per-action
  base64 **png** screenshot from the `BROWSE` observation → `<img>`. No stream.
- **OpenHands SDK** `OpenHands/software-agent-sdk` `openhands-agent-server/.../desktop_service.py`
  + `desktop_router.py` — the live tier: **TigerVNC** (`Xvnc` on `DISPLAY :1`, 1280×800) running a
  full **XFCE** desktop, proxied by **noVNC** on `NOVNC_PORT=8002`, bound to **loopback**;
  `GET /desktop/url` returns the noVNC URL for the frontend to iframe; **gated behind
  `enable_vnc=true`**. Browser runs headed via the `browser-use` library; also has session
  recording (`recording.py`).
- **CDP screencast** (Steel, Browserbase, vercel agent-browser) — `Page.startScreencast` → WS →
  canvas. Chromium-only; the modern browser-as-a-service approach.

Conclusion: Tier-1 screenshots = ecosystem norm (we're at parity). The live tier is opt-in
everywhere because it's heavy. OpenHands' noVNC is the proven blueprint; we lean it out.

## Why noVNC over CDP screencast for Disco
| | CDP screencast | noVNC (this design) |
|---|---|---|
| Runtime cost | low/frame, high sustained BW | higher (but lazy → ~0 idle) |
| **New code for Disco** | **2-4 days, new frame channel per backend** | ~2-3 days, **reuses E3 preview proxy** |
| Smoothness | 15-30fps | smooth |
| Takeover | yes (input injection) | yes (native; view-only optional) |
| Browser support | **Chromium only** | any (headed) |
| Firefox `*.localhost` issue | n/a | sidestepped (just an iframe) |
| Precedent | Steel/Browserbase | **OpenHands SDK (verified)** |

The deciding factor: the bespoke transport CDP forces us to invent is *the exact plumbing we
already built in E3* (the `{cid8}-{port}.localhost` host proxy + `/conversations/{cid}/preview-app/`).
noVNC rides it; CDP rebuilds it.

## Architecture

### Components (all gated behind opt-in + lazy-start)
1. **Sandbox image** (`deploy/sandbox/Dockerfile`): add `Xvfb`, `x11vnc` (or TigerVNC), `novnc` +
   `websockify`, a tiny/no WM (chromium `--kiosk` sized to the display; optionally `openbox`
   ~5-10MB if window mapping needs it), and the minimal fonts. **NO XFCE / desktop environment.**
2. **A desktop/live service in the daemon** (mirror OpenHands `DesktopService`, lean): on demand,
   ensure `Xvfb :1` is up, ensure the browser is running **headed** on that `DISPLAY`, start
   `x11vnc` bound to **127.0.0.1** only, start `websockify`/noVNC serving the static noVNC web on a
   curated USER port (reuse the `USER_PORTS` set / `PREVIEW_PORT` machinery). Idempotent
   health-check + start, exactly like `browser.py::_ensure_daemon`.
3. **Agent-server route**: `GET /conversations/{cid}/browser/live-url` → starts the live stack
   (lazy) and returns the noVNC URL, which points at the EXISTING preview proxy
   (`previewHostUrl(cid, NOVNC_PORT)` or the path-based `/conversations/{cid}/preview-app/`-style
   route). Reuse `runtime.port_upstream(cid, NOVNC_PORT)` + the host-proxy resolver. Gated:
   503/typed-reason when the live tier is disabled in Settings.
4. **Frontend** (`AgentCanvas.tsx` BrowserPane): a "Live" toggle next to the screenshot reel.
   When on, fetch the live-url and render an `<iframe>` (noVNC `vnc.html?autoconnect=1&...`).
   Default stays the screenshot reel. `view_only=1` query param by default.
5. **Settings**: an `enable_live_browser` toggle (default OFF), mirroring OpenHands' `enable_vnc`.

### Data flow (live tier)
user clicks "Live" → frontend GET …/browser/live-url → agent-server lazily starts
Xvfb+headed-browser+x11vnc(loopback)+noVNC(port) in the sandbox → returns the auth-gated proxy
URL → frontend iframes it → noVNC ⇄ websockify ⇄ x11vnc(loopback) ⇄ Xvfb framebuffer. VNC encodes
frames **only while the iframe is connected**.

### The two levers that make it lean
- **Strip the desktop**: browser-on-bare-Xvfb, not XFCE. Cuts ~150-250MB RAM *and* attack surface.
- **Lazy-start + encode-on-connect**: VNC does no work without a connected viewer. Default tier is
  headless+screenshots; the VNC stack spins up only on "Live" and tears down on close/idle (reuse
  the existing sandbox-suspend idle path). Unwatched sandboxes cost ~0. Co-tenancy self-bounds
  (you can only watch so many at once).

## Security model (no new holes vs the existing preview)
- **VNC bound to 127.0.0.1 only** — never network-reachable (OpenHands does this verbatim).
- Reached **only** through the existing **auth-gated preview proxy**, **jailed per-conversation**
  (same `{cid8}-{port}` owner-scoping as the dev-server preview). No raw VNC port exposed.
- **View-only by default** (`view_only=1`) — no input injection until takeover is explicitly
  enabled; even then it's the *owner* driving *their own* jailed sandbox over the same auth path.
- Leaning out (no XFCE) **reduces** surface: no D-Bus settings daemon, file manager, panel, etc.
- Optional: a per-session VNC password as defense-in-depth on top of loopback+proxy-auth.

## Performance model
- **Idle (no viewer): ~0** — encode-on-connect.
- **While watched (one session): ~100-200MB RAM overhead** (Xvfb + x11vnc + noVNC + headed
  delta), moderate CPU for VNC encode, ~0.5-2 Mbps. Geometry capped at 1280×800; quality/fps knobs.
- **Image: +100-250MB** (vs +300-600MB for OpenHands' full XFCE).
- OOM-awareness (our history): the headed browser + Xvfb are a constant small add *only when the
  live stack runs*; integrate with the RAM-aware gating already in the runtime.

## Implementation plan (phased)
- **P1 — sandbox image:** add Xvfb/x11vnc/noVNC/websockify (+ tiny WM if needed) to
  `deploy/sandbox/Dockerfile`; rebuild + retag `disco-sandbox:base`; verify size + a smoke test
  (start Xvfb, launch chromium headed, x11vnc on loopback, noVNC serves). ~½-1 day.
- **P2 — daemon live service:** add the lazy desktop service to the browser tooling
  (`browser.py` / a new `live_view.py`): ensure-Xvfb, headed-browser-on-DISPLAY, x11vnc(loopback),
  noVNC(port). Idempotent health-check/start; teardown on idle. ~1 day.
- **P3 — agent-server route + proxy wiring:** `GET /conversations/{cid}/browser/live-url`,
  `port_upstream(cid, NOVNC_PORT)`, host-proxy resolver entry, per-conv jail, 503-when-disabled.
  ~½ day.
- **P4 — frontend + settings:** "Live" toggle + iframe in AgentCanvas BrowserPane; `enable_live_browser`
  Settings toggle (default OFF). ~½ day.
- **P5 — verify:** live-acceptance on a real sandbox backend (local/podman): open Live, watch the
  agent navigate, confirm loopback-bind + per-conv jail (another owner can't reach it), view-only,
  idle teardown. ~½ day.

Total: **~2-3 days**.

## Chosen defaults (LOCKED 2026-06-16)
1. **Browser lifecycle — lazy headed-on-Xvfb on FIRST browser use; VNC bridge lazy on Live-open.**
   Non-browsing sessions pay nothing; the first browser action starts the browser headed on Xvfb
   and keeps it headed for the session; the VNC encode bridge starts only when a viewer connects.
   No browser restart when toggling Live (state preserved). The "leanest" alternative
   (headed-on-demand) is rejected: it trades a trivial RAM saving for a page-losing restart.
2. **View-only first.** Ship watch-only (`view_only=1`); no input channel into the sandbox.
   Takeover (input injection) is a later, explicit opt-in.
3. **No window manager.** chromium `--kiosk` sized to the display; add `openbox` (~8MB) ONLY if a
   P1/P2 smoke test shows window mapping misbehaving.
4. **local/podman backends first; gVisor deferred.** gVisor needs the egress-proxy allowlist
   updated for the noVNC port (the D7 egress work) — a clean follow-up, not a blocker.

## References (verified this session, clones at ~/agent-refs/)
- OpenHands SDK: `software-agent-sdk/openhands-agent-server/openhands/agent_server/desktop_service.py`,
  `desktop_router.py`; browser tool `openhands-tools/openhands/tools/browser_use/`.
- OpenHands app: `frontend/src/components/features/browser/browser.tsx`, `services/observations.ts`.
- cline: `apps/vscode/src/services/browser/BrowserSession.ts` (webp screenshots).
- Disco files to touch: `_browser_daemon.py`, `browser.py`, `host_proxy.py`, `app.py` (preview
  routes), `runtime.py` (`port_upstream`/`ensure_preview`), `AgentCanvas.tsx`,
  `deploy/sandbox/Dockerfile`.
