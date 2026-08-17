"""Pass the explicitly configured Disclaude harness credential to the server."""

import os
import sys


_CREDENTIAL_ENV = "DISCO_OPENROUTER_API_KEY"


credential = os.environ.get(_CREDENTIAL_ENV, "")
if not credential.startswith("sk-or-"):
    print(f"{_CREDENTIAL_ENV} is required", file=sys.stderr)
    raise SystemExit(2)
print(credential, end="")
