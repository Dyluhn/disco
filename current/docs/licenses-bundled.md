# Bundled Runtime Licenses

This file covers heavyweight dependencies and model artifacts intentionally baked
into the compose images. It is not a full transitive SBOM.

## Server Image

| Artifact | Why bundled | License / attribution |
|---|---|---|
| `fastembed` | In-process ONNX embeddings/reranking. | Apache License; package ships `LICENSE` and `NOTICE`. |
| `intfloat/multilingual-e5-large` via FastEmbed/Qdrant ONNX | Default embedding model. | MIT per FastEmbed supported-model metadata: <https://qdrant.github.io/fastembed/examples/Supported_Models/>. |
| `BAAI/bge-reranker-base` | Default reranker and NLI proxy. | MIT; BAAI/FlagEmbedding says released models may be used commercially: <https://huggingface.co/BAAI/bge-reranker-base>. |
| `kokoro-onnx` package | Bundled Kokoro ONNX runtime wrapper. | Package includes a `LICENSE` file; project: <https://github.com/thewh1teagle/kokoro-onnx>. |
| `kokoro-v1.0.onnx`, `voices-v1.0.bin` | Bundled TTS model and voices. | Kokoro model weights are Apache-2.0 per upstream Kokoro model/project docs: <https://github.com/hexgrad/kokoro> and <https://huggingface.co/hexgrad/Kokoro-82M>. |
| `onnxruntime` | CPU inference for fastembed and Kokoro. | MIT. |
| `lameenc` | MP3 encoding for audio overviews. | LGPL-3.0; package metadata includes the LGPL text. |
| LibreOffice Impress | Deck to PDF conversion in server/process mode. | Mozilla Public License 2.0 plus bundled third-party notices from Debian packages. |
| WeasyPrint/Pango/Cairo stack | PDF export. | Mixed open-source licenses from Debian/Python packages; retain package notices. |

## Sandbox Image

| Artifact | Why bundled | License / attribution |
|---|---|---|
| Playwright + Chromium | Browser automation and Marp rendering. | Playwright is Apache-2.0. Chromium is BSD-style for Google-authored code with third-party components under their own licenses; see Chromium licensing docs: <https://www.chromium.org/chromium-os/developer-library/reference/licensing/licensing-for-chromiumos-package-owners/>. |
| LibreOffice Impress | Sandboxed PPTX to PDF conversion. | MPL-2.0 plus Debian third-party notices. |
| Node.js 22, npm, pnpm | JS/TS build toolchain. | Node.js is MIT; npm/pnpm and their dependencies carry their own notices. |
| Python, uv, ipykernel, Jupyter Kernel Gateway | Python execution and persistent kernels. | Python Software Foundation License / MIT / BSD-style notices, depending on package. |
| Pandoc | DOCX export. | GPL-2.0-or-later; distributed only inside the sandbox image. |

## Operator Notes

The authoritative license texts for Debian packages are available in
`/usr/share/doc/<package>/copyright` inside the image. Python package metadata is
under the installed `.dist-info` directories in the server virtualenv.

If the default encoder tier changes, update this file and `current/docs/self-host.md` in
the same patch. In particular, do not make the CC-BY-NC Jina reranker the
default for a commercially usable image.
