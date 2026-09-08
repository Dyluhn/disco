"""Durable same-directory replacement for research artifacts."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from disco.core.env import disco_env


def research_data_dir() -> Path:
    configured = disco_env("DATA_DIR")
    if configured:
        return Path(configured)
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "disco"


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Readers see the old complete file or the new complete file after a crash."""
    new_directories: list[Path] = []
    parent = path.parent
    while not parent.exists():
        new_directories.append(parent)
        parent = parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        for parent in dict.fromkeys([path.parent, *(item.parent for item in new_directories)]):
            directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
