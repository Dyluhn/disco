"""Imports and immutable module constants shared by moved tests."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import io
import json
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from _eventlog import action, agent_error, clean_smoke_log, msg, observation, plan, status

import harness.build_soak.adapters.disco_api as _disco_mod
from harness.build_soak import run as _run_mod
from harness.build_soak.adapters.disco_api import (
    FOLLOWUP_PICKED_UP,
    FOLLOWUP_PICKUP_TIMEOUT,
    FOLLOWUP_REPLANNED,
    INACTIVE_TIMEOUT,
    LIVE_THRASH_STOP,
    PROGRESSING_TIMEOUT,
    BrowserEvidenceCollectionError,
    CollectedRun,
    DiscoApiClient,
    FinishUnsealableContentError,
    HttpTransport,
    SnapshotNotReadyError,
)
from harness.build_soak.classify import (
    _successful_browser_verification_paths,
    classify,
    classify_run_folder,
)
from harness.build_soak.evidence import load_manifest, verify_evidence_unchanged
from harness.build_soak.oracles.browser_evidence import SidecarStopOracle
from harness.build_soak.oracles.contract import ContractOracle
from harness.build_soak.oracles.thrash import ThrashOracle
from harness.build_soak.run import (
    _driver_catalog_contains,
    _is_terminal_sandbox_preflight_trace,
    assemble_dossier,
    classify_dossier,
    drive_scenario,
    load_scenarios,
    run_once,
)

_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS events (
    conversation_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    id TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (conversation_id, seq)
);
"""

_CID = "conv_fake123"

_TERMINAL_VOCAB = {"FINISHED", "VERIFIED", "STUCK", "ERROR", "IDLE"}

_EVENT_CHAIN_KEYS = {
    "require_user_event",
    "require_plan_before_execution",
    "require_action_observation_pairs",
}

_ASSERTION_KEYS = {
    "event_chain",
    "planning",
    "revisions",
    "workspace",
    "preview",
    "terminal_status_in",
    "thrash",
    "tool_scope",
    "browser_verification",
}

_FOLLOWUP_TRIGGERS = {"after_terminal", "after_first_file_write"}

__all__ = (
    "Any",
    "BrowserEvidenceCollectionError",
    "CollectedRun",
    "ContractOracle",
    "DiscoApiClient",
    "FOLLOWUP_PICKED_UP",
    "FOLLOWUP_PICKUP_TIMEOUT",
    "FOLLOWUP_REPLANNED",
    "FinishUnsealableContentError",
    "HttpTransport",
    "INACTIVE_TIMEOUT",
    "LIVE_THRASH_STOP",
    "PROGRESSING_TIMEOUT",
    "Path",
    "SidecarStopOracle",
    "SnapshotNotReadyError",
    "ThrashOracle",
    "UTC",
    "_ASSERTION_KEYS",
    "_CID",
    "_EVENTS_DDL",
    "_EVENT_CHAIN_KEYS",
    "_FOLLOWUP_TRIGGERS",
    "_TERMINAL_VOCAB",
    "_disco_mod",
    "_driver_catalog_contains",
    "_is_terminal_sandbox_preflight_trace",
    "_run_mod",
    "_successful_browser_verification_paths",
    "action",
    "agent_error",
    "assemble_dossier",
    "asyncio",
    "cast",
    "classify",
    "classify_dossier",
    "classify_run_folder",
    "clean_smoke_log",
    "datetime",
    "drive_scenario",
    "hashlib",
    "httpx",
    "inspect",
    "io",
    "json",
    "load_manifest",
    "load_scenarios",
    "msg",
    "observation",
    "plan",
    "pytest",
    "run_once",
    "sqlite3",
    "status",
    "verify_evidence_unchanged",
    "zipfile",
)
