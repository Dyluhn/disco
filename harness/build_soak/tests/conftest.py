"""Make the in-repo `harness` package importable regardless of the PYTHONPATH the
test runner was launched with (the build-soak verify command sets only the
package-src dirs, not the repo root). Insert the repo root at the front of
sys.path so `import harness.build_soak...` resolves."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
