"""Real HTTP response parsing must return bounded, decoded representation bytes."""

import gzip
import io
import zlib

import pytest
from disco.core import host_egress


class WireSocket:
    def __init__(self, payload):
        self.payload = payload
        self.sent = b""
        self.closed = False

    def makefile(self, *args):
        return io.BytesIO(self.payload)

    def sendall(self, data):
        self.sent += data

    def close(self):
        self.closed = True


def request(monkeypatch, body, encoding="", *, limit=5000, status=200, method="GET", headers=None):
    fields = [
        f"HTTP/1.1 {status} OK",
        f"Content-Length: {len(body)}",
        "Content-Type: text/html; charset=utf-8",
    ]
    if encoding:
        fields.append(f"Content-Encoding: {encoding}")
    sock = WireSocket(("\r\n".join(fields) + "\r\n\r\n").encode() + body)
    monkeypatch.setattr(host_egress, "_connect_validated", lambda *args: sock)
    try:
        result = host_egress.guarded_request(
            method, "https://example.com/spec", max_bytes=limit, headers=headers
        )
    finally:
        assert sock.closed
    return result, sock


@pytest.mark.parametrize("coding", ["", "identity", "gzip", "x-gzip", "deflate", "gzip, deflate"])
def test_decodes_content_before_text_and_preserves_exact_bytes(monkeypatch, coding):
    plain = "<p>A specification: café, 日本語, العربية.</p>".encode()
    encoded = plain
    for part in coding.split(", "):
        if part in {"gzip", "x-gzip"}:
            encoded = gzip.compress(encoded)
        elif part == "deflate":
            encoded = zlib.compress(encoded)
    result, sock = request(monkeypatch, encoded, coding)
    assert result.content == plain
    assert result.text == plain.decode()
    assert b"Accept-Encoding: gzip, deflate\r\n" in sock.sent
    assert result.status_code == 200


@pytest.mark.parametrize("coding,compress", [("gzip", gzip.compress), ("deflate", zlib.compress)])
def test_decoded_expansion_keeps_the_existing_fetch_limit(monkeypatch, coding, compress):
    encoded = compress(b"A" * 100000)
    assert len(encoded) < 200
    with pytest.raises(host_egress.EgressDenied, match="decoded response exceeded"):
        request(monkeypatch, encoded, coding, limit=200)


def test_wire_limit_still_applies_before_decoding(monkeypatch):
    with pytest.raises(host_egress.EgressDenied, match="response exceeded"):
        request(monkeypatch, b"A" * 201, limit=200)


@pytest.mark.parametrize(
    "coding,body",
    [
        ("gzip", b"not gzip"),
        ("gzip", gzip.compress(b"complete")[:-3]),
        ("deflate", zlib.compress(b"complete")[:-2]),
        ("br", b"unsupported content"),
    ],
    ids=["invalid-gzip", "truncated-gzip", "truncated-deflate", "unsupported-brotli"],
)
def test_invalid_or_unsupported_encoding_never_becomes_source_text(monkeypatch, coding, body):
    with pytest.raises(OSError):
        request(monkeypatch, body, coding)


def test_concatenated_gzip_members_are_decoded_with_one_total_bound(monkeypatch):
    body = gzip.compress(b"First ") + gzip.compress(b"second.")
    result, _ = request(monkeypatch, body, "gzip")
    assert result.content == b"First second."


@pytest.mark.parametrize("method,status", [("HEAD", 200), ("GET", 204), ("GET", 304)])
def test_bodyless_responses_do_not_decode_metadata_only_encoding(monkeypatch, method, status):
    result, sock = request(
        monkeypatch,
        b"",
        "gzip",
        method=method,
        status=status,
        headers={"accept-encoding": "identity"},
    )
    assert result.content == b""
    assert sock.sent.lower().count(b"accept-encoding:") == 1
    assert b"accept-encoding: identity" in sock.sent
