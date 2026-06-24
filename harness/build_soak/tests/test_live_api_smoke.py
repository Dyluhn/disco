"""Opt-in LIVE-API smoke (PR S3). SKIPPED BY DEFAULT — never runs in CI / costs no
live model spend unless explicitly enabled.

Enable with a running agent-server on :8000 and a reachable model:

    DISCO_SOAK_LIVE=1 \
    DISCO_SOAK_MODEL=driver-local \
    DISCO_DB=/var/home/dylan/projects/disco/disco.db \
    .venv/bin/python -m pytest harness/build_soak/tests/test_live_api_smoke.py -m live

It drives `static_html_minimal` end-to-end through the REAL HttpTransport (the same
path `python -m harness.build_soak.run` uses), asserts a complete run folder was
assembled + classified, and prints the classification. A FAIL here is a REAL
surfaced outcome (record it per guidelines §17), not a test to weaken.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness.build_soak.adapters.disco_api import DiscoApiClient, HttpTransport
from harness.build_soak.run import load_scenarios, run_once

_LIVE = os.environ.get("DISCO_SOAK_LIVE")


@pytest.mark.live
@pytest.mark.skipif(not _LIVE, reason="live smoke is opt-in (set DISCO_SOAK_LIVE=1)")
@pytest.mark.asyncio
async def test_live_static_html_minimal_smoke(tmp_path):
    base_url = os.environ.get("DISCO_SOAK_BASE_URL", "http://127.0.0.1:8000")
    db_path = os.environ.get("DISCO_DB", "/var/home/dylan/projects/disco/disco.db")
    model = os.environ.get("DISCO_SOAK_MODEL", "driver-local")

    client = DiscoApiClient(HttpTransport(base_url), db_path=db_path, poll_interval_s=2.0)
    scenario = load_scenarios()["static_html_minimal"]
    classification = await run_once(
        client,
        scenario,
        run_id="live_smoke_static_html_minimal",
        out_root=tmp_path,
        model=model,
        autonomous=False,
        commit="",
        timeout_s=float(os.environ.get("DISCO_SOAK_TIMEOUT", "300")),
    )
    print("LIVE classification:", classification.get("status"), classification.get("code"))
    # the dossier was assembled + classified (PASS or a concrete failure code)
    assert (Path(tmp_path) / "live_smoke_static_html_minimal" / "classification.json").is_file()
    assert classification["status"] in {"PASS", "FAIL", "INVALID_RUN", "INFRA_FAILURE"}
