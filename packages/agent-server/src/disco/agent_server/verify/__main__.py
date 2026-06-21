"""``python -m disco.agent_server.verify`` entry point.

Preserved when the flat ``verify.py`` module became the ``verify/`` package (W15): the
self-host setup-check CLI (``disco-verify`` / ``python -m disco.agent_server.verify``) still
runs the same ``main()``.
"""

from __future__ import annotations

from ._setup_checks import main

if __name__ == "__main__":
    main()
