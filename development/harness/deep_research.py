"""Compatibility entrypoint for the Deep Research observation harness.

The implementation lives in :mod:`harness.research_harness`; this short name
keeps invocation discoverable beside the existing harness modules.
"""

from .research_harness import *  # noqa: F403
from .research_harness import main as _main

if __name__ == "__main__":
    raise SystemExit(_main())
