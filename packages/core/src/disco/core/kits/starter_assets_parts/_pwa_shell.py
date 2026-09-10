"""Starter kit data table: `pwa_shell` icon constants + file contents.

Extracted from ``starter_assets`` to keep that module's facade under the
module logical-line budget. Pure data/content carry — file contents are
unchanged (the ``__TITLE_*__``/``__ICON_INITIAL_HTML__`` markers are replaced
by ``starter_assets._render`` at call time, not here).
"""

from __future__ import annotations

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
