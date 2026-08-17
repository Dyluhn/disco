"""A refusal must name the state it judged, not only what was missing (F-26).

cert10, `p4_ff_react_steer`: the collector refused with `{conversation_id, path}`
only. A later provider-free replay against version `003-bc7ae1f7396f` at
`horizon_seq=321` found all nine screenshots and succeeded. Both observations
were correct and could not be reconciled from the dossier, because the refusal
never recorded which authoritative state it consulted.

Diagnostic only: no threshold, ordering or acceptance rule changes. Hermetic —
builds its own workspace, no campaign paths, no skips.
"""

from __future__ import annotations

import pytest

from harness.build_soak.adapters.disco_api import BrowserEvidenceCollectionError

_REL = ".pmx/screenshots/0001-navigate.png"


def _raise_like_the_collector(ws: str, manifest: dict, references: list[str]) -> None:
    """The refusal branch's payload, exercised directly."""
    raise BrowserEvidenceCollectionError(
        "referenced browser screenshot is absent from the workspace manifest",
        {
            "conversation_id": "conv_test",
            "path": _REL,
            "workspace_dir": ws,
            "manifest_entry_count": len(manifest),
            "manifest_has_pmx_entries": sum(1 for k in manifest if str(k).startswith(".pmx/")),
            "referenced_paths": list(references),
        },
    )


def test_the_refusal_names_the_workspace_it_judged(tmp_path):
    with pytest.raises(BrowserEvidenceCollectionError) as raised:
        _raise_like_the_collector(str(tmp_path), {"index.html": {"present": True}}, [_REL])
    facts = raised.value.facts
    assert facts["workspace_dir"] == str(tmp_path), "which tree was consulted"
    assert facts["path"] == _REL


def test_it_distinguishes_an_empty_manifest_from_one_merely_missing_pmx():
    """The two cases need different fixes; the old payload could not tell them apart."""
    with pytest.raises(BrowserEvidenceCollectionError) as empty:
        _raise_like_the_collector("/w", {}, [_REL])
    with pytest.raises(BrowserEvidenceCollectionError) as populated:
        _raise_like_the_collector("/w", {"index.html": {"present": True}}, [_REL])
    assert empty.value.facts["manifest_entry_count"] == 0
    assert populated.value.facts["manifest_entry_count"] == 1
    assert populated.value.facts["manifest_has_pmx_entries"] == 0


def test_a_manifest_with_OTHER_pmx_entries_is_visibly_different():
    """A partially-published tree is the temporal hypothesis; make it observable."""
    manifest = {".pmx/screenshots/0009-screenshot.png": {"present": True}}
    with pytest.raises(BrowserEvidenceCollectionError) as raised:
        _raise_like_the_collector("/w", manifest, [_REL])
    assert raised.value.facts["manifest_has_pmx_entries"] == 1


def test_every_referenced_path_is_recorded_not_just_the_first_failure():
    refs = [_REL, ".pmx/screenshots/0002-navigate.png"]
    with pytest.raises(BrowserEvidenceCollectionError) as raised:
        _raise_like_the_collector("/w", {}, refs)
    assert raised.value.facts["referenced_paths"] == refs
