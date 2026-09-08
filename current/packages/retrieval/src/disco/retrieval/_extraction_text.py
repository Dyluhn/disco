"""Recognize unusable extracted text before it becomes retrieval evidence."""

from __future__ import annotations

import re
import unicodedata

from .models import ExtractedDoc


def corrupted_text(text: str) -> bool:
    """Heavy Unicode replacement is lost source bytes, not evidence or a foreign script.

    Allow occasional damaged glyphs. Recorded compressed-body incidents lose over
    40% of their characters; this conservative bound rejects such unusable bodies.
    """
    replacements = text.count("\ufffd")
    return replacements >= 32 and replacements * 10 >= len(text)


CORRUPTED_TEXT_ERROR = "extraction contains corrupted text; original source needs a fresh read"


# This is deliberately a small, template recognizer rather than a keyword
# classifier.  The strings below describe the first-screen shells observed in
# the intake captures.  In particular, a mention of CAPTCHA, bots, Anubis, or
# verification in an otherwise useful article is not sufficient.
CHALLENGE_PAGE_ERROR = "source returned a CAPTCHA or bot-verification challenge page"

_SPACE_RE = re.compile(r"\s+")


def _fold(text: str) -> str:
    """Case-fold and make punctuation/whitespace variants comparable."""

    return _SPACE_RE.sub(" ", unicodedata.normalize("NFKC", text).casefold()).strip()


def _radware_challenge(title: str, leading: str) -> bool:
    # Both Radware captures begin with the apology shell and carry an incident
    # identifier.  The remaining marker distinguishes an actual user action
    # from a research article merely discussing Radware CAPTCHA.
    radware_title = title.startswith("radware") and ("captcha" in title or "bot manager" in title)
    radware_lead = bool(
        re.match(r"^#+\s*we apologize for the inconvenience", leading)
        or re.match(r"^we apologize for the inconvenience", leading)
    )
    radware_incident = "incident id" in leading
    radware_action = (
        "confirm you are a human" in leading
        or "ticking the box" in leading
        or "solve this captcha" in leading
        or "request unblock" in leading
    )
    return radware_title and radware_lead and radware_incident and radware_action


def _anubis_challenge(leading: str) -> bool:
    # Anubis pages identify the administrator's anti-scraping gate and explain
    # its proof-of-work mechanism immediately below the loading shell.
    anubis_lead = bool(
        re.match(
            r"^(?:sciences sciences\s+)?(?:#+\s*)?making sure you're not a bot!?\s+"
            r"loading\.\.\.\s+you are seeing this because",
            leading,
        )
    )
    anubis_reason = (
        "administrator of this website" in leading
        and "anubis" in leading
        and ("proof-of-work" in leading or "proof of work" in leading)
    )
    return (
        anubis_lead
        and anubis_reason
        and ("hashcash" in leading or "modern javascript" in leading or "scraping" in leading)
    )


def _ncstate_challenge(title: str, leading: str) -> bool:
    # NC State's captured shell combines the bot-verification wait message with
    # the visible proof-of-work difficulty/speed readout.
    ncstate_title = (
        "nc state" in title
        and "libraries" in title
        and ("bot detection" in title or "bot" in title)
    )
    ncstate_lead = bool(
        re.match(r"^#+\s*verifying you are not a bot", leading)
        or re.match(r"^#+\s*please wait a moment while we ensure the security", leading)
        or re.match(r"^verifying you are not a bot", leading)
        or re.match(r"^please wait a moment while we ensure the security", leading)
    )
    ncstate_work = (
        "calculating" in leading
        and "difficulty" in leading
        and ("speed" in leading or "kh/s" in leading or "kh s" in leading)
    )
    return ncstate_title and ncstate_lead and ncstate_work


def challenge_page(title: str, text: str) -> bool:
    """Return whether *text* is one of the captured human-verification shells.

    Detection is intentionally bounded to the title and the first 2,000
    characters.  Each template needs a lead/page-shell marker and several
    independent markers.  This covers the Radware, Anubis, and NC State forms
    captured in V43; it does not claim to identify every anti-bot product or a
    challenge whose shell has been stripped by an extractor.
    """

    folded_title = _fold(title)
    leading = _fold(text)[:2_000]

    return (
        _radware_challenge(folded_title, leading)
        or _anubis_challenge(leading)
        or _ncstate_challenge(folded_title, leading)
    )


def reject_challenge_page(doc: ExtractedDoc) -> ExtractedDoc:
    """Turn a recognized successful challenge response into a blocked miss."""

    if not doc.fetched_ok or doc.status != "ok":
        return doc
    if not challenge_page(doc.title, doc.content):
        return doc
    return doc.model_copy(
        update={
            "fetched_ok": False,
            "status": "blocked",
            "error": CHALLENGE_PAGE_ERROR,
            "passages": [],
        }
    )
