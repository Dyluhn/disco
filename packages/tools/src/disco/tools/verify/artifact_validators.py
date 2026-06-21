"""Cheap (PR-tier) mechanical artifact validators for the evidence harness (W14a)."""

import subprocess


def validate_pdf(path: str) -> list[str]:
    """Validate a PDF file using pdfinfo and pdftotext."""
    problems: list[str] = []

    # Check page count
    try:
        result = subprocess.run(
            ["pdfinfo", path], capture_output=True, text=True
        )
    except FileNotFoundError:
        return ["pdfinfo unavailable"]
    if result.returncode != 0:
        problems.append(f"pdfinfo failed: {result.stderr}")
    else:
        pages = None
        for line in result.stdout.splitlines():
            if line.startswith("Pages:"):
                pages = int(line.split(":", 1)[1].strip())
                break
        if pages is None or pages == 0:
            problems.append("pdf has 0 pages")

    # Check for extractable text (pdftotext is optional — skip silently if absent)
    try:
        result = subprocess.run(
            ["pdftotext", path, "-"], capture_output=True, text=True
        )
        if result.returncode == 0 and not result.stdout.strip():
            problems.append("pdf has no extractable text")
    except FileNotFoundError:
        pass  # pdftotext optional; pdfinfo check is primary

    return problems


def validate_audio(path: str) -> list[str]:
    """Validate an audio file using ffprobe."""
    problems: list[str] = []

    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return ["ffprobe unavailable"]
    if result.returncode != 0:
        problems.append(f"ffprobe failed: {result.stderr}")
    else:
        try:
            duration = float(result.stdout.strip())
            if duration <= 0:
                problems.append("audio duration is zero")
        except ValueError:
            problems.append("audio duration is zero")

    return problems


def validate_sheet(path: str) -> list[str]:
    """Validate an Excel workbook using openpyxl."""
    import openpyxl

    problems: list[str] = []

    try:
        wb = openpyxl.load_workbook(path)
    except Exception as e:
        return [f"sheet failed to open: {e}"]

    if len(wb.sheetnames) == 0:
        problems.append("sheet has no worksheets")

    return problems


def _looks_procedural(img_bytes: bytes) -> bool:
    """True iff an image looks like the keyless ``pil-procedural`` placeholder backend's
    output (W3 bug signature). That backend draws a flat ≤~7-colour palette plus a single
    corner-to-corner diagonal; real photos/diffusion images have hundreds+ of colours.
    Requires BOTH low colour diversity AND the corner-to-corner diagonal so a genuine flat
    illustration/chart is not falsely flagged. Seed-independent (catches any placeholder)."""
    from collections import Counter
    from io import BytesIO

    try:
        from PIL import Image, ImageOps
    except ImportError:
        return False  # can't introspect without Pillow → don't flag
    try:
        img = Image.open(BytesIO(img_bytes)).convert("RGB")
    except Exception:
        return False
    w, h = img.size
    if w < 4 or h < 4:
        return False
    # Posterize before counting so JPEG compression noise doesn't inflate the palette. Then
    # require BOTH a small flat palette AND the procedural backend's SPECIFIC signature: a
    # corner-to-corner diagonal accent line. The diagonal is what distinguishes a procedural
    # placeholder from ordinary flat art (a logo/chart also has a small palette + a dominant
    # background, so a background-fraction test alone false-positives on real flat art —
    # codex round-4). NOTE: this is a BYTES heuristic and is PNG-reliable; the format-agnostic
    # defense against procedural images is PROVENANCE (the image_generate `placeholder` field,
    # which disco-verify's forbid check reads) plus W3 stripping them at generation time.
    probe = ImageOps.posterize(img, 2)
    colors = probe.getcolors(maxcolors=8192)
    if colors is None or len(colors) > 24:
        return False  # rich image → not a flat placeholder
    total = max(1, w * h)
    count_by_color = {c: cnt for cnt, c in colors}
    bg_color = max(colors, key=lambda cc: cc[0])[1]  # most common colour = the background
    px = probe.load()
    if px is None:
        return False
    n = min(w, h)
    diag = [px[round(i * (w - 1) / (n - 1)), round(i * (h - 1) / (n - 1))] for i in range(n)]
    diag_top, diag_count = Counter(diag).most_common(1)[0]
    # Procedural placeholder = a corner-to-corner accent LINE: one colour dominates the
    # diagonal, that colour is NOT the background, AND it is a THIN feature overall (a drawn
    # 1px line is rare across the whole image). A big shape that merely crosses the diagonal of
    # real flat art fails the last test, so genuine logos/charts aren't false-flagged (codex r4).
    return (
        diag_count >= n * 0.6
        and diag_top != bg_color
        and count_by_color.get(diag_top, 0) / total < 0.15
    )


def validate_deck_deliverable(descriptor: dict) -> list[str]:
    """A presentable deck deliverable must not default to RAW HTML (W3/#4). ``descriptor`` is
    the deliverable metadata (e.g. the deck event payload) carrying a ``format`` field."""
    problems: list[str] = []
    if str(descriptor.get("format", "")).lower() == "html":
        problems.append("raw_html_default")
    return problems


def validate_deck_file(path: str) -> list[str]:
    """Inspect a rendered deck (``.pptx`` or ``.html``) for embedded PROCEDURAL placeholder
    images (W3 bug). Extracts images (pptx ``ppt/media/*`` or html base64 data-URIs) and flags
    if any looks procedural."""
    import base64
    import pathlib
    import re
    import zipfile

    problems: list[str] = []
    images: list[bytes] = []
    low = path.lower()
    if low.endswith((".html", ".htm")):
        text = pathlib.Path(path).read_text(errors="replace")
        for m in re.finditer(r"data:image/(?:png|jpe?g);base64,([A-Za-z0-9+/=]+)", text):
            try:
                images.append(base64.b64decode(m.group(1)))
            except Exception:
                continue
    elif low.endswith(".pptx"):
        try:
            with zipfile.ZipFile(path) as z:
                for name in z.namelist():
                    if name.startswith("ppt/media/") and name.lower().endswith(
                        (".png", ".jpg", ".jpeg")
                    ):
                        images.append(z.read(name))
        except zipfile.BadZipFile as exc:
            return [f"corrupt pptx: {exc}"]
    if any(_looks_procedural(b) for b in images):
        problems.append("procedural_placeholder_image")
    return problems
