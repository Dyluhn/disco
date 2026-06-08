FROM debian:stable-slim
# Agent toolset: shell, python, common CLI utils. NO secrets/keys — ever.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip ca-certificates curl git jq \
      ripgrep procps less nano \
    && rm -rf /var/lib/apt/lists/*
# Node.js 22 LTS (current LTS) via NodeSource — gives node + npm + npx + corepack,
# so the `code_exec` node path and JS/TS builds (vite/next/etc.) work in the box.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && corepack enable \
    && npm install -g npm@latest \
    && rm -rf /var/lib/apt/lists/* /root/.npm
# Non-root user available (gVisor is the isolation boundary; app may pick --user)
RUN useradd -m -u 1000 -s /bin/bash agent && mkdir -p /workspace && chown agent:agent /workspace
WORKDIR /workspace
CMD ["/bin/bash"]
