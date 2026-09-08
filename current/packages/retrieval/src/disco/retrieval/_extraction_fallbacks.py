"""The two pages the browser cannot read, read another way.

Extracted from :mod:`.live` (module size cap). Both routes below were measured
on the 2026-09-03 batches — 8 runs, 6,501 page fetches, 48% of extractions ok:

* **arXiv abstracts.** 1,425 ``arxiv.org/abs/`` pages went to the crawler and
  ZERO came back ok. At that volume Cloudflare answers the browser with a JS
  challenge (307) and the abstract never arrives. arXiv's own export API answers
  the same abstract as Atom XML with no challenge at all, so an /abs page is
  never a browser page again — it is an API call, paced at arXiv's stated
  courtesy rate of one request every three seconds.
* **Anti-bot pages.** 1,139 failures were the same JS challenge on ssrn, nejm,
  sciencedirect, researchgate, oup, cambridge and sage. The Internet Archive
  holds the page the challenge is hiding. A recovered doc carries the ORIGINAL
  url and re-chunked passages, because a citation must point at the source and
  not at the archive that happened to keep a copy of it. The archived copy is
  read IN PROCESS rather than through the crawler: on a later batch all 17
  archive attempts came back 429, because archive.org rate-limits the shared
  exit the Crawl4AI server sits behind while it answers the disco host itself
  normally. Hence the ``fetch_archive`` seam — the wiring hands it the bundled
  in-process extractor, which is a browser the archive will actually talk to.

Both routes take their fetch alongside the extraction provider's own ``failed``
and ``chunk`` seams, the way :func:`.extract_with_isolation` already does, so the
provider keeps ownership of how one ExtractedDoc is built and a test can drive
either route with no socket at all.

``_pace_now`` / ``_pace_sleep`` are the test seams (mirroring the ones in
:mod:`._transport_retry`): a hermetic test drives the gates without spending
real seconds on them. Production never reassigns either.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections.abc import Awaitable, Callable
from xml.etree import ElementTree

import httpx

from ._extraction_text import reject_challenge_page
from ._transport_retry import _extraction_error_class
from .models import ExtractedDoc, Passage

_LOG = logging.getLogger(__name__)

# An arXiv ABSTRACT page, and the API that answers it without a browser. Ids look
# like 2401.00001, 2401.00001v2, or the old astro-ph/0601001 (a slash included).
_ARXIV_ABS = re.compile(r"^https?://(?:www\.)?arxiv\.org/abs/(?P<id>[^?#]+)$")
_ARXIV_API = "https://export.arxiv.org/api/query"
_ATOM = "{http://www.w3.org/2005/Atom}"

# The archive's "as captured, no toolbar" replay prefix — the archived BYTES,
# with none of the Wayback chrome a crawler would otherwise read as content.
_WAYBACK_REPLAY = "https://web.archive.org/web/2id_/"

_pace_now: Callable[[], float] = time.monotonic
_pace_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

Chunker = Callable[[str, str, str], list[Passage]]
FailedDoc = Callable[[str, str], ExtractedDoc]
# One archived URL in, one ExtractedDoc out. A refusal arrives as a non-ok doc
# carrying its own reason, not as an exception — that is what lets the log name
# the HTTP status instead of an exception class.
ArchiveFetch = Callable[[str], Awaitable[ExtractedDoc]]


class _PaceGate:
    """One process-wide minimum interval between outbound calls to one host.

    The slot is RESERVED under a plain lock and waited for OUTSIDE it, so
    arrivals leave in order and no lock is ever held across an await.
    """

    def __init__(self, interval_s: float) -> None:
        self._interval = interval_s
        self._next = 0.0
        self._guard = threading.Lock()

    async def wait(self) -> None:
        """Hold this call until its reserved slot comes around."""
        with self._guard:
            now = _pace_now()
            start = max(now, self._next)
            self._next = start + self._interval
        if start > now:
            await _pace_sleep(start - now)

    def reset(self) -> None:
        """Forget the reservations (mirrors `reset_search_pacing`)."""
        with self._guard:
            self._next = 0.0


# arXiv's stated courtesy rate is one API request every three seconds. The
# archive is gentler, but it is still one host being asked for every blocked
# page in the run.
_ARXIV_GATE = _PaceGate(3.0)
_WAYBACK_GATE = _PaceGate(1.0)


def reset_extraction_pacing() -> None:
    """Drop both outbound gates' reservations."""
    _ARXIV_GATE.reset()
    _WAYBACK_GATE.reset()


def arxiv_id(url: str) -> str:
    """The arXiv id an /abs URL names, or "" when the URL is not one."""
    match = _ARXIV_ABS.match(url.strip())
    return match.group("id").rstrip("/") if match else ""


async def extract_arxiv_abstracts(
    urls: list[str],
    *,
    timeout_s: float,
    transport: httpx.AsyncBaseTransport | None,
    max_chars: int | None,
    chunk: Chunker,
    failed: FailedDoc,
) -> dict[str, ExtractedDoc]:
    """Every /abs URL in ``urls`` as an ok doc built from its API abstract.

    Paced, and never retried: the gate has already spent the wait arXiv asks for,
    and a second immediate query would spend it again for the same answer.
    """
    docs: dict[str, ExtractedDoc] = {}
    for url in urls:
        await _ARXIV_GATE.wait()
        docs[url] = await _one_abstract(
            url, timeout_s, transport, max_chars, chunk=chunk, failed=failed
        )
    return docs


async def _one_abstract(
    url: str,
    timeout_s: float,
    transport: httpx.AsyncBaseTransport | None,
    max_chars: int | None,
    *,
    chunk: Chunker,
    failed: FailedDoc,
) -> ExtractedDoc:
    _LOG.info("arxiv export api fetch for %s", url)
    try:
        async with httpx.AsyncClient(
            timeout=timeout_s, transport=transport, trust_env=False, follow_redirects=True
        ) as client:
            resp = await client.get(_ARXIV_API, params={"id_list": arxiv_id(url)})
    except httpx.HTTPError as exc:
        return failed(url, f"arxiv api: {type(exc).__name__}: {exc}")
    if resp.status_code != 200:
        return failed(url, f"arxiv api http {resp.status_code}")
    title, summary = _arxiv_entry(resp.text)
    if not summary:
        return failed(url, "arxiv api: no entry")
    content = summary[:max_chars]
    title = title or url
    return ExtractedDoc(
        url=url,
        title=title,
        content=content,
        passages=chunk(url, title, content),
        fetched_ok=True,
        status="ok",
    )


def _arxiv_entry(feed: str) -> tuple[str, str]:
    """The feed's single entry as (title, whitespace-collapsed abstract).

    ("", "") when the feed is unparseable or carries no entry — an id arXiv does
    not know answers 200 with an empty feed, which is a miss, not an abstract.
    """
    try:
        entry = ElementTree.fromstring(feed).find(f"{_ATOM}entry")
    except ElementTree.ParseError:
        return "", ""
    if entry is None:
        return "", ""
    return (
        " ".join((entry.findtext(f"{_ATOM}title") or "").split()),
        " ".join((entry.findtext(f"{_ATOM}summary") or "").split()),
    )


async def recover_blocked_pages(
    docs: dict[str, ExtractedDoc], *, fetch_archive: ArchiveFetch, chunk: Chunker
) -> dict[str, ExtractedDoc]:
    """The anti-bot failures in ``docs``, re-read out of the Internet Archive.

    Only anti-bot is retried this way. A timeout is transient and already has its
    own retry; a 404 is a settled fact about the source. Whatever the archive
    cannot supply is left exactly as the crawler reported it.
    """
    blocked = [url for url, doc in docs.items() if _extraction_error_class(doc) == "anti_bot"]
    recovered: dict[str, ExtractedDoc] = {}
    for url in blocked:
        await _WAYBACK_GATE.wait()
        archived = await _one_snapshot(url, fetch_archive=fetch_archive, chunk=chunk)
        if archived is None:
            continue  # `_one_snapshot` has already logged why
        _LOG.info("wayback fallback used for %s", url)
        recovered[url] = archived
    return recovered


async def _one_snapshot(
    url: str, *, fetch_archive: ArchiveFetch, chunk: Chunker
) -> ExtractedDoc | None:
    """The archived copy of one refused page, rebadged as the source itself."""
    snapshot = _WAYBACK_REPLAY + url
    try:
        doc = await fetch_archive(snapshot)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        _LOG.info("wayback copy of %s: %s", url, type(exc).__name__)
        return None
    doc = reject_challenge_page(doc)
    if not doc.fetched_ok or not doc.content:
        _LOG.info("wayback copy of %s: %s", url, _miss_reason(doc))
        return None
    return doc.model_copy(update={"url": url, "passages": chunk(url, doc.title, doc.content)})


def _miss_reason(doc: ExtractedDoc) -> str:
    """Why one archived copy is unusable — the HTTP status whenever there is one.

    The status IS the diagnosis: a 403 is a publisher excluded from replay and a
    real miss, while a 429 is our own exit being rate-limited and a bug to fix.
    Logging only the exception class made a whole batch of 429s read as "found
    nothing", which is the one reading that was wrong.
    """
    match = re.search(r"HTTP \d+", doc.error or "")
    return match.group(0) if match else (doc.error or "no snapshot")


__all__ = [
    "arxiv_id",
    "extract_arxiv_abstracts",
    "recover_blocked_pages",
    "reset_extraction_pacing",
]
