"""First-party starter file templates for ``disco.core.kits.starter``.

The templates here are deliberately plain text and dependency-free. Builders replace
only title placeholders, then the registry applies path safety before a tool writes
anything to a workspace.
"""

from __future__ import annotations

import html
import json


def _render(files: dict[str, str], title: str) -> dict[str, str]:
    short = title.strip()[:12] or "App"
    initial = (title.strip()[:1] or "A").upper()
    replacements = {
        "__TITLE_HTML__": html.escape(title, quote=True),
        "__TITLE_JSON__": json.dumps(title),
        "__TITLE_TEXT__": title,
        "__SHORT_TITLE_JSON__": json.dumps(short),
        "__ICON_INITIAL_HTML__": html.escape(initial, quote=True),
    }
    rendered: dict[str, str] = {}
    for path, text in files.items():
        for marker, value in replacements.items():
            text = text.replace(marker, value)
        rendered[path] = text
    return rendered


def game_loop_vanilla(title: str) -> dict[str, str]:
    return _render(_GAME_LOOP_FILES, title)


def pwa_shell(title: str) -> dict[str, str]:
    return _render(_PWA_SHELL_FILES, title)


def device_frames(title: str) -> dict[str, str]:
    return _render(_DEVICE_FRAMES_FILES, title)


def ui_kit_dense(title: str) -> dict[str, str]:
    return _render(_UI_KIT_DENSE_FILES, title)


_GAME_LOOP_FILES: dict[str, str] = {
    "index.html": """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE_HTML__</title>
<style>
:root{color-scheme:dark;--bg:#101217;--panel:#191d26;--ink:#f5f7fb;--muted:#aeb6c6;--gold:#ffd36\
a;--blue:#75d1ff}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:radial-gradient(circle at 20% 10%,#223149 0,#101217 44rem);color:var(--\
ink);font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;\
display:grid;place-items:center;touch-action:manipulation}
.shell{width:min(100vw,960px);padding:18px;display:grid;gap:12px}
.hud{display:flex;align-items:center;justify-content:space-between;gap:12px;color:var(--muted);f\
ont-size:14px}
.hud strong{color:var(--ink);font-variant-numeric:tabular-nums}
.stage{position:relative;aspect-ratio:16/9;width:100%;overflow:hidden;border:1px solid #303749;b\
order-radius:8px;background:#080a0e;box-shadow:0 18px 48px #0008}
canvas{display:block;width:100%;height:100%;image-rendering:pixelated}
.touch-pad{display:grid;grid-template-columns:repeat(3,54px);grid-template-rows:repeat(2,48px);g\
ap:8px;justify-content:center}
.touch-pad button{min-width:48px;min-height:44px;border:1px solid #3a4358;border-radius:8px;back\
ground:#1d2430;color:var(--ink);font:700 14px/1 ui-sans-serif,system-ui,sans-serif;touch-action:\
none}
.touch-pad button:active{transform:scale(.96);background:#273349}
:where(button):focus-visible{outline:3px solid var(--blue);outline-offset:3px}
.touch-pad [data-press="left"]{grid-column:1;grid-row:2}.touch-pad [data-press="right"]{grid-col\
umn:3;grid-row:2}.touch-pad [data-press="up"]{grid-column:2;grid-row:1}.touch-pad [data-press="d\
own"]{grid-column:2;grid-row:2}.touch-pad [data-press="action"]{grid-column:3;grid-row:1}
@media (hover:hover){.touch-pad button:hover{border-color:#6a7896}}
@media (min-width:720px){.touch-pad{display:none}}
</style>
</head>
<body>
<main class="shell">
  <div class="hud">
    <span><strong>Move:</strong> WASD/Arrows or touch pad</span>
    <span><strong id="scoreLabel">Score 0</strong></span>
  </div>
  <div class="stage">
    <canvas id="game" width="480" height="270" aria-label="Playable canvas game starter"></canvas>
  </div>
  <div class="touch-pad" aria-label="Touch controls">
    <button type="button" data-press="up">UP</button>
    <button type="button" data-press="action">GO</button>
    <button type="button" data-press="left">LEFT</button>
    <button type="button" data-press="down">DOWN</button>
    <button type="button" data-press="right">RIGHT</button>
  </div>
</main>
<script src="game.js"></script>
</body>
</html>
""",
    "game.js": """const GAME_TITLE = __TITLE_JSON__;
const canvas = document.querySelector("#game");
const ctx = canvas.getContext("2d");
const scoreLabel = document.querySelector("#scoreLabel");
const VIEW_W = canvas.width;
const VIEW_H = canvas.height;
const FIXED_DT = 1 / 60;
const MAX_FRAME = 0.25;

const keyMap = new Map([
  ["ArrowUp", "up"], ["KeyW", "up"], ["w", "up"],
  ["ArrowDown", "down"], ["KeyS", "down"], ["s", "down"],
  ["ArrowLeft", "left"], ["KeyA", "left"], ["a", "left"],
  ["ArrowRight", "right"], ["KeyD", "right"], ["d", "right"],
  ["Space", "action"], ["Enter", "action"], [" ", "action"],
]);

const input = {
  held: new Set(),
  pressed: new Set(),
  set(name, down) {
    if (down && !this.held.has(name)) this.pressed.add(name);
    down ? this.held.add(name) : this.held.delete(name);
  },
  down(name) { return this.held.has(name); },
  tap(name) { return this.pressed.has(name); },
  clearPressed() { this.pressed.clear(); },
};

addEventListener("keydown", (event) => {
  const mapped = keyMap.get(event.code) || keyMap.get(event.key);
  if (!mapped) return;
  event.preventDefault();
  input.set(mapped, true);
});
addEventListener("keyup", (event) => {
  const mapped = keyMap.get(event.code) || keyMap.get(event.key);
  if (!mapped) return;
  event.preventDefault();
  input.set(mapped, false);
});
document.querySelectorAll("[data-press]").forEach((button) => {
  const name = button.dataset.press;
  const release = () => input.set(name, false);
  button.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    button.setPointerCapture(event.pointerId);
    input.set(name, true);
    ensureAudio();
  });
  button.addEventListener("pointerup", release);
  button.addEventListener("pointercancel", release);
  button.addEventListener("lostpointercapture", release);
});
canvas.addEventListener("pointerdown", () => ensureAudio());

function makeSpriteSheetDataUri() {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="16" viewBox="0 0 32 16">
    <rect width="16" height="16" rx="3" fill="#75d1ff"/>
    <rect x="19" y="3" width="10" height="10" rx="2" fill="#ffd36a"/>
    <path d="M5 5h6v6H5z" fill="#102030" opacity=".35"/>
  </svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

function loadSpriteSheet(src, frameWidth, frameHeight) {
  const image = new Image();
  const sheet = {
    image, frameWidth, frameHeight, loaded: false,
    draw(target, frame, x, y, width = frameWidth, height = frameHeight) {
      if (!this.loaded) {
        target.fillStyle = frame === 0 ? "#75d1ff" : "#ffd36a";
        target.fillRect(x, y, width, height);
        return;
      }
      const cols = Math.max(1, Math.floor(image.width / frameWidth));
      const sx = (frame % cols) * frameWidth;
      const sy = Math.floor(frame / cols) * frameHeight;
      target.drawImage(image, sx, sy, frameWidth, frameHeight, x, y, width, height);
    },
  };
  image.addEventListener("load", () => { sheet.loaded = true; }, { once: true });
  image.src = src;
  return sheet;
}
const sprites = loadSpriteSheet(makeSpriteSheetDataUri(), 16, 16);

let audioCtx = null;
function ensureAudio() {
  audioCtx ||= new (window.AudioContext || window.webkitAudioContext)();
  if (audioCtx.state === "suspended") audioCtx.resume();
  return audioCtx;
}

// Self-authored clean-room one-shot synth: tiny parameter SFX, not vendored ZzFX code.
function oneShotSynth(opts = {}) {
  const a = ensureAudio(), now = a.currentTime;
  const p = { freq: 440, end: 0, dur: 0.14, wave: "square", vol: 0.12, attack: 0.004, noise: 0, \
...opts };
  const out = a.createGain();
  out.gain.setValueAtTime(0, now);
  out.gain.linearRampToValueAtTime(p.vol, now + p.attack);
  out.gain.exponentialRampToValueAtTime(0.0001, now + p.dur);
  out.connect(a.destination);
  if (p.noise > 0) {
    const frames = Math.max(1, Math.floor(a.sampleRate * p.dur));
    const buffer = a.createBuffer(1, frames, a.sampleRate);
    const data = buffer.getChannelData(0);
    for (let i = 0; i < frames; i += 1) data[i] = (Math.random() * 2 - 1) * p.noise;
    const src = a.createBufferSource();
    src.buffer = buffer;
    src.connect(out);
    src.start(now);
    src.stop(now + p.dur);
    return;
  }
  const osc = a.createOscillator();
  osc.type = p.wave;
  osc.frequency.setValueAtTime(p.freq, now);
  osc.frequency.exponentialRampToValueAtTime(Math.max(20, p.freq + p.end), now + p.dur);
  osc.connect(out);
  osc.start(now);
  osc.stop(now + p.dur);
}

const camera = { x: 0, y: 0 };
const shake = {
  time: 0, amount: 0,
  kick(amount = 5, time = 0.18) {
    this.amount = Math.max(this.amount, amount);
    this.time = Math.max(this.time, time);
  },
  offset(dt) {
    this.time = Math.max(0, this.time - dt);
    const power = this.amount * (this.time / 0.18);
    return this.time > 0 ? { x: (Math.random() - 0.5) * power, y: (Math.random() - 0.5) * power \
} : { x: 0, y: 0 };
  },
};
const tweens = [];
const particles = [];
let hitStopUntil = 0;
const clamp = (n, min, max) => Math.max(min, Math.min(max, n));
const lerp = (a, b, t) => a + (b - a) * t;
const easeOut = (t) => 1 - Math.pow(1 - t, 3);

function hitStop(ms = 60) {
  hitStopUntil = Math.max(hitStopUntil, performance.now() + clamp(ms, 40, 80));
}
function squash(target, sx = 1.25, sy = 0.75, duration = 0.12) {
  target.scaleX = sx;
  target.scaleY = sy;
  tweens.push({ target, duration, age: 0 });
}
function burst(x, y, color = "#ffd36a", count = 16) {
  for (let i = 0; i < count; i += 1) {
    const angle = Math.random() * Math.PI * 2;
    const speed = 36 + Math.random() * 92;
    particles.push({ x, y, vx: Math.cos(angle) * speed, vy: Math.sin(angle) * speed, age: 0, lif\
e: 0.32 + Math.random() * 0.25, color });
  }
}
function updateJuice(dt) {
  for (let i = tweens.length - 1; i >= 0; i -= 1) {
    const t = tweens[i];
    t.age += dt;
    const k = easeOut(clamp(t.age / t.duration, 0, 1));
    t.target.scaleX = lerp(t.target.scaleX, 1, k);
    t.target.scaleY = lerp(t.target.scaleY, 1, k);
    if (t.age >= t.duration) tweens.splice(i, 1);
  }
  for (let i = particles.length - 1; i >= 0; i -= 1) {
    const p = particles[i];
    p.age += dt;
    p.vy += 260 * dt;
    p.x += p.vx * dt;
    p.y += p.vy * dt;
    if (p.age >= p.life) particles.splice(i, 1);
  }
}

function makeCollectible() {
  return { x: 40 + Math.random() * 400, y: 42 + Math.random() * 178, w: 14, h: 14 };
}
function overlaps(a, b) {
  return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
}

const sceneStack = [];
const topScene = () => sceneStack[sceneStack.length - 1];
const pushScene = (scene) => sceneStack.push(scene);
const replaceScene = (scene) => { sceneStack.splice(0, sceneStack.length, scene); };

class MenuScene {
  update() {
    if (input.tap("action") || input.tap("up") || input.tap("down") || input.tap("left") || inpu\
t.tap("right")) {
      oneShotSynth({ freq: 330, end: 260, dur: 0.12, wave: "triangle" });
      replaceScene(new PlayScene());
    }
  }
  render() {
    ctx.fillStyle = "#0b0f16";
    ctx.fillRect(0, 0, VIEW_W, VIEW_H);
    ctx.fillStyle = "#f5f7fb";
    ctx.font = "700 28px ui-sans-serif, system-ui";
    ctx.textAlign = "center";
    ctx.fillText(GAME_TITLE, VIEW_W / 2, 104);
    ctx.font = "14px ui-sans-serif, system-ui";
    ctx.fillStyle = "#aeb6c6";
    ctx.fillText("Press Space, Enter, or any touch control to start", VIEW_W / 2, 138);
  }
}

class PlayScene {
  constructor() {
    this.player = { x: 60, y: 120, w: 18, h: 18, speed: 112, scaleX: 1, scaleY: 1 };
    this.coin = makeCollectible();
    this.score = 0;
    this.targetScore = 8;
    scoreLabel.textContent = "Score 0";
  }
  update(dt) {
    const p = this.player;
    let dx = Number(input.down("right")) - Number(input.down("left"));
    let dy = Number(input.down("down")) - Number(input.down("up"));
    const mag = Math.hypot(dx, dy) || 1;
    dx /= mag; dy /= mag;
    p.x = clamp(p.x + dx * p.speed * dt, 8, 900);
    p.y = clamp(p.y + dy * p.speed * dt, 8, 480);
    camera.x = lerp(camera.x, p.x - VIEW_W / 2, 1 - Math.pow(0.001, dt));
    camera.y = lerp(camera.y, p.y - VIEW_H / 2, 1 - Math.pow(0.001, dt));
    if (overlaps(p, this.coin)) {
      this.score += 1;
      scoreLabel.textContent = `Score ${this.score}`;
      burst(this.coin.x + 7, this.coin.y + 7);
      shake.kick(7, 0.18);
      squash(p);
      hitStop(55);
      oneShotSynth({ freq: 660 + this.score * 20, end: 180, dur: 0.11, wave: "square", vol: 0.09 });
      this.coin = makeCollectible();
      if (this.score >= this.targetScore) pushScene(new GameOverScene(this.score));
    }
    updateJuice(dt);
  }
  render(dt) {
    const s = shake.offset(dt);
    ctx.save();
    ctx.fillStyle = "#0b0f16";
    ctx.fillRect(0, 0, VIEW_W, VIEW_H);
    ctx.translate(Math.round(-camera.x + s.x), Math.round(-camera.y + s.y));
    drawGrid();
    sprites.draw(ctx, 1, this.coin.x, this.coin.y, this.coin.w, this.coin.h);
    const p = this.player;
    ctx.save();
    ctx.translate(p.x + p.w / 2, p.y + p.h / 2);
    ctx.scale(p.scaleX, p.scaleY);
    sprites.draw(ctx, 0, -p.w / 2, -p.h / 2, p.w, p.h);
    ctx.restore();
    for (const part of particles) {
      const alpha = 1 - part.age / part.life;
      ctx.globalAlpha = alpha;
      ctx.fillStyle = part.color;
      ctx.fillRect(part.x - 2, part.y - 2, 4, 4);
    }
    ctx.globalAlpha = 1;
    ctx.restore();
  }
}

class GameOverScene {
  constructor(score) {
    this.score = score;
    this.cooldown = 0.35;
  }
  update(dt) {
    this.cooldown -= dt;
    if (this.cooldown <= 0 && input.tap("action")) replaceScene(new PlayScene());
  }
  render(dt) {
    sceneStack[0]?.render(dt);
    ctx.fillStyle = "rgba(5,8,13,.72)";
    ctx.fillRect(0, 0, VIEW_W, VIEW_H);
    ctx.fillStyle = "#f5f7fb";
    ctx.textAlign = "center";
    ctx.font = "700 24px ui-sans-serif, system-ui";
    ctx.fillText("Collected all sparks", VIEW_W / 2, 112);
    ctx.font = "14px ui-sans-serif, system-ui";
    ctx.fillStyle = "#ffd36a";
    ctx.fillText(`Final score ${this.score} - press Space to restart`, VIEW_W / 2, 142);
  }
}

function drawGrid() {
  ctx.strokeStyle = "#1b2432";
  ctx.lineWidth = 1;
  for (let x = 0; x <= 960; x += 24) {
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, 540); ctx.stroke();
  }
  for (let y = 0; y <= 540; y += 24) {
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(960, y); ctx.stroke();
  }
}

let previous = performance.now();
let accumulator = 0;
function frame(now) {
  const dt = Math.min((now - previous) / 1000, MAX_FRAME);
  previous = now;
  if (now >= hitStopUntil) accumulator += dt;
  while (accumulator >= FIXED_DT) {
    topScene().update(FIXED_DT);
    accumulator -= FIXED_DT;
  }
  topScene().render(now < hitStopUntil ? 0 : accumulator);
  input.clearPressed();
  requestAnimationFrame(frame);
}

replaceScene(new MenuScene());
requestAnimationFrame(frame);
""",
    "README.md": """# __TITLE_TEXT__ game starter

This is a zero-dependency Canvas2D starter that is already playable: move the square,
collect sparks, and watch the score update. It is intended as a small running base for
single-screen games before you choose a larger engine.

What is included:

- Fixed-timestep requestAnimationFrame loop with an accumulator.
- Scene stack with menu, play, and game-over scenes.
- Keyboard plus touch-button input map.
- Sprite-sheet loader using an inline starter sheet.
- Self-authored clean-room one-shot WebAudio synth for small effects.
- Juice helpers for screen shake, squash/stretch, hit-stop, particles, and lerp camera.

Start by editing `PlayScene`, `makeCollectible`, and `drawGrid` in `game.js`.
""",
    "NOTES.md": """# game_loop_vanilla usage notes

- Open `index.html` directly or serve the folder with any static server.
- Keep the fixed-step `update(FIXED_DT)` path deterministic; put rendering-only
  interpolation in `render`.
- Replace the inline SVG sheet in `makeSpriteSheetDataUri()` with your real sprite sheet
  once art exists.
- The synth is a clean-room, self-authored one-shot helper inspired by tiny game SFX
  tools. It is not vendored ZzFX code.
- Use `hitStop(40..80)`, `shake.kick()`, `squash()`, and `burst()` at collisions,
  pickups, and attacks so the stub keeps game feel as it grows.
""",
}


_PWA_ICON_192_ANY = """<svg xmlns="http://www.w3.org/2000/svg" width="192" height="192" viewBox=\
"0 0 192 192">
<rect width="192" height="192" rx="42" fill="#0f766e"/>
<rect x="34" y="34" width="124" height="124" rx="30" fill="#f8fafc"/>
<text x="96" y="116" text-anchor="middle" font-family="Arial,sans-serif" font-size="64" font-wei\
ght="700" fill="#0f766e">__ICON_INITIAL_HTML__</text>
</svg>
"""

_PWA_ICON_512_ANY = """<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox=\
"0 0 512 512">
<rect width="512" height="512" rx="112" fill="#0f766e"/>
<rect x="90" y="90" width="332" height="332" rx="80" fill="#f8fafc"/>
<text x="256" y="308" text-anchor="middle" font-family="Arial,sans-serif" font-size="172" font-w\
eight="700" fill="#0f766e">__ICON_INITIAL_HTML__</text>
</svg>
"""

_PWA_ICON_192_MASKABLE = """<svg xmlns="http://www.w3.org/2000/svg" width="192" height="192" vie\
wBox="0 0 192 192">
<rect width="192" height="192" fill="#0f766e"/>
<circle cx="96" cy="96" r="62" fill="#f8fafc"/>
<text x="96" y="116" text-anchor="middle" font-family="Arial,sans-serif" font-size="58" font-wei\
ght="700" fill="#0f766e">__ICON_INITIAL_HTML__</text>
</svg>
"""

_PWA_ICON_512_MASKABLE = """<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" vie\
wBox="0 0 512 512">
<rect width="512" height="512" fill="#0f766e"/>
<circle cx="256" cy="256" r="166" fill="#f8fafc"/>
<text x="256" y="308" text-anchor="middle" font-family="Arial,sans-serif" font-size="160" font-w\
eight="700" fill="#0f766e">__ICON_INITIAL_HTML__</text>
</svg>
"""

_PWA_SHELL_FILES: dict[str, str] = {
    "index.html": """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fi\
t=cover">
<meta name="theme-color" content="#0f766e">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="__TITLE_HTML__">
<link rel="manifest" href="manifest.webmanifest">
<link rel="apple-touch-icon" href="icons/icon-192-any.svg">
<link rel="stylesheet" href="styles.4fd8.css">
<title>__TITLE_HTML__</title>
</head>
<body>
<header class="top-chrome app-chrome">
  <strong>__TITLE_HTML__</strong>
  <button id="installButton" type="button" hidden>Install</button>
</header>
<main class="app-screen">
  <section class="hero-panel">
    <p class="eyebrow">Offline-ready shell</p>
    <h1>Build the phone workflow first.</h1>
    <p>Manifest, service worker, safe-area chrome, install prompt deferral, and iOS coach-mark h\
ooks are wired.</p>
    <button class="primary-action" type="button" data-value-proof>Mark value delivered</button>
  </section>
  <aside id="iosCoach" class="coach-mark" hidden>
    On iOS, use Share, then Add to Home Screen.
  </aside>
</main>
<nav class="bottom-tabs app-chrome" aria-label="Primary">
  <a href="#" aria-current="page">Home</a>
  <a href="#">Work</a>
  <a href="#">Profile</a>
</nav>
<script src="app.68ca.js"></script>
</body>
</html>
""",
    "styles.4fd8.css": """:root{--theme:#0f766e;--ink:#10201d;--muted:#5a6964;--bg:#f7faf7;--pan\
el:#ffffff;--line:rgba(16,32,29,.1);--safe-top:env(safe-area-inset-top);--safe-right:env(safe-ar\
ea-inset-right);--safe-bottom:env(safe-area-inset-bottom);--safe-left:env(safe-area-inset-left)}
*{box-sizing:border-box}
html{touch-action:manipulation;-webkit-text-size-adjust:100%;background:var(--bg)}
body{margin:0;min-height:100svh;padding:calc(56px + var(--safe-top)) var(--safe-right) calc(64px\
 + var(--safe-bottom)) var(--safe-left);font-family:ui-sans-serif,system-ui,-apple-system,BlinkM\
acSystemFont,"Segoe UI",sans-serif;color:var(--ink);background:var(--bg);overscroll-behavior-y:c\
ontain}
a,button{font:inherit}
:where(a,button,[role="button"]){touch-action:manipulation;-webkit-tap-highlight-color:transparent}
:where(a,button,[role="button"]):focus-visible{outline:3px solid color-mix(in srgb,var(--theme) \
70%,white);outline-offset:3px}
:where(a,button,[role="button"]):active{transform:scale(.97)}
@media (hover:hover){:where(a,button,[role="button"]):hover{filter:brightness(.97)}}
.app-chrome{position:fixed;left:0;right:0;z-index:10;background:rgba(255,255,255,.9);backdrop-fi\
lter:blur(18px);border-color:var(--line);padding-left:max(16px,var(--safe-left));padding-right:m\
ax(16px,var(--safe-right))}
.top-chrome{top:0;min-height:calc(56px + var(--safe-top));padding-top:var(--safe-top);display:fl\
ex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line)}
.bottom-tabs{bottom:0;min-height:calc(56px + var(--safe-bottom));padding-bottom:var(--safe-botto\
m);display:grid;grid-template-columns:repeat(3,1fr);border-top:1px solid var(--line)}
.bottom-tabs a{min-height:56px;display:grid;place-items:center;color:var(--muted);text-decoratio\
n:none;border-radius:12px}
.bottom-tabs a[aria-current="page"]{color:var(--theme);font-weight:700;background:rgba(15,118,11\
0,.09)}
.app-screen{min-height:calc(100svh - 120px);display:grid;align-items:end;padding:20px}
.hero-panel{display:grid;gap:14px;background:var(--panel);border:1px solid var(--line);border-ra\
dius:20px;padding:22px;box-shadow:0 16px 40px rgba(20,40,35,.08)}
.eyebrow{margin:0;color:var(--theme);font-size:12px;font-weight:800;text-transform:uppercase;let\
ter-spacing:.08em}
h1{margin:0;font-size:clamp(28px,8vw,44px);line-height:1.02;letter-spacing:0}
p{margin:0;color:var(--muted);line-height:1.5}
button,.primary-action{min-height:44px;border:0;border-radius:12px;padding:0 16px;background:var\
(--theme);color:white;font-weight:800}
#installButton[hidden],.coach-mark[hidden]{display:none}
.coach-mark{position:fixed;right:max(12px,var(--safe-right));bottom:calc(72px + var(--safe-botto\
m));max-width:260px;padding:12px 14px;border:1px solid var(--line);border-radius:14px;background\
:#10201d;color:white;box-shadow:0 16px 36px rgba(0,0,0,.24)}
""",
    "app.68ca.js": """const installButton = document.querySelector("#installButton");
const proofButton = document.querySelector("[data-value-proof]");
const iosCoach = document.querySelector("#iosCoach");
let deferredInstallPrompt = null;
let valueProofSeen = false;

if ("serviceWorker" in navigator) {
  addEventListener("load", () => {
    navigator.serviceWorker.register("sw.js").catch((error) => {
      console.warn("Service worker registration failed", error);
    });
  });
}

function isStandalone() {
  return matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
}

function updateInstallSurface() {
  const isIos = /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacInt\
el" && navigator.maxTouchPoints > 1);
  if (deferredInstallPrompt && valueProofSeen && !isStandalone()) installButton.hidden = false;
  if (isIos && valueProofSeen && !isStandalone()) iosCoach.hidden = false;
}

addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  deferredInstallPrompt = event;
  updateInstallSurface();
});

installButton.addEventListener("click", async () => {
  if (!deferredInstallPrompt) return;
  installButton.hidden = true;
  deferredInstallPrompt.prompt();
  await deferredInstallPrompt.userChoice;
  deferredInstallPrompt = null;
});

proofButton.addEventListener("click", () => {
  valueProofSeen = true;
  proofButton.textContent = "Value delivered";
  updateInstallSurface();
});
""",
    "sw.js": """const SHELL_CACHE = "pwa-shell-v1";
const HTML_CACHE = "pwa-html-v1";
const HASHED_SHELL_ASSETS = [
  "/styles.4fd8.css",
  "/app.68ca.js",
  "/manifest.webmanifest",
  "/icons/icon-192-any.svg",
  "/icons/icon-512-any.svg",
  "/icons/icon-192-maskable.svg",
  "/icons/icon-512-maskable.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL_CACHE).then((cache) => cache.addAll(["/", ...HASHED_SHELL_AS\
SETS])));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => ![SHELL_CACHE, H\
TML_CACHE].includes(key)).map((key) => caches.delete(key)))));
  self.clients.claim();
});

async function cacheFirst(request) {
  const cached = await caches.match(request, { ignoreSearch: true });
  if (cached) return cached;
  const response = await fetch(request);
  const cache = await caches.open(SHELL_CACHE);
  cache.put(request, response.clone());
  return response;
}

async function networkFirst(request) {
  const cache = await caches.open(HTML_CACHE);
  try {
    const response = await fetch(request);
    cache.put(request, response.clone());
    return response;
  } catch {
    return (await cache.match(request)) || caches.match("/");
  }
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const acceptsHtml = request.headers.get("accept")?.includes("text/html");
  if (request.mode === "navigate" || acceptsHtml) {
    event.respondWith(networkFirst(request));
    return;
  }
  const url = new URL(request.url);
  if (HASHED_SHELL_ASSETS.includes(url.pathname)) event.respondWith(cacheFirst(request));
});
""",
    "manifest.webmanifest": """{
  "name": __TITLE_JSON__,
  "short_name": __SHORT_TITLE_JSON__,
  "start_url": ".",
  "scope": ".",
  "display": "standalone",
  "background_color": "#f7faf7",
  "theme_color": "#0f766e",
  "icons": [
    { "src": "icons/icon-192-any.svg", "sizes": "192x192", "type": "image/svg+xml", "purpose": "\
any" },
    { "src": "icons/icon-512-any.svg", "sizes": "512x512", "type": "image/svg+xml", "purpose": "\
any" },
    { "src": "icons/icon-192-maskable.svg", "sizes": "192x192", "type": "image/svg+xml", "purpos\
e": "maskable" },
    { "src": "icons/icon-512-maskable.svg", "sizes": "512x512", "type": "image/svg+xml", "purpos\
e": "maskable" }
  ]
}
""",
    "icons/icon-192-any.svg": _PWA_ICON_192_ANY,
    "icons/icon-512-any.svg": _PWA_ICON_512_ANY,
    "icons/icon-192-maskable.svg": _PWA_ICON_192_MASKABLE,
    "icons/icon-512-maskable.svg": _PWA_ICON_512_MASKABLE,
    "NOTES.md": """# pwa_shell usage notes

- Edit `manifest.webmanifest` names, colors, and icon artwork before shipping.
- Keep separate `purpose: any` and `purpose: maskable` icon entries at 192 and 512.
- The service worker precaches the app shell and cache-first hashed CSS/JS assets, then
  uses network-first for HTML navigations.
- Fixed top and bottom chrome use `viewport-fit=cover` plus `env(safe-area-inset-*)`.
- The install prompt is deferred until `data-value-proof` is clicked. iOS has no
  `beforeinstallprompt`, so the coach mark is a snippet to replace with your product copy.
""",
}


_DEVICE_FRAMES_FILES: dict[str, str] = {
    "index.html": """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE_HTML__ device frames</title>
<link rel="stylesheet" href="device-frames.css">
</head>
<body>
<main class="demo-page">
  <header>
    <p class="eyebrow">First-party clean-room frames</p>
    <h1>__TITLE_HTML__ previews</h1>
  </header>
  <section class="frame-grid">
    <article class="device-frame iphone-ish" style="--frame-screen-width:300px">
      <span class="side-button side-button-a"></span>
      <span class="side-button side-button-b"></span>
      <div class="frame-screen">
        <div class="screen-demo">iPhone-ish screen slot</div>
      </div>
    </article>
    <article class="device-frame android-ish" style="--frame-screen-width:300px">
      <span class="side-button side-button-a"></span>
      <span class="side-button side-button-b"></span>
      <div class="frame-screen">
        <div class="screen-demo alt">Android-ish screen slot</div>
      </div>
    </article>
  </section>
  <section class="window-grid">
    <article class="macos-window">
      <div class="window-titlebar"><span class="traffic-lights"></span><strong>Dashboard</strong\
></div>
      <div class="window-body">macOS-style window body</div>
    </article>
    <article class="browser-window">
      <div class="browser-toolbar"><span class="traffic-lights"></span><div class="address-bar">\
https://example.local</div></div>
      <div class="window-body">Browser chrome body</div>
    </article>
  </section>
</main>
</body>
</html>
""",
    "device-frames.css": """/* Clean-room frame CSS: first-party shapes inspired by common hardw\
are, not copied from devices.css. */
:root{--page-bg:#f5f5f0;--ink:#161712;--muted:#62655c;--frame:#111319;--screen:#f8fafc;--line:rg\
ba(22,23,18,.12);--frame-screen-width:320px}
*{box-sizing:border-box}
body{margin:0;min-height:100svh;background:var(--page-bg);color:var(--ink);font-family:ui-sans-s\
erif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.demo-page{width:min(1120px,100%);margin:0 auto;padding:32px 18px;display:grid;gap:28px}
.eyebrow{margin:0 0 6px;color:#0d766f;font-size:12px;font-weight:800;text-transform:uppercase;le\
tter-spacing:.08em}
h1{margin:0;font-size:clamp(30px,6vw,56px);letter-spacing:0}
.frame-grid,.window-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));g\
ap:24px;align-items:start}
.device-frame{--bezel:16px;--screen-radius:34px;position:relative;width:calc(var(--frame-screen-\
width) + var(--bezel) * 2);margin-inline:auto;padding:var(--bezel);background:linear-gradient(14\
5deg,#252a33,#080a0f);border:1px solid #3d4350;box-shadow:0 24px 70px rgba(0,0,0,.24),inset 0 0 \
0 2px rgba(255,255,255,.04)}
.device-frame::before,.device-frame::after{content:"";position:absolute;z-index:3;pointer-events\
:none}
.iphone-ish{border-radius:52px}
.iphone-ish::before{width:92px;height:25px;left:50%;top:20px;transform:translateX(-50%);border-r\
adius:999px;background:#06070a;box-shadow:inset 0 -1px 1px rgba(255,255,255,.12)}
.iphone-ish::after{width:112px;height:4px;left:50%;bottom:10px;transform:translateX(-50%);border\
-radius:999px;background:rgba(255,255,255,.55)}
.android-ish{border-radius:38px;--screen-radius:24px}
.android-ish::before{width:15px;height:15px;left:50%;top:24px;transform:translateX(-50%);border-\
radius:999px;background:#050609;box-shadow:0 0 0 2px rgba(255,255,255,.08)}
.android-ish::after{width:72px;height:3px;left:50%;bottom:12px;transform:translateX(-50%);border\
-radius:999px;background:rgba(255,255,255,.32)}
.frame-screen{position:relative;width:var(--frame-screen-width);aspect-ratio:9/19.5;overflow:hid\
den;border-radius:var(--screen-radius);background:var(--screen);box-shadow:inset 0 0 0 1px rgba(\
255,255,255,.18)}
.side-button{position:absolute;width:4px;border-radius:999px;background:#2c323e}
.side-button-a{height:48px;left:-3px;top:96px}.side-button-b{height:72px;right:-3px;top:142px}
.screen-demo{height:100%;display:grid;place-items:center;padding:24px;text-align:center;color:#f\
ff;background:linear-gradient(160deg,#0f766e,#162033 62%,#101217)}
.screen-demo.alt{background:linear-gradient(160deg,#334155,#f59e0b)}
.macos-window,.browser-window{overflow:hidden;border:1px solid var(--line);border-radius:10px;ba\
ckground:#fff;box-shadow:0 18px 48px rgba(37,40,32,.12)}
.window-titlebar,.browser-toolbar{height:42px;display:flex;align-items:center;gap:12px;padding:0\
 14px;border-bottom:1px solid var(--line);background:#f6f7f4}
.traffic-lights{position:relative;display:inline-block;flex:0 0 auto;width:50px;height:12px}
.traffic-lights::before{content:"";position:absolute;left:0;top:0;width:12px;height:12px;border-\
radius:50%;background:#ff5f57;box-shadow:19px 0 #febc2e,38px 0 #28c840}
.address-bar{flex:1;min-width:0;height:28px;display:flex;align-items:center;padding:0 12px;borde\
r:1px solid var(--line);border-radius:999px;background:#fff;color:var(--muted);font-size:13px}
.window-body{min-height:180px;display:grid;place-items:center;padding:26px;color:var(--muted);ba\
ckground:linear-gradient(#fff,#f8f9f6)}
:where(a,button,[tabindex]):focus-visible{outline:3px solid #0d766f;outline-offset:3px}
""",
    "NOTES.md": """# device_frames usage notes

- Use `device-frames.css` directly; it has no dependency on devices.css or any copied
  device-frame library source.
- Set `--frame-screen-width` on each `.device-frame` to control the slot width.
- Put the app preview inside `.frame-screen`; keep overflow hidden so screenshots clip.
- Use `.iphone-ish` for a rounded frame with a dynamic-island-style slot and
  `.android-ish` for a rounded frame with a punch-hole camera.
- Use `.macos-window` or `.browser-window` when the preview should read as desktop
  software rather than a mobile app.
""",
}


_UI_KIT_DENSE_FILES: dict[str, str] = {
    "index.html": """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE_HTML__ dense UI kit</title>
<link rel="stylesheet" href="styles.css">
</head>
<body>
<div class="app-shell" data-collapsed="false">
  <aside class="sidebar-rail" aria-label="Primary">
    <button class="rail-toggle" type="button" aria-label="Collapse sidebar" aria-pressed="false"\
>=</button>
    <nav>
      <a href="#" aria-current="page"><span class="nav-icon"></span><span class="nav-label">Over\
view</span></a>
      <a href="#"><span class="nav-icon"></span><span class="nav-label">Accounts</span></a>
      <a href="#"><span class="nav-icon"></span><span class="nav-label">Reports</span></a>
    </nav>
  </aside>
  <div class="workspace">
    <header class="topbar">
      <div>
        <p class="breadcrumb">Operations / __TITLE_HTML__</p>
        <h1>Dense dashboard shell</h1>
      </div>
      <button class="cmdk-trigger" type="button">Search</button>
    </header>
    <main class="dashboard-grid">
      <section class="kpi-card metric">
        <p class="kpi-label">Qualified pipeline</p>
        <strong class="kpi-value">$428,600</strong>
        <span class="delta-badge" data-direction="up">12.4%</span>
      </section>
      <section class="kpi-card metric" data-lower-is-better="true">
        <p class="kpi-label">Cycle time</p>
        <strong class="kpi-value">18.2d</strong>
        <span class="delta-badge" data-direction="down">4.1%</span>
      </section>
      <section class="table-panel">
        <div class="panel-heading">
          <h2>Accounts</h2>
          <button type="button">Export</button>
        </div>
        <table>
          <thead>
            <tr><th>Account</th><th>Status</th><th class="numeric">ARR</th><th class="numeric">S\
core</th></tr>
          </thead>
          <tbody>
            <tr><td>Northstar Labs</td><td><span class="status">Active</span></td><td class="num\
eric">$84,200</td><td class="numeric">92</td></tr>
            <tr><td>Harbor Works</td><td><span class="status warn">Review</span></td><td class="\
numeric">$51,900</td><td class="numeric">76</td></tr>
            <tr class="skeleton-row" aria-hidden="true"><td colspan="4"><span></span></td></tr>
            <tr class="skeleton-row" aria-hidden="true"><td colspan="4"><span></span></td></tr>
          </tbody>
        </table>
      </section>
    </main>
  </div>
</div>
<script src="app.js"></script>
</body>
</html>
""",
    "styles.css": """:root{--font-base:13px;--row-height:32px;--topbar-height:56px;--rail-collap\
sed:64px;--rail-expanded:256px;--sidebar-width:var(--rail-expanded);--inline-gap:8px;--card-padd\
ing:14px;--radius:6px;--line:rgba(24,31,28,.08);--ink:#17201d;--muted:#65716c;--bg:#f6f7f4;--pan\
el:#ffffff;--active:#dff3ec;--good:#13795b;--bad:#b42318;--neutral:#687076}
*{box-sizing:border-box}
html{font-size:var(--font-base);font-variant-numeric:tabular-nums;background:var(--bg)}
body{margin:0;color:var(--ink);background:var(--bg);font-family:ui-sans-serif,system-ui,-apple-s\
ystem,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;line-height:1.25}
button,a{font:inherit}
:where(button,a,[role="button"],input,select,textarea):focus-visible{outline:3px solid #2aa67c;o\
utline-offset:2px}
button{min-height:32px;border:1px solid var(--line);border-radius:var(--radius);background:#fff;\
color:var(--ink);padding:0 10px}
@media (hover:hover){button:hover,a:hover{background:rgba(19,121,91,.08)}}
button:active,a:active{transform:translateY(1px)}
.app-shell{min-height:100svh;display:grid;grid-template-columns:var(--sidebar-width) minmax(0,1f\
r);transition:grid-template-columns .16s ease}
.app-shell[data-collapsed="true"]{--sidebar-width:var(--rail-collapsed)}
.sidebar-rail{position:sticky;top:0;height:100svh;display:grid;grid-template-rows:var(--topbar-h\
eight) 1fr;gap:12px;padding:12px 10px;border-right:1px solid var(--line);background:#fbfcf9}
.rail-toggle{width:40px;justify-self:end}
nav{display:grid;gap:4px;align-content:start}
nav a{height:32px;display:grid;grid-template-columns:24px minmax(0,1fr);align-items:center;gap:8\
px;padding:0 10px;border-radius:6px;color:var(--muted);text-decoration:none;white-space:nowrap;o\
verflow:hidden}
nav a[aria-current="page"]{background:var(--active);color:var(--good);font-weight:700}
.app-shell[data-collapsed="true"] .nav-label{position:absolute;width:1px;height:1px;overflow:hid\
den;clip:rect(0 0 0 0)}
.nav-icon{width:18px;height:18px;border-radius:5px;background:currentColor;opacity:.52}
.workspace{min-width:0}
.topbar{height:var(--topbar-height);display:flex;align-items:center;justify-content:space-betwee\
n;gap:16px;padding:0 18px;border-bottom:1px solid var(--line);background:rgba(255,255,255,.9);ba\
ckdrop-filter:blur(12px);position:sticky;top:0;z-index:5}
.breadcrumb{margin:0 0 2px;color:var(--muted);font-size:12px}
h1,h2,p{margin:0;letter-spacing:0}
h1{font-size:16px;line-height:1.2}.cmdk-trigger{min-width:148px;text-align:left;color:var(--muted)}
.dashboard-grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:12px;padding:16px}
.kpi-card,.table-panel{border:1px solid var(--line);border-radius:8px;background:var(--panel);pa\
dding:var(--card-padding)}
.kpi-card{grid-column:span 3;display:grid;gap:8px;min-height:112px}
.kpi-label{color:var(--muted)}
.kpi-value{font-size:24px;line-height:1;font-variant-numeric:tabular-nums}
.delta-badge{width:max-content;display:inline-flex;align-items:center;gap:4px;border-radius:999p\
x;padding:2px 8px;font-weight:800;background:rgba(104,112,118,.12);color:var(--neutral)}
.delta-badge::before{content:"-"}
.delta-badge[data-direction="up"]{background:rgba(19,121,91,.12);color:var(--good)}
.delta-badge[data-direction="up"]::before{content:"up"}
.delta-badge[data-direction="down"]{background:rgba(180,35,24,.1);color:var(--bad)}
.delta-badge[data-direction="down"]::before{content:"down"}
.metric[data-lower-is-better="true"] .delta-badge[data-direction="down"]{background:rgba(19,121,\
91,.12);color:var(--good)}
.metric[data-lower-is-better="true"] .delta-badge[data-direction="up"]{background:rgba(180,35,24\
,.1);color:var(--bad)}
.table-panel{grid-column:1/-1;padding:0;overflow:auto}
.panel-heading{height:48px;display:flex;align-items:center;justify-content:space-between;padding\
:0 14px;border-bottom:1px solid var(--line)}
table{width:100%;border-collapse:separate;border-spacing:0}
th,td{height:var(--row-height);padding:0 14px;border-bottom:1px solid var(--line);text-align:lef\
t;white-space:nowrap}
thead th{position:sticky;top:0;z-index:2;background:#fbfcf9;color:var(--muted);font-size:12px;fo\
nt-weight:800}
.numeric{text-align:right;font-variant-numeric:tabular-nums}
.status{display:inline-flex;align-items:center;height:22px;border-radius:999px;padding:0 8px;bac\
kground:rgba(19,121,91,.12);color:var(--good);font-weight:700}
.status.warn{background:rgba(245,158,11,.16);color:#8a5700}
.skeleton-row span{display:block;height:14px;border-radius:999px;background:linear-gradient(90de\
g,#eef0eb,#f7f8f5,#eef0eb);background-size:240% 100%;animation:skeleton 1.1s linear infinite}
@keyframes skeleton{to{background-position:-240% 0}}
@media (max-width:900px){.kpi-card{grid-column:span 6}.cmdk-trigger{min-width:96px}}
@media (max-width:720px){.app-shell{grid-template-columns:var(--rail-collapsed) minmax(0,1fr)}.k\
pi-card{grid-column:1/-1}.dashboard-grid{padding:12px;gap:10px}}
""",
    "app.js": """const shell = document.querySelector(".app-shell");
const toggle = document.querySelector(".rail-toggle");

function setCollapsed(collapsed) {
  shell.dataset.collapsed = String(collapsed);
  toggle.setAttribute("aria-pressed", String(collapsed));
  toggle.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
}

toggle.addEventListener("click", () => {
  setCollapsed(shell.dataset.collapsed !== "true");
});

addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "b") {
    event.preventDefault();
    setCollapsed(shell.dataset.collapsed !== "true");
  }
});
""",
    "NOTES.md": """# ui_kit_dense usage notes

- This is a dense operational shell, not a marketing page. Start in `index.html` and
  keep workflows on the first screen.
- H11 density tokens are in `:root`: 13px base, 32px rows, 56px topbar,
  64px/256px sidebar rail, 8px inline gaps, tabular numbers, and tint hover states.
- The sidebar collapse is wired in `app.js` and also supports Ctrl+B or Command-B.
- Delta badges are direction-aware. For lower-is-better metrics, add
  `data-lower-is-better="true"` on the `.metric` container to flip red/green.
- Table headers are sticky with an explicit background, and skeleton rows are inline
  placeholders rather than blocking overlays.
""",
}
