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
import time
import urllib.request

AGENT = os.environ.get("DISCO_AGENT_URL", "http://localhost:8000")
LEDGER = os.environ.get("MINIMAX_RELAY_LOG", "")
RUN_DIR = os.environ.get(
    "RUN_DIR",
    "/tmp/claude-1000/-var-home-dylan/c1e33ca0-6ffb-409a-8303-c38c11bb886d/scratchpad/p1blive3b",
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
    built_ok = HERO_OLD in served_before  # the seed token is in the served page → index.html exists

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

    def calls(evs):
        for e in evs:
            tc = e.get("tool_call")
            if tc:
                yield tc.get("tool_name"), (tc.get("arguments") or {})

    def results(evs):
        for e in evs:
            tr = e.get("tool_result")
            if tr:
                yield tr.get("tool_name"), tr

    targeted_calls = [n for n, _ in calls(phase2) if n in _EDIT_TOOLS]
    targeted_success = any(tr.get("success") for n, tr in results(phase2) if n in _EDIT_TOOLS)
    did_read = any(n == "file_read" for n, _ in calls(phase2))

    # ORDERING (codex r3): a file_read of index.html must come BEFORE the first SUCCESSFUL targeted
    # edit — the actual fresh-read-BEFORE-edit guarantee, not just "a read happened somewhere".
    ok_edit_call_ids = {
        tr.get("call_id") for n, tr in results(phase2) if n in _EDIT_TOOLS and tr.get("success")
    }

    def _seq_first_read() -> int | None:
        for e in phase2:
            tc = e.get("tool_call") or {}
            if tc.get("tool_name") == "file_read" and "index.html" in str(
                (tc.get("arguments") or {}).get("path", "")
            ):
                return int(e.get("seq") or 0)
        return None

    def _seq_first_edit_ok() -> int | None:
        for e in phase2:
            tc = e.get("tool_call") or {}
            if tc.get("tool_name") in _EDIT_TOOLS and tc.get("call_id") in ok_edit_call_ids:
                return int(e.get("seq") or 0)
        return None

    rseq, eseq = _seq_first_read(), _seq_first_edit_ok()
    fresh_read_before_edit = rseq is not None and eseq is not None and rseq < eseq

    # the SUCCESSFUL targeted edit must be on index.html (not a misdirected edit on another file).
    def _targets_index(name: str, a: dict) -> bool:
        if name == "run_project_script":
            return any(
                "index.html" in str((o or {}).get("path", "")) for o in (a.get("operations") or [])
            )
        return "index.html" in str(a.get("path", ""))

    edit_on_index = any(
        (e.get("tool_call") or {}).get("call_id") in ok_edit_call_ids
        and (e.get("tool_call") or {}).get("tool_name") in _EDIT_TOOLS
        and _targets_index(
            (e.get("tool_call") or {}).get("tool_name") or "",
            (e.get("tool_call") or {}).get("arguments") or {},
        )
        for e in phase2
    )
    old_not_found = sum(
        1 for _, tr in results(phase2) if "old_text_not_found" in str(tr.get("error") or "")
    )
    fresh_required = any(
        "FRESH_READ_REQUIRED" in str(tr.get("error") or "")
        or (tr.get("structured") or {}).get("kind") == "fresh_read_required"
        for _, tr in results(phase2)
    )
    elision_rej = any(
        ("ELISION_MARKER_REJECTED" in str(tr.get("error") or ""))
        or ("elision" in str(tr.get("error") or "").lower())
        for _, tr in results(phase2)
    )
    served_after = _get_text(f"/conversations/{cid}/preview-app/")
    new_present = all(t in served_after for t in (HERO_NEW, CTA_NEW)) and (
        f"© {YEAR_NEW}" in served_after
    )
    # OLD tokens must be GONE (codex r3): a real IN-PLACE replace, not an append that leaves the old
    # content alongside the new (which a partial/append edit would).
    old_absent = (
        (HERO_OLD not in served_after)
        and (CTA_OLD not in served_after)
        and (f"© {YEAR_OLD}" not in served_after)
    )
    edits_applied = new_present and old_absent
    openrouter = sum(1 for ln in slice_ if "openrouter" in str(ln.get("host") or "").lower())
    all_minimax = (
        bool(slice_)
        and openrouter == 0
        and all("minimax" in str(ln.get("host") or "").lower() for ln in slice_)
    )
    # the BUILD must have produced ALL three old sentinels (else 'old absent' after is vacuous —
    # a build that never wrote them would trivially pass). Proven from the served page after build.
    old_present_before = all(t in served_before for t in (HERO_OLD, CTA_OLD, f"© {YEAR_OLD}"))

    verdict = {
        "tag": tag,
        "cid": cid,
        "build_status": build_status,
        "edit_status": edit_status,
        "built_ok": built_ok,
        "PASS": bool(
            built_ok
            and old_present_before
            and targeted_calls
            and targeted_success
            and edit_on_index
            and fresh_read_before_edit
            and old_not_found < 3
            and not elision_rej
            and edits_applied
            and all_minimax
            and post_terminal == 0
        ),
        "checks": {
            "build_produced_file": built_ok,
            "old_sentinels_present_before_edit": old_present_before,
            "targeted_edit_tools_called": targeted_calls,
            "a_targeted_edit_succeeded": targeted_success,
            "successful_edit_on_index_html": edit_on_index,
            "fresh_read_before_first_edit": fresh_read_before_edit,
            "any_fresh_read_or_required": did_read or fresh_required,
            "old_text_not_found_count": old_not_found,
            "no_elision_marker_rejected": not elision_rej,
            "edits_applied_new_present_old_absent": edits_applied,
            "ledger_all_minimax_0_openrouter": all_minimax,
            "openrouter_count": openrouter,
            "ledger_hosts": sorted({str(ln.get("host")) for ln in slice_ if ln.get("host")}),
            "provider_calls_after_terminal": post_terminal,
            "provider_calls_this_run": len(slice_),
        },
        "served_after_len": len(served_after),
    }
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
