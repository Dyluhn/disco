"""Starter kit data table: `device_frames` file contents.

Extracted from ``starter_assets`` to keep that module's facade under the
module logical-line budget. Pure data/content carry — file contents are
unchanged (the ``__TITLE_*__``/``__ICON_INITIAL_HTML__`` markers are replaced
by ``starter_assets._render`` at call time, not here).
"""

from __future__ import annotations

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
