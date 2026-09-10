#!/usr/bin/env python3
"""CD-TOOLS-9 — live MiniMax-M3 targeted-edit run: the Mode-B-gone proof (build-then-edit).

Drives a REAL build through the agent-server (:8000, default driver = relay :8080 → MiniMax-M3):
PHASE 1 the model BUILDS a large index.html landing page (so its own file_write content is later
elided from context — the Mode-B setup); PHASE 2 a targeted FOLLOWUP asks it to change ONLY the
hero/CTA/footer in place. Then classify, from REAL tool-call/tool-result events of PHASE 2, whether
a targeted-edit tool was used + succeeded, fresh-read behavior held, there was NO edit-elision
thrash, the edits actually applied (output-truth on the SERVED page), and the provider ledger is
clean (0 OpenRouter, 0 post-terminal). NOT a cassette — every call hits MiniMax. Dossier → RUN_DIR.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.request

AGENT = os.environ.get("DISCO_AGENT_URL", "http://localhost:8000")
LEDGER = os.environ.get("MINIMAX_RELAY_LOG", "")
RUN_DIR = os.environ.get(
    "RUN_DIR",
    os.path.join(tempfile.gettempdir(), "disco-targeted-edit"),
)
BUILD_TIMEOUT_S = int(os.environ.get("CD9_BUILD_TIMEOUT_S", "420"))
EDIT_TIMEOUT_S = int(os.environ.get("CD9_EDIT_TIMEOUT_S", "360"))
_TERMINAL = {"FINISHED", "VERIFIED", "STUCK", "ERROR", "AWAITING_USER", "FAILED", "CANCELLED"}
_EDIT_TOOLS = {
    "exact_replace",
    "run_project_script",
    "safe_write_file",
    "file_edit",
    "file_replace_lines",
    "file_str_replace",
}

HERO_OLD, HERO_NEW = "OLD_HERO_HEADLINE_X1", "NEW_HERO_2099_HEADLINE"
CTA_OLD, CTA_NEW = "OLD_CTA_BUTTON_TEXT", "Get Started Now Please"
YEAR_OLD, YEAR_NEW = "2024", "2026"

BUILD_PROMPT = (
    "Build a single-file static landing page `index.html` (plain HTML/CSS, no framework). "
    f"It MUST contain, verbatim: a big hero headline with the exact text `{HERO_OLD}`, a CTA "
    f"button with the exact text `{CTA_OLD}`, and a footer line `© {YEAR_OLD} Acme Corp`. Also add "
    "a features section with AT LEAST 40 list items of descriptive copy so the file is substantial "
    "(well over 2 KB). Serve it on the preview and finish."
)
EDIT_PROMPT = (
    "Now change ONLY these three things in `index.html`, editing precisely IN PLACE — do NOT "
    "rewrite the whole file:\n"
    f"1. hero headline `{HERO_OLD}` -> `{HERO_NEW}`\n"
    f"2. CTA button text `{CTA_OLD}` -> `{CTA_NEW}`\n"
    f"3. footer year `{YEAR_OLD}` -> `{YEAR_NEW}`\n"
    "Read the file first, apply the edits with a targeted edit tool, then verify and finish."
)


def _post_json(path: str, body: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        AGENT + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _get(path: str, timeout: int = 60):
    with urllib.request.urlopen(AGENT + path, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _get_text(path: str, timeout: int = 30) -> str:
    try:
        with urllib.request.urlopen(AGENT + path, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _events(cid: str) -> list[dict]:
    d = _get(f"/conversations/{cid}/events?limit=1000")
    if isinstance(d, list):
        return d
    for k in ("events", "items", "log"):
        if isinstance(d.get(k), list):
            return d[k]
    return []


def _status(cid: str) -> str:
    return str(_get(f"/conversations/{cid}/state").get("execution_status") or "")


def _last_seq(cid: str) -> int:
    return int(_get(f"/conversations/{cid}/state").get("last_seq") or 0)


def _ledger() -> list[dict]:
    if not LEDGER or not os.path.exists(LEDGER):
        return []
    out = []
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def _poll_terminal(cid: str, timeout_s: int) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(8)
        try:
            s = _status(cid)
        except Exception:
            continue
        if s in _TERMINAL:
            return s
    return "TIMEOUT"


def _all_minimax(slice_: list, openrouter: int) -> bool:
    """True when every ledger host is minimax and none is openrouter."""
    return (
        bool(slice_)
        and openrouter == 0
        and all("minimax" in str(ln.get("host") or "").lower() for ln in slice_)
    )


def _phase2_tool_calls(phase2: list) -> list:
    """Yield (tool_name, arguments) for each tool_call in phase2."""
    return [
        (tc.get("tool_name"), (tc.get("arguments") or {}))
        for e in phase2
        if (tc := e.get("tool_call"))
    ]


def _phase2_tool_results(phase2: list) -> list:
    """Yield (tool_name, tool_result) for each tool_result in phase2."""
    return [(tr.get("tool_name"), tr) for e in phase2 if (tr := e.get("tool_result"))]


def _check_fresh_read_before_edit(phase2: list, ok_edit_call_ids: set) -> bool:
    """A file_read of index.html must come BEFORE the first successful targeted edit."""
    rseq = _seq_first_read(phase2)
    eseq = _seq_first_edit_ok(phase2, ok_edit_call_ids)
    return rseq is not None and eseq is not None and rseq < eseq


def _seq_first_read(phase2: list) -> int | None:
    for e in phase2:
        tc = e.get("tool_call") or {}
        if tc.get("tool_name") == "file_read" and "index.html" in str(
            (tc.get("arguments") or {}).get("path", "")
        ):
            return int(e.get("seq") or 0)
    return None


def _seq_first_edit_ok(phase2: list, ok_edit_call_ids: set) -> int | None:
    for e in phase2:
        tc = e.get("tool_call") or {}
        if tc.get("tool_name") in _EDIT_TOOLS and tc.get("call_id") in ok_edit_call_ids:
            return int(e.get("seq") or 0)
    return None


def _targets_index(name: str, a: dict) -> bool:
    if name == "run_project_script":
        return any(
            "index.html" in str((o or {}).get("path", "")) for o in (a.get("operations") or [])
        )
    return "index.html" in str(a.get("path", ""))


def _analyze_edit_ordering(phase2: list, ok_edit_call_ids: set) -> dict:
    """Extract fresh-read-before-edit and edit-on-index facts."""
    fresh_read_before_edit = _check_fresh_read_before_edit(phase2, ok_edit_call_ids)
    edit_on_index = any(
        (e.get("tool_call") or {}).get("call_id") in ok_edit_call_ids
        and (e.get("tool_call") or {}).get("tool_name") in _EDIT_TOOLS
        and _targets_index(
            (e.get("tool_call") or {}).get("tool_name") or "",
            (e.get("tool_call") or {}).get("arguments") or {},
        )
        for e in phase2
    )
    return {"fresh_read_before_edit": fresh_read_before_edit, "edit_on_index": edit_on_index}


def _analyze_edit_errors(results: list) -> dict:
    """Extract old_not_found, fresh_required, elision_rej from results."""
    old_not_found = sum(
        1 for _, tr in results if "old_text_not_found" in str(tr.get("error") or "")
    )
    fresh_required = any(
        "FRESH_READ_REQUIRED" in str(tr.get("error") or "")
        or (tr.get("structured") or {}).get("kind") == "fresh_read_required"
        for _, tr in results
    )
    elision_rej = any(
        ("ELISION_MARKER_REJECTED" in str(tr.get("error") or ""))
        or ("elision" in str(tr.get("error") or "").lower())
        for _, tr in results
    )
    return {
        "old_not_found": old_not_found,
        "fresh_required": fresh_required,
        "elision_rej": elision_rej,
    }


def _analyze_targeted_edit(phase2: list) -> dict:
    """Extract all phase2 targeted-edit analysis facts."""
    calls = _phase2_tool_calls(phase2)
    results = _phase2_tool_results(phase2)
    targeted_calls = [n for n, _ in calls if n in _EDIT_TOOLS]
    targeted_success = any(tr.get("success") for n, tr in results if n in _EDIT_TOOLS)
    did_read = any(n == "file_read" for n, _ in calls)
    ok_edit_call_ids = {
        tr.get("call_id") for n, tr in results if n in _EDIT_TOOLS and tr.get("success")
    }
    ordering = _analyze_edit_ordering(phase2, ok_edit_call_ids)
    errors = _analyze_edit_errors(results)
    return {
        "targeted_calls": targeted_calls,
        "targeted_success": targeted_success,
        "did_read": did_read,
        "fresh_read_before_edit": ordering["fresh_read_before_edit"],
        "edit_on_index": ordering["edit_on_index"],
        "old_not_found": errors["old_not_found"],
        "fresh_required": errors["fresh_required"],
        "elision_rej": errors["elision_rej"],
    }


def _edits_applied(served_after: str) -> tuple:
    """Compute new_present, old_absent, edits_applied from the served page."""
    new_present = all(t in served_after for t in (HERO_NEW, CTA_NEW)) and (
        f"© {YEAR_NEW}" in served_after
    )
    old_absent = (
        (HERO_OLD not in served_after)
        and (CTA_OLD not in served_after)
        and (f"© {YEAR_OLD}" not in served_after)
    )
    return new_present, old_absent, new_present and old_absent


def _targeted_pass(
    built_ok: bool,
    old_present_before: bool,
    analysis: dict,
    edits_applied: bool,
    all_minimax: bool,
    post_terminal: int,
) -> bool:
    """The full PASS gate for the targeted-edit proof."""
    return bool(
        built_ok
        and old_present_before
        and analysis["targeted_calls"]
        and analysis["targeted_success"]
        and analysis["edit_on_index"]
        and analysis["fresh_read_before_edit"]
        and analysis["old_not_found"] < 3
        and not analysis["elision_rej"]
        and edits_applied
        and all_minimax
        and post_terminal == 0
    )


def _build_targeted_verdict(
    tag: str,
    cid: str,
    build_status: str,
    edit_status: str | None,
    built_ok: bool,
    served_before: str,
    served_after: str,
    analysis: dict,
    slice_: list,
    post_terminal: int,
) -> dict:
    """Assemble the targeted-edit verdict dict."""
    new_present, old_absent, edits_applied = _edits_applied(served_after)
    openrouter = sum(1 for ln in slice_ if "openrouter" in str(ln.get("host") or "").lower())
    all_minimax = _all_minimax(slice_, openrouter)
    old_present_before = all(t in served_before for t in (HERO_OLD, CTA_OLD, f"© {YEAR_OLD}"))
    return {
        "tag": tag,
        "cid": cid,
        "build_status": build_status,
        "edit_status": edit_status,
        "built_ok": built_ok,
        "PASS": _targeted_pass(
            built_ok, old_present_before, analysis, edits_applied, all_minimax, post_terminal
        ),
        "checks": {
            "build_produced_file": built_ok,
            "old_sentinels_present_before_edit": old_present_before,
            "targeted_edit_tools_called": analysis["targeted_calls"],
            "a_targeted_edit_succeeded": analysis["targeted_success"],
            "successful_edit_on_index_html": analysis["edit_on_index"],
            "fresh_read_before_first_edit": analysis["fresh_read_before_edit"],
            "any_fresh_read_or_required": analysis["did_read"] or analysis["fresh_required"],
            "old_text_not_found_count": analysis["old_not_found"],
            "no_elision_marker_rejected": not analysis["elision_rej"],
            "edits_applied_new_present_old_absent": edits_applied,
            "ledger_all_minimax_0_openrouter": all_minimax,
            "openrouter_count": openrouter,
            "ledger_hosts": sorted({str(ln.get("host")) for ln in slice_ if ln.get("host")}),
            "provider_calls_after_terminal": post_terminal,
            "provider_calls_this_run": len(slice_),
        },
        "served_after_len": len(served_after),
    }


def main() -> int:
    tag = sys.argv[sys.argv.index("--tag") + 1] if "--tag" in sys.argv else "r1"
    os.makedirs(RUN_DIR, exist_ok=True)
    n0 = len(_ledger())

    _conv = _post_json(
        "/conversations",
        {"owner_id": "local", "surface": "build", "autonomous": True, "title": f"cd9 {tag}"},
    )
    cid = _conv.get("conversation_id") or _conv.get("id")
    assert cid, f"no conversation id in {_conv}"

    # PHASE 1 — build the large index.html
    _post_json(f"/conversations/{cid}/messages", {"content": BUILD_PROMPT})
    build_status = _poll_terminal(cid, BUILD_TIMEOUT_S)
    seq_build = _last_seq(cid)
    served_before = _get_text(f"/conversations/{cid}/preview-app/")
    built_ok = HERO_OLD in served_before

    # PHASE 2 — targeted edit (only if the build produced the file)
    edit_status = None
    if built_ok:
        _post_json(f"/conversations/{cid}/followup", {"content": EDIT_PROMPT})
        edit_status = _poll_terminal(cid, EDIT_TIMEOUT_S)

    n_terminal = len(_ledger())
    time.sleep(6)
    ledger_all = _ledger()
    slice_ = ledger_all[n0:]
    post_terminal = len(ledger_all) - n_terminal

    events = _events(cid)
    phase2 = [e for e in events if int(e.get("seq") or 0) > seq_build]
    analysis = _analyze_targeted_edit(phase2)

    served_after = _get_text(f"/conversations/{cid}/preview-app/")
    verdict = _build_targeted_verdict(
        tag,
        cid,
        build_status,
        edit_status,
        built_ok,
        served_before,
        served_after,
        analysis,
        slice_,
        post_terminal,
    )
    dossier = {
        "verdict": verdict,
        "phase2_events": phase2,
        "ledger_slice": slice_,
        "served_before": served_before[:20000],
        "served_after": served_after[:20000],
    }
    with open(os.path.join(RUN_DIR, f"cd9_dossier_{tag}.json"), "w", encoding="utf-8") as f:
        json.dump(dossier, f, indent=2)
    print(json.dumps(verdict, indent=2))
    return 0 if verdict["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
