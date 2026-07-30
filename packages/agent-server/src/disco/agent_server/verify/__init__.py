"""disco-verify — two things in one namespace:

1. **Setup verification** (original ``verify.py``): a small battery of capability
   checks run against the persisted config and live endpoints to prove a configured
   model can actually drive the agent loop (``check_config``, ``check_completion``,
   ``check_tool_calling``, ``check_grounding``, ``run_checks``).

2. **API-first scenario runner** (W15, new): drives the running app through its
   HTTP/WS API, waits for terminal state, locates deliverables, runs validators,
   and writes a redacted evidence dossier (``run_scenario``, ``Scenario``,
   ``VerifyResult``, ``AbstractVerifyClient``, ``HttpVerifyClient``).

The two sub-systems are independent; this ``__init__`` re-exports both so
``from disco.agent_server import verify; verify.check_config(...)`` and
``from disco.agent_server.verify import run_scenario`` both work.

Note: the original ``verify.py`` flat module is shadowed by this package once the
``verify/`` directory exists. Its content is preserved verbatim in
``_setup_checks.py`` and re-exported here so existing callers are unaffected.
"""

from __future__ import annotations

# ---- setup-verification (original verify.py content, preserved in _setup_checks) ---
from ._setup_checks import (  # noqa: F401  (public re-export)
    Check,
    Status,
    _build_runtime,
    _render,
    check_completion,
    check_config,
    check_grounding,
    check_tool_calling,
    main,
    run_checks,
)

# ---- API-first scenario runner (W15) ----------------------------------------
from .dispatcher import HostVerifierDispatcher  # noqa: F401
from .evidence_source import ClientEvidenceAdapter, EvidenceSource  # noqa: F401
from .host import HostWebAppVerifier  # noqa: F401
from .model_verifier import ModelVerifier  # noqa: F401
from .reliability import (  # noqa: F401
    aggregate_reliability_metrics,
    run_reliability_metrics,
)
from .runner import AbstractVerifyClient, HttpVerifyClient, run_scenario  # noqa: F401
from .schema import Scenario, VerifyResult  # noqa: F401

__all__ = [
    # setup-verification
    "Check",
    "Status",
    "check_config",
    "check_completion",
    "check_grounding",
    "check_tool_calling",
    "main",
    "run_checks",
    # W15 runner
    "AbstractVerifyClient",
    "ClientEvidenceAdapter",
    "EvidenceSource",
    "HttpVerifyClient",
    "HostVerifierDispatcher",
    "HostWebAppVerifier",
    "ModelVerifier",
    "Scenario",
    "VerifyResult",
    "aggregate_reliability_metrics",
    "run_reliability_metrics",
    "run_scenario",
]
