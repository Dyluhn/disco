"""Fast qualification profile (F0/F1) — selection, refusal, and non-promotion.

The load-bearing test here is the last one: a qualification batch must be
*structurally* unable to enter promotion accounting. Everything else guards the
path that reaches it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from harness.build_soak import profile as prof
from harness.build_soak.run import _BATCH_SUMMARY_NAME, batch_summary_name
from harness.reliability.run import _build_soak_result

_BASE = [
    "p4_ff_static_basic",
    "p4_ff_react_steer",
    "p4_ff_react_continue",
    "p4_ff_node_pause",
    "p4_ff_python_cancel_recovery",
    "p4_ff_import_rollback",
    "p4_appkit_create",
    "p4_appkit_semantic_edit",
]


def _write_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict) -> str:
    """Install a throwaway profile and point the loader at it."""
    directory = tmp_path / "profiles"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "tmp_profile.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    monkeypatch.setattr(prof, "_PROFILE_DIR", directory)
    return "tmp_profile"


def _valid_body(**overrides) -> dict:
    body = {
        "schema_version": 1,
        "id": "tmp_profile",
        "version": 1,
        "counts_toward_promotion": False,
        "scenarios_file": "scenarios_phase4.yaml",
        "base": list(_BASE),
    }
    body.update(overrides)
    return body


# ---- selection ------------------------------------------------------------


def test_selection_and_order_are_deterministic():
    """The manifest's order IS the run order, byte for byte, every time."""
    profile, _path, _digest = prof.load_profile("fast_qualification")
    first, _ = prof.resolve_selection(profile, [])
    second, _ = prof.resolve_selection(profile, [])

    assert first == _BASE
    assert first == second


def test_live_overlay_appends_the_governed_context_scenarios():
    profile, _path, _digest = prof.load_profile("fast_qualification")
    selection, meta = prof.resolve_selection(profile, ["context"])

    assert selection == [*_BASE, "p4_ff_context_catalog", "p4_ff_context_ledger"]
    assert meta["context"]["kind"] == "live"


def test_provider_free_overlay_contributes_no_live_scenario():
    profile, _path, _digest = prof.load_profile("fast_qualification")
    selection, meta = prof.resolve_selection(profile, ["freeze"])

    assert selection == _BASE, "a provider-free overlay must not add live scenarios"
    assert meta["freeze"]["pytest"], "the freeze overlay must name its provider-free tests"


def test_the_shipped_profile_stays_application_shape_diverse():
    """A profile that collapsed onto one framework would stop falsifying."""
    profile, _path, _digest = prof.load_profile("fast_qualification")
    selection, _ = prof.resolve_selection(profile, [])
    joined = " ".join(selection)

    for shape in ("static", "react", "node", "python", "import", "appkit"):
        assert shape in joined, f"profile lost its {shape} shape"


# ---- refusals, all BEFORE provider spend -----------------------------------


def test_unknown_scenario_id_is_refused(tmp_path, monkeypatch):
    name = _write_profile(tmp_path, monkeypatch, _valid_body(base=["p4_ff_static_basic", "nope"]))
    profile, _path, _digest = prof.load_profile(name)

    with pytest.raises(prof.ProfileError, match="unknown scenario id"):
        prof.resolve_selection(profile, [])


def test_duplicate_scenario_id_is_refused(tmp_path, monkeypatch):
    name = _write_profile(
        tmp_path, monkeypatch, _valid_body(base=["p4_ff_static_basic", "p4_ff_static_basic"])
    )
    profile, _path, _digest = prof.load_profile(name)

    with pytest.raises(prof.ProfileError, match="duplicate scenario id"):
        prof.resolve_selection(profile, [])


def test_missing_base_is_refused(tmp_path, monkeypatch):
    body = _valid_body()
    body.pop("base")
    name = _write_profile(tmp_path, monkeypatch, body)

    with pytest.raises(prof.ProfileError, match="no base scenario list"):
        prof.load_profile(name)


def test_a_profile_claiming_promotion_credit_is_refused(tmp_path, monkeypatch):
    """Tampering the one field that matters must fail closed."""
    name = _write_profile(tmp_path, monkeypatch, _valid_body(counts_toward_promotion=True))

    with pytest.raises(prof.ProfileError, match="counts_toward_promotion"):
        prof.load_profile(name)


def test_unsupported_schema_version_is_refused(tmp_path, monkeypatch):
    name = _write_profile(tmp_path, monkeypatch, _valid_body(schema_version=99))

    with pytest.raises(prof.ProfileError, match="schema_version"):
        prof.load_profile(name)


def test_unknown_overlay_is_refused():
    profile, _path, _digest = prof.load_profile("fast_qualification")

    with pytest.raises(prof.ProfileError, match="unknown overlay"):
        prof.resolve_selection(profile, ["does_not_exist"])


def test_manifest_digest_changes_when_the_manifest_changes(tmp_path, monkeypatch):
    name = _write_profile(tmp_path, monkeypatch, _valid_body())
    _p1, path, first = prof.load_profile(name)
    path.write_text(path.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    _p2, _path, second = prof.load_profile(name)

    assert first != second, "the receipt digest must move when the manifest moves"


# ---- the summary-name seam -------------------------------------------------


def test_batch_summary_name_defaults_to_the_promotion_name():
    assert batch_summary_name(None) == _BATCH_SUMMARY_NAME
    assert batch_summary_name("") == _BATCH_SUMMARY_NAME


@pytest.mark.parametrize(
    "bad", ["../batch-summary.json", "a/b.json", "..", ".", "batch-summary.txt"]
)
def test_batch_summary_name_rejects_escaping_or_non_json(bad):
    """`../batch-summary.json` would put a promotion-visible report back in a
    parent tree, silently undoing a non-promoting lane's exclusion."""
    with pytest.raises(ValueError):
        batch_summary_name(bad)


# ---- THE load-bearing one --------------------------------------------------


def test_qualification_evidence_cannot_be_ingested_as_promotion_evidence(tmp_path):
    """The promotion reader must be UNABLE to count a qualification batch.

    `_build_soak_result` discovers evidence with
    `sorted(out.rglob("batch-summary.json"), key=mtime)` and reads the newest
    match. A qualification lane therefore cannot rely on a label or a filename
    convention: it must never create that name at all.
    """
    qual = tmp_path / "qualification"
    (qual / "batch_x" / "conversations").mkdir(parents=True)

    # A qualification batch that PASSED everything -- the most dangerous shape,
    # because if it were visible it would look like clean promotion evidence.
    passing = {
        "schema_version": 1,
        "runs": [{"status": "PASS", "index": i} for i in range(8)],
        "status_counts": {"PASS": 8},
    }
    (qual / "batch_x" / prof._PROFILE_SUMMARY_NAME).write_text(json.dumps(passing))

    # The promotion reader finds nothing to count.
    status, units, detail = _build_soak_result(qual, exit_code=0, units=8)
    assert status == "INFRA"
    assert units == 0
    assert "no batch-summary.json" in detail

    # And the runner's own guard agrees the tree is not promotion-visible.
    prof.assert_not_promotion_visible(qual)


def test_the_guard_catches_a_promotion_visible_file_leaking_back_in(tmp_path):
    """Negative control: if the promotion name ever appears, refuse loudly."""
    qual = tmp_path / "qualification"
    (qual / "batch_y").mkdir(parents=True)
    (qual / "batch_y" / _BATCH_SUMMARY_NAME).write_text(json.dumps({"runs": []}))

    with pytest.raises(prof.ProfileError, match="visible to the promotion reader"):
        prof.assert_not_promotion_visible(qual)


def test_f1_dry_run_writes_a_receipt_and_makes_no_provider_call(tmp_path):
    out = tmp_path / "f1"
    code = prof.main(
        [
            "f1",
            "--dry-run",
            "--out",
            str(out),
            "--model",
            "test-model",
            "--seed-base",
            "470000",
        ]
    )

    assert code == 0
    receipt = json.loads((out / prof._RECEIPT_NAME).read_text(encoding="utf-8"))
    assert receipt["counts_toward_promotion"] is False
    assert receipt["results"]["provider_calls"] == 0
    assert receipt["promotion_exclusion"]["mechanism"] == "structural"
    assert receipt["selection"]["scenario_ids"] == _BASE
    # The invocation it WOULD make must carry the non-promoting summary name.
    argv = receipt["binding"]["argv"]
    assert "--summary-name" in argv
    assert argv[argv.index("--summary-name") + 1] == prof._PROFILE_SUMMARY_NAME
    prof.assert_not_promotion_visible(out)


def test_f1_requires_an_explicit_seed_base() -> None:
    with pytest.raises(SystemExit) as exc_info:
        prof.main(["f1", "--dry-run", "--model", "test-model"])

    assert exc_info.value.code == 2
