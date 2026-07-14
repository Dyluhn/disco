"""Truth oracles for live Search and Deep Research outputs.

Rendering a citation badge is not evidence.  These checks follow every citation
back to a real passage, verify claim-grade consistency, and optionally make a
fresh network connection to each cited web origin.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
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


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


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


def _finish(findings: Findings, metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": PASS if not findings.items else FAIL,
        "findings": findings.items,
        "metrics": metrics,
    }


def validate_grounded_answer(
    answer: Any,
    *,
    require_web: bool = True,
    connectivity: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    findings = Findings()
    if not isinstance(answer, dict):
        findings.add("BAD_ANSWER", "$", "grounded answer is not an object")
        return _finish(findings, {})
    findings.require(bool(_text(answer.get("query"))), "EMPTY_QUERY", "query", "query is empty")
    by_id = _passage_index(answer, findings, require_web=require_web)
    unsupported = _validate_claims(answer.get("claims"), by_id=by_id, findings=findings)
    declared_unsupported = answer.get("unsupported_count")
    findings.require(
        isinstance(declared_unsupported, int)
        and not isinstance(declared_unsupported, bool)
        and declared_unsupported == unsupported,
        "UNSUPPORTED_COUNT_MISMATCH",
        "unsupported_count",
        f"declared {declared_unsupported!r}; graded claims contain {unsupported}",
    )
    _validate_blocks(answer.get("blocks"), by_id=by_id, findings=findings)
    _validate_discovery(answer, findings, require_web=require_web)
    follow_ups = _list(answer.get("follow_ups"))
    valid_followups = [item for item in follow_ups if len(_text(item)) >= 8]
    findings.require(
        len(valid_followups) >= 2,
        "MISSING_FOLLOW_UPS",
        "follow_ups",
        "fewer than two useful follow-up questions were returned",
    )
    if len(valid_followups) != len(set(valid_followups)):
        findings.add("DUPLICATE_FOLLOW_UP", "follow_ups", "follow-up questions repeat")
    if connectivity is not None:
        _validate_connectivity(connectivity, by_id, _cited_passage_ids(answer), findings)
    return _finish(
        findings,
        {
            "passages": len(by_id),
            "claims": len(_list(answer.get("claims"))),
            "unsupported": unsupported,
            "follow_ups": len(valid_followups),
        },
    )


def validate_report_event(
    report: Any,
    *,
    require_web: bool = True,
    connectivity: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    findings = Findings()
    if not isinstance(report, dict):
        findings.add("BAD_REPORT", "$", "report event is not an object")
        return _finish(findings, {})
    findings.require(bool(_text(report.get("query"))), "EMPTY_QUERY", "query", "query is empty")
    findings.require(
        len(_text(report.get("summary"))) >= 40,
        "EMPTY_SUMMARY",
        "summary",
        "report summary is missing or too short",
    )
    by_id = _passage_index(report, findings, require_web=require_web)
    _validate_discovery(report, findings, require_web=require_web)
    sections = _list(report.get("sections"))
    findings.require(bool(sections), "NO_REPORT_SECTIONS", "sections", "report has no sections")
    section_ids: set[str] = set()
    unsupported = 0
    for index, section in enumerate(sections):
        path = f"sections[{index}]"
        if not isinstance(section, dict):
            findings.add("BAD_REPORT_SECTION", path, "section is not an object")
            continue
        section_id = _text(section.get("id"))
        if not section_id or section_id in section_ids:
            findings.add("BAD_SECTION_ID", f"{path}.id", "section id is empty or duplicated")
        section_ids.add(section_id)
        findings.require(
            bool(_text(section.get("title"))), "EMPTY_SECTION_TITLE", path, "empty title"
        )
        markdown = _text(section.get("markdown"))
        findings.require(
            len(markdown) >= 40,
            "EMPTY_SECTION",
            f"{path}.markdown",
            "section body is missing or too short",
        )
        cited = _validate_ids(
            section.get("cited_passage_ids"),
            by_id=by_id,
            findings=findings,
            path=f"{path}.cited_passage_ids",
        )
        markers = set(_CITATION.findall(markdown))
        findings.require(bool(cited), "UNCITED_SECTION", path, "section has no cited passages")
        findings.require(
            bool(markers), "NO_SECTION_MARKERS", path, "section has no inline citations"
        )
        for passage_id in markers:
            if passage_id not in by_id:
                findings.add("UNRESOLVED_INLINE_CITATION", path, passage_id)
            if passage_id not in cited:
                findings.add("UNDECLARED_INLINE_CITATION", path, passage_id)
        for passage_id in cited:
            if passage_id not in markers:
                findings.add("UNRENDERED_SECTION_CITATION", path, passage_id)
        confidence = section.get("confidence")
        findings.require(
            confidence in _CONFIDENCE,
            "BAD_CONFIDENCE",
            f"{path}.confidence",
            f"unknown confidence {confidence!r}",
        )
        section_unsupported = section.get("unsupported_count")
        if isinstance(section_unsupported, int) and not isinstance(section_unsupported, bool):
            findings.require(
                section_unsupported >= 0,
                "BAD_UNSUPPORTED_COUNT",
                f"{path}.unsupported_count",
                "count is negative",
            )
            unsupported += max(0, section_unsupported)
        else:
            findings.add(
                "BAD_UNSUPPORTED_COUNT", f"{path}.unsupported_count", "count is not an integer"
            )
        notes = section.get("disputed_notes")
        findings.require(
            isinstance(notes, list) and all(bool(_text(note)) for note in notes),
            "BAD_DISPUTED_NOTES",
            f"{path}.disputed_notes",
            "disputed notes must be a list of nonempty strings",
        )
    findings.require(
        report.get("unsupported_count") == unsupported,
        "UNSUPPORTED_COUNT_MISMATCH",
        "unsupported_count",
        f"declared {report.get('unsupported_count')!r}; sections total {unsupported}",
    )
    claims = report.get("claims")
    if claims is not None:
        _validate_claims(claims, by_id=by_id, findings=findings, required=False)
    if connectivity is not None:
        _validate_connectivity(connectivity, by_id, _cited_passage_ids(report), findings)
    return _finish(
        findings,
        {"passages": len(by_id), "sections": len(sections), "unsupported": unsupported},
    )


def _safe_public_host(url: str) -> tuple[bool, str | None]:
    parsed = urlparse(url)
    if not _web_url(url) or not parsed.hostname:
        return False, "not a public HTTP(S) URL"
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM
            )
        }
    except OSError as exc:
        return False, f"DNS failed: {exc}"
    if not addresses:
        return False, "DNS returned no addresses"
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        return False, "DNS resolved to a non-public address"
    return True, None


def _probe_one(url: str, timeout_s: float) -> dict[str, Any]:
    safe, error = _safe_public_host(url)
    if not safe:
        return {"url": url, "connected": False, "status": None, "error": error}
    headers = {"User-Agent": "Disco-Reliability-Harness/1.0"}
    for method in ("HEAD", "GET"):
        request = urllib.request.Request(url, headers=headers, method=method)
        if method == "GET":
            request.add_header("Range", "bytes=0-1023")
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
                return {
                    "url": url,
                    "connected": True,
                    "status": int(response.status),
                    "final_url": response.geturl(),
                    "error": None,
                }
        except urllib.error.HTTPError as exc:
            # A surprising number of otherwise healthy sources reject or
            # misroute HEAD.  Only an actual GET failure is evidence that a
            # citation cannot be opened by a reader.
            if method == "HEAD" and exc.code not in {401, 403, 429}:
                continue
            return {
                "url": url,
                "connected": True,
                "status": int(exc.code),
                "final_url": exc.geturl(),
                "error": str(exc),
            }
        except (OSError, urllib.error.URLError) as exc:
            if method == "HEAD":
                continue
            return {"url": url, "connected": False, "status": None, "error": str(exc)}
    return {"url": url, "connected": False, "status": None, "error": "probe exhausted"}


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


def probe_cited_sources(
    payload: dict[str, Any], *, timeout_s: float = 15.0
) -> list[dict[str, Any]]:
    cited_ids = _cited_passage_ids(payload)
    urls = sorted(
        {
            passage.get("source_url")
            for passage in _list(payload.get("passages"))
            if isinstance(passage, dict)
            and passage.get("id") in cited_ids
            and isinstance(passage.get("source_url"), str)
        }
    )
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(urls)))) as executor:
        return list(executor.map(lambda url: _probe_one(url, timeout_s), urls))


def _validate_connectivity(
    connectivity: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    cited_ids: set[str],
    findings: Findings,
) -> None:
    by_url = {
        item.get("url"): item
        for item in connectivity
        if isinstance(item, dict) and isinstance(item.get("url"), str)
    }
    urls = {by_id[passage_id].get("source_url") for passage_id in cited_ids if passage_id in by_id}
    successful = 0
    for url in sorted(url for url in urls if isinstance(url, str)):
        probe = by_url.get(url)
        if not probe:
            findings.add("SOURCE_NOT_PROBED", "connectivity", url)
            continue
        status = probe.get("status")
        acceptable = (
            bool(probe.get("connected"))
            and isinstance(status, int)
            and (200 <= status < 400 or status in {401, 403, 429})
        )
        if acceptable:
            successful += 1
        else:
            findings.add(
                "CITED_SOURCE_UNREACHABLE",
                "connectivity",
                f"{url}: status={status!r} error={probe.get('error')!r}",
            )
    findings.require(
        successful > 0,
        "NO_REACHABLE_CITED_SOURCE",
        "connectivity",
        "no cited source accepted a fresh network connection",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate live Search evidence")
    parser.add_argument("kind", choices=("answer", "report"))
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-corpus-only", action="store_true")
    parser.add_argument("--probe-connectivity", action="store_true")
    parser.add_argument("--probe-timeout", type=float, default=15.0)
    args = parser.parse_args(argv)
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    connectivity = (
        probe_cited_sources(payload, timeout_s=args.probe_timeout)
        if args.probe_connectivity
        else None
    )
    validate = validate_grounded_answer if args.kind == "answer" else validate_report_event
    result = validate(
        payload,
        require_web=not args.allow_corpus_only,
        connectivity=connectivity,
    )
    if connectivity is not None:
        result["connectivity"] = connectivity
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if result["status"] == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
