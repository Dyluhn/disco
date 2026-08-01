"""Starter kit data table: `ui_kit_dense` file contents.

Extracted from ``starter_assets`` to keep that module's facade under the
module logical-line budget. Pure data/content carry — file contents are
unchanged (the ``__TITLE_*__``/``__ICON_INITIAL_HTML__`` markers are replaced
by ``starter_assets._render`` at call time, not here).
"""

from __future__ import annotations

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
