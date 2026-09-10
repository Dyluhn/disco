"""Small, genuine three-way NLI on the existing ONNX CPU runtime.

The pinned checkpoint classifies contradiction, entailment and neutral. Its
probabilities are model estimates, not proof of a claim's factual truth.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnxruntime as ort
from disco.core.env import disco_env
from huggingface_hub import hf_hub_download
from tokenizers import Tokenizer

from .nli import Entailment

NLI_MODEL = "cross-encoder/nli-deberta-v3-small"
NLI_REVISION = "fa2804872c3b4bd748f38c0185cc85775361e735"
NLI_ONNX_FILE = "onnx/model_quint8_avx2.onnx"
_LABELS: dict[str, Entailment] = {
    "contradiction": "contradict",
    "entailment": "entail",
    "neutral": "neutral",
}


@dataclass(frozen=True)
class NLIPrediction:
    label: Entailment
    entailment: float
    available: bool = True


def _asset(name: str) -> Path:
    directory = disco_env("NLI_MODEL_DIR")
    if directory:
        return Path(directory) / Path(name).name
    return Path(hf_hub_download(NLI_MODEL, name, revision=NLI_REVISION))


class OnnxNLIPredictor:
    """One lazy-loaded CPU model; one bounded pair per prediction."""

    def __init__(self) -> None:
        config = json.loads(_asset("config.json").read_text())
        self._labels: dict[int, Entailment] = {
            int(index): _LABELS[label.lower()] for index, label in config["id2label"].items()
        }
        if set(self._labels.values()) != set(_LABELS.values()):
            raise ValueError("NLI checkpoint must declare entailment, contradiction and neutral")
        self._tokenizer = Tokenizer.from_file(str(_asset("tokenizer.json")))
        self._tokenizer.enable_truncation(max_length=512)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.enable_cpu_mem_arena = False
        self._session = ort.InferenceSession(
            str(_asset(NLI_ONNX_FILE)), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._inputs = [item.name for item in self._session.get_inputs()]

    def predict(self, premise: str, hypothesis: str) -> NLIPrediction:
        encoded = self._tokenizer.encode(premise, hypothesis)
        values = {
            "input_ids": np.asarray([encoded.ids], dtype=np.int64),
            "attention_mask": np.asarray([encoded.attention_mask], dtype=np.int64),
            "token_type_ids": np.asarray([encoded.type_ids], dtype=np.int64),
        }
        logits = np.asarray(self._session.run(None, {key: values[key] for key in self._inputs})[0])
        probabilities = np.exp(logits[0] - logits[0].max())
        probabilities /= probabilities.sum()
        if probabilities.shape != (3,) or not np.isfinite(probabilities).all():
            raise ValueError("NLI checkpoint returned invalid class probabilities")
        entail_index = next(i for i, label in self._labels.items() if label == "entail")
        return NLIPrediction(
            self._labels[int(probabilities.argmax())], float(probabilities[entail_index])
        )
