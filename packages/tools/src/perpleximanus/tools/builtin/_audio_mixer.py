"""PCM mixer for audio overviews — concatenate per-turn PCM with inter-turn
silence, then encode the WHOLE overview to MP3 once.

This replaces the old MP3-frame-concatenation approach, which hand-built MPEG
frames and broke when turns and silence had different sample rates (44.1 kHz
silence vs 24 kHz Kokoro turns → decoder glitches). Mixing in PCM and encoding
once is simpler and correct: every turn is float32 mono PCM at one sample rate,
we splice silence as zeros, and `lameenc` encodes the result a single time.

numpy + lameenc are lazy-imported inside the functions so the tools package stays
importable without the optional `tts` extra installed (audio_overview only needs
them when it actually synthesizes).
"""

from __future__ import annotations

from typing import Any


def mix_pcm(turns: list[Any], *, silence_ms: int, sample_rate: int) -> Any:
    """Concatenate per-turn mono float32 PCM with `silence_ms` of silence between
    turns. Returns a single float32 numpy array. Empty input → empty array."""
    import numpy as np

    arrays = [np.asarray(t, dtype=np.float32).reshape(-1) for t in turns]
    arrays = [a for a in arrays if a.size]
    if not arrays:
        return np.zeros(0, dtype=np.float32)
    gap = np.zeros(max(0, int(sample_rate * silence_ms / 1000)), dtype=np.float32)
    out: list[Any] = []
    for i, a in enumerate(arrays):
        if i:
            out.append(gap)
        out.append(a)
    return np.concatenate(out)


def encode_mp3(pcm: Any, *, sample_rate: int, bitrate_kbps: int = 128) -> bytes:
    """Encode mono float32 PCM in [-1, 1] to MP3 bytes via lameenc (one pass)."""
    import lameenc
    import numpy as np

    pcm16 = (np.clip(np.asarray(pcm, dtype=np.float32), -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    enc = lameenc.Encoder()
    enc.set_bit_rate(bitrate_kbps)
    enc.set_in_sample_rate(int(sample_rate))
    enc.set_channels(1)
    enc.set_quality(2)  # 0=best/slowest .. 9=worst/fastest
    # lameenc returns bytearray; coerce to bytes so downstream write_file / length
    # checks see an immutable bytes object (the tool's structured output asserts it).
    return bytes(enc.encode(pcm16)) + bytes(enc.flush())


def mix_turns_count(n_turns: int) -> int:
    """Expected number of inter-turn silence gaps for N turns (N-1, min 0)."""
    return max(0, n_turns - 1)
