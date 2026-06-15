"""Print the OpenRouter API key for the harness-respawned agent-server.

The encrypted secret store is permanently locked (PMX_SECRET_KEY lost;
OpenRouter never re-shows keys), so the runtime's PMX_OPENROUTER_API_KEY env
fallback is the live auth path. Source of truth: Pi's auth.json (free-models
key — dev traffic only). Stdout is consumed by _SERVER_SH's $(...); never log
or echo the key anywhere else.
"""
import json


def _find(o):
    if isinstance(o, dict):
        k = o.get("key")
        if isinstance(k, str) and k.startswith("sk-or-"):
            return k
        for v in o.values():
            r = _find(v)
            if r:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _find(v)
            if r:
                return r
    return None

print(_find(json.load(open("/home/dylan/.pi/agent/auth.json"))) or "")
