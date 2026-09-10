"""PDF warning provenance remains local to its document's synchronous reader."""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from disco.retrieval._pdf_warnings import pdf_warnings


def test_concurrent_pdf_reads_do_not_receive_each_others_warnings() -> None:
    barrier = threading.Barrier(2, timeout=5)

    def read(label: str) -> list[str]:
        with pdf_warnings() as warnings:
            barrier.wait()
            logging.getLogger("pypdf._page").warning("Unreadable figure in %s", label)
            barrier.wait()
            return list(warnings)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(read, ["first document", "second document"]))
    assert first == ["Unreadable figure in first document"]
    assert second == ["Unreadable figure in second document"]
