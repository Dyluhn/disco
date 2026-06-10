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

# Non-root user available (gVisor is the isolation boundary; app may pick --user)
RUN useradd -m -u 1000 -s /bin/bash agent && mkdir -p /workspace && chown agent:agent /workspace
WORKDIR /workspace
CMD ["/bin/bash"]
