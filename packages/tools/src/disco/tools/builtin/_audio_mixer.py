"""PCM mixer for audio overviews — concatenate per-turn PCM with inter-turn
silence, then encode the WHOLE overview to MP3 once.

This replaces the old MP3-frame-concatenation approach, which hand-built MPEG
frames and broke when turns and silence had different sample rates (44.1 kHz
silence vs 24 kHz Kokoro turns → decoder glitches). Mixing in PCM and encoding
once is simpler and correct: every turn is float32 mono PCM at one sample rate,
we splice silence as zeros, and `lameenc` encodes the result a single time.

Hardened to handle malformed input gracefully (D4 — never crash the loop):
  * empty / missing / non-ndarray PCM → coerced or skipped, never raised
  * single-turn overview → just that turn, no leading/trailing silence
  * sample-rate mismatch (turns at rates other than the target) → linear-
    interpolation resample to the target rate (turns may be bare arrays at
    `sample_rate` OR `(pcm, turn_rate)` tuples)
  * clipping (PCM outside [-1, 1]) → clipped, not wrapped
  * empty input → empty/silence-only MP3 bytes (not a crash)

numpy + lameenc are lazy-imported inside the functions so the tools package stays
importable even where the bundled TTS deps aren't present (audio_overview only needs
them when it actually synthesizes).
"""

from __future__ import annotations

from typing import Any

# MPEG Layer III samples-per-frame, indexed by MPEG version ID from the frame
# header byte 1 (bits 4-3): 0b11 → MPEG-1 (1152), 0b10 → MPEG-2 (576),
# 0b00 → MPEG-2.5 (576). MPEG-1 is double MPEG-2/2.5.
_MPEG_L3_SAMPLES_PER_FRAME_MPEG1 = 1152
_MPEG_L3_SAMPLES_PER_FRAME_MPEG2 = 576

# Bitrate tables (kbps) for Layer III, indexed by the 4-bit bitrate index in
# frame header byte 2 bits 7-4. Index 0 and 15 are "free" / "bad" → ignored.
_MPEG1_L3_BITRATE_KBPS = (
    None, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, None,
)
_MPEG2_L3_BITRATE_KBPS = (
    None, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None,
)

# Sample-rate tables (Hz) indexed by the 2-bit samplerate index in frame header
# byte 2 bits 3-2.  Index 3 is "reserved" → invalid.
_MPEG1_SR_HZ = (44100, 48000, 32000, None)
_MPEG2_SR_HZ = (22050, 24000, 16000, None)
_MPEG25_SR_HZ = (11025, 12000, 8000, None)


def _coerce_pcm(pcm: Any) -> Any:
    """Coerce a turn's PCM into a 1-D float32 ndarray. Returns an empty array
    for None / empty input so the caller can filter with `if a.size`."""
    import numpy as np

    if pcm is None:
        return np.zeros(0, dtype=np.float32)
    try:
        arr = np.asarray(pcm, dtype=np.float32)
    except (TypeError, ValueError):
        return np.zeros(0, dtype=np.float32)
    return arr.reshape(-1)


def resample_pcm(pcm: Any, *, from_rate: int, to_rate: int) -> Any:
    """Linear-interpolation resample of mono float32 PCM from `from_rate` Hz to
    `to_rate` Hz. No scipy dependency — pure numpy. Returns a new float32
    ndarray; the input is not modified.

    Used by `mix_pcm` to splice rate-mismatched turns back onto the overview's
    target rate (a single sample rate is required by the MP3 encoder). This is
    a low-quality resampler (linear in time-domain) but it is correct enough
    for inter-turn splices — turns are typically short, and the per-turn
    resampling happens before the mix, so the audible artifact is at most a
    few percent pitch drift on that turn, far better than pitch-shifting the
    whole overview.
    """
    import numpy as np

    if from_rate == to_rate or from_rate <= 0 or to_rate <= 0:
        return _coerce_pcm(pcm)
    arr = _coerce_pcm(pcm)
    if arr.size < 2:
        return arr
    duration_s = arr.size / float(from_rate)
    n_out = max(1, int(round(duration_s * to_rate)))
    # Map output sample i → source time (i + 0.5) / to_rate, then to source
    # index.  Centre-of-bin mapping avoids the half-sample bias of edge
    # alignment; interpolation is linear between the two surrounding samples.
    t = (np.arange(n_out, dtype=np.float64) + 0.5) * from_rate / to_rate - 0.5
    t = np.clip(t, 0.0, arr.size - 1.0)
    lo = np.floor(t).astype(np.int64)
    hi = np.minimum(lo + 1, arr.size - 1)
    frac = (t - lo).astype(np.float32)
    out = (1.0 - frac) * arr[lo] + frac * arr[hi]
    return out.astype(np.float32)


def mix_pcm(turns: list[Any], *, silence_ms: int, sample_rate: int) -> Any:
    """Concatenate per-turn mono float32 PCM with `silence_ms` of silence between
    turns. Returns a single float32 numpy array at `sample_rate` Hz.

    Each item in `turns` may be either:
      * a bare numpy ndarray (or array-like) at `sample_rate` Hz
      * a `(pcm, turn_rate)` tuple, in which case the turn is resampled
        (linear) to `sample_rate` before splicing

    Empty / None / non-numeric turns are coerced to zero-length arrays and
    dropped, so they don't create phantom silence gaps. Empty input → empty
    array. Single turn → exactly that turn (no leading/trailing silence).
    """
    import numpy as np

    out_parts: list[Any] = []
    for t in turns:
        if isinstance(t, tuple) and len(t) == 2:
            pcm, turn_rate = t
            try:
                turn_rate_i = int(turn_rate)
            except (TypeError, ValueError):
                turn_rate_i = int(sample_rate)
            if turn_rate_i != sample_rate:
                arr = resample_pcm(pcm, from_rate=turn_rate_i, to_rate=int(sample_rate))
            else:
                arr = _coerce_pcm(pcm)
        else:
            arr = _coerce_pcm(t)
        if arr.size:
            out_parts.append(arr)
    if not out_parts:
        return np.zeros(0, dtype=np.float32)
    gap_n = max(0, int(int(sample_rate) * int(silence_ms) / 1000))
    gap = np.zeros(gap_n, dtype=np.float32) if gap_n else None
    parts: list[Any] = []
    for i, a in enumerate(out_parts):
        if i and gap is not None:
            parts.append(gap)
        parts.append(a)
    return np.concatenate(parts)


def encode_mp3(pcm: Any, *, sample_rate: int, bitrate_kbps: int = 128) -> bytes:
    """Encode mono float32 PCM in [-1, 1] to MP3 bytes via lameenc (one pass).

    Hardened:
      * `pcm is None` or non-numeric → returns a one-frame silent MP3 of the
        expected length for the target sample rate (so callers can blindly
        encode a missing turn without crashing the pipeline)
      * empty PCM → same (a brief silent lead-in; lameenc itself pads to a
        full frame, so the output is non-zero and decodable)
      * out-of-[-1, 1] samples → clipped, never wrapped
      * returns `bytes` (not `bytearray`) for downstream write_file / length
        checks
    """
    import lameenc
    import numpy as np

    arr = _coerce_pcm(pcm) if pcm is not None else np.zeros(0, dtype=np.float32)
    if arr.size:
        pcm16 = (np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    else:
        # lameenc requires a bytes-like input; feed one silent frame's worth
        # of zeros so the encoder emits a valid (silent) MPEG frame rather
        # than an empty buffer that downstream decoders reject.
        silent_n = max(1, int(int(sample_rate) * 1152 / 24000))
        pcm16 = b"\x00\x00" * silent_n
    enc = lameenc.Encoder()
    enc.set_bit_rate(int(bitrate_kbps))
    enc.set_in_sample_rate(int(sample_rate))
    enc.set_channels(1)
    enc.set_quality(2)  # 0=best/slowest .. 9=worst/fastest
    return bytes(enc.encode(pcm16)) + bytes(enc.flush())


def mix_turns_count(n_turns: int) -> int:
    """Expected number of inter-turn silence gaps for N turns (N-1, min 0)."""
    return max(0, n_turns - 1)


def mp3_duration_seconds(mp3_bytes: bytes) -> float | None:
    """Compute the playback duration of an MPEG-1/2/2.5 Layer III MP3 byte
    string by scanning for frame syncs and summing `samples_per_frame /
    sample_rate` across frames. Returns None if no decodable frame is found
    (caller can fall back to PCM length). For our encoder (lameenc) this
    matches the input PCM length within encoder padding (a few hundredths of
    a second at most)."""
    i = 0
    n = len(mp3_bytes)
    total_samples = 0
    last_sr: int | None = None
    while i + 4 <= n:
        if mp3_bytes[i] != 0xFF or (mp3_bytes[i + 1] & 0xE0) != 0xE0:
            i += 1
            continue
        b1, b2 = mp3_bytes[i + 1], mp3_bytes[i + 2]
        ver = (b1 >> 3) & 0x03
        br_idx = (b2 >> 4) & 0x0F
        sr_idx = (b2 >> 2) & 0x03
        pad = (b2 >> 1) & 0x01
        if ver == 0b11:
            br_kbps = _MPEG1_L3_BITRATE_KBPS[br_idx]
            sr_hz = _MPEG1_SR_HZ[sr_idx]
            spf = _MPEG_L3_SAMPLES_PER_FRAME_MPEG1
        elif ver == 0b10:
            br_kbps = _MPEG2_L3_BITRATE_KBPS[br_idx]
            sr_hz = _MPEG2_SR_HZ[sr_idx]
            spf = _MPEG_L3_SAMPLES_PER_FRAME_MPEG2
        elif ver == 0b00:
            br_kbps = _MPEG2_L3_BITRATE_KBPS[br_idx]
            sr_hz = _MPEG25_SR_HZ[sr_idx]
            spf = _MPEG_L3_SAMPLES_PER_FRAME_MPEG2
        else:
            i += 1
            continue
        if br_kbps is None or sr_hz is None:
            i += 1
            continue
        frame_bytes = (144 * br_kbps * 1000) // sr_hz + pad
        if ver != 0b11:
            frame_bytes = (72 * br_kbps * 1000) // sr_hz + pad
        if frame_bytes <= 0 or i + frame_bytes > n:
            i += 1
            continue
        total_samples += spf
        last_sr = sr_hz
        i += frame_bytes
    if total_samples == 0 or last_sr is None:
        return None
    return total_samples / float(last_sr)
