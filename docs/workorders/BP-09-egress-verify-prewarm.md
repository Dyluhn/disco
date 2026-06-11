# BP-09 — Dependency installs: verify live, pre-warm, add the filtered profile

**Read `README.md` first. Requires BP-02 (no hidden server; image rebuild pipeline
exercised). Touches the live VM-201 host.**

## Why + the decided posture (do not re-litigate)

Verified facts (2026-06-09): the Build sandbox grants `Capability.NETWORK`
(`runtime.py` `_compose_build_loop`, the `permitted | {Capability.NETWORK}` model_copy)
→ `egress_mode()` = **"open"** (`sandbox/_container.py`), and the image ships node 22 +
npm + pip + git (`deploy/sandbox/Dockerfile`). So installs are *supposed* to work — but
the path has never been live-verified inside gVisor, and the prior egress-proxy
investigation (2026-06-07) found real gVisor/Docker DNS gotchas (dead embedded DNS on
internal networks, reach-by-IP).

**Posture decision:** Build egress STAYS "open" by default. Receipt: the browser tool's
architecture (`builtin/browser.py` docstring) makes egress a granted capability because
the agent browses the open web; the quarantine + analyzer + ConfirmRisky are the defense
layer. Switching Build to filtered-by-default would silently break public-web browsing —
that would be the half measure. The FULL fix is: (1) prove the open path works, (2) make
installs fast, (3) ship a working, tested **opt-in** filtered profile for users who want
registry-only egress and accept the browsing restriction.

## Implementation

### 1. Live verification matrix (gvisor on VM-201, open egress)

Run each inside a fresh Build-spec sandbox via `exec_shell`; record wall time + exit code
in a table in your report AND in `test-record/bp-09/install-matrix.md`:

| check | command |
|---|---|
| DNS | `getent hosts registry.npmjs.org && getent hosts pypi.org` |
| npm install | `cd /workspace && npm init -y && npm install express` |
| pip install | `pip3 install --break-system-packages requests && python3 -c "import requests; print(requests.__version__)"` |
| vite scaffold | `cd /workspace && npm create vite@latest app -- --template react-ts && cd app && npm install` |
| git clone | `git clone --depth 1 https://github.com/sindresorhus/is-up.git /tmp/x` |

If DNS fails: the documented fix from the egress investigation is explicit `--dns`
servers on container create (add `dns=["1.1.1.1", "9.9.9.9"]` to the
`client.containers.run(...)` kwargs in `gvisor.py` for the open mode) — apply it, re-run
the matrix, and note it. If something ELSE fails, STOP and report; do not patch blind.

### 2. Image pre-warm (`deploy/sandbox/Dockerfile`)

Add (keep the layer lean — no node_modules baked into the image):

```dockerfile
RUN npm install -g pnpm && pip3 install --break-system-packages uv
```

Rationale: corepack is already enabled; pnpm + uv cover the fast-path installers the
agent should prefer. Then add ONE prompt bullet (prompts.py, the BP-03 section):

```
"  • Installing dependencies works (npm/pnpm/pip/uv; network is granted). Prefer "
"pnpm and uv — they are pre-installed and fast. Watch the install in your session "
"with shell_view; do not assume it finished.\n"
```

### 3. The filtered profile (opt-in, fully wired, fully tested)

- New constant in `sandbox/base.py`:

```python
REGISTRY_EGRESS_ALLOW: frozenset[str] = frozenset({
    "registry.npmjs.org", ".npmjs.org", "pypi.org", "files.pythonhosted.org",
    "github.com", "codeload.github.com", ".githubusercontent.com",
    "deb.debian.org", "security.debian.org",
    "cdn.jsdelivr.net", "unpkg.com", "esm.sh",
    "fonts.googleapis.com", "fonts.gstatic.com",
})
```
  (Matching semantics are `SandboxSpec.egress_allowed`: exact host or leading-dot
  suffix — already implemented and pinned by the shared-semantics test.)
- `runtime.py` `_compose_build_loop`: read env `PMX_BUILD_EGRESS` — `"open"` (default,
  current behavior) | `"filtered"` → build_spec gets
  `egress_allow=REGISTRY_EGRESS_ALLOW` and does NOT add `Capability.NETWORK`
  (`egress_mode()` precedence already makes a non-empty allowlist "filtered").
- Filtered mode rides the EXISTING egress-proxy sidecar (`_setup_filtered_egress`,
  live-verified 2026-06-07) — you are configuring, not building.
- Document in the report: in filtered mode the browser tool reaches localhost + the
  allowlist only; that's the user's tradeoff.

## Acceptance

1. Matrix in §1 fully green on VM-201 (paste table). npm express install must complete
   in < 120s; if slower, investigate before accepting.
2. **Filtered profile integration (VM-201)**: `PMX_BUILD_EGRESS=filtered` sandbox —
   `pip3 install requests` SUCCEEDS; `curl -s https://example.com` FAILS (blocked);
   `curl -s http://127.0.0.1:8000` (preview session) SUCCEEDS. Paste outputs.
3. **Behavioral (live driver, gvisor)**: prompt a build that genuinely needs a dependency
   ("build an express server on :8000 that returns JSON; install express first").
   Event log shows the install running in a session, `shell_view` on it, and a working
   app. Save log → `test-record/bp-09/`.
4. **UI surface (live, Firefox)** — `bp-09-install.spec.ts`: the step-3 run via the UI;
   Terminal tab shows live `npm install`/`pnpm add` output (BP-14 if landed, else feed
   observation); Preview renders the JSON. Screenshot →
   `test-record/screenshots/bp-09/install-and-serve.png`, sent to user.

## Prohibitions

- Do not flip the default to filtered. Do not remove `Capability.NETWORK` from the open
  path. Do not bake `node_modules`/template caches into the image.
- No new proxy code — reuse the existing sidecar.
