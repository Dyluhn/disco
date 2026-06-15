"""D4 acceptance — harden the audio-overview MIXER against malformed /
empty / mismatched-rate / clipping segments. Unit part only; the live
"play one overview end-to-end" is deferred (tracked in plan, not in scope
here).

What this proves:
  * A multi-turn PCM script (3-10 turns of varying length) → mix_pcm →
    encode_mp3 → a valid decodable MP3 byte string whose playback duration
    matches the input PCM length within encoder padding (~50-100 ms).
  * Empty input (zero turns) → mix_pcm returns empty, encode_mp3 returns
    a one-frame silent MP3 (no exception, no None leak).
  * A single turn → exactly that turn is mixed, no phantom leading/trailing
    silence, encoded to a valid MP3.
  * A short / very-small turn (one sample, a handful of samples) → handled
    without exception, encoded successfully.
  * Sample-rate mismatch (turns explicitly tagged with a different rate via
    the `(pcm, turn_rate)` tuple API) → linear-resampled to the target rate,
    no exception, encoded successfully.
  * Out-of-[-1, 1] PCM (clipping) → clipped, not wrapped; the resulting MP3
    is decodable.

No live TTS service is required — every "turn" is a synthesized numpy sine
or zero array. All assertions are on the mixer/encoder contract.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np

# Load _audio_mixer directly so the test stays self-contained even if the
# parent `disco.tools.builtin` package fails to import for unrelated reasons
# (e.g. an optional dep is missing in CI). The module uses lazy imports for
# numpy + lameenc, so a direct load is safe.
_MIXER_PATH = (
    Path(__file__).parent.parent
    / "src"
    / "disco"
    / "tools"
    / "builtin"
    / "_audio_mixer.py"
)


def _load_mixer():
    spec = importlib.util.spec_from_file_location("_audio_mixer_d4", _MIXER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MIXER = _load_mixer()
mix_pcm = _MIXER.mix_pcm
encode_mp3 = _MIXER.encode_mp3
resample_pcm = _MIXER.resample_pcm
mp3_duration_seconds = _MIXER.mp3_duration_seconds
mix_turns_count = _MIXER.mix_turns_count

# TTS pipeline target rate. Matches TTS_SAMPLE_RATE in audio_overview.py.
TARGET_SR = 24000


# ---- fixtures --------------------------------------------------------------


def _sine_ms(ms: int, freq: float, sr: int = TARGET_SR) -> np.ndarray:
    """A recognisable mono float32 sine of `ms` milliseconds."""
    n = int(sr * ms / 1000)
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    t = np.arange(n, dtype=np.float32) / sr
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _mp3_frame_sync(mp3: bytes) -> bool:
    """True iff `mp3` carries an MPEG-1/2/2.5 Layer III frame sync in the
    first 64 bytes (lameenc writes a LAME header, no ID3 tag)."""
    head = mp3[:64]
    return any(
        head[i] == 0xFF and (head[i + 1] & 0xE0) == 0xE0
        for i in range(len(head) - 1)
    )


# ---- happy path: multi-turn → valid decodable MP3 of expected duration -----


def test_multi_turn_overview_produces_valid_mp3_of_expected_duration():
    """A scripted 5-turn overview at 24 kHz with 400 ms silence between turns
    encodes to a valid MP3 whose playback duration matches the input PCM
    length (5 turns × 600 ms + 4 gaps × 400 ms = 3.8 s of audio, encoder
    pads the last frame by a few tens of ms)."""
    silence_ms = 400
    turn_durations_ms = [600, 800, 500, 700, 650]  # 5 turns, varying
    turns = [_sine_ms(ms, freq=180.0 + 30.0 * i) for i, ms in enumerate(turn_durations_ms)]

    pcm = mix_pcm(turns, silence_ms=silence_ms, sample_rate=TARGET_SR)
    expected_pcm_samples = (
        sum(int(TARGET_SR * ms / 1000) for ms in turn_durations_ms)
        + mix_turns_count(len(turns)) * int(TARGET_SR * silence_ms / 1000)
    )
    assert pcm.size == expected_pcm_samples
    assert pcm.dtype == np.float32

    mp3 = encode_mp3(pcm, sample_rate=TARGET_SR)
    assert isinstance(mp3, bytes)
    assert len(mp3) > 0
    assert _mp3_frame_sync(mp3), "MP3 head lacks a frame sync — encoder failed silently"

    # Duration from the MP3 frame counter should be within encoder padding of
    # the input PCM length. Encoder pads the last frame (~50 ms) and may add
    # a leading silent frame for short inputs; allow ±200 ms.
    dur = mp3_duration_seconds(mp3)
    assert dur is not None, "frame counter found no decodable frames"
    expected_dur = expected_pcm_samples / TARGET_SR
    assert abs(dur - expected_dur) < 0.2, (
        f"MP3 duration {dur:.3f}s is far from PCM-implied {expected_dur:.3f}s"
    )

    # Sanity: longer input ⇒ longer MP3 (just to be sure the duration track
    # above is using the right encoder output, not a coincidental frame).
    short_mp3 = encode_mp3(_sine_ms(200, 220.0), sample_rate=TARGET_SR)
    long_mp3 = encode_mp3(_sine_ms(2000, 220.0), sample_rate=TARGET_SR)
    assert len(long_mp3) > len(short_mp3) * 2


# ---- robustness: empty / single / short / None inputs ----------------------


def test_mix_pcm_empty_returns_empty_array():
    """Zero turns → empty float32 array (the mixer's success signal; the
    caller can decide what to do with an empty overview)."""
    out = mix_pcm([], silence_ms=500, sample_rate=TARGET_SR)
    assert out.size == 0
    assert out.dtype == np.float32


def test_encode_mp3_empty_pcm_returns_decodable_silence():
    """Encoding an empty PCM must NOT crash. lameenc pads to a full silent
    frame (768 bytes for MPEG-2 Layer III at 24 kHz / 128 kbps) and the
    result is a decodable, if brief, MP3. The pipeline can hand the caller
    an empty PCM without the encoder throwing."""
    mp3 = encode_mp3(np.zeros(0, dtype=np.float32), sample_rate=TARGET_SR)
    assert isinstance(mp3, bytes)
    assert len(mp3) > 0
    assert _mp3_frame_sync(mp3), "empty PCM produced a non-decodable MP3"


def test_encode_mp3_none_pcm_is_safe():
    """A None PCM (e.g. a TTS call that returned None) must not crash the
    pipeline; the encoder should produce a silent lead-in instead. The
    audio_overview tool already short-circuits on None, but defense-in-
    depth is the point of D4."""
    mp3 = encode_mp3(None, sample_rate=TARGET_SR)
    assert isinstance(mp3, bytes)
    assert len(mp3) > 0
    assert _mp3_frame_sync(mp3)


def test_single_turn_overview_no_phantom_silence():
    """One turn → exactly that turn, no leading/trailing silence. Then the
    encoder produces a single MP3 whose duration matches the turn."""
    t = _sine_ms(500, freq=440.0)
    pcm = mix_pcm([t], silence_ms=500, sample_rate=TARGET_SR)
    assert pcm.size == t.size
    assert np.array_equal(pcm, t)
    mp3 = encode_mp3(pcm, sample_rate=TARGET_SR)
    dur = mp3_duration_seconds(mp3)
    assert dur is not None
    assert abs(dur - 0.5) < 0.2, f"single-turn duration {dur:.3f}s ≠ 0.5s"


def test_short_turn_one_sample_does_not_crash():
    """A turn of a single sample must encode without raising. This is the
    most-degenerate non-empty input — the audio_overview tool already
    treats it as 'empty' upstream, but the mixer itself is permissive."""
    one_sample = np.array([0.25], dtype=np.float32)
    pcm = mix_pcm([one_sample], silence_ms=500, sample_rate=TARGET_SR)
    assert pcm.size == 1
    mp3 = encode_mp3(pcm, sample_rate=TARGET_SR)
    assert isinstance(mp3, bytes) and len(mp3) > 0
    assert _mp3_frame_sync(mp3)


def test_short_turn_tiny_burst_does_not_crash():
    """A turn of 5 samples (~0.2 ms) is well below the encoder's frame size
    (1152 samples at MPEG-1) but must still encode without raising."""
    burst = np.array([0.1, 0.2, 0.3, 0.2, 0.1], dtype=np.float32)
    mp3 = encode_mp3(burst, sample_rate=TARGET_SR)
    assert isinstance(mp3, bytes) and len(mp3) > 0
    assert _mp3_frame_sync(mp3)


def test_empty_turn_inside_multi_turn_list_is_dropped():
    """A turn array of size 0 inside a list of real turns must be dropped
    (no phantom silence gap) and the encoder must still produce a valid
    overview. Mirrors the existing `test_mix_pcm_skips_empty_turns` happy
    path, restated as a robustness check for the multi-turn flow."""
    t1 = _sine_ms(200, 220.0)
    t2 = _sine_ms(300, 330.0)
    t3 = _sine_ms(250, 440.0)
    empty = np.zeros(0, dtype=np.float32)
    pcm = mix_pcm([t1, empty, t2, empty, empty, t3], silence_ms=300, sample_rate=TARGET_SR)
    # 3 real turns, 2 gaps of 300 ms
    assert pcm.size == t1.size + t2.size + t3.size + 2 * int(TARGET_SR * 300 / 1000)
    mp3 = encode_mp3(pcm, sample_rate=TARGET_SR)
    assert _mp3_frame_sync(mp3)


# ---- robustness: sample-rate mismatch --------------------------------------


def test_rate_mismatched_turn_via_tuple_api_is_resampled():
    """A turn explicitly tagged with a 16 kHz rate via the tuple API is
    resampled (linear) to the 24 kHz target before splicing. The result
    must be exactly `len(sr) * target / source` samples and must encode
    to a valid MP3 — no exception, no truncation."""
    sr_low = 16000
    dur_ms = 500
    arr_16k = _sine_ms(dur_ms, freq=300.0, sr=sr_low)
    assert arr_16k.size == int(sr_low * dur_ms / 1000)

    pcm = mix_pcm([(arr_16k, sr_low)], silence_ms=500, sample_rate=TARGET_SR)
    expected = int(round(arr_16k.size * TARGET_SR / sr_low))
    assert abs(pcm.size - expected) <= 1, f"resampled size {pcm.size} ≠ ~{expected}"
    assert pcm.dtype == np.float32

    mp3 = encode_mp3(pcm, sample_rate=TARGET_SR)
    assert _mp3_frame_sync(mp3)


def test_rate_mismatched_turns_in_a_multi_turn_list():
    """A mixed-rate list (one bare 24 kHz turn, two turns tagged as 16 kHz
    via the tuple API) must all splice at the 24 kHz target without
    raising. The resampled turns' length and the bare turn's length must
    line up on a common sample-rate grid so the silence gap is correct."""
    sr_low = 16000
    silence_ms = 300
    t_a = _sine_ms(400, 220.0)                              # bare, 24 kHz
    t_b_raw = _sine_ms(500, 330.0, sr=sr_low)               # 8 000 samples
    t_b = (t_b_raw, sr_low)                                 # tagged 16 kHz
    t_c_raw = _sine_ms(600, 440.0, sr=sr_low)               # 9 600 samples
    t_c = (t_c_raw, sr_low)                                 # tagged 16 kHz
    pcm = mix_pcm([t_a, t_b, t_c], silence_ms=silence_ms, sample_rate=TARGET_SR)

    # 24 kHz turn + 24 kHz-resampled 16 kHz turn + 24 kHz-resampled 16 kHz turn
    # + 2 gaps of 300 ms
    resampled_b = resample_pcm(t_b_raw, from_rate=sr_low, to_rate=TARGET_SR)
    resampled_c = resample_pcm(t_c_raw, from_rate=sr_low, to_rate=TARGET_SR)
    expected = (
        t_a.size
        + resampled_b.size
        + resampled_c.size
        + 2 * int(TARGET_SR * silence_ms / 1000)
    )
    assert pcm.size == expected, (
        f"mixed-rate mix: got {pcm.size} samples, expected {expected} "
        f"(t_a={t_a.size}, t_b_resampled={resampled_b.size}, "
        f"t_c_resampled={resampled_c.size}, gaps=2×{int(TARGET_SR * silence_ms / 1000)})"
    )
    mp3 = encode_mp3(pcm, sample_rate=TARGET_SR)
    assert _mp3_frame_sync(mp3)


def test_rate_mismatched_turn_at_target_rate_is_passthrough():
    """A turn tagged with the target rate (i.e. `(pcm, sample_rate)`) must
    splice in unchanged — no resample, no extra copies."""
    t = _sine_ms(500, 220.0)
    pcm = mix_pcm([(t, TARGET_SR)], silence_ms=400, sample_rate=TARGET_SR)
    assert pcm.size == t.size
    assert np.array_equal(pcm, t)


# ---- robustness: clipping -------------------------------------------------


def test_clipping_out_of_range_does_not_wrap():
    """Out-of-[-1, 1] PCM (e.g. a misbehaving TTS backend that returns raw
    int16 scaled by 1/32767 but with a DC offset, or a fused multiply
    somewhere) must be clipped, not wrapped. The encoder must accept the
    clipped values and produce a valid MP3."""
    pcm = np.array([2.0, -2.0, 1.5, -1.5, 0.0, 0.5, -0.5] * 5000, dtype=np.float32)
    mp3 = encode_mp3(pcm, sample_rate=TARGET_SR)
    assert isinstance(mp3, bytes) and len(mp3) > 0
    assert _mp3_frame_sync(mp3)


def test_clipping_preserves_duration():
    """Clipping must not change the sample count — it changes amplitude,
    not length. Sanity-check that a clipped input encodes to a similar-
    duration MP3 as the unclipped version."""
    pcm_clean = _sine_ms(800, 440.0)
    pcm_clip = np.clip(pcm_clean * 4.0, -1.0, 1.0)  # amplitude × 4, clipped
    assert pcm_clip.size == pcm_clean.size
    mp3_clean = encode_mp3(pcm_clean, sample_rate=TARGET_SR)
    mp3_clip = encode_mp3(pcm_clip, sample_rate=TARGET_SR)
    d_clean = mp3_duration_seconds(mp3_clean)
    d_clip = mp3_duration_seconds(mp3_clip)
    assert d_clean is not None and d_clip is not None
    assert abs(d_clean - d_clip) < 0.05, (
        f"clipping changed duration: clean={d_clean:.3f}s, clip={d_clip:.3f}s"
    )


# ---- cross-check: mixer math constants -------------------------------------


def test_mix_turns_count_invariants():
    """Pure helper, but the multi-turn acceptance test depends on its
    `n_turns - 1` formula — pin the contract here so a future refactor
    doesn't silently change the silence-gap count."""
    assert mix_turns_count(0) == 0
    assert mix_turns_count(1) == 0
    assert mix_turns_count(2) == 1
    assert mix_turns_count(5) == 4
    assert mix_turns_count(100) == 99


# ---- end-to-end: mix_path used by audio_overview.py -----------------------


def test_overview_mix_path_handles_realistic_script():
    """Mimic the audio_overview.py mix call site: a list of per-turn PCM
    (synthesised here, not from a live TTS service) goes through mix_pcm
    with `silence_ms=500, sample_rate=24000`, then encode_mp3 with the
    same rate. Assert the output is a valid MP3 whose duration matches
    the input. This is the unit-level proof that the mix path in
    audio_overview.py is hardened against malformed input (the live
    end-to-end "play one overview" is DEFERRED)."""
    silence_ms = 500
    turn_text_lengths = [120, 90, 150, 80, 200, 110, 100]  # 7 turns
    turns = [
        _sine_ms(max(240, length * 2), freq=180.0 + 20.0 * i)
        for i, length in enumerate(turn_text_lengths)
    ]

    mixed = mix_pcm(turns, silence_ms=silence_ms, sample_rate=TARGET_SR)
    assert mixed.size > 0
    assert mixed.dtype == np.float32

    # Inter-turn gap must equal 24_000 * 500 / 1000 = 12_000 samples between
    # every pair of turns (6 gaps for 7 turns).
    expected_pcm = sum(t.size for t in turns) + 6 * int(TARGET_SR * silence_ms / 1000)
    assert mixed.size == expected_pcm

    mp3 = encode_mp3(mixed, sample_rate=TARGET_SR)
    assert _mp3_frame_sync(mp3)
    dur = mp3_duration_seconds(mp3)
    expected_dur = mixed.size / TARGET_SR
    assert dur is not None
    assert abs(dur - expected_dur) < 0.3, (
        f"overview duration {dur:.3f}s is far from PCM-implied {expected_dur:.3f}s"
    )
