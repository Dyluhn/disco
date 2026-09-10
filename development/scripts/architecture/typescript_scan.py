"""TypeScript/TSX structure scanner for the architecture budget gate.

Uses the locally pinned TypeScript 5.9.3 compiler from
``frontend/node_modules/typescript``. Fails closed if the compiler is absent or
mismatched.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
TS_COMPILER_PATH = REPO_ROOT / "frontend" / "node_modules" / "typescript"
TS_PACKAGE_JSON = REPO_ROOT / "frontend" / "node_modules" / "typescript" / "package.json"
REQUIRED_TS_VERSION = "5.9.3"


def check_compiler() -> str:
    """Verify the pinned TypeScript compiler is present and matches 5.9.3.

    Returns the version string. Raises if absent or mismatched.
    """
    if not TS_PACKAGE_JSON.is_file():
        raise FileNotFoundError(
            f"TypeScript compiler not found at {TS_COMPILER_PATH}. "
            f"Run `npm ci` in frontend/ to install the pinned compiler."
        )
    pkg = json.loads(TS_PACKAGE_JSON.read_text(encoding="utf-8"))
    version = pkg.get("version", "")
    if version != REQUIRED_TS_VERSION:
        raise RuntimeError(
            f"TypeScript compiler version mismatch: expected {REQUIRED_TS_VERSION}, "
            f"got {version}. The production gate must use the pinned compiler."
        )
    return version


def scan_typescript(root: Path | None = None) -> dict[str, Any]:
    """Scan all tracked TypeScript files using the pinned compiler.

    Delegates to the Node.js scanner script which uses the TypeScript compiler
    API directly. Fails closed on compiler absence/mismatch or parse errors.
    """
    if root is None:
        root = REPO_ROOT
    version = check_compiler()

    # Run the Node scanner that uses the TypeScript compiler API.
    scanner_script = REPO_ROOT / "development" / "scripts" / "architecture" / "ts_scan.mjs"
    if not scanner_script.is_file():
        raise FileNotFoundError(f"TypeScript scanner script missing: {scanner_script}")

    result = subprocess.run(
        [
            "node",
            str(scanner_script),
            "--repo",
            str(root),
            "--compiler",
            str(TS_COMPILER_PATH),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    payload["compiler_version"] = version
    return payload
