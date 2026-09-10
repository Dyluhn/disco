"""Focused, provider-independent checks for the captured challenge shells."""

import pytest
from disco.retrieval._extraction_text import (
    CHALLENGE_PAGE_ERROR,
    challenge_page,
    reject_challenge_page,
)
from disco.retrieval.models import ExtractedDoc, Passage

CAPTURED = [
    (
        "Making sure you're not a bot!",
        """# Making sure you're not a bot!
Loading...
You are seeing this because the administrator of this website has set up Anubis
to protect the server against the scourge of AI companies aggressively scraping
websites. Anubis uses a Proof-of-Work scheme in the vein of Hashcash.""",
    ),
    (
        "Radware Bot Manager Captcha",
        """## We apologize for the inconvenience...
To ensure we keep this website safe, please can you confirm you are a human by
ticking the box below.
Incident ID: f98c521a-cnvj-4f1c-b259-240414e360af""",
    ),
    (
        "NC State University Libraries Bot Detection",
        """# Verifying you are not a bot...
Please wait a moment while we ensure the security of your connection.
Calculating...Difficulty: 2, Speed: 0kH/s""",
    ),
    (
        "Radware Captcha Page",
        """# We apologize for the inconvenience...
...but your activity and behavior on this site made us think that you are a bot.
Incident ID: 40168cb1-dkd2-49a8-a6c6-3f53facd34b1
Please solve this CAPTCHA to request unblock to the website""",
    ),
]


@pytest.mark.parametrize("title,text", CAPTURED)
def test_captured_challenge_templates_are_recognized(title, text):
    assert challenge_page(title, text)


@pytest.mark.parametrize(
    "title,text",
    [
        (
            "CAPTCHA methods in modern web security",
            "This substantive article compares CAPTCHA verification systems and bot detection.",
        ),
        (
            "Anubis proof of work in distributed systems",
            "Anubis uses Hashcash style proof of work to limit abuse; this is a short abstract.",
        ),
        (
            "Verification at NC State University Libraries",
            "The study evaluates bot detection and security verification for library services.",
        ),
        (
            "Research note: Radware Bot Manager Captcha",
            "The article quotes a historical page: “We apologize for the inconvenience. "
            "Please solve this CAPTCHA.” It then analyzes false positives in detail. "
            "The discussion includes an Incident ID as an example.",
        ),
        (
            "A study of Anubis challenge behavior",
            "The article quotes a captured page near its introduction: # Making sure you're "
            "not a bot! Loading... You are seeing this because the administrator of this "
            "website has set up Anubis to protect the server. Anubis uses a Proof-of-Work "
            "scheme in the vein of Hashcash. The study then compares completion rates.",
        ),
        (
            "Radware Bot Manager Captcha",
            "This substantive article analyzes Radware false positives before quoting a "
            "historical response. We apologize for the inconvenience. Please solve this "
            "CAPTCHA. Incident ID: an-example-only.",
        ),
        (
            "A short abstract with Unicode",
            "Résumé: 人間による検証と CAPTCHA の評価。これは通常の研究要約です。",
        ),
    ],
)
def test_topics_quotes_short_abstracts_and_unicode_are_retained(title, text):
    assert not challenge_page(title, text)


def test_page_shell_and_independent_markers_are_required():
    assert not challenge_page(
        "Radware Bot Manager Captcha",
        "Incident ID: 123. Please solve this CAPTCHA to request unblock.",
    )
    assert not challenge_page(
        "Making sure you're not a bot!",
        "Loading... Anubis is discussed in this ordinary product article.",
    )
    assert not challenge_page(
        "NC State University Libraries Bot Detection",
        "Verifying you are not a bot; the paper studies library access.",
    )


def test_reject_preserves_provenance_and_removes_evidence():
    url = "https://example.test/challenge"
    title, text = CAPTURED[1]
    passage = Passage(id="p0", source_url=url, source_title=title, text=text)
    doc = ExtractedDoc(url=url, title=title, content=text, passages=[passage])

    rejected = reject_challenge_page(doc)

    assert rejected.url == url
    assert rejected.title == title
    assert rejected.content == text
    assert rejected.passages == []
    assert rejected.fetched_ok is False
    assert rejected.status == "blocked"
    assert rejected.error == CHALLENGE_PAGE_ERROR
    assert doc.fetched_ok is True and doc.passages == [passage]


def test_reject_leaves_failures_and_ordinary_documents_unchanged():
    failure = ExtractedDoc(
        url="https://example.test/failure",
        title="Making sure you're not a bot!",
        content=CAPTURED[0][1],
        fetched_ok=False,
        status="error",
        error="connection failed",
    )
    ordinary = ExtractedDoc(
        url="https://example.test/article",
        title="CAPTCHA research",
        content="A useful abstract about verification.",
    )

    assert reject_challenge_page(failure) == failure
    assert reject_challenge_page(ordinary) == ordinary
