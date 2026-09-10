"""Print this install's admin pairing token on demand.

The boot banner prints the token once, and healthcheck lines push it out of
`compose logs app-server | tail` within minutes. The token is not minted per
boot: it is an HMAC tag over the install secret (see
``disco.core.auth.pairing_token``), so any process that can read the secret can
recompute it. Run it where the secret lives — inside the app-server container:

    podman compose exec app-server python -m disco.app_server.pairing_cli

Deliberately a module entry point rather than a ``[project.scripts]`` console
script: every packages/*/pyproject.toml is a pinned contract file in
``development/architecture/public-api.json``, and re-pinning one needs a
single-use governance authority that is already spent. ``python -m`` ships the
same command with no packaging-contract change.
"""

from __future__ import annotations

from disco.core.auth import pairing_token

from .auth import public_ui_url


def main() -> int:
    print(f"UI: {public_ui_url()}")
    print(f"Pairing token: {pairing_token()}")
    print("Paste it if the browser asks to pair; it is valid until the install secret changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
