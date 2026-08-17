"""Docker-host evidence artifacts + genuine live-run evidence aggregation/validation
+ evidence-hygiene scanning — extracted from :mod:`verify_export_track1_closeout`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# ---- evidence files a real Docker-host live run produces (plan §1.4). At authoring
# / on a host with no Docker engine they are HONESTLY absent — recorded, never faked.
_DOCKER_HOST_ARTIFACTS = (
    "bundle-digests.json",
    "docker-inspect-sanitized.json",
    "compose-ps.json",
    "cleanup.json",
)

# ---- evidence-hygiene scan: registered planted-credential sentinels (WO-B) -----
# LABEL -> distinctive MARKER substring. Every written TEXT evidence artifact is scanned
# for these markers; any hit fails acceptance CLOSED (folds into `passed`), making the
# ABSENCE of a planted credential from the evidence enforceable and regression-proof. We
# register the MARKER (a unique NON-secret substring of the planted token) and NOT the
# full credential literal, so this verifier source never itself re-plants a real secret
# value — yet the marker is a substring of the literal, so scanning for it still catches
# the literal wherever it lands. ``G02POSCRED`` is the positional-credential sentinel
# planted in ``development/tests/export_track1_closeout/test_g02_positional_credential.py``.
_EVIDENCE_HYGIENE_SENTINELS: dict[str, str] = {
    "G02_POSITIONAL_CRED": "G02POSCRED",
}
# The TEXT evidence artifacts the hygiene scan reads (each scanned only if it exists).
# NOT ``evidence.json`` — it is written AFTER this scan runs and would otherwise, by
# recording violations, scan itself.
_HYGIENE_SCAN_FILES: tuple[str, ...] = (
    "pytest-nonlive.xml",
    "pytest-closeout.xml",
    "pytest-live.xml",
    "pytest-capture.xml",
    "frontend-vitest.json",
    # R6: the structured Firefox e2e report is a written text artifact too — a planted
    # credential sentinel here must likewise force passed:false (plan §9.4 / criterion 6).
    "frontend-e2e.json",
    "g11-typecheck.txt",
    "anti-bypass-scan.json",
    "docker-versions.txt",
    *_DOCKER_HOST_ARTIFACTS,
)

# ---- Docker-host-only evidence placeholders (honest, never faked) -------------


def _write_docker_host_artifacts(
    evidence_dir: Path, docker_available: bool, *, live_lifecycle_ok: bool = False
) -> dict[str, str]:
    """Write the Docker-host evidence artifacts with a TRUTHFUL status tied to the ACTUAL
    live lifecycle (G13(a) / plan §9.2). The live-production claim
    ``produced_by_live_lane_on_docker_host`` is stamped ONLY when a Docker engine is
    present AND the live lane completed a clean lifecycle (``live_lifecycle_ok``); with no
    engine the status is ``absent_no_docker_engine``, and with an engine but a failed lane
    it is ``live_lane_failed``. ``live_lifecycle_ok`` is keyword-only and defaults to a
    NOT-live value, so the frozen G13(a) call ``_write_docker_host_artifacts(dir,
    docker_available=False)`` can never emit the live claim."""
    if docker_available and live_lifecycle_ok:
        status = "produced_by_live_lane_on_docker_host"
        note = (
            "This artifact was produced by the live-docker lane on a real Docker host that "
            "completed a clean lifecycle (plan §12) — a genuine live-production record."
        )
    elif docker_available:
        status = "live_lane_failed"
        note = (
            "A Docker engine is present but the live-docker lane did NOT complete a clean "
            "lifecycle; this artifact records that failure honestly and is NOT a "
            "live-production claim."
        )
    else:
        status = "absent_no_docker_engine"
        note = (
            "No Docker engine is available on this host, so the live-docker lane could not "
            "run; this artifact is honestly marked absent, not faked, and carries no "
            "live-production claim."
        )
    written: dict[str, str] = {}
    for name in _DOCKER_HOST_ARTIFACTS:
        payload = {
            "status": status,
            "available": docker_available,
            "live_lifecycle_ok": live_lifecycle_ok,
            "note": note,
        }
        (evidence_dir / name).write_text(
            json.dumps(payload, indent=2) + "\n" if name.endswith(".json") else note + "\n",
            encoding="utf-8",
        )
        written[name] = status
    return written


# ---- genuine live-run evidence: aggregate + validate (C9-02) ------------------

_EVIDENCE_FIXTURE_SUBDIR = "live-fixtures"
_REQUIRED_EVIDENCE_FAMILIES = (
    "express",
    "fastapi",
    "imported_node",
    "vite_static",
    "appkit",
    "public_build_env_vite",
)


def _read_family_evidence(fixtures_dir: Path, kind: str) -> dict[str, dict[str, object]]:
    """Load every ``<family>.<kind>.json`` under the per-fixture evidence dir, keyed by
    family. Unparseable/malformed files are dropped (they surface as a missing family in
    validation), never raised."""
    out: dict[str, dict[str, object]] = {}
    for path in sorted(fixtures_dir.glob(f"*.{kind}.json")):
        family = path.name[: -len(f".{kind}.json")]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            out[family] = data
    return out


def _run_id_ok(rec: object, run_id: str) -> bool:
    return isinstance(rec, dict) and rec.get("_run_id") == run_id


def _validate_digest_evidence(
    family: str,
    digests: dict[str, dict[str, object]],
    run_id: str,
    hex64: re.Pattern[str],
) -> list[str]:
    """Strict schema + internal-consistency validation (C9-02 P4-1) for one family's
    ``bundle-digests`` record: the run's nonce, 64-hex digests that actually differ, a
    ``release.json`` whose ``provenance.assessment`` agrees with the recorded response,
    and a bound download that really returned 200 and matches the response."""
    d = digests.get(family)
    if d is None:
        return [f"live evidence: no bundle-digest for family {family!r}"]
    if not _run_id_ok(d, run_id):
        return [f"live evidence: {family} digest is not from this run (run_id mismatch)"]
    reasons: list[str] = []
    if d.get("family") != family:
        reasons.append(f"live evidence: {family} digest payload family mislabelled")
    resp_sha, zip_sha = str(d.get("response_sha256", "")), str(d.get("zip_sha256", ""))
    if not hex64.match(resp_sha) or not hex64.match(zip_sha):
        reasons.append(f"live evidence: {family} digest sha256 not 64-hex")
    elif resp_sha == zip_sha:
        reasons.append(f"live evidence: {family} response and zip sha256 identical")
    rj = d.get("release_json")
    prov = rj.get("provenance") if isinstance(rj, dict) else None
    resp = d.get("release_response")
    resp_assessment = resp.get("assessment") if isinstance(resp, dict) else None
    if not (isinstance(prov, dict) and prov.get("assessment")):
        reasons.append(f"live evidence: {family} release.json lacks provenance.assessment")
    elif prov.get("assessment") != resp_assessment:
        reasons.append(f"live evidence: {family} provenance/response assessment disagree")
    sb = d.get("source_binding")
    if not (isinstance(sb, dict) and sb.get("download_status") == 200):
        reasons.append(f"live evidence: {family} bound download was not a 200")
    if d.get("binding_matches_response") is not True:
        reasons.append(f"live evidence: {family} source binding does not match the response")
    return reasons


def _validate_inspect_evidence(
    family: str,
    inspects: dict[str, dict[str, object]],
    run_id: str,
    image_id_re: re.Pattern[str],
) -> list[str]:
    """The family's ``docker-inspect`` record: the run's nonce and at least one real
    docker image Id."""
    ins = inspects.get(family)
    if not _run_id_ok(ins, run_id):
        return [f"live evidence: {family} inspect is not from this run (run_id mismatch)"]
    images = ins.get("images") if isinstance(ins, dict) else None
    has_image_id = isinstance(images, list) and any(
        isinstance(i, dict) and image_id_re.match(str(i.get("Id", ""))) for i in images
    )
    if not has_image_id:
        return [f"live evidence: {family} inspect has no valid docker image Id"]
    return []


def _validate_ps_evidence(
    family: str, ps_rows: dict[str, dict[str, object]], run_id: str
) -> list[str]:
    """The family's ``compose-ps`` record: the run's nonce and at least one row with a
    service + state."""
    ps = ps_rows.get(family)
    if not _run_id_ok(ps, run_id):
        return [f"live evidence: {family} compose-ps is not from this run (run_id mismatch)"]
    rows = ps.get("rows") if isinstance(ps, dict) else None
    has_row = isinstance(rows, list) and any(
        isinstance(r, dict) and r.get("Service") and r.get("State") for r in rows
    )
    if not has_row:
        return [f"live evidence: {family} compose-ps has no service+state row"]
    return []


def _validate_cleanup_evidence(
    family: str, cleanups: dict[str, dict[str, object]], run_id: str
) -> list[str]:
    """The family's ``cleanup`` record: the run's nonce, a ``zero_remaining`` claim that
    is CONSISTENT with an all-empty ``remaining`` (not merely asserted), and real disk
    before/after snapshots."""
    cl = cleanups.get(family)
    if not _run_id_ok(cl, run_id):
        return [f"live evidence: {family} cleanup is not from this run (run_id mismatch)"]
    # `_run_id_ok` returning True already guarantees `isinstance(cl, dict)` (it is the
    # first half of that check); assert it so the type checker narrows `cl` for the
    # rest of this function instead of leaving every `.get()` below on an Optional.
    assert isinstance(cl, dict)
    reasons: list[str] = []
    remaining = cl.get("remaining")
    all_empty = isinstance(remaining, dict) and all(
        isinstance(v, list) and not v for v in remaining.values()
    )
    if cl.get("zero_remaining") is not True or not all_empty:
        reasons.append(f"live evidence: {family} cleanup did not consistently reach zero remaining")
    db, da = cl.get("disk_before"), cl.get("disk_after")
    if not (isinstance(db, dict) and db) or not (isinstance(da, dict) and da):
        reasons.append(f"live evidence: {family} cleanup missing real disk before/after")
    return reasons


def _finalize_live_evidence(evidence_dir: Path, run_id: str) -> tuple[bool, list[str]]:
    """Aggregate the live lane's GENUINE per-fixture evidence into the four required
    artifacts and VALIDATE their contents before ``passed:true`` (C9-02). Overwrites the
    honest placeholder markers written by ``_write_docker_host_artifacts`` with the real
    aggregate. Returns ``(ok, reasons)``.

    Validation is STRICT SCHEMA + INTERNAL CONSISTENCY, not truthiness (C9-02 P4-1),
    delegated per evidence family to ``_validate_digest_evidence``,
    ``_validate_inspect_evidence``, ``_validate_ps_evidence``, and
    ``_validate_cleanup_evidence`` — every record must carry the run's nonce (``run_id``,
    P4-2); digest SHA-256s must be 64-hex and the zip differs from the response; the
    parsed ``release.json`` must carry a ``provenance.assessment`` that agrees with the
    recorded response and the recorded download status must be 200; each image Id must be
    a real docker id; each ps row must carry a service+state; and cleanup's
    ``zero_remaining`` must be CONSISTENT with an all-empty ``remaining`` (not merely
    asserted) with real disk before/after. A contradictory or self-reported payload cannot
    satisfy this gate."""
    fixtures_dir = evidence_dir / _EVIDENCE_FIXTURE_SUBDIR
    digests = _read_family_evidence(fixtures_dir, "digest")
    ps_rows = _read_family_evidence(fixtures_dir, "compose-ps")
    inspects = _read_family_evidence(fixtures_dir, "inspect")
    cleanups = _read_family_evidence(fixtures_dir, "cleanup")

    hex64 = re.compile(r"^[0-9a-f]{64}$")
    image_id_re = re.compile(r"^(sha256:)?[0-9a-f]{12,64}$")
    reasons: list[str] = []

    for family in _REQUIRED_EVIDENCE_FAMILIES:
        reasons.extend(_validate_digest_evidence(family, digests, run_id, hex64))
        reasons.extend(_validate_inspect_evidence(family, inspects, run_id, image_id_re))
        reasons.extend(_validate_ps_evidence(family, ps_rows, run_id))
        reasons.extend(_validate_cleanup_evidence(family, cleanups, run_id))

    # Aggregate the four required artifacts (overwriting the placeholder markers).
    (evidence_dir / "bundle-digests.json").write_text(
        json.dumps({"families": digests}, indent=2) + "\n", encoding="utf-8"
    )
    (evidence_dir / "docker-inspect-sanitized.json").write_text(
        json.dumps({"families": inspects}, indent=2) + "\n", encoding="utf-8"
    )
    (evidence_dir / "compose-ps.json").write_text(
        json.dumps({"families": ps_rows}, indent=2) + "\n", encoding="utf-8"
    )
    (evidence_dir / "cleanup.json").write_text(
        json.dumps({"families": cleanups}, indent=2) + "\n", encoding="utf-8"
    )
    return (not reasons), reasons


# ---- evidence-hygiene scan (WO-B) ---------------------------------------------


def _scan_evidence_hygiene(evidence_dir: Path) -> tuple[bool, list[dict[str, object]]]:
    """Scan every written TEXT evidence artifact for any REGISTERED planted-credential
    sentinel and return ``(ok, violations)``. Records a typed violation — the evidence
    file NAME + the 1-based line number + the sentinel LABEL ONLY, and NEVER the
    surrounding text or the matched value — for each hit, so a leak is located without
    the report itself re-emitting the credential. Runs AFTER the evidence files are
    written and folds into ``passed`` exactly like ``frozen_ok``/``clean``, which makes
    the credential's absence enforceable and regression-proof. Reads leniently: a
    sentinel is ASCII, so a ``replace``-errors decode cannot hide it and a non-UTF-8
    artifact cannot crash the gate."""
    violations: list[dict[str, object]] = []
    # Every fixed top-level artifact, PLUS every per-fixture live-evidence file (C9-02:
    # the hygiene scan must cover EVERY textual evidence artifact, including the genuine
    # per-family digests/inspects/ps/cleanup the live lane emits).
    scan_paths: list[tuple[str, Path]] = [
        (name, evidence_dir / name) for name in _HYGIENE_SCAN_FILES
    ]
    fixtures_dir = evidence_dir / _EVIDENCE_FIXTURE_SUBDIR
    if fixtures_dir.is_dir():
        for path in sorted(fixtures_dir.glob("*.json")):
            scan_paths.append((f"{_EVIDENCE_FIXTURE_SUBDIR}/{path.name}", path))
    for name, path in scan_paths:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for label, marker in _EVIDENCE_HYGIENE_SENTINELS.items():
                if marker in line:
                    violations.append({"file": name, "line": lineno, "sentinel": label})

    def _sort_key(v: dict[str, object]) -> tuple[str, int, str]:
        line = v["line"]
        return (str(v["file"]), line if isinstance(line, int) else 0, str(v["sentinel"]))

    violations.sort(key=_sort_key)
    return (not violations), violations
