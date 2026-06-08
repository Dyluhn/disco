"""The cassette — a verbatim, on-disk recording of real service responses, keyed
by the SEMANTIC inputs of each call so replay matches deterministically.

A cassette is a jsonl file; each line is one recorded interaction:
    {"seam": "search", "key": "<sha16>", "input": {...}, "output": <serialized>}

`key` is a stable hash of the call's semantic inputs (query, url, message
content, …) — deliberately EXCLUDING nondeterministic fields like a request_id
or a timestamp, so the same logical call replays the same response.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def cassette_key(seam: str, payload: dict[str, Any]) -> str:
    """Stable 16-hex key from a seam name + its semantic-input payload. `default=str`
    so any stray non-JSON value still hashes (it just won't collide-protect it)."""
    canon = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(f"{seam}:{canon}".encode()).hexdigest()[:16]


class CassetteMiss(KeyError):
    """No recorded interaction matches this call — the cassette needs (re)recording."""


class Cassette:
    """A loaded recording. `record()` appends; `lookup()` fetches by (seam, key).
    Recording is append-only in memory; `save()` writes the jsonl."""

    def __init__(self) -> None:
        # (seam, key) -> output (already-serialized JSON value)
        self._entries: dict[tuple[str, str], Any] = {}
        # ordered log for save() + human inspection
        self._log: list[dict[str, Any]] = []

    # ---- io -----------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> Cassette:
        c = cls()
        p = Path(path)
        if not p.exists():
            return c
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            c._entries[(row["seam"], row["key"])] = row["output"]
            c._log.append(row)
        return c

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            for row in self._log:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # ---- record / replay ----------------------------------------------------

    def record(self, seam: str, payload: dict[str, Any], output: Any) -> None:
        key = cassette_key(seam, payload)
        if (seam, key) in self._entries:
            return  # idempotent — first real response for a call wins
        self._entries[(seam, key)] = output
        self._log.append({"seam": seam, "key": key, "input": payload, "output": output})

    def lookup(self, seam: str, payload: dict[str, Any]) -> Any:
        key = cassette_key(seam, payload)
        try:
            return self._entries[(seam, key)]
        except KeyError as e:
            raise CassetteMiss(
                f"cassette miss: {seam} key={key} input={payload!r:.200}"
            ) from e

    def __len__(self) -> int:
        return len(self._log)

    def seams(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self._log:
            out[row["seam"]] = out.get(row["seam"], 0) + 1
        return out
