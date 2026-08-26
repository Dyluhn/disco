"""Compatibility imports for the shared provider HTTP policy.

The implementation belongs to core so app-server can use the exact same
bounded executor without depending on the optional retrieval wheel. Existing
retrieval imports remain stable for downstream adapters and tests.
"""

from disco.core._provider_http import (
    BoundedHttpExecutor,
    HttpAttemptPolicy,
    HttpResult,
    ProviderHttpExecutor,
    ProviderOutcome,
    with_outcome,
)

__all__ = [
    "BoundedHttpExecutor",
    "HttpAttemptPolicy",
    "HttpResult",
    "ProviderHttpExecutor",
    "ProviderOutcome",
    "with_outcome",
]
