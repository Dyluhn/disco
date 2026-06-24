"""Live-surface adapters for the Build Soak runner.

These drive the REAL product surfaces (the agent-server HTTP/WS API; later the
Playwright UI). They are the ONLY harness modules permitted to import an HTTP/WS
client — never `disco.*`. The deterministic oracle/classifier path stays
dependency-free (guidelines §10).
"""
