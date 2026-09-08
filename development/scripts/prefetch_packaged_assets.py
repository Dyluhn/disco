"""Prefetch runtime model assets for the compose server image.

This script intentionally avoids importing Disco application modules. It mirrors
the packaged defaults so the Dockerfile can download heavyweight assets after
Python dependencies are installed but before app source changes invalidate the
layer.
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
from pathlib import Path

FULL_EMBED_MODEL = "intfloat/multilingual-e5-large"
FULL_RERANK_MODEL = "BAAI/bge-reranker-base"
LITE_EMBED_MODEL = "BAAI/bge-small-en-v1.5"
LITE_RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
NLI_MODEL = "cross-encoder/nli-deberta-v3-small"
NLI_REVISION = "fa2804872c3b4bd748f38c0185cc85775361e735"
NLI_ASSETS = {
    "config.json": "885d0dceae8fa5c136da9209121ec9eb11160488e840de3bc1f29353674e5712",
    "tokenizer.json": "5124ef2ead1a10a717703bc436de7f353da76d6340e4587719b42b1693707964",
    "onnx/model_quint8_avx2.onnx": (
        "03c2221313dc0c3eac9cec1f746d1319d33f2c2901fcce1c0f08f4daac9b6dae"
    ),
}

KOKORO_MODEL_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/kokoro-v1.0.onnx"
)
KOKORO_MODEL_SHA = "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"
KOKORO_VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
)
KOKORO_VOICES_SHA = "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"DISCO_{name}") or os.environ.get(f"PMX_{name}") or default


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch(url: str, dest: Path, sha: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        got = _sha256(dest)
        if got == sha:
            print(f"[prefetch] present {dest}")
            return
        dest.unlink()
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"[prefetch] downloading {url} -> {dest}")
    urllib.request.urlretrieve(url, tmp)  # noqa: S310 - pinned release URL plus SHA256.
    got = _sha256(tmp)
    if got != sha:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"checksum mismatch for {url}: got {got}, want {sha}")
    tmp.replace(dest)


def _prefetch_fastembed(cache_dir: Path) -> None:
    from fastembed import TextEmbedding
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    tier = _env("ENCODER_TIER", "full").strip().lower()
    embed_model = _env("EMBED_MODEL") or (LITE_EMBED_MODEL if tier == "lite" else FULL_EMBED_MODEL)
    rerank_model = _env("RERANK_MODEL") or (
        LITE_RERANK_MODEL if tier == "lite" else FULL_RERANK_MODEL
    )
    print(f"[prefetch] fastembed cache={cache_dir}")
    print(f"[prefetch] embedding model={embed_model}")
    TextEmbedding(model_name=embed_model, cache_dir=str(cache_dir), lazy_load=True)
    print(f"[prefetch] reranker model={rerank_model}")
    TextCrossEncoder(model_name=rerank_model, cache_dir=str(cache_dir), lazy_load=True)

    # Prove the downloaded files are enough for fastembed's offline resolver.
    TextEmbedding(
        model_name=embed_model,
        cache_dir=str(cache_dir),
        lazy_load=True,
        local_files_only=True,
    )
    TextCrossEncoder(
        model_name=rerank_model,
        cache_dir=str(cache_dir),
        lazy_load=True,
        local_files_only=True,
    )


def _prefetch_kokoro(tts_dir: Path) -> None:
    _fetch(KOKORO_MODEL_URL, tts_dir / "kokoro-v1.0.onnx", KOKORO_MODEL_SHA)
    _fetch(KOKORO_VOICES_URL, tts_dir / "voices-v1.0.bin", KOKORO_VOICES_SHA)


def _prefetch_nli(nli_dir: Path) -> None:
    # This classifier is separate from the relevance reranker. Put it under
    # the directory copied into the runtime image, not the builder's HF cache.
    for name, digest in NLI_ASSETS.items():
        url = f"https://huggingface.co/{NLI_MODEL}/resolve/{NLI_REVISION}/{name}"
        _fetch(url, nli_dir / Path(name).name, digest)


def main() -> None:
    fastembed_cache = Path(os.environ.get("FASTEMBED_CACHE_PATH", "/opt/disco-cache/fastembed"))
    tts_dir = Path(_env("TTS_DIR", "/opt/disco-cache/tts"))
    nli_dir = Path(_env("NLI_MODEL_DIR", "/opt/disco-cache/nli"))
    _prefetch_fastembed(fastembed_cache)
    _prefetch_nli(nli_dir)
    _prefetch_kokoro(tts_dir)
    print("[prefetch] packaged assets ready")


if __name__ == "__main__":
    main()
