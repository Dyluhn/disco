# BP-09 Live Verification Matrix

| check | command | exit code | time |
|---|---|---|---|
| DNS | `getent hosts registry.npmjs.org && getent hosts pypi.org` | 0 | 1.10s |
| npm install | `cd /workspace && npm init -y && npm install express` | 0 | 5.44s |
| pip install | `pip3 install --break-system-packages requests && python3 -c "import requests; print(requests.__version__)"` | 0 | 1.55s |
| vite scaffold | `cd /workspace && npm create vite@latest app -- --template react-ts && cd app && npm install` | 0 | 25.23s |
| git clone | `git clone --depth 1 https://github.com/sindresorhus/is-up.git /tmp/x` | 0 | 1.20s |

Verified on VM-201 (gVisor, open egress) on 2026-06-10.
Image `pmx-sandbox:base` rebuilt with `pnpm` and `uv` pre-warmed.
All checks passed well within the 120s limit.
No DNS fix was required for the open mode as DNS resolved correctly.
