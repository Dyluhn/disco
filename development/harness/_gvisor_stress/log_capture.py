"""Sandbox debug-log capture for the gVisor stress harness."""

from __future__ import annotations

import logging
import time
from pathlib import Path


class SandboxLogCapture:
    """Capture every sandbox descendant logger at DEBUG beside the JSON report."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.logger = logging.getLogger("disco.tools.sandbox")
        self.handler: logging.FileHandler | None = None
        self.old_level = self.logger.level
        self.old_propagate = self.logger.propagate

    def __enter__(self) -> SandboxLogCapture:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(self.path, mode="w", encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            "%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s thread=%(threadName)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        formatter.converter = time.gmtime
        handler.setFormatter(formatter)
        self.logger.setLevel(logging.DEBUG)
        # This is a standalone process.  Keeping propagation local avoids spraying
        # the package's DEBUG evidence onto any root console handler in the image.
        self.logger.propagate = False
        self.logger.addHandler(handler)
        self.handler = handler
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.handler is not None:
            self.handler.flush()
            self.logger.removeHandler(self.handler)
            self.handler.close()
        self.logger.setLevel(self.old_level)
        self.logger.propagate = self.old_propagate
