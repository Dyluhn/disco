#!/usr/bin/env python3
"""Extract per-claim grounding grades from a FINISHED Deep Research report.

WHY THIS RE-GRADES INSTEAD OF READING STORED VERDICTS
-----------------------------------------------------
The finished `ReportEvent` (core/events.py:403) does NOT persist per-claim
verdicts. The synthesis path (`synthesize_section`) runs the NLI verifier per
section via `streaming._verify_claims`, then DISCARDS the individual
{verdict, entailment_score} results, keeping only the rolled-up
`ReportSection.confidence` + `ReportSection.unsupported_count`
(synthesis.py:426-439). The on-disk report stores, per section, only:
`markdown` (with inline `[[passage_id]]` markers), `cited_passage_ids`,
`confidence`, `disputed_notes`, `unsupported_count` — and at report scope the
cited `passages` store ({id, source_url, source_title, text, ...}).

So to recover the per-claim grades for a fairness audit we FAITHFULLY RE-RUN the
exact same machinery the pipeline used: `streaming._verify_claims(markdown,
by_id, nli)` over each section's stored markdown, with the cited passages as the
premise store. The premise (cited passage's full text, stored on the report) and
the hypothesis (claim text segmented out of the markdown by the same regex) are
byte-identical to what synthesis fed the verifier, and the cross-encoder is
deterministic — so the recomputed verdict/entailment_score reproduce the grades
the report rolled up. The verifier used is the ACTIVE one for this install
(see `_build_nli`): with `encoders.remote=false` that is the in-process
`FastEmbedNLIVerifier`, which is the `BAAI/bge-reranker-base` cross-encoder
sigmoid-squashed and thresholded (entail>=0.5, contradict<=0.1) — a relevance
proxy, NOT a true 3-way NLI. That is itself the central fairness caveat.

USAGE
-----
    PYTHONPATH=packages/core/src:packages/retrieval/src:packages/tools/src:\\
packages/agent-server/src:packages/app-server/src \\
      .venv/bin/python3 scripts/extract_grounding.py <conversation_id>

The script also self-bootstraps sys.path from the repo root, so the bare form
    .venv/bin/python3 scripts/extract_grounding.py <conversation_id>
works too. First NLI run lazily downloads the ~0.5 GB ONNX reranker.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

# --- self-bootstrap the 5 package src dirs (honor an existing PYTHONPATH too) ---
_REPO = Path(__file__).resolve().parent.parent
for _pkg in ("core", "retrieval", "tools", "agent-server", "app-server"):
    _src = _REPO / "packages" / _pkg / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))


def _load_report(db_path: Path, conversation_id: str) -> dict[str, Any]:
    """Return the payload of the latest `report` event for the conversation."""
    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute(
            "SELECT payload FROM events "
            "WHERE conversation_id = ? AND kind = 'report' "
            "ORDER BY seq DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise SystemExit(
            f"No 'report' event found for conversation {conversation_id!r} in {db_path}"
        )
    return json.loads(row[0])


def _active_remote() -> bool:
    """Mirror build_live_retrieval's remote decision: env wins, else
    disco-config.json `encoders.remote`, else local (False)."""
    import os

    env = os.environ.get("DISCO_ENCODERS") or os.environ.get("PMX_ENCODERS")
    if env is not None:
        return env.lower() == "remote"
    cfg = _REPO / "disco-config.json"
    if cfg.is_file():
        try:
            data = json.loads(cfg.read_text())
            return bool(data.get("encoders", {}).get("remote", False))
        except Exception:  # noqa: BLE001
            pass
    return False


def _build_nli() -> tuple[Any, dict[str, Any]]:
    """Construct the ACTIVE NLI verifier and a descriptor of it (for the output)."""
    if _active_remote():
        import os

        from disco.retrieval.live import (
            _DEFAULTS,  # type: ignore[attr-defined]
            SidecarNLIVerifier,
        )

        url = (
            os.environ.get("DISCO_NLI_URL")
            or os.environ.get("PMX_NLI_URL")
            or _DEFAULTS["DISCO_NLI_URL"]
        )
        return SidecarNLIVerifier(url), {
            "active": "remote SidecarNLIVerifier (NLI sidecar /verify)",
            "remote": True,
            "endpoint": url,
            "note": "real 3-way NLI cross-encoder (mDeBERTa-class) at the sidecar",
        }

    from disco.retrieval.local_encoders import (
        RERANK_MODEL,
        FastEmbedNLIVerifier,
        _tier_rerank_default,
    )

    return FastEmbedNLIVerifier(), {
        "active": "in-process FastEmbedNLIVerifier (ONNX/CPU via fastembed)",
        "remote": False,
        "model": _tier_rerank_default(),
        "configured_full_default": RERANK_MODEL,
        "note": (
            "NOT a true 3-way NLI: the bge-reranker cross-encoder framed as "
            "claim<->passage relevance, sigmoid-squashed; entail>=0.5, "
            "contradict<=0.1, else neutral->weak."
        ),
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: extract_grounding.py <conversation_id>")
    conversation_id = sys.argv[1]
    db_path = _REPO / "disco.db"

    payload = _load_report(db_path, conversation_id)

    # Lazy disco imports (after sys.path bootstrap).
    from disco.retrieval.models import Passage
    from disco.retrieval.streaming import _verify_claims

    # Report-scope passage store: id -> {id, title, url, text} and id -> Passage.
    raw_passages = payload.get("passages", [])
    by_id_meta: dict[str, dict[str, Any]] = {}
    by_id_passage: dict[str, Passage] = {}
    for p in raw_passages:
        pid = p["id"]
        by_id_meta[pid] = {
            "id": pid,
            "title": p.get("source_title", ""),
            "url": p.get("source_url", ""),
            "text": p.get("text", ""),
        }
        by_id_passage[pid] = Passage.model_validate(p)

    nli, nli_desc = _build_nli()

    claims_out: list[dict[str, Any]] = []
    for section in payload.get("sections", []):
        heading = section.get("title", "")
        markdown = section.get("markdown", "")
        # Faithful re-run of the pipeline's per-section claim verification.
        verified = _verify_claims(markdown, by_id_passage, nli)
        for v in verified:
            cited_ids = v["claim"]["cited_passage_ids"]
            cited = [by_id_meta[i] for i in cited_ids if i in by_id_meta]
            claims_out.append(
                {
                    "section_heading": heading,
                    "claim_text": v["claim"]["text"],
                    "verdict": v["verdict"],
                    "entailment_score": v["entailment_score"],
                    "best_passage_id": v["best_passage_id"],
                    "cited_passage_ids": cited_ids,
                    "cited_passages": cited,  # resolved {id,title,url,text}; [] if none resolve
                }
            )

    out = {
        "conversation_id": conversation_id,
        "query": payload.get("query", ""),
        "summary": payload.get("summary", ""),
        "depth_tier": payload.get("depth_tier"),
        "bounded_by": payload.get("bounded_by"),
        "report_unsupported_count": payload.get("unsupported_count"),
        "n_sections": len(payload.get("sections", [])),
        "n_passages": len(raw_passages),
        "n_graded_claims": len(claims_out),
        "nli_provider": nli_desc,
        "claims": claims_out,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
