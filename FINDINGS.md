# FINDINGS

## What changed

- Reordered `/api/auth/mint` in both app-server and agent-server so an allowed
  `Origin` plus a valid, unconsumed pairing token mints an admin session from any
  client address.
- Kept no-token minting loopback-only, including `AUTH_DEV_AUTO_PAIR=1`.
- Kept `/api/auth/pairing-token` loopback-only.
- Added app-server boot banner lines when `FRONTEND_ORIGINS` is configured.
- Added a minimal one-time pairing dialog in `frontend/src/api/client.ts` for
  remote browsers that cannot mint or fetch the loopback pairing token.
- Added focused backend auth coverage for remote valid token, consumed token,
  remote auto-pair denial, disallowed origins, loopback fallback behavior, and
  `FRONTEND_ORIGINS` round-trip behavior.
- Added frontend client tests for `403 loopback_required` and failed
  `/api/auth/pairing-token` auto-fetch prompting paths.

## Test results

- `PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/app-server/tests -q -k auth`
  passed: 8 tests.
- `PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/agent-server/tests -q -k auth`
  passed: 41 tests.
- `cd frontend && npx vitest run src/api/`
  passed: 5 files, 23 tests. Existing jsdom navigation warnings appeared in
  `deepResearch.test.ts`.
- `PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/app-server/tests packages/agent-server/tests -q`
  passed with warnings only.
- Extra sanity check: `cd frontend && npm run typecheck:build -- --pretty false`
  passed.

## Deviations

- The requested `pnpm install --frozen-lockfile` could not run because `pnpm` and
  `corepack` are not installed in this environment, and this frontend has
  `package-lock.json` but no `pnpm-lock.yaml`. I used `npm ci` instead. It
  completed successfully and did not change `package-lock.json`; `npm audit`
  reported 4 existing vulnerabilities.
- No existing React auth gate/modal is wired into the API client's session
  initialization path. The frontend pairing UI is therefore a small local DOM
  dialog in `client.ts`, styled with the app's existing utility classes, and the
  entered token is only used for the immediate mint request.
