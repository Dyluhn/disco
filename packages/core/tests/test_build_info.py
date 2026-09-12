"""Build identity: image build args first, checkout commit second, never a guess."""

from __future__ import annotations

from pathlib import Path

from disco.core import build_info as bi


def test_image_build_args_win(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_BUILD_TAG", "v0.3.0")
    monkeypatch.setenv("DISCO_BUILD_COMMIT", "3f9c1a2")
    info = bi.build_info()
    assert (info.tag, info.commit, info.source) == ("v0.3.0", "3f9c1a2", "image")
    assert info.label() == "v0.3.0 (3f9c1a2)"
    assert info.as_dict() == {"tag": "v0.3.0", "commit": "3f9c1a2", "source": "image"}


def test_commit_alone_is_the_label(monkeypatch) -> None:
    monkeypatch.delenv("DISCO_BUILD_TAG", raising=False)
    monkeypatch.setenv("DISCO_BUILD_COMMIT", "3f9c1a2")
    assert bi.build_info().label() == "3f9c1a2"


def test_checkout_fallback_or_unknown(monkeypatch) -> None:
    monkeypatch.delenv("DISCO_BUILD_TAG", raising=False)
    monkeypatch.delenv("DISCO_BUILD_COMMIT", raising=False)
    monkeypatch.delenv("PMX_BUILD_TAG", raising=False)
    monkeypatch.delenv("PMX_BUILD_COMMIT", raising=False)
    info = bi.build_info()
    if info.source == "checkout":
        assert info.commit != bi.UNKNOWN and info.tag == bi.UNKNOWN
    else:
        assert info == bi.BuildInfo(bi.UNKNOWN, bi.UNKNOWN, "unknown")
        assert info.label() == bi.UNKNOWN


def test_checkout_commit_returns_none_outside_a_repo(tmp_path: Path) -> None:
    assert bi._checkout_commit(tmp_path / "nowhere") is None
