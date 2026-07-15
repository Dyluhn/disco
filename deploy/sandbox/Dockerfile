FROM debian:stable-slim
# Agent toolset: shell, python, common CLI utils. NO secrets/keys — ever.
# `python-is-python3` provides the `python` alias (Debian ships only `python3`):
# agents overwhelmingly write `python script.py` / `python -m pip`, and without it
# every such call exits 127 and the agent flails trying to recover (caught in a
# Gemma build eval). `pip` is symlinked below for the same reason.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip python-is-python3 ca-certificates curl git jq \
      ripgrep procps less nano tmux \
    && ln -sf /usr/bin/pip3 /usr/local/bin/pip \
    && rm -rf /var/lib/apt/lists/*
# Node.js 22 LTS (current LTS) via NodeSource — gives node + npm + npx + corepack,
# so the `code_exec` node path and JS/TS builds (vite/next/etc.) work in the box.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && corepack enable \
    && npm install -g npm@latest pnpm --force \
    && pip3 install --break-system-packages uv \
    && rm -rf /var/lib/apt/lists/* /root/.npm

# BP-08: Persistent IPython kernel stack. No matplotlib in the image (gVisor
# integration covers state + interrupt; plot tests run on the process backend).
RUN pip3 install --break-system-packages ipykernel jupyter_kernel_gateway

# BP-04: Real rendering browser (Playwright + Chromium).  Sandboxes execute as
# UID 1000, so the browser bundle must not land in root's private cache.  Keep
# Playwright's lookup path in the image environment and explicitly make the
# shared install traversable/readable/executable by every runtime user.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip3 install --break-system-packages playwright \
    && playwright install --with-deps chromium \
    && chromium_path="$(find "$PLAYWRIGHT_BROWSERS_PATH" -name chrome -type f -path '*chrome-linux*' -print -quit)" \
    && test -n "$chromium_path" \
    && ln -sf "$chromium_path" /usr/local/bin/playwright-chromium \
    && chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH" \
    && test -x /usr/local/bin/playwright-chromium \
    && rm -rf /var/lib/apt/lists/*

# RP-10: Marp slide-deck rendering (HTML / PDF / PPTX), run INSIDE the sandbox by
# the slides_generate tool via exec_shell — NOT on the agent-server host (the deck
# markdown is agent-generated; a full Chromium over it belongs behind the jail).
# Reuses the Playwright Chromium installed above; the stable shared symlink decouples
# CHROME_PATH from the version-pinned Playwright bundle path. Verified: marp renders
# PDF+PPTX+HTML with CHROME_NO_SANDBOX=true (Chromium's own sandbox can't nest;
# gVisor/runc is the boundary).
RUN npm install -g @marp-team/marp-cli \
    && rm -rf /root/.npm
ENV CHROME_PATH=/usr/local/bin/playwright-chromium \
    CHROME_NO_SANDBOX=true

# RP-07: DOCX export — pandoc, run INSIDE a transient render sandbox by the export
# endpoint (so DOCX works with NO host install, jailed like marp). pandoc renders
# the report's server-generated markdown to .docx in /workspace, the bytes are read
# back, the throwaway sandbox is destroyed. (PDF stays in-process — weasyprint is
# pure-Python, safe on trusted report data.)
RUN apt-get update && apt-get install -y --no-install-recommends pandoc \
    && rm -rf /var/lib/apt/lists/*

# C3: LibreOffice headless — converts native editable .pptx decks to PDF inside the
# sandbox (same jailing rationale as marp: agent-generated deck content runs only
# in the jail).  `soffice --headless --convert-to pdf --outdir . deck.pptx`.
# libreoffice-impress is the minimal Impress-only pull; fonts-liberation gives
# standard metric-compatible fonts so slide geometry doesn't shift on conversion.
# Absent soffice → C3 render_deck() returns a clear error, never a silent blank PDF.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-impress fonts-liberation \
    && rm -rf /var/lib/apt/lists/*


# Build toolchain + common CLIs build agents reach for. `build-essential` gives
# gcc/g++/make/libc-dev — without it `make`-based builds exit 127 and C-extension
# pip installs (any dependency with no prebuilt wheel) fail to compile, both of
# which make a build agent flail (same class as the missing python alias / pytest).
# unzip + sqlite3 are frequent (archive extraction, ad-hoc DB inspection). lsof is
# a general port/process debugging staple (live_view.py's port probe no longer
# needs it — it bind-probes via stdlib — but agents debugging "address in use"
# reach for it constantly). Late layer = cheap, keeps the expensive
# playwright/chromium layer cached.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential unzip sqlite3 lsof \
    && rm -rf /var/lib/apt/lists/*

# noVNC live-browser stack (E6 / noVNC P1). Packages ONLY — no services started in
# the image (the lazy Xvfb+x11vnc+websockify daemon is P2; the agent-server route +
# the NOVNC_PORT in tools/sandbox/_container.py USER_PORTS are P3). xvfb = headless X
# display for a headed browser; x11vnc = VNC server exposing that display; novnc +
# websockify = the browser-side noVNC client + the WebSocket↔VNC bridge. Late layer
# keeps the expensive playwright/chromium layer cached.
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/*

# Pre-install the common Python dev tools build agents reach for (pytest, ruff)
# so a build session does not burn iterations rediscovering
# `pip install --break-system-packages`. (placed late = cheap layer, keeps the
# expensive playwright/chromium layer cached.)
RUN pip3 install --break-system-packages pytest ruff
# Non-root user available (gVisor is the isolation boundary; app may pick --user)
RUN useradd -m -u 1000 -s /bin/bash agent && mkdir -p /workspace && chown agent:agent /workspace
WORKDIR /workspace
CMD ["/bin/bash"]
