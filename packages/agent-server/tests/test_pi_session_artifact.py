"""PR I3 — the sanitized Pi session artifact.

The Pi sidecar's raw frame stream is persisted as DEBUG EVIDENCE, never product
truth, and is SECRET-FREE: the ephemeral gateway run token and any provider key
are stripped by BOTH the structural redactor (secret-named keys / sk-/Bearer
patterns) AND a literal pass over every encoded form of the known secret. These
tests prove a token/key embedded in a frame — as a value under a secret-named key,
as a bare value, and base64/percent-encoded — never reaches disk.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
from pathlib import Path

from disco.agent_server.build_kernel.pi_session_artifact import (
    sanitize_frame,
    write_pi_session_artifact,
)

CID = "c-art"
KERNEL = "k-art"
TOKEN = "run-tok-abcDEF1234567890abcDEF1234567890ZZ"
PROVIDER_KEY = "sk-secretproviderkey0123456789abcdef"


def test_sanitize_frame_strips_token_in_every_form() -> None:
    frame = {
        "type": "agent_event",
        "event": {
            "kind": "message_end",
            "gateway": {"apiKey": TOKEN},  # secret-named key
            "echo": f"Authorization: Bearer {TOKEN}",  # header-shaped
            "bare": TOKEN,  # bare value, no marker
            "b64": base64.b64encode(TOKEN.encode()).decode(),  # base64
            "pct": urllib.parse.quote(TOKEN, safe=""),  # percent-encoded
            "message": {"role": "assistant", "text": "ok"},
        },
    }
    cleaned = sanitize_frame(frame, [TOKEN, PROVIDER_KEY])
    blob = json.dumps(cleaned)
    assert TOKEN not in blob
    assert base64.b64encode(TOKEN.encode()).decode() not in blob
    assert urllib.parse.quote(TOKEN, safe="") not in blob
    # Non-secret content survives.
    assert "ok" in blob


def test_write_artifact_file_has_no_secret(tmp_path: Path) -> None:
    frames = [
        {"type": "ready", "model": "disco-selected", "gateway": {"apiKey": TOKEN}},
        {
            "type": "agent_event",
            "event": {
                "kind": "message_end",
                "leaked": TOKEN,
                "provider": PROVIDER_KEY,
                "message": {"role": "assistant", "text": "building"},
            },
        },
        {"type": "exit", "code": 0},
    ]
    path = write_pi_session_artifact(
        base_dir=tmp_path,
        conversation_id=CID,
        kernel_id=KERNEL,
        frames=frames,
        redact_secrets=[TOKEN, PROVIDER_KEY],
    )
    assert path is not None
    assert path.name == f"pi-session-{KERNEL}.jsonl"
    text = path.read_text()
    assert TOKEN not in text
    assert PROVIDER_KEY not in text
    # It IS a usable artifact: a header line + one line per frame, all valid JSON.
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == len(frames) + 1  # header + frames
    header = json.loads(lines[0])
    assert header["_artifact"] == "pi_session"
    assert header["conversation_id"] == CID
    for ln in lines[1:]:
        json.loads(ln)  # each frame line parses
    # The non-secret payload is preserved.
    assert "building" in text


def test_write_artifact_best_effort_returns_none_on_bad_dir(tmp_path: Path) -> None:
    """A write failure (base_dir is a FILE, not a directory) returns None, never
    raises — an evidence write must not break teardown."""
    clash = tmp_path / "afile"
    clash.write_text("x")
    out = write_pi_session_artifact(
        base_dir=clash / "child",  # parent is a file → mkdir fails
        conversation_id=CID,
        kernel_id=KERNEL,
        frames=[{"type": "ready"}],
        redact_secrets=[TOKEN],
    )
    assert out is None
