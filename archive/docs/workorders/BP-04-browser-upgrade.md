# BP-04 — Real rendering browser with the Manus return shape

**Read `README.md` first. Requires BP-01 (daemon runs in a session). BP-10 is NOT
required (the daemon is reached via in-sandbox curl, not a published port).**

## Why

The current `browser` tool (`builtin/browser.py`) is curl + an HTML quarantine parser. It
cannot execute JS, see console errors, or screenshot — so the agent cannot verify a built
app the way Manus does (screenshot + indexed elements + extracted text + console). This
order upgrades the engine to a real browser **while preserving the file's two structural
invariants verbatim**:

1. **Containment**: the browser runs INSIDE the sandbox (browser.py docstring §1 — "a
   browser exploit is contained"). Do NOT move it host-side.
2. **Quarantine**: everything from a page reaches the model as FENCED UNTRUSTED DATA via
   a deterministic parser; acting (click/fill/submit) stays a separate, analyzer-scored
   agent decision (docstring §2–4). Keep `_FENCE_OPEN`/`_FENCE_CLOSE` and the
   ObservationEvent/role=tool channel exactly as-is.

Production provenance: in-sandbox Playwright is what Manus (browser in their VM),
OpenHands (browsergym in the runtime container), and E2B ship. A *persistent* browser
(cookies/state across calls) is the production norm; one-shot launches are the half
measure and are forbidden here.

## The decided design

A small **browser daemon** inside the sandbox: Python stdlib `http.server` +
`playwright.sync_api`, listening on `127.0.0.1:8901` (loopback inside the sandbox — never
published). It is started lazily in tmux session `__browser` via BP-01's manager. The
tool talks to it with `curl -s -X POST 127.0.0.1:8901 -d @job.json` through `exec_shell`.
One Chromium context persists across calls → cookies, localStorage, SPA state survive.

## Implementation

### 1. Image (`deploy/sandbox/Dockerfile`)

```dockerfile
RUN pip3 install --break-system-packages playwright==1.* \
    && playwright install --with-deps chromium \
    && rm -rf /root/.cache/ms-playwright/*-mac* /var/lib/apt/lists/*
```
(Accept the image-size cost; it is the production pattern.) Rebuild + push to VM-201.
Process backend: add `playwright` to `packages/tools` optional dependency group
`browser` (`pyproject.toml`), document `uv sync --extra browser && playwright install
chromium` in the order's report; acceptance step 0 verifies it.

### 2. Daemon source: `packages/tools/src/disco/tools/builtin/_browser_daemon.py`

A single self-contained script (it is shipped into the sandbox via `write_file`, like the
egress-proxy pattern in `sandbox/gvisor.py` `_setup_filtered_egress`). Contract:

- `POST /` with JSON `{action, url?, index?, fields?, text?, full_page?}` →
  JSON response `{ok, url, title, console: [{level, text}], elements: [...],
  text: str, screenshot_path: str|null, error: str|null}`.
- Actions: `navigate` (goto url, wait `load` + 500ms settle), `screenshot` (current page),
  `click` (by element `index`), `fill` (index + text), `submit` (index — press Enter in
  the field or click the form's submit), `back`, `console_view` (returns accumulated
  console only).
- Console: accumulate `page.on("console")` and `page.on("pageerror")` since last
  navigate; cap 200 entries.
- **Element index** (the Manus shape, via `page.evaluate` of a deterministic JS walker):
  enumerate visible `a, button, input, select, textarea, [role=button], [onclick]`;
  assign 1-based indices in DOM order; store a `data-pmx-index` attribute for click
  targeting; return lines `index[:]<tag>visible text … (≤80 chars)</tag>`. Cap 120.
- **Text extraction**: `document.body.innerText` capped 4000 chars (same `_MAX_TEXT`
  budget as today).
- **Screenshot**: viewport 1280×800, PNG →
  `/workspace/.pmx/screenshots/{seq:04d}-{action}.png`; return the workspace-relative
  path. (Workspace is the bind-mounted dir, so the host/agent-server can read it — BP-15
  consumes this.)
- Single page, single context, `chromium.launch(headless=True, args=["--no-sandbox"])`
  (gVisor is the isolation boundary; document this in a comment).

### 3. Tool rewrite (`builtin/browser.py`)

Keep file, module docstring, fences, `BrowserArgs` extended:

```python
class BrowserArgs(BaseModel):
    action: Literal["navigate","screenshot","click","fill","submit","back","console_view"]
    url: str = ""
    index: int | None = None          # element index for click/fill/submit
    text: str = ""                    # fill text
    full_page: bool = False
```

`run()`: ensure daemon (if `curl -sf 127.0.0.1:8901/health` fails → `sessions.exec(
"__browser", "python3 /workspace/.pmx/_browser_daemon.py")`, write the script first via
`write_file`, wait ≤10s for health); POST the job; parse JSON; render the observation:

```
[UNTRUSTED WEB CONTENT — …]            ← existing _FENCE_OPEN
URL: …
TITLE: …
CONSOLE (3 errors, 1 warning):
  - error: Uncaught TypeError: …
ELEMENTS:
  1[:]<button>Start game</button>
  2[:]<a>Leaderboard</a>
TEXT:
…
[END UNTRUSTED WEB CONTENT]
screenshot: .pmx/screenshots/0007-navigate.png
```

`structured` payload carries the full JSON (BP-05 gate and BP-15 UI read
`structured["console"]` / `structured["screenshot_path"]` / `structured["url"]`).
Risk: navigate/screenshot/console_view LOW-MEDIUM as today; click MEDIUM; fill/submit
HIGH (unchanged analyzer expectations — check `_score_other` keywords still match).
`needs` unchanged (`NETWORK`, `DISPLAY`).

Delete the curl `_fetch` path and `_Quarantine`'s use for live fetches, but KEEP
`_quarantine`/`_fence` exported if other code/tests import them (grep; the retrieval
extract tool has its own pipeline — do not touch it).

### 4. Localhost verification works with no egress

`navigate` to `http://127.0.0.1:8000/` must work in a SEALED sandbox (it's loopback).
Add an explicit test — this is the BP-05 dependency.

## Acceptance

0. Process-backend playwright installed; image rebuilt; daemon health-checks in both.
1. **Unit**: job/response schema round-trip; observation rendering (fences present,
   console counts, element lines); daemon-restart path when health fails.
2. **Integration (process backend)**: serve a fixture page (real `http.server`, real
   file) containing a button that `console.error("boom")` on load and increments a
   counter on click. (a) `navigate` returns the error in CONSOLE, the button in ELEMENTS,
   a screenshot file that EXISTS and is a valid PNG; (b) `click` by index then
   `navigate`-less `screenshot` shows updated text in TEXT — proving state persisted in
   one daemon across calls; (c) cookie persistence: page sets a cookie; re-`navigate`;
   cookie still present (assert via page text that echoes `document.cookie`).
3. **Integration (gvisor, VM-201)**: 2(a) against `127.0.0.1:8000` served by the
   `preview` session inside the SAME sandbox — the loopback-verification path.
4. **UI surface (live, Firefox)** — `frontend/e2e/bp-04-browser.spec.ts`: a live build
   prompted to *"create index.html with a visible heading, then use the browser tool to
   navigate to http://127.0.0.1:8000/ and report what you see"*. Assert the feed shows a
   `browser` observation containing `TITLE:` and `CONSOLE`. Screenshot →
   `test-record/screenshots/bp-04/feed-browser-observation.png`, sent to user.

## Prohibitions

- No host-side browser. No one-shot per-call browser launches. No Firefox-in-sandbox
  (Chromium is the decided engine; the e2e harness's Firefox is unrelated).
- The quarantine fences and the role=tool data channel are inviolable — page text must
  never be rendered into a system/user message.
- Do not implement vision analysis here (BP-00) or feed thumbnails (BP-15).
