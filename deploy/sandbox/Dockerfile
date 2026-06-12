FROM debian:stable-slim
# Agent toolset: shell, python, common CLI utils. NO secrets/keys — ever.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip ca-certificates curl git jq \
      ripgrep procps less nano tmux \
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

# BP-04: Real rendering browser (Playwright + Chromium)
RUN pip3 install --break-system-packages playwright \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# RP-10: Marp slide-deck rendering (HTML / PDF / PPTX), run INSIDE the sandbox by
# the slides_generate tool via exec_shell — NOT on the agent-server host (the deck
# markdown is agent-generated; a full Chromium over it belongs behind the jail).
# Reuses the Playwright Chromium installed above; a stable symlink decouples
# CHROME_PATH from the version-pinned playwright cache path. Verified: marp renders
# PDF+PPTX+HTML with CHROME_NO_SANDBOX=true (Chromium's own sandbox can't nest;
# gVisor/runc is the boundary).
RUN npm install -g @marp-team/marp-cli \
    && ln -sf "$(find /root/.cache/ms-playwright -name chrome -type f -path '*chrome-linux*' | head -1)" /usr/local/bin/marp-chromium \
    && rm -rf /root/.npm
ENV CHROME_PATH=/usr/local/bin/marp-chromium \
    CHROME_NO_SANDBOX=true

# RP-07: DOCX export — pandoc, run INSIDE a transient render sandbox by the export
# endpoint (so DOCX works with NO host install, jailed like marp). pandoc renders
# the report's server-generated markdown to .docx in /workspace, the bytes are read
# back, the throwaway sandbox is destroyed. (PDF stays in-process — weasyprint is
# pure-Python, safe on trusted report data.)
RUN apt-get update && apt-get install -y --no-install-recommends pandoc \
    && rm -rf /var/lib/apt/lists/*

# Non-root user available (gVisor is the isolation boundary; app may pick --user)
RUN useradd -m -u 1000 -s /bin/bash agent && mkdir -p /workspace && chown agent:agent /workspace
WORKDIR /workspace
CMD ["/bin/bash"]
