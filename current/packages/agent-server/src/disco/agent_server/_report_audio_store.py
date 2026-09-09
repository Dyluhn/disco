"""The generated-overview artifact on disk — keys, cache hits, sidecar, restore.

Everything about the cid-scoped audio-cache directory lives here: how an
overview is keyed to the report it narrates (``report_content_key``), which two
files a given (report, follow-ups, mode) triple maps to, whether a previous run
already wrote them, how a finished run is written atomically, and what earlier
runs left behind for a fresh page load (``existing_report_audio``).

The sidecar matters because the note ("read in the default voice", "summarised
to about 5 minutes") has to survive an agent-server restart: the browser
reloads with no memory of the run, so the JSON next to the MP3 is the only
place that fact can live.

Private decomposition of report_audio.py (the ``_report_normalize`` pattern):
report_audio re-exports these names, so the public seam stays
``disco.agent_server.report_audio``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from disco.core import ReportEvent


def report_content_key(report: ReportEvent) -> str:
    """Identity of the REPORT itself, ignoring which follow-ups were included.

    The artifact cache key mixes in the follow-up selection and the mode, so it
    cannot answer "is this audio for the report on screen?". A conversation can
    hold more than one research run, and restoring the previous run's audio
    onto a new report would be worse than showing no player at all.
    """
    seed = report.query + (report.summary or "") + "".join(s.markdown for s in report.sections)
    return hashlib.sha256(seed.encode()).hexdigest()[:12]


def _report_audio_cache_paths(
    report: ReportEvent,
    follow_ups: list[tuple[str, str]] | None,
    mode: str,
    out_dir: Path,
) -> tuple[Path, Path]:
    """B3: content-hash cache key — the key is a hash of the report text + follow-up
    content so that regenerating after an edit (new summary/sections/follow-ups)
    busts the cache and produces a fresh audio file.  The hash replaces the old
    mode+fu_count suffix which collided across edits of the same conversation.
    """
    content_seed = (
        report.query
        + (report.summary or "")
        + "".join(s.markdown for s in report.sections)
        + ("|".join(f"{q}:{a}" for q, a in follow_ups) if follow_ups else "")
        + mode
    )
    content_hash = hashlib.sha256(content_seed.encode()).hexdigest()[:12]
    mp3_path = out_dir / f"audio_overview_{mode}_{content_hash}.mp3"
    transcript_path = out_dir / f"audio_overview_{mode}_{content_hash}.md"
    return mp3_path, transcript_path


def _audio_cache_hit(mp3_path: Path, transcript_path: Path) -> bool:
    """True when a previous run for this conversation already wrote both
    artifacts verbatim.  The pipeline is deterministic given the report, the
    LLM, and the voice settings — and the LLM call is the slow / expensive
    step.  This makes the endpoint idempotent (idempotency is good for
    retries, and the UI can re-press the button without re-spending RAM)."""
    return mp3_path.exists() and transcript_path.exists()


def _meta_path(mp3_path: Path) -> Path:
    return mp3_path.with_suffix(".json")


def read_report_audio_meta(mp3_path: Path) -> dict[str, str]:
    """The sidecar for one generated overview: its mode and its note.

    The note has to survive an agent-server restart -- the browser reloads with
    no memory of the run, and the artifact on the data volume is the only thing
    left -- so it is written next to the MP3 rather than held in the process.
    """
    try:
        data = json.loads(_meta_path(mp3_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


# The sentence shown under a restored player whose artifact we cannot prove was
# made from the report currently on screen.
_EARLIER_VERSION_NOTE = "This audio was made from an earlier version of this report."


def _audio_entry(
    mp3_path: Path, transcript_path: Path, meta: dict[str, str], *, stale: bool
) -> dict[str, str]:
    """One listing row for a generated overview.

    ``mode`` comes from the sidecar when there is one and from the filename
    otherwise -- ``audio_overview_<mode>_<hash>.mp3`` is the only name this
    module has ever written, so the mode is recoverable either way.
    """
    parts = mp3_path.stem.split("_")
    note = meta.get("note", "")
    if stale:
        note = f"{_EARLIER_VERSION_NOTE} {note}".strip()
    return {
        "mode": meta.get("mode") or (parts[2] if len(parts) > 2 else "podcast"),
        "note": note,
        "stale": "true" if stale else "",
        "mp3_name": mp3_path.name,
        "transcript_name": transcript_path.name,
    }


def existing_report_audio(out_dir: Path, report_key: str) -> list[dict[str, str]]:
    """Overviews already on disk for this conversation, newest first.

    Used to restore the player on a fresh page load: the artifacts live on the
    data volume, so audio the user generated before a restart (or before
    closing the tab) is still there and must not look like it never happened.
    Only files this module wrote are matched, and only inside ``out_dir``.

    Two passes, because "which report is this audio for?" is only answerable
    for artifacts written since the sidecar existed:

      1. Sidecars naming THIS report win outright, unmarked.
      2. Otherwise take whatever is there -- an artifact from a build with no
         sidecar at all, or one whose sidecar names a different report -- and
         mark it ``stale`` so the UI can say it came from an earlier version.
         Showing nothing while an MP3 sits on the volume is the worse answer:
         that is the whole of UI-42, and it would have persisted for every
         overview generated before this change shipped.
    """
    if not out_dir.is_dir():
        return []
    exact: list[tuple[float, dict[str, str]]] = []
    other: list[tuple[float, dict[str, str]]] = []
    for mp3_path in out_dir.glob("audio_overview_*.mp3"):
        transcript_path = mp3_path.with_suffix(".md")
        if not transcript_path.is_file():
            continue  # a half-written pair, or an orphan transcript
        meta = read_report_audio_meta(mp3_path)
        mtime = mp3_path.stat().st_mtime
        if meta.get("report_key") == report_key:
            exact.append((mtime, _audio_entry(mp3_path, transcript_path, meta, stale=False)))
        else:
            other.append((mtime, _audio_entry(mp3_path, transcript_path, meta, stale=True)))
    chosen = exact or other
    return [entry for _mtime, entry in sorted(chosen, key=lambda row: row[0], reverse=True)]


def _write_audio_artifacts(
    mp3_path: Path,
    transcript_path: Path,
    mixed_mp3: bytes,
    transcript_text: str,
    meta: dict[str, str],
) -> None:
    """Step 5: write to the cid-scoped cache dir.  Atomic: write to .part then
    rename, so a partial file is never served."""
    mp3_tmp = mp3_path.with_suffix(mp3_path.suffix + ".part")
    tr_tmp = transcript_path.with_suffix(transcript_path.suffix + ".part")
    meta_tmp = _meta_path(mp3_path).with_suffix(".json.part")
    mp3_tmp.write_bytes(mixed_mp3)
    tr_tmp.write_text(transcript_text, encoding="utf-8")
    meta_tmp.write_text(json.dumps(meta), encoding="utf-8")
    mp3_tmp.replace(mp3_path)
    tr_tmp.replace(transcript_path)
    meta_tmp.replace(_meta_path(mp3_path))
