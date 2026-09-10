"""Entropy check for the operator-supplied session secret.

Extracted from ``auth.py`` under the same rule as the other parts here: nothing
in this module is public API — ``auth.py`` re-imports the one name it calls.
"""

from __future__ import annotations

import logging

# The SAME heuristic the secret store applies to the app secret — imported, not
# re-implemented, so the two paths can never drift on what "weak" means. It is
# private there; this is its only other caller.
from ..llm.secrets import _looks_weak

_LOG = logging.getLogger(__name__)
_weak_auth_secret_warned = False


def _warn_if_weak_auth_secret(secret: str) -> None:
    """Log ONCE per process if the operator-supplied session secret is low-entropy.

    Warn, never fail — the same contract the secret store's app-secret path
    already carries (``llm/secrets.py::_warn_if_weak_secret``): raising here
    would lock a running deployment out of its own sessions on the next restart,
    which is a worse outcome than the risk being reported. (``WeakSecretError``
    is reserved there for *deploy credentials*, which opt in to failing closed —
    a session secret is not one.) A separate latch and message from that path, so
    neither silences the other and the text names the right variable.

    Why it matters more here than for stored ciphertext: the session signer is a
    plain HMAC, so ONE captured cookie is an offline brute-force oracle — and the
    recovered secret also yields ``pairing_token()``, which is derived from it,
    i.e. full admin.
    """
    global _weak_auth_secret_warned
    if _weak_auth_secret_warned or not _looks_weak(secret):
        return
    _weak_auth_secret_warned = True
    _LOG.warning(
        "DISCO_AUTH_SECRET/DISCO_SECRET_KEY looks low-entropy (length %d). Session "
        "cookies are HMAC-signed with it, so one captured cookie makes it "
        "brute-forceable offline — and recovering it also yields the admin pairing "
        "token, which is derived from the same secret. Set a high-entropy value — "
        "e.g. `openssl rand -base64 32`.",
        len(secret),
    )
