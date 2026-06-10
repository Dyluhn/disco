# BP-09 Report — Dependency installs & Filtered Profile

## 1. Live verification matrix (VM-201, gVisor, open egress)

The following matrix was run inside a fresh Build-spec sandbox on VM-201. All checks passed.

| check | command | exit code | time |
|---|---|---|---|
| DNS | `getent hosts registry.npmjs.org && getent hosts pypi.org` | 0 | 1.10s |
| npm install | `cd /workspace && npm init -y && npm install express` | 0 | 5.44s |
| pip install | `pip3 install --break-system-packages requests && python3 -c "import requests; print(requests.__version__)"` | 0 | 1.55s |
| vite scaffold | `cd /workspace && npm create vite@latest app -- --template react-ts && cd app && npm install` | 0 | 25.23s |
| git clone | `git clone --depth 1 https://github.com/sindresorhus/is-up.git /tmp/x` | 0 | 1.20s |

**Note**: DNS resolved correctly on VM-201 without the explicit `--dns` fix in `gvisor.py`.

## 2. Image pre-warm

- Modified `deploy/sandbox/Dockerfile` to include `pnpm` and `uv`.
- Rebuilt image `pmx-sandbox:base` on VM-201.
- Verified `pnpm --version` (11.5.3) and `uv --version` (0.11.19) inside a fresh container.
- **Pending**: The `prompts.py` bullet is skipped as it's owned by the orchestrator (BP-11 collision).

## 3. Filtered profile (opt-in)

- Added `REGISTRY_EGRESS_ALLOW` to `packages/tools/src/perpleximanus/tools/sandbox/base.py`.
- Exported `REGISTRY_EGRESS_ALLOW` via `perpleximanus.tools` and `perpleximanus.tools.sandbox`.
- Updated `packages/agent-server/src/perpleximanus/agent_server/runtime.py`'s `_compose_build_loop` to check `PMX_BUILD_EGRESS`.
  - `PMX_BUILD_EGRESS=filtered` -> uses `REGISTRY_EGRESS_ALLOW` and denies full network capability.
  - Default stays `open` (full network capability granted).

## 4. Acceptance Verification

### Filtered Profile Integration (VM-201)
Output from `verify_filtered.py` (saved to `test-record/bp-09/filtered-profile.log`):
- `pip3 install requests`: SUCCEEDED (exit 0).
- `curl -s https://example.com`: FAILED (exit 56 - blocked by proxy).
- `curl -s http://127.0.0.1:8000`: SUCCEEDED (reached local test server).

### Unit Tests
Extended `packages/agent-server/tests/test_sandbox_config.py` with:
- `test_compose_build_loop_egress_modes`: Verifies env-flag dispatch and spec shape.
- `test_registry_egress_allow_semantics`: Verifies `egress_allowed` matching logic for the registry list.
- All tests passed (10/10 in `test_sandbox_config.py`).

### Quality Checks
- `ruff check` clean on modified files.
- Added `python-multipart` to `packages/agent-server/pyproject.toml` to fix pre-existing `RuntimeError` in `app.py` during tests.

## Deviations
- **DNS Fix**: Not applied to `gvisor.py` as DNS was working natively on VM-201.
- **Prompts**: Not modified per brief instruction (orchestrator ownership).
- **Multipart**: Added missing dependency `python-multipart` to `packages/agent-server` to unblock tests.
