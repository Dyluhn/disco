#!/usr/bin/env python3
"""Operate the bounded SecretStore key-rotation state machine.

Key material is accepted only through the normal SecretStore environment:
``DISCO_SECRET_KEY`` + ``DISCO_SECRET_KEY_ID`` select the active write key and
``DISCO_SECRET_READ_KEYS`` is a JSON object containing at most four old read
keys. This command never prints key material, ciphertext, secret names or values.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from disco.core.llm import SecretStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("status", "migrate", "resume", "verify", "finalize", "rollback"),
    )
    parser.add_argument("--path", type=Path, help="encrypted store path; defaults to DISCO_SECRETS")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        store = SecretStore(args.path)
        result: dict[str, object]
        succeeded = True
        if args.action == "status":
            result = {
                "can_store": store.can_store,
                "locked": store.locked,
                "record_count": len(store.secret_names()),
                "rotation_pending": store.rotation.pending,
            }
        elif args.action in {"migrate", "resume"}:
            result = {
                "record_count": store.rotation.reencrypt_to_active(),
                "rotation_pending": store.rotation.pending,
            }
        elif args.action == "verify":
            succeeded = store.rotation.verify()
            result = {"verified": succeeded, "rotation_pending": store.rotation.pending}
        elif args.action == "finalize":
            succeeded = store.rotation.finalize()
            result = {
                "finalized": succeeded,
                "rotation_pending": store.rotation.pending,
            }
        else:
            succeeded = store.rotation.rollback()
            result = {
                "rolled_back": succeeded,
                "rotation_pending": store.rotation.pending,
            }
    except Exception as exc:
        # This is an operator boundary: internal exception text can contain a
        # record name. Emit only the non-sensitive exception class, never a
        # traceback or message assembled from encrypted-store contents.
        print(
            json.dumps({"error": type(exc).__name__, "ok": False}, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
