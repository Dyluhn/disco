"""HarnessValidityOracle (guidelines §10, §8 INVALID_RUN).

Runs FIRST. It does not judge the product — it judges whether the harness
collected enough durable evidence to adjudicate at all. A failure here maps to
INVALID_RUN (which never counts as a pass and blocks promotion until the harness
is fixed), NOT to a product FAIL.

Checks, in order:
  1. evidence integrity — the frozen evidence hashes still match (caller passes the
     verdict in; a mismatch means the run folder was mutated after classification,
     §6 → INVALID_RUN).
  2. events present + parseable (an empty / missing event log can't be adjudicated).
  3. a user message exists (a run that never began can't be judged — §8 "test
     aborted before scenario actually began").
"""

from __future__ import annotations

from typing import Any

from .. import failure_codes as fc
from ..events import has_user_message
from .schema import OracleResult, failing, passing

_ORACLE = "HarnessValidityOracle"


class HarnessValidityOracle:
    def check(
        self,
        events: list[dict[str, Any]],
        *,
        evidence_intact: bool = True,
        parse_ok: bool = True,
    ) -> list[OracleResult]:
        if not evidence_intact:
            return [
                failing(
                    _ORACLE,
                    fc.EVIDENCE_HASH_MISMATCH,
                    first_broken_link="frozen_evidence -> recomputed_hash",
                    facts={"evidence_intact": False},
                )
            ]
        if not parse_ok:
            return [
                failing(
                    _ORACLE,
                    fc.UNPARSEABLE_EVENTS,
                    first_broken_link="events.jsonl -> parsed_events",
                    facts={"parse_ok": False},
                )
            ]
        if not events:
            return [
                failing(
                    _ORACLE,
                    fc.NO_EVENTS,
                    first_broken_link="run -> events.jsonl",
                    facts={"event_count": 0},
                )
            ]
        if not has_user_message(events):
            return [
                failing(
                    _ORACLE,
                    fc.MISSING_REQUIRED_EVIDENCE,
                    first_broken_link="run -> user_message",
                    facts={
                        "event_count": len(events),
                        "reason": "no user message — scenario never began",
                    },
                )
            ]
        return [passing(_ORACLE, facts={"event_count": len(events)})]
