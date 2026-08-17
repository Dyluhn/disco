#!/usr/bin/env python3
"""Generate the architecture container/dependency diagram from the checked context registry.

This is the stable entrypoint. It delegates to the bounded helper in
``development/scripts/architecture/diagram.py`` which reads the same checked context
registry as the import gate.

    uv run python development/scripts/gen_arch_diagram.py        # write the file
    uv run python development/scripts/gen_arch_diagram.py --check # CI: fail if the file is stale
    uv run python development/scripts/gen_arch_diagram.py --root /tmp/test-repo --check
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from architecture.diagram import check_diagram, write_diagram


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the tracked diagram is stale",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="repository root (defaults to this script's parent)",
    )
    args = parser.parse_args()

    if args.check:
        result = check_diagram(args.root)
        if not result["ok"]:
            print(
                f"STALE: {result['path']} is out of date — run "
                "`uv run python development/scripts/gen_arch_diagram.py`."
            )
            return 1
        print("architecture diagram up to date.")
        return 0
    path = write_diagram(args.root)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
