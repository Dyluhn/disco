Captured verbatim from https://ollama.com/v1/chat/completions on 2026-09-22.
Public probe prompt: `Return exactly {"ok": true} as JSON.`
Both requests used stream=true and max_tokens=256. DeepSeek v4.1 Flash used
reasoning_effort=none; GLM 5.3 Flash used reasoning_effort=low. Both returned
HTTP 200, no reasoning deltas, and the requested JSON. No credentials or private
user inputs are included. This demonstrates acceptance on these models, not a
promise that every prompt suppresses all internal reasoning.
