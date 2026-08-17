"""AppKit EPIC O — real Cloudflare provisioning, owner-only, dry-run by default;
INERT until an owner connects an API token via POST /connect (encrypted at rest).
The agent loop has no deploy tool — its ceiling is cloudflare_export_ready.

The deploy capability lives in agent-server (NOT core — ``disco.core`` stays a
leaf; it only lends the pure ``cloudflare_export_ready`` model + SecretStore).
A real deploy fires ONLY through ``deploy.execute_deploy`` with all four hard
gates satisfied (export-ready + connected account + explicit owner confirmation
+ not autonomous), and dry-run is the default. See ``deploy.py`` for the gates,
``routes.py`` for the owner API, ``wrangler.py`` for the injectable runner.
"""

from __future__ import annotations

from .models import (
    ConnectionStatus,
    DeployExecutionResult,
    DeployPlan,
    DeployRefused,
    DeployStep,
    RefusalReason,
)
from .routes import CloudflareDeployCorsMiddleware, make_cloudflare_router

__all__ = [
    "CloudflareDeployCorsMiddleware",
    "ConnectionStatus",
    "DeployExecutionResult",
    "DeployPlan",
    "DeployRefused",
    "DeployStep",
    "RefusalReason",
    "make_cloudflare_router",
]
