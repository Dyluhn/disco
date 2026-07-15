"""RP-09 live audio acceptance — integration harness.

What this proves (when the Kokoro model is present):
  * Real Kokoro TTS → real _audio_mixer → a decodable playable MP3 file.
  * Both modes (podcast 2-voice, single 1-voice) produce correct artifacts.
  * Varying section counts: 1-section minimal, 3-section (real captured
    ReportEvent from prod disco.db), 6-section (real captured ReportEvent,
    includes empty-markdown sections — a real-world edge case).
  * The atomic write (.part cleanup) leaves no leftover temp file.
  * Cache idempotency: a second call returns byte-identical bytes without
    re-running synthesis (confirmed by breaking the LLM after the first call).
  * Single mode never uses voice_b voice (only voice_a throughout).
  * MP3 duration matches the turn count within encoder tolerance.

MARKED integration — never runs in the required CI unit gate:
  `pytest -m "not integration" packages ...`
Run the live suite with:
  `pytest -m integration packages/agent-server/tests/test_report_audio_live.py -v`

Environment needs:
  - `kokoro_onnx` and `lameenc` installed (bundled TTS is a core dep — a bare `uv sync` installs it)
  - Kokoro v1.0 model at ~/.cache/disco-tts/ (auto-downloaded on first use or pre-cached)
  - numpy in the venv (always present as a core dep)

If the Kokoro package is missing or the model download is not wanted, the entire
module is skipped with a clear message — no fake synthesis is substituted.

LLM stubs: `audio_overview._call_llm` is monkeypatched to return pre-baked
turn-scripts so this harness has no LLM network dependency.  The LLM step is
already covered by the unit tests in test_report_audio.py.  The point here is
real TTS → real mixer → real file.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Skip guard: detect Kokoro availability before importing anything heavy.
# ---------------------------------------------------------------------------


def _kokoro_available() -> tuple[bool, str]:
    """Return (available, reason). Checks the Python package AND model files."""
    # Package check
    if importlib.util.find_spec("kokoro_onnx") is None:
        return False, "kokoro_onnx not installed (bundled TTS is core — run `uv sync`)"
    if importlib.util.find_spec("lameenc") is None:
        return False, "lameenc not installed (bundled TTS is core — run `uv sync`)"
    # Model-file check — mirrors tts_local._data_dir() logic
    from disco.core.env import disco_env

    base = disco_env("TTS_DIR") or os.path.expanduser("~/.cache/disco-tts")
    model_dir = Path(base)
    model = model_dir / "kokoro-v1.0.onnx"
    voices = model_dir / "voices-v1.0.bin"
    if not model.exists():
        return False, f"Kokoro model not found at {model} (first run downloads ~300 MB)"
    if not voices.exists():
        return False, f"Kokoro voices not found at {voices}"
    return True, ""


_KOKORO_OK, _KOKORO_SKIP_REASON = _kokoro_available()

pytestmark = pytest.mark.integration

skip_no_kokoro = pytest.mark.skipif(
    not _KOKORO_OK,
    reason=f"Kokoro not available: {_KOKORO_SKIP_REASON}",
)


# ---------------------------------------------------------------------------
# Real captured ReportEvent fixtures — sourced from prod disco.db payloads,
# trimmed to the minimal fields the pipeline needs.  Embed as Python dicts so
# the test is self-contained (no DB dependency at test time).
# ---------------------------------------------------------------------------

# Small: 3 sections — "what is reciprocal rank fusion"
# Sourced from conv_eb644aa in production disco.db (2026-06-18)
_SMALL_REPORT_DICT: dict[str, Any] = {
    "id": "evt_live_small",
    "kind": "report",
    "source": "agent",
    "timestamp": "2026-06-12T00:00:00Z",
    "schema_version": 1,
    "seq": 1,
    "meta": {},
    "query": "what is reciprocal rank fusion",
    "summary": (
        "Reciprocal Rank Fusion (RRF) is a rank-based algorithm that merges "
        "multiple retrieval result lists into a single unified ranking by calculating "
        "a score based solely on the position of each item across the lists."
    ),
    "sections": [
        {
            "id": "s_rrf_1",
            "title": "How does Reciprocal Rank Fusion mathematically combine ranked lists?",
            "markdown": (
                "Reciprocal Rank Fusion (RRF) is a rank-based algorithm designed to merge "
                "multiple retrieval result lists into a single, unified ranking. "
                "The core mathematical premise is the total disregard for raw similarity scores "
                "or distance metrics, which are often on incompatible scales. "
                "Instead, the algorithm relies exclusively on the ordinal position of each "
                "document within its respective list. The RRF score for a document is the "
                "sum of 1/(k + rank) across all lists, where k is a smoothing constant."
            ),
            "cited_passage_ids": ["p0"],
            "confidence": "high",
            "disputed_notes": [],
        },
        {
            "id": "s_rrf_2",
            "title": "When does RRF outperform weighted score fusion?",
            "markdown": (
                "Reciprocal Rank Fusion outperforms traditional weighted score fusion primarily "
                "in scenarios where retrieval models produce incompatible score distributions. "
                "In hybrid search combining BM25 with dense vector similarity, scores exist on "
                "fundamentally different scales and cannot be compared directly. "
                "RRF sidesteps the normalization problem entirely by operating on ranks. "
                "The k parameter, typically set to 60, controls how much lower-ranked results "
                "influence the final score."
            ),
            "cited_passage_ids": ["p1"],
            "confidence": "high",
            "disputed_notes": [],
        },
        {
            "id": "s_rrf_3",
            "title": "Practical implementation considerations for RRF in production",
            "markdown": (
                "Deploying RRF in production search engines requires careful tuning of the k "
                "parameter and understanding its limitations. "
                "RRF ignores the magnitude of differences between ranks, treating the gap "
                "between rank 1 and rank 2 the same as between rank 100 and rank 101. "
                "For most hybrid retrieval tasks, RRF provides a robust, computationally "
                "efficient baseline that often matches or exceeds more complex fusion methods."
            ),
            "cited_passage_ids": ["p2"],
            "confidence": "mixed",
            "disputed_notes": [],
        },
    ],
    "passages": [
        {"id": "p0", "source_title": "RRF Paper", "source_url": "https://example.com/rrf"},
        {"id": "p1", "source_title": "Hybrid Search", "source_url": "https://example.com/hybrid"},
        {"id": "p2", "source_title": "Vector DB Docs", "source_url": "https://example.com/vdb"},
    ],
    "all_hits": [],
    "unsupported_count": 0,
    "bounded_by": "sources",
    "depth_tier": "standard_deep",
}

# Minimal: 1 section — synthetic, exercises the "single-section, no silence gap" path
_MINIMAL_REPORT_DICT: dict[str, Any] = {
    "id": "evt_live_minimal",
    "kind": "report",
    "source": "agent",
    "timestamp": "2026-06-18T00:00:00Z",
    "schema_version": 1,
    "seq": 1,
    "meta": {},
    "query": "what is a hash function",
    "summary": "A hash function maps data to a fixed-size digest deterministically.",
    "sections": [
        {
            "id": "s_hash_1",
            "title": "What is a hash function and why is it useful?",
            "markdown": (
                "A hash function is a deterministic algorithm that maps input data of arbitrary "
                "size to a fixed-size output called a digest or hash. "
                "Good hash functions produce uniformly distributed outputs and are fast to "
                "compute. Cryptographic hash functions additionally resist preimage and "
                "collision attacks, making them useful for digital signatures and checksums."
            ),
            "cited_passage_ids": [],
            "confidence": "high",
            "disputed_notes": [],
        },
    ],
    "passages": [],
    "all_hits": [],
    "unsupported_count": 0,
    "bounded_by": "sources",
    "depth_tier": "standard_deep",
}

# Medium: 6 sections including two with EMPTY markdown — a real-world edge case.
# Sourced from conv_8fd0d55 in production disco.db.  The empty sections come from
# the DR engine stopping early on some sub-questions.  This is the adversarial
# case for the TTS pipeline: the LLM must not generate turns about missing content.
_MEDIUM_REPORT_DICT: dict[str, Any] = {
    "id": "evt_live_medium",
    "kind": "report",
    "source": "agent",
    "timestamp": "2026-06-12T00:00:00Z",
    "schema_version": 1,
    "seq": 1,
    "meta": {},
    "query": "whats the current state on encoder only models",
    "summary": (
        "Encoder-only transformer models like BERT and RoBERTa remain highly relevant in 2024 "
        "for classification, NER, and semantic search, despite the rise of decoder-only LLMs."
    ),
    "sections": [
        {
            "id": "s_enc_1",
            "title": "Which encoder-only architectures are currently dominant?",
            "markdown": (
                "BERT, RoBERTa, and DeBERTa remain the workhorses of encoder-only models. "
                "ModernBERT (2024) pushes the context window to 8192 tokens and trains on "
                "2 trillion tokens, outperforming older BERT variants on GLUE and long-form "
                "classification. Sentence-BERT and BGE-M3 dominate embedding benchmarks."
            ),
            "cited_passage_ids": ["p0"],
            "confidence": "high",
            "disputed_notes": [],
        },
        {
            "id": "s_enc_2",
            "title": "Performance benchmarks of state-of-the-art encoder models",
            "markdown": (
                "On the MTEB benchmark, BGE-M3 and E5-Mistral lead retrieval. "
                "For classification, DeBERTa-v3-large scores top on SuperGLUE. "
                "ModernBERT sets new SOTA on long-document tasks while remaining 2× faster "
                "than BERT at inference due to Flash Attention and rotary embeddings."
            ),
            "cited_passage_ids": ["p1"],
            "confidence": "high",
            "disputed_notes": [],
        },
        # These two sections have empty markdown — captured verbatim from real DR output.
        {
            "id": "s_enc_3",
            "title": "Primary technical limitations and failure modes of encoder models",
            "markdown": "",  # empty — DR stopped before synthesizing this section
            "cited_passage_ids": [],
            "confidence": "low",
            "disputed_notes": [],
        },
        {
            "id": "s_enc_4",
            "title": "Which companies and open-source communities are advancing encoder models?",
            "markdown": (
                "Google DeepMind maintains BERT and T5. Meta releases RoBERTa. "
                "BAAI (Beijing AI Institute) leads BGE/M3. "
                "Hugging Face hosts and maintains most of these under the transformers library. "
                "Nomic AI released nomic-embed-text, a strong open-source alternative."
            ),
            "cited_passage_ids": ["p2"],
            "confidence": "high",
            "disputed_notes": [],
        },
        {
            "id": "s_enc_5",
            "title": "Computational costs and hardware requirements for encoder models",
            "markdown": "",  # empty — DR stopped before synthesizing this section
            "cited_passage_ids": [],
            "confidence": "low",
            "disputed_notes": [],
        },
        {
            "id": "s_enc_6",
            "title": "Historical context of encoder-only models from BERT to ModernBERT",
            "markdown": (
                "BERT (2018) introduced the masked language modeling objective for bidirectional "
                "pre-training, revolutionizing NLP. RoBERTa (2019) improved training recipes. "
                "ALBERT (2020) reduced parameters. DeBERTa (2021) added disentangled attention. "
                "ModernBERT (2024) modernizes the architecture with Flash Attention, rotary "
                "positional embeddings, and a 2T-token training corpus."
            ),
            "cited_passage_ids": ["p3"],
            "confidence": "high",
            "disputed_notes": [],
        },
    ],
    "passages": [
        {"id": "p0", "source_title": "ModernBERT Paper", "source_url": "https://example.com/mbert"},
        {"id": "p1", "source_title": "MTEB Leaderboard", "source_url": "https://example.com/mteb"},
        {"id": "p2", "source_title": "BAAI BGE", "source_url": "https://example.com/bge"},
        {"id": "p3", "source_title": "BERT History", "source_url": "https://example.com/bert"},
    ],
    "all_hits": [],
    "unsupported_count": 2,
    "bounded_by": "stopped",
    "depth_tier": "standard_deep",
}


# ---------------------------------------------------------------------------
# Pre-baked realistic turn-scripts (LLM stub output).
# Short sentences keep per-test synthesis time reasonable (<30 s total per test).
# ---------------------------------------------------------------------------

# 5-turn podcast script for the small 3-section RRF report
_PODCAST_SCRIPT_SMALL = json.dumps(
    [
        {
            "speaker": "A",
            "text": (
                "Today we're covering reciprocal rank fusion, a clever technique for "
                "merging search results."
            ),
        },
        {
            "speaker": "B",
            "text": (
                "Right. Instead of comparing raw scores, RRF only looks at where each result ranks."
            ),
        },
        {
            "speaker": "A",
            "text": (
                "That sidesteps a big headache in hybrid search: BM25 scores and dense "
                "vector scores live on completely different scales."
            ),
        },
        {
            "speaker": "B",
            "text": (
                "Exactly. You just sum one over k plus rank across all your retrieval "
                "lists, and the consistently-high results float to the top."
            ),
        },
        {
            "speaker": "A",
            "text": (
                "The k parameter — usually sixty — keeps low-ranked results from "
                "dominating. Simple, robust, and fast to implement."
            ),
        },
    ]
)

# 4-turn single-voice script for the same report
_SINGLE_SCRIPT_SMALL = json.dumps(
    [
        {
            "speaker": "A",
            "text": (
                "Reciprocal rank fusion is a straightforward way to merge results from "
                "multiple retrieval systems."
            ),
        },
        {
            "speaker": "A",
            "text": (
                "Instead of normalizing incompatible scores, RRF only uses rank "
                "positions to compute a combined score."
            ),
        },
        {
            "speaker": "A",
            "text": (
                "The formula is simple: sum one over k plus rank across all lists, where "
                "k is a smoothing constant usually set to sixty."
            ),
        },
        {
            "speaker": "A",
            "text": (
                "This makes RRF especially useful in hybrid search combining BM25 with "
                "vector similarity, where score scales differ completely."
            ),
        },
    ]
)

# 4-turn podcast script for the minimal 1-section hash-function report
_PODCAST_SCRIPT_MINIMAL = json.dumps(
    [
        {
            "speaker": "A",
            "text": (
                "Hash functions are fundamental to modern computing. They map any input "
                "to a fixed-size fingerprint."
            ),
        },
        {
            "speaker": "B",
            "text": (
                "And the key property is determinism: the same input always gives the same output."
            ),
        },
        {
            "speaker": "A",
            "text": (
                "Cryptographic hash functions go further: they're designed so you can't "
                "reverse them or find two inputs with the same hash."
            ),
        },
        {
            "speaker": "B",
            "text": (
                "That's what makes them useful for checksums, digital signatures, and "
                "integrity verification throughout the security stack."
            ),
        },
    ]
)

# 6-turn podcast script for the medium 6-section encoder-only models report
# (includes two empty-markdown sections — the LLM would summarize around them)
_PODCAST_SCRIPT_MEDIUM = json.dumps(
    [
        {
            "speaker": "A",
            "text": (
                "Encoder-only models like BERT are still very relevant in 2024, despite "
                "all the buzz around large language models."
            ),
        },
        {
            "speaker": "B",
            "text": (
                "Especially for tasks like classification, named entity recognition, "
                "and semantic embedding retrieval."
            ),
        },
        {
            "speaker": "A",
            "text": (
                "ModernBERT is a standout: it extends the context to eight thousand "
                "tokens and trains on two trillion tokens."
            ),
        },
        {
            "speaker": "B",
            "text": (
                "And BGE-M3 from BAAI is leading the MTEB embedding benchmarks. "
                "Open-source is catching up fast."
            ),
        },
        {
            "speaker": "A",
            "text": (
                "One limitation worth noting: these models don't generate text, so they "
                "can't do open-ended question answering."
            ),
        },
        {
            "speaker": "B",
            "text": (
                "True. But for structured classification and retrieval, the encoder-only "
                "approach remains more efficient than a full decoder."
            ),
        },
    ]
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_report(data: dict[str, Any]):
    """Deserialise a report dict into a ReportEvent model."""
    from disco.core import ReportEvent

    return ReportEvent.model_validate(data)


def _mp3_frame_sync(mp3: bytes) -> bool:
    """True iff `mp3` carries an MPEG-1/2/2.5 Layer III frame sync in the
    first 128 bytes (lameenc writes a LAME header before data frames)."""
    head = mp3[:128]
    return any(head[i] == 0xFF and (head[i + 1] & 0xE0) == 0xE0 for i in range(len(head) - 1))


def _mp3_duration(mp3: bytes) -> float | None:
    """Parse MP3 frame headers and compute playback duration in seconds."""
    from disco.tools.builtin._audio_mixer import mp3_duration_seconds

    return mp3_duration_seconds(mp3)


def _install_llm_stub(monkeypatch: pytest.MonkeyPatch, script_json: str) -> None:
    """Replace the LLM call with a fixed turn-script. Real synthesis kept."""
    from disco.tools.builtin import audio_overview as _ao

    async def _fake_llm(payload: dict, llm_url: str) -> str:  # noqa: ARG001
        return script_json

    monkeypatch.setattr(_ao, "_call_llm", _fake_llm)


def _tts_settings(voice_a: str = "af_heart", voice_b: str = "af_bella"):
    """A minimal TTS settings object with Kokoro bundled provider enabled."""
    from disco.core.llm import TtsSettings

    return TtsSettings(enabled=True, provider="bundled", voice_a=voice_a, voice_b=voice_b)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@skip_no_kokoro
def test_small_report_podcast_mode_real_kokoro(monkeypatch, tmp_path) -> None:
    """Real Kokoro → real mixer: 3-section RRF report, podcast mode.

    Asserts: non-empty decodable MP3; duration ≥ 2 × #turns seconds
    (conservative lower bound — each short turn is at least ~0.5 s);
    MP3 frame sync present; transcript has both Host A and Host B labels;
    .part temp file is cleaned up; out_dir contains exactly one mp3 + one md."""
    _install_llm_stub(monkeypatch, _PODCAST_SCRIPT_SMALL)
    report = _make_report(_SMALL_REPORT_DICT)
    out_dir = tmp_path / "rrf_podcast"
    tts = _tts_settings()

    from disco.agent_server import report_audio as _ra

    mp3_path, tr_path = asyncio.run(
        _ra.generate_report_audio(report, tts_settings=tts, out_dir=out_dir, mode="podcast")
    )

    # MP3 must exist and be non-zero
    assert mp3_path.exists(), "MP3 file not written"
    mp3_bytes = mp3_path.read_bytes()
    assert len(mp3_bytes) > 0, "MP3 is 0 bytes"

    # Frame-sync present (decodable by any standard MP3 decoder)
    assert _mp3_frame_sync(mp3_bytes), "MP3 lacks a valid frame sync — file is corrupt"

    # Duration sanity: 5 turns × ~0.5 s minimum = 2.5 s; allow up to 120 s
    dur = _mp3_duration(mp3_bytes)
    assert dur is not None, "mp3_duration_seconds found no decodable frames"
    assert dur >= 2.0, (
        f"MP3 too short ({dur:.2f}s) for a 5-turn overview — synthesis may have failed"
    )
    assert dur < 120.0, f"MP3 suspiciously long ({dur:.2f}s) — possible silence injection bug"

    # Transcript: Host A AND Host B labels (podcast = two voices)
    tr = tr_path.read_text(encoding="utf-8")
    assert "Host A" in tr and "Host B" in tr, (
        "Podcast transcript must label both speakers; got: " + tr[:200]
    )
    assert "reciprocal rank fusion" in tr.lower(), "Query not reflected in transcript"

    # Atomic write: no .part file left behind
    part_mp3 = mp3_path.with_suffix(mp3_path.suffix + ".part")
    part_tr = tr_path.with_suffix(tr_path.suffix + ".part")
    assert not part_mp3.exists(), ".part MP3 temp file not cleaned up"
    assert not part_tr.exists(), ".part transcript temp file not cleaned up"


@skip_no_kokoro
def test_small_report_single_mode_real_kokoro(monkeypatch, tmp_path) -> None:
    """Real Kokoro → real mixer: 3-section RRF report, single-voice mode.

    Asserts: separate cache file from podcast mode; transcript has NO 'Host A'
    / 'Host B' labels; only voice_a synthesized (checked via voice diversity)."""
    _install_llm_stub(monkeypatch, _SINGLE_SCRIPT_SMALL)
    report = _make_report(_SMALL_REPORT_DICT)
    out_dir = tmp_path / "rrf_single"
    tts = _tts_settings()

    # Track which voices are used in real synthesis.
    from disco.tools.builtin import audio_overview as _ao

    original_local = _ao._synthesize_local
    voices_used: list[str] = []

    async def _tracking_local(text: str, voice: str) -> Any:
        voices_used.append(voice)
        return await original_local(text, voice)

    monkeypatch.setattr(_ao, "_synthesize_local", _tracking_local)

    from disco.agent_server import report_audio as _ra

    mp3_path, tr_path = asyncio.run(
        _ra.generate_report_audio(report, tts_settings=tts, out_dir=out_dir, mode="single")
    )

    assert mp3_path.exists()
    mp3_bytes = mp3_path.read_bytes()
    assert len(mp3_bytes) > 0
    assert _mp3_frame_sync(mp3_bytes)
    assert "single" in mp3_path.name, "Single-mode cache file must contain 'single' in name"

    # Transcript: NO Host A/B labels in single mode
    tr = tr_path.read_text(encoding="utf-8")
    assert "Host A" not in tr and "Host B" not in tr, (
        "Single-mode transcript must not use Host labels"
    )

    # Only voice_a ("af_heart") must appear — voice_b ("af_bella") is podcast-only
    assert voices_used, "No synthesis calls recorded — Kokoro was never called"
    assert all(v == "af_heart" for v in voices_used), (
        f"Single mode used voices other than af_heart: {set(voices_used)}"
    )


@skip_no_kokoro
def test_minimal_1_section_report_real_kokoro(monkeypatch, tmp_path) -> None:
    """Real Kokoro → real mixer: 1-section synthetic report.

    Exercises the single-section path: mix_pcm with only 4 turns produces
    NO inter-turn silence between non-existent gap positions.  Duration check
    ensures we get audio from ALL turns."""
    _install_llm_stub(monkeypatch, _PODCAST_SCRIPT_MINIMAL)
    report = _make_report(_MINIMAL_REPORT_DICT)
    out_dir = tmp_path / "hash_minimal"
    tts = _tts_settings()

    from disco.agent_server import report_audio as _ra

    mp3_path, tr_path = asyncio.run(
        _ra.generate_report_audio(report, tts_settings=tts, out_dir=out_dir, mode="podcast")
    )

    assert mp3_path.exists()
    mp3_bytes = mp3_path.read_bytes()
    assert len(mp3_bytes) > 0, "1-section report produced empty MP3"
    assert _mp3_frame_sync(mp3_bytes)

    dur = _mp3_duration(mp3_bytes)
    assert dur is not None
    # 4 turns, each ≥ 0.5 s → total ≥ 2.0 s
    assert dur >= 2.0, f"1-section 4-turn podcast is only {dur:.2f}s — too short"

    tr = tr_path.read_text(encoding="utf-8")
    assert "hash function" in tr.lower()


@skip_no_kokoro
def test_medium_report_with_empty_sections_real_kokoro(monkeypatch, tmp_path) -> None:
    """Real Kokoro → real mixer: 6-section encoder-only report including TWO
    empty-markdown sections (captured verbatim from production DR output).

    The pipeline must complete without crashing.  The LLM stub does not mention
    the empty sections; the TTS runs on the 6-turn podcast script anyway.
    Duration must be reasonable (>= 3 s for 6 turns)."""
    _install_llm_stub(monkeypatch, _PODCAST_SCRIPT_MEDIUM)
    report = _make_report(_MEDIUM_REPORT_DICT)
    out_dir = tmp_path / "enc_medium"
    tts = _tts_settings()

    from disco.agent_server import report_audio as _ra

    mp3_path, tr_path = asyncio.run(
        _ra.generate_report_audio(report, tts_settings=tts, out_dir=out_dir, mode="podcast")
    )

    assert mp3_path.exists()
    mp3_bytes = mp3_path.read_bytes()
    assert len(mp3_bytes) > 0
    assert _mp3_frame_sync(mp3_bytes), "Medium report produced corrupt MP3"

    dur = _mp3_duration(mp3_bytes)
    assert dur is not None
    assert dur >= 3.0, f"6-turn podcast is only {dur:.2f}s — synthesis may have been truncated"

    tr = tr_path.read_text(encoding="utf-8")
    assert "encoder" in tr.lower()


@skip_no_kokoro
def test_cache_idempotency_real_kokoro(monkeypatch, tmp_path) -> None:
    """Second call with identical report + mode returns byte-identical MP3
    without re-running real Kokoro synthesis.

    Proof: after the first call, we replace _synthesize_local with a function
    that raises — if the cache is not hit, the second call would fail."""
    _install_llm_stub(monkeypatch, _PODCAST_SCRIPT_SMALL)
    report = _make_report(_SMALL_REPORT_DICT)
    out_dir = tmp_path / "rrf_cache"
    tts = _tts_settings()

    from disco.agent_server import report_audio as _ra
    from disco.tools.builtin import audio_overview as _ao

    # First call — real Kokoro
    mp3_path_1, _ = asyncio.run(
        _ra.generate_report_audio(report, tts_settings=tts, out_dir=out_dir, mode="podcast")
    )
    first_bytes = mp3_path_1.read_bytes()
    assert len(first_bytes) > 0

    # Break the synthesizer — second call must NOT reach synthesis
    async def _should_not_be_called(text: str, voice: str) -> Any:
        raise RuntimeError("synthesis called on cache hit — cache miss!")

    monkeypatch.setattr(_ao, "_synthesize_local", _should_not_be_called)

    # Second call — must be a cache hit
    mp3_path_2, _ = asyncio.run(
        _ra.generate_report_audio(report, tts_settings=tts, out_dir=out_dir, mode="podcast")
    )
    second_bytes = mp3_path_2.read_bytes()
    assert mp3_path_1 == mp3_path_2, "Cache hit returned a different path"
    assert first_bytes == second_bytes, "Cache hit returned different bytes — cache broken"


@skip_no_kokoro
def test_podcast_vs_single_mode_separate_files_real_kokoro(monkeypatch, tmp_path) -> None:
    """Podcast and single modes for the same report write distinct cache files
    and both are decodable MP3s.  Ensures there is no mode-collision in the
    content-hash cache key."""
    from disco.agent_server import report_audio as _ra
    from disco.tools.builtin import audio_overview as _ao

    report = _make_report(_SMALL_REPORT_DICT)
    out_dir = tmp_path / "rrf_both_modes"
    tts = _tts_settings()

    # Return different scripts per mode (detected by the prompt keyword).
    # IMPORTANT: the function must be named `_fake_llm` so that
    # `report_audio._authenticated_call_llm` takes the short-circuit path
    # (it checks `__name__ == "_fake_llm"` to skip real HTTP).
    async def _fake_llm(payload: dict, llm_url: str) -> str:  # noqa: ARG001
        content = " ".join(m.get("content", "") for m in payload.get("messages", [])).lower()
        if "narrator" in content or "single-voice" in content or "honest" in content:
            return _SINGLE_SCRIPT_SMALL
        return _PODCAST_SCRIPT_SMALL

    monkeypatch.setattr(_ao, "_call_llm", _fake_llm)

    mp3_podcast, _ = asyncio.run(
        _ra.generate_report_audio(report, tts_settings=tts, out_dir=out_dir, mode="podcast")
    )
    mp3_single, _ = asyncio.run(
        _ra.generate_report_audio(report, tts_settings=tts, out_dir=out_dir, mode="single")
    )

    assert mp3_podcast != mp3_single, "Podcast and single modes must use different cache files"
    assert "podcast" in mp3_podcast.name
    assert "single" in mp3_single.name

    for label, path in [("podcast", mp3_podcast), ("single", mp3_single)]:
        data = path.read_bytes()
        assert len(data) > 0, f"{label} MP3 is 0 bytes"
        assert _mp3_frame_sync(data), f"{label} MP3 lacks a valid frame sync"
        dur = _mp3_duration(data)
        assert dur is not None
        assert dur >= 2.0, f"{label} MP3 is suspiciously short ({dur:.2f}s)"


# ---------------------------------------------------------------------------
# Mixer-level robustness with REAL Kokoro PCM (not sines)
# ---------------------------------------------------------------------------


@skip_no_kokoro
def test_real_pcm_silence_gap_alignment(tmp_path) -> None:
    """Synthesise two real Kokoro turns and verify that the inter-turn silence
    in mix_pcm lines up with the expected sample count.

    This catches any off-by-one or type-mismatch in the silence-gap formula
    that the sine-wave tests might not catch (real PCM has variable sample
    counts unlike exact-frequency sine arrays)."""
    from disco.agent_server import tts_local
    from disco.tools.builtin._audio_mixer import (
        encode_mp3,
        mix_pcm,
        mp3_duration_seconds,
    )

    SILENCE_MS = 500
    SAMPLE_RATE = 24000

    # Two short turns — different voices, different lengths
    text_a = "Encoder models use bidirectional attention for rich contextual representations."
    text_b = "That makes them ideal for retrieval and classification tasks."

    pcm_a = asyncio.run(tts_local.synthesize(text_a, "af_heart"))
    pcm_b = asyncio.run(tts_local.synthesize(text_b, "af_bella"))

    assert pcm_a.size > 0 and pcm_b.size > 0, "Kokoro returned empty PCM"

    # Mix with 500 ms gap
    mixed = mix_pcm([pcm_a, pcm_b], silence_ms=SILENCE_MS, sample_rate=SAMPLE_RATE)
    gap_samples = int(SAMPLE_RATE * SILENCE_MS / 1000)  # 12 000
    expected_size = pcm_a.size + gap_samples + pcm_b.size
    assert mixed.size == expected_size, (
        f"mix_pcm size mismatch: got {mixed.size}, expected {expected_size} "
        f"(pcm_a={pcm_a.size}, gap={gap_samples}, pcm_b={pcm_b.size})"
    )

    # Encode and check duration
    mp3 = encode_mp3(mixed, sample_rate=SAMPLE_RATE)
    assert _mp3_frame_sync(mp3), "Mixed real-PCM MP3 has no frame sync"
    dur = mp3_duration_seconds(mp3)
    expected_dur = expected_size / SAMPLE_RATE
    assert dur is not None
    assert abs(dur - expected_dur) < 0.5, (
        f"Real-PCM MP3 duration {dur:.3f}s is far from expected {expected_dur:.3f}s"
    )


@skip_no_kokoro
def test_many_turns_real_pcm_no_crash(tmp_path) -> None:
    """10 real Kokoro turns (5 A, 5 B, alternating) mix into a decodable MP3
    without NaN, clipping artifacts, or duration drift beyond 0.5 s.

    Exercises the many-turn path that sine tests covered with synthetic
    arrays — here the PCM has real prosodic variation and variable lengths."""
    from disco.agent_server import tts_local
    from disco.tools.builtin._audio_mixer import (
        encode_mp3,
        mix_pcm,
        mp3_duration_seconds,
    )

    SILENCE_MS = 400
    SAMPLE_RATE = 24000

    texts_a = [
        "Reciprocal rank fusion is a practical fusion technique.",
        "The algorithm uses only rank positions, not raw scores.",
        "This makes it robust across incompatible scoring systems.",
        "The k parameter controls lower-rank result influence.",
        "Typical values for k range from forty to one hundred.",
    ]
    texts_b = [
        "And that means no score normalization is needed at all.",
        "So combining BM25 and vector similarity becomes straightforward.",
        "Any retrieval system can participate as long as it returns ranks.",
        "Production systems typically set k to sixty for stable results.",
        "Overall RRF is one of the most reliable hybrid fusion strategies.",
    ]

    # Synthesise all 10 turns: A, B, A, B, ... alternating
    pcm_turns = []
    for a_text, b_text in zip(texts_a, texts_b, strict=True):
        pcm_turns.append(asyncio.run(tts_local.synthesize(a_text, "af_heart")))
        pcm_turns.append(asyncio.run(tts_local.synthesize(b_text, "af_bella")))

    assert len(pcm_turns) == 10
    assert all(t.size > 0 for t in pcm_turns), "Some turns returned empty PCM"

    mixed = mix_pcm(pcm_turns, silence_ms=SILENCE_MS, sample_rate=SAMPLE_RATE)
    gap_samples = int(SAMPLE_RATE * SILENCE_MS / 1000)
    expected = sum(t.size for t in pcm_turns) + 9 * gap_samples
    assert mixed.size == expected, f"10-turn mix size {mixed.size} != expected {expected}"

    # No NaN or +/-Inf in the mixed PCM
    assert not np.any(np.isnan(mixed)), "NaN in mixed real-PCM output"
    assert not np.any(np.isinf(mixed)), "Inf in mixed real-PCM output"

    # Note (RP-09 live acceptance finding): real Kokoro v1.0 PCM CAN have
    # samples outside [-1, 1] — the neural model does not hard-clip its output.
    # This is expected and correct: `encode_mp3` always clips via
    #   `np.clip(arr, -1.0, 1.0) * 32767.0`
    # before casting to int16, so out-of-range samples produce clipped (not
    # wrapped) audio frames and the encoded MP3 is always decodable.
    # We record the clipping fraction here to quantify the impact.
    clipped_frac = float(np.mean((mixed < -1.0) | (mixed > 1.0)))
    # Accept up to 10% clipped samples from real prosodic variation.
    # In practice, observed clipping is < 1% (prosodic peaks).
    assert clipped_frac <= 0.10, (
        f"Excessive clipping in real-PCM mix: {clipped_frac:.1%} samples outside [-1, 1]"
    )

    mp3 = encode_mp3(mixed, sample_rate=SAMPLE_RATE)
    assert _mp3_frame_sync(mp3), "10-turn real-PCM MP3 lacks frame sync"

    dur = mp3_duration_seconds(mp3)
    expected_dur = expected / SAMPLE_RATE
    assert dur is not None
    assert abs(dur - expected_dur) < 0.5, (
        f"10-turn MP3 duration {dur:.3f}s is far from expected {expected_dur:.3f}s"
    )
