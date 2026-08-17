"""Frozen resolved-disposition ID sets for accepted packages PKG-02..PKG-09.

These sets are immutable history: once a package is epic-accepted its IDs never
change. They were lifted verbatim out of ``generate_debt.py`` (lines 51-233 at
``8242a597``) with no edit to any set.

Epic 10's sub-epic sets already live in ``debt_resolved_epic10.py`` for the same
reason and are imported here as one aggregate, so the union stays in one place.
Epic 11 follows that precedent with ``debt_resolved_epic11.py``.
"""

from __future__ import annotations

try:
    from ..debt_resolved_epic10 import EPIC10_RESOLVED_IDS
    from ..debt_resolved_epic11 import EPIC11_RESOLVED_IDS
    from ..debt_resolved_epic12 import EPIC12_RESOLVED_IDS
    from ..debt_resolved_pkg19 import PKG19_CERT_SIGNOFF_RESOLVED_IDS
except ImportError:
    from debt_resolved_epic10 import EPIC10_RESOLVED_IDS
    from debt_resolved_epic11 import EPIC11_RESOLVED_IDS
    from debt_resolved_epic12 import EPIC12_RESOLVED_IDS
    from debt_resolved_pkg19 import PKG19_CERT_SIGNOFF_RESOLVED_IDS

PKG02_RESOLVED_IDS = frozenset({"PY-0890", "PY-0891", "DM-010"})
PKG03_HARNESS_TRANSPORT_RESOLVED_IDS = frozenset(
    {
        *(f"PY-{number:04d}" for number in range(1, 33)),
        *(f"PY-{number:04d}" for number in range(91, 115)),
        *(f"PY-{number:04d}" for number in range(117, 152)),
        *(f"PY-{number:04d}" for number in range(154, 163)),
        "PY-0896",
    }
)
PKG03_HARNESS_ORACLES_RESOLVED_IDS = frozenset(
    {
        *(f"PY-{number:04d}" for number in range(33, 91)),
        "PY-0152",
        "PY-0153",
        "DM-006",
    }
)
PKG03_HARNESS_TESTS_RESOLVED_IDS = frozenset({"PY-0115", "PY-0116"})
PKG04_EVENTS_RESOLVED_IDS = frozenset(
    {
        *(f"PY-{number:04d}" for number in range(444, 455)),
        "PY-0663",
    }
)
PKG04_STORES_RESOLVED_IDS = frozenset(
    {
        "DM-014",
        "DM-020",
        *(f"PY-{number:04d}" for number in range(664, 670)),
    }
)
PKG04_IDENTITY_RESOLVED_IDS = frozenset({"PY-0661"})
PKG05_CONTEXT_RESOLVED_IDS = frozenset(
    {
        "PY-0201",
        "PY-0435",
        "PY-0436",
        "PY-0437",
        "PY-0461",
        "PY-0462",
        "PY-0463",
        "PY-0464",
        "PY-0465",
        "PY-0466",
        "PY-0467",
        "PY-0468",
        "PY-0469",
        "PY-0470",
        "PY-0471",
        "PY-0472",
        "PY-0474",
        "PY-0475",
        "PY-0662",
        "PY-0692",
        "PY-0693",
        "PY-0694",
        "PY-0695",
        "PY-0696",
        "PY-0697",
        "PY-0698",
        "PY-0703",
    }
)
PKG05_LOOP_RESOLVED_IDS = frozenset(
    {
        *(f"PY-{number:04d}" for number in range(476, 504)),
        *(f"PY-{number:04d}" for number in range(556, 632)),
        *(f"PY-{number:04d}" for number in range(652, 661)),
        "PY-0702",
    }
)
PKG06_LIFECYCLE_RESOLVED_IDS = frozenset(
    {
        "DM-016",
        "PY-0191",
        "PY-0220",
        "PY-0221",
        "PY-0244",
        "PY-0245",
        "PY-0246",
        "PY-0247",
    }
)
PKG06_RUNTIME_RESOLVED_IDS = frozenset(
    {
        "DM-001",
        "PY-0166",
        "PY-0183",
        "PY-0184",
        "PY-0185",
        "PY-0188",
        "PY-0189",
        "PY-0190",
        "PY-0193",
        "PY-0239",
    }
) | frozenset(
    {
        *(f"PY-{number:04d}" for number in range(248, 251)),
        *(f"PY-{number:04d}" for number in range(300, 325)),
        "PY-0898",
    }
)
PKG07_WORKSPACE_RESOLVED_IDS = frozenset(
    {
        "DM-002",
        *(f"PY-{number:04d}" for number in range(252, 257)),
        *(f"PY-{number:04d}" for number in range(281, 287)),
        *(f"PY-{number:04d}" for number in range(294, 298)),
        *(f"PY-{number:04d}" for number in range(346, 360)),
        "PY-0361",
    }
)
PKG07_PREVIEW_RESOLVED_IDS = frozenset(
    {
        "DM-003",
        "DM-012",
        *(f"PY-{number:04d}" for number in range(225, 239)),
        *(f"PY-{number:04d}" for number in range(258, 275)),
        "PY-0362",
        "PY-0363",
    }
)
PKG08_VERIFY_RESOLVED_IDS = frozenset(
    {
        "DM-013",
        "PY-0163",
        "PY-0164",
        "PY-0165",
        *(f"PY-{number:04d}" for number in range(329, 346)),
        "PY-0438",
        *(f"PY-{number:04d}" for number in range(440, 444)),
        "PY-0455",
        "PY-0675",
        "PY-0676",
        *(f"PY-{number:04d}" for number in range(679, 692)),
        *(f"PY-{number:04d}" for number in range(805, 809)),
        *(f"PY-{number:04d}" for number in range(882, 887)),
    }
)
PKG08_FINISH_RESOLVED_IDS = frozenset(
    {
        *(f"PY-{number:04d}" for number in range(504, 556)),
        "PY-0701",
    }
)
PKG09_RELEASE_RESOLVED_IDS = frozenset(
    {
        *(f"PY-{number:04d}" for number in range(634, 652)),
        *(f"PY-{number:04d}" for number in range(893, 896)),
        "PY-0897",
        *(f"PY-{number:04d}" for number in range(899, 908)),
    }
)
PKG09_CONNECTORS_RESOLVED_IDS = frozenset(
    {
        *(f"PY-{number:04d}" for number in range(167, 183)),
        "PY-0287",
        "PY-0288",
        "PY-0360",
    }
)
RESOLVED_IDS = (
    PKG02_RESOLVED_IDS
    | PKG03_HARNESS_TRANSPORT_RESOLVED_IDS
    | PKG03_HARNESS_ORACLES_RESOLVED_IDS
    | PKG03_HARNESS_TESTS_RESOLVED_IDS
    | PKG04_EVENTS_RESOLVED_IDS
    | PKG04_STORES_RESOLVED_IDS
    | PKG04_IDENTITY_RESOLVED_IDS
    | PKG05_CONTEXT_RESOLVED_IDS
    | PKG05_LOOP_RESOLVED_IDS
    | PKG06_LIFECYCLE_RESOLVED_IDS
    | PKG06_RUNTIME_RESOLVED_IDS
    | PKG07_WORKSPACE_RESOLVED_IDS
    | PKG07_PREVIEW_RESOLVED_IDS
    | PKG08_VERIFY_RESOLVED_IDS
    | PKG08_FINISH_RESOLVED_IDS
    | PKG09_RELEASE_RESOLVED_IDS
    | PKG09_CONNECTORS_RESOLVED_IDS
    | EPIC10_RESOLVED_IDS
    | EPIC11_RESOLVED_IDS
    | EPIC12_RESOLVED_IDS
    | PKG19_CERT_SIGNOFF_RESOLVED_IDS
)
