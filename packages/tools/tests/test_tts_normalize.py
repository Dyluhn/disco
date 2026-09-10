from __future__ import annotations

import io
import wave
from typing import Any

import numpy as np
import pytest
from disco.tools.builtin._tts_normalize import normalize_tts_text
from hypothesis import given, settings
from hypothesis import strategies as st


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("## The Main Focus", "The Main Focus."),
        ("**Key point** and _supporting detail_.", "Key point and supporting detail."),
        ("The item weighs 200 lbs.", "The item weighs 200 pounds."),
        (
            "The result improved [1] and stayed stable [23].",
            "The result improved and stayed stable.",
        ),
        ("- First item\n- Second item", "First item. Second item."),
        ("Before.\n```python\nprint('hello')\n```\nAfter.", "Before. After."),
        ("See https://www.example.com/report?id=1.", "See example.com."),
        ("Good news 😀 for launch.", "Good news for launch."),
        ("Wait!!! What?? Really...", "Wait! What? Really."),
        ("Alpha — beta", "Alpha, beta."),
        ("[Readable link](https://example.com/path)", "Readable link."),
        ("![Architecture diagram](https://example.com/a.png)", "Architecture diagram."),
        ("> quoted insight", "quoted insight."),
        ("Name | Value\n--- | ---\nWeight | 200 lbs", "Name: Weight; Value: 200 pounds."),
        (
            "Speed is 55 mph, or about 88 km/h.",
            "Speed is 55 miles per hour, or about 88 kilometers per hour.",
        ),
        (
            "Set it to 72°F, not 22°C.",
            "Set it to 72 degrees Fahrenheit, not 22 degrees Celsius.",
        ),
        (
            "Revenue was $4.2B vs $4B, e.g. strong growth.",
            "Revenue was 4.2 billion dollars versus 4 billion dollars, for example strong growth.",
        ),
        ("Use `short_name` here.", "Use short_name here."),
    ],
)
def test_normalize_tts_text_artifact_cases(raw: str, expected: str) -> None:
    assert normalize_tts_text(raw) == expected


_CLEAN_WORDS = st.sampled_from(
    [
        "This",
        "clean",
        "prose",
        "keeps",
        "quotes",
        "apostrophes",
        "dates",
        "times",
        "ready",
        "speech",
        "Dylan's",
        '"quoted"',
        "2026",
        "9:30",
    ]
)


@settings(max_examples=40)
@given(
    st.lists(
        st.lists(_CLEAN_WORDS, min_size=3, max_size=10).map(lambda words: " ".join(words) + "."),
        min_size=1,
        max_size=4,
    ).map(" ".join)
)
def test_normalize_tts_text_is_idempotent_on_clean_prose(clean_text: str) -> None:
    assert normalize_tts_text(clean_text) == clean_text
    assert normalize_tts_text(normalize_tts_text(clean_text)) == clean_text


@pytest.mark.asyncio
async def test_local_synthesis_receives_normalized_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from disco.agent_server import tts_local
    from disco.tools.builtin.audio_overview import _synthesize_local

    received: list[str] = []

    async def fake_synthesize(text: str, voice: str) -> np.ndarray:
        received.append(text)
        return np.ones(16, dtype=np.float32)

    monkeypatch.setattr(tts_local, "synthesize", fake_synthesize)

    audio = await _synthesize_local(
        "## The Main Focus\n\nThe item weighs **200 lbs**!!! 😀",
        "af_heart",
    )

    assert int(audio.size) == 16
    assert received == ["The Main Focus. The item weighs 200 pounds!"]


@pytest.mark.asyncio
async def test_remote_synthesis_payload_receives_normalized_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from disco.tools.builtin import audio_overview

    payloads: list[dict[str, Any]] = []

    class _FakeResponse:
        content = _wav_bytes()

        def raise_for_status(self) -> None:
            return None

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            json: dict[str, Any],
            headers: dict[str, str],
        ) -> _FakeResponse:
            payloads.append(json)
            return _FakeResponse()

    monkeypatch.setattr(audio_overview.httpx, "AsyncClient", _FakeClient)

    audio = await audio_overview._synthesize_remote(
        "**Key point** costs $5 and weighs 2 kg.",
        "af_heart",
        "http://tts.local",
    )

    assert int(audio.size) == 8
    assert payloads[0]["input"] == "Key point costs 5 dollars and weighs 2 kilograms."


def _wav_bytes() -> bytes:
    samples = np.zeros(8, dtype="<i2")
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(samples.tobytes())
    return out.getvalue()
