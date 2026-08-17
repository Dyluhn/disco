"""The structured fact every oracle emits (guidelines §10).

Each oracle returns `OracleResult` objects — structured facts, never prose. The
classifier reads these; a human reads them; nothing interprets free text. The
result round-trips losslessly to/from a JSON-safe dict so it lands verbatim in the
frozen `classification.json` (§13) and the evidence dossier.

Plain dataclasses (no pydantic, no disco import) keep the schema standalone and the
serialization byte-stable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Oracle result status. Distinct from the run OUTCOME (PASS/FAIL/INVALID_RUN/
# INFRA_FAILURE): an oracle PASSES, FAILS, or is SKIPPED (not applicable to this
# scenario, or needs evidence this slice doesn't capture — e.g. the live tool
# scope). A SKIP is never silently a pass; the classifier records it.
OK = "PASS"
FAILED = "FAIL"
SKIPPED = "SKIP"

_STATUSES = frozenset({OK, FAILED, SKIPPED})


@dataclass(frozen=True)
class OracleResult:
    """One oracle's verdict on one predicate.

    Fields:
      oracle: the emitting oracle class name (e.g. "RevisionOracle").
      status: PASS | FAIL | SKIP.
      code:   a failure_codes constant when status==FAIL; for PASS/SKIP it may be
              None or a short reason token.
      facts:  structured, machine-readable evidence (never prose narrative).
      first_broken_link: the "A -> B" link this result breaks (FAIL only), used by
              the classifier's first-broken-link wins ordering (§16).
      shape:  an OPTIONAL machine-readable sub-classification of the failure, for
              oracles that detect structurally different failures (A11 §2). It is
              adjudicable, unlike `first_broken_link`, which is free text: reading
              a shape out of a prose link is what let A6.3 §1 misattribute 10-C.
              When present it is emitted with a code that agrees with it, derived
              from one table (see `_thrash_shapes`), so the two cannot drift.
      reason: an OPTIONAL short human token (e.g. why a SKIP) — advisory only,
              never adjudicated on.
    """

    oracle: str
    status: str
    code: str | None = None
    facts: dict[str, Any] = field(default_factory=dict)
    first_broken_link: str | None = None
    shape: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError(
                f"OracleResult.status must be one of {sorted(_STATUSES)}, got {self.status!r}"
            )
        if self.status == FAILED and not self.code:
            raise ValueError("a FAIL OracleResult must carry a failure code")
        if not isinstance(self.facts, dict):
            raise TypeError("OracleResult.facts must be a dict")

    @property
    def passed(self) -> bool:
        return self.status == OK

    @property
    def failed(self) -> bool:
        return self.status == FAILED

    @property
    def skipped(self) -> bool:
        return self.status == SKIPPED

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict (stable key order) for the dossier."""
        out: dict[str, Any] = {
            "oracle": self.oracle,
            "status": self.status,
            "code": self.code,
            "facts": dict(self.facts),
        }
        if self.first_broken_link is not None:
            out["first_broken_link"] = self.first_broken_link
        if self.shape is not None:
            out["shape"] = self.shape
        if self.reason is not None:
            out["reason"] = self.reason
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> OracleResult:
        if not isinstance(raw, dict):
            raise TypeError("OracleResult.from_dict expects a dict")
        missing = {"oracle", "status"} - set(raw)
        if missing:
            raise ValueError(f"OracleResult dict missing keys: {sorted(missing)}")
        return cls(
            oracle=str(raw["oracle"]),
            status=str(raw["status"]),
            code=raw.get("code"),
            facts=dict(raw.get("facts") or {}),
            first_broken_link=raw.get("first_broken_link"),
            shape=raw.get("shape"),
            reason=raw.get("reason"),
        )


def passing(
    oracle: str, *, facts: dict[str, Any] | None = None, code: str | None = None
) -> OracleResult:
    return OracleResult(oracle=oracle, status=OK, code=code, facts=facts or {})


def failing(
    oracle: str,
    code: str,
    *,
    first_broken_link: str,
    facts: dict[str, Any] | None = None,
    shape: str | None = None,
) -> OracleResult:
    return OracleResult(
        oracle=oracle,
        status=FAILED,
        code=code,
        facts=facts or {},
        first_broken_link=first_broken_link,
        shape=shape,
    )


def skipping(oracle: str, *, reason: str, facts: dict[str, Any] | None = None) -> OracleResult:
    return OracleResult(oracle=oracle, status=SKIPPED, code=None, facts=facts or {}, reason=reason)
