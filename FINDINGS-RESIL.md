# Provider Resilience Findings

## Reproduction

Added focused regression coverage before implementation:

- Router + `OpenAIProvider` with `httpx.MockTransport` returning 403 then 200 failed because the first `LLMAuthError` was terminal.
- Router + `OpenAIProvider` returning 403 then 403 failed the new call-count expectation because only one request was attempted.
- Stale last-selected model at conversation create failed because the sidecar remained set and no environment note was persisted.

## Changes

- `DefaultLLMRouter.complete()` now treats `LLMAuthError` as call-scoped transient once: log a warning, sleep for the bounded auth retry delay, and retry the same provider/model once. A second auth failure is terminal and preserves the provider-raised message.
- `DefaultLLMRouter.stream_complete()` has the same one-auth-retry behavior only before any stream chunk has been yielded, matching the existing no-replay stream invariant.
- Build conversation creation now ignores a stale last-selected model, logs it, persists a visible environment note on the new conversation, falls back to `RouterConfig.default_model`, and clears the stale last-selected sidecar.
- Added regression tests for 403-then-200, 403-then-403, context-window no-retry, updated legacy auth-error expectations, and sticky missing/present/default behavior.

## Deviations / Code Reality

- `OpenAIProvider` intentionally redacts raw upstream auth bodies into safe summaries. The terminal retry path preserves the `LLMAuthError` message raised by the provider, which is the current public error surface.
- The persisted environment note is prefixed with a warning marker so the Build UI treats it as visible; the spec sentence is preserved in the message body.
- No create-time network probe was added for sticky models; only catalogue membership is checked.
- Stale sidecar clearing was implemented using the existing `set_last_selected_model(None)` path.

## Verification

Passed:

```bash
PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/core/tests packages/agent-server/tests -q
```

Warnings observed were existing dependency/test warnings, including Starlette `TestClient`, websockets deprecations, WeasyPrint deprecations, and an existing `AsyncMock` runtime warning in `test_upload_corpus.py`.
