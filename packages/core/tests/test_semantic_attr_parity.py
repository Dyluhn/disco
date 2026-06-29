"""P8C drift guard: the TS mirror (frontend/src/lib/discoSemanticAttrs.ts) must carry
EXACTLY the data-disco-* attribute values of the canonical Python DataDiscoAttr (P8B).
TS can't import Python, so this test enforces parity across the language boundary."""

from __future__ import annotations

import re
from pathlib import Path

from disco.core.appkit.semantic_metadata import DataDiscoAttr

_TS = Path(__file__).resolve().parents[3] / "frontend" / "src" / "lib" / "discoSemanticAttrs.ts"


def test_ts_mirror_matches_python_canonical_exactly() -> None:
    assert _TS.is_file(), f"missing TS mirror at {_TS}"
    ts_values = set(re.findall(r'"(data-disco-[a-z-]+)"', _TS.read_text(encoding="utf-8")))
    py_values = {a.value for a in DataDiscoAttr}
    assert ts_values == py_values, (
        f"data-disco-* attr drift — only in TS: {ts_values - py_values}; "
        f"only in Python: {py_values - ts_values}"
    )
