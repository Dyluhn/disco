"""Run-admission identity bound to the existing durable run-intent authority."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import Field, field_validator

from .contracts import FrozenModel

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^run:sha256:[0-9a-f]{64}$")


class RunAdmissionAnchor(FrozenModel):
    """Reference to a persisted `agent.run-intent.*` WorkspaceMutationEvent."""

    conversation_id: str = Field(min_length=1, max_length=160)
    run_intent_event_id: str = Field(min_length=1, max_length=160)
    run_intent_seq: int = Field(ge=0)
    run_intent_operation: str = Field(pattern=r"^agent\.run-intent\.[a-z0-9_.-]+$")


class CompositionDigest(FrozenModel):
    scheme: Literal["build-composition.v1"] = "build-composition.v1"
    digest: str

    @field_validator("digest")
    @classmethod
    def _valid_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("composition digest must be sha256:<64 lowercase hex>")
        return value


class RunAdmissionIdentity(FrozenModel):
    scheme: Literal["run-admission.v1"] = "run-admission.v1"
    value: str
    composition: CompositionDigest
    anchor: RunAdmissionAnchor

    @field_validator("value")
    @classmethod
    def _valid_value(cls, value: str) -> str:
        if not _RUN_ID.fullmatch(value):
            raise ValueError("run admission identity must be run:sha256:<64 lowercase hex>")
        return value


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def composition_digest(payload: Any) -> CompositionDigest:
    """Digest an already-validated composition's JSON representation."""

    raw = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
    return CompositionDigest(digest=f"sha256:{hashlib.sha256(_canonical_json(raw)).hexdigest()}")


def derive_run_admission_identity(
    composition: CompositionDigest, anchor: RunAdmissionAnchor
) -> RunAdmissionIdentity:
    """Bind composition bytes to the durable run-intent event, without new state."""

    material = {
        "scheme": "run-admission.v1",
        "composition": composition.model_dump(mode="json"),
        "anchor": anchor.model_dump(mode="json"),
    }
    value = f"run:sha256:{hashlib.sha256(_canonical_json(material)).hexdigest()}"
    return RunAdmissionIdentity(value=value, composition=composition, anchor=anchor)
