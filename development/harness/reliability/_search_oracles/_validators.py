"""Shared helpers and validators for search oracle validation."""

from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlparse

PASS = "PASS"
FAIL = "FAIL"
_CITATION = re.compile(r"\[\[([\w-]+)\]\]")
_VERDICTS = frozenset({"supported", "weak", "unsupported"})
_CONFIDENCE = frozenset({"high", "mixed", "low"})


class Findings:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, code: str, path: str, message: str) -> None:
        self.items.append({"code": code, "path": path, "message": message})

    def require(self, condition: bool, code: str, path: str, message: str) -> None:
        if not condition:
            self.add(code, path, message)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _web_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(".local"):
        return False
    with _suppress_value_error():
        address = ipaddress.ip_address(host)
        return address.is_global
    return True


class _suppress_value_error:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return exc_type is ValueError


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def _finish(findings: Findings, metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": PASS if not findings.items else FAIL,
        "findings": findings.items,
        "metrics": metrics,
    }


def _passage_index(
    payload: dict[str, Any], findings: Findings, *, require_web: bool
) -> dict[str, dict[str, Any]]:
    passages = _list(payload.get("passages"))
    findings.require(bool(passages), "NO_PASSAGES", "passages", "no cited passages returned")
    by_id: dict[str, dict[str, Any]] = {}
    for index, passage in enumerate(passages):
        path = f"passages[{index}]"
        if not isinstance(passage, dict):
            findings.add("BAD_PASSAGE", path, "passage is not an object")
            continue
        passage_id = _text(passage.get("id"))
        findings.require(bool(passage_id), "BAD_PASSAGE_ID", f"{path}.id", "id is empty")
        if passage_id in by_id:
            findings.add("DUPLICATE_PASSAGE_ID", f"{path}.id", passage_id)
        elif passage_id:
            by_id[passage_id] = passage
        findings.require(
            len(_text(passage.get("text"))) >= 20,
            "EMPTY_PASSAGE_TEXT",
            f"{path}.text",
            "passage text is missing or too short to support a claim",
        )
        findings.require(
            bool(_text(passage.get("source_title"))),
            "EMPTY_SOURCE_TITLE",
            f"{path}.source_title",
            "source title is empty",
        )
        if require_web or not passage.get("corpus_id"):
            findings.require(
                _web_url(passage.get("source_url")),
                "NONPUBLIC_SOURCE_URL",
                f"{path}.source_url",
                "citation URL is not a public HTTP(S) URL",
            )
    return by_id


def _validate_ids(
    ids: Any, *, by_id: dict[str, dict[str, Any]], findings: Findings, path: str
) -> list[str]:
    if not isinstance(ids, list):
        findings.add("BAD_CITATION_LIST", path, "cited_passage_ids is not a list")
        return []
    normalized: list[str] = []
    for index, passage_id in enumerate(ids):
        if not isinstance(passage_id, str) or not passage_id:
            findings.add("BAD_CITATION_ID", f"{path}[{index}]", "citation id is empty")
            continue
        normalized.append(passage_id)
        if passage_id not in by_id:
            findings.add(
                "UNRESOLVED_CITATION", f"{path}[{index}]", f"unknown passage {passage_id!r}"
            )
    if len(normalized) != len(set(normalized)):
        findings.add("DUPLICATE_CITATION", path, "citation list contains duplicate ids")
    return normalized


def _validate_claims(
    claims_value: Any,
    *,
    by_id: dict[str, dict[str, Any]],
    findings: Findings,
    path: str = "claims",
    required: bool = True,
) -> int:
    claims = _list(claims_value)
    if required:
        findings.require(bool(claims), "NO_GRADED_CLAIMS", path, "no graded claims returned")
    unsupported = 0
    for index, verified in enumerate(claims):
        claim_path = f"{path}[{index}]"
        if not isinstance(verified, dict) or not isinstance(verified.get("claim"), dict):
            findings.add("BAD_GRADED_CLAIM", claim_path, "graded claim shape is invalid")
            continue
        claim = verified["claim"]
        findings.require(
            len(_text(claim.get("text"))) >= 3,
            "EMPTY_CLAIM",
            f"{claim_path}.claim.text",
            "claim text is empty",
        )
        cited = _validate_ids(
            claim.get("cited_passage_ids"),
            by_id=by_id,
            findings=findings,
            path=f"{claim_path}.claim.cited_passage_ids",
        )
        findings.require(
            bool(cited),
            "UNCITED_CLAIM",
            f"{claim_path}.claim.cited_passage_ids",
            "a graded factual claim has no citation",
        )
        verdict = verified.get("verdict")
        findings.require(
            verdict in _VERDICTS,
            "BAD_VERDICT",
            f"{claim_path}.verdict",
            f"unsupported verdict {verdict!r}",
        )
        if verdict == "unsupported":
            unsupported += 1
        score = verified.get("entailment_score")
        findings.require(
            isinstance(score, (int, float)) and not isinstance(score, bool) and 0 <= score <= 1,
            "BAD_ENTAILMENT_SCORE",
            f"{claim_path}.entailment_score",
            "entailment score must be between 0 and 1",
        )
        best = verified.get("best_passage_id")
        if cited:
            findings.require(
                isinstance(best, str) and best in cited and best in by_id,
                "BAD_BEST_PASSAGE",
                f"{claim_path}.best_passage_id",
                "best passage must resolve and belong to the claim's citations",
            )
    return unsupported


def _validate_blocks(
    blocks_value: Any, *, by_id: dict[str, dict[str, Any]], findings: Findings
) -> None:
    blocks = _list(blocks_value)
    findings.require(bool(blocks), "NO_ANSWER_BLOCKS", "blocks", "answer has no blocks")
    block_ids: set[str] = set()
    substantive = 0
    markers_seen: set[str] = set()
    for index, block in enumerate(blocks):
        path = f"blocks[{index}]"
        if not isinstance(block, dict):
            findings.add("BAD_BLOCK", path, "block is not an object")
            continue
        block_id = _text(block.get("id"))
        if not block_id or block_id in block_ids:
            findings.add("BAD_BLOCK_ID", f"{path}.id", "block id is empty or duplicated")
        block_ids.add(block_id)
        kind = block.get("kind")
        content = " ".join(_strings(block))
        if kind not in {"heading"} and len(content.strip()) >= 20:
            substantive += 1
        markers = set(_CITATION.findall(content))
        markers_seen.update(markers)
        declared = _validate_ids(
            block.get("cited_passage_ids", []),
            by_id=by_id,
            findings=findings,
            path=f"{path}.cited_passage_ids",
        )
        for passage_id in markers:
            if passage_id not in by_id:
                findings.add(
                    "UNRESOLVED_INLINE_CITATION",
                    path,
                    f"inline citation {passage_id!r} has no passage",
                )
            if declared and passage_id not in declared:
                findings.add(
                    "UNDECLARED_INLINE_CITATION",
                    path,
                    f"inline citation {passage_id!r} is absent from block citation metadata",
                )
    findings.require(
        substantive > 0,
        "NO_SUBSTANTIVE_ANSWER",
        "blocks",
        "answer contains no substantive content block",
    )
    findings.require(
        bool(markers_seen),
        "NO_INLINE_CITATIONS",
        "blocks",
        "answer renders no inline passage citation",
    )


def _validate_discovery(payload: dict[str, Any], findings: Findings, *, require_web: bool) -> None:
    hits = _list(payload.get("all_hits"))
    if require_web:
        findings.require(bool(hits), "NO_DISCOVERY_HITS", "all_hits", "web search found no URLs")
    for index, hit in enumerate(hits):
        if not isinstance(hit, dict):
            findings.add("BAD_SEARCH_HIT", f"all_hits[{index}]", "hit is not an object")
            continue
        if require_web:
            findings.require(
                _web_url(hit.get("url")),
                "NONPUBLIC_HIT_URL",
                f"all_hits[{index}].url",
                "search hit is not a public HTTP(S) URL",
            )


def _cited_passage_ids(payload: dict[str, Any]) -> set[str]:
    """Return passage IDs that the rendered answer/report actually cites."""
    cited: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            ids = value.get("cited_passage_ids")
            if isinstance(ids, list):
                cited.update(item for item in ids if isinstance(item, str) and item)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return cited
