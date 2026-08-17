"""Starter kit data table: `game_loop_vanilla` file contents.

Extracted from ``starter_assets`` to keep that module's facade under the
module logical-line budget. Pure data/content carry — file contents are
unchanged (the ``__TITLE_*__``/``__ICON_INITIAL_HTML__`` markers are replaced
by ``starter_assets._render`` at call time, not here).
"""

from __future__ import annotations

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
