"""Imports and immutable module constants shared by moved tests."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from disco.core.loop import HostVerificationDeliverable
from disco.core.verification import (
    AdmittedVerificationContract,
    HostVerificationClaim,
    HostVerificationClaimResult,
    HostVerificationObservedFact,
    HostVerificationResult,
    PreviewSelectionIdentity,
    VerificationArtifactIdentity,
    VerificationCheckContract,
    VerificationClaimKind,
    VerificationClaimStatus,
    VerificationDeliveryContract,
    VerificationEvidenceModality,
    VerificationExecutionIdentity,
    default_structured_web_claims,
    target_verification_result,
    with_verification_effect_receipt,
)

from harness.build_soak.failure_codes import GOVERNED_ADMISSION_BYPASSED
from harness.build_soak.oracles.contract import ContractOracle
from harness.build_soak.oracles.governed_admission import (
    GovernedAdmissionOracle,
    _preview_identity_from_pair,
    _shared_execution_authority,
)
from harness.build_soak.run import load_scenarios

_RUN_ID = "run:sha256:" + "b" * 64

__all__ = (
    "AdmittedVerificationContract",
    "Any",
    "ContractOracle",
    "GOVERNED_ADMISSION_BYPASSED",
    "GovernedAdmissionOracle",
    "HostVerificationClaim",
    "HostVerificationClaimResult",
    "HostVerificationDeliverable",
    "HostVerificationObservedFact",
    "HostVerificationResult",
    "PreviewSelectionIdentity",
    "VerificationArtifactIdentity",
    "VerificationCheckContract",
    "VerificationClaimKind",
    "VerificationClaimStatus",
    "VerificationDeliveryContract",
    "VerificationEvidenceModality",
    "VerificationExecutionIdentity",
    "_RUN_ID",
    "_preview_identity_from_pair",
    "_shared_execution_authority",
    "default_structured_web_claims",
    "hashlib",
    "json",
    "load_scenarios",
    "pytest",
    "target_verification_result",
    "with_verification_effect_receipt",
)
