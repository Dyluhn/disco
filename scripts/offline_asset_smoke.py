"""Network-disabled smoke for baked encoder and TTS assets."""

from __future__ import annotations

import asyncio
import os


async def main() -> None:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

    from disco.retrieval.local_encoders import FastEmbedEmbedder, FastEmbedReranker
    from disco.retrieval.models import Passage

    vectors = await FastEmbedEmbedder().embed(["Disco offline embedding smoke"])
    if not vectors or not vectors[0]:
        raise RuntimeError("embedding returned no vector")
    passages = [
        Passage(
            id="p1",
            source_url="https://example.test/a",
            source_title="A",
            text="Disco packages local ONNX encoders into the server image.",
        ),
        Passage(
            id="p2",
            source_url="https://example.test/b",
            source_title="B",
            text="This passage is unrelated filler.",
        ),
    ]
    ranked = await FastEmbedReranker().rerank("local ONNX encoders", passages, top_k=1)
    if not ranked:
        raise RuntimeError("reranker returned no passage")

    from disco.agent_server import tts_local

    await tts_local.ensure_model()
    pcm = await tts_local.synthesize("Disco", "af_heart")
    sample_count = int(getattr(pcm, "size", 0))
    if sample_count <= 0:
        raise RuntimeError("TTS returned no samples")
    print(
        "offline assets ok: "
        f"embedding_dim={len(vectors[0])} rerank_top={ranked[0].id} tts_samples={sample_count}"
    )


if __name__ == "__main__":
    asyncio.run(main())
